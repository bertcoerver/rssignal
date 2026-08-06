"""Follow a YouTube channel, and attach the video behind each of its items.

Discovery needs no API key and no scraping: YouTube publishes an Atom feed per
channel at ``/feeds/videos.xml?channel_id=UC…`` carrying the last 15 uploads with
their real publication dates, titles, descriptions and thumbnails. So a channel
url is not parsed here at all — it is *rewritten* to that address and handed to
feedparser like any other feed, which is why filters, templates, extracts and
previews all work on a YouTube feed without knowing it is one.

A key would buy handle resolution, durations and backfill past 15 items. The
first two are had here for free (see :func:`youtube_feed_url` and
:func:`resolve`), and the third is not wanted: a new group should open with the
latest video, not a decade of them.

Downloading is yt-dlp's job. It is run as a command rather than imported, for
the same reason ffmpeg is: ``subprocess.run`` gives the wall-clock timeout its
python API has no knob for, ``-J`` is a stable documented interface where that
api is not across yt-dlp's weekly releases, and a missing binary then reads the
same way a missing ffmpeg does. It is run signed out unless the environment says
otherwise (see :data:`COOKIES_ENV`), which is enough until YouTube decides an
address looks like a robot.

Which quality: the same "sharpest that fits" rule as ARTE, over a different
catalogue. YouTube serves video and audio separately above 360p, so a plan names
a pair of formats and yt-dlp muxes them; the sizes are usually exact rather than
estimated, because ``-J`` reports them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from urllib.parse import urlparse

from . import cache, timing
from .feeds import FeedError, FeedItem, SourceBlocked
from .video import SIZE_ESTIMATE_MARGIN, VIDEO_MAX_BYTES, VideoTooShort

# A YouTube video, in any of the shapes a feed or a human might write it.
YOUTUBE_LINK_RE = re.compile(
    r"https?://(?:(?:www|m|music)\.)?(?:"
    r"youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/|v/)"
    r"|youtu\.be/"
    r")(?P<video_id>[\w-]{11})",
    re.IGNORECASE,
)

# A channel, in the four forms YouTube still hands out: the canonical id, the
# modern @handle, and the two legacy vanity paths.
YOUTUBE_CHANNEL_RE = re.compile(
    r"https?://(?:(?:www|m)\.)?youtube\.com/(?:"
    r"channel/(?P<channel_id>UC[\w-]+)"
    r"|@(?P<handle>[\w.-]+)"
    r"|c/(?P<vanity>[\w.-]+)"
    r"|user/(?P<user>[\w.-]+)"
    r")(?:/[\w-]*)?(?:[/?#]|$)",
    re.IGNORECASE,
)

YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v={video_id}"
YOUTUBE_UPLOADS = "https://www.youtube.com/channel/{channel_id}/videos"

# What the reachability probe asks for. The host is the whole question, so it
# asks for as little of it as there is.
YOUTUBE_PROBE = "https://www.youtube.com/"

# Hosts a YouTube feed or video is read from, and so the ones a network-level
# block takes out. Matched as whole labels, not substrings, so that a lookalike
# domain can't pass for one of these.
YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)

# The channel id, read back out of the feed url rssignal rewrote a channel to.
FEED_CHANNEL_RE = re.compile(r"[?&]channel_id=(?P<channel_id>UC[\w-]+)")

# How many uploads the Atom feed carries, and so how far down the channel's own
# listing the durations for them can be.
FEED_VIDEO_COUNT = 15

# Below this, an upload is a Short or a teaser rather than something worth its
# own message. A compromise, and worth knowing it is one: Shorts now run to
# three minutes, but a three-minute floor would throw away real short videos.
MIN_VIDEO_SECONDS = 90

# Reading a channel's id or a video's formats is one small request on yt-dlp's
# side; it should not be able to hang a run.
YTDLP_QUERY_TIMEOUT = 60

# YouTube increasingly refuses anonymous requests from datacentre and repeat-
# visitor addresses, asking them to "confirm you're not a bot". The only answer
# it accepts is a signed-in session, so these hand yt-dlp one: a cookies.txt
# export, or a browser profile to read the cookies out of. Neither is set by
# default — most channels never ask — and every yt-dlp call gets them, because
# the listing that fills in durations is challenged the same way a download is.
COOKIES_ENV = "RSSIGNAL_YOUTUBE_COOKIES"
COOKIES_FROM_BROWSER_ENV = "RSSIGNAL_YOUTUBE_COOKIES_FROM_BROWSER"

# The distinctive part of that refusal, matched loosely because the wording
# around it moves and the apostrophe is a typographic one.
_BOT_CHECK = "not a bot"

# Video codecs to leave alone. VP9 and AV1 rungs duplicate resolutions that also
# exist in H.264 and play back unevenly across Signal's clients; Opus audio has
# no place in an mp4. An avc1/m4a pair is always sitting next to them.
_VIDEO_CODEC = "avc1"
_AUDIO_CODEC = "mp4a"

# Cache namespaces: what channel an address names, and how long that channel's
# latest uploads are. See :mod:`rssignal.cache`.
_CHANNEL_NS = "youtube_channel_id"
_DURATIONS_NS = "youtube_durations"


@dataclass(frozen=True)
class YoutubePlan:
    """The formats a YouTube item would be fetched at.

    ``selector`` is yt-dlp's ``-f`` argument: either a single progressive format
    id, or ``"<video>+<audio>"`` for the separate streams YouTube serves above
    360p, which yt-dlp muxes into one mp4 without re-encoding.
    """

    url: str
    selector: str
    width: int
    height: int
    estimated_bytes: int

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def describe(self) -> str:
        mb = self.estimated_bytes / (1024 * 1024)
        return f"{self.resolution}, ~{mb:.0f} MB"

    def fetch(self, into: str, *, timeout: float) -> str:
        """Download the chosen formats into ``into``, returning the file's path."""
        return _run_download(self.url, self.selector, into, timeout=timeout)


def is_youtube_url(url: str | None) -> bool:
    """Whether ``url`` is one that would be read from YouTube."""
    if not url:
        return False
    return (urlparse(url).hostname or "").lower() in YOUTUBE_HOSTS


def check_available(url: str) -> None:
    """Raise :class:`SourceBlocked` if ``url`` is YouTube's and YouTube is not up.

    Reading a YouTube feed means several requests through yt-dlp, each with its
    own minute-long timeout. When the host is not reachable at all, those spend
    minutes arriving at what one HEAD request answers in milliseconds, and leave
    a page of traceback for a condition that isn't a fault.

    The case this is really for is a scheduled DNS-level block — a NextDNS
    profile that closes YouTube during the day and opens it at night. A run that
    lands inside the closed window should say so in one line and move on to the
    other feeds; the next run inside the open window reads the feed from the same
    watermark, having lost nothing.

    Anything that isn't a YouTube url returns immediately, so a config with no
    YouTube in it never pays for this.
    """
    if not is_youtube_url(url):
        return

    from .download import reachable

    if not reachable(YOUTUBE_PROBE):
        raise SourceBlocked(
            f"{YOUTUBE_PROBE} is not reachable from here — a DNS or firewall "
            "block, not a broken feed. Skipping until it lifts."
        )


def youtube_video_id(link: str | None) -> str | None:
    """Return the eleven-character video id in ``link``, else ``None``."""
    if not link:
        return None
    match = YOUTUBE_LINK_RE.search(link)
    return match["video_id"] if match else None


def youtube_feed_url(url: str | None) -> str | None:
    """The Atom feed for a YouTube channel page, or ``None`` if not one.

    A ``/channel/UC…`` url is rewritten with no network at all — the id is
    already in it. The other three forms name the channel without identifying
    it, so yt-dlp is asked for the id, listing exactly one video because it is
    the channel's own fields that are wanted, not its contents.
    """
    if not url:
        return None
    # The feed itself is a youtube.com url and must be left alone, or reading a
    # feed would mean resolving it all over again.
    if "/feeds/videos.xml" in url:
        return None

    match = YOUTUBE_CHANNEL_RE.search(url)
    if not match:
        return None

    channel_id = match["channel_id"] or _channel_id(url)
    return YOUTUBE_FEED.format(channel_id=channel_id)


def with_durations(feed_url: str, items: list[FeedItem]) -> list[FeedItem]:
    """Return ``items`` with ``duration_seconds`` filled in, if they are YouTube's.

    The Atom feed says how a video is titled, described and thumbnailed, and
    nothing at all about how long it is — which is the one thing a channel that
    posts both clips and full episodes has to be filtered on. yt-dlp's flat
    listing of the channel does say, for every upload at once, so this costs one
    small request per feed rather than one per video.

    Items keep the id they already carry: the listing is matched to the feed by
    video id, not by position, so the two disagreeing about order or about which
    fifteen uploads are the latest costs a duration rather than mismatching one.

    ``feed_url`` that isn't a YouTube channel feed leaves ``items`` alone. So
    does a lookup that fails: a duration is worth a request, not worth losing
    the feed over — though a filter that needs one will then drop the items,
    which is what the warning is for.
    """
    match = FEED_CHANNEL_RE.search(feed_url or "")
    if not match or not items:
        return items

    try:
        durations = _channel_durations(match["channel_id"])
    except FeedError as exc:
        print(
            f"Could not read how long the videos in {feed_url} are: {exc}\n"
            "They have no duration_seconds this run.",
            file=sys.stderr,
        )
        return items

    annotated = []
    for item in items:
        seconds = durations.get(youtube_video_id(item.link) or "")
        if seconds is None:
            annotated.append(item)
            continue
        annotated.append(
            replace(item, extra={**item.extra, "duration_seconds": str(int(seconds))})
        )
    return annotated


def resolve(item: FeedItem, *, max_bytes: int = VIDEO_MAX_BYTES) -> YoutubePlan:
    """Work out which formats ``item``'s video would be fetched at.

    Raises :class:`VideoTooShort` for a Short, and
    :class:`~rssignal.feeds.FeedError` if the item is not a YouTube video, the
    video can't be read, or nothing fits ``max_bytes``.
    """
    video_id = youtube_video_id(item.link)
    if not video_id:
        raise FeedError(f"Not a YouTube link: {item.link!r}")

    url = YOUTUBE_WATCH.format(video_id=video_id)
    info = _video_info(url)

    duration = info.get("duration")
    if duration and float(duration) < MIN_VIDEO_SECONDS:
        raise VideoTooShort(
            f"{video_id} is {float(duration):.0f}s — a Short, not sending it"
        )

    formats = info.get("formats") or []
    if not formats:
        raise FeedError(f"YouTube offered no formats for {video_id}")

    return _pick(formats, float(duration or 0), max_bytes, url, video_id)


def _channel_id(url: str) -> str:
    """Ask yt-dlp what channel ``url`` names, or recall what it said last time.

    Worth remembering because the answer is fixed — a handle names the channel it
    was created for — while asking costs a yt-dlp launch and a signed-out request
    to YouTube, on every run, for a feed whose address never changes.
    """
    hit = cache.get(_CHANNEL_NS, url, max_age=cache.YOUTUBE_CHANNEL_TTL)
    if isinstance(hit, str) and hit.startswith("UC"):
        return hit

    info = _ytdlp_json(
        ["--flat-playlist", "--playlist-items", "1", url],
        timeout=YTDLP_QUERY_TIMEOUT,
    )
    channel_id = info.get("channel_id") or info.get("uploader_id")
    if not channel_id or not str(channel_id).startswith("UC"):
        raise FeedError(f"Could not work out the channel id for {url!r}")
    cache.put(_CHANNEL_NS, url, str(channel_id))
    return str(channel_id)


def _channel_durations(channel_id: str) -> dict[str, float]:
    """Ask yt-dlp how long each of a channel's latest uploads is.

    ``--flat-playlist`` is what makes this one request: yt-dlp lists the channel
    without opening any of the videos, and still reports each one's duration.

    Held only briefly between runs — unlike the other things rssignal remembers,
    this one really does go stale, because which fifteen uploads are the latest
    changes every time the channel posts.
    """
    hit = cache.get(_DURATIONS_NS, channel_id, max_age=cache.YOUTUBE_DURATIONS_TTL)
    if isinstance(hit, dict):
        return {str(k): float(v) for k, v in hit.items()}

    info = _ytdlp_json(
        [
            "--flat-playlist",
            "--playlist-items",
            f"1:{FEED_VIDEO_COUNT}",
            YOUTUBE_UPLOADS.format(channel_id=channel_id),
        ],
        timeout=YTDLP_QUERY_TIMEOUT,
    )

    durations: dict[str, float] = {}
    for entry in _videos(info):
        video_id = entry.get("id")
        duration = entry.get("duration")
        if video_id and duration:
            durations[str(video_id)] = float(duration)
    cache.put(_DURATIONS_NS, channel_id, durations)
    return durations


def _videos(playlist: dict) -> Iterator[dict]:
    """Walk a yt-dlp listing down to the videos in it.

    A channel url can list its *tabs* rather than its uploads, one level of
    playlist deeper, so the entries are followed until they stop nesting.
    """
    for entry in playlist.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("entries"):
            yield from _videos(entry)
        else:
            yield entry


def _video_info(url: str) -> dict:
    return _ytdlp_json(["--no-playlist", url], timeout=YTDLP_QUERY_TIMEOUT)


def _pick(
    formats: list[dict], duration: float, max_bytes: int, url: str, video_id: str
) -> YoutubePlan:
    """Choose the sharpest video (plus audio) expected to fit ``max_bytes``."""
    audio = _smallest_audio(formats, duration)

    fitting: list[tuple[tuple[int, int], YoutubePlan]] = []
    smallest: int | None = None

    for fmt in formats:
        pair = _pairing(fmt, audio, duration)
        if pair is None:
            continue
        selector, size = pair
        smallest = size if smallest is None else min(smallest, size)
        if size > max_bytes:
            continue
        height = int(fmt.get("height") or 0)
        width = int(fmt.get("width") or 0)
        plan = YoutubePlan(
            url=url,
            selector=selector,
            width=width,
            height=height,
            estimated_bytes=size,
        )
        fitting.append(((height, int(fmt.get("tbr") or 0)), plan))

    if not fitting:
        if smallest is None:
            raise FeedError(f"No usable format for {video_id}")
        raise FeedError(
            f"Video {video_id} is too big to send: the smallest usable format "
            f"is about {smallest / 1024 / 1024:.0f} MB, over the "
            f"{max_bytes / 1024 / 1024:.0f} MB limit"
        )
    return max(fitting, key=lambda pair: pair[0])[1]


def _pairing(
    fmt: dict, audio: dict | None, duration: float
) -> tuple[str, int] | None:
    """``(selector, bytes)`` for ``fmt``, or ``None`` if it isn't usable.

    Two shapes qualify: an H.264 video-only stream, which needs the audio
    stream muxed alongside it, and a progressive format that already has both —
    the 360p one YouTube still serves, and the only thing left when a video has
    no separate H.264 rungs at all.
    """
    if not str(fmt.get("vcodec") or "none").startswith(_VIDEO_CODEC):
        return None
    if not fmt.get("height"):
        return None

    size = _size(fmt, duration)
    if size is None:
        return None
    format_id = str(fmt.get("format_id") or "")
    if not format_id:
        return None

    acodec = str(fmt.get("acodec") or "none")
    if acodec == "none":
        if audio is None:
            return None
        audio_size = _size(audio, duration)
        if audio_size is None:
            return None
        return f"{format_id}+{audio['format_id']}", size + audio_size

    if not acodec.startswith(_AUDIO_CODEC):
        return None
    return format_id, size


def _smallest_audio(formats: list[dict], duration: float) -> dict | None:
    """The leanest AAC audio-only stream, which is the one worth muxing.

    Audio is a rounding error next to video here — roughly 2 MB for ten minutes
    against 60 — so the cheapest one buys the most room for picture.
    """
    candidates = [
        fmt
        for fmt in formats
        if str(fmt.get("vcodec") or "none") == "none"
        and str(fmt.get("acodec") or "none").startswith(_AUDIO_CODEC)
        and fmt.get("format_id")
        and _size(fmt, duration) is not None
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda fmt: _size(fmt, duration) or 0)


def _size(fmt: dict, duration: float) -> int | None:
    """How big ``fmt`` is, best available answer, or ``None`` if there isn't one.

    ``filesize`` is exact and taken as it stands. The other two are estimates
    and carry :data:`~rssignal.video.SIZE_ESTIMATE_MARGIN` accordingly.
    """
    exact = fmt.get("filesize")
    if exact:
        return int(exact)

    approx = fmt.get("filesize_approx")
    if approx:
        return int(int(approx) * SIZE_ESTIMATE_MARGIN)

    tbr = fmt.get("tbr")
    if tbr and duration:
        return int(float(tbr) * 1000 / 8 * duration * SIZE_ESTIMATE_MARGIN)
    return None


def _ytdlp_json(args: list[str], *, timeout: float) -> dict:
    """Run ``yt-dlp -J`` with ``args`` and return what it printed."""
    result = _run_ytdlp(["-J", *args], timeout=timeout)
    try:
        info = json.loads(result.stdout)
    except ValueError as exc:
        raise FeedError(f"Could not read yt-dlp's output: {exc}") from exc
    if not isinstance(info, dict):
        raise FeedError("yt-dlp reported something that isn't a video")
    return info


def _run_download(url: str, selector: str, into: str, *, timeout: float) -> str:
    """Download ``selector`` of ``url`` into ``into``, returning the file's path.

    ``--merge-output-format mp4`` is what turns a separate video and audio
    stream into one file; yt-dlp calls ffmpeg to do it and copies the streams
    rather than re-encoding, so this costs bandwidth and seconds rather than
    minutes of cpu. The extension is left to yt-dlp and the file found
    afterwards, because it has the last word on what it wrote.
    """
    _run_ytdlp(
        [
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--quiet",
            "--format",
            selector,
            "--merge-output-format",
            "mp4",
            "--output",
            os.path.join(into, "video.%(ext)s"),
            url,
        ],
        timeout=timeout,
    )

    files = [
        os.path.join(into, name)
        for name in sorted(os.listdir(into))
        if os.path.isfile(os.path.join(into, name))
    ]
    if not files:
        raise FeedError("yt-dlp produced no video")
    # More than one means the mux didn't happen and the parts were left behind;
    # the biggest is the video, and sending it beats sending nothing.
    return max(files, key=os.path.getsize)


def _run_ytdlp(args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    binary = shutil.which("yt-dlp")
    if binary is None:
        raise FeedError(
            "yt-dlp is needed to fetch YouTube videos but was not found on PATH "
            "(`pip install rssignal[video]`)"
        )

    try:
        with timing.step("yt-dlp", args[0] if args else ""):
            result = subprocess.run(
                [binary, *_cookie_args(), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise FeedError(f"yt-dlp timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise FeedError(f"Could not run yt-dlp: {exc}") from exc

    if result.returncode != 0:
        raise FeedError(f"yt-dlp failed: {_complaint(result.stderr)}")
    return result


def _cookie_args() -> list[str]:
    """How yt-dlp should sign in, from the environment, or nothing if it shouldn't.

    A cookies file wins over a browser profile when both are set: it is the
    explicit one, and the one that works where there is no browser to read.
    """
    path = os.environ.get(COOKIES_ENV, "").strip()
    if path:
        expanded = os.path.expanduser(path)
        if not os.path.isfile(expanded):
            raise FeedError(
                f"{COOKIES_ENV} is set to {path!r}, which is not a file. Point it "
                "at a cookies.txt export, or unset it."
            )
        return ["--cookies", expanded]

    browser = os.environ.get(COOKIES_FROM_BROWSER_ENV, "").strip()
    if browser:
        return ["--cookies-from-browser", browser]
    return []


def _complaint(stderr: str) -> str:
    """What went wrong, and for the bot check, what to do about it.

    That one is worth expanding because the fix is configuration rather than
    anything about the video, and yt-dlp's own advice is for someone running
    yt-dlp by hand.
    """
    last = _last_line(stderr)
    if _BOT_CHECK not in last.lower():
        return last
    return (
        f"{last}\nYouTube wants a signed-in session for this one. Set "
        f"{COOKIES_FROM_BROWSER_ENV} to a browser you are logged into "
        f"(e.g. firefox, chrome, safari, or 'firefox:<profile>'), or export a "
        f"cookies.txt from that browser and set {COOKIES_ENV} to its path."
    )


def _last_line(stderr: str) -> str:
    """The final line of yt-dlp's complaint — the one that says what went wrong."""
    lines = [line for line in (stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no error output"
