"""Tests for rssignal.download (urlopen is monkeypatched — no network)."""

import io
import os

import pytest

from rssignal import download
from rssignal.download import download_temp
from rssignal.feeds import FeedError


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_download_temp_writes_and_cleans_up(monkeypatch):
    monkeypatch.setattr(
        download, "urlopen", lambda url, timeout=30: _FakeResponse(b"audio-bytes")
    )

    seen_path = None
    with download_temp("https://a/ep1.mp3") as path:
        seen_path = path
        assert path.endswith(".mp3")
        with open(path, "rb") as fh:
            assert fh.read() == b"audio-bytes"

    # File is removed once the context exits.
    assert not os.path.exists(seen_path)


def test_download_temp_default_suffix(monkeypatch):
    monkeypatch.setattr(
        download, "urlopen", lambda url, timeout=30: _FakeResponse(b"x")
    )
    with download_temp("https://a/stream?id=5") as path:
        assert path.endswith(".mp3")


def test_download_temp_error_raises_and_cleans_up(monkeypatch):
    def boom(url, timeout=30):
        raise OSError("connection reset")

    monkeypatch.setattr(download, "urlopen", boom)

    with pytest.raises(FeedError):
        with download_temp("https://a/ep1.mp3"):
            pass
