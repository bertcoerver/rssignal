"""Tests for rssignal.feeds (feedparser is monkeypatched — no network)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from rssignal import feeds
from rssignal.feeds import (
    FeedConfig,
    FeedError,
    FeedFilter,
    FeedItem,
    filter_recent,
    format_message,
    item_fields,
    load_feeds,
    parse_feed,
    render_message,
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


# --- item fields -----------------------------------------------------------


def test_parse_feed_lifts_author_categories_and_extras(monkeypatch):
    fake = type("Parsed", (), {})()
    fake.bozo = False
    fake.entries = [
        {
            "title": "Episode 1",
            "description": "Show notes",
            "author": "Example Media",
            "tags": [{"term": "news"}, {"term": "politics"}],
            "id": "urn:uuid:1234",
            "itunes_duration": "00:42:11",
            "itunes_episode": 402,
            "published_parsed": (2026, 7, 21, 8, 0, 0, 0, 0, 0),
            "title_detail": {"value": "Episode 1"},
        }
    ]
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url: fake)

    item = parse_feed(FeedConfig(url="https://a", type="podcast", name="Pod"))[0]

    assert item.author == "Example Media"
    assert item.categories == ("news", "politics")
    assert item.feed_name == "Pod"
    assert item.extra == {
        "id": "urn:uuid:1234",
        "itunes_duration": "00:42:11",
        "itunes_episode": "402",
    }
    # Structured mirrors feedparser adds carry no useful string form.
    assert "title_detail" not in item.extra


def test_item_fields_includes_core_and_extras():
    item = FeedItem(
        title="T",
        description="D",
        published=datetime(2026, 7, 22, 6, 0, 0, tzinfo=timezone.utc),
        categories=("news", "politics"),
        feed_name="Pod",
        extra={"itunes_duration": "00:42:11"},
    )

    fields = item_fields(item)

    assert fields["title"] == "T"
    assert fields["published"] == "2026-07-22T06:00:00+00:00"
    assert fields["published_date"] == "2026-07-22"
    assert fields["categories"] == "news, politics"
    assert fields["feed_name"] == "Pod"
    assert fields["itunes_duration"] == "00:42:11"
    # Absent values are empty strings, never None.
    assert fields["link"] == ""
    assert fields["author"] == ""


def test_item_fields_extra_cannot_shadow_a_core_field():
    item = FeedItem(title="Real", description="D", extra={"title": "Impostor"})
    assert item_fields(item)["title"] == "Real"


# --- message templates -----------------------------------------------------


def test_render_message_without_template_uses_default():
    item = FeedItem(title="Title", description="Body", link="https://a/1")
    cfg = FeedConfig(url="https://a", type="regular")
    assert render_message(item, cfg) == format_message(item, "regular")


def test_render_message_with_template():
    item = FeedItem(
        title="Ep",
        description="Notes",
        published=datetime(2026, 7, 22, tzinfo=timezone.utc),
        extra={"itunes_duration": "00:42:11"},
    )
    cfg = FeedConfig(
        url="https://a",
        type="podcast",
        message_template="🎧 {title} ({published_date}) [{itunes_duration}]\n\n{description}",
    )
    assert render_message(item, cfg) == (
        "🎧 Ep (2026-07-22) [00:42:11]\n\nNotes"
    )


def test_render_message_unknown_field_renders_empty_without_gap():
    item = FeedItem(title="Ep", description="Notes")
    cfg = FeedConfig(
        url="https://a",
        type="regular",
        message_template="{title}\n\n{nope}\n\n{description}",
    )
    # The missing field collapses instead of leaving a blank paragraph.
    assert render_message(item, cfg) == "Ep\n\nNotes"


def test_render_message_malformed_template_raises():
    cfg = FeedConfig(
        url="https://a", type="regular", name="Blog", message_template="{title"
    )
    with pytest.raises(FeedError) as excinfo:
        render_message(FeedItem(title="T", description="D"), cfg)
    assert "Blog" in str(excinfo.value)


# --- filter configuration --------------------------------------------------


def test_load_feeds_parses_message_template(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {
                    "url": "https://a",
                    "type": "regular",
                    "message_template": "{title} — {link}",
                }
            ]
        },
    )
    assert load_feeds(path)[0].message_template == "{title} — {link}"


def test_load_feeds_parses_filters(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {
                    "url": "https://a",
                    "type": "regular",
                    "title_contains": ["one", "two"],
                    "description_excludes": "rerun",
                    "title_matches": r"^Ep \d+",
                }
            ]
        },
    )

    filters = load_feeds(path)[0].filters

    assert FeedFilter("title", "contains", ("one", "two")) in filters
    # A bare string becomes a single-value filter.
    assert FeedFilter("description", "excludes", ("rerun",)) in filters
    assert FeedFilter("title", "matches", (r"^Ep \d+",)) in filters


def test_load_feeds_no_filters_is_empty_tuple(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"url": "https://a", "type": "regular"}]})
    assert load_feeds(path)[0].filters == ()


def test_load_feeds_unknown_key_raises(tmp_path):
    # A typo like max_age_hour must not be silently ignored.
    path = _write_config(
        tmp_path, {"feeds": [{"url": "https://a", "type": "regular", "max_age_hour": 5}]}
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert "max_age_hour" in str(excinfo.value)


def test_load_feeds_bad_regex_raises(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"url": "https://a", "type": "regular", "title_matches": "([unclosed"}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)


def test_load_feeds_filter_value_must_be_string_or_list(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"url": "https://a", "type": "regular", "title_contains": 42}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)
