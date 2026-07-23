"""Tests for rssignal.run (feeds, sending, and downloads are monkeypatched)."""

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from rssignal import run
from rssignal.feeds import FeedConfig, FeedError, FeedFilter, FeedItem, ParsedFeed
from rssignal.run import run_feeds
from rssignal.signal_cli import LinkPreview, SignalError, SignalGroup

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


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
            items=items_by_url[cfg.url], image_url=image, description=blurb
        ),
    )
    if groups is None:
        groups = [SignalGroup(id=f"{c.name}=", name=c.name) for c in configs]
    return _patch_groups(monkeypatch, groups)


def _patch_groups(monkeypatch, existing=(), *, created_id="new="):
    """Patch group listing/creation, recording every call.

    Nothing here reaches signal-cli: no group is ever listed, created, or
    messaged for real.
    """
    calls = {"list": 0, "receive": 0, "created": []}
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
        group = SignalGroup(id=created_id, name=name)
        groups.append(group)
        return group

    monkeypatch.setattr(run, "list_groups", fake_list_groups)
    monkeypatch.setattr(run, "receive", fake_receive)
    monkeypatch.setattr(run, "create_group", fake_create_group)
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
    cfg = FeedConfig(url="https://a", type="regular", name="Blog")
    items = [
        FeedItem(title="One", description="d1", link="https://a/1"),
        FeedItem(title="Two", description="d2", link="https://a/2"),
    ]
    _patch_feeds(monkeypatch, [cfg], {"https://a": items})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", now=NOW)

    assert count == 2
    assert sends[0]["text"] == "One\n\nd1\n\nhttps://a/1"
    assert all(s["voice_note"] is False for s in sends)


def test_run_podcast_downloads_and_sends_voice_note(monkeypatch):
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")
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

    count = run_feeds("feeds.json", now=NOW)

    assert count == 1
    assert sends[0]["voice_note"] is True
    assert sends[0]["attachments"] == ["/tmp/fake-ep.mp3"]
    assert sends[0]["recipient"] == "group:Pod="
    assert sends[0]["text"] == "Ep\n\nnotes"


def test_run_podcast_without_enclosure_falls_back_to_text(monkeypatch):
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")
    item = FeedItem(title="Ep", description="notes", enclosure_url=None)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", now=NOW)

    assert count == 1
    assert sends[0]["voice_note"] is False
    assert sends[0]["attachments"] is None


def test_run_uses_message_template(monkeypatch):
    cfg = FeedConfig(
        url="https://a",
        type="regular",
        name="Blog",
        message_template="📰 {title} [{feed_name}]\n\n{link}",
    )
    item = FeedItem(title="One", description="d1", link="https://a/1", feed_name="Blog")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json", now=NOW)

    assert sends[0]["text"] == "📰 One [Blog]\n\nhttps://a/1"


def test_run_applies_field_filters(monkeypatch):
    cfg = FeedConfig(
        url="https://a",
        type="regular",
        name="Blog",
        filters=(FeedFilter("title", "excludes", ("sponsored",)),),
    )
    items = [
        FeedItem(title="Real post", description="d1"),
        FeedItem(title="Sponsored post", description="d2"),
    ]
    _patch_feeds(monkeypatch, [cfg], {"https://a": items})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", now=NOW)

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
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    sends = _capture_sends(monkeypatch)
    downloaded = _fake_downloads(monkeypatch)

    count = run_feeds("feeds.json", now=NOW)

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
    # The text already went out with the card; repeating it would be noise.
    assert audio["text"] == ""


def test_run_podcast_without_a_card_stays_one_message(monkeypatch):
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod", link_preview=False)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    sends = _capture_sends(monkeypatch)
    _fake_downloads(monkeypatch)

    run_feeds("feeds.json", now=NOW)

    assert len(sends) == 1
    assert sends[0]["voice_note"] is True
    assert sends[0]["text"].startswith("Ep")


def test_run_sends_without_artwork_when_its_download_fails(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    sends = _capture_sends(monkeypatch)
    _fake_downloads(monkeypatch, fail_on="https://a/ep.jpg")

    count = run_feeds("feeds.json", now=NOW)

    # The episode still goes out; only the card's image is lost.
    assert count == 1
    assert sends[0]["preview"].image == ""
    assert sends[0]["preview"].title == "Ep"
    assert sends[1]["attachments"] == ["/tmp/fake-ep.mp3"]
    assert "preview image skipped" in capsys.readouterr().err


def test_run_regular_feed_sends_no_preview(monkeypatch):
    cfg = FeedConfig(url="https://a", type="regular", name="Blog")
    item = FeedItem(title="One", description="d1", link="https://a/1")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json", now=NOW)

    assert sends[0]["preview"] is None


def test_run_dry_run_describes_preview_without_downloading(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    _capture_sends(monkeypatch)

    def fail(url, **kwargs):
        raise AssertionError("dry run must not download")

    monkeypatch.setattr(run, "download_temp", fail)

    run_feeds("feeds.json", dry_run=True, now=NOW)

    out = capsys.readouterr().out
    assert "preview: Ep <https://a/ep.mp3>" in out
    assert "preview image: https://a/ep.jpg" in out


def test_run_dry_run_sends_nothing(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", type="regular", name="Blog")
    item = FeedItem(title="One", description="d1", link="https://a/1")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", dry_run=True, now=NOW)

    assert count == 1
    assert sends == []
    assert "Blog" in capsys.readouterr().out


# --- one group per feed ----------------------------------------------------

_POST = FeedItem(title="One", description="d1", link="https://a/1")


def _blog(name="Blog", **kwargs):
    return FeedConfig(url="https://a", type="regular", name=name, **kwargs)


def test_run_sends_to_the_group_named_after_the_feed(monkeypatch):
    existing = SignalGroup(id="book=", name="Blog")
    calls = _patch_feeds(
        monkeypatch, [_blog()], {"https://a": [_POST]}, groups=[existing]
    )
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json", now=NOW)

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

    run_feeds("feeds.json", now=NOW)

    assert sends[0]["recipient"] == "group:book="
    assert calls["created"] == []


def test_run_creates_the_group_when_it_is_missing(monkeypatch, capsys):
    calls = _patch_feeds(
        monkeypatch, [_blog()], {"https://a": [_POST]}, groups=[]
    )
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json", now=NOW)

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

    run_feeds("feeds.json", now=NOW)

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

    run_feeds("feeds.json", now=NOW)

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

    run_feeds("feeds.json", now=NOW)

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

    run_feeds("feeds.json", now=NOW)

    assert calls["receive"] == 1


def test_run_lists_groups_once_for_several_feeds(monkeypatch):
    feeds = [_blog("Blog"), FeedConfig(url="https://b", type="regular", name="Other")]
    calls = _patch_feeds(
        monkeypatch,
        feeds,
        {"https://a": [_POST], "https://b": [_POST]},
        groups=[SignalGroup(id="a=", name="Blog"), SignalGroup(id="b=", name="Other")],
    )
    _capture_sends(monkeypatch)

    run_feeds("feeds.json", now=NOW)

    # Every signal-cli call pays a JVM start; N feeds must not cost N listings,
    # and one refresh covers the whole run.
    assert calls["list"] == 1
    assert calls["receive"] == 1


def test_run_creates_no_group_for_a_feed_with_nothing_to_send(monkeypatch):
    cfg = _blog(filters=(FeedFilter("title", "excludes", ("One",)),))
    calls = _patch_feeds(monkeypatch, [cfg], {"https://a": [_POST]}, groups=[])
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", now=NOW)

    assert count == 0
    assert sends == []
    # An unused feed shouldn't leave an empty group behind.
    assert calls["created"] == []


def test_run_dry_run_creates_nothing_and_says_so(monkeypatch, capsys):
    calls = _patch_feeds(
        monkeypatch, [_blog()], {"https://a": [_POST]}, groups=[]
    )
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json", dry_run=True, now=NOW)

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

    run_feeds("feeds.json", dry_run=True, now=NOW)

    out = capsys.readouterr().out
    assert "group:book=" in out
    assert "would be created" not in out


def test_run_to_overrides_every_group(monkeypatch):
    calls = _patch_feeds(
        monkeypatch, [_blog()], {"https://a": [_POST]}, groups=[]
    )
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json", to="+31611111111", now=NOW)

    assert sends[0]["recipient"] == "+31611111111"
    # A test send must not touch groups at all — not even to look.
    assert calls == {"list": 0, "receive": 0, "created": []}


def test_run_refuses_to_guess_between_two_groups_with_one_name(monkeypatch):
    _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [_POST]},
        groups=[SignalGroup(id="a=", name="Blog"), SignalGroup(id="b=", name="Blog")],
    )
    sends = _capture_sends(monkeypatch)

    with pytest.raises(SignalError, match="2 groups are called"):
        run_feeds("feeds.json", now=NOW)
    assert sends == []


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

    run_feeds("feeds.json", now=NOW)

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

    run_feeds("feeds.json", now=NOW)

    assert calls["created"][0]["description"] == "Everything worth reading, daily."


def test_run_creates_the_group_without_a_description_when_the_feed_has_none(
    monkeypatch,
):
    calls = _patch_feeds(monkeypatch, [_blog()], {"https://a": [_POST]}, groups=[])
    _capture_sends(monkeypatch)

    run_feeds("feeds.json", now=NOW)

    assert calls["created"][0]["description"] is None
