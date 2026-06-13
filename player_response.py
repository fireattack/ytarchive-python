import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional

from utils import (
    DownloadData, GetVideoIdFromWatchPage, GenerateSAPISIDHash,
    LogDebug, LogError, LogGeneral, LogWarn, LogTrace,
    LogInfo, session, DEFAULT_POLL_TIME,
    VideoQualities, ParseQualitySelection, GetQualityFromUser,
    IsFragmented, LOGLEVEL_QUIET,
    ACTION_ASK, ACTION_DO_NOT,
)
import utils

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLAYABLE_OK = "OK"
PLAYABLE_OFFLINE = "LIVE_STREAM_OFFLINE"
PLAYABLE_UNPLAYABLE = "UNPLAYABLE"
PLAYABLE_ERROR = "ERROR"

PLAYER_RESPONSE_FOUND = 0
PLAYER_RESPONSE_NOT_FOUND = 1
PLAYER_RESPONSE_NOT_USABLE = 2

WEB_API_POST_DATA = """{
    "context": {
        "client": {
            "clientName": "%s",
            "clientVersion": "%s",
            "hl": "en"
        }
    },
    "videoId": "%s",
    "playbackContext": {
        "contentPlaybackContext": {
            "html5Preference": "HTML5_PREF_WANTS"
        }
    },
    "serviceIntegrityDimensions": {
        "poToken": "%s"
    }
}"""

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class YTCFG:
    """YouTube client configuration scraped from watch page."""
    DelegatedSessionId: str = ""
    IdToken: str = ""
    Hl: str = ""
    InnertubeApiKey: str = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
    InnertubeClientName: str = "WEB"
    InnertubeClientVersion: str = "2.20241119.01.01"
    InnertubeCtxClientName: int = 1
    InnertubeCtxClientVersion: str = "2.20241119.01.01"
    SessionIndex: str = ""
    VisitorData: str = ""


def GetDefaultYTCFG() -> YTCFG:
    """Create a YTCFG with default values."""
    return YTCFG()


# ---------------------------------------------------------------------------
# JSON Scraping from HTML
# ---------------------------------------------------------------------------

_ytplayer_pattern = re.compile(r'var\s+ytInitialPlayerResponse\s*=\s*(\{.*?\});', re.DOTALL)
_ytinitialdata_pattern = re.compile(r'var\s+ytInitialData\s*=\s*(\{.*?\});', re.DOTALL)
_ytcfg_pattern = re.compile(r'ytcfg\.set\((\{.*?\})\);', re.DOTALL)


def _get_json_from_html(html_data: str, pattern: re.Pattern) -> Optional[str]:
    """Extract a JSON object from HTML using a regex pattern."""
    match = pattern.search(html_data)
    if match:
        return match.group(1)
    return None


def GetPlayerResponseJson(html_data: str) -> Optional[str]:
    """Extract ytInitialPlayerResponse JSON from watch page HTML."""
    return _get_json_from_html(html_data, _ytplayer_pattern)


def GetInitialDataJson(html_data: str) -> Optional[str]:
    """Extract ytInitialData JSON from page HTML."""
    return _get_json_from_html(html_data, _ytinitialdata_pattern)


def GetYTCFGFromHtml(html_data: str) -> Optional[str]:
    """Extract ytcfg.set(...) JSON from watch page HTML."""
    return _get_json_from_html(html_data, _ytcfg_pattern)


# ---------------------------------------------------------------------------
# Player Response Types (as dicts for simplicity, or we parse dynamically)
# ---------------------------------------------------------------------------

def parse_player_response(json_str: str) -> Optional[dict]:
    """Parse a PlayerResponse JSON string into a dict."""
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return None


def parse_initial_data(json_str: str) -> Optional[dict]:
    """Parse a ytInitialData JSON string into a dict."""
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Player Response Access Helpers
# ---------------------------------------------------------------------------

def pr_playability_status(pr: dict) -> str:
    """Get playability status string from player response."""
    try:
        return pr["playabilityStatus"]["status"]
    except (KeyError, TypeError):
        return ""


def pr_playability_reason(pr: dict) -> str:
    """Get playability reason from player response."""
    try:
        return pr["playabilityStatus"]["reason"]
    except (KeyError, TypeError):
        return ""


def pr_video_details(pr: dict) -> dict:
    """Get videoDetails from player response."""
    return pr.get("videoDetails", {}) if pr else {}


def pr_video_id(pr: dict) -> str:
    """Get video ID from player response video details."""
    return pr_video_details(pr).get("videoId", "")


def pr_is_live_content(pr: dict) -> bool:
    """Check if video is live content from player response."""
    return pr_video_details(pr).get("isLiveContent", False)


def pr_author(pr: dict) -> str:
    """Get channel author from player response."""
    return pr_video_details(pr).get("author", "")


def pr_title(pr: dict) -> str:
    """Get video title from player response."""
    return pr_video_details(pr).get("title", "")


def pr_streaming_data(pr: dict) -> dict:
    """Get streamingData from player response."""
    return pr.get("streamingData", {}) if pr else {}


def pr_adaptive_formats(pr: dict) -> list:
    """Get adaptiveFormats from player response."""
    return pr_streaming_data(pr).get("adaptiveFormats", [])


def pr_dash_manifest_url(pr: dict) -> str:
    """Get DASH manifest URL from player response."""
    return pr_streaming_data(pr).get("dashManifestUrl", "")


def pr_microformat(pr: dict) -> dict:
    """Get microformat renderer from player response."""
    try:
        return pr["microformat"]["playerMicroformatRenderer"]
    except (KeyError, TypeError):
        return {}


def pr_live_broadcast_details(pr: dict) -> dict:
    """Get live broadcast details from player response."""
    return pr_microformat(pr).get("liveBroadcastDetails", {})


def pr_is_live_now(pr: dict) -> bool:
    """Check if stream is currently live."""
    return pr_live_broadcast_details(pr).get("isLiveNow", False)


def pr_start_timestamp(pr: dict) -> str:
    """Get stream start timestamp."""
    return pr_live_broadcast_details(pr).get("startTimestamp", "")


def pr_end_timestamp(pr: dict) -> str:
    """Get stream end timestamp."""
    return pr_live_broadcast_details(pr).get("endTimestamp", "")


def pr_thumbnail_url(pr: dict) -> str:
    """Get first thumbnail URL from player response."""
    try:
        thumbnails = pr_microformat(pr).get("thumbnail", {}).get("thumbnails", [])
        if thumbnails:
            return thumbnails[0].get("url", "")
    except (KeyError, TypeError, IndexError):
        pass
    return ""


def pr_publish_date(pr: dict) -> str:
    """Get publish date from player response."""
    return pr_microformat(pr).get("publishDate", "")


def pr_upload_date(pr: dict) -> str:
    """Get upload date from player response."""
    return pr_microformat(pr).get("uploadDate", "")


def pr_scheduled_start_time(pr: dict) -> Optional[int]:
    """Get scheduled start time as unix timestamp."""
    try:
        ts = pr["playabilityStatus"]["liveStreamability"]["liveStreamabilityRenderer"]["offlineSlate"]["liveStreamOfflineSlateRenderer"]["scheduledStartTime"]
        return int(ts)
    except (KeyError, TypeError, ValueError):
        return None


def pr_is_logged_out(pr: dict) -> bool:
    """Check if response context shows logged out."""
    try:
        return pr.get("responseContext", {}).get("mainAppWebResponseContext", {}).get("loggedOut", True)
    except (KeyError, TypeError):
        return True


# ---------------------------------------------------------------------------
# Channel /streams Page
# ---------------------------------------------------------------------------

def _get_newest_stream_from_streams(di) -> Optional[str]:
    """Scrape a channel's /streams page for the newest live video URL.
    `di` is a DownloadInfo-like object with .URL, .LiveURL, .MembersOnly attrs."""
    if not di.LiveURL:
        return None

    MAX_STREAM_CHECK = 5
    streams_url = di.URL.replace("/live", "/streams")
    streams_html = DownloadData(streams_url)
    if not streams_html:
        return None

    html_str = streams_html.decode("utf-8", errors="replace")
    initial_data_json = GetInitialDataJson(html_str)
    if not initial_data_json:
        return None

    initial_data = parse_initial_data(initial_data_json)
    if not initial_data:
        return None

    # Navigate to the streams tab contents
    try:
        tabs = initial_data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
    except (KeyError, TypeError):
        return None

    contents = []
    for tab in tabs:
        try:
            url = tab["tabRenderer"]["endpoint"]["commandMetadata"]["webCommandMetadata"]["url"]
            if url.endswith("/streams"):
                contents = tab["tabRenderer"]["content"]["richGridRenderer"]["contents"]
                break
        except (KeyError, TypeError):
            continue

    for i, content in enumerate(contents):
        if i >= MAX_STREAM_CHECK:
            break

        try:
            video_renderer = content["richItemRenderer"]["content"]["videoRenderer"]
        except (KeyError, TypeError):
            continue

        # Check for members-only badge
        if di.MembersOnly:
            badges = video_renderer.get("badges", [])
            is_members = False
            for badge in badges:
                if badge.get("metadataBadgeRenderer", {}).get("style") == "BADGE_STYLE_TYPE_MEMBERS_ONLY":
                    is_members = True
                    break
            if not is_members:
                continue

        # Check for LIVE overlay
        overlays = video_renderer.get("thumbnailOverlays", [])
        for overlay in overlays:
            if overlay.get("thumbnailOverlayTimeStatusRenderer", {}).get("style") == "LIVE":
                video_id = video_renderer.get("videoId", "")
                if video_id:
                    return f"https://www.youtube.com/watch?v={video_id}"

    return None


# ---------------------------------------------------------------------------
# Web API Player Response
# ---------------------------------------------------------------------------

def DownloadWebAPIPlayerResponse(di) -> Optional[dict]:
    """Download player response via YouTube's Web API (requires PO token)."""
    if not di.PoToken:
        LogDebug("Cannot retrieve web API player response without a PO Token set")
        return None

    auth = ""
    if di.CookiesURL:
        auth = GenerateSAPISIDHash(di.CookiesURL)

    ytcfg = di.Ytcfg if di.Ytcfg else GetDefaultYTCFG()

    query_params = ""
    if ytcfg.InnertubeApiKey:
        query_params = f"?innertube_key={ytcfg.InnertubeApiKey}"

    post_data = WEB_API_POST_DATA % (
        ytcfg.InnertubeClientName,
        ytcfg.InnertubeClientVersion,
        di.VideoID,
        di.PoToken,
    )

    url = f"https://www.youtube.com/youtubei/v1/player{query_params}"
    headers = {
        "X-YouTube-Client-Name": str(ytcfg.InnertubeCtxClientName),
        "X-YouTube-Client-Version": ytcfg.InnertubeCtxClientVersion,
        "Origin": "https://www.youtube.com",
        "Content-Type": "application/json",
    }

    if auth:
        headers["X-Origin"] = "https://www.youtube.com"
        headers["Authorization"] = auth

    if ytcfg.IdToken:
        headers["X-Youtube-Identity-Token"] = ytcfg.IdToken

    if ytcfg.DelegatedSessionId:
        headers["X-Goog-PageId"] = ytcfg.DelegatedSessionId

    visitor_data = di.VisitorData or ytcfg.VisitorData
    if visitor_data:
        headers["X-Goog-Visitor-Id"] = visitor_data

    if ytcfg.SessionIndex:
        headers["X-Goog-AuthUser"] = ytcfg.SessionIndex

    LogTrace("POST %s", url)
    try:
        resp = session.post(url, data=post_data, headers=headers, timeout=30)
        if resp.status_code != 200:
            LogDebug("Web API returned non-200 status code %d", resp.status_code)
            return None

        return resp.json()
    except Exception as e:
        LogDebug("Error getting Web API player response: %s", str(e))
        return None


# ---------------------------------------------------------------------------
# Video HTML Retrieval
# ---------------------------------------------------------------------------

def GetVideoHtml(di) -> bytes:
    """Get the HTML of the video watch page."""
    video_html = b""

    if di.LiveURL:
        stream_url = _get_newest_stream_from_streams(di)
        if stream_url:
            video_html = DownloadData(stream_url)

    if not video_html and not di.MembersOnly:
        video_html = DownloadData(di.URL)

    return video_html


# ---------------------------------------------------------------------------
# Player Response Retrieval
# ---------------------------------------------------------------------------

def GetPlayerResponse(di, video_html: bytes) -> Optional[dict]:
    """Extract and parse PlayerResponse from watch page HTML."""
    if not video_html:
        LogDebug("Unable to retrieve data from video page")
        return None

    html_str = video_html.decode("utf-8", errors="replace")
    pr_json = GetPlayerResponseJson(html_str)

    if not pr_json:
        LogDebug("Could not find player response from video watch page.")
        return None

    pr = parse_player_response(pr_json)
    if not pr:
        LogDebug("Failed to parse player response JSON")
        return None

    if di.LiveURL:
        video_id = GetVideoIdFromWatchPage(video_html)
        if video_id:
            di.VideoID = video_id

    return pr


# ---------------------------------------------------------------------------
# YTCFG Retrieval
# ---------------------------------------------------------------------------

def GetYTCFG(di, video_html: bytes):
    """Extract and parse YTCFG from watch page HTML, update on di."""
    if di.Ytcfg is None:
        di.Ytcfg = GetDefaultYTCFG()

    if not video_html:
        LogDebug("Unable to retrieve data from video page for ytcfg")
        return

    html_str = video_html.decode("utf-8", errors="replace")
    ytcfg_json = GetYTCFGFromHtml(html_str)
    if not ytcfg_json:
        LogDebug("Unable to retrieve ytcfg data from watch page")
        return

    try:
        data = json.loads(ytcfg_json)
        for key, value in data.items():
            if hasattr(di.Ytcfg, key):
                setattr(di.Ytcfg, key, value)
    except json.JSONDecodeError as e:
        LogDebug("Error parsing ytcfg JSON: %s", str(e))


# ---------------------------------------------------------------------------
# Get Playable Player Response (State Machine)
# ---------------------------------------------------------------------------

def GetPlayablePlayerResponse(di):
    """Get a playable PlayerResponse, handling waiting/retry logic.
    Returns (status, player_response_dict, selected_qualities_list)."""
    first_wait = True
    is_live_url = di.LiveURL
    wait_on_live_url = is_live_url and di.RetrySecs > 0 and not di.InProgress
    live_waited = 0
    retry_count = 0
    response_retry_count = 0
    video_details_retry_count = 0
    MAX_RETRIES = 3
    secs_late = 0

    selected_qualities = []
    if di.SelectedQuality:
        selected_qualities = ParseQualitySelection(VideoQualities, di.SelectedQuality)

    # Reference to utils for module-level globals like status_newlines
    import utils as u

    while True:
        video_html = GetVideoHtml(di)
        pr = GetPlayerResponse(di, video_html)

        if pr is None:
            if wait_on_live_url:
                if not selected_qualities:
                    print(file=sys.stderr)
                    selected_qualities = GetQualityFromUser(VideoQualities, True)

                if live_waited == 0:
                    LogGeneral("You have opted to wait for a livestream to be scheduled. Retrying every %d seconds.\n", di.RetrySecs)

                time.sleep(di.RetrySecs)
                live_waited += di.RetrySecs
                retry_count += 1
                if utils.loglevel > LOGLEVEL_QUIET:
                    msg = "Retries: %d (Last retry: %s), Total time waited: %d seconds"
                    if not getattr(u, 'status_newlines', False):
                        msg = "\r" + msg
                    else:
                        msg = msg + "\n"
                    sys.stderr.write(msg % (
                        retry_count,
                        time.strftime("%Y/%m/%d %H:%M:%S"),
                        live_waited,
                    ))
                    sys.stderr.flush()
                continue

            if response_retry_count < MAX_RETRIES:
                response_retry_count += 1
                LogWarn("Error retrieving player response. (Retry %d/%d)", response_retry_count, MAX_RETRIES)
                session.close()
                time.sleep(2)
                continue

            print(file=sys.stderr)
            LogError("Error retrieving player response, max retries reached.")
            return PLAYER_RESPONSE_NOT_FOUND, None, None

        # Check video details
        if not pr_video_id(pr):
            if video_details_retry_count < MAX_RETRIES:
                video_details_retry_count += 1
                LogWarn("Video Details not found, video may be private or does not exist. (Retry %d/%d)", video_details_retry_count, MAX_RETRIES)
                session.close()
                time.sleep(2)
                continue

            if di.InProgress:
                LogWarn("Video details no longer available mid download.")
                LogWarn("Stream was likely privated after finishing.")
                LogWarn("We will continue to download, but if it starts to fail, nothing can be done.")

            LogError("Video Details not found, max retries reached.")
            di.Live = False
            di.Unavailable = True
            return PLAYER_RESPONSE_NOT_USABLE, None, None

        # Check if this is a livestream at all
        live_streamability = pr.get("playabilityStatus", {}).get("liveStreamability", {}).get("liveStreamabilityRenderer", {})
        if not live_streamability.get("videoId") and not pr_is_live_content(pr):
            if di.Live:
                di.Live = False
            else:
                LogError("%s is not a livestream. It would be better to use yt-dlp to download it.", di.URL)
            return PLAYER_RESPONSE_NOT_USABLE, None, None

        status = pr_playability_status(pr)

        if status == PLAYABLE_ERROR:
            if di.InProgress:
                LogInfo("Finishing download")
            LogError("Playability status: ERROR. Reason: %s", pr_playability_reason(pr))
            di.Live = False
            return PLAYER_RESPONSE_NOT_USABLE, None, None

        elif status == PLAYABLE_UNPLAYABLE:
            logged_in = not pr_is_logged_out(pr)
            LogError("Playability status: UNPLAYABLE.")
            LogError("Reason: %s", pr_playability_reason(pr))
            LogError("Logged in status: %s", logged_in)
            LogError("If this is a members only stream, you provided a cookies.txt file, and the above 'logged in' status is not True, please try updating your cookies file.")
            di.Unavailable = True
            return PLAYER_RESPONSE_NOT_USABLE, None, None

        elif status == PLAYABLE_OFFLINE:
            if di.InProgress:
                LogDebug("Livestream status is %s mid-download", PLAYABLE_OFFLINE)
                return PLAYER_RESPONSE_NOT_USABLE, None, None

            if di.LiveFromVal and di.LiveFromVal.startswith("-"):
                LogError("Option --live-from with a negative duration is not valid for a scheduled stream.")
                return PLAYER_RESPONSE_NOT_USABLE, None, None

            if di.Wait == ACTION_DO_NOT:
                LogError("Stream has not started, and you have opted not to wait.")
                return PLAYER_RESPONSE_NOT_USABLE, None, None

            if first_wait and di.Wait == ACTION_ASK and di.RetrySecs == 0:
                if not di.AskWaitForStream():
                    return PLAYER_RESPONSE_NOT_USABLE, None, None

            if first_wait:
                if not (is_live_url and di.RetrySecs > 0):
                    di.PrintChannelAndTitle(pr)
                print(file=sys.stderr)
                if not selected_qualities:
                    selected_qualities = GetQualityFromUser(VideoQualities, True)

            if di.RetrySecs > 0:
                if first_wait:
                    first_wait = False
                    LogGeneral("Waiting for stream, retrying every %d seconds...\n", di.RetrySecs)
                time.sleep(di.RetrySecs)
                live_waited += di.RetrySecs
                retry_count += 1
                if utils.loglevel > LOGLEVEL_QUIET:
                    msg = "Retries: %d (Last retry: %s), Total time waited: %d seconds"
                    if not getattr(u, 'status_newlines', False):
                        msg = "\r" + msg
                    else:
                        msg = msg + "\n"
                    sys.stderr.write(msg % (
                        retry_count,
                        time.strftime("%Y/%m/%d %H:%M:%S"),
                        live_waited,
                    ))
                    sys.stderr.flush()
                continue

            sched_time = pr_scheduled_start_time(pr)
            if sched_time is None:
                LogWarn("Failed to get stream start time.")
                LogWarn("Falling back to polling.")
                di.RetrySecs = DEFAULT_POLL_TIME
                time.sleep(di.RetrySecs)
                continue

            cur_time = int(time.time())
            sleep_time = sched_time - cur_time

            if sleep_time > 0:
                if not first_wait:
                    LogGeneral("Stream rescheduled.")
                first_wait = False
                secs_late = 0
                LogGeneral("Stream starts at %s in %d seconds. ",
                    pr_start_timestamp(pr), sleep_time)
                LogGeneral("Waiting for this time to elapse...")

                while sleep_time > 0:
                    time.sleep(sleep_time)
                    cur_time = int(time.time())
                    sleep_time = sched_time - cur_time
                    if sleep_time > 0:
                        LogDebug("Woke up %d seconds early. Continuing sleep...", sleep_time)
                continue

            if first_wait:
                LogGeneral("Stream should have started. Checking back every %d seconds\n", DEFAULT_POLL_TIME)
                first_wait = False

            time.sleep(DEFAULT_POLL_TIME)
            secs_late += DEFAULT_POLL_TIME
            LogGeneral("Stream is %d seconds late...", secs_late)
            continue

        elif status == PLAYABLE_OK:
            # player response from /live does not include full information
            if is_live_url:
                di.URL = f"https://www.youtube.com/watch?v={di.VideoID}"
                di.MembersOnly = False
                is_live_url = False
                continue

            di.PrintChannelAndTitle(pr)
            stream_data = pr_streaming_data(pr)
            live_details = pr_live_broadcast_details(pr)
            is_live = pr_is_live_now(pr)

            if not is_live and not di.InProgress:
                if pr_end_timestamp(pr):
                    adaptive_formats = pr_adaptive_formats(pr)
                    if adaptive_formats:
                        if not adaptive_formats[0].get("url"):
                            LogGeneral("Livestream has ended and is being processed. Download URLs not available.")
                            return PLAYER_RESPONSE_NOT_USABLE, None, None
                        if not IsFragmented(adaptive_formats[0].get("url", "")):
                            LogGeneral("Livestream has been processed. Use yt-dlp instead.")
                            return PLAYER_RESPONSE_NOT_USABLE, None, None
                    else:
                        LogGeneral("Livestream has ended and is being processed. Download URLs not available.")
                        return PLAYER_RESPONSE_NOT_USABLE, None, None
                else:
                    LogGeneral("Livestream is offline, should have started, and does not have an end timestamp.")
                    LogGeneral("Waiting %d seconds and trying again.\n", DEFAULT_POLL_TIME)
                    time.sleep(DEFAULT_POLL_TIME)
                    continue

            GetYTCFG(di, video_html)
        else:
            if secs_late > 0:
                print(file=sys.stderr)
            LogError("Unknown playability status: %s", status)
            if di.InProgress:
                di.Live = False
            return PLAYER_RESPONSE_NOT_USABLE, None, None

        if secs_late > 0:
            print(file=sys.stderr)
        break

    return PLAYER_RESPONSE_FOUND, pr, selected_qualities
