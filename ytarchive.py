import argparse
import os
import queue
import shutil
import signal
import sys
import tempfile
import threading
import time

# Add the script directory to path for module imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import (
    setup as platform_setup,
    SetLoglevel,
    LOGLEVEL_QUIET, LOGLEVEL_ERROR, LOGLEVEL_INFO, LOGLEVEL_DEBUG, LOGLEVEL_TRACE,
    LogError, LogGeneral, LogInfo, LogWarn,
    InitializeHttpClient, FormatSize, GetFFmpegArgs, Execute,
    TryMove, TryDelete, CleanupFiles, Exists, GetUserInput, GetYesNo,
    DownloadThumbnail, FormatFilename,
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
        SetLoglevel(LOGLEVEL_TRACE)
    elif args.debug:
        SetLoglevel(LOGLEVEL_DEBUG)
    elif args.verbose:
        SetLoglevel(LOGLEVEL_INFO)
    elif args.error:
        SetLoglevel(LOGLEVEL_ERROR)
    elif args.quiet:
        SetLoglevel(LOGLEVEL_QUIET)

    import utils as _u
    _u.status_newlines = args.newline

    # Initialize HTTP client
    proxy = args.proxy if hasattr(args, 'proxy') else None
    InitializeHttpClient(proxy)

    # Transfer args to download info
    di.VP9 = args.vp9
    di.AV1 = args.av1
    di.H264 = args.h264
    di.FragMaxTries = args.retry_frags
    di.MembersOnly = False  # detection happens via cookies
    di.FileMode = args.file_permissions
    di.DirMode = args.directory_permissions
    di.VisitorData = args.visitor_data or ""
    di.PoToken = args.potoken or ""
    di.YtdlpPath = args.ytdlp_path
    di.YtdlpOpts = args.ytdlp_opts

    # Wait/merge/save defaults
    if args.wait:
        di.Wait = ACTION_DO
    elif args.no_wait:
        di.Wait = ACTION_DO_NOT

    if args.merge:
        merge_on_cancel = ACTION_DO
    elif args.no_merge:
        merge_on_cancel = ACTION_DO_NOT

    if args.no_save_state or args.disable_save_state:
        save_state_on_cancel = ACTION_DO_NOT
        di.DisableSaveState = True
    elif args.save_state:
        save_state_on_cancel = ACTION_DO

    if args.no_audio:
        di.VideoOnly = True
    elif args.no_video:
        di.Quality = AUDIO_ONLY_QUALITY
        di.AudioOnly = True

    di.FragFiles = not args.no_frag_files

    # Thread count
    if args.threads > 1:
        di.Jobs = args.threads

    # Monitor channel
    if args.monitor_channel:
        if di.RetrySecs < MINIMUM_MONITOR_TIME:
            di.RetrySecs = DEFAULT_MONITOR_TIME

    # Retry stream
    if args.retry_stream is not None:
        di.RetrySecs = args.retry_stream
        if di.RetrySecs > 0 and di.RetrySecs < DEFAULT_POLL_TIME:
            di.RetrySecs = DEFAULT_POLL_TIME

    # URL and quality from positional args
    url = args.url
    quality = args.quality

    # Handle --video-url / --audio-url (direct Google Video URLs)
    if args.video_url:
        di.URL = args.video_url
        di.SetDownloadUrl(DTYPE_VIDEO, args.video_url)
    if args.audio_url:
        if not di.URL:
            di.URL = args.audio_url
        di.SetDownloadUrl(DTYPE_AUDIO, args.audio_url)

    if args.monitor_channel and not quality:
        LogError("You must specify a channel AND quality when choosing to monitor a channel")
        return 1

    if not di.URL:
        if url and quality:
            di.URL = url
            di.SelectedQuality = quality
        elif url:
            di.URL = url
        else:
            di.URL = GetUserInput("Enter a youtube livestream URL: ")

    # Parse the URL
    if not parse_input_url(di):
        return 1

    # Filename format
    fname_format = args.output
    lookalike_chars = args.lookalike_chars

    # Validate filename format
    try:
        FormatFilename(fname_format, di.FormatInfo, lookalike_chars)
    except Exception as e:
        LogError("%s", str(e))
        return 1

    # Load cookies
    _u.cookie_file = args.cookies or ""
    if _u.cookie_file:
        if not parse_netscape_cookies(di, _u.cookie_file):
            return 1

    # Parse duration options
    if args.start_delay:
        if args.live_from:
            LogError("You cannot use both --start-delay and --live-from at the same time.")
            return 1
        parse_start_delay(di, args.start_delay)

    di.LiveFromVal = args.live_from or ""

    if args.capture_duration:
        parse_capture_duration(di, args.capture_duration)

    # If not a direct Google Video URL, get video info
    if not di.GVideoDDL and not get_video_info(di):
        return 1

    # Parse live-from
    if di.LiveFromVal:
        parse_live_from_str(di)

    # Initialize download states
    di.DLState[AUDIO_ITAG] = DownloadState()
    di.DLState[di.Quality] = DownloadState()

    # Set up output paths
    try:
        full_fpath = FormatFilename(fname_format, di.FormatInfo, lookalike_chars)
    except Exception as e:
        LogError("Error formatting filename: %s", str(e))
        return 1

    fdir = os.path.dirname(full_fpath)
    if fdir and not os.path.isabs(fdir):
        fdir = fdir.lstrip(os.sep)
    if not fdir or not fdir.strip():
        fdir = "."

    fdir = os.path.abspath(fdir)
    os.makedirs(fdir, exist_ok=True)

    fname = os.path.basename(full_fpath)
    fname = fname.lstrip()
    if fname.startswith("-"):
        fname = "_" + fname

    if fname == "." or not fname.strip():
        LogError("Output file name appears to be empty after formatting.")
        LogError("Expanded output file path: %s", full_fpath)
        return 1

    # Temporary directory
    tmp_dir = args.temporary_dir or ""
    if tmp_dir:
        tmp_dir = os.path.abspath(tmp_dir)
    else:
        tmp_dir = tempfile.mkdtemp(prefix="ytarchive_", dir=fdir)

    os.makedirs(tmp_dir, exist_ok=True)

    # Base path for fragments
    base_path = os.path.join(tmp_dir, fname)

    di.SetBaseFilePath(DTYPE_AUDIO, f"{base_path}.f{AUDIO_ITAG}")
    di.SetBaseFilePath(DTYPE_VIDEO, f"{base_path}.f{di.Quality}")

    # Set state files
    audio_state_file = os.path.join(tmp_dir, f"{di.VideoID}.f{AUDIO_ITAG}.state")
    video_state_file = os.path.join(tmp_dir, f"{di.VideoID}.f{di.Quality}.state")
    if AUDIO_ITAG in di.DLState:
        di.DLState[AUDIO_ITAG].File = audio_state_file
        di.DLState[AUDIO_ITAG].TempDir = tmp_dir
    if di.Quality in di.DLState:
        di.DLState[di.Quality].File = video_state_file
        di.DLState[di.Quality].TempDir = tmp_dir

    # Load existing state for resume
    if not di.DisableSaveState:
        di.LoadState(AUDIO_ITAG)
        di.LoadState(di.Quality)

    # File paths
    afile = os.path.join(tmp_dir, f"{fname}.f{AUDIO_ITAG}.ts")
    vfile = os.path.join(tmp_dir, f"{fname}.f{di.Quality}.ts")
    final_audio_file = os.path.join(fdir, f"{fname}.f{AUDIO_ITAG}.ts")
    final_video_file = os.path.join(fdir, f"{fname}.f{di.Quality}.ts")
    thmbnl_file = os.path.join(tmp_dir, f"{fname}.jpg")
    final_thumbnail = os.path.join(fdir, f"{fname}.jpg")
    desc_file = os.path.join(tmp_dir, f"{fname}.description")
    final_desc_file = os.path.join(fdir, f"{fname}.description")
    mux_file = os.path.join(tmp_dir, f"{fname}.ffmpeg.txt")
    final_mux_file = os.path.join(fdir, f"{fname}.ffmpeg.txt")

    # Write thumbnail and description
    if args.write_thumbnail and di.Thumbnail:
        LogGeneral("Downloading thumbnail...")
        DownloadThumbnail(di.Thumbnail, thmbnl_file, di.FileMode)

    if args.write_description:
        try:
            with open(desc_file, "w", encoding="utf-8") as f:
                f.write(di.FormatInfo.get("description", ""))
        except Exception as e:
            LogWarn("Failed to write description file: %s", str(e))

    # Start downloads
    progress_queue = queue.Queue()
    dl_done_events = []
    active_downloads = 0
    cancelled = False

    if not di.VideoOnly and di.GetDownloadUrl(DTYPE_AUDIO):
        LogInfo("Starting audio download to %s", afile)
        done_event = threading.Event()
        dl_done_events.append(done_event)
        active_downloads += 1
        t = threading.Thread(
            target=download_stream,
            args=(di, DTYPE_AUDIO, afile, progress_queue, done_event),
            daemon=True,
        )
        t.start()

    if not di.AudioOnly and di.GetDownloadUrl(DTYPE_VIDEO):
        LogInfo("Starting video download to %s", vfile)
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
        LogError("Neither audio nor video downloads were started.")
        LogError("Make sure you did not have both --no-video and --no-audio set.")
        if tmp_dir != fdir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return 1

    # Set up signal handler
    sig_received = [False]

    def sig_handler(signum, frame):
        if not sig_received[0]:
            sig_received[0] = True
            di.Stop()
            nonlocal cancelled
            cancelled = True
            sys.stderr.write("\n")
            sys.stderr.flush()
            LogWarn("User Interrupt, Stopping download...")

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

        if progress.Itag in di.DLState:
            di.DLState[progress.Itag].Size += progress.ByteCount
            di.DLState[progress.Itag].Fragments += 1
        total_bytes += progress.ByteCount
        di.SaveState(progress.Itag)

        if progress.MaxSeq > max_seq:
            max_seq = progress.MaxSeq

        status = "\r" if not _u.status_newlines else ""
        video_frags = di.DLState.get(di.Quality, DownloadState()).Fragments
        audio_frags = di.DLState.get(AUDIO_ITAG, DownloadState()).Fragments
        status += f"Video Fragments: {video_frags}; Audio Fragments: {audio_frags}; "
        if args.verbose:
            status += f"Max Fragments: {max_seq - progress.StartFrag if max_seq > -1 else '?'}; Max Sequence: {max_seq}; "
        status += f"Total Downloaded: {FormatSize(total_bytes)}"
        if _u.status_newlines:
            status += "\n"
        else:
            status += "\033[K"

        di.SetStatus(status)
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
            merge = GetYesNo("\nDownload stopped prematurely. Would you like to merge the currently downloaded data?")
        elif merge_on_cancel == ACTION_DO:
            merge = True

        if not merge:
            save_files = False
            save_state = False

            if save_files_on_cancel == ACTION_ASK:
                save_files = GetYesNo("\nWould you like to save any created files?")
            elif save_files_on_cancel == ACTION_DO:
                save_files = True

            if not save_files:
                if save_state_on_cancel == ACTION_ASK:
                    save_state = GetYesNo("\nWould you like to leave files to resume downloading later?")
                elif save_state_on_cancel == ACTION_DO:
                    save_state = True

            if save_files:
                TryMove(afile, final_audio_file)
                TryMove(vfile, final_video_file)
                TryMove(thmbnl_file, final_thumbnail)
                TryMove(desc_file, final_desc_file)

                if not di.DisableSaveState:
                    for state in di.DLState.values():
                        TryDelete(state.File)

                if tmp_dir != fdir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
            elif not save_state:
                if tmp_dir != fdir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                if not di.DisableSaveState:
                    for state in di.DLState.values():
                        TryDelete(state.File)

            return 2

    # Download completed normally
    if not di.DisableSaveState:
        for state in di.DLState.values():
            TryDelete(state.File)

    if _u.loglevel > LOGLEVEL_QUIET:
        sys.stderr.write("\n")
        sys.stderr.flush()

    LogGeneral("Download Finished")

    # Warn if fragment counts mismatch
    audio_frags = di.DLState.get(AUDIO_ITAG, DownloadState()).Fragments
    video_frags = di.DLState.get(di.Quality, DownloadState()).Fragments
    if not di.AudioOnly and not di.VideoOnly and audio_frags != video_frags:
        LogWarn("Mismatched number of video and audio fragments.")
        LogWarn("The files should still be mergeable but data might be missing.")

    # Move files from tmp to final
    moves_ok = True
    for err in [
        TryMove(afile, final_audio_file),
        TryMove(vfile, final_video_file),
        TryMove(thmbnl_file, final_thumbnail),
        TryMove(desc_file, final_desc_file),
        TryMove(mux_file, final_mux_file),
    ]:
        if err:
            moves_ok = False

    files_to_del = [final_mux_file]
    if not args.keep_ts_files:
        files_to_del.extend([final_audio_file, final_video_file])
    if not args.write_thumbnail:
        files_to_del.append(final_thumbnail)

    # Build ffmpeg args
    ffmpeg_args = GetFFmpegArgs(
        audio_file=final_audio_file,
        video_file=final_video_file,
        thumbnail=final_thumbnail,
        file_dir=fdir,
        file_name=fname,
        only_audio=di.AudioOnly,
        only_video=di.VideoOnly,
        download_thumbnail=args.write_thumbnail and Exists(final_thumbnail),
        mkv=args.mkv,
        add_meta=args.add_metadata,
        metadata=di.Metadata,
    )

    # Write mux command file
    if args.write_mux_file:
        try:
            with open(final_mux_file, "w") as mf:
                mf.write(f"{args.ffmpeg_path} {' '.join(ffmpeg_args['args'])}\n")
        except Exception as e:
            LogWarn("Failed to write mux file: %s", str(e))

        if not moves_ok:
            LogError("At least one error occurred when moving files. Will not delete them.")
        elif tmp_dir != fdir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return 0

    # Check ffmpeg availability
    ffmpeg_path = args.ffmpeg_path
    if not shutil.which(ffmpeg_path):
        LogError("%s not found. Please install ffmpeg or provide a location using --ffmpeg-path", ffmpeg_path)
        if not moves_ok:
            LogError("At least one error occurred when moving files. Will not delete them.")
        elif tmp_dir != fdir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return 1

    # Mux with ffmpeg
    LogGeneral("Muxing final file...")
    retcode = Execute(ffmpeg_path, ffmpeg_args["args"])
    if retcode != 0:
        LogError("Execute returned code %d. Something must have gone wrong with ffmpeg.", retcode)
        LogError("The .ts files will not be deleted in case the final file is broken.")
        LogError("Finally, the ffmpeg command was either written to a file or output above.")

    # Separate audio
    if args.separate_audio:
        LogGeneral("Creating separate audio file...")
        audio_ffmpeg_args = GetFFmpegArgs(
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
            metadata=di.Metadata,
        )
        a_retcode = Execute(ffmpeg_path, audio_ffmpeg_args["args"])
        if a_retcode != 0:
            retcode = a_retcode
            LogError("Execute returned code %d. Something must have gone wrong with ffmpeg.", retcode)
            LogError("The .ts files will not be deleted in case the final file is broken.")

    if not moves_ok:
        LogError("At least one error occurred when moving files. Will not delete them.")
    elif tmp_dir != fdir:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if retcode != 0:
        return retcode

    CleanupFiles(files_to_del)

    LogGeneral("%sFinal file: %s%s", "\n", ffmpeg_args["file_name"], "\n")
    if args.separate_audio:
        LogGeneral("%sFinal audio file: %s%s", "\n", audio_ffmpeg_args["file_name"], "\n")

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
                di_temp.Metadata[key.strip()] = value.strip()

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
