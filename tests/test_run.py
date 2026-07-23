"""Tests for rssignal.run (feeds, sending, and downloads are monkeypatched)."""

import dataclasses
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from rssignal import run
from rssignal.feeds import FeedConfig, FeedError, FeedFilter, FeedItem, ParsedFeed
from rssignal.run import run_feeds
from rssignal.signal_cli import LinkPreview, SignalError, SignalGroup
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
    cfg = FeedConfig(url="https://a", type="regular", name="Blog")
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

    count = run_feeds("feeds.json")

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

    count = run_feeds("feeds.json")

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

    run_feeds("feeds.json")

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
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")
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
    # The text already went out with the card; repeating it would be noise.
    assert audio["text"] == ""


def test_run_podcast_without_a_card_stays_one_message(monkeypatch):
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod", link_preview=False)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    sends = _capture_sends(monkeypatch)
    _fake_downloads(monkeypatch)

    run_feeds("feeds.json")

    assert len(sends) == 1
    assert sends[0]["voice_note"] is True
    assert sends[0]["text"].startswith("Ep")


def test_run_sends_without_artwork_when_its_download_fails(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")
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
    cfg = FeedConfig(url="https://a", type="regular", name="Blog")
    item = FeedItem(title="One", description="d1", link="https://a/1")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json")

    assert sends[0]["preview"] is None


def test_run_dry_run_describes_preview_without_downloading(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")
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
    cfg = FeedConfig(url="https://a", type="regular", name="Blog")
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
    return FeedConfig(url="https://a", type="regular", name=name, **kwargs)


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
    feeds = [_blog("Blog"), FeedConfig(url="https://b", type="regular", name="Other")]
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


def test_run_refuses_to_guess_between_two_groups_with_one_name(monkeypatch):
    _patch_feeds(
        monkeypatch,
        [_blog()],
        {"https://a": [_POST]},
        groups=[SignalGroup(id="a=", name="Blog"), SignalGroup(id="b=", name="Blog")],
    )
    sends = _capture_sends(monkeypatch)

    with pytest.raises(SignalError, match="2 groups are called"):
        run_feeds("feeds.json")
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

    with pytest.raises(SignalError):
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

    with pytest.raises(SignalError):
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

    with pytest.raises(SignalError, match="no route to host"):
        run_feeds("feeds.json")

    # The send failure is what propagates; the rollback failure is reported.
    err = capsys.readouterr().err
    assert "undoing its watermark failed" in err
    assert "--since" in err


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
