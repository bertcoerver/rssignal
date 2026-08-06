"""Tests for rssignal.arte (fetch_text and ffmpeg are monkeypatched — no network).

The playlist and player-config fixtures are trimmed copies of what ARTE really
served for programme 127395-052-A, so the parsing and quality-picking tests are
about the format as it actually is rather than as it might be.
"""

import json
import os
import subprocess

import pytest

from rssignal import arte
from rssignal.arte import Variant, arte_program_id
from rssignal.feeds import FeedError, FeedItem
from rssignal.video import video_temp

LINK = "https://www.arte.tv/fr/videos/127395-052-A/le-dessous-des-images/"
MASTER_URL = (
    "https://manifest-arte.akamaized.net/api/manifest/v1/Generate/"
    "2f93a34c-c554-445b-99f6-d898299737e8/fr/XQ+KS+CHEV1/127395-052-A.m3u8"
)
DURATION = 641.0

PLAYER_CONFIG = json.dumps(
    {
        "data": {
            "attributes": {
                "metadata": {"duration": {"seconds": 641}},
                "streams": [
                    {
                        "url": MASTER_URL,
                        "protocol": "API_HLS_NG_MA",
                        "versions": [{"code": "VOF"}],
                    }
                ],
            }
        }
    }
)

# Rung order here is the order ARTE serves, which is deliberately not sorted by
# resolution — that is exactly what the program-index mapping has to survive.
MASTER = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-INDEPENDENT-SEGMENTS

#EXT-X-STREAM-INF:BANDWIDTH=2333328,AVERAGE-BANDWIDTH=1163016,CODECS="avc1.4D001E,mp4a.40.2",RESOLUTION=768x432
https://cdn/v432.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5104424,AVERAGE-BANDWIDTH=2472688,CODECS="avc1.4d0028,mp4a.40.2",RESOLUTION=1920x1080
https://cdn/v1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4471280,AVERAGE-BANDWIDTH=2138912,CODECS="avc1.4D001F,mp4a.40.2",RESOLUTION=1280x720
https://cdn/v720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1447064,AVERAGE-BANDWIDTH=754704,CODECS="avc1.4D001E,mp4a.40.2",RESOLUTION=640x360
https://cdn/v360.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=788152,AVERAGE-BANDWIDTH=423264,CODECS="avc1.42000D,mp4a.40.2",RESOLUTION=384x216
https://cdn/v216.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5354736,AVERAGE-BANDWIDTH=2518296,CODECS="hev1.1.6.L120.B0,mp4a.40.2",RESOLUTION=1920x1080
https://cdn/v1080_h265.m3u8

#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=205824,AVERAGE-BANDWIDTH=99904,CODECS="avc1.4D001E",RESOLUTION=768x432,URI="https://cdn/v432_iframe.m3u8"

#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio_0",LANGUAGE="fr",NAME="français",DEFAULT=YES,URI="https://cdn/aud_fr.m3u8"
"""


def _patch_fetch(monkeypatch, *, config=PLAYER_CONFIG, master=MASTER):
    """Serve the player config and the master playlist, recording urls."""
    seen = []

    def fake_fetch_text(url, *, timeout=30):
        seen.append(url)
        if "api.arte.tv" in url:
            return config
        return master

    monkeypatch.setattr(arte, "fetch_text", fake_fetch_text)
    return seen


def _item(link=LINK):
    return FeedItem(title="La bataille du drapeau", description="d", link=link)


# --- link detection -------------------------------------------------------


@pytest.mark.parametrize(
    "link, expected",
    [
        (LINK, ("127395-052-A", "fr")),
        ("https://www.arte.tv/de/videos/127395-052-A/x/", ("127395-052-A", "de")),
        ("https://arte.tv/fr/videos/127395-052-A/", ("127395-052-A", "fr")),
        ("http://www.arte.tv/fr/videos/127395-052-A", ("127395-052-A", "fr")),
        # A collection listing is not a programme.
        ("https://www.arte.tv/fr/videos/RC-023176/le-dessous-des-images/", None),
        ("https://www.arte.tv/fr/", None),
        ("https://example.com/fr/videos/127395-052-A/", None),
        (None, None),
        ("", None),
    ],
)
def test_arte_program_id(link, expected):
    assert arte_program_id(link) == expected


# --- playlist parsing -----------------------------------------------------


def test_variants_indexes_match_stream_inf_order():
    variants = arte._variants(MASTER)

    # Six playable rungs; the I-FRAME entry must not take an index, or every
    # later rung would be off by one when handed to ffmpeg.
    assert [(v.index, v.resolution) for v in variants] == [
        (0, "768x432"),
        (1, "1920x1080"),
        (2, "1280x720"),
        (3, "640x360"),
        (4, "384x216"),
        (5, "1920x1080"),
    ]
    assert variants[0].bandwidth == 1163016
    assert variants[5].codecs.startswith("hev1")


def test_variants_falls_back_to_bandwidth_without_average():
    master = (
        '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n'
        "https://cdn/a.m3u8\n"
    )
    assert arte._variants(master)[0].bandwidth == 800000


def test_variants_without_playable_rungs_raises():
    with pytest.raises(FeedError, match="no playable variants"):
        arte._variants("#EXTM3U\n#EXT-X-VERSION:7\n")


# --- quality choice -------------------------------------------------------


def test_pick_variant_takes_sharpest_that_fits():
    variants = arte._variants(MASTER)

    # Over 641s, with the margin applied: 360p is ~63 MB and fits the 95 MB
    # budget, 432p is ~98 MB and doesn't.
    picked = arte._pick_variant(variants, DURATION, arte.VIDEO_MAX_BYTES, "id")
    assert picked.resolution == "640x360"
    # Not the first rung in the playlist — the index has to come from the
    # chosen variant, not from the order the rungs happen to appear in.
    assert picked.index == 3


def test_pick_variant_drops_to_a_lower_rung_when_longer():
    variants = arte._variants(MASTER)
    # Twice as long, so only the smallest rung still fits.
    picked = arte._pick_variant(variants, DURATION * 2, arte.VIDEO_MAX_BYTES, "id")
    assert picked.resolution == "384x216"


def test_pick_variant_never_picks_h265():
    variants = arte._variants(MASTER)
    # A budget big enough for everything: the 1080p h264 must win over the
    # h265 rung of the same resolution.
    picked = arte._pick_variant(variants, DURATION, 10**9, "id")
    assert picked.resolution == "1920x1080"
    assert picked.codecs.startswith("avc1")


def test_pick_variant_raises_when_nothing_fits():
    variants = arte._variants(MASTER)
    with pytest.raises(FeedError, match="too big to send"):
        arte._pick_variant(variants, DURATION, 1024, "127395-052-A")


# --- resolving ------------------------------------------------------------


def test_resolve_video_reports_stream_and_size(monkeypatch):
    seen = _patch_fetch(monkeypatch)

    plan = arte.resolve(_item())

    assert seen[0] == (
        "https://api.arte.tv/api/player/v2/config/fr/127395-052-A"
    )
    assert seen[1] == MASTER_URL
    assert plan.master_url == MASTER_URL
    assert plan.duration == DURATION
    assert plan.variant.resolution == "640x360"
    assert plan.describe() == "640x360, ~63 MB"


def test_resolve_video_prefers_the_original_language_stream(monkeypatch):
    config = json.dumps(
        {
            "data": {
                "attributes": {
                    "metadata": {"duration": {"seconds": 641}},
                    "streams": [
                        {"url": "https://dub/de.m3u8", "versions": [{"code": "VA"}]},
                        {"url": MASTER_URL, "versions": [{"code": "VOF-STMF"}]},
                    ],
                }
            }
        }
    )
    _patch_fetch(monkeypatch, config=config)
    assert arte.resolve(_item()).master_url == MASTER_URL


def test_resolve_rejects_a_non_arte_link():
    with pytest.raises(FeedError, match="Not an ARTE video link"):
        arte.resolve(_item("https://example.com/post"))


def test_resolve_video_without_streams_raises(monkeypatch):
    config = json.dumps(
        {"data": {"attributes": {"metadata": {"duration": {"seconds": 1}}, "streams": []}}}
    )
    _patch_fetch(monkeypatch, config=config)
    with pytest.raises(FeedError, match="No stream available"):
        arte.resolve(_item())


def test_resolve_video_with_unexpected_payload_raises(monkeypatch):
    _patch_fetch(monkeypatch, config='{"nope": true}')
    with pytest.raises(FeedError, match="Unexpected player config"):
        arte.resolve(_item())


def test_resolve_video_without_duration_raises(monkeypatch):
    config = json.dumps(
        {"data": {"attributes": {"metadata": {}, "streams": [{"url": MASTER_URL}]}}}
    )
    _patch_fetch(monkeypatch, config=config)
    with pytest.raises(FeedError, match="no duration"):
        arte.resolve(_item())


# --- downloading ----------------------------------------------------------


def _patch_ffmpeg(monkeypatch, *, returncode=0, stderr="", payload=b"video"):
    """Stand in for the ffmpeg binary, recording the command it was given."""
    calls = []

    monkeypatch.setattr(arte.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if payload is not None:
            with open(cmd[-1], "wb") as fh:
                fh.write(payload)
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(arte.subprocess, "run", fake_run)
    return calls


def test_video_temp_writes_and_cleans_up(monkeypatch):
    _patch_fetch(monkeypatch)
    calls = _patch_ffmpeg(monkeypatch)

    seen_path = None
    with video_temp(_item()) as path:
        seen_path = path
        assert path.endswith(".mp4")
        with open(path, "rb") as fh:
            assert fh.read() == b"video"

    assert not os.path.exists(seen_path)

    cmd = calls[0]
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert cmd[cmd.index("-i") + 1] == MASTER_URL
    # Both maps, pinned to the chosen program: video plus *one* audio track.
    assert "p:3:v:0" in cmd and "p:3:a:0" in cmd
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[cmd.index("-bsf:a") + 1] == "aac_adtstoasc"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"


def test_video_temp_reuses_a_resolved_plan(monkeypatch):
    seen = _patch_fetch(monkeypatch)
    _patch_ffmpeg(monkeypatch)

    plan = arte.resolve(_item())
    seen.clear()
    with video_temp(_item(), plan=plan):
        pass

    # The manifest is not fetched a second time.
    assert seen == []


def test_video_temp_ffmpeg_failure_raises_and_cleans_up(monkeypatch):
    _patch_fetch(monkeypatch)
    _patch_ffmpeg(
        monkeypatch, returncode=1, stderr="warning: x\nServer returned 403\n"
    )

    with pytest.raises(FeedError, match="Server returned 403"):
        with video_temp(_item()):
            pass


def test_video_temp_empty_output_raises(monkeypatch):
    _patch_fetch(monkeypatch)
    _patch_ffmpeg(monkeypatch, payload=b"")

    with pytest.raises(FeedError, match="produced no video"):
        with video_temp(_item()):
            pass


def test_video_temp_oversize_output_raises(monkeypatch):
    """The estimate can be wrong; the file that lands is what counts."""
    _patch_fetch(monkeypatch)
    _patch_ffmpeg(monkeypatch, payload=b"x" * 2048)

    with pytest.raises(FeedError, match="over the"):
        with video_temp(_item(), max_bytes=1024):
            pass


def test_video_temp_timeout_raises(monkeypatch):
    _patch_fetch(monkeypatch)
    monkeypatch.setattr(arte.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 900)

    monkeypatch.setattr(arte.subprocess, "run", fake_run)

    with pytest.raises(FeedError, match="timed out"):
        with video_temp(_item()):
            pass


def test_video_temp_without_ffmpeg_raises(monkeypatch):
    _patch_fetch(monkeypatch)
    monkeypatch.setattr(arte.shutil, "which", lambda name: None)

    with pytest.raises(FeedError, match="ffmpeg is needed"):
        with video_temp(_item()):
            pass


# --- collections as feeds -------------------------------------------------

COLLECTION_URL = "https://www.arte.tv/fr/videos/RC-023176/le-dessous-des-images/"

PLAYLIST = json.dumps(
    {
        "data": {
            "attributes": {
                "metadata": {
                    "title": "Le dessous des images",
                    "description": "<p>Sonia Devillers raconte.</p>",
                    "images": [{"url": "https://img/show.jpg"}],
                },
                "items": [
                    {
                        "providerId": "127395-052-A",
                        "title": "Le dessous des images",
                        "subtitle": "La bataille du drapeau",
                        "description": "Retiré sur ordre.",
                        "images": [{"url": "https://img/ep1.jpg"}],
                        "link": {"url": LINK},
                        "duration": {"seconds": 641},
                    },
                    {
                        "providerId": "127395-070-A",
                        "title": "Le dessous des images",
                        "subtitle": "Israël-Palestine",
                        "description": "Le 10 avril.",
                        "images": [{"url": "https://img/ep2.jpg"}],
                        "link": {"url": "https://www.arte.tv/fr/videos/127395-070-A/x/"},
                        "duration": {"seconds": 646},
                    },
                    {
                        "providerId": "127395-069-A",
                        "title": "Le dessous des images",
                        "subtitle": "Trump veut son arc",
                        "description": "À l’approche.",
                        "images": [],
                        "link": {"url": "https://www.arte.tv/fr/videos/127395-069-A/x/"},
                        "duration": {"seconds": 646},
                    },
                ],
            }
        }
    }
)

RIGHTS = {
    "127395-052-A": "2026-06-25T03:00:00+00:00",
    "127395-070-A": "2026-06-23T03:00:00+00:00",
    "127395-069-A": "2026-06-22T03:00:00+00:00",
}


def _patch_collection(monkeypatch, *, playlist=PLAYLIST, rights=RIGHTS):
    """Serve the playlist, and each programme's config for its rights window."""
    seen = []

    def fake_fetch_text(url, *, timeout=30):
        seen.append(url)
        if "/playlist/" in url:
            return playlist
        program_id = url.rsplit("/", 1)[-1]
        begin = rights.get(program_id)
        return json.dumps(
            {"data": {"attributes": {"rights": {"begin": begin} if begin else None}}}
        )

    monkeypatch.setattr(arte, "fetch_text", fake_fetch_text)
    return seen


def _collection_cfg():
    from rssignal.feeds import FeedConfig

    return FeedConfig(url=COLLECTION_URL, name="Le Dessous des Images")


@pytest.mark.parametrize(
    "url, expected",
    [
        (COLLECTION_URL, ("RC-023176", "fr")),
        ("https://www.arte.tv/de/videos/RC-023176/mit-offenen-augen/", ("RC-023176", "de")),
        # A single programme is not a collection.
        (LINK, None),
        ("https://waitbutwhy.com/feed", None),
        (None, None),
    ],
)
def test_arte_collection_id(url, expected):
    assert arte.arte_collection_id(url) == expected


def test_parse_arte_collection_builds_items(monkeypatch):
    seen = _patch_collection(monkeypatch)

    parsed = arte.parse_arte_collection(_collection_cfg())

    assert seen[0] == "https://api.arte.tv/api/player/v2/playlist/fr/RC-023176"
    assert parsed.image_url == "https://img/show.jpg"
    assert parsed.description == "Sonia Devillers raconte."

    first = parsed.items[0]
    # The episode's own name, not the show's, which is what a chat list shows.
    assert first.title == "La bataille du drapeau"
    assert first.description == "Retiré sur ordre."
    assert first.link == LINK
    assert first.image_url == "https://img/ep1.jpg"
    assert first.feed_name == "Le Dessous des Images"
    assert first.extra["program_id"] == "127395-052-A"
    assert first.extra["duration_seconds"] == "641"
    assert first.extra["show_title"] == "Le dessous des images"


def test_parse_arte_collection_dates_from_the_rights_window(monkeypatch):
    from datetime import datetime, timezone

    _patch_collection(monkeypatch)

    items = arte.parse_arte_collection(_collection_cfg()).items

    assert items[0].published == datetime(2026, 6, 25, 3, tzinfo=timezone.utc)
    # Newest first, as the playlist serves them — the order the dating relies on.
    assert [i.published for i in items] == sorted(
        (i.published for i in items), reverse=True
    )


def test_parse_arte_collection_only_dates_the_newest(monkeypatch):
    monkeypatch.setattr(arte, "ARTE_DATED_ITEMS", 2)
    seen = _patch_collection(monkeypatch)

    items = arte.parse_arte_collection(_collection_cfg()).items

    # One playlist fetch plus one config per dated item, and no more: the back
    # catalogue is left undated rather than costing a request each.
    assert len(seen) == 3
    assert items[2].published is None
    assert items[0].published is not None


def test_parse_arte_collection_survives_a_missing_date(monkeypatch):
    """One programme that won't give up a date must not sink the whole feed."""
    _patch_collection(monkeypatch, rights={"127395-052-A": RIGHTS["127395-052-A"]})

    items = arte.parse_arte_collection(_collection_cfg()).items

    assert items[0].published is not None
    assert items[1].published is None


def test_parse_arte_collection_falls_back_to_the_show_title(monkeypatch):
    playlist = json.dumps(
        {
            "data": {
                "attributes": {
                    "metadata": {},
                    "items": [
                        {
                            "providerId": "1-A",
                            "title": "Le dessous des images",
                            "subtitle": "",
                            "link": {"url": LINK},
                        }
                    ],
                }
            }
        }
    )
    _patch_collection(monkeypatch, playlist=playlist, rights={})

    items = arte.parse_arte_collection(_collection_cfg()).items
    assert items[0].title == "Le dessous des images"


def test_parse_arte_collection_without_episodes_raises(monkeypatch):
    playlist = json.dumps({"data": {"attributes": {"metadata": {}, "items": []}}})
    _patch_collection(monkeypatch, playlist=playlist)

    with pytest.raises(FeedError, match="listed no episodes"):
        arte.parse_arte_collection(_collection_cfg())


def test_parse_arte_collection_with_unexpected_payload_raises(monkeypatch):
    _patch_collection(monkeypatch, playlist='{"nope": true}')

    with pytest.raises(FeedError, match="Unexpected playlist"):
        arte.parse_arte_collection(_collection_cfg())


def test_parse_feed_routes_a_collection_url_to_arte(monkeypatch):
    """A collection url is read as episodes, never handed to feedparser."""
    from rssignal import feeds

    _patch_collection(monkeypatch)

    def fail(url, **kwargs):
        raise AssertionError("feedparser must not be used for a collection url")

    monkeypatch.setattr(feeds.feedparser, "parse", fail)

    parsed = feeds.parse_feed(_collection_cfg())
    assert parsed.items[0].title == "La bataille du drapeau"


def test_variant_estimate_includes_margin():
    variant = Variant(index=0, bandwidth=8000, width=640, height=360, codecs="avc1")
    # 8000 bps over 100s is 100_000 bytes, plus the 10% margin.
    assert variant.estimated_bytes(100) == 110_000
