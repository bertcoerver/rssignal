"""Tests for rssignal.artwork (feeds, downloads and signal-cli are monkeypatched)."""

from contextlib import contextmanager

import pytest

from rssignal import artwork
from rssignal.feeds import FeedConfig, FeedError
from rssignal.signal_cli import SignalGroup


def _patch(monkeypatch, tmp_path, images, *, fail_download=None):
    """Serve ``images`` (feed name -> (url, bytes)) and record every upload.

    Each image is written to a real file, since what is compared is its content.
    """
    updated = []

    def fake_feed_image(cfg):
        found = images.get(cfg.name)
        if isinstance(found, Exception):
            raise found
        return found[0] if found else None

    contents = {v[0]: v[1] for v in images.values() if isinstance(v, tuple)}

    @contextmanager
    def fake_download(url, **kwargs):
        if url == fail_download:
            raise FeedError(f"Could not download {url!r}: nope")
        path = tmp_path / url.rsplit("/", 1)[-1]
        path.write_bytes(contents[url])
        yield str(path)

    def fake_update(group_id, *, avatar=None):
        updated.append((group_id, avatar.rsplit("/", 1)[-1]))

    monkeypatch.setattr(artwork, "feed_image", fake_feed_image)
    monkeypatch.setattr(artwork, "download_temp", fake_download)
    monkeypatch.setattr(artwork, "update_group", fake_update)
    return updated


def _feeds(*names):
    return [FeedConfig(url=f"https://{n.lower()}", name=n) for n in names]


def _groups(*names):
    return [SignalGroup(id=f"{n}=", name=n) for n in names]


def test_refresh_puts_the_current_artwork_on_each_group(monkeypatch, tmp_path):
    updated = _patch(
        monkeypatch,
        tmp_path,
        {"Pod": ("https://a/pod.jpg", b"pod"), "Chan": ("https://b/chan.jpg", b"chan")},
    )

    changed = artwork.refresh(_feeds("Pod", "Chan"), _groups("Pod", "Chan"))

    assert changed == 2
    assert updated == [("Pod=", "pod.jpg"), ("Chan=", "chan.jpg")]


def test_refresh_leaves_an_unchanged_picture_alone(monkeypatch, tmp_path):
    # Every upload is a "changed the group picture" line in the chat.
    updated = _patch(monkeypatch, tmp_path, {"Pod": ("https://a/pod.jpg", b"pod")})
    artwork.refresh(_feeds("Pod"), _groups("Pod"))

    changed = artwork.refresh(_feeds("Pod"), _groups("Pod"))

    assert changed == 0
    assert len(updated) == 1


def test_refresh_uploads_artwork_that_changed(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, {"Pod": ("https://a/pod.jpg", b"old")})
    artwork.refresh(_feeds("Pod"), _groups("Pod"))
    # Same url, new picture behind it: compared by content, not address.
    updated = _patch(monkeypatch, tmp_path, {"Pod": ("https://a/pod.jpg", b"new")})

    assert artwork.refresh(_feeds("Pod"), _groups("Pod")) == 1
    assert updated == [("Pod=", "pod.jpg")]


def test_refresh_creates_no_group(monkeypatch, tmp_path):
    updated = _patch(monkeypatch, tmp_path, {"Pod": ("https://a/pod.jpg", b"pod")})

    assert artwork.refresh(_feeds("Pod"), []) == 0
    assert updated == []


def test_refresh_skips_a_feed_that_opted_out(monkeypatch, tmp_path):
    # A source whose artwork is this week's episode would be uploaded every time.
    updated = _patch(
        monkeypatch,
        tmp_path,
        {"Maps": ("https://a/maps.jpg", b"maps"), "Pod": ("https://a/pod.jpg", b"pod")},
    )
    maps = FeedConfig(url="https://maps", name="Maps", refresh_image=False)

    assert artwork.refresh([maps, *_feeds("Pod")], _groups("Maps", "Pod")) == 1
    assert updated == [("Pod=", "pod.jpg")]


def test_refresh_leaves_the_picture_of_a_feed_without_artwork(monkeypatch, tmp_path):
    updated = _patch(monkeypatch, tmp_path, {})

    assert artwork.refresh(_feeds("Blog"), _groups("Blog")) == 0
    assert updated == []


def test_one_feed_failing_does_not_stop_the_others(monkeypatch, tmp_path, capsys):
    updated = _patch(
        monkeypatch,
        tmp_path,
        {
            "Down": FeedError("source is down"),
            "Broken": ("https://c/broken.jpg", b"x"),
            "Pod": ("https://a/pod.jpg", b"pod"),
        },
        fail_download="https://c/broken.jpg",
    )

    changed = artwork.refresh(
        _feeds("Down", "Broken", "Pod"), _groups("Down", "Broken", "Pod")
    )

    assert changed == 1
    assert updated == [("Pod=", "pod.jpg")]
    err = capsys.readouterr().err
    assert "[Down] group image not refreshed: source is down" in err
    assert "[Broken] group image not refreshed" in err


def test_a_failed_upload_is_tried_again_next_time(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, {"Pod": ("https://a/pod.jpg", b"pod")})

    def failing_update(group_id, *, avatar=None):
        raise RuntimeError("signal-cli fell over")

    monkeypatch.setattr(artwork, "update_group", failing_update)
    artwork.refresh(_feeds("Pod"), _groups("Pod"))

    updated = _patch(monkeypatch, tmp_path, {"Pod": ("https://a/pod.jpg", b"pod")})
    assert artwork.refresh(_feeds("Pod"), _groups("Pod")) == 1
    assert updated == [("Pod=", "pod.jpg")]


def test_dry_run_reports_and_changes_nothing(monkeypatch, tmp_path, capsys):
    updated = _patch(monkeypatch, tmp_path, {"Pod": ("https://a/pod.jpg", b"pod")})

    assert artwork.refresh(_feeds("Pod"), _groups("Pod"), dry_run=True) == 1
    assert updated == []
    assert "would be updated from https://a/pod.jpg" in capsys.readouterr().out
    # Nothing remembered, so the real refresh still uploads it.
    assert artwork.refresh(_feeds("Pod"), _groups("Pod")) == 1


# --- the dice -----------------------------------------------------------------


def test_refresh_is_about_once_a_month_at_fifteen_runs_a_day(monkeypatch):
    monkeypatch.delenv(artwork.REFRESH_ONE_IN_ENV)

    assert artwork.refresh_one_in() == 15 * 30


@pytest.mark.parametrize(
    "roll, due", [(0.0, True), (1 / 450 - 1e-9, True), (1 / 450, False), (0.9, False)]
)
def test_refresh_due_is_one_run_in_n(monkeypatch, roll, due):
    monkeypatch.setenv(artwork.REFRESH_ONE_IN_ENV, "450")

    assert artwork.refresh_due(lambda: roll) is due


def test_one_in_one_refreshes_every_run(monkeypatch):
    monkeypatch.setenv(artwork.REFRESH_ONE_IN_ENV, "1")

    assert artwork.refresh_due(lambda: 0.999) is True


def test_zero_turns_the_refresh_off(monkeypatch):
    monkeypatch.setenv(artwork.REFRESH_ONE_IN_ENV, "0")

    assert artwork.refresh_due(lambda: 0.0) is False


def test_a_setting_that_is_not_a_number_falls_back(monkeypatch, capsys):
    monkeypatch.setenv(artwork.REFRESH_ONE_IN_ENV, "monthly")

    assert artwork.refresh_one_in() == artwork.DEFAULT_REFRESH_ONE_IN
    assert "not a whole number" in capsys.readouterr().err
