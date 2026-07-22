"""Tests for rssignal.feeds (feedparser is monkeypatched — no network)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from rssignal import feeds
from rssignal.feeds import (
    FeedConfig,
    FeedError,
    FeedItem,
    filter_recent,
    format_message,
    load_feeds,
    parse_feed,
    strip_html,
)


def _write_config(tmp_path, data):
    path = tmp_path / "feeds.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_load_feeds_valid(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {"name": "Blog", "url": "https://a/rss", "type": "regular", "max_age_hours": 12},
                {"url": "https://b/rss", "type": "podcast", "max_age_days": 2, "recipient": "+31600000000"},
            ]
        },
    )

    configs = load_feeds(path)

    assert configs[0] == FeedConfig(
        url="https://a/rss", type="regular", name="Blog", max_age=timedelta(hours=12)
    )
    assert configs[1].type == "podcast"
    assert configs[1].recipient == "+31600000000"
    assert configs[1].max_age == timedelta(days=2)


def test_load_feeds_max_age_combines_hours_and_days(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"url": "https://a", "type": "regular", "max_age_days": 1, "max_age_hours": 6}]},
    )
    assert load_feeds(path)[0].max_age == timedelta(days=1, hours=6)


def test_load_feeds_no_max_age_is_none(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"url": "https://a", "type": "regular"}]})
    assert load_feeds(path)[0].max_age is None


def test_load_feeds_missing_file_raises():
    with pytest.raises(FeedError):
        load_feeds("/nonexistent/feeds.json")


def test_load_feeds_bad_type_raises(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"url": "https://a", "type": "newsletter"}]})
    with pytest.raises(FeedError):
        load_feeds(path)


def test_load_feeds_missing_url_raises(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"type": "regular"}]})
    with pytest.raises(FeedError):
        load_feeds(path)


def test_load_feeds_not_an_object_raises(tmp_path):
    path = _write_config(tmp_path, [{"url": "https://a", "type": "regular"}])
    with pytest.raises(FeedError):
        load_feeds(path)


def test_strip_html():
    assert strip_html("<p>Hello <b>world</b> &amp; more</p>") == "Hello world & more"
    assert strip_html("") == ""


def test_parse_feed_regular(monkeypatch):
    fake = type("Parsed", (), {})()
    fake.bozo = False
    fake.entries = [
        {
            "title": "Post one",
            "summary": "<p>Body &amp; text</p>",
            "link": "https://a/1",
            "published_parsed": (2026, 7, 20, 10, 0, 0, 0, 0, 0),
        }
    ]
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: fake)

    items = parse_feed(FeedConfig(url="https://a", type="regular"))

    assert items == [
        FeedItem(
            title="Post one",
            description="Body & text",
            link="https://a/1",
            published=datetime(2026, 7, 20, 10, 0, 0, tzinfo=timezone.utc),
        )
    ]


def test_parse_feed_podcast_enclosure(monkeypatch):
    fake = type("Parsed", (), {})()
    fake.bozo = False
    fake.entries = [
        {
            "title": "Episode 1",
            "description": "Show notes",
            "published_parsed": (2026, 7, 21, 8, 0, 0, 0, 0, 0),
            "enclosures": [{"href": "https://a/ep1.mp3", "type": "audio/mpeg"}],
        }
    ]
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: fake)

    item = parse_feed(FeedConfig(url="https://a", type="podcast"))[0]

    assert item.enclosure_url == "https://a/ep1.mp3"
    assert item.enclosure_type == "audio/mpeg"


def test_parse_feed_bozo_no_entries_raises(monkeypatch):
    fake = type("Parsed", (), {})()
    fake.bozo = True
    fake.bozo_exception = "malformed"
    fake.entries = []
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: fake)

    with pytest.raises(FeedError):
        parse_feed(FeedConfig(url="https://a", type="regular"))


def _item(published):
    return FeedItem(title="t", description="d", published=published)


def test_filter_recent_no_max_age_passes_all():
    items = [_item(None), _item(datetime(2020, 1, 1, tzinfo=timezone.utc))]
    assert filter_recent(items, None) == items


def test_filter_recent_window():
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    inside = _item(now - timedelta(hours=1))
    outside = _item(now - timedelta(hours=30))
    undated = _item(None)

    result = filter_recent([inside, outside, undated], timedelta(hours=24), now=now)

    assert result == [inside]


def test_format_message_regular():
    item = FeedItem(title="Title", description="Body", link="https://a/1")
    assert format_message(item, "regular") == "Title\n\nBody\n\nhttps://a/1"


def test_format_message_podcast_omits_link():
    item = FeedItem(title="Ep", description="Notes", link="https://a/1")
    assert format_message(item, "podcast") == "Ep\n\nNotes"
