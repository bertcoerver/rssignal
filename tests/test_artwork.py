"""Tests for rssignal.artwork (a real library on disk; signal-cli is monkeypatched)."""

import json

from rssignal import artwork
from rssignal.feeds import FeedConfig
from rssignal.signal_cli import SignalError, SignalGroup


def _shelve(root, name, picture=None):
    """A feed's folder in the library, with ``picture`` as its cover if given."""
    folder = root / "podcasts" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "feed.json").write_text(json.dumps({"format": 1, "name": name, "image": "cover.jpg"}))
    if picture is not None:
        (folder / "cover.jpg").write_bytes(picture)
    return folder


def _patch_uploads(monkeypatch, fail_for=()):
    updated = []

    def fake_update(group_id, *, avatar=None):
        if group_id in fail_for:
            raise SignalError("upload failed")
        updated.append((group_id, open(avatar, "rb").read()))

    monkeypatch.setattr(artwork, "update_group", fake_update)
    return updated


def _feeds(*names, **kwargs):
    return [FeedConfig(name=n, **kwargs) for n in names]


def _groups(*names):
    return [SignalGroup(id=f"{n}=", name=n) for n in names]


def test_refresh_puts_the_librarys_artwork_on_each_group(monkeypatch, own_library):
    _shelve(own_library, "Pod", b"pod")
    _shelve(own_library, "Chan", b"chan")
    updated = _patch_uploads(monkeypatch)

    changed = artwork.refresh(_feeds("Pod", "Chan"), _groups("Pod", "Chan"))

    assert changed == 2
    assert updated == [("Pod=", b"pod"), ("Chan=", b"chan")]


def test_refresh_leaves_an_unchanged_picture_alone(monkeypatch, own_library):
    # Every upload is a "changed the group picture" line in the chat.
    _shelve(own_library, "Pod", b"pod")
    updated = _patch_uploads(monkeypatch)
    artwork.refresh(_feeds("Pod"), _groups("Pod"))

    assert artwork.refresh(_feeds("Pod"), _groups("Pod")) == 0
    assert len(updated) == 1


def test_refresh_uploads_artwork_that_changed(monkeypatch, own_library):
    _shelve(own_library, "Pod", b"old")
    _patch_uploads(monkeypatch)
    artwork.refresh(_feeds("Pod"), _groups("Pod"))

    _shelve(own_library, "Pod", b"new")
    updated = _patch_uploads(monkeypatch)
    assert artwork.refresh(_feeds("Pod"), _groups("Pod")) == 1
    assert updated == [("Pod=", b"new")]


def test_refresh_reads_the_feed_its_source_names(monkeypatch, own_library):
    _shelve(own_library, "Le Dessous des Images", b"ldi")
    updated = _patch_uploads(monkeypatch)

    feeds = [FeedConfig(name="Images", source="Le Dessous des Images")]
    artwork.refresh(feeds, _groups("Images"))

    assert updated == [("Images=", b"ldi")]


def test_refresh_creates_no_group(monkeypatch, own_library):
    _shelve(own_library, "Pod", b"pod")
    updated = _patch_uploads(monkeypatch)

    assert artwork.refresh(_feeds("Pod"), []) == 0
    assert updated == []


def test_refresh_skips_a_feed_that_opted_out(monkeypatch, own_library):
    _shelve(own_library, "Pod", b"pod")
    updated = _patch_uploads(monkeypatch)

    assert artwork.refresh(_feeds("Pod", refresh_image=False), _groups("Pod")) == 0
    assert updated == []


def test_refresh_leaves_the_picture_of_a_feed_without_artwork(monkeypatch, own_library):
    _shelve(own_library, "Pod")  # a folder, but no cover in it yet
    updated = _patch_uploads(monkeypatch)

    assert artwork.refresh(_feeds("Pod", "Gone"), _groups("Pod", "Gone")) == 0
    assert updated == []


def test_a_failed_upload_does_not_stop_the_others_and_is_tried_again(
    monkeypatch, own_library, capsys
):
    _shelve(own_library, "Pod", b"pod")
    _shelve(own_library, "Chan", b"chan")
    updated = _patch_uploads(monkeypatch, fail_for={"Pod="})

    assert artwork.refresh(_feeds("Pod", "Chan"), _groups("Pod", "Chan")) == 1
    assert updated == [("Chan=", b"chan")]
    assert "[Pod] group image not refreshed: upload failed" in capsys.readouterr().err

    updated = _patch_uploads(monkeypatch)
    assert artwork.refresh(_feeds("Pod", "Chan"), _groups("Pod", "Chan")) == 1
    assert updated == [("Pod=", b"pod")]


def test_dry_run_reports_and_changes_nothing(monkeypatch, own_library, capsys):
    folder = _shelve(own_library, "Pod", b"pod")
    updated = _patch_uploads(monkeypatch)

    assert artwork.refresh(_feeds("Pod"), _groups("Pod"), dry_run=True) == 1
    assert updated == []
    assert f"group image would be updated from {folder / 'cover.jpg'}" in capsys.readouterr().out
    # Nothing remembered either, so the real run still uploads it.
    assert artwork.refresh(_feeds("Pod"), _groups("Pod")) == 1
