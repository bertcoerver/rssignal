"""Tests for rssignal.library: reading what fetchit filed, and nothing else."""

import json
from datetime import datetime, timezone

import pytest

from rssignal.feeds import FeedConfig, FeedError, SourceMissing
from rssignal.library import feed_image, library_root, read_feed

STEM = "Karambolage - S2026E101801 - Les feuilles Séyès"


def _feed(root, kind, name, folder=None, **meta):
    path = root / kind / (folder or name)
    path.mkdir(parents=True, exist_ok=True)
    (path / "feed.json").write_text(json.dumps({"format": 1, "name": name, **meta}))
    return path


def _record(folder, stem, **record):
    data = {"format": 1, "id": stem, "title": stem, "kind": "text", **record}
    (folder / f"{stem}.json").write_text(json.dumps(data, ensure_ascii=False))


def test_library_root_comes_from_the_environment(own_library):
    assert library_root() == str(own_library)


def test_a_video_is_read_with_its_file_picture_and_fields(own_library):
    show = _feed(own_library, "shows", "Karambolage", description="Mots, rites.", image="poster.jpg")
    (show / "poster.jpg").write_bytes(b"jpg")
    season = show / "Season 2026"
    season.mkdir()
    (season / f"{STEM}.mp4").write_bytes(b"video")
    (season / f"{STEM}-thumb.jpg").write_bytes(b"jpg")
    _record(
        season,
        STEM,
        id="arte:fr:133252-006-A",
        kind="video",
        title="Les feuilles Séyès",
        description="Chloé Bion donne des cours.",
        published="2026-10-18T03:00:00+00:00",
        link="https://www.arte.tv/fr/videos/133252-006-A/karambolage/",
        media=f"{STEM}.mp4",
        image=f"{STEM}-thumb.jpg",
        season=2026,
        episode=101801,
        duration=691,
        fields={"title": "x", "image_url": "https://img/1.jpg", "program_id": "133252-006-A"},
    )

    parsed = read_feed(FeedConfig(name="Karambolage"))

    assert parsed.description == "Mots, rites."
    assert parsed.image == str(show / "poster.jpg")
    (item,) = parsed.items
    assert item.kind == "video"
    assert item.title == "Les feuilles Séyès"
    assert item.media == str(season / f"{STEM}.mp4")
    assert item.image == str(season / f"{STEM}-thumb.jpg")
    assert item.published == datetime(2026, 10, 18, 3, tzinfo=timezone.utc)
    assert item.image_url == "https://img/1.jpg"
    assert item.feed_name == "Karambolage"
    assert item.extra["program_id"] == "133252-006-A"
    assert item.extra["season"] == "2026" and item.extra["episode"] == "101801"
    assert item.extra["duration_seconds"] == "691"
    assert item.extra["library_id"] == "arte:fr:133252-006-A"
    # Core fields come from the record, not from the copy in "fields".
    assert "title" not in item.extra


def test_items_of_every_kind_are_read_together_oldest_first(own_library):
    _record(_feed(own_library, "articles", "Mixed"), "note", published="2026-10-02T00:00:00+00:00")
    pod = _feed(own_library, "podcasts", "Mixed")
    (pod / "ep.mp3").write_bytes(b"mp3")
    _record(pod, "ep", kind="audio", media="ep.mp3", published="2026-10-01T00:00:00+00:00")

    items = read_feed(FeedConfig(name="Mixed")).items
    assert [(i.title, i.kind) for i in items] == [("ep", "audio"), ("note", "text")]


def test_an_item_whose_file_is_missing_is_not_read(own_library):
    pod = _feed(own_library, "podcasts", "Pod")
    _record(pod, "gone", kind="audio", media="gone.mp3")
    assert read_feed(FeedConfig(name="Pod")).items == []


def test_a_feed_is_found_by_the_name_in_its_feed_json(own_library):
    """fetchit makes the folder name safe; the feed's own name is what counts."""
    folder = _feed(own_library, "shows", "A Closer Look: Seth", folder="A Closer Look - Seth")
    _record(folder, "one")
    items = read_feed(FeedConfig(name="Seth", source="a closer look: seth")).items
    assert [i.title for i in items] == ["one"]
    assert items[0].feed_name == "Seth"


def test_a_feed_with_nothing_in_the_library_is_missing_not_broken(own_library):
    _feed(own_library, "shows", "Other")
    with pytest.raises(SourceMissing, match="nothing for 'Pod' yet"):
        read_feed(FeedConfig(name="Pod"))


def test_no_library_at_all_says_where_it_looked(own_library):
    with pytest.raises(SourceMissing, match="there is no library at"):
        read_feed(FeedConfig(name="Pod"))


def test_a_record_from_a_newer_fetchit_is_refused(own_library):
    _record(_feed(own_library, "articles", "Blog"), "post", format=2)
    with pytest.raises(FeedError, match="layout version 2"):
        read_feed(FeedConfig(name="Blog"))


def test_feed_image_is_the_artwork_feed_json_names(own_library):
    folder = _feed(own_library, "podcasts", "Pod", image="cover.jpg")
    assert feed_image(FeedConfig(name="Pod")) is None
    (folder / "cover.jpg").write_bytes(b"jpg")
    assert feed_image(FeedConfig(name="Pod")) == str(folder / "cover.jpg")
