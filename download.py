import json
import math
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from queue import Queue, Empty
from typing import Optional
from urllib.parse import urlparse, parse_qs

from utils import (
    DTYPE_AUDIO, DTYPE_VIDEO, AUDIO_ITAG, AUDIO_ONLY_QUALITY,
    DEFAULT_POLL_TIME, DEFAULT_THREADS,
    DEFAULT_FRAG_MAX_TRIES, LIVE_MAXIMUM_SEEKABLE, ACTION_ASK, LogDebug, LogError, LogGeneral, LogInfo, LogWarn, SecondsToDurationAndTimeStr, GetYesNo,
    TryDelete, RemoveAtoms, IsFragmented,
    VideoQualities, VideoLabelItags, Contains,
    ParseQualitySelection, GetQualityFromUser,
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
    Seq: int
    FileName: str = ""
    XHeadSeqNum: int = -1
    Data: Optional[bytearray] = None
    Slow: bool = False
    MimeType: str = ""


@dataclass
class ProgressInfo:
    """Progress info sent from download thread to main thread."""
    Itag: int
    ByteCount: int
    MaxSeq: int
    StartFrag: int


@dataclass
class SeqChanInfo:
    """Information sent through the sequence channel."""
    CurSequence: int
    MaxSequence: int


@dataclass
class FragThreadState:
    """State shared between fragment download functions."""
    Name: str
    BaseFilePath: str
    DataType: str
    SeqNum: int = 0
    MaxSeq: int = -1
    Tries: int = 0
    FullRetries: int = 3
    Is403: bool = False
    ToFile: bool = True
    SleepTime: float = 5.0


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
    def ActiveJobs(self):
        with self._lock:
            return self._active_jobs

    @ActiveJobs.setter
    def ActiveJobs(self, val):
        with self._lock:
            self._active_jobs = val

    @property
    def DownloadURL(self):
        with self._lock:
            return self._download_url

    @DownloadURL.setter
    def DownloadURL(self, val):
        with self._lock:
            self._download_url = val

    @property
    def BasePath(self):
        with self._lock:
            return self._base_path

    @BasePath.setter
    def BasePath(self, val):
        with self._lock:
            self._base_path = val

    @property
    def DataType(self):
        with self._lock:
            return self._data_type

    @DataType.setter
    def DataType(self, val):
        with self._lock:
            self._data_type = val

    @property
    def Finished(self):
        with self._lock:
            return self._finished

    @Finished.setter
    def Finished(self, val):
        with self._lock:
            self._finished = val

    @property
    def URLHost(self):
        with self._lock:
            return self._url_host

    @URLHost.setter
    def URLHost(self, val):
        with self._lock:
            self._url_host = val


@dataclass
class DownloadState:
    """State for resumable downloading."""
    StartFrag: int = 0
    Fragments: int = 0
    Size: int = 0
    TempDir: str = ""
    File: str = ""


class DownloadInfo:
    """Central state for the download process."""

    def __init__(self):
        self._lock = threading.RLock()

        # Format info
        self.FormatInfo = self.NewFormatInfo()
        self.Metadata = self.NewMetaInfo()
        self.CookiesURL = None
        self.VisitorData = ""
        self.PoToken = ""

        # State flags
        self.Stopping = False
        self.InProgress = False
        self.Live = False
        self.VP9 = False
        self.H264 = False
        self.AV1 = False
        self.Unavailable = False
        self.GVideoDDL = False
        self.FragFiles = True
        self.LiveURL = False
        self.AudioOnly = False
        self.VideoOnly = False
        self.MembersOnly = False
        self.InfoPrinted = False
        self.DisableSaveState = False

        # Stream info
        self.Thumbnail = ""
        self.VideoID = ""
        self.URL = ""
        self.SelectedQuality = ""
        self.Status = ""
        self.LiveFromVal = ""
        self.YtdlpPath = "yt-dlp"
        self.YtdlpOpts = ""

        # Numeric settings
        self.FragMaxTries = DEFAULT_FRAG_MAX_TRIES
        self.Wait = ACTION_ASK
        self.Quality = -1
        self.RetrySecs = 0
        self.Jobs = DEFAULT_THREADS
        self.TargetDuration = 5
        self.LastSq = -1
        self.LiveFromSq = 0
        self.CaptureDurationSecs = 0
        self.StartDelaySecs = 0
        self.LastUpdated = 0.0

        # Download state
        self.MDLInfo = {
            DTYPE_VIDEO: MediaDLInfo(),
            DTYPE_AUDIO: MediaDLInfo(),
        }
        self.DLState = {}

        # File modes
        self.FileMode = 0o644
        self.DirMode = 0o755

    @staticmethod
    def NewFormatInfo() -> FormatInfo:
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
    def NewMetaInfo() -> MetaInfo:
        return MetaInfo({
            "title": "%(title)s",
            "artist": "%(channel)s",
            "date": "%(upload_date)s",
            "comment": "%(url)s\n\n%(description)s",
        })

    # Thread-safe property accessors

    def IsStopping(self) -> bool:
        with self._lock:
            return self.Stopping

    def Stop(self):
        with self._lock:
            self.Stopping = True
            self.SetFinished(DTYPE_AUDIO)
            self.SetFinished(DTYPE_VIDEO)

    def IsLive(self) -> bool:
        with self._lock:
            return self.Live

    def IsUnavailable(self) -> bool:
        with self._lock:
            return self.Unavailable

    def IsGVideoDDL(self) -> bool:
        with self._lock:
            return self.GVideoDDL

    def IsFinished(self, data_type: str) -> bool:
        with self._lock:
            return self.MDLInfo[data_type].Finished

    def SetFinished(self, data_type: str):
        with self._lock:
            self.MDLInfo[data_type].Finished = True

    def GetDownloadUrl(self, data_type: str) -> str:
        with self._lock:
            return self.MDLInfo[data_type].DownloadURL

    def SetDownloadUrl(self, data_type: str, url: str):
        with self._lock:
            self.MDLInfo[data_type].DownloadURL = url
            if url:
                try:
                    parsed = urlparse(url)
                    # Format URL for sequence number insertion (handle already-formatted)
                    self.MDLInfo[data_type].URLHost = parsed.hostname or ""
                except Exception:
                    pass

    def GetDownloadUrlHost(self, data_type: str) -> str:
        with self._lock:
            return self.MDLInfo[data_type].URLHost

    def GetBaseFilePath(self, data_type: str) -> str:
        with self._lock:
            return self.MDLInfo[data_type].BasePath

    def SetBaseFilePath(self, data_type: str, path: str):
        with self._lock:
            self.MDLInfo[data_type].BasePath = path

    def GetActiveJobCount(self, data_type: str) -> int:
        with self._lock:
            return self.MDLInfo[data_type].ActiveJobs

    def IncrementJobs(self, data_type: str):
        with self._lock:
            self.MDLInfo[data_type].ActiveJobs += 1

    def DecrementJobs(self, data_type: str):
        with self._lock:
            self.MDLInfo[data_type].ActiveJobs -= 1

    def SetStatus(self, status: str):
        with self._lock:
            self.Status = status

    def GetStatus(self) -> str:
        with self._lock:
            return self.Status

    def PrintStatus(self):
        """Print the current download status."""
        status = self.GetStatus()
        if status:
            import sys
            sys.stderr.write(status)
            sys.stderr.flush()

    def GetTimeSinceUpdated(self) -> float:
        with self._lock:
            if self.LastUpdated == 0:
                return float('inf')
            return time.time() - self.LastUpdated

    # Quality selection helpers
    def GetCodecPriorityOrder(self) -> list:
        """Get ordered list of preferred codecs based on user flags."""
        base_order = ["av1", "vp9", "h264"]
        preferred = []
        for codec in base_order:
            if codec == "h264" and self.H264:
                preferred.append(codec)
            elif codec == "vp9" and self.VP9:
                preferred.append(codec)
            elif codec == "av1" and self.AV1:
                preferred.append(codec)

        order = list(preferred)
        for codec in base_order:
            if codec not in order:
                order.append(codec)
        return order

    # State save/load
    def SaveState(self, itag: int):
        """Save download state to a JSON file for resume."""
        if self.DisableSaveState:
            return
        if itag not in self.DLState:
            return
        state = self.DLState[itag]
        if not state.File:
            return

        data = {
            "StartFrag": state.StartFrag,
            "Fragments": state.Fragments,
            "Size": state.Size,
            "TempDir": state.TempDir,
        }
        try:
            with open(state.File, "w") as f:
                json.dump(data, f)
        except Exception as e:
            LogDebug("Failed to save state for itag %d: %s", itag, str(e))

    def LoadState(self, itag: int) -> bool:
        """Load download state from a JSON file for resume.
        Returns True if state was loaded."""
        if itag not in self.DLState:
            return False
        state = self.DLState[itag]
        if not state.File or not os.path.exists(state.File):
            return False
        try:
            with open(state.File, "r") as f:
                data = json.load(f)
            state.StartFrag = data.get("StartFrag", 0)
            state.Fragments = data.get("Fragments", 0)
            state.Size = data.get("Size", 0)
            state.TempDir = data.get("TempDir", "")
            return True
        except Exception as e:
            LogDebug("Failed to load state for itag %d: %s", itag, str(e))
            return False

    # Metadata formatting
    def SetFormatInfoFromYtdlp(self, data: dict):
        """Populate FormatInfo from yt-dlp JSON."""
        fi = self.FormatInfo
        fi["id"] = data.get("id", self.VideoID)
        fi["title"] = data.get("title", "")
        fi["channel_id"] = data.get("channel_id", "")
        fi["channel"] = data.get("uploader", "") or data.get("channel", "")
        fi["description"] = data.get("description", "")
        fi["url"] = self.URL

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

    def SetMetadataFromFormatInfo(self):
        """Format metadata values using FormatInfo."""
        for k, v in self.Metadata.items():
            try:
                self.Metadata[k] = v % self.FormatInfo
            except (KeyError, ValueError):
                pass

    def PrintChannelAndTitle(self, data: dict):
        """Print channel and title info from yt-dlp data."""
        if self.InfoPrinted:
            return
        channel = data.get("uploader", "") or data.get("channel", "Unknown")
        title = data.get("title", "Unknown")
        self.InfoPrinted = True
        LogGeneral("Channel: %s", channel)
        LogGeneral("Title: %s", title)

    def AskWaitForStream(self) -> bool:
        """Ask user if they want to wait for a scheduled stream."""
        LogGeneral("Stream is currently offline.")
        LogGeneral("You can wait until it starts or exit.")
        return GetYesNo("Wait for the stream to start?")


# ---------------------------------------------------------------------------
# yt-dlp Integration
# ---------------------------------------------------------------------------

def execute_ytdlp(di: DownloadInfo) -> Optional[bytes]:
    """Execute yt-dlp to get stream info JSON."""
    args = [di.YtdlpPath, "-j", "--extractor-args", "youtube:formats=incomplete"]

    # Add cookies
    import utils as _u
    if _u.cookie_file:
        args.extend(["--cookies", _u.cookie_file])

    # Add proxy
    if _u.proxy_url:
        args.extend(["--proxy", _u.proxy_url])

    # Add custom yt-dlp options
    if di.YtdlpOpts:
        import shlex
        try:
            custom_args = shlex.split(di.YtdlpOpts)
        except ValueError:
            custom_args = di.YtdlpOpts.split()
        args.extend(custom_args)

    # Add URL
    args.append(di.URL)

    LogDebug("Executing yt-dlp (attempt): %s", " ".join(args))

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            LogDebug("Successfully retrieved stream info from yt-dlp")
            return result.stdout
        else:
            LogWarn("yt-dlp returned non-zero exit code: %d", result.returncode)
            if result.stderr:
                LogDebug("yt-dlp stderr: %s", result.stderr.decode("utf-8", errors="replace")[:500])
            return None
    except subprocess.TimeoutExpired:
        LogWarn("yt-dlp timed out after 30 seconds")
        return None
    except FileNotFoundError:
        LogWarn("yt-dlp not found at '%s'", di.YtdlpPath)
        return None
    except Exception as e:
        LogWarn("yt-dlp execution error: %s", str(e))
        return None


def execute_ytdlp_with_retry(di: DownloadInfo, max_retries: int = 3) -> Optional[bytes]:
    """Execute yt-dlp with retry logic."""
    for i in range(max_retries):
        LogDebug("Executing yt-dlp (attempt %d/%d)", i + 1, max_retries)
        output = execute_ytdlp(di)
        if output is not None:
            return output
        if i < max_retries - 1:
            time.sleep(2)
    LogWarn("Failed to get stream info from yt-dlp after %d attempts", max_retries)
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
        LogDebug("Failed to parse yt-dlp json: %v", str(e))
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
        LogDebug("Loaded %d adaptive format URLs from yt-dlp", len(adaptive_urls))
    if dash_urls:
        LogDebug("Loaded %d dash format URLs from yt-dlp", len(dash_urls))

    return adaptive_urls, dash_urls, last_sq


# ---------------------------------------------------------------------------
# URL Parsing
# ---------------------------------------------------------------------------

def parse_input_url(di: DownloadInfo) -> bool:
    """Parse the input URL to extract video ID and determine URL type.
    Returns True on success."""
    try:
        parsed = urlparse(di.URL)
    except Exception as e:
        LogError("Error parsing URL: %s", str(e))
        return False

    lower_host = parsed.hostname or ""
    lower_host = lower_host.lower()
    lower_host = lower_host.removeprefix("www.").removeprefix("m.")
    lower_path = (parsed.path or "").lower()
    query = parse_qs(parsed.query)

    if lower_host == "youtube.com":
        if lower_path.startswith("/watch"):
            if "v" not in query:
                LogError("YouTube URL missing video ID")
                return False
            di.VideoID = query["v"][0]
            return True

        elif (lower_path.startswith("/channel/") or lower_path.startswith("/c/") or
              lower_path.startswith("/user/") or lower_path.startswith("/@")):
            # Channel URL - append /live for monitoring
            # Strip sub-page path
            chan_slash_idx = lower_path[1:].find("/") + 1
            no_chan_path = lower_path[chan_slash_idx:]
            if no_chan_path.rfind("/") > 0:
                last_slash = di.URL.rfind("/")
                di.URL = di.URL[:last_slash]
            di.URL = f"{di.URL}/live"
            di.LiveURL = True
            return True

        elif lower_path.startswith("/live/"):
            di.VideoID = parsed.path.removeprefix("/live/")
            return True

        elif lower_path.startswith("/shorts/"):
            di.VideoID = parsed.path.removeprefix("/shorts/")
            return True

    elif lower_host == "youtu.be":
        di.VideoID = parsed.path.strip("/")
        return True

    elif lower_host.endswith(".googlevideo.com"):
        if "noclen" not in query:
            LogError("Given Google Video URL is not for a fragmented stream")
            return False

        di.GVideoDDL = True
        id_val = query.get("id", [""])[0]
        dot_idx = id_val.rfind(".")
        if dot_idx > 0:
            id_val = id_val[:dot_idx]
        di.VideoID = id_val
        di.FormatInfo["id"] = di.VideoID

        sq_idx = di.URL.find("&sq=")
        try:
            itag = int(query.get("itag", ["-1"])[0])
        except ValueError:
            LogError("Error parsing itag parameter of Google Video URL")
            return False

        if sq_idx < 0:
            LogError("Could not find 'sq' parameter in given Google Video URL")
            return False

        if itag == AUDIO_ITAG:
            if not di.GetDownloadUrl(DTYPE_AUDIO):
                di.SetDownloadUrl(DTYPE_AUDIO, di.URL[:sq_idx] + "&sq=%d")
            if not di.GetDownloadUrl(DTYPE_VIDEO) and not di.AudioOnly:
                # Will be handled later via GetVideoInfo
                pass
        else:
            if not di.GetDownloadUrl(DTYPE_VIDEO):
                di.SetDownloadUrl(DTYPE_VIDEO, di.URL[:sq_idx] + "&sq=%d")
            if not di.GetDownloadUrl(DTYPE_AUDIO) and not di.VideoOnly:
                pass

        di.Quality = itag
        return True

    LogError("%s is not a known valid YouTube URL", di.URL)
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
    """Parse --live-from value and set LiveFromSq."""
    if not di.LiveFromVal:
        return

    val = di.LiveFromVal
    if val.lower() == "now":
        di.LiveFromSq = di.LastSq
        LogGeneral("Starting download from current time")
        return

    is_negative = val.startswith("-")
    duration_val = val.removeprefix("-")

    seconds_total = _parse_duration_str(duration_val)
    if seconds_total is None:
        LogError("Unable to parse value as either a duration or a time string: %s", val)
        return

    frag_dur = float(di.TargetDuration)
    seconds_rounded = int(math.ceil(seconds_total / frag_dur) * frag_dur)
    no_of_frags = seconds_rounded // di.TargetDuration

    if is_negative:
        if seconds_total < 0 or seconds_total > LIVE_MAXIMUM_SEEKABLE:
            LogError("Invalid duration specified '%s'. (Maximum video seek time is %d days)",
                     val, LIVE_MAXIMUM_SEEKABLE // 86400)
            return
        if no_of_frags > di.LastSq:
            stream_length = di.LastSq * di.TargetDuration
            LogError("Invalid duration specified. The stream has not been live for that long [Live for %s].",
                     SecondsToDurationAndTimeStr(stream_length))
            return
        di.LiveFromSq = di.LastSq - no_of_frags
        LogGeneral("Jumping back %d seconds from now, and starting to download from that time.", seconds_rounded)
        LogDebug("Jumping back %d frags. Will start from sequence %d [current is %d].", no_of_frags, di.LiveFromSq, di.LastSq)
    else:
        max_sq = di.LastSq
        target_start_frag = no_of_frags
        if di.LastSq < target_start_frag:
            stream_length = di.LastSq * di.TargetDuration
            LogError("Invalid duration specified. The stream has not been live for that long [Live for %s].",
                     SecondsToDurationAndTimeStr(stream_length))
            return
        if target_start_frag < (di.LastSq - LIVE_MAXIMUM_SEEKABLE // di.TargetDuration):
            LogError("YT only retains the livestream 7 days past for seeking, your --live-from value of '%s' is not valid.", val)
            stream_live_time = di.LastSq * di.TargetDuration
            min_seek_time = stream_live_time - LIVE_MAXIMUM_SEEKABLE
            LogError("You must specify a --live-from value between: %s and %s",
                     SecondsToDurationAndTimeStr(min_seek_time),
                     SecondsToDurationAndTimeStr(stream_live_time))
            return
        di.LiveFromSq = target_start_frag
        start_time_str = SecondsToDurationAndTimeStr(di.LiveFromSq * di.TargetDuration)
        total_time_str = SecondsToDurationAndTimeStr((max_sq - di.LiveFromSq) * di.TargetDuration)
        LogGeneral("Starting from stream time '%s' and grabbing '%s' of content (and counting).", start_time_str, total_time_str)
        LogDebug("Starting from sequence %d [max right now is %d]", di.LiveFromSq, max_sq)


def parse_capture_duration(di: DownloadInfo, val: str):
    """Parse --capture-duration value."""
    if not val:
        return
    seconds = _parse_duration_str(val)
    if seconds is None:
        LogError("Unable to parse value as either a Duration or a Time String: %s", val)
        return
    di.CaptureDurationSecs = seconds
    LogGeneral("Downloading a minimum of %s of content and then exiting...", SecondsToDurationAndTimeStr(seconds))


def parse_start_delay(di: DownloadInfo, val: str):
    """Parse --start-delay value."""
    if not val:
        return
    seconds = _parse_duration_str(val)
    if seconds is None:
        LogError("Unable to parse value as either a Duration or a Time String: %s", val)
        return
    di.StartDelaySecs = seconds


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
        LogDebug("Failed to parse yt-dlp JSON: %s", str(e))
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
        if di.GVideoDDL or di.Stopping or di.Unavailable:
            return False
        delta = time.time() - di.LastUpdated
        if delta < DEFAULT_POLL_TIME:
            return False

    first_wait = True
    retry_count = 0
    live_waited = 0

    sel_qualities = []
    if di.SelectedQuality:
        sel_qualities = ParseQualitySelection(VideoQualities, di.SelectedQuality)

    while True:
        json_data = execute_ytdlp_with_retry(di, 3)
        if not json_data:
            LogError("Failed to get stream info from yt-dlp")
            di.Live = False
            di.Unavailable = True
            return False

        data = _parse_ytdlp_info(json_data)
        if not data:
            LogError("Failed to parse yt-dlp output")
            return False

        live_status = data.get("live_status", "")

        # Handle scheduled / upcoming streams
        if live_status == "is_upcoming":
            if di.InProgress:
                LogDebug("Stream status changed to upcoming mid-download")
                return False

            if di.LiveFromVal and di.LiveFromVal.startswith("-"):
                LogError("Option --live-from with a negative duration is not valid for a scheduled stream.")
                return False

            if di.Wait == ACTION_DO_NOT:
                LogError("Stream has not started, and you have opted not to wait.")
                return False

            if first_wait and di.Wait == ACTION_ASK and di.RetrySecs == 0:
                if not di.AskWaitForStream():
                    return False

            if first_wait:
                di.PrintChannelAndTitle(data)
                if not sel_qualities:
                    sel_qualities = GetQualityFromUser(VideoQualities, True)

            release_ts = data.get("release_timestamp")
            if release_ts and di.RetrySecs <= 0:
                cur_time = int(time.time())
                sleep_time = release_ts - cur_time
                if sleep_time > 0:
                    if first_wait:
                        first_wait = False
                    LogGeneral("Stream starts at %s in %d seconds.",
                        time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(release_ts)), sleep_time)
                    LogGeneral("Waiting for this time to elapse...")
                    while sleep_time > 0:
                        time.sleep(min(sleep_time, 60))
                        cur_time = int(time.time())
                        sleep_time = release_ts - cur_time
                    continue

            di.RetrySecs = di.RetrySecs or DEFAULT_POLL_TIME

            if first_wait:
                first_wait = False
                LogGeneral("Waiting for stream, retrying every %d seconds...\n", di.RetrySecs)

            time.sleep(di.RetrySecs)
            live_waited += di.RetrySecs
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
            if di.Live:
                di.Live = False
            else:
                LogError("%s is not a livestream. It would be better to use yt-dlp to download it.", di.URL)
            return False

        # Stream has ended and is being processed
        if live_status in ("was_live", "post_live") and not di.InProgress:
            if not data.get("formats"):
                LogGeneral("Livestream has ended and is being processed. Download URLs not available.")
                return False
            adaptive = data.get("_adaptive_urls", {})
            dash = data.get("_dash_urls", {})
            if not adaptive and not dash:
                LogGeneral("Livestream has been processed. Use yt-dlp instead.")
                return False

        # Stream is live (or was live with formats) — proceed
        di.PrintChannelAndTitle(data)

        with di._lock:
            di.LastUpdated = time.time()

        # Extract format URLs
        dl_urls = {}
        adaptive = data.get("_adaptive_urls", {})
        dash = data.get("_dash_urls", {})
        last_sq = data.get("_last_sq", -1)

        if adaptive:
            LogDebug("Using yt-dlp adaptive formats as primary source")
            dl_urls.update(adaptive)
            if last_sq > 0:
                di.LastSq = last_sq
        elif dash:
            LogDebug("Using yt-dlp dash formats as fallback")
            dl_urls.update(dash)
            if last_sq > 0:
                di.LastSq = last_sq

        if not dl_urls:
            LogError("No download URLs found")
            return False

        # Target duration
        target_dur = data.get("_target_duration")
        if target_dur:
            di.TargetDuration = target_dur
            LogDebug("Target fragment duration: %ds", target_dur)

        # Quality selection (unchanged logic)
        if di.Quality < 0:
            qualities = ["audio_only"]
            found = False

            for qlabel in VideoQualities:
                video_itag = VideoLabelItags[qlabel]
                vp9_ok = video_itag.VP9 in dl_urls
                h264_ok = video_itag.H264 in dl_urls
                av1_ok = video_itag.AV1 in dl_urls

                if qlabel.endswith("60"):
                    base_quality = qlabel[:-2]
                    if base_quality in VideoLabelItags:
                        base_itag = VideoLabelItags[base_quality]
                        if base_itag.AV1 == video_itag.AV1:
                            if base_itag.H264 in dl_urls or base_itag.VP9 in dl_urls:
                                av1_ok = False

                if Contains(qualities, qlabel) or (not vp9_ok and not h264_ok and not av1_ok):
                    continue
                qualities.append(qlabel)

            while not found:
                if not sel_qualities:
                    sel_qualities = GetQualityFromUser(qualities, False)

                for q in sel_qualities:
                    q = q.strip()
                    if q == "best":
                        q = qualities[-1]
                    elif q == "audio":
                        q = "audio_only"

                    video_itag = VideoLabelItags[q]
                    aonly = video_itag.VP9 == AUDIO_ONLY_QUALITY

                    if not di.VideoOnly and AUDIO_ITAG in dl_urls:
                        di.SetDownloadUrl(DTYPE_AUDIO, dl_urls[AUDIO_ITAG])

                    if aonly:
                        di.Quality = AUDIO_ONLY_QUALITY
                        di.SetDownloadUrl(DTYPE_VIDEO, "")
                        found = True
                        break

                    codec_order = di.GetCodecPriorityOrder()
                    LogDebug("Codec priority order: %s", ", ".join(codec_order).upper())
                    for codec in codec_order:
                        if codec == "h264":
                            itag = video_itag.H264
                        elif codec == "vp9":
                            itag = video_itag.VP9
                        elif codec == "av1":
                            itag = video_itag.AV1
                        else:
                            continue

                        if itag == AUDIO_ONLY_QUALITY:
                            continue

                        if codec == "av1" and q.endswith("60"):
                            if video_itag.AV1 in dl_urls:
                                base_quality = q[:-2]
                                if base_quality in VideoLabelItags:
                                    base_itag = VideoLabelItags[base_quality]
                                    if base_itag.AV1 == video_itag.AV1:
                                        if base_itag.H264 in dl_urls or base_itag.VP9 in dl_urls:
                                            LogDebug("Treating %s AV1 itag=%d as unavailable", q, video_itag.AV1)
                                            continue

                        url = dl_urls.get(itag)
                        LogDebug("Codec availability: %s itag=%d ok=%s", codec.upper(), itag, url is not None)
                        if url is None:
                            continue

                        di.SetDownloadUrl(DTYPE_VIDEO, url)
                        di.Quality = itag
                        found = True
                        LogGeneral("Selected quality: %s (%s)", q, codec.upper())
                        break
                    if found:
                        break

                if not found:
                    LogGeneral("The qualities you selected ended up unavailable for this stream")
                    LogGeneral("You will now have the option to select from the available qualities")
                    sel_qualities = []
        else:
            aonly = di.Quality == AUDIO_ONLY_QUALITY
            if not di.VideoOnly and AUDIO_ITAG in dl_urls and IsFragmented(dl_urls.get(AUDIO_ITAG, "")):
                di.SetDownloadUrl(DTYPE_AUDIO, dl_urls[AUDIO_ITAG])
            if not aonly:
                vid_ok = di.Quality in dl_urls
                if vid_ok and IsFragmented(dl_urls.get(di.Quality, "")):
                    di.SetDownloadUrl(DTYPE_VIDEO, dl_urls[di.Quality])

        if not di.InProgress:
            timestamp = data.get("timestamp") or data.get("release_timestamp")
            if timestamp:
                LogGeneral("Stream started at time %s",
                    time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(timestamp)))
            di.SetFormatInfoFromYtdlp(data)
            di.SetMetadataFromFormatInfo()
            thumb_url = data.get("thumbnail", "")
            if thumb_url:
                di.Thumbnail = thumb_url
            di.InProgress = True

        di.Live = (live_status == "is_live")
        return True


def wait_for_start_delay(di: DownloadInfo) -> bool:
    """Wait for --start-delay duration before starting download."""
    if di.Live and di.StartDelaySecs > 0:
        frag_dur = float(di.TargetDuration)
        seconds_rounded = int(math.ceil(di.StartDelaySecs / frag_dur) * frag_dur)
        no_of_frags = seconds_rounded // di.TargetDuration
        di.LiveFromSq = di.LastSq + no_of_frags

        LogGeneral("Waiting %s before starting to download...", SecondsToDurationAndTimeStr(seconds_rounded))
        LogDebug("Will start from sequence %d [current is %d]", di.LiveFromSq, di.LastSq)

        time.sleep(seconds_rounded)

        if seconds_rounded > DEFAULT_POLL_TIME:
            return get_video_info(di)

    return True


# ---------------------------------------------------------------------------
# Fragment Download
# ---------------------------------------------------------------------------

def handle_frag_http_error(di: DownloadInfo, state: FragThreadState, status_code: int, url: str):
    """Handle HTTP error during fragment download."""
    LogDebug("%s: HTTP Error for fragment %d: %d", state.Name, state.SeqNum, status_code)
    di.PrintStatus()

    if status_code == 403:
        state.Is403 = True
        refresh_url(di, state.DataType, url)
    elif status_code == 404 and state.MaxSeq > -1 and not di.IsLive() and state.SeqNum > (state.MaxSeq - 2):
        LogDebug("%s: Stream has ended and fragment within the last two not found, probably not actually created", state.Name)
        di.PrintStatus()
        di.SetFinished(state.DataType)


def handle_frag_download_error(di: DownloadInfo, state: FragThreadState, err: Exception):
    """Handle network error during fragment download."""
    LogDebug("%s: Error with fragment %d: %s", state.Name, state.SeqNum, str(err))
    di.PrintStatus()

    if state.MaxSeq > -1 and not di.IsLive() and state.SeqNum >= (state.MaxSeq - 2):
        LogDebug("%s: Stream has ended and fragment number is within two of the known max, probably not actually created", state.Name)
        di.SetFinished(state.DataType)
        di.PrintStatus()


def continue_fragment_download(di: DownloadInfo, state: FragThreadState) -> bool:
    """Determine whether to continue retrying a fragment download."""
    if di.IsFinished(state.DataType):
        return False

    if di.FragMaxTries > 0 and state.Tries >= di.FragMaxTries:
        state.FullRetries -= 1
        LogDebug("%s: Fragment %d: %d/%d retries", state.Name, state.SeqNum, state.Tries, di.FragMaxTries)
        di.PrintStatus()

        if di.IsLive():
            get_video_info(di)

        if not di.IsLive() or di.IsUnavailable():
            if state.Is403:
                if di.IsUnavailable():
                    LogWarn("%s: Download link likely expired and stream is privated or members only, cannot continue download", state.Name)
                else:
                    LogWarn("%s: Download link has likely expired and the stream has probably finished processing.", state.Name)
                    LogWarn("%s: You might want to use youtube-dl to download instead.", state.Name)
                di.PrintStatus()
                di.SetFinished(state.DataType)
                return False
            elif state.MaxSeq > -1 and state.SeqNum < (state.MaxSeq - 2) and state.FullRetries > 0:
                LogDebug("%s: More than two fragments away from the highest known fragment", state.Name)
                LogDebug("%s: Will try grabbing the fragment %d more times", state.Name, state.FullRetries)
                di.PrintStatus()
            else:
                di.SetFinished(state.DataType)
                return False
        else:
            LogDebug("%s: Fragment %d: Stream still live, continuing download attempt", state.Name, state.SeqNum)
            di.PrintStatus()
            state.Tries = 0

    return True


def refresh_url(di: DownloadInfo, data_type: str, current_url: str):
    """Attempt to get a new download URL on 403 error."""
    if not di.IsGVideoDDL():
        new_url = di.GetDownloadUrl(data_type)
        if not current_url or new_url == current_url:
            LogDebug("%s: Attempting to retrieve a new download URL", data_type)
            di.PrintStatus()
            get_video_info(di)


def download_fragment(di: DownloadInfo, state: FragThreadState, data_queue: Queue):
    """Download a single fragment."""
    state.Tries = 0
    state.FullRetries = 3
    state.Is403 = False
    fname = f"{state.BaseFilePath}.frag{state.SeqNum}.ts"

    while state.Tries < di.FragMaxTries or di.FragMaxTries == 0:
        if di.IsStopping():
            return

        if di.FragMaxTries == 0:
            state.Tries = 0

        base_url = di.GetDownloadUrl(state.DataType)
        seq_url = base_url % state.SeqNum

        dl_start = time.time()

        try:
            host = di.GetDownloadUrlHost(state.DataType)
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
            state.Tries += 1
            if not continue_fragment_download(di, state):
                return
            time.sleep(state.SleepTime)
            continue

        dl_duration = time.time() - dl_start

        if resp.status_code >= 400:
            handle_frag_http_error(di, state, resp.status_code, base_url)
            state.Tries += 1
            if not continue_fragment_download(di, state):
                return
            time.sleep(state.SleepTime)
            continue

        resp_data = resp.content
        if not resp_data:
            state.Tries += 1
            if not continue_fragment_download(di, state):
                return
            time.sleep(state.SleepTime)
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

        if state.ToFile:
            try:
                with open(fname, "wb") as f:
                    f.write(resp_data)
            except Exception as e:
                LogDebug("%s: Failed to write fragment %d to file: %s", state.Name, state.SeqNum, str(e))
                di.PrintStatus()
                state.Tries += 1
                if not continue_fragment_download(di, state):
                    TryDelete(fname)
                    return
                time.sleep(state.SleepTime)
                continue
            data = None
        else:
            data = bytearray(resp_data)

        # Slow fragment detection
        is_slow = False
        if header_seqnum < 0 or state.SeqNum < (header_seqnum - 10):
            is_slow = dl_duration > (di.TargetDuration * 1.5)

        data_queue.put(Fragment(
            Seq=state.SeqNum,
            XHeadSeqNum=header_seqnum,
            FileName=fname,
            Data=data,
            Slow=is_slow,
            MimeType=mime_type,
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
            Name=name,
            BaseFilePath=di.GetBaseFilePath(data_type),
            DataType=data_type,
            ToFile=di.FragFiles,
            SleepTime=float(di.TargetDuration),
        )

        end_seq = 0
        while True:
            try:
                seq_info = seq_queue.get(timeout=1)
            except Empty:
                if di.IsStopping() or di.IsFinished(data_type):
                    break
                continue

            if di.IsStopping() or di.IsFinished(data_type):
                break

            # --capture-duration check
            if di.CaptureDurationSecs != 0:
                if end_seq == 0:
                    cap_seq_cnt = int(math.ceil(di.CaptureDurationSecs / di.TargetDuration))
                    end_seq = seq_info.CurSequence + cap_seq_cnt
                elif seq_info.CurSequence >= end_seq:
                    LogDebug("%s: Reached the maximum duration specified by --capture-duration.", name)
                    di.SetFinished(data_type)
                    break

            if seq_info.MaxSequence > -1 and not di.IsLive() and seq_info.CurSequence >= seq_info.MaxSequence:
                LogDebug("%s: Stream is finished and highest sequence reached", name)
                di.SetFinished(data_type)
                break

            state.SeqNum = seq_info.CurSequence
            state.MaxSeq = seq_info.MaxSequence

            download_fragment(di, state, data_queue)
    except Exception as e:
        LogError("%s: Unhandled error in download worker: %s", name, str(e))
        import traceback
        LogDebug("%s", traceback.format_exc())
    finally:
        di.DecrementJobs(data_type)
        LogDebug("%s: exiting", name)
        di.PrintStatus()


# ---------------------------------------------------------------------------
# Stream Download Orchestrator
# ---------------------------------------------------------------------------

def download_stream(di: DownloadInfo, data_type: str, data_file: str,
                    progress_queue: Queue, done_event: threading.Event):
    """Orchestrate downloading a single stream (audio or video).
    Manages worker threads and sequential fragment writing."""
    data_queue = Queue(maxsize=di.Jobs * 2)
    seq_queue = Queue(maxsize=di.Jobs * 2)
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
        itag = di.Quality

    log_name = f"{data_type}-download"

    # Open file for writing
    f = None
    resumed_state = False

    if itag in di.DLState and di.DLState[itag].Fragments > 0:
        if di.LiveFromSq != 0:
            if di.LiveFromVal:
                LogWarn("%s: Option --live-from is being ignored as a download is being resumed.", data_type)
            if di.StartDelaySecs != 0:
                LogWarn("%s: Option --start-delay is being ignored as a download is being resumed.", data_type)

        try:
            f = open(data_file, "r+b")
            f.seek(di.DLState[itag].Size)
            resumed_state = True
        except FileNotFoundError:
            LogDebug("%s: State file found but data file missing. Starting fresh.", data_type)
            f = open(data_file, "wb")
            resumed_state = False
        except Exception as e:
            LogWarn("%s: Failed to open %s to resume: %s", data_type, data_file, str(e))
            LogWarn("%s: Will truncate and start from the beginning", data_type)
            f = open(data_file, "wb")
            resumed_state = False
    else:
        f = open(data_file, "wb")

    if resumed_state:
        start_frag = di.DLState[itag].StartFrag
        cur_frag = start_frag + di.DLState[itag].Fragments
        max_seqs = di.LastSq
        LogInfo("%s: Resuming download from sequence %d", data_type, cur_frag)
    else:
        if di.LastSq >= 0:
            cur_frag = di.LastSq - (LIVE_MAXIMUM_SEEKABLE // di.TargetDuration)
            max_seqs = di.LastSq

        if di.LiveFromSq != 0:
            cur_frag = di.LiveFromSq
            start_frag = cur_frag
            LogDebug("%s: Starting from sequence %d (latest is %d)", data_type, start_frag, di.LastSq)
        elif cur_frag > 0:
            LogWarn("%s: YT only retains the livestream 7 days past for seeking, starting from sequence %d (latest is %d)", data_type, cur_frag, di.LastSq)
            start_frag = cur_frag
        else:
            cur_frag = 0

        if itag not in di.DLState:
            di.DLState[itag] = DownloadState()
        di.DLState[itag].StartFrag = start_frag

    cur_seq = cur_frag

    # Spawn initial worker threads
    workers_started = 0
    for _ in range(di.Jobs):
        job_name = f"{data_type}{workers_started + 1}"
        di.IncrementJobs(data_type)
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
            downloading = di.GetActiveJobCount(data_type) > 0
            stopping = di.IsStopping()

            if stopping or not downloading or di.IsFinished(data_type):
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

                if data.XHeadSeqNum > max_seqs:
                    max_seqs = data.XHeadSeqNum

                if max_seqs > 0:
                    while (cur_seq <= max_seqs + 1 and active_downloads < di.Jobs) or active_downloads < 1:
                        seq_queue.put(SeqChanInfo(cur_seq, max_seqs))
                        cur_seq += 1
                        active_downloads += 1
                else:
                    seq_queue.put(SeqChanInfo(cur_seq, max_seqs))
                    cur_seq += 1
                    active_downloads += 1

                if data.Slow:
                    if (data.Seq - last_slow_frag) < 10:
                        slow_frags += 1
                    else:
                        slow_frags = 1
                    last_slow_frag = data.Seq

            if not data_to_write and not data_received and downloading:
                if not stopping and active_downloads <= 0:
                    LogDebug("%s: Somehow no active downloads and no data to write", log_name)
                    LogDebug("%s: Fragment this happened at: %d", log_name, cur_frag)
                    di.PrintStatus()
                    while active_downloads < di.GetActiveJobCount(data_type):
                        seq_queue.put(SeqChanInfo(cur_seq, max_seqs))
                        cur_seq += 1
                        active_downloads += 1

                time.sleep(0.1)
                continue

            # Write fragments in order
            i = 0
            while i < len(data_to_write) and tries > 0:
                data = data_to_write[i]
                if data.Seq != cur_frag:
                    i += 1
                    continue

                # Read fragment data from file if needed
                if di.FragFiles:
                    try:
                        with open(data.FileName, "rb") as frag_f:
                            read_bytes = frag_f.read()
                        data.Data = bytearray(read_bytes)
                    except Exception as e:
                        tries -= 1
                        LogWarn("%s: Error when attempting to read fragment %d for writing: %s", log_name, cur_frag, str(e))
                        di.PrintStatus()
                        if tries > 0:
                            LogWarn("%s: Will try %d more time(s)", log_name, tries)
                            di.PrintStatus()
                        continue

                buf = bytes(data.Data)

                # Remove unwanted MP4 atoms
                mime = data.MimeType
                if buf:
                    if mime.endswith("/mp4") or not mime:
                        bad_atoms = ["sidx"]
                        if cur_frag != start_frag:
                            bad_atoms.append("ftyp")
                        buf = RemoveAtoms(bytearray(buf), *bad_atoms)
                        buf = bytes(buf)

                try:
                    f.write(buf)
                except Exception as e:
                    tries -= 1
                    LogWarn("%s: Error when attempting to write fragment %d to %s: %s", log_name, cur_frag, data_file, str(e))
                    di.PrintStatus()
                    if tries > 0:
                        LogWarn("%s: Will try %d more time(s)", log_name, tries)
                        di.PrintStatus()
                    continue

                cur_frag += 1
                progress_queue.put(ProgressInfo(itag, len(buf), max_seqs, start_frag))

                if di.FragFiles:
                    try:
                        os.remove(data.FileName)
                    except OSError as e:
                        LogWarn("%s: Error deleting fragment %d: %s", log_name, data.Seq, str(e))
                        LogWarn("%s: Will try again after the download has finished", log_name)
                        deleting_frags.append(data.FileName)
                        di.PrintStatus()

                data_to_write.pop(i)
                tries = 10
                i = 0

            if not downloading:
                break

            if tries <= 0:
                LogWarn("%s: Stopping download, something must be wrong...", log_name)
                di.PrintStatus()
                di.Stop()
    except Exception as e:
        LogError("%s: Unhandled error in download stream: %s", log_name, str(e))
        import traceback
        LogDebug("%s", traceback.format_exc())
    finally:
        f.close()

        # Cleanup remaining fragment files
        if di.FragFiles:
            for d in data_to_write:
                TryDelete(d.FileName)
        for d in deleting_frags:
            TryDelete(d)

        done_event.set()
        LogDebug("%s thread closing", log_name)
        di.PrintStatus()


# ---------------------------------------------------------------------------
# Netscape Cookies Parser (delegates to utils)
# ---------------------------------------------------------------------------

def parse_netscape_cookies(di: DownloadInfo, cookie_file: str) -> bool:
    """Parse netscape cookies file and set on HTTP session."""
    from utils import ParseNetscapeCookiesFile
    try:
        jar = ParseNetscapeCookiesFile(cookie_file)
        session.cookies = jar
        LogInfo("Loaded cookie file %s", cookie_file)
        return True
    except Exception as e:
        LogError("Failed to load cookies file: %s", str(e))
        return False
