from pathlib import Path
import shutil
import struct
import sys
import time
from typing import Optional

import requests
from colorama import init as colorama_init, Fore, Style

# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------

def setup():
    """Initialize colorama for ANSI color support on Windows."""
    colorama_init()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOGLEVEL_QUIET = 0
LOGLEVEL_ERROR = 1
LOGLEVEL_WARNING = 2
LOGLEVEL_INFO = 3
LOGLEVEL_DEBUG = 4
LOGLEVEL_TRACE = 5

KiB = 1024.0
MiB = 1024.0 * 1024.0
GiB = 1024.0 * 1024.0 * 1024.0

DEFAULT_POLL_TIME = 15
MINIMUM_MONITOR_TIME = 30
DEFAULT_MONITOR_TIME = 60
DEFAULT_VIDEO_QUALITY = "best"
DEFAULT_FILENAME_FORMAT = "%(title)s-%(id)s"

# Max filename length (255 - len(".description"))
MAX_FILENAME_LENGTH = 243

# 7 days in seconds
LIVE_MAXIMUM_SEEKABLE = 86400 * 7

DEFAULT_THREADS = 1
DEFAULT_FRAG_MAX_TRIES = 10

DTYPE_AUDIO = "audio"
DTYPE_VIDEO = "video"
AUDIO_ITAG = 140
AUDIO_ONLY_QUALITY = 0

ACTION_ASK = 0
ACTION_DO = 1
ACTION_DO_NOT = 2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

loglevel = LOGLEVEL_WARNING

COLORS = {
    LOGLEVEL_ERROR: Fore.RED,
    LOGLEVEL_WARNING: Fore.YELLOW,
    LOGLEVEL_INFO: Fore.GREEN,
    LOGLEVEL_DEBUG: Fore.CYAN,
    LOGLEVEL_TRACE: Fore.MAGENTA,
}

PREFIXES = {
    LOGLEVEL_ERROR: "ERROR",
    LOGLEVEL_WARNING: "WARNING",
    LOGLEVEL_INFO: "INFO",
    LOGLEVEL_DEBUG: "DEBUG",
    LOGLEVEL_TRACE: "TRACE",
}


def _log(level: int, msg: str, *args):
    if level > loglevel:
        return
    color = COLORS.get(level, "")
    prefix = PREFIXES.get(level, "")
    formatted = msg % args if args else msg
    if level > LOGLEVEL_QUIET:
        ts = time.strftime("%Y/%m/%d %H:%M:%S")
        body = f"{ts} {color}{prefix}: {formatted}{Style.RESET_ALL}"
        if status_newlines:
            line = body + "\n"
        else:
            line = "\r" + body + "\033[K\n"
    else:
        line = f"{formatted}\n"
    sys.stderr.write(line)
    sys.stderr.flush()


def LogError(msg: str, *args):
    _log(LOGLEVEL_ERROR, msg, *args)


def LogWarn(msg: str, *args):
    _log(LOGLEVEL_WARNING, msg, *args)


def LogInfo(msg: str, *args):
    _log(LOGLEVEL_INFO, msg, *args)


def LogDebug(msg: str, *args):
    _log(LOGLEVEL_DEBUG, msg, *args)


def SetLoglevel(level: int):
    """Set the global log level."""
    global loglevel
    loglevel = level


def LogGeneral(msg: str, *args):
    """Log a message always (even in quiet mode)."""
    formatted = msg % args if args else msg
    ts = time.strftime("%Y/%m/%d %H:%M:%S")
    sys.stderr.write(f"{ts} {formatted}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------

# Session-level state
session: requests.Session = requests.Session()
proxy_url: Optional[str] = None
cookie_file: str = ""
status_newlines: bool = False
_network_type = "tcp"  # "tcp", "tcp4", "tcp6"


def InitializeHttpClient(proxy: Optional[str] = None):
    """Set up the HTTP session with proper headers and optional proxy."""
    global session, proxy_url

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:87.0) Gecko/20100101 Firefox/87.0",
        "Origin": "https://www.youtube.com",
    })

    if proxy:
        proxy_url = proxy
        session.proxies.update({
            "http": proxy,
            "https": proxy,
        })

    # Configure adapter for connection pooling
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)


def DownloadThumbnail(url: str, fname: str, file_mode: int = 0o644) -> bool:
    """Download a thumbnail image to the given file."""
    try:
        resp = session.get(url, timeout=(15, 30))
        resp.raise_for_status()
        with open(fname, "wb") as f:
            f.write(resp.content)
        if file_mode:
            Path(fname).chmod(file_mode)
        return True
    except Exception as e:
        LogWarn("Failed to download thumbnail: %v", str(e))
        TryDelete(fname)
        return False


# ---------------------------------------------------------------------------
# Filename formatting
# ---------------------------------------------------------------------------

# Characters not allowed in filenames (Windows + common)
_FILENAME_REPLACEMENTS_NORMAL = {
    "<": "_", ">": "_", ":": "_", '"': "_",
    "/": "_", "\\": "_", "|": "_", "?": "_", "*": "_",
}

_FILENAME_REPLACEMENTS_LOOKALIKE = {
    "<": "＜",  # ＜
    ">": "＞",  # ＞
    ":": "：",  # ：
    '"': "″",  # ″
    "/": "⧸",  # ⧸
    "\\": "⧹", # ⧹
    "|": "｜",  # ｜
    "?": "？",  # ？
    "*": "＊",  # ＊
}

# Blacklisted keys for filename formatting (description can be too long)
FILENAME_FORMAT_BLACKLIST = ["description"]


def SterilizeFilename(s: str, lookalike_chars: bool = False) -> str:
    """Replace invalid filename characters."""
    replacements = _FILENAME_REPLACEMENTS_LOOKALIKE if lookalike_chars else _FILENAME_REPLACEMENTS_NORMAL
    for old, new in replacements.items():
        s = s.replace(old, new)
    return s


def FormatPythonMapString(format_str: str, vals: dict) -> str:
    """Format a string using Python's %(key)s style, similar to youtube-dl.
    Raises KeyError if a key is not found."""
    # Blacklist certain keys
    safe_vals = {}
    for k, v in vals.items():
        if k.lower() in FILENAME_FORMAT_BLACKLIST:
            safe_vals[k] = ""
        else:
            safe_vals[k] = v
    return format_str % safe_vals


def TruncateString(s: str, max_bytes: int) -> str:
    """Truncate string to not exceed max_bytes in UTF-8 encoding."""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    # Truncate byte by byte, trying not to break multi-byte chars
    truncated = encoded[:max_bytes]
    # Remove any incomplete multi-byte sequence at the end
    while True:
        try:
            result = truncated.decode("utf-8")
            break
        except UnicodeDecodeError:
            truncated = truncated[:-1]
            if not truncated:
                return ""
    return result


def FormatFilename(format_str: str, vals: dict, lookalike_chars: bool = False) -> str:
    """Format output filename with sanitized values."""
    fname_vals = {}
    for k, v in vals.items():
        fname_vals[k] = SterilizeFilename(v, lookalike_chars)

    try:
        result = FormatPythonMapString(format_str, fname_vals)
    except KeyError as e:
        raise KeyError(f"Unknown output format key: {e}")

    # Check filename length
    fname = Path(result).name
    if len(fname.encode("utf-8")) > MAX_FILENAME_LENGTH:
        LogWarn("Formatted filename is too long. Truncating the title to try and fix.")
        bytes_over = len(fname.encode("utf-8")) - MAX_FILENAME_LENGTH
        title = fname_vals.get("title", "")
        truncate_len = len(title.encode("utf-8")) - bytes_over
        if truncate_len > 0:
            fname_vals["title"] = TruncateString(title, truncate_len)
            try:
                result = FormatPythonMapString(format_str, fname_vals)
            except KeyError:
                pass

    return result


# ---------------------------------------------------------------------------
# MP4 Atom Removal
# ---------------------------------------------------------------------------

def _get_atoms(data: bytes) -> dict:
    """Parse MP4 atoms from data. Returns {name: (offset, length)}."""
    atoms = {}
    ofs = 0
    while ofs + 8 <= len(data):
        # First 4 bytes: atom length (big-endian)
        try:
            a_len = struct.unpack(">I", data[ofs:ofs + 4])[0]
        except struct.error:
            break
        if a_len <= 0 or ofs + a_len > len(data):
            break
        a_name = data[ofs + 4:ofs + 8].decode("ascii", errors="replace")
        atoms[a_name] = (ofs, a_len)
        ofs += a_len
    return atoms


def RemoveAtoms(data: bytearray, *atom_names: str) -> bytearray:
    """Remove specified MP4 atoms from the data buffer. Modifies in place."""
    atoms = _get_atoms(bytes(data))

    # Collect atoms to remove, sorted by offset descending
    to_remove = []
    for name in atom_names:
        if name in atoms:
            to_remove.append(atoms[name])

    to_remove.sort(key=lambda x: x[0], reverse=True)

    for ofs, a_len in to_remove:
        del data[ofs:ofs + a_len]

    return data


# ---------------------------------------------------------------------------
# File Helpers
# ---------------------------------------------------------------------------

def Exists(filepath: str) -> bool:
    """Check if a file exists."""
    return Path(filepath).exists()


def TryDelete(fname: str):
    """Try to delete a file, ignoring if it doesn't exist."""
    try:
        if Path(fname).exists():
            LogInfo("Deleting file %s", fname)
            Path(fname).unlink()
    except OSError as e:
        LogWarn("Error deleting file: %s", str(e))


def TryMove(src: str, dst: str) -> Optional[Exception]:
    """Try to rename/move a file. Falls back to copy+delete. Returns error or None."""
    if not Path(src).exists():
        return None

    LogInfo("Moving file %s to %s", src, dst)
    try:
        Path(src).rename(dst)
        return None
    except OSError as e:
        LogWarn("Error moving file: %s", str(e))
        LogWarn("Attempting to copy file instead")
        try:
            shutil.copy2(src, dst)
            Path(src).unlink()
            return None
        except OSError as e2:
            LogWarn("Error copying file: %s", str(e2))
            return e2


def CleanupFiles(files: list):
    """Delete all files in the list."""
    for f in files:
        TryDelete(f)


# ---------------------------------------------------------------------------
# Netscape Cookies Parser
# ---------------------------------------------------------------------------

def ParseNetscapeCookiesFile(filepath: str) -> requests.cookies.RequestsCookieJar:
    """Parse a Netscape-format cookies.txt file into a RequestsCookieJar."""
    jar = requests.cookies.RequestsCookieJar()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                # Format: domain flag path secure expires name value
                domain = parts[0]
                # flag = parts[1]  # TRUE/FALSE
                path = parts[2]
                secure = parts[3].upper() == "TRUE"
                try:
                    expires = int(parts[4])
                except ValueError:
                    expires = None
                name = parts[5]
                value = parts[6]

                jar.set(
                    name=name,
                    value=value,
                    domain=domain,
                    path=path,
                    secure=secure,
                    expires=expires,
                )
        LogDebug("Loaded %d cookies from %s", len(jar), filepath)
    except Exception as e:
        LogWarn("Failed to load cookies file: %s", str(e))
    return jar


# ---------------------------------------------------------------------------
# Formatting Utilities
# ---------------------------------------------------------------------------

def FormatSize(bsize: int) -> str:
    """Format a byte count into human-readable form (KiB, MiB, GiB)."""
    b = float(bsize)
    if b >= GiB:
        return f"{b / GiB:.2f}GiB"
    elif b >= MiB:
        return f"{b / MiB:.2f}MiB"
    elif b >= KiB:
        return f"{b / KiB:.2f}KiB"
    return f"{bsize}B"


def SecondsToDurationStr(seconds: int) -> str:
    """Convert seconds to a human-readable duration string like '1d5h30m10s'."""
    days = seconds // 86400
    seconds -= days * 86400
    hours = seconds // 3600
    seconds -= hours * 3600
    minutes = seconds // 60
    seconds -= minutes * 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return "".join(parts)


def SecondsToTimeStr(seconds: int) -> str:
    """Convert seconds to HH:MM:SS or MM:SS format."""
    hours = seconds // 3600
    seconds -= hours * 3600
    minutes = seconds // 60
    seconds -= minutes * 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def SecondsToDurationAndTimeStr(seconds: int) -> str:
    """Combined duration + time string like '1h30m (1:30:00)'."""
    return f"{SecondsToDurationStr(seconds)} ({SecondsToTimeStr(seconds)})"


def Contains(arr: list, val: str) -> bool:
    """Case-insensitive linear search."""
    val_lower = val.strip().lower()
    for s in arr:
        if s.strip().lower() == val_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Quality Constants and Selection
# ---------------------------------------------------------------------------

class VideoItag:
    def __init__(self, h264: int, vp9: int, av1: int):
        self.H264 = h264
        self.VP9 = vp9
        self.AV1 = av1


VideoLabelItags = {
    "audio_only": VideoItag(0, 0, 0),
    "144p":       VideoItag(160, 278, 394),
    "240p":       VideoItag(133, 242, 395),
    "360p":       VideoItag(134, 243, 396),
    "480p":       VideoItag(135, 244, 397),
    "720p":       VideoItag(136, 247, 398),
    "720p60":     VideoItag(298, 302, 398),
    "1080p":      VideoItag(137, 248, 399),
    "1080p60":    VideoItag(299, 303, 399),
    "1440p":      VideoItag(264, 271, 400),
    "1440p60":    VideoItag(304, 308, 400),
    "2160p":      VideoItag(266, 313, 401),
    "2160p60":    VideoItag(305, 315, 401),
}

VideoQualities = [
    "audio_only",
    "144p",
    "240p",
    "360p",
    "480p",
    "720p",
    "720p60",
    "1080p",
    "1080p60",
    "1440p",
    "1440p60",
    "2160p",
    "2160p60",
]


def MakeQualityList(formats: list) -> str:
    """Make a comma-separated list of available formats."""
    return ", ".join(formats) + ", best"


def ParseQualitySelection(formats: list, quality: str) -> list:
    """Parse a slash-delimited user quality selection string."""
    sel_qualities = []
    quality = quality.strip().lower()
    qualities = [q.strip() for q in quality.split("/")]

    for q in qualities:
        if q == "best":
            sel_qualities.append(q)
            continue
        elif q == "audio":
            sel_qualities.append(q)
            continue

        for v in formats:
            if q == v:
                sel_qualities.append(q)
                break

    if len(sel_qualities) < 1:
        print("No valid qualities selected")

    return sel_qualities


def GetQualityFromUser(formats: list, waiting: bool = False) -> list:
    """Prompt the user to select a video quality."""
    qualities = MakeQualityList(formats)

    if waiting:
        print(
            "Since you are going to wait for the stream, you must pre-emptively "
            "select a video quality.\n"
            "There is no way to know which qualities will be available before "
            "the stream starts, so a list of all possible stream qualities will "
            "be presented.\n"
            "You can use youtube-dl style selection (slash-delimited first to "
            "last preference). Default is 'best'"
        )

    print(f"Available video qualities: {qualities}")

    sel_qualities = []
    while len(sel_qualities) < 1:
        quality = GetUserInput("Enter desired video quality: ")
        quality = quality.strip().lower()
        if len(quality) == 0:
            quality = DEFAULT_VIDEO_QUALITY
        sel_qualities = ParseQualitySelection(formats, quality)

    return sel_qualities


# ---------------------------------------------------------------------------
# User Input with Signal Handling
# ---------------------------------------------------------------------------

def GetUserInput(prompt: str) -> str:
    """Get user input, handling Ctrl+C gracefully."""
    try:
        return input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print("\nExiting...")
        sys.exit(1)


def GetYesNo(prompt: str) -> bool:
    """Ask a yes/no question."""
    answer = GetUserInput(f"{prompt} [y/N]: ")
    return answer.lower().startswith("y")


# ---------------------------------------------------------------------------
# ffmpeg Argument Builder
# ---------------------------------------------------------------------------

def GetFFmpegArgs(audio_file: str, video_file: str, thumbnail: str,
                  file_dir: str, file_name: str, only_audio: bool,
                  only_video: bool, download_thumbnail: bool,
                  mkv: bool, add_meta: bool, metadata: dict) -> dict:
    """Build ffmpeg command arguments for muxing.
    Returns {"args": [...], "file_name": "..."}."""

    ffmpeg_args = [
        "-hide_banner",
        "-nostdin",
        "-loglevel", "fatal",
        "-stats",
    ]

    if download_thumbnail and not mkv:
        ffmpeg_args.extend(["-i", thumbnail])

    if only_audio:
        ext = "m4a"
    elif mkv:
        ext = "mkv"
    else:
        ext = "mp4"

    # Find a non-conflicting output filename
    merge_counter = 0
    merge_file = Path(file_dir) / f"{file_name}.{ext}"
    while merge_file.exists() and merge_counter < 10:
        merge_counter += 1
        merge_file = Path(file_dir) / f"{file_name}-{merge_counter}.{ext}"

    if not only_video:
        ffmpeg_args.extend([
            "-seekable", "0",
            "-thread_queue_size", "1024",
            "-i", audio_file,
        ])

    if not only_audio:
        ffmpeg_args.extend([
            "-seekable", "0",
            "-thread_queue_size", "1024",
            "-i", video_file,
        ])
        if not mkv:
            ffmpeg_args.extend(["-movflags", "faststart"])

        if download_thumbnail and not mkv:
            ffmpeg_args.extend(["-map", "0"])
            if not only_video:
                ffmpeg_args.extend(["-map", "2"])
            ffmpeg_args.extend(["-map", "1"])

    ffmpeg_args.extend(["-c", "copy"])

    if download_thumbnail:
        if mkv:
            ffmpeg_args.extend([
                "-attach", thumbnail,
                "-metadata:s:t", "filename=cover_land.jpg",
                "-metadata:s:t", "mimetype=image/jpeg",
            ])
        else:
            ffmpeg_args.extend(["-disposition:v:0", "attached_pic"])

    if add_meta:
        for k, v in metadata.items():
            if v:
                ffmpeg_args.extend(["-metadata", f"{k.upper()}={v}"])

    ffmpeg_args.append(merge_file)

    return {"args": ffmpeg_args, "file_name": merge_file}


# ---------------------------------------------------------------------------
# Subprocess Execution
# ---------------------------------------------------------------------------

def Execute(prog: str, args: list) -> int:
    """Execute an external process. Returns exit code."""
    import subprocess

    LogDebug("Executing command: %s %s", prog, " ".join(map(str, args)))

    try:
        result = subprocess.run(
            [prog] + args,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        )
        if result.stderr:
            sys.stderr.buffer.write(result.stderr)
            sys.stderr.buffer.flush()
        return result.returncode
    except FileNotFoundError:
        LogError("%s not found.", prog)
        return -1
    except Exception as e:
        LogError("Error executing %s: %s", prog, str(e))
        return -1


# ---------------------------------------------------------------------------
# Miscellaneous
# ---------------------------------------------------------------------------

def IsFragmented(url: str) -> bool:
    """Check if a URL is for a fragmented (livestream) stream.
    Fragmented streams have 'noclen' in the URL, VODs have 'clen'."""
    return "noclen" in url.lower()



