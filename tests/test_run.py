"""Tests for rssignal.run (feeds, sending, and downloads are monkeypatched)."""

import dataclasses
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from rssignal import pending, run
from rssignal.feeds import (
    FeedConfig,
    FeedError,
    FeedFilter,
    FeedItem,
    ParsedFeed,
    SourceBlocked,
)
from rssignal.run import AlreadyRunning, run_feeds, single_run
from rssignal.signal_cli import LinkPreview, SignalError, SignalGroup
from rssignal.video import VideoTooShort
from rssignal.watermark import format_watermark, read_watermark

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)

# A watermark old enough that every item in a fixture counts as new. Tests about
# first-run behaviour pass their own groups (or none) instead.
LONG_AGO = format_watermark(NOW - timedelta(days=365))


def _stamp(items):
    """Give undated fixture items increasing publication dates, in feed order.

    Undated items are never sent — there is no way to tell whether they are new
    — so a test that isn't about dates would otherwise send nothing. Increasing
    in list order means the chronological send order matches how the test wrote
    them.
    """
    return [
        item
        if item.published is not None
        else dataclasses.replace(item, published=NOW + timedelta(seconds=i))
        for i, item in enumerate(items)
    ]


def _patch_feeds(
    monkeypatch, configs, items_by_url, *, image=None, blurb="", groups=None
):
    """Stand in for the config, the feeds, and the account's Signal groups.

    ``groups`` defaults to one group per config, already named after its feed,
    so a test that isn't about group resolution never trips over it.
    """
    monkeypatch.setattr(run, "load_feeds", lambda path: configs)
    monkeypatch.setattr(
        run,
        "parse_feed",
        lambda cfg: ParsedFeed(
            items=_stamp(items_by_url[cfg.url]), image_url=image, description=blurb
        ),
    )
    if groups is None:
        groups = [
            SignalGroup(id=f"{c.name}=", name=c.name, description=LONG_AGO)
            for c in configs
        ]
    return _patch_groups(monkeypatch, groups)


def _patch_groups(monkeypatch, existing=(), *, created_id="new="):
    """Patch group listing/creation, recording every call.

    Nothing here reaches signal-cli: no group is ever listed, created, or
    messaged for real.
    """
    calls = {"list": 0, "receive": 0, "created": [], "updated": []}
    groups = list(existing)

    def fake_list_groups():
        calls["list"] += 1
        return list(groups)

    def fake_receive():
        calls["receive"] += 1

    def fake_create_group(
        name, *, description=None, avatar=None, announcement_only=False
    ):
        calls["created"].append(
            {
                "name": name,
                "description": description,
                "avatar": avatar,
                "announcement_only": announcement_only,
            }
        )
        group = SignalGroup(id=created_id, name=name, description=description or "")
        groups.append(group)
        return group

    def fake_update_group(group_id, *, description=None, avatar=None):
        calls["updated"].append({"id": group_id, "description": description})

    monkeypatch.setattr(run, "list_groups", fake_list_groups)
    monkeypatch.setattr(run, "receive", fake_receive)
    monkeypatch.setattr(run, "create_group", fake_create_group)
    monkeypatch.setattr(run, "update_group", fake_update_group)
    return calls


def _capture_sends(monkeypatch):
    sends = []

    def fake_send(
        text, recipient=None, attachments=None, voice_note=False, preview=None
    ):
        sends.append(
            {
                "text": text,
                "recipient": recipient,
                "attachments": attachments,
                "voice_note": voice_note,
                "preview": preview,
            }
        )

    monkeypatch.setattr(run, "send_msg", fake_send)
    return sends


def test_run_regular_sends_text_per_item(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Blog")
    items = [
        FeedItem(title="One", description="d1", link="https://a/1"),
        FeedItem(title="Two", description="d2", link="https://a/2"),
    ]
    _patch_feeds(monkeypatch, [cfg], {"https://a": items})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json")

    assert count == 2
    assert sends[0]["text"] == "One\n\nd1\n\nhttps://a/1"
    assert all(s["voice_note"] is False for s in sends)


def test_run_podcast_downloads_and_sends_voice_note(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Pod")
    item = FeedItem(
        title="Ep", description="notes", enclosure_url="https://a/ep.mp3"
    )
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    @contextmanager
    def fake_download(url, **kwargs):
        assert url == "https://a/ep.mp3"
        yield "/tmp/fake-ep.mp3"

    monkeypatch.setattr(run, "download_temp", fake_download)

    count = run_feeds("feeds.json")

    assert count == 1
    assert sends[0]["voice_note"] is True
    assert sends[0]["attachments"] == ["/tmp/fake-ep.mp3"]
    assert sends[0]["recipient"] == "group:Pod="
    assert sends[0]["text"] == "Ep\n\nnotes"


def test_run_podcast_without_enclosure_falls_back_to_text(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Pod")
    item = FeedItem(title="Ep", description="notes", enclosure_url=None)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json")

    assert count == 1
    assert sends[0]["voice_note"] is False
    assert sends[0]["attachments"] is None


ARTE_LINK = "https://www.arte.tv/fr/videos/127395-052-A/le-dessous-des-images/"
YOUTUBE_LINK = "https://www.youtube.com/watch?v=BFcjfKZ0BeI"


class _FakePlan:
    """What resolve_video would hand back, without asking anyone anything."""

    def describe(self):
        return "640x360, ~63 MB"

    def fetch(self, into, *, timeout):  # pragma: no cover - video_temp is faked
        raise AssertionError("video_temp is faked; fetch should never run")


def _patch_video(monkeypatch, *, fail=None, resolve_fail=None, path="/tmp/fake.mp4"):
    """Stand in for the video fetch, recording the items it was asked about."""
    seen = []

    def fake_resolve(item, **kwargs):
        if resolve_fail is not None:
            raise resolve_fail
        return _FakePlan()

    @contextmanager
    def fake_video_temp(item, **kwargs):
        seen.append(item.link)
        if fail is not None:
            raise fail
        yield path

    monkeypatch.setattr(run, "resolve_video", fake_resolve)
    monkeypatch.setattr(run, "video_temp", fake_video_temp)
    return seen


def test_run_video_item_attaches_the_video(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Arte")
    item = FeedItem(title="Le drapeau", description="d", link=ARTE_LINK)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)
    seen = _patch_video(monkeypatch)

    count = run_feeds("feeds.json")

    assert count == 1
    assert seen == [ARTE_LINK]
    # One message, not two: no preview card means nothing for the attachment
    # to displace.
    assert len(sends) == 1
    assert sends[0]["attachments"] == ["/tmp/fake.mp4"]
    # A video is not a voice note.
    assert sends[0]["voice_note"] is False
    assert sends[0]["preview"] is None
    assert sends[0]["text"] == f"Le drapeau\n\nd\n\n{ARTE_LINK}"


def test_run_video_failure_still_sends_the_text(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", name="Arte")
    item = FeedItem(title="Le drapeau", description="d", link=ARTE_LINK)
    calls = _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)
    _patch_video(monkeypatch, resolve_fail=FeedError("expired rights"))

    count = run_feeds("feeds.json")

    assert count == 1
    assert sends[0]["attachments"] is None
    assert sends[0]["text"].endswith(ARTE_LINK)
    assert "video skipped: expired rights" in capsys.readouterr().err
    # The item counted as sent, so its watermark stands rather than being
    # rolled back for a retry that would fail the same way.
    assert calls["updated"]


def test_run_ordinary_link_is_not_treated_as_video(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Blog")
    item = FeedItem(title="One", description="d", link="https://a/1")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)
    seen = _patch_video(monkeypatch)

    run_feeds("feeds.json")

    assert seen == []
    assert sends[0]["attachments"] is None


def test_run_audio_enclosure_wins_over_a_video_link(monkeypatch):
    """An episode is an episode: no point asking ARTE about it as well."""
    cfg = FeedConfig(url="https://a", name="Pod")
    item = FeedItem(
        title="Ep", description="d", link=ARTE_LINK, enclosure_url="https://a/ep.mp3"
    )
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    _capture_sends(monkeypatch)
    seen = _patch_video(monkeypatch)

    @contextmanager
    def fake_download(url, **kwargs):
        yield "/tmp/fake-ep.mp3"

    monkeypatch.setattr(run, "download_temp", fake_download)

    run_feeds("feeds.json")

    assert seen == []


def test_run_video_download_failure_still_sends_the_text(monkeypatch, capsys):
    """Resolving worked and the download didn't; the text still goes out."""
    cfg = FeedConfig(url="https://a", name="Arte")
    item = FeedItem(title="Le drapeau", description="d", link=ARTE_LINK)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)
    _patch_video(monkeypatch, fail=FeedError("ffmpeg failed: 403"))

    assert run_feeds("feeds.json") == 1
    assert sends[0]["attachments"] is None
    assert "video skipped: ffmpeg failed: 403" in capsys.readouterr().err


def test_run_too_short_video_sends_nothing_at_all(monkeypatch, capsys):
    """A Short is not a message — and its watermark stands, so it stays gone."""
    cfg = FeedConfig(url="https://a", name="Tube")
    item = FeedItem(title="A Short", description="d", link=YOUTUBE_LINK)
    calls = _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)
    seen = _patch_video(monkeypatch, resolve_fail=VideoTooShort("47s — a Short"))

    count = run_feeds("feeds.json")

    assert sends == []
    # Not downloaded either: refused before anything was fetched.
    assert seen == []
    assert "skipped: 47s — a Short" in capsys.readouterr().err
    assert calls["updated"]
    # It counts as dealt with, which is what stops it being reconsidered.
    assert count == 1


def test_run_dry_run_reports_the_video_quality(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", name="Arte")
    item = FeedItem(title="Le drapeau", description="d", link=ARTE_LINK)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    monkeypatch.setattr(run, "resolve_video", lambda item: _FakePlan())

    run_feeds("feeds.json", dry_run=True)

    assert "video: 640x360, ~63 MB" in capsys.readouterr().out


def test_run_dry_run_reports_a_video_it_would_not_send(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", name="Tube")
    item = FeedItem(title="A Short", description="d", link=YOUTUBE_LINK)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})

    def boom(item):
        raise VideoTooShort("47s — a Short")

    monkeypatch.setattr(run, "resolve_video", boom)

    run_feeds("feeds.json", dry_run=True)

    assert "nothing sent: 47s — a Short" in capsys.readouterr().out


def test_run_dry_run_reports_an_unavailable_video(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", name="Arte")
    item = FeedItem(title="Le drapeau", description="d", link=ARTE_LINK)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})

    def boom(item):
        raise FeedError("expired rights")

    monkeypatch.setattr(run, "resolve_video", boom)

    run_feeds("feeds.json", dry_run=True)

    assert "video: unavailable (expired rights)" in capsys.readouterr().out


def test_run_uses_message_template(monkeypatch):
    cfg = FeedConfig(
        url="https://a",
        name="Blog",
        message_template="📰 {title} [{feed_name}]\n\n{link}",
    )
    item = FeedItem(title="One", description="d1", link="https://a/1", feed_name="Blog")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert sends[0]["text"] == "📰 One [Blog]\n\nhttps://a/1"


def test_run_applies_field_filters(monkeypatch):
    cfg = FeedConfig(
        url="https://a",
        name="Blog",
        filters=(FeedFilter("title", "excludes", ("sponsored",)),),
    )
    items = [
        FeedItem(title="Real post", description="d1"),
        FeedItem(title="Sponsored post", description="d2"),
    ]
    _patch_feeds(monkeypatch, [cfg], {"https://a": items})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json")

    assert count == 1
    assert sends[0]["text"].startswith("Real post")


# --- link previews ---------------------------------------------------------

_EPISODE = FeedItem(
    title="Ep",
    description="notes",
    link="https://a/ep.mp3",
    enclosure_url="https://a/ep.mp3",
    image_url="https://a/ep.jpg",
)


def _fake_downloads(monkeypatch, fail_on=None):
    """Patch download_temp, recording urls and optionally failing on one."""
    got = []

    @contextmanager
    def fake_download(url, **kwargs):
        got.append(url)
        if url == fail_on:
            raise FeedError(f"Could not download {url!r}: nope")
        yield f"/tmp/fake-{url.rsplit('/', 1)[-1]}"

    monkeypatch.setattr(run, "download_temp", fake_download)
    return got


def test_run_podcast_splits_card_and_voice_note(monkeypatch):
    # Signal drops a preview card from a message that has an attachment, so the
    # card and the audio have to be two messages.
    cfg = FeedConfig(url="https://a", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    sends = _capture_sends(monkeypatch)
    downloaded = _fake_downloads(monkeypatch)

    count = run_feeds("feeds.json")

    assert downloaded == ["https://a/ep.mp3", "https://a/ep.jpg"]
    assert count == 1  # one item, even though it took two messages
    assert len(sends) == 2

    card, audio = sends
    assert card["preview"] == LinkPreview(
        url="https://a/ep.mp3",
        title="Ep",
        description="",
        image="/tmp/fake-ep.jpg",
    )
    # The card carries the title, so the body is description + url only.
    assert card["text"] == "notes\n\nhttps://a/ep.mp3"
    assert card["attachments"] is None
    assert card["voice_note"] is False

    assert audio["attachments"] == ["/tmp/fake-ep.mp3"]
    assert audio["voice_note"] is True
    assert audio["preview"] is None
    # The title, though — a message with no body at all shows up in the chat
    # list as a bare "Voice Message", and this is the group's last message.
    assert audio["text"] == "Ep"


def test_run_decides_per_item_within_one_feed(monkeypatch):
    # A show that posts the occasional written note. There is no feed-level
    # setting to get this wrong: the note keeps its link and sends no audio.
    note = FeedItem(title="A note", description="words", link="https://a/note")
    cfg = FeedConfig(url="https://a", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE, note]})
    sends = _capture_sends(monkeypatch)
    _fake_downloads(monkeypatch)

    count = run_feeds("feeds.json")

    assert count == 2
    # Two messages for the episode, then one for the note.
    assert len(sends) == 3
    assert sends[-1] == {
        "text": "A note\n\nwords\n\nhttps://a/note",
        "recipient": sends[0]["recipient"],
        "attachments": None,
        "voice_note": False,
        "preview": None,
    }


def test_run_podcast_without_a_card_stays_one_message(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Pod", link_preview=False)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    sends = _capture_sends(monkeypatch)
    _fake_downloads(monkeypatch)

    run_feeds("feeds.json")

    assert len(sends) == 1
    assert sends[0]["voice_note"] is True
    assert sends[0]["text"].startswith("Ep")


def test_run_sends_without_artwork_when_its_download_fails(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    sends = _capture_sends(monkeypatch)
    _fake_downloads(monkeypatch, fail_on="https://a/ep.jpg")

    count = run_feeds("feeds.json")

    # The episode still goes out; only the card's image is lost.
    assert count == 1
    assert sends[0]["preview"].image == ""
    assert sends[0]["preview"].title == "Ep"
    assert sends[1]["attachments"] == ["/tmp/fake-ep.mp3"]
    assert "preview image skipped" in capsys.readouterr().err


def test_run_regular_feed_sends_no_preview(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Blog")
    item = FeedItem(title="One", description="d1", link="https://a/1")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert sends[0]["preview"] is None


def test_run_dry_run_describes_preview_without_downloading(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    _capture_sends(monkeypatch)

    def fail(url, **kwargs):
        raise AssertionError("dry run must not download")

    monkeypatch.setattr(run, "download_temp", fail)

    run_feeds("feeds.json", dry_run=True)

    out = capsys.readouterr().out
    assert "preview: Ep <https://a/ep.mp3>" in out
    assert "preview image: https://a/ep.jpg" in out


def test_run_dry_run_sends_nothing(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", name="Blog")
    item = FeedItem(title="One", description="d1", link="https://a/1")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", dry_run=True)

    assert count == 1
    assert sends == []
    assert "Blog" in capsys.readouterr().out


# --- one group per feed ----------------------------------------------------

_POST = FeedItem(title="One", description="d1", link="https://a/1")


def _blog(name="Blog", **kwargs):
    return FeedConfig(url="https://a", name=name, **kwargs)


def test_run_sends_to_the_group_named_after_the_feed(monkeypatch):
    existing = SignalGroup(id="book=", name="Blog")
    calls = _patch_feeds(
        monkeypatch, [_blog()], {"https://a": [_POST]}, groups=[existing]
    )
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert sends[0]["recipient"] == "group:book="
    assert calls["created"] == []


def test_run_matches_a_group_name_case_insensitively(monkeypatch):
    calls = _patch_feeds(
        monkeypatch,
        [_blog("blog")],
        {"https://a": [_POST]},
        groups=[SignalGroup(id="book=", name="  Blog  ")],
    )
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert sends[0]["recipient"] == "group:book="
    assert calls["created"] == []


def test_run_creates_the_group_when_it_is_missing(monkeypatch, capsys):
    calls = _patch_feeds(
        monkeypatch, [_blog()], {"https://a": [_POST]}, groups=[]
    )
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert calls["created"] == [
        {"name": "Blog", "description": None, "avatar": None, "announcement_only": True}
    ]
    assert sends[0]["recipient"] == "group:new="
    # Creating a group is loud: a typo in a feed's name shouldn't pass unseen.
    assert "Created group 'Blog'" in capsys.readouterr().out


def test_run_uses_the_feed_artwork_as_the_group_image(monkeypatch):
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [_POST]},
        image="https://a/show.jpg",
        groups=[],
    )
    _capture_sends(monkeypatch)
    downloaded = _fake_downloads(monkeypatch)

    run_feeds("feeds.json")

    assert downloaded == ["https://a/show.jpg"]
    assert calls["created"][0]["avatar"] == "/tmp/fake-show.jpg"


def test_run_creates_the_group_without_an_image_when_the_download_fails(
    monkeypatch, capsys
):
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [_POST]},
        image="https://a/show.jpg",
        groups=[],
    )
    _capture_sends(monkeypatch)
    _fake_downloads(monkeypatch, fail_on="https://a/show.jpg")

    run_feeds("feeds.json")

    # Artwork is decoration; losing it must not cost you the group.
    assert calls["created"][0]["avatar"] is None
    assert "group image skipped" in capsys.readouterr().err


def test_run_drains_the_queue_before_reading_the_group_list(monkeypatch):
    # listGroups reads local state on a linked device. Without a receive first,
    # a group you made on your phone is invisible and a group you *left* still
    # reads as active — and sending into one you left fails silently.
    calls = _patch_feeds(
        monkeypatch, [_blog()], {"https://a": [_POST]}, groups=[]
    )
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert calls["receive"] == 1


def test_run_refreshes_even_when_a_stale_group_matches(monkeypatch):
    # The regression: refreshing only on a miss leaves a stale hit uncorrected.
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [_POST]},
        groups=[SignalGroup(id="book=", name="Blog")],
    )
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert calls["receive"] == 1


def test_run_lists_groups_once_for_several_feeds(monkeypatch):
    feeds = [_blog("Blog"), FeedConfig(url="https://b", name="Other")]
    calls = _patch_feeds(
        monkeypatch,
        feeds,
        {"https://a": [_POST], "https://b": [_POST]},
        groups=[SignalGroup(id="a=", name="Blog"), SignalGroup(id="b=", name="Other")],
    )
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    # Every signal-cli call pays a JVM start; N feeds must not cost N listings,
    # and one refresh covers the whole run.
    assert calls["list"] == 1
    assert calls["receive"] == 1


def test_run_creates_no_group_for_a_feed_with_nothing_to_send(monkeypatch):
    cfg = _blog(filters=(FeedFilter("title", "excludes", ("One",)),))
    calls = _patch_feeds(monkeypatch, [cfg], {"https://a": [_POST]}, groups=[])
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json")

    assert count == 0
    assert sends == []
    # An unused feed shouldn't leave an empty group behind.
    assert calls["created"] == []


def test_run_dry_run_creates_nothing_and_says_so(monkeypatch, capsys):
    calls = _patch_feeds(
        monkeypatch, [_blog()], {"https://a": [_POST]}, groups=[]
    )
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json", dry_run=True)

    assert calls["created"] == []
    assert sends == []
    assert "would be created" in capsys.readouterr().out


def test_run_dry_run_names_an_existing_group(monkeypatch, capsys):
    _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [_POST]},
        groups=[SignalGroup(id="book=", name="Blog")],
    )
    _capture_sends(monkeypatch)

    run_feeds("feeds.json", dry_run=True)

    out = capsys.readouterr().out
    assert "group:book=" in out
    assert "would be created" not in out


def test_run_to_overrides_every_group(monkeypatch):
    calls = _patch_feeds(
        monkeypatch, [_blog()], {"https://a": [_POST]}, groups=[]
    )
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json", to="+31611111111")

    assert sends[0]["recipient"] == "+31611111111"
    # A test send must not touch groups at all — not even to look.
    assert calls == {"list": 0, "receive": 0, "created": [], "updated": []}


def test_run_refuses_to_guess_between_two_groups_with_one_name(monkeypatch, capsys):
    _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [_POST]},
        groups=[SignalGroup(id="a=", name="Blog"), SignalGroup(id="b=", name="Blog")],
    )
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 0
    assert sends == []
    assert "2 groups are called" in capsys.readouterr().err


def test_run_ignores_groups_you_have_left_or_blocked(monkeypatch):
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [_POST]},
        groups=[
            SignalGroup(id="old=", name="Blog", active=False),
            SignalGroup(id="bad=", name="Blog", blocked=True),
        ],
    )
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    # Neither is a place to send, so a fresh group is the right answer.
    assert calls["created"] == [
        {"name": "Blog", "description": None, "avatar": None, "announcement_only": True}
    ]


def test_run_uses_the_feed_blurb_as_the_group_description(monkeypatch):
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [_POST]},
        blurb="Everything worth reading, daily.",
        groups=[],
    )
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert calls["created"][0]["description"] == "Everything worth reading, daily."


def test_run_creates_the_group_without_a_description_when_the_feed_has_none(
    monkeypatch,
):
    calls = _patch_feeds(monkeypatch, [_blog()], {"https://a": [_POST]}, groups=[])
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert calls["created"][0]["description"] is None


# --- watermarks -------------------------------------------------------------
#
# How far a feed got is remembered in its group's description, because Signal
# offers nowhere else to put it. Group names, ids and descriptions here are all
# invented, and nothing in these tests reaches signal-cli.


def _dated(title, when):
    return FeedItem(title=title, description="d", link=f"https://a/{title}", published=when)


OLDEST = _dated("Oldest", NOW - timedelta(days=2))
MIDDLE = _dated("Middle", NOW - timedelta(days=1))
LATEST = _dated("Latest", NOW)


def _group_at(when, *, name="Blog", blurb="A show."):
    """A group whose description says the feed got as far as ``when``."""
    description = blurb if when is None else f"{blurb}\n\n{format_watermark(when)}"
    return SignalGroup(id="blog=", name=name, description=description)


def test_run_sends_only_what_is_newer_than_the_watermark(monkeypatch):
    group = _group_at(MIDDLE.published)
    _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST, MIDDLE, OLDEST]},
        groups=[group],
    )
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json")

    # MIDDLE is the watermark itself, so it must not go again.
    assert count == 1
    assert [s["text"].splitlines()[0] for s in sends] == ["Latest"]


def test_run_sends_nothing_when_the_feed_has_not_moved(monkeypatch):
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST, MIDDLE]},
        groups=[_group_at(LATEST.published)],
    )
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 0
    assert sends == []
    # Nothing sent means nothing recorded, so the chat stays silent: no
    # "changed the group description" line for a run with no news.
    assert calls["updated"] == []


def test_run_without_a_watermark_sends_only_the_newest_item(monkeypatch):
    # A group that has never been used by rssignal must not disgorge the whole
    # back catalogue on its first run.
    _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST, MIDDLE, OLDEST]},
        groups=[_group_at(None)],
    )
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 1
    assert sends[0]["text"].splitlines()[0] == "Latest"


def test_run_on_a_brand_new_group_sends_only_the_newest_item(monkeypatch):
    calls = _patch_feeds(
        monkeypatch, [_blog()], {"https://a": [LATEST, MIDDLE, OLDEST]}, groups=[]
    )
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 1
    assert len(calls["created"]) == 1
    assert sends[0]["text"].splitlines()[0] == "Latest"


def test_run_ignores_a_corrupt_watermark(monkeypatch):
    # Editing the description by hand should cost one duplicate, not a crash.
    group = SignalGroup(id="blog=", name="Blog", description="A show.\n\n[rssignal soon]")
    _patch_feeds(
        monkeypatch, [_blog()], {"https://a": [LATEST, MIDDLE]}, groups=[group]
    )
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 1
    assert sends[0]["text"].splitlines()[0] == "Latest"


def test_run_records_the_newest_item_it_sent(monkeypatch):
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST, MIDDLE, OLDEST]},
        groups=[_group_at(OLDEST.published)],
    )
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    # One marker per item, since each goes up before its own messages.
    assert [c["id"] for c in calls["updated"]] == ["blog="] * 2
    # The stamp is the item's publication date, not the time of the run.
    assert [read_watermark(c["description"]) for c in calls["updated"]] == [
        MIDDLE.published,
        LATEST.published,
    ]


def test_run_records_an_item_before_it_sends_it(monkeypatch):
    # The group-detail line Signal adds to the chat should sit above the item it
    # belongs to, not after its messages.
    _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST, MIDDLE]},
        groups=[_group_at(OLDEST.published)],
    )

    trace = []
    monkeypatch.setattr(
        run,
        "update_group",
        lambda group_id, *, description=None, avatar=None: trace.append(
            f"mark {read_watermark(description).isoformat()}"
        ),
    )
    monkeypatch.setattr(
        run,
        "send_msg",
        lambda text, **kwargs: trace.append(f"send {text.splitlines()[0]}"),
    )

    run_feeds("feeds.json")

    assert trace == [
        f"mark {MIDDLE.published.isoformat()}",
        "send Middle",
        f"mark {LATEST.published.isoformat()}",
        "send Latest",
    ]


def test_run_sends_oldest_first(monkeypatch):
    _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST, MIDDLE, OLDEST]},
        groups=[_group_at(NOW - timedelta(days=10))],
    )
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    # Feeds list newest-first; sending in that order would strand the older
    # items behind the watermark if a send failed part-way.
    assert [s["text"].splitlines()[0] for s in sends] == ["Oldest", "Middle", "Latest"]


def test_run_rolls_the_watermark_back_when_a_send_fails_part_way(monkeypatch):
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST, MIDDLE, OLDEST]},
        groups=[_group_at(NOW - timedelta(days=10))],
    )

    sent = []

    def flaky_send(text, recipient=None, **kwargs):
        if text.startswith("Middle"):
            raise SignalError("no route to host")
        sent.append(text)

    monkeypatch.setattr(run, "send_msg", flaky_send)

    run_feeds("feeds.json")

    # Middle's marker went up before its messages, as designed; when the send
    # failed it was rolled back, so the run ends on Oldest and Middle and Latest
    # are both retried next time. Losing them would be the worse failure.
    assert [t.splitlines()[0] for t in sent] == ["Oldest"]
    assert [read_watermark(c["description"]) for c in calls["updated"]] == [
        OLDEST.published,
        MIDDLE.published,
        OLDEST.published,  # the rollback
    ]


def test_run_rolls_back_to_no_watermark_when_there_was_none(monkeypatch):
    # A group rssignal has never written to must be left exactly as it was, not
    # marked with an item that never arrived.
    blurb = "A blurb somebody typed themselves."
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST]},
        groups=[SignalGroup(id="blog=", name="Blog", description=blurb)],
    )

    def failing_send(text, recipient=None, **kwargs):
        raise SignalError("no route to host")

    monkeypatch.setattr(run, "send_msg", failing_send)

    run_feeds("feeds.json")

    assert calls["updated"][-1]["description"] == blurb
    assert read_watermark(calls["updated"][-1]["description"]) is None


def test_run_reports_a_rollback_that_itself_fails(monkeypatch, capsys):
    # Two failures in a row is the one case where an item really is lost. Say so
    # plainly, and point at the way to get it back.
    _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST]},
        groups=[_group_at(OLDEST.published)],
    )

    updates = []

    def flaky_update(group_id, *, description=None, avatar=None):
        updates.append(description)
        if len(updates) > 1:  # the rollback
            raise SignalError("group update rejected")

    def failing_send(text, recipient=None, **kwargs):
        raise SignalError("no route to host")

    monkeypatch.setattr(run, "update_group", flaky_update)
    monkeypatch.setattr(run, "send_msg", failing_send)

    run_feeds("feeds.json")

    # Both are reported: the send failure ends the feed, and the rollback
    # failure is the reason the item is gone rather than merely delayed.
    err = capsys.readouterr().err
    assert "undoing its watermark failed" in err
    assert "--since" in err
    assert "no route to host" in err


def test_run_keeps_a_hand_edited_blurb_when_it_records(monkeypatch):
    mine = "My own words about this feed."
    group = SignalGroup(
        id="blog=", name="Blog", description=f"{mine}\n\n{format_watermark(OLDEST.published)}"
    )
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST]},
        blurb="The feed's own boilerplate.",
        groups=[group],
    )
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    written = calls["updated"][0]["description"]
    assert written.startswith(mine)
    assert "boilerplate" not in written


def test_run_falls_back_to_the_feed_blurb_when_the_group_has_none(monkeypatch):
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST]},
        blurb="What the feed says about itself.",
        groups=[SignalGroup(id="blog=", name="Blog", description="")],
    )
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert calls["updated"][0]["description"].startswith("What the feed says")


def test_run_since_overrides_the_stored_watermark(monkeypatch):
    _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST, MIDDLE, OLDEST]},
        groups=[_group_at(LATEST.published)],
    )
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", since=NOW - timedelta(days=10))

    # The group says it is up to date; --since says replay anyway.
    assert count == 3
    assert [s["text"].splitlines()[0] for s in sends] == ["Oldest", "Middle", "Latest"]


def test_run_to_reads_and_writes_no_watermark(monkeypatch):
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST, MIDDLE, OLDEST]},
        groups=[_group_at(NOW - timedelta(days=10))],
    )
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json", to="+31611111111")

    # A test send has no effect on rssignal's idea of where the feed got to,
    # in either direction: it sends the newest item and records nothing.
    assert [s["text"].splitlines()[0] for s in sends] == ["Latest"]
    assert calls["updated"] == []
    assert calls["list"] == 0


def test_run_dry_run_records_nothing(monkeypatch, capsys):
    calls = _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST, MIDDLE]},
        groups=[_group_at(MIDDLE.published)],
    )
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", dry_run=True)

    assert count == 1
    assert sends == []
    assert calls["updated"] == []
    assert "Latest" in capsys.readouterr().out


def test_run_recording_failure_does_not_fail_the_run(monkeypatch, capsys):
    # Sending the item is the point; a marker that didn't land only means it
    # goes out once more on the next run.
    _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [LATEST]},
        groups=[_group_at(OLDEST.published)],
    )
    _capture_sends(monkeypatch)

    def failing_update(group_id, *, description=None, avatar=None):
        raise SignalError("group update rejected")

    monkeypatch.setattr(run, "update_group", failing_update)

    assert run_feeds("feeds.json") == 1
    assert "recording how far the feed got failed" in capsys.readouterr().err


# --- one bad feed does not take the others with it --------------------------
#
# A run is usually unattended — a scheduled job, an Apple Shortcut — so the
# interesting question about a feed that fails is what happens to the ones
# after it.

_BROKEN = FeedConfig(url="https://broken", name="Broken")
_FINE = FeedConfig(url="https://fine", name="Fine")


def _patch_two_feeds(monkeypatch, parse):
    """Configure Broken and Fine, in that order, parsed by ``parse``."""
    configs = [_BROKEN, _FINE]
    monkeypatch.setattr(run, "load_feeds", lambda path: configs)
    monkeypatch.setattr(run, "parse_feed", parse)
    return _patch_groups(
        monkeypatch,
        [
            SignalGroup(id=f"{c.name}=", name=c.name, description=LONG_AGO)
            for c in configs
        ],
    )


def _one_item(cfg):
    return ParsedFeed(items=_stamp([FeedItem(title=cfg.name, description="d", link=f"{cfg.url}/1")]))


def test_run_carries_on_after_a_feed_fails_to_parse(monkeypatch, capsys):
    def parse(cfg):
        if cfg is _BROKEN:
            raise FeedError("could not read the feed")
        return _one_item(cfg)

    _patch_two_feeds(monkeypatch, parse)
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 1
    assert [s["text"].splitlines()[0] for s in sends] == ["Fine"]
    err = capsys.readouterr().err
    assert "[Broken] skipped: could not read the feed" in err
    assert "1 of 2 feed(s) failed" in err


def test_run_reports_a_blocked_source_as_a_skip_not_a_failure(
    monkeypatch, capsys, error_log
):
    # A DNS profile that closes YouTube during the day is not a fault: the other
    # feeds go, the line says why, and no traceback is written. The log itself
    # exists either way — every run marks its own start and end in it.
    def parse(cfg):
        if cfg is _BROKEN:
            raise SourceBlocked("youtube.com is not reachable from here")
        return _one_item(cfg)

    _patch_two_feeds(monkeypatch, parse)
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 1
    assert [s["text"].splitlines()[0] for s in sends] == ["Fine"]

    err = capsys.readouterr().err
    assert "[Broken] skipped: youtube.com is not reachable from here" in err
    assert "1 of 2 feed(s) skipped: source blocked" in err
    assert "failed" not in err
    assert "Traceback" not in error_log.read_text()


def test_run_logs_which_feeds_were_blocked(monkeypatch, error_log):
    # stderr is thrown away by a scheduled run, and "why has that feed gone
    # quiet?" is asked days later. One stamped line, no traceback.
    def parse(cfg):
        if cfg is _BROKEN:
            raise SourceBlocked("youtube.com is not reachable from here")
        return _one_item(cfg)

    _patch_two_feeds(monkeypatch, parse)
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    logged = error_log.read_text()
    assert "[Broken] skipped: youtube.com is not reachable from here" in logged
    assert "Traceback" not in logged


def test_run_logs_the_traceback_of_a_failed_feed(monkeypatch, capsys, error_log):
    # stderr gets a line; the file gets what you actually need to debug it,
    # and is still there when the unattended run is long over.
    def parse(cfg):
        if cfg is _BROKEN:
            raise FeedError("could not read the feed")
        return _one_item(cfg)

    _patch_two_feeds(monkeypatch, parse)
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    logged = error_log.read_text()
    assert "feed 'Broken'" in logged
    assert "Traceback (most recent call last)" in logged
    assert "FeedError: could not read the feed" in logged
    # And the summary says where to look.
    assert str(error_log) in capsys.readouterr().err


def test_run_appends_to_the_log_rather_than_replacing_it(monkeypatch, error_log):
    error_log.write_text("something from an earlier run\n")

    def parse(cfg):
        raise FeedError("could not read the feed")

    _patch_two_feeds(monkeypatch, parse)
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    logged = error_log.read_text()
    assert logged.startswith("something from an earlier run")
    # Both feeds failed, and both are in there.
    assert "feed 'Broken'" in logged and "feed 'Fine'" in logged


def test_a_second_run_stands_aside(monkeypatch):
    # The duplicate this exists to prevent: both runs read the same watermark,
    # both decide the same items are new, both send them.
    _patch_two_feeds(monkeypatch, _one_item)
    sends = _capture_sends(monkeypatch)

    with single_run():
        with pytest.raises(AlreadyRunning):
            run_feeds("feeds.json")

    assert sends == []


def test_the_lock_is_released_when_the_run_ends(monkeypatch):
    _patch_two_feeds(monkeypatch, _one_item)
    _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 2
    # Nothing is holding it now, so the next run gets straight in.
    with single_run():
        pass


def test_the_lock_is_released_when_the_run_fails(monkeypatch):
    # A run that died must not lock the next one out — the file descriptor
    # closing is what releases it, which happens however the block is left.
    _patch_two_feeds(monkeypatch, _one_item)

    def die(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(run, "_locked_run", die)

    with pytest.raises(RuntimeError):
        run_feeds("feeds.json")
    with single_run():
        pass


def test_a_dry_run_takes_no_lock(monkeypatch, capsys):
    # It sends nothing, so it can't collide with anything — and staying usable
    # while a real run is going is the whole point of a dry run.
    _patch_two_feeds(monkeypatch, _one_item)
    _capture_sends(monkeypatch)

    with single_run():
        assert run_feeds("feeds.json", dry_run=True) == 2
    capsys.readouterr()


def test_run_logs_its_start_and_end(monkeypatch, error_log):
    _patch_two_feeds(monkeypatch, _one_item)
    _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 2

    logged = error_log.read_text()
    assert "run started: 2 feed(s)" in logged
    assert "run finished in" in logged
    assert "2 item(s) sent" in logged


def test_run_killed_partway_leaves_a_start_with_no_end(monkeypatch, error_log):
    # The whole point of the pair. Nothing here can simulate a SIGKILL, so this
    # asserts the half that would survive one: the start line is written before
    # any work happens, not alongside the end.
    _patch_two_feeds(monkeypatch, _one_item)

    def die(*args, **kwargs):
        raise AssertionError("killed midway")

    monkeypatch.setattr(run, "_run_prepared", die)

    with pytest.raises(AssertionError):
        run_feeds("feeds.json")

    logged = error_log.read_text()
    assert "run started" in logged
    assert "run finished" not in logged
    # A run that died where we could see it says so; one that was killed
    # outright says nothing, and the missing line is the evidence.
    assert "run aborted after" in logged


def test_run_marks_a_dry_run_as_one(monkeypatch, error_log, capsys):
    _patch_two_feeds(monkeypatch, _one_item)
    _capture_sends(monkeypatch)

    run_feeds("feeds.json", dry_run=True)
    capsys.readouterr()

    assert "run started (dry run)" in error_log.read_text()


def test_run_survives_a_log_that_cannot_be_written(monkeypatch, capsys, tmp_path):
    # A log that can't be written must never be the reason a feed isn't sent.
    monkeypatch.setenv("RSSIGNAL_LOG", str(tmp_path / "nope" / "rssignal.log"))

    def parse(cfg):
        if cfg is _BROKEN:
            raise FeedError("could not read the feed")
        return _one_item(cfg)

    _patch_two_feeds(monkeypatch, parse)
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 1
    assert [s["text"].splitlines()[0] for s in sends] == ["Fine"]
    assert "could not write to" in capsys.readouterr().err


def test_run_carries_on_after_a_send_fails(monkeypatch, capsys):
    _patch_two_feeds(monkeypatch, _one_item)

    sent = []

    def flaky_send(text, recipient=None, **kwargs):
        if text.startswith("Broken"):
            raise SignalError("no route to host")
        sent.append(text)

    monkeypatch.setattr(run, "send_msg", flaky_send)

    assert run_feeds("feeds.json") == 1
    assert [t.splitlines()[0] for t in sent] == ["Fine"]
    assert "no route to host" in capsys.readouterr().err


def test_run_carries_on_after_a_feed_fails_unexpectedly(
    monkeypatch, capsys, error_log
):
    # Not every way a feed can fail is a FeedError — yt-dlp and the parsers can
    # still surprise us, and the run has to survive those too.
    def parse(cfg):
        if cfg is _BROKEN:
            raise KeyError("format_id")
        return _one_item(cfg)

    _patch_two_feeds(monkeypatch, parse)
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 1
    assert [s["text"].splitlines()[0] for s in sends] == ["Fine"]
    assert "[Broken] skipped" in capsys.readouterr().err
    # A bug, not a bad feed: the traceback is what makes it fixable.
    assert "KeyError" in error_log.read_text()


def test_run_leaves_a_failed_feed_where_it_was(monkeypatch):
    # Nothing was sent for Broken, so nothing may be recorded for it either:
    # the next run has to start from the same place.
    def parse(cfg):
        if cfg is _BROKEN:
            raise FeedError("could not read the feed")
        return _one_item(cfg)

    calls = _patch_two_feeds(monkeypatch, parse)
    _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert [c["id"] for c in calls["updated"]] == ["Fine="]


def test_run_still_raises_when_the_config_cannot_be_read(monkeypatch):
    # No feeds at all means there is nothing to isolate one failure from.
    def failing_load(path):
        raise FeedError("feeds.json is not valid JSON")

    monkeypatch.setattr(run, "load_feeds", failing_load)

    with pytest.raises(FeedError):
        run_feeds("feeds.json")


# --- an item that got half-way out -----------------------------------------
#
# A podcast episode with a card is two messages, and the gap between them is the
# only place a run can leave an item neither sent nor unsent. The watermark
# rollback brings the item back, which is right; what it must not do any more is
# bring the card back with it.


def _half_failing_sends(monkeypatch, *, fail_voice_notes):
    """Capture sends, failing every voice note while ``fail_voice_notes`` is on.

    The list is mutable on purpose: a test flips it off between runs to model the
    upload working the second time round.
    """
    sends = []

    def fake_send(
        text, recipient=None, attachments=None, voice_note=False, preview=None
    ):
        if voice_note and fail_voice_notes[0]:
            raise SignalError("Connection reset (PushNetworkException)")
        sends.append(
            {
                "text": text,
                "recipient": recipient,
                "attachments": attachments,
                "voice_note": voice_note,
                "preview": preview,
            }
        )

    monkeypatch.setattr(run, "send_msg", fake_send)
    return sends


def test_run_sends_only_the_missing_half_after_a_failed_voice_note(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    _fake_downloads(monkeypatch)
    failing = [True]
    sends = _half_failing_sends(monkeypatch, fail_voice_notes=failing)

    run_feeds("feeds.json")  # The card lands; the upload doesn't.
    assert [s["voice_note"] for s in sends] == [False]

    # Same episode, next run: the watermark was rolled back so it comes round
    # again, and it is the audio — not a second copy of the card — that goes out.
    failing[0] = False
    sends.clear()
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})

    assert run_feeds("feeds.json") == 1
    assert [s["voice_note"] for s in sends] == [True]
    assert sends[0]["attachments"] == ["/tmp/fake-ep.mp3"]


def test_run_says_it_is_finishing_a_half_sent_item(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    _fake_downloads(monkeypatch)
    failing = [True]
    _half_failing_sends(monkeypatch, fail_voice_notes=failing)

    run_feeds("feeds.json")
    failing[0] = False
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    capsys.readouterr()

    run_feeds("feeds.json")

    assert "card already sent" in capsys.readouterr().err


def test_run_owes_nothing_once_both_halves_are_out(monkeypatch):
    # The note is cleared by the voice note landing, so an episode replayed with
    # --since gets its card again rather than arriving as a bare audio file.
    cfg = FeedConfig(url="https://a", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    _fake_downloads(monkeypatch)
    sends = _half_failing_sends(monkeypatch, fail_voice_notes=[False])

    run_feeds("feeds.json")
    assert len(sends) == 2

    sends.clear()
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    run_feeds("feeds.json", since=NOW - timedelta(days=1))

    assert [s["voice_note"] for s in sends] == [False, True]


def test_run_owes_nothing_for_a_different_item(monkeypatch):
    # The note names one item in one group. Nothing else may inherit it.
    other = FeedItem(
        title="Ep 2",
        description="notes",
        link="https://a/ep2.mp3",
        enclosure_url="https://a/ep2.mp3",
        image_url="https://a/ep.jpg",
    )
    cfg = FeedConfig(url="https://a", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    _fake_downloads(monkeypatch)
    failing = [True]
    sends = _half_failing_sends(monkeypatch, fail_voice_notes=failing)

    run_feeds("feeds.json")

    failing[0] = False
    sends.clear()
    _patch_feeds(monkeypatch, [cfg], {"https://a": [other]})
    run_feeds("feeds.json")

    assert [s["voice_note"] for s in sends] == [False, True]


def test_run_writes_off_a_debt_that_is_never_paid(monkeypatch):
    # An episode whose audio can never be sent would otherwise suppress its own
    # card for good. After the TTL the item is treated as new again.
    cfg = FeedConfig(url="https://a", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    _fake_downloads(monkeypatch)
    failing = [True]
    sends = _half_failing_sends(monkeypatch, fail_voice_notes=failing)

    run_feeds("feeds.json")

    later = time.time() + pending.TTL + 1
    monkeypatch.setattr(pending.time, "time", lambda: later)
    failing[0] = False
    sends.clear()
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    run_feeds("feeds.json")

    assert [s["voice_note"] for s in sends] == [False, True]


def test_run_sends_normally_when_the_note_cannot_be_written(monkeypatch, tmp_path):
    # Everything here is best-effort: an unwritable note costs a duplicate card
    # one day and must never cost a send.
    # A real unwritable path rather than a patched one: the directory the note
    # would live in is already a file.
    blocked = tmp_path / "in-the-way"
    blocked.write_text("not a directory")
    monkeypatch.setenv(pending.PENDING_ENV, str(blocked / "pending.json"))
    cfg = FeedConfig(url="https://a", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    _fake_downloads(monkeypatch)
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 1
    assert [s["voice_note"] for s in sends] == [False, True]
