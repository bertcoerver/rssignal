"""Tests for rssignal.video, the front door (the sources themselves are faked).

What matters here is only the dispatch and the temp-file contract: which module
an item is handed to, and that the directory a source downloads into is gone
afterwards whatever happened. What each source then does is
tests/test_arte.py and tests/test_youtube.py.
"""

import os

import pytest

from rssignal import arte, video, youtube
from rssignal.feeds import FeedConfig, FeedError, FeedItem
from rssignal.video import (
    VideoTooShort,
    channel_feed_url,
    collection_feed,
    is_video_item,
    resolve_video,
    video_temp,
)

ARTE_LINK = "https://www.arte.tv/fr/videos/127395-052-A/le-dessous-des-images/"
YOUTUBE_LINK = "https://www.youtube.com/watch?v=BFcjfKZ0BeI"


class FakePlan:
    """A stand-in source result: writes ``payload`` and says where it went."""

    def __init__(self, payload=b"video", error=None):
        self.payload = payload
        self.error = error
        self.into = None

    def describe(self):
        return "640x360, ~63 MB"

    def fetch(self, into, *, timeout):
        self.into = into
        if self.error:
            raise self.error
        path = os.path.join(into, "video.mp4")
        with open(path, "wb") as fh:
            fh.write(self.payload)
        return path


def _item(link=ARTE_LINK):
    return FeedItem(title="t", description="d", link=link)


# --- dispatch -------------------------------------------------------------


@pytest.mark.parametrize(
    "link, expected",
    [
        (ARTE_LINK, True),
        (YOUTUBE_LINK, True),
        ("https://youtu.be/BFcjfKZ0BeI", True),
        ("https://waitbutwhy.com/2026/07/post.html", False),
        (None, False),
    ],
)
def test_is_video_item(link, expected):
    assert is_video_item(_item(link)) is expected


def test_resolve_video_asks_the_right_source(monkeypatch):
    asked = []
    monkeypatch.setattr(arte, "resolve", lambda item, **kw: asked.append("arte"))
    monkeypatch.setattr(youtube, "resolve", lambda item, **kw: asked.append("youtube"))

    resolve_video(_item(ARTE_LINK))
    resolve_video(_item(YOUTUBE_LINK))

    assert asked == ["arte", "youtube"]


def test_resolve_video_rejects_a_link_no_source_claims():
    with pytest.raises(FeedError, match="Not a video link"):
        resolve_video(_item("https://example.com/post"))


def test_collection_feed_leaves_an_ordinary_url_alone():
    assert collection_feed(FeedConfig(url="https://waitbutwhy.com/feed")) is None


def test_channel_feed_url_leaves_an_ordinary_url_alone():
    assert channel_feed_url("https://waitbutwhy.com/feed") is None


# --- the temp-file contract -----------------------------------------------


def test_video_temp_yields_the_file_and_removes_it_after():
    plan = FakePlan()

    with video_temp(_item(), plan=plan) as path:
        with open(path, "rb") as fh:
            assert fh.read() == b"video"
        assert os.path.dirname(path) == plan.into

    assert not os.path.exists(path)
    # Not just the file: the directory it was given goes too.
    assert not os.path.exists(plan.into)


def test_video_temp_cleans_up_when_the_download_fails():
    plan = FakePlan(error=FeedError("yt-dlp failed: 403"))

    with pytest.raises(FeedError, match="403"):
        with video_temp(_item(), plan=plan):
            pass

    assert not os.path.exists(plan.into)


def test_video_temp_cleans_up_when_sending_fails():
    plan = FakePlan()

    with pytest.raises(RuntimeError):
        with video_temp(_item(), plan=plan):
            raise RuntimeError("send blew up")

    assert not os.path.exists(plan.into)


def test_video_temp_rejects_a_file_over_the_limit():
    """The estimate can be wrong; the file that lands is what counts."""
    plan = FakePlan(payload=b"x" * 2048)

    with pytest.raises(FeedError, match="over the"):
        with video_temp(_item(), plan=plan, max_bytes=1024):
            pass

    assert not os.path.exists(plan.into)


def test_video_temp_resolves_when_given_no_plan(monkeypatch):
    plan = FakePlan()
    monkeypatch.setattr(video, "resolve_video", lambda item, **kw: plan)

    with video_temp(_item()) as path:
        assert os.path.exists(path)


def test_video_temp_passes_a_too_short_video_straight_through(monkeypatch):
    """VideoTooShort must reach the caller intact — it means "send nothing"."""
    monkeypatch.setattr(
        video, "resolve_video", lambda item, **kw: (_ for _ in ()).throw(
            VideoTooShort("47s")
        )
    )

    with pytest.raises(VideoTooShort):
        with video_temp(_item()):
            pass
