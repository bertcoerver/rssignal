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
    FieldExtract,
    filter_recent,
    format_message,
    item_fields,
    load_feeds,
    parse_feed,
    preview_fields,
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
                {"name": "Pod", "url": "https://b/rss", "type": "podcast", "max_age_days": 2},
            ]
        },
    )

    configs = load_feeds(path)

    assert configs[0] == FeedConfig(
        url="https://a/rss", type="regular", name="Blog", max_age=timedelta(hours=12)
    )
    assert configs[1].type == "podcast"
    assert configs[1].name == "Pod"
    assert configs[1].max_age == timedelta(days=2)


def test_load_feeds_max_age_combines_hours_and_days(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "url": "https://a", "type": "regular", "max_age_days": 1, "max_age_hours": 6}]},
    )
    assert load_feeds(path)[0].max_age == timedelta(days=1, hours=6)


def test_load_feeds_no_max_age_is_none(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"name": "F", "url": "https://a", "type": "regular"}]})
    assert load_feeds(path)[0].max_age is None


def test_load_feeds_missing_file_raises():
    with pytest.raises(FeedError):
        load_feeds("/nonexistent/feeds.json")


def test_load_feeds_bad_type_raises(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"name": "F", "url": "https://a", "type": "newsletter"}]})
    with pytest.raises(FeedError):
        load_feeds(path)


def test_load_feeds_missing_url_raises(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"name": "F", "type": "regular"}]})
    with pytest.raises(FeedError):
        load_feeds(path)


def test_load_feeds_not_an_object_raises(tmp_path):
    path = _write_config(tmp_path, [{"name": "F", "url": "https://a", "type": "regular"}])
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

    items = parse_feed(FeedConfig(url="https://a", type="regular")).items

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

    item = parse_feed(FeedConfig(url="https://a", type="podcast")).items[0]

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


def test_format_message_can_drop_the_title():
    item = FeedItem(title="Ep", description="Notes", link="https://a/1")
    assert format_message(item, "podcast", include_title=False) == "Notes"
    assert (
        format_message(item, "regular", include_title=False) == "Notes\n\nhttps://a/1"
    )


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

    item = parse_feed(FeedConfig(url="https://a", type="podcast", name="Pod")).items[0]

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
                    "name": "F",
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
                    "name": "F",
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
    path = _write_config(tmp_path, {"feeds": [{"name": "F", "url": "https://a", "type": "regular"}]})
    assert load_feeds(path)[0].filters == ()


def test_load_feeds_unknown_key_raises(tmp_path):
    # A typo like max_age_hour must not be silently ignored.
    path = _write_config(
        tmp_path, {"feeds": [{"name": "F", "url": "https://a", "type": "regular", "max_age_hour": 5}]}
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert "max_age_hour" in str(excinfo.value)


def test_load_feeds_bad_regex_raises(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "url": "https://a", "type": "regular", "title_matches": "([unclosed"}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)


def test_load_feeds_filter_value_must_be_string_or_list(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "url": "https://a", "type": "regular", "title_contains": 42}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)


# --- artwork ---------------------------------------------------------------


def _parsed(entries, feed=None):
    """A stand-in for a feedparser result."""
    fake = type("Parsed", (), {})()
    fake.bozo = False
    fake.entries = entries
    fake.feed = feed or {}
    return fake


def _parse_one(monkeypatch, entry, feed=None, feed_type="podcast"):
    monkeypatch.setattr(
        feeds.feedparser, "parse", lambda url: _parsed([entry], feed)
    )
    return parse_feed(FeedConfig(url="https://a", type=feed_type)).items[0]


def test_parse_feed_takes_episode_itunes_image(monkeypatch):
    item = _parse_one(
        monkeypatch,
        {"title": "Ep", "image": {"href": "https://a/ep.jpg"}},
        feed={"image": {"href": "https://a/show.jpg"}},
    )
    # The episode's own artwork wins over the channel's.
    assert item.image_url == "https://a/ep.jpg"


def test_parse_feed_takes_media_thumbnail(monkeypatch):
    item = _parse_one(
        monkeypatch,
        {"title": "Ep", "media_thumbnail": [{"url": "https://a/thumb.jpg"}]},
    )
    assert item.image_url == "https://a/thumb.jpg"


def test_parse_feed_ignores_channel_artwork(monkeypatch):
    # Most podcasts set artwork once on the show. That logo says nothing about
    # the episode, so an item without its own image gets none.
    item = _parse_one(
        monkeypatch, {"title": "Ep"}, feed={"image": {"href": "https://a/show.jpg"}}
    )
    assert item.image_url is None


def test_parse_feed_without_any_image(monkeypatch):
    assert _parse_one(monkeypatch, {"title": "Ep"}).image_url is None


def test_image_keys_do_not_leak_into_extra(monkeypatch):
    item = _parse_one(
        monkeypatch,
        {"title": "Ep", "image": {"href": "https://a/ep.jpg"}, "id": "abc"},
    )
    assert "image" not in item.extra
    assert item.extra["id"] == "abc"


def test_item_fields_includes_image_url():
    item = FeedItem(title="Ep", description="d", image_url="https://a/ep.jpg")
    assert item_fields(item)["image_url"] == "https://a/ep.jpg"


# --- link previews ---------------------------------------------------------

_EPISODE = FeedItem(
    title="Episode 402",
    description="A long look at something interesting.",
    link="https://a/402.mp3",
    image_url="https://a/402.jpg",
    feed_name="Pod",
)


def test_preview_fields_defaults_for_a_podcast():
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")

    # No description by default: the message body sits right under the card.
    assert preview_fields(_EPISODE, cfg) == {
        "url": "https://a/402.mp3",
        "title": "Episode 402",
        "description": "",
        "image_url": "https://a/402.jpg",
    }


def test_preview_fields_is_none_for_a_regular_feed():
    cfg = FeedConfig(url="https://a", type="regular")
    assert preview_fields(_EPISODE, cfg) is None


def test_preview_fields_honours_link_preview_toggle():
    off = FeedConfig(url="https://a", type="podcast", link_preview=False)
    on = FeedConfig(url="https://a", type="regular", link_preview=True)

    assert preview_fields(_EPISODE, off) is None
    assert preview_fields(_EPISODE, on) is not None


def test_preview_fields_uses_templates():
    cfg = FeedConfig(
        url="https://a",
        type="podcast",
        preview_url="https://pod.example/{feed_name}",
        preview_title="🎧 {title}",
        preview_description="from {feed_name}",
    )

    card = preview_fields(_EPISODE, cfg)

    assert card["url"] == "https://pod.example/Pod"
    assert card["title"] == "🎧 Episode 402"
    assert card["description"] == "from Pod"


def test_preview_fields_is_none_without_a_url():
    # An item with no link can't anchor a preview card.
    item = FeedItem(title="Ep", description="d")
    cfg = FeedConfig(url="https://a", type="podcast")
    assert preview_fields(item, cfg) is None


def test_preview_fields_is_none_without_a_title():
    cfg = FeedConfig(url="https://a", type="podcast", preview_title="{nonexistent}")
    assert preview_fields(_EPISODE, cfg) is None


def test_preview_description_is_opt_in():
    item = FeedItem(title="Ep", description="word " * 100, link="https://a/1")
    cfg = FeedConfig(url="https://a", type="podcast")

    assert preview_fields(item, cfg)["description"] == ""


def test_render_message_appends_the_preview_url():
    # The built-in podcast layout has no link, but signal-cli needs the preview
    # url to appear in the body.
    cfg = FeedConfig(url="https://a", type="podcast")

    text = render_message(_EPISODE, cfg)

    assert text.endswith("\n\nhttps://a/402.mp3")


def test_render_message_drops_the_title_carried_by_the_card():
    # The card already shows "Episode 402"; repeating it directly underneath
    # would just be the same line twice.
    cfg = FeedConfig(url="https://a", type="podcast")

    text = render_message(_EPISODE, cfg)

    assert text == "A long look at something interesting.\n\nhttps://a/402.mp3"


def test_render_message_keeps_the_title_when_a_template_asks_for_it():
    cfg = FeedConfig(
        url="https://a", type="podcast", message_template="🎧 {title}\n\n{description}"
    )

    assert render_message(_EPISODE, cfg).startswith("🎧 Episode 402")


def test_render_message_does_not_duplicate_an_existing_url():
    cfg = FeedConfig(
        url="https://a", type="podcast", message_template="{title}\n{link}"
    )

    text = render_message(_EPISODE, cfg)

    assert text.count("https://a/402.mp3") == 1


def test_render_message_leaves_body_alone_without_a_preview():
    cfg = FeedConfig(url="https://a", type="podcast", link_preview=False)
    assert render_message(_EPISODE, cfg) == format_message(_EPISODE, "podcast")


# --- preview configuration -------------------------------------------------


def test_load_feeds_parses_preview_keys(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {
                    "name": "F",
                    "url": "https://a",
                    "type": "podcast",
                    "link_preview": False,
                    "preview_url": "https://pod.example",
                    "preview_title": "🎧 {title}",
                    "preview_description": "{feed_name}",
                }
            ]
        },
    )

    cfg = load_feeds(path)[0]

    assert cfg.link_preview is False
    assert cfg.previews_enabled is False
    assert cfg.preview_url == "https://pod.example"
    assert cfg.preview_title == "🎧 {title}"
    assert cfg.preview_description == "{feed_name}"


def test_load_feeds_preview_defaults_are_none(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"name": "F", "url": "https://a", "type": "podcast"}]})

    cfg = load_feeds(path)[0]

    assert cfg.link_preview is None
    # Unset means "podcast yes, regular no".
    assert cfg.previews_enabled is True


def test_load_feeds_link_preview_must_be_boolean(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "url": "https://a", "type": "podcast", "link_preview": "yes"}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)


def test_load_feeds_unknown_preview_key_raises(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "url": "https://a", "type": "podcast", "preview_image": "x"}]},
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert "preview_image" in str(excinfo.value)


# --- field extraction ------------------------------------------------------

# Podcast feeds routinely bury the only per-episode id inside the media url.
_MP3 = "https://pod.example/file/show/54321/an-episode.mp3?token=abc"


def _extracted(monkeypatch, pattern):
    """Parse a one-item feed through a single ``episode_id`` rule."""
    monkeypatch.setattr(
        feeds.feedparser, "parse", lambda url: _parsed([{"title": "Ep", "link": _MP3}])
    )
    cfg = FeedConfig(
        url="https://a",
        type="podcast",
        extract=(FieldExtract(name="episode_id", source="link", pattern=pattern),),
    )
    return parse_feed(cfg).items[0]


def test_extract_takes_the_first_capture_group(monkeypatch):
    item = _extracted(monkeypatch, r"/file/[^/]+/(\d+)/")

    assert item.extra["episode_id"] == "54321"
    # And it is a field like any other, so templates and filters can see it.
    assert item_fields(item)["episode_id"] == "54321"


def test_extract_without_a_group_takes_the_whole_match(monkeypatch):
    assert _extracted(monkeypatch, r"\d+").extra["episode_id"] == "54321"


def test_extract_without_a_match_is_empty(monkeypatch):
    assert _extracted(monkeypatch, r"/episode/(\d+)/").extra["episode_id"] == ""


def test_extract_cannot_shadow_a_core_field():
    item = FeedItem(title="Real title", description="d", link=_MP3)
    rules = (FieldExtract(name="title", source="link", pattern=r"(\d+)"),)

    assert item_fields(feeds.apply_extracts(item, rules))["title"] == "Real title"


def test_extract_reads_the_item_not_other_extracts():
    # Rules all see the item as it came off the feed, so ordering can't matter.
    item = FeedItem(title="Ep", description="d", link=_MP3)
    rules = (
        FieldExtract(name="episode_id", source="link", pattern=r"/(\d+)/"),
        FieldExtract(name="derived", source="episode_id", pattern=r"\d"),
    )

    result = feeds.apply_extracts(item, rules)

    assert result.extra["episode_id"] == "54321"
    assert result.extra["derived"] == ""


def test_extracted_field_feeds_templates_and_previews():
    item = FeedItem(title="Ep", description="Notes", link=_MP3)
    cfg = FeedConfig(
        url="https://a",
        type="podcast",
        extract=(FieldExtract(name="episode_id", source="link", pattern=r"/(\d+)/"),),
        preview_url="https://pod.example/listen/{episode_id}",
        message_template="{description} — #{episode_id}",
    )
    item = feeds.apply_extracts(item, cfg.extract)

    assert preview_fields(item, cfg)["url"] == "https://pod.example/listen/54321"
    assert render_message(item, cfg).startswith("Notes — #54321")


def test_extracted_field_can_be_filtered_on():
    item = FeedItem(title="Ep", description="d", link=_MP3)
    rules = (FieldExtract(name="episode_id", source="link", pattern=r"/(\d+)/"),)
    item = feeds.apply_extracts(item, rules)

    keep = (FeedFilter("episode_id", "matches", (r"^5",)),)
    drop = (FeedFilter("episode_id", "matches", (r"^9",)),)

    assert feeds.apply_filters([item], keep) == [item]
    assert feeds.apply_filters([item], drop) == []


def test_load_feeds_parses_extract(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {
                    "name": "F",
                    "url": "https://a",
                    "type": "podcast",
                    "extract": {
                        "episode_id": {"from": "link", "pattern": r"/(\d+)/"}
                    },
                }
            ]
        },
    )

    cfg = load_feeds(path)[0]

    assert cfg.extract == (
        FieldExtract(name="episode_id", source="link", pattern=r"/(\d+)/"),
    )


def test_load_feeds_extract_defaults_to_empty(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"name": "F", "url": "https://a", "type": "podcast"}]})
    assert load_feeds(path)[0].extract == ()


@pytest.mark.parametrize(
    "spec",
    [
        "link:/(\\d+)/",  # not an object
        {"pattern": "/(\\d+)/"},  # no source field
        {"from": "link"},  # no pattern
        {"from": "link", "pattern": ""},  # empty pattern
        {"from": "link", "pattern": "([0-9]+"},  # unbalanced parenthesis
        {"from": "link", "pattern": "/(\\d+)/", "flags": "i"},  # unknown key
    ],
)
def test_load_feeds_rejects_a_malformed_extract(tmp_path, spec):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {"name": "F", "url": "https://a", "type": "podcast", "extract": {"episode_id": spec}}
            ]
        },
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert "episode_id" in str(excinfo.value)


def test_load_feeds_extract_must_be_an_object(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "url": "https://a", "type": "podcast", "extract": ["episode_id"]}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)


# --- channel description ---------------------------------------------------


def test_parse_feed_takes_the_channel_description(monkeypatch):
    monkeypatch.setattr(
        feeds.feedparser,
        "parse",
        lambda url: _parsed([{"title": "Ep"}], {"summary": "<p>A daily &amp; show</p>"}),
    )
    parsed = parse_feed(FeedConfig(url="https://a", type="podcast"))
    # HTML is stripped: it becomes a Signal group description, not a web page.
    assert parsed.description == "A daily & show"


def test_parse_feed_falls_back_to_the_channel_subtitle(monkeypatch):
    monkeypatch.setattr(
        feeds.feedparser,
        "parse",
        lambda url: _parsed([{"title": "Ep"}], {"subtitle": "Short blurb"}),
    )
    assert parse_feed(FeedConfig(url="https://a", type="podcast")).description == (
        "Short blurb"
    )


def test_parse_feed_without_a_channel_description(monkeypatch):
    monkeypatch.setattr(
        feeds.feedparser, "parse", lambda url: _parsed([{"title": "Ep"}])
    )
    assert parse_feed(FeedConfig(url="https://a", type="podcast")).description == ""


def test_channel_description_does_not_leak_into_items(monkeypatch):
    # The show's blurb is not the episode's notes.
    monkeypatch.setattr(
        feeds.feedparser,
        "parse",
        lambda url: _parsed([{"title": "Ep"}], {"summary": "Show blurb"}),
    )
    assert parse_feed(FeedConfig(url="https://a", type="podcast")).items[0].description == ""
