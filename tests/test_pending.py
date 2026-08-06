"""Tests for the note of a two-message item's unsent half.

This is the one piece of local state that decides what gets sent, so the tests
lean on the direction it is allowed to be wrong in: forgetting costs a duplicate
card, and remembering something it shouldn't costs an episode its card. Nothing
in here may raise, whatever the file on disk turns out to be.
"""

from __future__ import annotations

import json
import os

from rssignal import pending
from rssignal.feeds import FeedItem

EPISODE = FeedItem(
    title="Ep",
    description="notes",
    link="https://a/ep.mp3",
    enclosure_url="https://a/ep.mp3",
)


def test_nothing_is_owed_to_begin_with():
    assert pending.owed(pending.item_key("group:G=", EPISODE)) is False


def test_what_was_remembered_is_owed():
    key = pending.item_key("group:G=", EPISODE)
    pending.remember(key)

    assert pending.owed(key) is True


def test_it_is_on_disk_the_moment_it_is_remembered():
    # The whole point: the run that owes the voice note may not be the run that
    # sends it, and may not get to exit cleanly either. Nothing is buffered to
    # the end of the run the way the cache is.
    key = pending.item_key("group:G=", EPISODE)
    pending.remember(key)

    assert list(_all()) == [key]


def test_forgetting_clears_the_debt():
    key = pending.item_key("group:G=", EPISODE)
    pending.remember(key)
    pending.forget(key)

    assert pending.owed(key) is False


def test_forgetting_what_was_never_owed_writes_nothing():
    # The common case by far — every fully-sent episode goes through here.
    pending.forget(pending.item_key("group:G=", EPISODE))

    assert not os.path.exists(pending.path())


def test_an_expired_debt_is_written_off(monkeypatch):
    key = pending.item_key("group:G=", EPISODE)
    pending.remember(key)

    real = pending.time.time()
    monkeypatch.setattr(pending.time, "time", lambda: real + pending.TTL + 1)

    assert pending.owed(key) is False


def test_an_expired_debt_is_dropped_from_the_file(monkeypatch):
    stale = pending.item_key("group:G=", EPISODE)
    pending.remember(stale)

    real = pending.time.time()
    monkeypatch.setattr(pending.time, "time", lambda: real + pending.TTL + 1)
    fresh = pending.item_key("group:H=", EPISODE)
    pending.remember(fresh)

    assert list(_all()) == [fresh]


def _all():
    with open(pending.path(), encoding="utf-8") as fh:
        return json.load(fh)


def test_a_corrupt_note_owes_nothing(tmp_path, monkeypatch):
    # A half-written file must read as "nothing owed" — a duplicate card — and
    # never as an exception in the middle of a send.
    path = tmp_path / "pending.json"
    path.write_text("{not json at all")
    monkeypatch.setenv(pending.PENDING_ENV, str(path))

    assert pending.owed(pending.item_key("group:G=", EPISODE)) is False


def test_a_note_that_cannot_be_written_is_not_an_error(tmp_path, monkeypatch):
    blocked = tmp_path / "in-the-way"
    blocked.write_text("not a directory")
    monkeypatch.setenv(pending.PENDING_ENV, str(blocked / "pending.json"))

    pending.remember(pending.item_key("group:G=", EPISODE))  # Must not raise.


def test_the_key_tells_two_items_apart():
    other = FeedItem(title="Ep 2", description="", link="https://a/ep2.mp3")

    assert pending.item_key("group:G=", EPISODE) != pending.item_key(
        "group:G=", other
    )


def test_the_key_tells_two_recipients_apart():
    # The same item can legitimately be owed to one group and not another.
    assert pending.item_key("group:G=", EPISODE) != pending.item_key(
        "group:H=", EPISODE
    )


def test_the_key_prefers_the_feeds_own_id():
    # A link that changes — a site moving to https, a tracking parameter added —
    # must not turn a half-sent item into a new one.
    with_id = FeedItem(
        title="Ep",
        description="",
        link="https://a/ep.mp3",
        extra={"id": "guid-1"},
    )
    moved = FeedItem(
        title="Ep",
        description="",
        link="https://a/ep.mp3?utm=x",
        extra={"id": "guid-1"},
    )

    assert pending.item_key("group:G=", with_id) == pending.item_key(
        "group:G=", moved
    )
