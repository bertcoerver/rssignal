"""Tests for rssignal.archive (no network — everything here is filesystem)."""

import os
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from rssignal import archive
from rssignal.feeds import FeedItem


def _item(title="Episode One", published=datetime(2026, 3, 4, tzinfo=timezone.utc)):
    return FeedItem(title=title, description="", published=published)


@pytest.fixture
def media(tmp_path, monkeypatch):
    """Switch archiving on, into a directory of this test's own."""
    root = tmp_path / "media"
    monkeypatch.setenv(archive.MEDIA_DIR_ENV, str(root))
    return root


def _source(tmp_path, name="ep.mp3", data=b"audio"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


# --- switched off by default ------------------------------------------------


def test_archiving_is_off_without_the_env_var(tmp_path):
    assert archive.media_dir() is None
    assert archive.staging_dir() is None
    assert archive.prepare() is None
    assert archive.prune() == 0
    assert archive.describe(_item(), feed="Show") is None
    assert archive.keep(_source(tmp_path), feed="Show", item=_item()) is None


def test_keep_off_leaves_no_files_anywhere(tmp_path):
    before = sorted(p.name for p in tmp_path.iterdir())
    archive.keep(_source(tmp_path), feed="Show", item=_item())
    assert sorted(p.name for p in tmp_path.iterdir()) == before + ["ep.mp3"]


# --- keeping ----------------------------------------------------------------


def test_keep_links_rather_than_copies(media, tmp_path):
    src = _source(tmp_path)
    dest = archive.keep(src, feed="My Show", item=_item())

    assert dest is not None
    # The point of the whole design: one file, two names, no second write.
    assert os.stat(dest).st_nlink == 2
    assert os.stat(dest).st_ino == os.stat(src).st_ino


def test_archived_file_survives_the_temp_file_going_away(media, tmp_path):
    src = _source(tmp_path, data=b"the episode")
    dest = archive.keep(src, feed="My Show", item=_item())

    os.unlink(src)  # what download_temp's `finally` does

    assert os.path.exists(dest)
    with open(dest, "rb") as fh:
        assert fh.read() == b"the episode"


def test_keep_falls_back_to_copying_when_linking_fails(media, tmp_path, monkeypatch):
    def no_links(_src, _dst):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(archive.os, "link", no_links)

    src = _source(tmp_path, data=b"copied")
    dest = archive.keep(src, feed="My Show", item=_item())

    assert dest is not None
    assert os.stat(dest).st_nlink == 1
    with open(dest, "rb") as fh:
        assert fh.read() == b"copied"


def test_keep_lays_files_out_by_feed_and_date(media, tmp_path):
    dest = archive.keep(
        _source(tmp_path, "x.mp3"), feed="My Show!", item=_item("Episode One")
    )

    assert os.path.relpath(dest, media) == os.path.join(
        "my-show", "2026-03-04-episode-one.mp3"
    )


def test_keep_uses_todays_date_when_the_item_has_none(media, tmp_path):
    dest = archive.keep(
        _source(tmp_path), feed="Show", item=_item(published=None)
    )
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    assert os.path.basename(dest).startswith(today)


def test_keep_does_not_overwrite_a_same_named_file(media, tmp_path):
    first = archive.keep(_source(tmp_path, "a.mp3", b"one"), feed="S", item=_item())
    second = archive.keep(_source(tmp_path, "b.mp3", b"two"), feed="S", item=_item())

    assert first != second
    assert second.endswith("-2.mp3")
    with open(first, "rb") as fh:
        assert fh.read() == b"one"


def test_keep_swallows_errors_and_reports_none(media, tmp_path, monkeypatch, error_log):
    def broken(*_args, **_kwargs):
        raise OSError("disk is full")

    monkeypatch.setattr(archive.os, "makedirs", broken)

    assert archive.keep(_source(tmp_path), feed="S", item=_item()) is None
    # An archive that fails must leave evidence, since nothing else will notice.
    assert "disk is full" in error_log.read_text()


def test_keep_preserves_the_extension_it_was_given(media, tmp_path):
    dest = archive.keep(_source(tmp_path, "v.mp4"), feed="S", item=_item())
    assert dest.endswith(".mp4")


# --- an episode of a series -------------------------------------------------


def _episode(season="2", episode="28", **fields):
    """An item that knows its place in a series, as NPO's do."""
    extra = {"show_title": "Bureau Buitenland", "duration_seconds": "1540"}
    if season is not None:
        extra["season"] = season
    if episode is not None:
        extra["episode"] = episode
    return FeedItem(
        title=fields.pop("title", "26.000 sancties tegen Rusland"),
        description=fields.pop("description", "Hoe doen ze dat?"),
        published=datetime(2026, 9, 27, 19, 30, tzinfo=timezone.utc),
        author="VPRO",
        categories=("Informatief", "Nieuws/actualiteiten"),
        feed_name="Bureau Buitenland",
        extra=extra,
        **fields,
    )


@pytest.mark.parametrize(
    "season, episode, expected",
    [
        ("2", "28", (2, 28)),
        ("0", "3", (0, 3)),  # specials
        (None, "28", None),
        ("2", None, None),
        ("two", "28", None),
        ("2", "-1", None),
    ],
)
def test_numbered(season, episode, expected):
    assert archive.numbered(_episode(season, episode)) == expected


def test_keep_files_a_numbered_episode_as_part_of_a_series(media, tmp_path):
    dest = archive.keep(
        _source(tmp_path, "v.mp4"), feed="Bureau Buitenland", item=_episode()
    )

    # Spelled the way a media server looks for it, and nothing after the number:
    # this title, slugged on, would read as episodes 28 to 26.
    assert os.path.relpath(dest, media) == os.path.join(
        "bureau-buitenland", "Season 02", "bureau-buitenland-s02e28.mp4"
    )


def test_keep_pads_numbers_without_cutting_long_ones(media, tmp_path):
    dest = archive.keep(
        _source(tmp_path, "v.mp4"), feed="Show", item=_episode("12", "345")
    )
    assert os.path.relpath(dest, media) == os.path.join(
        "show", "Season 12", "show-s12e345.mp4"
    )


def test_keep_replaces_a_numbered_episode_sent_again(media, tmp_path):
    first = archive.keep(_source(tmp_path, "a.mp4", b"one"), feed="S", item=_episode())
    second = archive.keep(_source(tmp_path, "b.mp4", b"two"), feed="S", item=_episode())

    # One episode, one file: a `-2` would be a second episode to anything
    # reading the folder.
    assert first == second
    assert os.listdir(os.path.dirname(second)) == ["s-s02e28.mp4"]
    with open(second, "rb") as fh:
        assert fh.read() == b"two"


def test_describe_shows_the_series_layout(media):
    assert archive.describe(_episode(), feed="Bureau Buitenland") == str(
        media / "bureau-buitenland" / "Season 02" / "bureau-buitenland-s02e28.*"
    )


def _kept_episode(tmp_path, item=None, feed="Bureau Buitenland"):
    item = item or _episode()
    # A source of its own per episode: two links to one file share its date.
    season, episode = archive.numbered(item)
    source = _source(tmp_path, f"v{season}-{episode}.mp4", b"video")
    return item, archive.keep(source, feed=feed, item=item)


def test_annotate_writes_an_nfo_that_says_which_episode_it_is(media, tmp_path):
    item, dest = _kept_episode(tmp_path)
    archive.annotate(dest, item)

    root = ET.parse(dest.removesuffix(".mp4") + ".nfo").getroot()
    assert root.tag == "episodedetails"
    assert {child.tag: child.text for child in root if child.tag != "genre"} == {
        "title": "26.000 sancties tegen Rusland",
        "showtitle": "Bureau Buitenland",
        "season": "2",
        "episode": "28",
        "plot": "Hoe doen ze dat?",
        "aired": "2026-09-27",
        "runtime": "26",
        "studio": "VPRO",
        # Or a media server goes looking for a series of this name online.
        "lockdata": "true",
    }
    assert [g.text for g in root.findall("genre")] == [
        "Informatief",
        "Nieuws/actualiteiten",
    ]


def test_annotate_still_writes_infuses_xml_for_a_numbered_episode(media, tmp_path):
    item, dest = _kept_episode(tmp_path)
    archive.annotate(dest, item)

    root = ET.parse(dest.removesuffix(".mp4") + ".xml").getroot()
    assert root.findtext("title") == "26.000 sancties tegen Rusland"
    assert [g.text for g in root.findall("genres/genre")] == [
        "Informatief",
        "Nieuws/actualiteiten",
    ]


def test_annotate_writes_no_nfo_for_a_video_with_no_place_in_a_series(media, tmp_path):
    dest = _described(tmp_path)
    assert not os.path.exists(dest.removesuffix(".mp4") + ".nfo")


def _show_files(media):
    show = media / "bureau-buitenland"
    return sorted(p.name for p in show.iterdir() if p.is_file())


def test_annotate_show_describes_the_series_above_its_seasons(media, tmp_path):
    item, dest = _kept_episode(tmp_path)
    art = _source(tmp_path, "tmpabc.jpg", b"artwork")

    archive.annotate_show(
        dest, item, description="De wereldpolitiek.", fetch_image=lambda: art
    )

    assert _show_files(media) == ["fanart.jpg", "poster.jpg", "tvshow.nfo"]
    root = ET.parse(media / "bureau-buitenland" / "tvshow.nfo").getroot()
    assert root.tag == "tvshow"
    assert root.findtext("title") == "Bureau Buitenland"
    assert root.findtext("plot") == "De wereldpolitiek."
    assert root.findtext("studio") == "VPRO"
    assert root.findtext("lockdata") == "true"
    for name in ("poster.jpg", "fanart.jpg"):
        with open(media / "bureau-buitenland" / name, "rb") as fh:
            assert fh.read() == b"artwork"


def test_annotate_show_names_the_series_after_the_feed_when_it_must(media, tmp_path):
    item = _episode()
    del item.extra["show_title"]
    item, dest = _kept_episode(tmp_path, item)

    archive.annotate_show(dest, item)

    root = ET.parse(media / "bureau-buitenland" / "tvshow.nfo").getroot()
    assert root.findtext("title") == "Bureau Buitenland"


def test_annotate_show_fetches_no_picture_it_already_has(media, tmp_path):
    item, dest = _kept_episode(tmp_path)
    art = _source(tmp_path, "tmpabc.jpg", b"artwork")
    archive.annotate_show(dest, item, fetch_image=lambda: art)
    # Somebody has since put a proper, tall poster there by hand.
    poster = media / "bureau-buitenland" / "poster.jpg"
    poster.unlink()
    (media / "bureau-buitenland" / "poster.png").write_bytes(b"by hand")
    _aged(media / "bureau-buitenland" / "poster.png", 10)

    def no_fetch():
        raise AssertionError("fetched a picture the series already has")

    archive.annotate_show(dest, item, fetch_image=no_fetch)

    assert _show_files(media) == ["fanart.jpg", "poster.png", "tvshow.nfo"]
    assert (media / "bureau-buitenland" / "poster.png").read_bytes() == b"by hand"
    # And dated afresh, so it lasts as long as the episode just archived.
    assert time.time() - os.path.getmtime(media / "bureau-buitenland" / "poster.png") < 60


def test_annotate_show_fills_in_only_the_picture_that_is_missing(media, tmp_path):
    item, dest = _kept_episode(tmp_path)
    (media / "bureau-buitenland" / "poster.jpg").write_bytes(b"by hand")

    art = _source(tmp_path, "tmpabc.jpg", b"artwork")
    archive.annotate_show(dest, item, fetch_image=lambda: art)

    assert (media / "bureau-buitenland" / "poster.jpg").read_bytes() == b"by hand"
    assert (media / "bureau-buitenland" / "fanart.jpg").read_bytes() == b"artwork"


def test_annotate_show_is_only_for_an_episode_of_a_series(media, tmp_path):
    item = _item()
    dest = archive.keep(_source(tmp_path, "v.mp4"), feed="Show", item=item)

    archive.annotate_show(dest, item, description="d", fetch_image=lambda: "")

    assert _archived_names(media) == [os.path.join("show", "2026-03-04-episode-one.mp4")]


def _archived_names(root):
    return sorted(
        os.path.relpath(os.path.join(dirpath, name), root)
        for dirpath, _dirs, files in os.walk(root)
        for name in files
    )


# --- describing a video to a player -----------------------------------------


def _described(tmp_path, **fields):
    """Archive a video and annotate it, returning the archived path."""
    item = FeedItem(
        title=fields.pop("title", "Episode One"),
        description=fields.pop("description", "What happens in it."),
        published=fields.pop("published", datetime(2026, 3, 4, tzinfo=timezone.utc)),
    )
    dest = archive.keep(_source(tmp_path, "v.mp4", b"video"), feed="Show", item=item)
    archive.annotate(dest, item, **fields)
    return dest


def test_annotate_writes_the_metadata_a_player_reads(media, tmp_path):
    dest = _described(tmp_path)

    root = ET.parse(dest.removesuffix(".mp4") + ".xml").getroot()
    # Infuse's own format, and "Other" rather than "Movie": it is not a film,
    # and should not be looked up as one.
    assert (root.tag, root.attrib) == ("media", {"type": "Other"})
    assert root.findtext("title") == "Episode One"
    assert root.findtext("description") == "What happens in it."
    assert root.findtext("published") == "2026-03-04"


def test_annotate_escapes_what_xml_would_trip_over(media, tmp_path):
    dest = _described(
        tmp_path, title="Tom & Jerry <live>", description="Ça \"marche\"\x0b, non ?"
    )

    root = ET.parse(dest.removesuffix(".mp4") + ".xml").getroot()
    assert root.findtext("title") == "Tom & Jerry <live>"
    # A vertical tab cannot be written in XML at all; the file has to parse.
    assert root.findtext("description") == 'Ça "marche", non ?'


def test_annotate_leaves_out_what_the_item_does_not_have(media, tmp_path):
    dest = _described(tmp_path, description="", published=None)

    root = ET.parse(dest.removesuffix(".mp4") + ".xml").getroot()
    assert [child.tag for child in root] == ["title"]


def test_annotate_puts_the_picture_under_the_videos_name(media, tmp_path):
    still = _source(tmp_path, "tmpabc.JPG", b"a still")
    dest = _described(tmp_path, image=still)

    picture = dest.removesuffix(".mp4") + ".jpg"
    with open(picture, "rb") as fh:
        assert fh.read() == b"a still"
    # Linked, like the video: the temporary name can go and this one stays.
    assert os.stat(picture).st_ino == os.stat(still).st_ino


@pytest.mark.parametrize("image", ["", "still.webp", "still"])
def test_annotate_without_a_usable_picture_still_writes_the_metadata(
    media, tmp_path, image
):
    if image:
        image = _source(tmp_path, image, b"?")
    dest = _described(tmp_path, image=image)

    assert sorted(os.listdir(os.path.dirname(dest))) == [
        "2026-03-04-episode-one.mp4",
        "2026-03-04-episode-one.xml",
    ]


def test_annotate_swallows_errors(media, tmp_path, error_log):
    item = _item()
    dest = archive.keep(_source(tmp_path, "v.mp4"), feed="Show", item=item)

    # A picture that has already gone: the caller got the order wrong.
    archive.annotate(dest, item, image=str(tmp_path / "gone.jpg"))

    assert os.path.exists(dest)
    assert "could not describe" in error_log.read_text()


# --- slugs ------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Episode 1: The Beginning", "episode-1-the-beginning"),
        ("a/b/c", "a-b-c"),
        ("Café Society", "cafe-society"),
        ("  spaced  out  ", "spaced-out"),
        ("...", "item"),
        ("日本語", "item"),
        ("../../etc/passwd", "etc-passwd"),
    ],
)
def test_slug(title, expected):
    assert archive._slug(title) == expected


def test_slug_is_capped(media, tmp_path):
    dest = archive.keep(_source(tmp_path), feed="S", item=_item("x" * 300))
    stem = os.path.basename(dest).removesuffix(".mp3").removeprefix("2026-03-04-")
    assert len(stem) == archive.SLUG_MAX


def test_a_traversing_title_cannot_escape_the_archive(media, tmp_path):
    dest = archive.keep(
        _source(tmp_path), feed="../../outside", item=_item("../../../etc/passwd")
    )
    assert os.path.commonpath([str(media), dest]) == str(media)


# --- staging ----------------------------------------------------------------


def test_prepare_creates_staging_and_points_tmpdir_at_it(media, monkeypatch):
    monkeypatch.delenv("TMPDIR", raising=False)

    staging = archive.prepare()

    assert staging == str(media / archive.STAGING_NAME)
    assert os.path.isdir(staging)
    assert os.environ["TMPDIR"] == staging
    # Dot-prefixed, so Home Assistant's media browser skips it.
    assert os.path.basename(staging).startswith(".")


def test_downloads_really_land_in_staging(media):
    # The environment variable alone is not enough: tempfile reads TMPDIR once
    # and caches it, so a process that had already made a temp file would keep
    # using the old directory and every archived file would be a copy instead
    # of a link. Force that cache to be warm first, which is the failing case.
    tempfile.gettempdir()

    archive.prepare()

    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        assert os.path.dirname(path) == str(media / archive.STAGING_NAME)
    finally:
        os.unlink(path)


def test_a_staged_download_is_linked_not_copied(media, monkeypatch):
    # The two halves together: a download staged by `prepare` is on the same
    # filesystem as the archive, so `keep` takes the cheap path. This is the
    # whole reason staging lives inside the media directory.
    def no_copying(*_args, **_kwargs):
        raise AssertionError("keep fell back to copying a staged download")

    monkeypatch.setattr(archive.shutil, "copy2", no_copying)

    archive.prepare()
    fd, src = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)

    dest = archive.keep(src, feed="S", item=_item())

    assert os.stat(dest).st_ino == os.stat(src).st_ino


def test_prepare_gives_up_quietly_when_it_cannot_make_the_directory(
    media, monkeypatch, error_log
):
    monkeypatch.setattr(
        archive.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    )
    assert archive.prepare() is None
    assert "nope" in error_log.read_text()


# --- pruning ----------------------------------------------------------------


def _aged(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_prune_removes_only_what_is_past_the_cutoff(media, tmp_path):
    archive.prepare()
    fresh = archive.keep(_source(tmp_path, "a.mp3"), feed="S", item=_item("fresh"))
    stale = archive.keep(_source(tmp_path, "b.mp3"), feed="S", item=_item("stale"))
    _aged(stale, 20)

    assert archive.prune() == 1
    assert os.path.exists(fresh)
    assert not os.path.exists(stale)


def test_prune_respects_the_configured_window(media, tmp_path, monkeypatch):
    monkeypatch.setenv(archive.MEDIA_KEEP_DAYS_ENV, "30")
    kept = archive.keep(_source(tmp_path), feed="S", item=_item())
    _aged(kept, 20)

    assert archive.prune() == 0
    assert os.path.exists(kept)


def test_zero_days_disables_expiry(media, tmp_path, monkeypatch):
    monkeypatch.setenv(archive.MEDIA_KEEP_DAYS_ENV, "0")
    kept = archive.keep(_source(tmp_path), feed="S", item=_item())
    _aged(kept, 999)

    assert archive.prune() == 0
    assert os.path.exists(kept)


def test_prune_removes_directories_it_empties(media, tmp_path):
    stale = archive.keep(_source(tmp_path), feed="Gone Show", item=_item())
    _aged(stale, 20)

    archive.prune()

    assert not os.path.exists(media / "gone-show")
    # ...but never the archive root itself, which the next run wants to write to.
    assert os.path.isdir(media)


def test_prune_clears_abandoned_staging_files(media, tmp_path):
    staging = archive.prepare()
    orphan = os.path.join(staging, "rssignal-video-halfdone.mp4")
    with open(orphan, "wb") as fh:
        fh.write(b"partial")
    _aged(orphan, 20)

    assert archive.prune() == 1
    assert not os.path.exists(orphan)


def test_prune_takes_a_videos_description_with_it(media, tmp_path):
    dest = _described(tmp_path, image=_source(tmp_path, "still.jpg", b"a still"))
    # Only the video is old. The other two are always a little newer than it,
    # and on their own dates would be left behind for a run.
    _aged(dest, 20)
    os.utime(dest.removesuffix(".mp4") + ".xml")

    # One episode expired, however many files it was.
    assert archive.prune() == 1
    assert not os.path.exists(media / "show")


def test_prune_leaves_the_description_of_a_video_that_stays(media, tmp_path):
    dest = _described(tmp_path, image=_source(tmp_path, "still.jpg", b"a still"))
    stem = dest.removesuffix(".mp4")
    _aged(stem + ".xml", 20)
    _aged(stem + ".jpg", 20)

    assert archive.prune() == 0
    assert sorted(os.listdir(media / "show")) == [
        "2026-03-04-episode-one.jpg",
        "2026-03-04-episode-one.mp4",
        "2026-03-04-episode-one.xml",
    ]


def test_prune_clears_a_description_with_no_video(media, tmp_path):
    dest = _described(tmp_path)
    os.unlink(dest)
    _aged(dest.removesuffix(".mp4") + ".xml", 20)

    assert archive.prune() == 1
    assert not os.path.exists(media / "show")


def test_prune_takes_a_series_with_its_last_episode(media, tmp_path):
    item, dest = _kept_episode(tmp_path)
    archive.annotate(dest, item, image=_source(tmp_path, "still.jpg", b"a still"))
    archive.annotate_show(
        dest, item, fetch_image=lambda: _source(tmp_path, "art.jpg", b"artwork")
    )
    # Only the video is old; the series' own files were dated a moment ago.
    _aged(dest, 20)

    assert archive.prune() == 1
    assert not os.path.exists(media / "bureau-buitenland")


def test_prune_leaves_a_series_that_still_has_an_episode(media, tmp_path):
    old_item, old = _kept_episode(tmp_path, _episode("1", "9"))
    new_item, new = _kept_episode(tmp_path, _episode("2", "28"))
    archive.annotate_show(new, new_item)
    _aged(old, 20)

    assert archive.prune() == 1
    assert _archived_names(media) == [
        os.path.join("bureau-buitenland", "Season 02", "bureau-buitenland-s02e28.mp4"),
        os.path.join("bureau-buitenland", "tvshow.nfo"),
    ]


def test_prune_on_a_missing_directory_is_not_an_error(media):
    assert not os.path.exists(media)
    assert archive.prune() == 0


def test_prune_skips_a_file_it_cannot_delete(media, tmp_path, monkeypatch):
    doomed = archive.keep(_source(tmp_path, "a.mp3"), feed="S", item=_item("a"))
    other = archive.keep(_source(tmp_path, "b.mp3"), feed="S", item=_item("b"))
    _aged(doomed, 20)
    _aged(other, 20)

    real_unlink = archive.os.unlink

    def stubborn(path):
        if path == doomed:
            raise OSError("permission denied")
        real_unlink(path)

    monkeypatch.setattr(archive.os, "unlink", stubborn)

    # The walk carries on rather than abandoning the rest of the archive.
    assert archive.prune() == 1
    assert os.path.exists(doomed)
    assert not os.path.exists(other)


# --- keep_days parsing ------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, archive.DEFAULT_KEEP_DAYS),
        ("", archive.DEFAULT_KEEP_DAYS),
        ("7", 7),
        ("0", 0),
        ("not a number", archive.DEFAULT_KEEP_DAYS),
        ("-3", archive.DEFAULT_KEEP_DAYS),
    ],
)
def test_keep_days(raw, expected, monkeypatch):
    if raw is None:
        monkeypatch.delenv(archive.MEDIA_KEEP_DAYS_ENV, raising=False)
    else:
        monkeypatch.setenv(archive.MEDIA_KEEP_DAYS_ENV, raw)
    assert archive.keep_days() == expected


# --- dry-run description ----------------------------------------------------


def test_describe_names_the_path_without_promising_an_extension(media):
    assert archive.describe(_item(), feed="My Show") == str(
        media / "my-show" / "2026-03-04-episode-one.*"
    )
