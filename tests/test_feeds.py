"""Tests for rssignal.feeds: the config, and turning items into messages."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from rssignal.feeds import (
    FeedConfig,
    FeedError,
    FeedFilter,
    FeedItem,
    FieldExtract,
    apply_extracts,
    apply_filters,
    filter_since,
    format_message,
    is_audio_item,
    item_fields,
    load_feeds,
    newest,
    preview_fields,
    render_message,
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
                {"name": "Blog"},
                {"name": "Pod", "source": "Some Podcast"},
            ]
        },
    )

    configs = load_feeds(path)

    assert configs[0] == FeedConfig(name="Blog")
    assert configs[0].library_name == "Blog"
    assert configs[1] == FeedConfig(source="Some Podcast", name="Pod")
    assert configs[1].library_name == "Some Podcast"


def test_load_feeds_url_is_refused_with_where_it_went(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"name": "F", "url": "https://a/rss"}]})
    with pytest.raises(FeedError, match="fetchit.json"):
        load_feeds(path)


@pytest.mark.parametrize("key", ["since", "max_per_run", "quality", "keep_days"])
def test_load_feeds_download_settings_belong_in_fetchit(tmp_path, key):
    path = _write_config(tmp_path, {"feeds": [{"name": "F", key: 1}]})
    with pytest.raises(FeedError, match="belongs on this feed in fetchit.json"):
        load_feeds(path)


@pytest.mark.parametrize("key", ["max_age_hours", "max_age_days"])
def test_load_feeds_max_age_is_rejected_with_a_migration_hint(tmp_path, key):
    # The window is gone: how far a feed got now lives in its group description.
    path = _write_config(
        tmp_path, {"feeds": [{"name": "F", key: 12}]}
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert key in str(excinfo.value)
    assert "--since" in str(excinfo.value)


def test_load_feeds_missing_file_raises():
    with pytest.raises(FeedError):
        load_feeds("/nonexistent/feeds.json")


@pytest.mark.parametrize("value", ["podcast", "regular", "newsletter"])
def test_load_feeds_type_is_rejected_with_a_migration_hint(tmp_path, value):
    # The kind of feed is no longer declared: it is read off each item's enclosure.
    path = _write_config(
        tmp_path, {"feeds": [{"name": "F", "type": value}]}
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert '"type"' in str(excinfo.value)
    assert "link_preview" in str(excinfo.value)


def test_load_feeds_missing_name_raises(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"source": "F"}]})
    with pytest.raises(FeedError, match="name"):
        load_feeds(path)


def test_load_feeds_not_an_object_raises(tmp_path):
    path = _write_config(tmp_path, [{"name": "F"}])
    with pytest.raises(FeedError):
        load_feeds(path)


def _item(published):
    return FeedItem(title="t", description="d", published=published)


MARK = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def test_filter_since_none_keeps_every_dated_item():
    old = _item(MARK - timedelta(days=400))
    new = _item(MARK)
    assert filter_since([new, old], None) == [old, new]


def test_filter_since_drops_undated_items():
    # Whether an undated item is new can't be known, and guessing either way is
    # worse than skipping it.
    dated = _item(MARK)
    assert filter_since([dated, _item(None)], None) == [dated]


def test_filter_since_is_strict_at_the_watermark():
    # The watermark is the last item sent, so that item must not go again.
    at = _item(MARK)
    after = _item(MARK + timedelta(seconds=1))
    before = _item(MARK - timedelta(seconds=1))

    assert filter_since([after, at, before], MARK) == [after]


def test_filter_since_returns_items_oldest_first():
    first = _item(MARK)
    second = _item(MARK + timedelta(hours=1))
    third = _item(MARK + timedelta(hours=2))

    # Feeds list newest-first; sending in that order would strand older items
    # behind the watermark if a send failed part-way.
    assert filter_since([third, first, second], None) == [first, second, third]


def test_newest_picks_the_latest_item():
    latest = _item(MARK + timedelta(hours=1))
    assert newest([_item(MARK), latest, _item(MARK - timedelta(days=2))]) is latest


def test_newest_of_nothing_datable_is_none():
    assert newest([]) is None
    assert newest([_item(None)]) is None


def _audio(**kwargs):
    """An item carrying audio, for the branches keyed off is_audio_item."""
    fields = {
        "title": "Ep",
        "description": "Notes",
        "link": "https://a/1",
        "enclosure_url": "https://a/ep.mp3",
        "enclosure_type": "audio/mpeg",
    }
    return FeedItem(**{**fields, **kwargs})


def test_format_message_appends_the_link():
    item = FeedItem(title="Title", description="Body", link="https://a/1")
    assert format_message(item) == "Title\n\nBody\n\nhttps://a/1"


def test_format_message_omits_the_link_when_the_item_carries_audio():
    # The enclosure is attached, and the card links to it anyway.
    assert format_message(_audio()) == "Ep\n\nNotes"


def test_format_message_can_drop_the_title():
    assert format_message(_audio(), include_title=False) == "Notes"
    item = FeedItem(title="Ep", description="Notes", link="https://a/1")
    assert format_message(item, include_title=False) == "Notes\n\nhttps://a/1"


# --- detecting audio -------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "mime", "expected"),
    [
        ("https://a/ep.mp3", "audio/mpeg", True),
        ("https://a/ep.m4a", "AUDIO/MP4", True),
        # Feeds really do serve mp3s as octet-stream, so the URL gets a say.
        ("https://a/ep.mp3", "application/octet-stream", True),
        ("https://a/ep.mp3", None, True),
        # …and CDNs really do bolt tracking parameters onto every enclosure.
        ("https://a/ep.mp3?source=rss&x=1", None, True),
        ("https://a/ep.opus", "", True),
        # Declared as something else: an ordinary feed with an attachment.
        ("https://a/cover.jpg", "image/jpeg", False),
        ("https://a/talk.mp4", "video/mp4", False),
        ("https://a/paper.pdf", "application/pdf", False),
        ("https://a/post", None, False),
    ],
)
def test_is_audio_item(url, mime, expected):
    item = FeedItem(
        title="t", description="d", enclosure_url=url, enclosure_type=mime
    )
    assert is_audio_item(item) is expected


def test_is_audio_item_without_an_enclosure():
    assert is_audio_item(FeedItem(title="t", description="d")) is False


# --- item fields -----------------------------------------------------------


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
    cfg = FeedConfig(source="https://a")
    assert render_message(item, cfg) == format_message(item)


def test_render_message_with_template():
    item = FeedItem(
        title="Ep",
        description="Notes",
        published=datetime(2026, 7, 22, tzinfo=timezone.utc),
        extra={"itunes_duration": "00:42:11"},
    )
    cfg = FeedConfig(
        source="https://a",
        message_template="🎧 {title} ({published_date}) [{itunes_duration}]\n\n{description}",
    )
    assert render_message(item, cfg) == (
        "🎧 Ep (2026-07-22) [00:42:11]\n\nNotes"
    )


def test_render_message_unknown_field_renders_empty_without_gap():
    item = FeedItem(title="Ep", description="Notes")
    cfg = FeedConfig(
        source="https://a",
        message_template="{title}\n\n{nope}\n\n{description}",
    )
    # The missing field collapses instead of leaving a blank paragraph.
    assert render_message(item, cfg) == "Ep\n\nNotes"


def test_render_message_malformed_template_raises():
    cfg = FeedConfig(
        source="https://a", name="Blog", message_template="{title"
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


def test_load_feeds_parses_numeric_bounds(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {
                    "name": "F",
                    "duration_seconds_min": 420,
                    "duration_seconds_max": "30:00",
                }
            ]
        },
    )

    filters = load_feeds(path)[0].filters

    # JSON may write a bound as a number or as a duration; both keep their text.
    assert FeedFilter("duration_seconds", "min", ("420",)) in filters
    assert FeedFilter("duration_seconds", "max", ("30:00",)) in filters


@pytest.mark.parametrize("bound", ["half an hour", ["420", "1800"], True])
def test_load_feeds_bad_numeric_bound_raises(tmp_path, bound):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "duration_seconds_min": bound}]},
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert "duration_seconds_min" in str(excinfo.value)


def test_load_feeds_no_filters_is_empty_tuple(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"name": "F"}]})
    assert load_feeds(path)[0].filters == ()


def test_load_feeds_unknown_key_raises(tmp_path):
    # A typo like title_contain must not be silently ignored.
    path = _write_config(
        tmp_path, {"feeds": [{"name": "F", "title_contain": "x"}]}
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert "title_contain" in str(excinfo.value)


def test_load_feeds_bad_regex_raises(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "title_matches": "([unclosed"}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)


def test_load_feeds_filter_value_must_be_string_or_list(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "title_contains": 42}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)


# --- artwork ---------------------------------------------------------------


def test_item_fields_includes_image_url():
    item = FeedItem(title="Ep", description="d", image_url="https://a/ep.jpg")
    assert item_fields(item)["image_url"] == "https://a/ep.jpg"


# --- link previews ---------------------------------------------------------

_EPISODE = FeedItem(
    title="Episode 402",
    description="A long look at something interesting.",
    link="https://a/402.mp3",
    enclosure_url="https://a/402.mp3",
    enclosure_type="audio/mpeg",
    image_url="https://a/402.jpg",
    feed_name="Pod",
    # The item's own picture, as fetchit filed it: what the card shows.
    image="/library/podcasts/Pod/402.jpg",
)

# The same item without the audio: whether a card is sent is decided per item,
# so the two fixtures are what the default branches on.
_POST = FeedItem(
    title="A post",
    description="Words about something.",
    link="https://a/post",
    feed_name="Blog",
)


def test_preview_fields_defaults_on_for_an_item_with_audio():
    cfg = FeedConfig(source="https://a", name="Pod")

    # No description by default: the message body sits right under the card.
    assert preview_fields(_EPISODE, cfg) == {
        "url": "https://a/402.mp3",
        "title": "Episode 402",
        "description": "",
        "image": "/library/podcasts/Pod/402.jpg",
    }


def test_preview_fields_defaults_off_for_an_item_without_audio():
    cfg = FeedConfig(source="https://a")
    assert preview_fields(_POST, cfg) is None


def test_preview_fields_decides_per_item_not_per_feed():
    # One config, two items: the written note in a podcast feed gets no card.
    cfg = FeedConfig(source="https://a", name="Pod")
    assert preview_fields(_EPISODE, cfg) is not None
    assert preview_fields(_POST, cfg) is None


def test_preview_fields_honours_link_preview_toggle():
    off = FeedConfig(source="https://a", link_preview=False)
    on = FeedConfig(source="https://a", link_preview=True)

    assert preview_fields(_EPISODE, off) is None
    assert preview_fields(_POST, on) is not None


def test_preview_fields_uses_templates():
    cfg = FeedConfig(
        source="https://a",
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
    cfg = FeedConfig(source="https://a")
    assert preview_fields(item, cfg) is None


def test_preview_fields_is_none_without_a_title():
    cfg = FeedConfig(source="https://a", preview_title="{nonexistent}")
    assert preview_fields(_EPISODE, cfg) is None


def test_preview_description_is_opt_in():
    item = _audio(description="word " * 100)
    cfg = FeedConfig(source="https://a")

    assert preview_fields(item, cfg)["description"] == ""


def test_render_message_appends_the_preview_url():
    # The built-in podcast layout has no link, but signal-cli needs the preview
    # url to appear in the body.
    cfg = FeedConfig(source="https://a")

    text = render_message(_EPISODE, cfg)

    assert text.endswith("\n\nhttps://a/402.mp3")


def test_render_message_drops_the_title_carried_by_the_card():
    # The card already shows "Episode 402"; repeating it directly underneath
    # would just be the same line twice.
    cfg = FeedConfig(source="https://a")

    text = render_message(_EPISODE, cfg)

    assert text == "A long look at something interesting.\n\nhttps://a/402.mp3"


def test_render_message_keeps_the_title_when_a_template_asks_for_it():
    cfg = FeedConfig(
        source="https://a", message_template="🎧 {title}\n\n{description}"
    )

    assert render_message(_EPISODE, cfg).startswith("🎧 Episode 402")


def test_render_message_does_not_duplicate_an_existing_url():
    cfg = FeedConfig(
        source="https://a", message_template="{title}\n{link}"
    )

    text = render_message(_EPISODE, cfg)

    assert text.count("https://a/402.mp3") == 1


def test_render_message_leaves_body_alone_without_a_preview():
    cfg = FeedConfig(source="https://a", link_preview=False)
    assert render_message(_EPISODE, cfg) == format_message(_EPISODE)


# --- preview configuration -------------------------------------------------


def test_load_feeds_parses_preview_keys(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {
                    "name": "F",
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
    assert preview_fields(_EPISODE, cfg) is None
    assert cfg.preview_url == "https://pod.example"
    assert cfg.preview_title == "🎧 {title}"
    assert cfg.preview_description == "{feed_name}"


def test_load_feeds_preview_defaults_are_none(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"name": "F"}]})

    cfg = load_feeds(path)[0]

    assert cfg.link_preview is None
    # Unset means "audio yes, everything else no", decided per item.
    assert preview_fields(_EPISODE, cfg) is not None
    assert preview_fields(_POST, cfg) is None


def test_load_feeds_refresh_image_defaults_to_on_and_can_be_turned_off(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {"name": "F"},
                {"name": "G", "refresh_image": False},
            ]
        },
    )

    kept, pinned = load_feeds(path)

    assert kept.refresh_image is True
    assert pinned.refresh_image is False


def test_load_feeds_refresh_image_must_be_boolean(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "refresh_image": "no"}]},
    )
    with pytest.raises(FeedError, match="refresh_image must be true or false"):
        load_feeds(path)


def test_load_feeds_link_preview_must_be_boolean(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "link_preview": "yes"}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)


def test_load_feeds_unknown_preview_key_raises(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "preview_image": "x"}]},
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert "preview_image" in str(excinfo.value)


# --- field extraction ------------------------------------------------------

# Podcast feeds routinely bury the only per-episode id inside the media url.
_MP3 = "https://pod.example/file/show/54321/an-episode.mp3?token=abc"


def _extracted(monkeypatch, pattern):
    """Run a one-item feed through a single ``episode_id`` rule."""
    rule = FieldExtract(name="episode_id", source="link", pattern=pattern)
    return apply_extracts(FeedItem(title="Ep", description="", link=_MP3), (rule,))


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

    assert item_fields(apply_extracts(item, rules))["title"] == "Real title"


def test_extract_reads_the_item_not_other_extracts():
    # Rules all see the item as it came off the feed, so ordering can't matter.
    item = FeedItem(title="Ep", description="d", link=_MP3)
    rules = (
        FieldExtract(name="episode_id", source="link", pattern=r"/(\d+)/"),
        FieldExtract(name="derived", source="episode_id", pattern=r"\d"),
    )

    result = apply_extracts(item, rules)

    assert result.extra["episode_id"] == "54321"
    assert result.extra["derived"] == ""


def test_extracted_field_feeds_templates_and_previews():
    item = _audio(link=_MP3)
    cfg = FeedConfig(
        source="https://a",
        extract=(FieldExtract(name="episode_id", source="link", pattern=r"/(\d+)/"),),
        preview_url="https://pod.example/listen/{episode_id}",
        message_template="{description} — #{episode_id}",
    )
    item = apply_extracts(item, cfg.extract)

    assert preview_fields(item, cfg)["url"] == "https://pod.example/listen/54321"
    assert render_message(item, cfg).startswith("Notes — #54321")


def test_extracted_field_can_be_filtered_on():
    item = FeedItem(title="Ep", description="d", link=_MP3)
    rules = (FieldExtract(name="episode_id", source="link", pattern=r"/(\d+)/"),)
    item = apply_extracts(item, rules)

    keep = (FeedFilter("episode_id", "matches", (r"^5",)),)
    drop = (FeedFilter("episode_id", "matches", (r"^9",)),)

    assert apply_filters([item], keep) == [item]
    assert apply_filters([item], drop) == []


def test_load_feeds_parses_extract(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {
                    "name": "F",
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
    path = _write_config(tmp_path, {"feeds": [{"name": "F"}]})
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
                {"name": "F", "extract": {"episode_id": spec}}
            ]
        },
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert "episode_id" in str(excinfo.value)


def test_load_feeds_extract_must_be_an_object(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "extract": ["episode_id"]}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)
