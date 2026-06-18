import argparse
from pathlib import Path
import queue
import shutil
import signal
import sys
import tempfile
import threading
import time
from utils import (
    setup as platform_setup,
    set_log_level,
    LOGLEVEL_QUIET, LOGLEVEL_ERROR, LOGLEVEL_INFO, LOGLEVEL_DEBUG, LOGLEVEL_TRACE,
    log_error, log_general, log_info, log_warn,
    initialize_http_client, format_size, get_ffmpeg_args, execute,
    try_move, try_delete, cleanup_files, exists, get_user_input, get_yes_no,
    download_thumbnail, format_filename,
    ACTION_ASK, ACTION_DO, ACTION_DO_NOT,
    DTYPE_AUDIO, DTYPE_VIDEO, AUDIO_ITAG, AUDIO_ONLY_QUALITY,
    DEFAULT_FILENAME_FORMAT, DEFAULT_POLL_TIME, MINIMUM_MONITOR_TIME, DEFAULT_MONITOR_TIME,
)

from download import (
    DownloadInfo, DownloadState, parse_input_url, parse_live_from_str, parse_capture_duration,
    parse_start_delay, get_video_info, download_stream,
    parse_netscape_cookies,
)

# Module-level state for the run() function
fname_format = DEFAULT_FILENAME_FORMAT
lookalike_chars = False


def build_argparser() -> argparse.ArgumentParser:
    """Build the argument parser matching the Go version's CLI flags."""
    parser = argparse.ArgumentParser(
        prog="ytarchive",
        description="Archive a given YouTube livestream from the start.",
        epilog="If [url] is not provided, you will be prompted to enter one. "
               "[quality] is a slash-delimited list of video qualities.",
    )

    # Positional arguments
    parser.add_argument("url", nargs="?", help="YouTube livestream URL")
    parser.add_argument("quality", nargs="?", help="Video quality (slash-delimited)")

    # Help and version
    parser.add_argument("--version", action="store_true", help="Show version and exit")

    # Network options
    net_group = parser.add_argument_group("Network Options")
    net_group.add_argument("--proxy", help="Proxy URL (http, https, socks5)")
    net_group.add_argument("-c", "--cookies", help="Netscape-format cookies.txt file")
    net_group.add_argument("--visitor-data", help="Visitor data for API requests")
    net_group.add_argument("--potoken", help="PO Token for authenticated API requests")

    # Output options
    out_group = parser.add_argument_group("Output Options")
    out_group.add_argument("-o", "--output", default=DEFAULT_FILENAME_FORMAT,
                           help="Output file name format (default: %%(title)s-%%(id)s)")
    out_group.add_argument("--temporary-dir", help="Directory for temporary files")
    out_group.add_argument("--directory-permissions", type=lambda x: int(x, 8), default=0o755,
                           help="Directory permissions in octal (default: 755)")
    out_group.add_argument("--file-permissions", type=lambda x: int(x, 8), default=0o644,
                           help="File permissions in octal (default: 644)")
    out_group.add_argument("--thumbnail", "--write-thumbnail", action="store_true",
                           dest="write_thumbnail", help="Download and embed thumbnail")
    out_group.add_argument("--write-description", action="store_true",
                           help="Write video description to a .description file")
    out_group.add_argument("--write-mux-file", action="store_true",
                           help="Write the ffmpeg mux command to a file instead of running it")
    out_group.add_argument("--keep-ts-files", action="store_true",
                           help="Keep the raw .ts files after muxing")
    out_group.add_argument("--separate-audio", action="store_true",
                           help="Create a separate audio-only file")
    out_group.add_argument("--mkv", action="store_true",
                           help="Mux into MKV instead of MP4")
    out_group.add_argument("--no-frag-files", action="store_true",
                           help="Keep fragments in memory instead of writing to disk")
    out_group.add_argument("--add-metadata", action="store_true",
                           help="Write metadata to the final file")
    out_group.add_argument("--metadata", action="append",
                           help="Add custom metadata KEY=VALUE (can be used multiple times)")
    out_group.add_argument("--lookalike-chars", action="store_true",
                           help="Use Unicode lookalike chars instead of underscores in filenames")

    # Video selection
    vid_group = parser.add_argument_group("Video Selection")
    vid_group.add_argument("--vp9", action="store_true", help="Prefer VP9 codec")
    vid_group.add_argument("--av1", action="store_true", help="Prefer AV1 codec")
    vid_group.add_argument("--h264", action="store_true", help="Prefer H264 codec")
    vid_group.add_argument("--no-video", action="store_true", help="Download audio only")
    vid_group.add_argument("--no-audio", action="store_true", help="Download video only")
    vid_group.add_argument("--video-url", help="Direct Google Video URL for video fragments")
    vid_group.add_argument("--audio-url", help="Direct Google Video URL for audio fragments (itag=140)")

    # Download control
    dl_group = parser.add_argument_group("Download Control")
    dl_group.add_argument("--threads", type=int, default=1,
                          help="Number of download threads per stream (default: 1)")
    dl_group.add_argument("--retry-stream", type=int,
                          help="Retry interval in seconds for waiting/polling a stream")
    dl_group.add_argument("--retry-frags", type=int, default=10,
                          help="Max retries per fragment (0=infinite, default: 10)")
    dl_group.add_argument("--live-from", help="Start downloading from a specific time (e.g., -1h30m, 15:00, now)")
    dl_group.add_argument("--start-delay", help="Wait this long before starting download")
    dl_group.add_argument("--capture-duration", help="Download this much content and then exit")
    dl_group.add_argument("--no-wait", action="store_true", help="Don't wait for a scheduled stream")
    dl_group.add_argument("--wait", action="store_true", help="Wait for a scheduled stream")
    dl_group.add_argument("--monitor-channel", action="store_true",
                          help="Monitor a channel URL and download new streams as they appear")

    # Merge/cancel behavior
    merge_group = parser.add_argument_group("Merge/Cancel Options")
    merge_group.add_argument("--merge", action="store_true",
                             help="Automatically merge on cancel")
    merge_group.add_argument("--no-merge", action="store_true",
                             help="Don't merge on cancel")
    merge_group.add_argument("--no-save-state", action="store_true",
                             dest="no_save_state", help="Disable saving download state")
    merge_group.add_argument("--disable-save-state", action="store_true",
                             help="Disable saving download state (alias)")
    merge_group.add_argument("--save-state", action="store_true",
                             help="Save download state on cancel")

    # Logging
    log_group = parser.add_argument_group("Logging Options")
    log_group.add_argument("--quiet", action="store_true", help="Suppress all output")
    log_group.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    log_group.add_argument("--debug", action="store_true", help="Debug output")
    log_group.add_argument("--trace", action="store_true", help="Very verbose debug output")
    log_group.add_argument("--error", action="store_true", help="Show only errors")
    log_group.add_argument("--newline", action="store_true",
                           help="Use newline instead of carriage return for progress")

    # External tools
    ext_group = parser.add_argument_group("External Tools")
    ext_group.add_argument("--ytdlp-path", default="yt-dlp",
                           help="Path to yt-dlp executable (default: yt-dlp)")
    ext_group.add_argument("--ytdlp-opts", default="",
                           help="Additional options to pass to yt-dlp")
    ext_group.add_argument("--ffmpeg-path", default="ffmpeg",
                           help="Path to ffmpeg executable (default: ffmpeg)")

    return parser


def run(args: argparse.Namespace) -> int:
    """Main download orchestration. Returns exit code (0 success, 1 error, 2 cancelled)."""
    global fname_format, lookalike_chars

    di = DownloadInfo()
    merge_on_cancel = ACTION_ASK
    save_files_on_cancel = ACTION_ASK
    save_state_on_cancel = ACTION_ASK
    move_errs = []

    # Configure logging
    if args.trace:
        set_log_level(LOGLEVEL_TRACE)
    elif args.debug:
        set_log_level(LOGLEVEL_DEBUG)
    elif args.verbose:
        set_log_level(LOGLEVEL_INFO)
    elif args.error:
        set_log_level(LOGLEVEL_ERROR)
    elif args.quiet:
        set_log_level(LOGLEVEL_QUIET)

    import utils as _u
    _u.status_newlines = args.newline

    # Initialize HTTP client
    proxy = args.proxy if hasattr(args, 'proxy') else None
    initialize_http_client(proxy)

    # Transfer args to download info
    di.vp9 = args.vp9
    di.av1 = args.av1
    di.h264 = args.h264
    di.frag_max_tries = args.retry_frags
    di.members_only = False  # detection happens via cookies
    di.file_mode = args.file_permissions
    di.dir_mode = args.directory_permissions
    di.visitor_data = args.visitor_data or ""
    di.po_token = args.potoken or ""
    di.ytdlp_path = args.ytdlp_path
    di.ytdlp_opts = args.ytdlp_opts

    # Wait/merge/save defaults
    if args.wait:
        di.wait = ACTION_DO
    elif args.no_wait:
        di.wait = ACTION_DO_NOT

    if args.merge:
        merge_on_cancel = ACTION_DO
    elif args.no_merge:
        merge_on_cancel = ACTION_DO_NOT

    if args.no_save_state or args.disable_save_state:
        save_state_on_cancel = ACTION_DO_NOT
        di.disable_save_state = True
    elif args.save_state:
        save_state_on_cancel = ACTION_DO

    if args.no_audio:
        di.video_only = True
    elif args.no_video:
        di.quality = AUDIO_ONLY_QUALITY
        di.audio_only = True

    di.frag_files = not args.no_frag_files

    # Thread count
    if args.threads > 1:
        di.jobs = args.threads

    # Monitor channel
    if args.monitor_channel:
        if di.retry_secs < MINIMUM_MONITOR_TIME:
            di.retry_secs = DEFAULT_MONITOR_TIME

    # Retry stream
    if args.retry_stream is not None:
        di.retry_secs = args.retry_stream
        if di.retry_secs > 0 and di.retry_secs < DEFAULT_POLL_TIME:
            di.retry_secs = DEFAULT_POLL_TIME

    # URL and quality from positional args
    url = args.url
    quality = args.quality

    # Handle --video-url / --audio-url (direct Google Video URLs)
    if args.video_url:
        di.url = args.video_url
        di.set_download_url(DTYPE_VIDEO, args.video_url)
    if args.audio_url:
        if not di.url:
            di.url = args.audio_url
        di.set_download_url(DTYPE_AUDIO, args.audio_url)

    if args.monitor_channel and not quality:
        log_error("You must specify a channel AND quality when choosing to monitor a channel")
        return 1

    if not di.url:
        if url and quality:
            di.url = url
            di.selected_quality = quality
        elif url:
            di.url = url
        else:
            di.url = get_user_input("Enter a youtube livestream URL: ")

    # Parse the URL
    if not parse_input_url(di):
        return 1

    # Filename format
    fname_format = args.output
    lookalike_chars = args.lookalike_chars

    # Validate filename format
    try:
        format_filename(fname_format, di.format_info, lookalike_chars)
    except Exception as e:
        log_error("%s", str(e))
        return 1

    # Load cookies
    _u.cookie_file = args.cookies or ""
    if _u.cookie_file:
        if not parse_netscape_cookies(di, _u.cookie_file):
            return 1

    # Parse duration options
    if args.start_delay:
        if args.live_from:
            log_error("You cannot use both --start-delay and --live-from at the same time.")
            return 1
        parse_start_delay(di, args.start_delay)

    di.live_from_val = args.live_from or ""

    if args.capture_duration:
        parse_capture_duration(di, args.capture_duration)

    # If not a direct Google Video URL, get video info
    if not di.g_video_ddl and not get_video_info(di):
        return 1

    # Parse live-from
    if di.live_from_val:
        parse_live_from_str(di)

    # Initialize download states
    di.dl_state[AUDIO_ITAG] = DownloadState()
    di.dl_state[di.quality] = DownloadState()

    # Set up output paths
    try:
        full_fpath = Path(format_filename(fname_format, di.format_info, lookalike_chars))
    except Exception as e:
        log_error("Error formatting filename: %s", str(e))
        return 1

    fdir = full_fpath.parent.resolve()
    fdir.mkdir(parents=True, exist_ok=True)

    fname = full_fpath.name
    fname = fname.lstrip()
    if fname.startswith("-"):
        fname = "_" + fname

    if fname == "." or not fname.strip():
        log_error("Output file name appears to be empty after formatting.")
        log_error("Expanded output file path: %s", full_fpath)
        return 1

    # Temporary directory
    tmp_dir = args.temporary_dir or ""
    if tmp_dir:
        tmp_dir = Path(tmp_dir).resolve()
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="ytarchive_", dir=fdir))

    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Base path for fragments
    base_path = tmp_dir / fname

    di.set_base_file_path(DTYPE_AUDIO, f"{base_path}.f{AUDIO_ITAG}")
    di.set_base_file_path(DTYPE_VIDEO, f"{base_path}.f{di.quality}")

    # Set state files
    audio_state_file = str(tmp_dir / f"{di.video_id}.f{AUDIO_ITAG}.state")
    video_state_file = str(tmp_dir / f"{di.video_id}.f{di.quality}.state")
    if AUDIO_ITAG in di.dl_state:
        di.dl_state[AUDIO_ITAG].file_path = audio_state_file
        di.dl_state[AUDIO_ITAG].temp_dir = str(tmp_dir)
    if di.quality in di.dl_state:
        di.dl_state[di.quality].file_path = video_state_file
        di.dl_state[di.quality].temp_dir = str(tmp_dir)

    # Load existing state for resume
    if not di.disable_save_state:
        di.load_state(AUDIO_ITAG)
        di.load_state(di.quality)

    # File paths
    afile = tmp_dir / f"{fname}.f{AUDIO_ITAG}.ts"
    vfile = tmp_dir / f"{fname}.f{di.quality}.ts"
    final_audio_file = fdir / f"{fname}.f{AUDIO_ITAG}.ts"
    final_video_file = fdir / f"{fname}.f{di.quality}.ts"
    thmbnl_file = tmp_dir / f"{fname}.jpg"
    final_thumbnail = fdir / f"{fname}.jpg"
    desc_file = tmp_dir / f"{fname}.description"
    final_desc_file = fdir / f"{fname}.description"
    mux_file = tmp_dir / f"{fname}.ffmpeg.txt"
    final_mux_file = fdir / f"{fname}.ffmpeg.txt"

    # Write thumbnail and description
    if args.write_thumbnail and di.thumbnail:
        log_general("Downloading thumbnail...")
        download_thumbnail(di.thumbnail, thmbnl_file, di.file_mode)

    if args.write_description:
        try:
            with open(desc_file, "w", encoding="utf-8") as f:
                f.write(di.format_info.get("description", ""))
        except Exception as e:
            log_warn("Failed to write description file: %s", str(e))

    # Start downloads
    progress_queue = queue.Queue()
    dl_done_events = []
    active_downloads = 0
    cancelled = False

    if not di.video_only and di.get_download_url(DTYPE_AUDIO):
        log_info("Starting audio download to %s", afile)
        done_event = threading.Event()
        dl_done_events.append(done_event)
        active_downloads += 1
        t = threading.Thread(
            target=download_stream,
            args=(di, DTYPE_AUDIO, afile, progress_queue, done_event),
            daemon=True,
        )
        t.start()

    if not di.audio_only and di.get_download_url(DTYPE_VIDEO):
        log_info("Starting video download to %s", vfile)
        done_event = threading.Event()
        dl_done_events.append(done_event)
        active_downloads += 1
        t = threading.Thread(
            target=download_stream,
            args=(di, DTYPE_VIDEO, vfile, progress_queue, done_event),
            daemon=True,
        )
        t.start()

    if active_downloads == 0:
        log_error("Neither audio nor video downloads were started.")
        log_error("Make sure you did not have both --no-video and --no-audio set.")
        if tmp_dir != fdir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return 1

    # Set up signal handler
    sig_received = [False]

    def sig_handler(signum, frame):
        if not sig_received[0]:
            sig_received[0] = True
            di.stop()
            nonlocal cancelled
            cancelled = True
            sys.stderr.write("\n")
            sys.stderr.flush()
            log_warn("User Interrupt, Stopping download...")

    original_sigint = signal.signal(signal.SIGINT, sig_handler)

    # Progress monitoring
    max_seq = -1
    total_bytes = 0

    while active_downloads > 0:
        try:
            progress = progress_queue.get(timeout=0.5)
        except queue.Empty:
            # Check if all download threads are done
            for ev in list(dl_done_events):
                if ev.is_set():
                    active_downloads -= 1
                    dl_done_events.remove(ev)
            continue

        if progress.itag in di.dl_state:
            di.dl_state[progress.itag].size += progress.byte_count
            di.dl_state[progress.itag].fragments += 1
        total_bytes += progress.byte_count
        di.save_state(progress.itag)

        if progress.max_seq > max_seq:
            max_seq = progress.max_seq

        status = "\r" if not _u.status_newlines else ""
        video_frags = di.dl_state.get(di.quality, DownloadState()).fragments
        audio_frags = di.dl_state.get(AUDIO_ITAG, DownloadState()).fragments
        status += f"Video Fragments: {video_frags}; Audio Fragments: {audio_frags}; "
        if args.verbose:
            status += f"Max Fragments: {max_seq - progress.start_frag if max_seq > -1 else '?'}; Max Sequence: {max_seq}; "
        status += f"Total Downloaded: {format_size(total_bytes)}"
        if _u.status_newlines:
            status += "\n"
        else:
            status += "\033[K"

        di.set_status(status)
        sys.stderr.write(status)
        sys.stderr.flush()

        # Check if any download threads have finished
        for ev in list(dl_done_events):
            if ev.is_set():
                active_downloads -= 1
                dl_done_events.remove(ev)

    # Reset signal handler
    signal.signal(signal.SIGINT, original_sigint)

    # Handle cancelled download
    if cancelled:
        merge = False
        if merge_on_cancel == ACTION_ASK:
            merge = get_yes_no("\nDownload stopped prematurely. Would you like to merge the currently downloaded data?")
        elif merge_on_cancel == ACTION_DO:
            merge = True

        if not merge:
            save_files = False
            save_state = False

            if save_files_on_cancel == ACTION_ASK:
                save_files = get_yes_no("\nWould you like to save any created files?")
            elif save_files_on_cancel == ACTION_DO:
                save_files = True

            if not save_files:
                if save_state_on_cancel == ACTION_ASK:
                    save_state = get_yes_no("\nWould you like to leave files to resume downloading later?")
                elif save_state_on_cancel == ACTION_DO:
                    save_state = True

            if save_files:
                try_move(afile, final_audio_file)
                try_move(vfile, final_video_file)
                try_move(thmbnl_file, final_thumbnail)
                try_move(desc_file, final_desc_file)

                if not di.disable_save_state:
                    for state in di.dl_state.values():
                        try_delete(state.file_path)

                if tmp_dir != fdir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
            elif not save_state:
                if tmp_dir != fdir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                if not di.disable_save_state:
                    for state in di.dl_state.values():
                        try_delete(state.file_path)

            return 2

    # Download completed normally
    if not di.disable_save_state:
        for state in di.dl_state.values():
            try_delete(state.file_path)

    if _u.loglevel > LOGLEVEL_QUIET:
        sys.stderr.write("\n")
        sys.stderr.flush()

    log_general("Download Finished")

    # Warn if fragment counts mismatch
    audio_frags = di.dl_state.get(AUDIO_ITAG, DownloadState()).fragments
    video_frags = di.dl_state.get(di.quality, DownloadState()).fragments
    if not di.audio_only and not di.video_only and audio_frags != video_frags:
        log_warn("Mismatched number of video and audio fragments.")
        log_warn("The files should still be mergeable but data might be missing.")

    # Move files from tmp to final
    moves_ok = True
    for err in [
        try_move(afile, final_audio_file),
        try_move(vfile, final_video_file),
        try_move(thmbnl_file, final_thumbnail),
        try_move(desc_file, final_desc_file),
        try_move(mux_file, final_mux_file),
    ]:
        if err:
            moves_ok = False

    files_to_del = [final_mux_file]
    if not args.keep_ts_files:
        files_to_del.extend([final_audio_file, final_video_file])
    if not args.write_thumbnail:
        files_to_del.append(final_thumbnail)

    # Build ffmpeg args
    ffmpeg_args = get_ffmpeg_args(
        audio_file=final_audio_file,
        video_file=final_video_file,
        thumbnail=final_thumbnail,
        file_dir=fdir,
        file_name=fname,
        only_audio=di.audio_only,
        only_video=di.video_only,
        download_thumbnail=args.write_thumbnail and exists(final_thumbnail),
        mkv=args.mkv,
        add_meta=args.add_metadata,
        metadata=di.metadata,
    )

    # Write mux command file
    if args.write_mux_file:
        try:
            with open(final_mux_file, "w") as mf:
                mf.write(f"{args.ffmpeg_path} {' '.join(ffmpeg_args['args'])}\n")
        except Exception as e:
            log_warn("Failed to write mux file: %s", str(e))

        if not moves_ok:
            log_error("At least one error occurred when moving files. Will not delete them.")
        elif tmp_dir != fdir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return 0

    # Check ffmpeg availability
    ffmpeg_path = args.ffmpeg_path
    if not shutil.which(ffmpeg_path):
        log_error("%s not found. Please install ffmpeg or provide a location using --ffmpeg-path", ffmpeg_path)
        if not moves_ok:
            log_error("At least one error occurred when moving files. Will not delete them.")
        elif tmp_dir != fdir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return 1

    # Mux with ffmpeg
    log_general("Muxing final file...")
    retcode = execute(ffmpeg_path, ffmpeg_args["args"])
    if retcode != 0:
        log_error("Execute returned code %d. Something must have gone wrong with ffmpeg.", retcode)
        log_error("The .ts files will not be deleted in case the final file is broken.")
        log_error("Finally, the ffmpeg command was either written to a file or output above.")

    # Separate audio
    if args.separate_audio:
        log_general("Creating separate audio file...")
        audio_ffmpeg_args = get_ffmpeg_args(
            audio_file=final_audio_file,
            video_file="",
            thumbnail="",
            file_dir=fdir,
            file_name=fname,
            only_audio=True,
            only_video=False,
            download_thumbnail=False,
            mkv=False,
            add_meta=args.add_metadata,
            metadata=di.metadata,
        )
        a_retcode = execute(ffmpeg_path, audio_ffmpeg_args["args"])
        if a_retcode != 0:
            retcode = a_retcode
            log_error("Execute returned code %d. Something must have gone wrong with ffmpeg.", retcode)
            log_error("The .ts files will not be deleted in case the final file is broken.")

    if not moves_ok:
        log_error("At least one error occurred when moving files. Will not delete them.")
    elif tmp_dir != fdir:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if retcode != 0:
        return retcode

    cleanup_files(files_to_del)

    log_general("%sFinal file: %s%s", "\n", ffmpeg_args["file_name"], "\n")
    if args.separate_audio:
        log_general("%sFinal audio file: %s%s", "\n", audio_ffmpeg_args["file_name"], "\n")

    return 0


def main():
    """Entry point."""
    platform_setup()

    parser = build_argparser()
    args = parser.parse_args()

    if args.version:
        print("ytarchive (Python rewrite)")
        print("Based on github.com/dreammu/ytarchive")
        sys.exit(0)

    # Handle --metadata accumulation
    if args.metadata:
        di_temp = DownloadInfo()
        for m in args.metadata:
            if "=" in m:
                key, value = m.split("=", 1)
                di_temp.metadata[key.strip()] = value.strip()

    # Monitor channel loop
    if args.monitor_channel:
        retry_secs = args.retry_stream or DEFAULT_MONITOR_TIME
        last_exit_time = 0.0

        while True:
            retcode = run(args)
            if retcode == 0:
                last_exit_time = time.time()
            elif retcode == 1:
                # Error - sleep retry interval
                pass

            # Sleep if last exit was too recent
            elapsed = time.time() - last_exit_time
            if elapsed < retry_secs:
                time.sleep(retry_secs - elapsed)
    else:
        retcode = run(args)
        sys.exit(retcode)


if __name__ == "__main__":
    main()
