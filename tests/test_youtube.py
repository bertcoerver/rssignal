"""Tests for rssignal.youtube (yt-dlp is monkeypatched — it is never run).

The format list is a trimmed copy of the shape ``yt-dlp -J`` really reports:
separate video and audio above 360p, one progressive rung left over, and VP9
duplicates of the resolutions that also exist in H.264. Sizes are rounded but
the relationships between them are the ones that decide the outcome.
"""

import json
import os
import subprocess

import pytest

from rssignal import youtube
from rssignal.feeds import FeedError, FeedItem
from rssignal.video import VideoTooShort

VIDEO_ID = "BFcjfKZ0BeI"
WATCH = f"https://www.youtube.com/watch?v={VIDEO_ID}"
DURATION = 600

AUDIO = {
    "format_id": "140",
    "ext": "m4a",
    "vcodec": "none",
    "acodec": "mp4a.40.2",
    "filesize": 9_700_000,
}
FORMATS = [
    {**AUDIO},
    # Opus: fine for the web, wrong for an mp4 going to Signal.
    {
        "format_id": "251",
        "ext": "webm",
        "vcodec": "none",
        "acodec": "opus",
        "filesize": 6_000_000,
    },
    {
        "format_id": "18",
        "ext": "mp4",
        "vcodec": "avc1.42001E",
        "acodec": "mp4a.40.2",
        "width": 640,
        "height": 360,
        "filesize": 30_000_000,
    },
    {
        "format_id": "134",
        "ext": "mp4",
        "vcodec": "avc1.4d401e",
        "acodec": "none",
        "width": 640,
        "height": 360,
        "filesize": 20_000_000,
    },
    {
        "format_id": "135",
        "ext": "mp4",
        "vcodec": "avc1.4d401f",
        "acodec": "none",
        "width": 854,
        "height": 480,
        "filesize": 40_000_000,
    },
    {
        "format_id": "136",
        "ext": "mp4",
        "vcodec": "avc1.4d401f",
        "acodec": "none",
        "width": 1280,
        "height": 720,
        "filesize": 85_000_000,
    },
    {
        "format_id": "137",
        "ext": "mp4",
        "vcodec": "avc1.640028",
        "acodec": "none",
        "width": 1920,
        "height": 1080,
        "filesize": 200_000_000,
    },
    # Small enough to win on size alone — and must still lose, because it is VP9.
    {
        "format_id": "248",
        "ext": "webm",
        "vcodec": "vp9",
        "acodec": "none",
        "width": 1920,
        "height": 1080,
        "filesize": 30_000_000,
    },
]

INFO = {"id": VIDEO_ID, "title": "A video", "duration": DURATION, "formats": FORMATS}


@pytest.fixture(autouse=True)
def _signed_out(monkeypatch):
    """Whoever runs the tests may have cookies configured; the tests do not."""
    monkeypatch.delenv(youtube.COOKIES_ENV, raising=False)
    monkeypatch.delenv(youtube.COOKIES_FROM_BROWSER_ENV, raising=False)


def _patch_ytdlp(monkeypatch, *, info=None, returncode=0, stderr="", payload=b"video"):
    """Stand in for the yt-dlp binary, recording every command it was given."""
    calls = []
    info = INFO if info is None else info

    monkeypatch.setattr(youtube.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "-J" in cmd:
            return subprocess.CompletedProcess(cmd, returncode, json.dumps(info), stderr)
        if payload is not None and "--output" in cmd:
            template = cmd[cmd.index("--output") + 1]
            with open(template.replace("%(ext)s", "mp4"), "wb") as fh:
                fh.write(payload)
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(youtube.subprocess, "run", fake_run)
    return calls


def _item(link=WATCH):
    return FeedItem(title="A video", description="d", link=link)


# --- link and channel detection -------------------------------------------


@pytest.mark.parametrize(
    "link, expected",
    [
        (WATCH, VIDEO_ID),
        (f"https://youtu.be/{VIDEO_ID}", VIDEO_ID),
        (f"https://www.youtube.com/shorts/{VIDEO_ID}", VIDEO_ID),
        (f"https://m.youtube.com/watch?v={VIDEO_ID}", VIDEO_ID),
        (f"https://www.youtube.com/watch?list=PL1&v={VIDEO_ID}", VIDEO_ID),
        (f"https://www.youtube.com/watch?v={VIDEO_ID}&t=42s", VIDEO_ID),
        ("https://www.youtube.com/@veritasium", None),
        ("https://vimeo.com/123456789", None),
        (None, None),
    ],
)
def test_youtube_video_id(link, expected):
    assert youtube.youtube_video_id(link) == expected


def test_youtube_feed_url_rewrites_a_channel_id_without_asking_anyone(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the id is already in the url; nothing to look up")

    monkeypatch.setattr(youtube.subprocess, "run", fail)

    assert youtube.youtube_feed_url(
        "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA/videos"
    ) == (
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCHnyfMqiRRG1u-2MsSQLbXA"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@veritasium",
        "https://www.youtube.com/@veritasium/videos",
        "https://www.youtube.com/c/veritasium",
        "https://www.youtube.com/user/1veritasium",
    ],
)
def test_youtube_feed_url_asks_ytdlp_for_a_named_channel(monkeypatch, url):
    calls = _patch_ytdlp(monkeypatch, info={"channel_id": "UCHnyfMqiRRG1u-2MsSQLbXA"})

    assert youtube.youtube_feed_url(url).endswith("UCHnyfMqiRRG1u-2MsSQLbXA")

    cmd = calls[0]
    assert "-J" in cmd and "--flat-playlist" in cmd
    # One item, because it is the channel's own fields that are wanted.
    assert cmd[cmd.index("--playlist-items") + 1] == "1"


def test_youtube_feed_url_leaves_the_feed_itself_alone():
    feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCHnyfMqiRRG1u-2MsSQLbXA"
    assert youtube.youtube_feed_url(feed) is None


@pytest.mark.parametrize(
    "url",
    [WATCH, "https://waitbutwhy.com/feed", "https://www.youtube.com/", None],
)
def test_youtube_feed_url_ignores_what_is_not_a_channel(url):
    assert youtube.youtube_feed_url(url) is None


def test_youtube_feed_url_without_an_id_raises(monkeypatch):
    _patch_ytdlp(monkeypatch, info={"title": "some channel"})

    with pytest.raises(FeedError, match="Could not work out the channel id"):
        youtube.youtube_feed_url("https://www.youtube.com/@veritasium")


# --- durations ------------------------------------------------------------

CHANNEL_FEED = (
    "https://www.youtube.com/feeds/videos.xml?channel_id=UCHnyfMqiRRG1u-2MsSQLbXA"
)
LISTING = {
    "entries": [
        {"id": VIDEO_ID, "duration": 1200.0},
        {"id": "aaaaaaaaaaa", "duration": 61.0},
    ]
}


def test_with_durations_puts_the_channel_listing_on_the_items(monkeypatch):
    calls = _patch_ytdlp(monkeypatch, info=LISTING)
    items = [_item(), _item(link="https://youtu.be/aaaaaaaaaaa")]

    annotated = youtube.with_durations(CHANNEL_FEED, items)

    assert [item.extra["duration_seconds"] for item in annotated] == ["1200", "61"]
    # One request for the whole feed, not one per video.
    cmd = calls[0]
    assert len(calls) == 1 and "--flat-playlist" in cmd
    assert cmd[-1] == (
        "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA/videos"
    )


def test_with_durations_matches_on_video_id_not_position(monkeypatch):
    _patch_ytdlp(monkeypatch, info=LISTING)
    items = [_item(link="https://youtu.be/aaaaaaaaaaa"), _item()]

    annotated = youtube.with_durations(CHANNEL_FEED, items)

    assert [item.extra["duration_seconds"] for item in annotated] == ["61", "1200"]


def test_with_durations_follows_a_nested_listing(monkeypatch):
    _patch_ytdlp(monkeypatch, info={"entries": [{"entries": LISTING["entries"]}]})

    annotated = youtube.with_durations(CHANNEL_FEED, [_item()])

    assert annotated[0].extra["duration_seconds"] == "1200"


def test_with_durations_leaves_an_unlisted_video_alone(monkeypatch):
    _patch_ytdlp(monkeypatch, info={"entries": []})
    item = _item()

    assert youtube.with_durations(CHANNEL_FEED, [item]) == [item]


def test_with_durations_ignores_a_feed_that_is_not_a_channel(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("nothing to look up for a feed with no channel in it")

    monkeypatch.setattr(youtube.subprocess, "run", fail)
    items = [_item()]

    assert youtube.with_durations("https://waitbutwhy.com/feed", items) == items


def test_with_durations_survives_a_failed_lookup(monkeypatch, capsys):
    _patch_ytdlp(monkeypatch, info={}, returncode=1, stderr="yt-dlp: unavailable")
    items = [_item()]

    assert youtube.with_durations(CHANNEL_FEED, items) == items
    assert "duration_seconds" in capsys.readouterr().err


# --- quality choice -------------------------------------------------------


def test_resolve_takes_the_sharpest_pair_that_fits(monkeypatch):
    calls = _patch_ytdlp(monkeypatch)

    plan = youtube.resolve(_item())

    assert calls[0][-1] == WATCH
    # 720p (85 MB) plus the audio (9.7 MB) is 94.7 MB and fits; 1080p doesn't.
    assert plan.selector == "136+140"
    assert plan.resolution == "1280x720"
    assert plan.describe() == "1280x720, ~90 MB"


def test_resolve_never_picks_vp9(monkeypatch):
    """The VP9 1080p rung is the smallest of the lot and must still lose."""
    _patch_ytdlp(monkeypatch)

    plan = youtube.resolve(_item(), max_bytes=200 * 1024 * 1024)

    assert plan.resolution == "1920x1080"
    assert plan.selector == "137+140"


def test_resolve_drops_a_rung_for_a_smaller_budget(monkeypatch):
    _patch_ytdlp(monkeypatch)

    plan = youtube.resolve(_item(), max_bytes=60 * 1024 * 1024)

    assert plan.selector == "135+140"


def test_resolve_falls_back_to_a_progressive_format(monkeypatch):
    """Nothing to mux: no separate H.264 video, no AAC audio, just itag 18."""
    formats = [f for f in FORMATS if f["format_id"] in ("18", "251", "248")]
    _patch_ytdlp(monkeypatch, info={**INFO, "formats": formats})

    plan = youtube.resolve(_item())

    assert plan.selector == "18"
    assert plan.resolution == "640x360"


def test_resolve_estimates_from_bitrate_when_no_size_is_given(monkeypatch):
    formats = [
        {**AUDIO, "filesize": None, "tbr": 128},
        {
            "format_id": "134",
            "vcodec": "avc1.4d401e",
            "acodec": "none",
            "width": 640,
            "height": 360,
            "tbr": 600,
        },
    ]
    _patch_ytdlp(monkeypatch, info={**INFO, "formats": formats})

    plan = youtube.resolve(_item())

    # (600 + 128) kbps over 600s, with the 10% margin, is about 57 MB.
    assert plan.selector == "134+140"
    assert 55 < plan.estimated_bytes / 1024 / 1024 < 60


def test_resolve_raises_when_nothing_fits(monkeypatch):
    _patch_ytdlp(monkeypatch)

    with pytest.raises(FeedError, match="too big to send"):
        youtube.resolve(_item(), max_bytes=1024)


def test_resolve_raises_when_no_format_is_usable(monkeypatch):
    formats = [f for f in FORMATS if f["format_id"] in ("251", "248")]
    _patch_ytdlp(monkeypatch, info={**INFO, "formats": formats})

    with pytest.raises(FeedError, match="No usable format"):
        youtube.resolve(_item())


def test_resolve_without_formats_raises(monkeypatch):
    _patch_ytdlp(monkeypatch, info={"id": VIDEO_ID, "duration": 600, "formats": []})

    with pytest.raises(FeedError, match="no formats"):
        youtube.resolve(_item())


def test_resolve_rejects_a_non_youtube_link():
    with pytest.raises(FeedError, match="Not a YouTube link"):
        youtube.resolve(_item("https://example.com/post"))


# --- Shorts ---------------------------------------------------------------


def test_resolve_refuses_a_short(monkeypatch):
    _patch_ytdlp(monkeypatch, info={**INFO, "duration": 47})

    with pytest.raises(VideoTooShort, match="47s"):
        youtube.resolve(_item())


def test_resolve_keeps_a_video_just_over_the_floor(monkeypatch):
    _patch_ytdlp(monkeypatch, info={**INFO, "duration": youtube.MIN_VIDEO_SECONDS})

    assert youtube.resolve(_item()).selector == "136+140"


# --- downloading ----------------------------------------------------------


def test_fetch_runs_ytdlp_and_returns_the_file(monkeypatch, tmp_path):
    calls = _patch_ytdlp(monkeypatch)
    plan = youtube.resolve(_item())

    path = plan.fetch(str(tmp_path), timeout=900)

    assert os.path.basename(path) == "video.mp4"
    with open(path, "rb") as fh:
        assert fh.read() == b"video"

    cmd = calls[-1]
    assert cmd[0] == "/usr/bin/yt-dlp"
    assert cmd[cmd.index("--format") + 1] == "136+140"
    assert cmd[cmd.index("--merge-output-format") + 1] == "mp4"
    assert cmd[cmd.index("--output") + 1] == os.path.join(str(tmp_path), "video.%(ext)s")
    assert cmd[-1] == WATCH


def test_fetch_takes_the_biggest_file_when_the_mux_left_parts_behind(
    monkeypatch, tmp_path
):
    _patch_ytdlp(monkeypatch, payload=None)
    plan = youtube.resolve(_item())

    (tmp_path / "video.f136.mp4").write_bytes(b"x" * 50)
    (tmp_path / "video.mp4").write_bytes(b"x" * 500)
    (tmp_path / "video.f140.m4a").write_bytes(b"x" * 10)

    assert plan.fetch(str(tmp_path), timeout=900).endswith("video.mp4")


def test_fetch_without_output_raises(monkeypatch, tmp_path):
    _patch_ytdlp(monkeypatch, payload=None)
    plan = youtube.resolve(_item())

    with pytest.raises(FeedError, match="produced no video"):
        plan.fetch(str(tmp_path), timeout=900)


def test_ytdlp_failure_reports_its_last_line(monkeypatch, tmp_path):
    _patch_ytdlp(monkeypatch)
    plan = youtube.resolve(_item())
    _patch_ytdlp(
        monkeypatch,
        returncode=1,
        stderr="WARNING: falling back\nERROR: Sign in to confirm you're not a bot\n",
    )

    with pytest.raises(FeedError, match="not a bot"):
        plan.fetch(str(tmp_path), timeout=900)


def test_ytdlp_failure_on_the_bot_check_says_how_to_sign_in(monkeypatch, tmp_path):
    _patch_ytdlp(monkeypatch)
    plan = youtube.resolve(_item())
    _patch_ytdlp(
        monkeypatch,
        returncode=1,
        stderr="ERROR: [youtube] BFcjfKZ0BeI: Sign in to confirm you’re not a bot.\n",
    )

    with pytest.raises(FeedError) as excinfo:
        plan.fetch(str(tmp_path), timeout=900)

    assert youtube.COOKIES_FROM_BROWSER_ENV in str(excinfo.value)
    assert youtube.COOKIES_ENV in str(excinfo.value)


def test_a_cookies_file_is_passed_to_every_ytdlp_call(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv(youtube.COOKIES_ENV, str(cookies))
    calls = _patch_ytdlp(monkeypatch)

    plan = youtube.resolve(_item())
    plan.fetch(str(tmp_path), timeout=900)

    assert len(calls) == 2
    for cmd in calls:
        assert cmd[cmd.index("--cookies") + 1] == str(cookies)


def test_a_browser_is_passed_when_there_is_no_cookies_file(monkeypatch):
    monkeypatch.setenv(youtube.COOKIES_FROM_BROWSER_ENV, "firefox")
    calls = _patch_ytdlp(monkeypatch)

    youtube.resolve(_item())

    assert calls[0][calls[0].index("--cookies-from-browser") + 1] == "firefox"


def test_a_cookies_file_wins_over_a_browser(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv(youtube.COOKIES_ENV, str(cookies))
    monkeypatch.setenv(youtube.COOKIES_FROM_BROWSER_ENV, "firefox")
    calls = _patch_ytdlp(monkeypatch)

    youtube.resolve(_item())

    assert "--cookies" in calls[0]
    assert "--cookies-from-browser" not in calls[0]


def test_a_cookies_file_that_is_not_there_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(youtube.COOKIES_ENV, str(tmp_path / "nope.txt"))
    _patch_ytdlp(monkeypatch)

    with pytest.raises(FeedError, match="not a file"):
        youtube.resolve(_item())


def test_no_cookies_configured_signs_nothing_in(monkeypatch):
    calls = _patch_ytdlp(monkeypatch)

    youtube.resolve(_item())

    assert "--cookies" not in calls[0]
    assert "--cookies-from-browser" not in calls[0]


def test_ytdlp_timeout_raises(monkeypatch, tmp_path):
    _patch_ytdlp(monkeypatch)
    plan = youtube.resolve(_item())

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 900)

    monkeypatch.setattr(youtube.subprocess, "run", fake_run)

    with pytest.raises(FeedError, match="timed out"):
        plan.fetch(str(tmp_path), timeout=900)


def test_without_ytdlp_raises(monkeypatch):
    monkeypatch.setattr(youtube.shutil, "which", lambda name: None)

    with pytest.raises(FeedError, match="yt-dlp is needed"):
        youtube.resolve(_item())


def test_unreadable_ytdlp_output_raises(monkeypatch):
    monkeypatch.setattr(youtube.shutil, "which", lambda name: "/usr/bin/yt-dlp")
    monkeypatch.setattr(
        youtube.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "not json", ""),
    )

    with pytest.raises(FeedError, match="Could not read yt-dlp's output"):
        youtube.resolve(_item())


# --- as a feed ------------------------------------------------------------


def test_parse_feed_rewrites_a_channel_url(monkeypatch):
    """A channel url reaches feedparser as the Atom feed, not as the page."""
    from rssignal import feeds

    _patch_ytdlp(monkeypatch, info={"channel_id": "UCHnyfMqiRRG1u-2MsSQLbXA"})

    seen = []

    class FakeParsed:
        bozo = False
        entries = []
        feed = {}

    def fake_parse(url):
        seen.append(url)
        return FakeParsed()

    monkeypatch.setattr(feeds.feedparser, "parse", fake_parse)

    feeds.parse_feed(
        feeds.FeedConfig(url="https://www.youtube.com/@veritasium", name="Veritasium")
    )

    assert seen == [
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCHnyfMqiRRG1u-2MsSQLbXA"
    ]
