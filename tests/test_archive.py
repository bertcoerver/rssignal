"""Tests for rssignal.archive (no network — everything here is filesystem)."""

import os
import tempfile
import time
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
