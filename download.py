import json
import math
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Queue, Empty
from typing import Optional
from urllib.parse import urlparse, parse_qs

from utils import (
    DTYPE_AUDIO, DTYPE_VIDEO, AUDIO_ITAG, AUDIO_ONLY_QUALITY,
    DEFAULT_POLL_TIME, DEFAULT_THREADS,
    DEFAULT_FRAG_MAX_TRIES, LIVE_MAXIMUM_SEEKABLE, ACTION_ASK,
    log_debug, log_error, log_general, log_info, log_warn,
    seconds_to_duration_and_time_str, get_yes_no,
    try_delete, remove_atoms, is_fragmented,
    video_qualities, video_label_itags, contains,
    parse_quality_selection, get_quality_from_user,
    session,
)



# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

class FormatInfo(dict):
    """Dictionary for filename format template values."""
    pass


class MetaInfo(dict):
    """Metadata for the final muxed file."""
    pass


@dataclass
class Fragment:
    """A single downloaded fragment."""
    seq: int
    file_name: str = ""
    x_head_seq_num: int = -1
    data: Optional[bytearray] = None
    slow: bool = False
    mime_type: str = ""


@dataclass
class ProgressInfo:
    """Progress info sent from download thread to main thread."""
    itag: int
    byte_count: int
    max_seq: int
    start_frag: int


@dataclass
class SeqChanInfo:
    """Information sent through the sequence channel."""
    cur_sequence: int
    max_sequence: int


@dataclass
class FragThreadState:
    """State shared between fragment download functions."""
    name: str
    base_file_path: str
    data_type: str
    seq_num: int = 0
    max_seq: int = -1
    tries: int = 0
    full_retries: int = 3
    is_403: bool = False
    to_file: bool = True
    sleep_time: float = 5.0


class MediaDLInfo:
    """Per-stream download info with thread-safe access."""
    def __init__(self):
        self._lock = threading.RLock()
        self._active_jobs = 0
        self._download_url = ""
        self._base_path = ""
        self._data_type = ""
        self._finished = False
        self._url_host = ""

    @property
    def active_jobs(self):
        with self._lock:
            return self._active_jobs

    @active_jobs.setter
    def active_jobs(self, val):
        with self._lock:
            self._active_jobs = val

    @property
    def download_url(self):
        with self._lock:
            return self._download_url

    @download_url.setter
    def download_url(self, val):
        with self._lock:
            self._download_url = val

    @property
    def base_path(self):
        with self._lock:
            return self._base_path

    @base_path.setter
    def base_path(self, val):
        with self._lock:
            self._base_path = val

    @property
    def data_type(self):
        with self._lock:
            return self._data_type

    @data_type.setter
    def data_type(self, val):
        with self._lock:
            self._data_type = val

    @property
    def finished(self):
        with self._lock:
            return self._finished

    @finished.setter
    def finished(self, val):
        with self._lock:
            self._finished = val

    @property
    def url_host(self):
        with self._lock:
            return self._url_host

    @url_host.setter
    def url_host(self, val):
        with self._lock:
            self._url_host = val


@dataclass
class DownloadState:
    """State for resumable downloading."""
    start_frag: int = 0
    fragments: int = 0
    size: int = 0
    temp_dir: str = ""
    file_path: str = ""


class DownloadInfo:
    """Central state for the download process."""

    def __init__(self):
        self._lock = threading.RLock()

        # Format info
        self.format_info = self.new_format_info()
        self.metadata = self.new_meta_info()
        self.cookies_url = None
        self.visitor_data = ""
        self.po_token = ""

        # State flags
        self.stopping = False
        self.in_progress = False
        self.live = False
        self.vp9 = False
        self.h264 = False
        self.av1 = False
        self.unavailable = False
        self.g_video_ddl = False
        self.frag_files = True
        self.live_url = False
        self.audio_only = False
        self.video_only = False
        self.members_only = False
        self.info_printed = False
        self.disable_save_state = False

        # Stream info
        self.thumbnail = ""
        self.video_id = ""
        self.url = ""
        self.selected_quality = ""
        self.status = ""
        self.live_from_val = ""
        self.ytdlp_path = "yt-dlp"
        self.ytdlp_opts = ""

        # Numeric settings
        self.frag_max_tries = DEFAULT_FRAG_MAX_TRIES
        self.wait = ACTION_ASK
        self.quality = -1
        self.retry_secs = 0
        self.jobs = DEFAULT_THREADS
        self.target_duration = 5
        self.last_sq = -1
        self.live_from_sq = 0
        self.capture_duration_secs = 0
        self.start_delay_secs = 0
        self.last_updated = 0.0

        # Download state
        self.mdl_info = {
            DTYPE_VIDEO: MediaDLInfo(),
            DTYPE_AUDIO: MediaDLInfo(),
        }
        self.dl_state = {}

        # File modes
        self.file_mode = 0o644
        self.dir_mode = 0o755

    @staticmethod
    def new_format_info() -> FormatInfo:
        return FormatInfo({
            "id": "",
            "title": "",
            "channel_id": "",
            "channel": "",
            "upload_date": "",
            "start_date": "",
            "year": "",
            "month": "",
            "day": "",
            "start_time": "",
            "hours": "",
            "minutes": "",
            "seconds": "",
            "publish_date": "",
            "description": "",
            "url": "",
        })

    @staticmethod
    def new_meta_info() -> MetaInfo:
        return MetaInfo({
            "title": "%(title)s",
            "artist": "%(channel)s",
            "date": "%(upload_date)s",
            "comment": "%(url)s\n\n%(description)s",
        })

    # Thread-safe property accessors

    def is_stopping(self) -> bool:
        with self._lock:
            return self.stopping

    def stop(self):
        with self._lock:
            self.stopping = True
            self.set_finished(DTYPE_AUDIO)
            self.set_finished(DTYPE_VIDEO)

    def is_live(self) -> bool:
        with self._lock:
            return self.live

    def is_unavailable(self) -> bool:
        with self._lock:
            return self.unavailable

    def is_g_video_ddl(self) -> bool:
        with self._lock:
            return self.g_video_ddl

    def is_finished(self, data_type: str) -> bool:
        with self._lock:
            return self.mdl_info[data_type].finished

    def set_finished(self, data_type: str):
        with self._lock:
            self.mdl_info[data_type].finished = True

    def get_download_url(self, data_type: str) -> str:
        with self._lock:
            return self.mdl_info[data_type].download_url

    def set_download_url(self, data_type: str, url: str):
        with self._lock:
            self.mdl_info[data_type].download_url = url
            if url:
                try:
                    parsed = urlparse(url)
                    # Format URL for sequence number insertion (handle already-formatted)
                    self.mdl_info[data_type].url_host = parsed.hostname or ""
                except Exception:
                    pass

    def get_download_url_host(self, data_type: str) -> str:
        with self._lock:
            return self.mdl_info[data_type].url_host

    def get_base_file_path(self, data_type: str) -> str:
        with self._lock:
            return self.mdl_info[data_type].base_path

    def set_base_file_path(self, data_type: str, path: str):
        with self._lock:
            self.mdl_info[data_type].base_path = path

    def get_active_job_count(self, data_type: str) -> int:
        with self._lock:
            return self.mdl_info[data_type].active_jobs

    def increment_jobs(self, data_type: str):
        with self._lock:
            self.mdl_info[data_type].active_jobs += 1

    def decrement_jobs(self, data_type: str):
        with self._lock:
            self.mdl_info[data_type].active_jobs -= 1

    def set_status(self, status: str):
        with self._lock:
            self.status = status

    def get_status(self) -> str:
        with self._lock:
            return self.status

    def print_status(self):
        """Print the current download status."""
        status = self.get_status()
        if status:
            import sys
            sys.stderr.write(status)
            sys.stderr.flush()

    def get_time_since_updated(self) -> float:
        with self._lock:
            if self.last_updated == 0:
                return float('inf')
            return time.time() - self.last_updated

    # Quality selection helpers
    def get_codec_priority_order(self) -> list:
        """Get ordered list of preferred codecs based on user flags."""
        base_order = ["av1", "vp9", "h264"]
        preferred = []
        for codec in base_order:
            if codec == "h264" and self.h264:
                preferred.append(codec)
            elif codec == "vp9" and self.vp9:
                preferred.append(codec)
            elif codec == "av1" and self.av1:
                preferred.append(codec)

        order = list(preferred)
        for codec in base_order:
            if codec not in order:
                order.append(codec)
        return order

    # State save/load
    def save_state(self, itag: int):
        """Save download state to a JSON file for resume."""
        if self.disable_save_state:
            return
        if itag not in self.dl_state:
            return
        state = self.dl_state[itag]
        if not state.file_path:
            return

        data = {
            "start_frag": state.start_frag,
            "fragments": state.fragments,
            "size": state.size,
            "temp_dir": state.temp_dir,
        }
        try:
            with open(state.file_path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            log_debug("Failed to save state for itag %d: %s", itag, str(e))

    def load_state(self, itag: int) -> bool:
        """Load download state from a JSON file for resume.
        Returns True if state was loaded."""
        if itag not in self.dl_state:
            return False
        state = self.dl_state[itag]
        if not state.file_path or not Path(state.file_path).exists():
            return False
        try:
            with open(state.file_path, "r") as f:
                data = json.load(f)
            state.start_frag = data.get("start_frag", 0)
            state.fragments = data.get("fragments", 0)
            state.size = data.get("size", 0)
            state.temp_dir = data.get("temp_dir", "")
            return True
        except Exception as e:
            log_debug("Failed to load state for itag %d: %s", itag, str(e))
            return False

    # Metadata formatting
    def set_format_info_from_ytdlp(self, data: dict):
        """Populate format_info from yt-dlp JSON."""
        fi = self.format_info
        fi["id"] = data.get("id", self.video_id)
        fi["title"] = data.get("title", "")
        fi["channel_id"] = data.get("channel_id", "")
        fi["channel"] = data.get("uploader", "") or data.get("channel", "")
        fi["description"] = data.get("description", "")
        fi["url"] = self.url

        upload_date = data.get("upload_date", "")
        fi["upload_date"] = upload_date
        fi["publish_date"] = upload_date

        # Timestamp
        ts = data.get("timestamp") or data.get("release_timestamp")
        if ts:
            try:
                dt = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))
                fi["start_date"] = dt
                parts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)).split(" ")
                ymd = parts[0].split("-") if len(parts) > 0 else []
                hms = parts[1].split(":") if len(parts) > 1 else ["00", "00", "00"]
                fi["year"] = ymd[0] if len(ymd) > 0 else ""
                fi["month"] = ymd[1] if len(ymd) > 1 else ""
                fi["day"] = ymd[2] if len(ymd) > 2 else ""
                fi["start_time"] = ":".join(hms)
                fi["hours"] = hms[0] if len(hms) > 0 else ""
                fi["minutes"] = hms[1] if len(hms) > 1 else ""
                fi["seconds"] = hms[2] if len(hms) > 2 else ""
            except Exception:
                pass

    def set_metadata_from_format_info(self):
        """Format metadata values using format_info."""
        for k, v in self.metadata.items():
            try:
                self.metadata[k] = v % self.format_info
            except (KeyError, ValueError):
                pass

    def print_channel_and_title(self, data: dict):
        """Print channel and title info from yt-dlp data."""
        if self.info_printed:
            return
        channel = data.get("uploader", "") or data.get("channel", "Unknown")
        title = data.get("title", "Unknown")
        self.info_printed = True
        log_general("Channel: %s", channel)
        log_general("Title: %s", title)

    def ask_wait_for_stream(self) -> bool:
        """Ask user if they want to wait for a scheduled stream."""
        log_general("Stream is currently offline.")
        log_general("You can wait until it starts or exit.")
        return get_yes_no("Wait for the stream to start?")


# ---------------------------------------------------------------------------
# yt-dlp Integration
# ---------------------------------------------------------------------------

def execute_ytdlp(di: DownloadInfo) -> Optional[bytes]:
    """Execute yt-dlp to get stream info JSON."""
    args = [di.ytdlp_path, "-j", "--extractor-args", "youtube:formats=incomplete"]

    # Add cookies
    import utils as _u
    if _u.cookie_file:
        args.extend(["--cookies", _u.cookie_file])

    # Add proxy
    if _u.proxy_url:
        args.extend(["--proxy", _u.proxy_url])

    # Add custom yt-dlp options
    if di.ytdlp_opts:
        import shlex
        try:
            custom_args = shlex.split(di.ytdlp_opts)
        except ValueError:
            custom_args = di.ytdlp_opts.split()
        args.extend(custom_args)

    # Add URL
    args.append(di.url)

    log_debug("Executing yt-dlp (attempt): %s", " ".join(args))

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            log_debug("Successfully retrieved stream info from yt-dlp")
            return result.stdout
        else:
            log_warn("yt-dlp returned non-zero exit code: %d", result.returncode)
            if result.stderr:
                log_debug("yt-dlp stderr: %s", result.stderr.decode("utf-8", errors="replace")[:500])
            return None
    except subprocess.TimeoutExpired:
        log_warn("yt-dlp timed out after 30 seconds")
        return None
    except FileNotFoundError:
        log_warn("yt-dlp not found at '%s'", di.ytdlp_path)
        return None
    except Exception as e:
        log_warn("yt-dlp execution error: %s", str(e))
        return None


def execute_ytdlp_with_retry(di: DownloadInfo, max_retries: int = 3) -> Optional[bytes]:
    """Execute yt-dlp with retry logic."""
    for i in range(max_retries):
        log_debug("Executing yt-dlp (attempt %d/%d)", i + 1, max_retries)
        output = execute_ytdlp(di)
        if output is not None:
            return output
        if i < max_retries - 1:
            time.sleep(2)
    log_warn("Failed to get stream info from yt-dlp after %d attempts", max_retries)
    return None


def parse_itag_from_format_id(format_id: str) -> int:
    """Extract itag from yt-dlp format_id (e.g., '140-m4a' -> 140)."""
    left = format_id.split("-")[0]
    try:
        return int(left)
    except (ValueError, TypeError):
        return -1


def parse_sq_from_path(fragment_path: str) -> int:
    """Extract sequence number from a fragment path like '.../sq/12345'."""
    path = fragment_path.strip("/")
    parts = path.split("/")
    for i in range(len(parts) - 1):
        if parts[i] == "sq":
            try:
                return int(parts[i + 1])
            except ValueError:
                return -1
    return -1


def parse_sq_from_url(fragment_url: str) -> int:
    """Extract sequence number from a fragment URL."""
    try:
        parsed = urlparse(fragment_url)
        return parse_sq_from_path(parsed.path)
    except Exception:
        return -1


def parse_last_sq_from_fragments(fragments: list) -> int:
    """Find the last sequence number from a list of fragment dicts."""
    for frag in reversed(fragments):
        sq = parse_sq_from_path(frag.get("path", ""))
        if sq > 0:
            return sq
        fc = frag.get("fragment_count", 0)
        if fc > 0:
            return fc
        sq = parse_sq_from_url(frag.get("url", ""))
        if sq > 0:
            return sq
    return -1


def parse_ytdlp_json(json_data: bytes) -> tuple:
    """Parse yt-dlp JSON output to extract adaptive format URLs.
    Returns (adaptive_urls, dash_urls, last_sq)."""
    adaptive_urls = {}
    dash_urls = {}
    last_sq = -1

    try:
        payload = json.loads(json_data)
    except json.JSONDecodeError as e:
        log_debug("Failed to parse yt-dlp json: %v", str(e))
        return adaptive_urls, dash_urls, last_sq

    formats = payload.get("formats", [])
    for fmt in formats:
        protocol = fmt.get("protocol", "")
        if protocol == "http_dash_segments":
            sq = parse_last_sq_from_fragments(fmt.get("fragments", []))
            if sq > last_sq:
                last_sq = sq
            itag = parse_itag_from_format_id(fmt.get("format_id", ""))
            base_url = fmt.get("fragment_base_url", "")
            dash_urls[itag] = base_url.replace("%", "%%") + "sq/%d"
            continue

        if protocol != "https":
            continue

        itag = parse_itag_from_format_id(fmt.get("format_id", ""))
        url = fmt.get("url", "")
        adaptive_urls[itag] = url.replace("%", "%%") + "&sq=%d"

    if adaptive_urls:
        log_debug("Loaded %d adaptive format URLs from yt-dlp", len(adaptive_urls))
    if dash_urls:
        log_debug("Loaded %d dash format URLs from yt-dlp", len(dash_urls))

    return adaptive_urls, dash_urls, last_sq


# ---------------------------------------------------------------------------
# URL Parsing
# ---------------------------------------------------------------------------

def parse_input_url(di: DownloadInfo) -> bool:
    """Parse the input URL to extract video ID and determine URL type.
    Returns True on success."""
    try:
        parsed = urlparse(di.url)
    except Exception as e:
        log_error("Error parsing URL: %s", str(e))
        return False

    lower_host = parsed.hostname or ""
    lower_host = lower_host.lower()
    lower_host = lower_host.removeprefix("www.").removeprefix("m.")
    lower_path = (parsed.path or "").lower()
    query = parse_qs(parsed.query)

    if lower_host == "youtube.com":
        if lower_path.startswith("/watch"):
            if "v" not in query:
                log_error("YouTube URL missing video ID")
                return False
            di.video_id = query["v"][0]
            return True

        elif (lower_path.startswith("/channel/") or lower_path.startswith("/c/") or
              lower_path.startswith("/user/") or lower_path.startswith("/@")):
            # Channel URL - append /live for monitoring
            # Strip sub-page path
            chan_slash_idx = lower_path[1:].find("/") + 1
            no_chan_path = lower_path[chan_slash_idx:]
            if no_chan_path.rfind("/") > 0:
                last_slash = di.url.rfind("/")
                di.url = di.url[:last_slash]
            di.url = f"{di.url}/live"
            di.live_url = True
            return True

        elif lower_path.startswith("/live/"):
            di.video_id = parsed.path.removeprefix("/live/")
            return True

        elif lower_path.startswith("/shorts/"):
            di.video_id = parsed.path.removeprefix("/shorts/")
            return True

    elif lower_host == "youtu.be":
        di.video_id = parsed.path.strip("/")
        return True

    elif lower_host.endswith(".googlevideo.com"):
        if "noclen" not in query:
            log_error("Given Google Video URL is not for a fragmented stream")
            return False

        di.g_video_ddl = True
        id_val = query.get("id", [""])[0]
        dot_idx = id_val.rfind(".")
        if dot_idx > 0:
            id_val = id_val[:dot_idx]
        di.video_id = id_val
        di.format_info["id"] = di.video_id

        sq_idx = di.url.find("&sq=")
        try:
            itag = int(query.get("itag", ["-1"])[0])
        except ValueError:
            log_error("Error parsing itag parameter of Google Video URL")
            return False

        if sq_idx < 0:
            log_error("Could not find 'sq' parameter in given Google Video URL")
            return False

        if itag == AUDIO_ITAG:
            if not di.get_download_url(DTYPE_AUDIO):
                di.set_download_url(DTYPE_AUDIO, di.url[:sq_idx] + "&sq=%d")
            if not di.get_download_url(DTYPE_VIDEO) and not di.audio_only:
                # Will be handled later via get_video_info
                pass
        else:
            if not di.get_download_url(DTYPE_VIDEO):
                di.set_download_url(DTYPE_VIDEO, di.url[:sq_idx] + "&sq=%d")
            if not di.get_download_url(DTYPE_AUDIO) and not di.video_only:
                pass

        di.quality = itag
        return True

    log_error("%s is not a known valid YouTube URL", di.url)
    return False


# ---------------------------------------------------------------------------
# Duration Parsing (--live-from, --capture-duration, --start-delay)
# ---------------------------------------------------------------------------

def _parse_duration_str(s: str) -> Optional[int]:
    """Parse a duration string like '1h30m10s' or '12:30:05' into seconds.
    Returns None if parsing fails."""
    if not s:
        return None

    # Try Go-style duration (1h30m10s, 5m, etc.)
    dur_pattern = re.compile(
        r'^(\d+d)?(\d+h)?(\d+m)?(\d+s)?$', re.IGNORECASE
    )
    match = dur_pattern.match(s.strip())
    if match:
        total = 0
        days = match.group(1)
        hours = match.group(2)
        minutes = match.group(3)
        secs = match.group(4)
        if days:
            total += int(days[:-1]) * 86400
        if hours:
            total += int(hours[:-1]) * 3600
        if minutes:
            total += int(minutes[:-1]) * 60
        if secs:
            total += int(secs[:-1])
        if total > 0 or s.strip().endswith('s'):
            return total

    # Try HH:MM:SS or MM:SS
    time_pattern = re.compile(r'^(\d+):(\d{2})(?::(\d{2}))?$')
    match = time_pattern.match(s.strip())
    if match:
        if match.group(3):  # HH:MM:SS
            return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
        else:  # MM:SS
            return int(match.group(1)) * 60 + int(match.group(2))

    return None


def parse_live_from_str(di: DownloadInfo):
    """Parse --live-from value and set live_from_sq."""
    if not di.live_from_val:
        return

    val = di.live_from_val
    if val.lower() == "now":
        di.live_from_sq = di.last_sq
        log_general("Starting download from current time")
        return

    is_negative = val.startswith("-")
    duration_val = val.removeprefix("-")

    seconds_total = _parse_duration_str(duration_val)
    if seconds_total is None:
        log_error("Unable to parse value as either a duration or a time string: %s", val)
        return

    frag_dur = float(di.target_duration)
    seconds_rounded = int(math.ceil(seconds_total / frag_dur) * frag_dur)
    no_of_frags = seconds_rounded // di.target_duration

    if is_negative:
        if seconds_total < 0 or seconds_total > LIVE_MAXIMUM_SEEKABLE:
            log_error("Invalid duration specified '%s'. (Maximum video seek time is %d days)",
                     val, LIVE_MAXIMUM_SEEKABLE // 86400)
            return
        if no_of_frags > di.last_sq:
            stream_length = di.last_sq * di.target_duration
            log_error("Invalid duration specified. The stream has not been live for that long [Live for %s].",
                     seconds_to_duration_and_time_str(stream_length))
            return
        di.live_from_sq = di.last_sq - no_of_frags
        log_general("Jumping back %d seconds from now, and starting to download from that time.", seconds_rounded)
        log_debug("Jumping back %d frags. Will start from sequence %d [current is %d].", no_of_frags, di.live_from_sq, di.last_sq)
    else:
        max_sq = di.last_sq
        target_start_frag = no_of_frags
        if di.last_sq < target_start_frag:
            stream_length = di.last_sq * di.target_duration
            log_error("Invalid duration specified. The stream has not been live for that long [Live for %s].",
                     seconds_to_duration_and_time_str(stream_length))
            return
        if target_start_frag < (di.last_sq - LIVE_MAXIMUM_SEEKABLE // di.target_duration):
            log_error("YT only retains the livestream 7 days past for seeking, your --live-from value of '%s' is not valid.", val)
            stream_live_time = di.last_sq * di.target_duration
            min_seek_time = stream_live_time - LIVE_MAXIMUM_SEEKABLE
            log_error("You must specify a --live-from value between: %s and %s",
                     seconds_to_duration_and_time_str(min_seek_time),
                     seconds_to_duration_and_time_str(stream_live_time))
            return
        di.live_from_sq = target_start_frag
        start_time_str = seconds_to_duration_and_time_str(di.live_from_sq * di.target_duration)
        total_time_str = seconds_to_duration_and_time_str((max_sq - di.live_from_sq) * di.target_duration)
        log_general("Starting from stream time '%s' and grabbing '%s' of content (and counting).", start_time_str, total_time_str)
        log_debug("Starting from sequence %d [max right now is %d]", di.live_from_sq, max_sq)


def parse_capture_duration(di: DownloadInfo, val: str):
    """Parse --capture-duration value."""
    if not val:
        return
    seconds = _parse_duration_str(val)
    if seconds is None:
        log_error("Unable to parse value as either a Duration or a Time String: %s", val)
        return
    di.capture_duration_secs = seconds
    log_general("Downloading a minimum of %s of content and then exiting...", seconds_to_duration_and_time_str(seconds))


def parse_start_delay(di: DownloadInfo, val: str):
    """Parse --start-delay value."""
    if not val:
        return
    seconds = _parse_duration_str(val)
    if seconds is None:
        log_error("Unable to parse value as either a Duration or a Time String: %s", val)
        return
    di.start_delay_secs = seconds


# ---------------------------------------------------------------------------
# URL Source Fallback Chain
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Get Video Info
# ---------------------------------------------------------------------------

def _parse_ytdlp_info(json_data: bytes) -> dict:
    """Parse yt-dlp JSON into metadata + format URL dicts."""
    try:
        data = json.loads(json_data)
    except json.JSONDecodeError as e:
        log_debug("Failed to parse yt-dlp JSON: %s", str(e))
        return {}

    # Extract format URLs (same logic as parse_ytdlp_json)
    adaptive_urls, dash_urls, last_sq = parse_ytdlp_json(json_data)
    data["_adaptive_urls"] = adaptive_urls
    data["_dash_urls"] = dash_urls
    data["_last_sq"] = last_sq

    # Derive target duration from format fragments if available
    for fmt in data.get("formats", []):
        fragments = fmt.get("fragments", [])
        if fragments:
            dur = fragments[0].get("duration")
            if dur:
                data["_target_duration"] = int(dur)
                break

    return data


def get_video_info(di: DownloadInfo) -> bool:
    """Get video info and download URLs from yt-dlp.
    Returns True on success."""
    with di._lock:
        if di.g_video_ddl or di.stopping or di.unavailable:
            return False
        delta = time.time() - di.last_updated
        if delta < DEFAULT_POLL_TIME:
            return False

    first_wait = True
    retry_count = 0
    live_waited = 0

    sel_qualities = []
    if di.selected_quality:
        sel_qualities = parse_quality_selection(video_qualities, di.selected_quality)

    while True:
        json_data = execute_ytdlp_with_retry(di, 3)
        if not json_data:
            log_error("Failed to get stream info from yt-dlp")
            di.live = False
            di.unavailable = True
            return False

        data = _parse_ytdlp_info(json_data)
        if not data:
            log_error("Failed to parse yt-dlp output")
            return False

        live_status = data.get("live_status", "")

        # Handle scheduled / upcoming streams
        if live_status == "is_upcoming":
            if di.in_progress:
                log_debug("Stream status changed to upcoming mid-download")
                return False

            if di.live_from_val and di.live_from_val.startswith("-"):
                log_error("Option --live-from with a negative duration is not valid for a scheduled stream.")
                return False

            if di.wait == ACTION_DO_NOT:
                log_error("Stream has not started, and you have opted not to wait.")
                return False

            if first_wait and di.wait == ACTION_ASK and di.retry_secs == 0:
                if not di.ask_wait_for_stream():
                    return False

            if first_wait:
                di.print_channel_and_title(data)
                if not sel_qualities:
                    sel_qualities = get_quality_from_user(video_qualities, True)

            release_ts = data.get("release_timestamp")
            if release_ts and di.retry_secs <= 0:
                cur_time = int(time.time())
                sleep_time = release_ts - cur_time
                if sleep_time > 0:
                    if first_wait:
                        first_wait = False
                    log_general("Stream starts at %s in %d seconds.",
                        time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(release_ts)), sleep_time)
                    log_general("Waiting for this time to elapse...")
                    while sleep_time > 0:
                        time.sleep(min(sleep_time, 60))
                        cur_time = int(time.time())
                        sleep_time = release_ts - cur_time
                    continue

            di.retry_secs = di.retry_secs or DEFAULT_POLL_TIME

            if first_wait:
                first_wait = False
                log_general("Waiting for stream, retrying every %d seconds...\n", di.retry_secs)

            time.sleep(di.retry_secs)
            live_waited += di.retry_secs
            retry_count += 1
            import utils as _u
            msg = "Retries: %d (Last retry: %s), Total time waited: %d seconds"
            if not _u.status_newlines:
                msg = "\r" + msg
            else:
                msg = msg + "\n"
            sys.stderr.write(msg % (retry_count, time.strftime("%Y/%m/%d %H:%M:%S"), live_waited))
            sys.stderr.flush()
            continue

        # Not a livestream at all
        if live_status not in ("is_live", "was_live", "post_live"):
            if di.live:
                di.live = False
            else:
                log_error("%s is not a livestream. It would be better to use yt-dlp to download it.", di.url)
            return False

        # Stream has ended and is being processed
        if live_status in ("was_live", "post_live") and not di.in_progress:
            if not data.get("formats"):
                log_general("Livestream has ended and is being processed. Download URLs not available.")
                return False
            adaptive = data.get("_adaptive_urls", {})
            dash = data.get("_dash_urls", {})
            if not adaptive and not dash:
                log_general("Livestream has been processed. Use yt-dlp instead.")
                return False

        # Stream is live (or was live with formats) — proceed
        di.print_channel_and_title(data)

        with di._lock:
            di.last_updated = time.time()

        # Extract format URLs
        dl_urls = {}
        adaptive = data.get("_adaptive_urls", {})
        dash = data.get("_dash_urls", {})
        last_sq = data.get("_last_sq", -1)

        if adaptive:
            log_debug("Using yt-dlp adaptive formats as primary source")
            dl_urls.update(adaptive)
            if last_sq > 0:
                di.last_sq = last_sq
        elif dash:
            log_debug("Using yt-dlp dash formats as fallback")
            dl_urls.update(dash)
            if last_sq > 0:
                di.last_sq = last_sq

        if not dl_urls:
            log_error("No download URLs found")
            return False

        # Target duration
        target_dur = data.get("_target_duration")
        if target_dur:
            di.target_duration = target_dur
            log_debug("Target fragment duration: %ds", target_dur)

        # Quality selection (unchanged logic)
        if di.quality < 0:
            qualities = ["audio_only"]
            found = False

            for qlabel in video_qualities:
                video_itag = video_label_itags[qlabel]
                vp9_ok = video_itag.vp9 in dl_urls
                h264_ok = video_itag.h264 in dl_urls
                av1_ok = video_itag.av1 in dl_urls

                if qlabel.endswith("60"):
                    base_quality = qlabel[:-2]
                    if base_quality in video_label_itags:
                        base_itag = video_label_itags[base_quality]
                        if base_itag.av1 == video_itag.av1:
                            if base_itag.h264 in dl_urls or base_itag.vp9 in dl_urls:
                                av1_ok = False

                if contains(qualities, qlabel) or (not vp9_ok and not h264_ok and not av1_ok):
                    continue
                qualities.append(qlabel)

            while not found:
                if not sel_qualities:
                    sel_qualities = get_quality_from_user(qualities, False)

                for q in sel_qualities:
                    q = q.strip()
                    if q == "best":
                        q = qualities[-1]
                    elif q == "audio":
                        q = "audio_only"

                    video_itag = video_label_itags[q]
                    aonly = video_itag.vp9 == AUDIO_ONLY_QUALITY

                    if not di.video_only and AUDIO_ITAG in dl_urls:
                        di.set_download_url(DTYPE_AUDIO, dl_urls[AUDIO_ITAG])

                    if aonly:
                        di.quality = AUDIO_ONLY_QUALITY
                        di.set_download_url(DTYPE_VIDEO, "")
                        found = True
                        break

                    codec_order = di.get_codec_priority_order()
                    log_debug("Codec priority order: %s", ", ".join(codec_order).upper())
                    for codec in codec_order:
                        if codec == "h264":
                            itag = video_itag.h264
                        elif codec == "vp9":
                            itag = video_itag.vp9
                        elif codec == "av1":
                            itag = video_itag.av1
                        else:
                            continue

                        if itag == AUDIO_ONLY_QUALITY:
                            continue

                        if codec == "av1" and q.endswith("60"):
                            if video_itag.av1 in dl_urls:
                                base_quality = q[:-2]
                                if base_quality in video_label_itags:
                                    base_itag = video_label_itags[base_quality]
                                    if base_itag.av1 == video_itag.av1:
                                        if base_itag.h264 in dl_urls or base_itag.vp9 in dl_urls:
                                            log_debug("Treating %s AV1 itag=%d as unavailable", q, video_itag.av1)
                                            continue

                        url = dl_urls.get(itag)
                        log_debug("Codec availability: %s itag=%d ok=%s", codec.upper(), itag, url is not None)
                        if url is None:
                            continue

                        di.set_download_url(DTYPE_VIDEO, url)
                        di.quality = itag
                        found = True
                        log_general("Selected quality: %s (%s)", q, codec.upper())
                        break
                    if found:
                        break

                if not found:
                    log_general("The qualities you selected ended up unavailable for this stream")
                    log_general("You will now have the option to select from the available qualities")
                    sel_qualities = []
        else:
            aonly = di.quality == AUDIO_ONLY_QUALITY
            if not di.video_only and AUDIO_ITAG in dl_urls and is_fragmented(dl_urls.get(AUDIO_ITAG, "")):
                di.set_download_url(DTYPE_AUDIO, dl_urls[AUDIO_ITAG])
            if not aonly:
                vid_ok = di.quality in dl_urls
                if vid_ok and is_fragmented(dl_urls.get(di.quality, "")):
                    di.set_download_url(DTYPE_VIDEO, dl_urls[di.quality])

        if not di.in_progress:
            timestamp = data.get("timestamp") or data.get("release_timestamp")
            if timestamp:
                log_general("Stream started at time %s",
                    time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(timestamp)))
            di.set_format_info_from_ytdlp(data)
            di.set_metadata_from_format_info()
            thumb_url = data.get("thumbnail", "")
            if thumb_url:
                di.thumbnail = thumb_url
            di.in_progress = True

        di.live = (live_status == "is_live")
        return True


def wait_for_start_delay(di: DownloadInfo) -> bool:
    """Wait for --start-delay duration before starting download."""
    if di.live and di.start_delay_secs > 0:
        frag_dur = float(di.target_duration)
        seconds_rounded = int(math.ceil(di.start_delay_secs / frag_dur) * frag_dur)
        no_of_frags = seconds_rounded // di.target_duration
        di.live_from_sq = di.last_sq + no_of_frags

        log_general("Waiting %s before starting to download...", seconds_to_duration_and_time_str(seconds_rounded))
        log_debug("Will start from sequence %d [current is %d]", di.live_from_sq, di.last_sq)

        time.sleep(seconds_rounded)

        if seconds_rounded > DEFAULT_POLL_TIME:
            return get_video_info(di)

    return True


# ---------------------------------------------------------------------------
# Fragment Download
# ---------------------------------------------------------------------------

def handle_frag_http_error(di: DownloadInfo, state: FragThreadState, status_code: int, url: str):
    """Handle HTTP error during fragment download."""
    log_debug("%s: HTTP Error for fragment %d: %d", state.name, state.seq_num, status_code)
    di.print_status()

    if status_code == 403:
        state.is_403 = True
        refresh_url(di, state.data_type, url)
    elif status_code == 404 and state.max_seq > -1 and not di.is_live() and state.seq_num > (state.max_seq - 2):
        log_debug("%s: Stream has ended and fragment within the last two not found, probably not actually created", state.name)
        di.print_status()
        di.set_finished(state.data_type)


def handle_frag_download_error(di: DownloadInfo, state: FragThreadState, err: Exception):
    """Handle network error during fragment download."""
    log_debug("%s: Error with fragment %d: %s", state.name, state.seq_num, str(err))
    di.print_status()

    if state.max_seq > -1 and not di.is_live() and state.seq_num >= (state.max_seq - 2):
        log_debug("%s: Stream has ended and fragment number is within two of the known max, probably not actually created", state.name)
        di.set_finished(state.data_type)
        di.print_status()


def continue_fragment_download(di: DownloadInfo, state: FragThreadState) -> bool:
    """Determine whether to continue retrying a fragment download."""
    if di.is_finished(state.data_type):
        return False

    if di.frag_max_tries > 0 and state.tries >= di.frag_max_tries:
        state.full_retries -= 1
        log_debug("%s: Fragment %d: %d/%d retries", state.name, state.seq_num, state.tries, di.frag_max_tries)
        di.print_status()

        if di.is_live():
            get_video_info(di)

        if not di.is_live() or di.is_unavailable():
            if state.is_403:
                if di.is_unavailable():
                    log_warn("%s: Download link likely expired and stream is privated or members only, cannot continue download", state.name)
                else:
                    log_warn("%s: Download link has likely expired and the stream has probably finished processing.", state.name)
                    log_warn("%s: You might want to use youtube-dl to download instead.", state.name)
                di.print_status()
                di.set_finished(state.data_type)
                return False
            elif state.max_seq > -1 and state.seq_num < (state.max_seq - 2) and state.full_retries > 0:
                log_debug("%s: More than two fragments away from the highest known fragment", state.name)
                log_debug("%s: Will try grabbing the fragment %d more times", state.name, state.full_retries)
                di.print_status()
            else:
                di.set_finished(state.data_type)
                return False
        else:
            log_debug("%s: Fragment %d: Stream still live, continuing download attempt", state.name, state.seq_num)
            di.print_status()
            state.tries = 0

    return True


def refresh_url(di: DownloadInfo, data_type: str, current_url: str):
    """Attempt to get a new download URL on 403 error."""
    if not di.is_g_video_ddl():
        new_url = di.get_download_url(data_type)
        if not current_url or new_url == current_url:
            log_debug("%s: Attempting to retrieve a new download URL", data_type)
            di.print_status()
            get_video_info(di)


def download_fragment(di: DownloadInfo, state: FragThreadState, data_queue: Queue):
    """Download a single fragment."""
    state.tries = 0
    state.full_retries = 3
    state.is_403 = False
    fname = f"{state.base_file_path}.frag{state.seq_num}.ts"

    while state.tries < di.frag_max_tries or di.frag_max_tries == 0:
        if di.is_stopping():
            return

        if di.frag_max_tries == 0:
            state.tries = 0

        base_url = di.get_download_url(state.data_type)
        seq_url = base_url % state.seq_num

        dl_start = time.time()

        try:
            host = di.get_download_url_host(state.data_type)
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:87.0) Gecko/20100101 Firefox/87.0",
                "Origin": "https://www.youtube.com",
            }
            if host:
                headers["Host"] = host
                headers["Referer"] = f"https://{host}/"

            resp = session.get(seq_url, headers=headers, timeout=(15, 30))
        except Exception as e:
            handle_frag_download_error(di, state, e)
            state.tries += 1
            if not continue_fragment_download(di, state):
                return
            time.sleep(state.sleep_time)
            continue

        dl_duration = time.time() - dl_start

        if resp.status_code >= 400:
            handle_frag_http_error(di, state, resp.status_code, base_url)
            state.tries += 1
            if not continue_fragment_download(di, state):
                return
            time.sleep(state.sleep_time)
            continue

        resp_data = resp.content
        if not resp_data:
            state.tries += 1
            if not continue_fragment_download(di, state):
                return
            time.sleep(state.sleep_time)
            continue

        # Get X-Head-Seqnum header
        header_seqnum = -1
        header_seqnum_str = resp.headers.get("X-Head-Seqnum", "")
        if header_seqnum_str:
            try:
                header_seqnum = int(header_seqnum_str)
            except ValueError:
                pass

        mime_type = resp.headers.get("Content-Type", "")

        if state.to_file:
            try:
                with open(fname, "wb") as f:
                    f.write(resp_data)
            except Exception as e:
                log_debug("%s: Failed to write fragment %d to file: %s", state.name, state.seq_num, str(e))
                di.print_status()
                state.tries += 1
                if not continue_fragment_download(di, state):
                    try_delete(fname)
                    return
                time.sleep(state.sleep_time)
                continue
            data = None
        else:
            data = bytearray(resp_data)

        # Slow fragment detection
        is_slow = False
        if header_seqnum < 0 or state.seq_num < (header_seqnum - 10):
            is_slow = dl_duration > (di.target_duration * 1.5)

        data_queue.put(Fragment(
            seq=state.seq_num,
            x_head_seq_num=header_seqnum,
            file_name=fname,
            data=data,
            slow=is_slow,
            mime_type=mime_type,
        ))
        return


# ---------------------------------------------------------------------------
# Fragment Download Worker
# ---------------------------------------------------------------------------

def download_frags(di: DownloadInfo, data_type: str, seq_queue: Queue,
                   data_queue: Queue, name: str):
    """Worker thread that downloads fragments from a sequence queue."""
    try:
        state = FragThreadState(
            name=name,
            base_file_path=di.get_base_file_path(data_type),
            data_type=data_type,
            to_file=di.frag_files,
            sleep_time=float(di.target_duration),
        )

        end_seq = 0
        while True:
            try:
                seq_info = seq_queue.get(timeout=1)
            except Empty:
                if di.is_stopping() or di.is_finished(data_type):
                    break
                continue

            if di.is_stopping() or di.is_finished(data_type):
                break

            # --capture-duration check
            if di.capture_duration_secs != 0:
                if end_seq == 0:
                    cap_seq_cnt = int(math.ceil(di.capture_duration_secs / di.target_duration))
                    end_seq = seq_info.cur_sequence + cap_seq_cnt
                elif seq_info.cur_sequence >= end_seq:
                    log_debug("%s: Reached the maximum duration specified by --capture-duration.", name)
                    di.set_finished(data_type)
                    break

            if seq_info.max_sequence > -1 and not di.is_live() and seq_info.cur_sequence >= seq_info.max_sequence:
                log_debug("%s: Stream is finished and highest sequence reached", name)
                di.set_finished(data_type)
                break

            state.seq_num = seq_info.cur_sequence
            state.max_seq = seq_info.max_sequence

            download_fragment(di, state, data_queue)
    except Exception as e:
        log_error("%s: Unhandled error in download worker: %s", name, str(e))
        import traceback
        log_debug("%s", traceback.format_exc())
    finally:
        di.decrement_jobs(data_type)
        log_debug("%s: exiting", name)
        di.print_status()


# ---------------------------------------------------------------------------
# Stream Download Orchestrator
# ---------------------------------------------------------------------------

def download_stream(di: DownloadInfo, data_type: str, data_file: str,
                    progress_queue: Queue, done_event: threading.Event):
    """Orchestrate downloading a single stream (audio or video).
    Manages worker threads and sequential fragment writing."""
    data_queue = Queue(maxsize=di.jobs * 2)
    seq_queue = Queue(maxsize=di.jobs * 2)
    closed = False
    cur_frag = 0
    start_frag = 0
    active_downloads = 0
    max_seqs = -1
    tries = 10
    job_num = 1
    slow_frags = 0
    last_slow_frag = 0

    if data_type == DTYPE_AUDIO:
        itag = AUDIO_ITAG
    else:
        itag = di.quality

    log_name = f"{data_type}-download"

    # Open file for writing
    f = None
    resumed_state = False

    if itag in di.dl_state and di.dl_state[itag].fragments > 0:
        if di.live_from_sq != 0:
            if di.live_from_val:
                log_warn("%s: Option --live-from is being ignored as a download is being resumed.", data_type)
            if di.start_delay_secs != 0:
                log_warn("%s: Option --start-delay is being ignored as a download is being resumed.", data_type)

        try:
            f = open(data_file, "r+b")
            f.seek(di.dl_state[itag].size)
            resumed_state = True
        except FileNotFoundError:
            log_debug("%s: State file found but data file missing. Starting fresh.", data_type)
            f = open(data_file, "wb")
            resumed_state = False
        except Exception as e:
            log_warn("%s: Failed to open %s to resume: %s", data_type, data_file, str(e))
            log_warn("%s: Will truncate and start from the beginning", data_type)
            f = open(data_file, "wb")
            resumed_state = False
    else:
        f = open(data_file, "wb")

    if resumed_state:
        start_frag = di.dl_state[itag].start_frag
        cur_frag = start_frag + di.dl_state[itag].fragments
        max_seqs = di.last_sq
        log_info("%s: Resuming download from sequence %d", data_type, cur_frag)
    else:
        if di.last_sq >= 0:
            cur_frag = di.last_sq - (LIVE_MAXIMUM_SEEKABLE // di.target_duration)
            max_seqs = di.last_sq

        if di.live_from_sq != 0:
            cur_frag = di.live_from_sq
            start_frag = cur_frag
            log_debug("%s: Starting from sequence %d (latest is %d)", data_type, start_frag, di.last_sq)
        elif cur_frag > 0:
            log_warn("%s: YT only retains the livestream 7 days past for seeking, starting from sequence %d (latest is %d)", data_type, cur_frag, di.last_sq)
            start_frag = cur_frag
        else:
            cur_frag = 0

        if itag not in di.dl_state:
            di.dl_state[itag] = DownloadState()
        di.dl_state[itag].start_frag = start_frag

    cur_seq = cur_frag

    # Spawn initial worker threads
    workers_started = 0
    for _ in range(di.jobs):
        job_name = f"{data_type}{workers_started + 1}"
        di.increment_jobs(data_type)
        seq_queue.put(SeqChanInfo(cur_seq, max_seqs))
        cur_seq += 1
        active_downloads += 1
        workers_started += 1
        t = threading.Thread(
            target=download_frags,
            args=(di, data_type, seq_queue, data_queue, job_name),
            daemon=True,
        )
        t.start()

    data_to_write = []
    deleting_frags = []

    try:
        while True:
            data_received = False
            downloading = di.get_active_job_count(data_type) > 0
            stopping = di.is_stopping()

            if stopping or not downloading or di.is_finished(data_type):
                if not closed:
                    # Drain seq_queue to unblock workers
                    while not seq_queue.empty():
                        try:
                            seq_queue.get_nowait()
                        except Empty:
                            break
                    closed = True
            elif slow_frags >= 10:
                slow_frags = 0

            # Drain data_queue
            while True:
                try:
                    data = data_queue.get_nowait()
                except Empty:
                    break

                data_received = True
                data_to_write.append(data)
                active_downloads -= 1

                if not downloading or stopping or closed:
                    continue

                if data.x_head_seq_num > max_seqs:
                    max_seqs = data.x_head_seq_num

                if max_seqs > 0:
                    while (cur_seq <= max_seqs + 1 and active_downloads < di.jobs) or active_downloads < 1:
                        seq_queue.put(SeqChanInfo(cur_seq, max_seqs))
                        cur_seq += 1
                        active_downloads += 1
                else:
                    seq_queue.put(SeqChanInfo(cur_seq, max_seqs))
                    cur_seq += 1
                    active_downloads += 1

                if data.slow:
                    if (data.seq - last_slow_frag) < 10:
                        slow_frags += 1
                    else:
                        slow_frags = 1
                    last_slow_frag = data.seq

            if not data_to_write and not data_received and downloading:
                if not stopping and active_downloads <= 0:
                    log_debug("%s: Somehow no active downloads and no data to write", log_name)
                    log_debug("%s: Fragment this happened at: %d", log_name, cur_frag)
                    di.print_status()
                    while active_downloads < di.get_active_job_count(data_type):
                        seq_queue.put(SeqChanInfo(cur_seq, max_seqs))
                        cur_seq += 1
                        active_downloads += 1

                time.sleep(0.1)
                continue

            # Write fragments in order
            i = 0
            while i < len(data_to_write) and tries > 0:
                data = data_to_write[i]
                if data.seq != cur_frag:
                    i += 1
                    continue

                # Read fragment data from file if needed
                if di.frag_files:
                    try:
                        with open(data.file_name, "rb") as frag_f:
                            read_bytes = frag_f.read()
                        data.data = bytearray(read_bytes)
                    except Exception as e:
                        tries -= 1
                        log_warn("%s: Error when attempting to read fragment %d for writing: %s", log_name, cur_frag, str(e))
                        di.print_status()
                        if tries > 0:
                            log_warn("%s: Will try %d more time(s)", log_name, tries)
                            di.print_status()
                        continue

                buf = bytes(data.data)

                # Remove unwanted MP4 atoms
                mime = data.mime_type
                if buf:
                    if mime.endswith("/mp4") or not mime:
                        bad_atoms = ["sidx"]
                        if cur_frag != start_frag:
                            bad_atoms.append("ftyp")
                        buf = remove_atoms(bytearray(buf), *bad_atoms)
                        buf = bytes(buf)

                try:
                    f.write(buf)
                except Exception as e:
                    tries -= 1
                    log_warn("%s: Error when attempting to write fragment %d to %s: %s", log_name, cur_frag, data_file, str(e))
                    di.print_status()
                    if tries > 0:
                        log_warn("%s: Will try %d more time(s)", log_name, tries)
                        di.print_status()
                    continue

                cur_frag += 1
                progress_queue.put(ProgressInfo(itag, len(buf), max_seqs, start_frag))

                if di.frag_files:
                    try:
                        Path(data.file_name).unlink()
                    except OSError as e:
                        log_warn("%s: Error deleting fragment %d: %s", log_name, data.seq, str(e))
                        log_warn("%s: Will try again after the download has finished", log_name)
                        deleting_frags.append(data.file_name)
                        di.print_status()

                data_to_write.pop(i)
                tries = 10
                i = 0

            if not downloading:
                break

            if tries <= 0:
                log_warn("%s: Stopping download, something must be wrong...", log_name)
                di.print_status()
                di.stop()
    except Exception as e:
        log_error("%s: Unhandled error in download stream: %s", log_name, str(e))
        import traceback
        log_debug("%s", traceback.format_exc())
    finally:
        f.close()

        # Cleanup remaining fragment files
        if di.frag_files:
            for d in data_to_write:
                try_delete(d.file_name)
        for d in deleting_frags:
            try_delete(d)

        done_event.set()
        log_debug("%s thread closing", log_name)
        di.print_status()


# ---------------------------------------------------------------------------
# Netscape Cookies Parser (delegates to utils)
# ---------------------------------------------------------------------------

def parse_netscape_cookies(di: DownloadInfo, cookie_file: str) -> bool:
    """Parse netscape cookies file and set on HTTP session."""
    from utils import parse_netscape_cookies_file
    try:
        jar = parse_netscape_cookies_file(cookie_file)
        session.cookies = jar
        log_info("Loaded cookie file %s", cookie_file)
        return True
    except Exception as e:
        log_error("Failed to load cookies file: %s", str(e))
        return False
