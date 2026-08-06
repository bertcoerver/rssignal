"""Tests for rssignal.download (urlopen is monkeypatched — no network)."""

import io
import os
import socket
from urllib.error import HTTPError, URLError

import pytest

from rssignal import download, feeds
from rssignal.download import download_temp
from rssignal.feeds import FeedError

# conftest stubs download.reachable out for the whole suite, so that no test
# quietly depends on a host being up. The tests for the probe itself want the
# real one, taken here before any fixture has had a chance to replace it.
probe = download.reachable


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


def test_fetch_text_retries_a_network_failure(monkeypatch):
    calls = []

    def flaky(url, timeout=30):
        calls.append(url)
        if len(calls) < 3:
            raise OSError("nodename nor servname provided, or not known")
        return _FakeResponse(b"ok")

    monkeypatch.setattr(download, "urlopen", flaky)

    assert download.fetch_text("https://a/playlist") == "ok"
    assert len(calls) == 3


def test_fetch_text_gives_up_after_the_last_attempt(monkeypatch):
    calls = []

    def boom(url, timeout=30):
        calls.append(url)
        raise OSError("connection reset")

    monkeypatch.setattr(download, "urlopen", boom)

    with pytest.raises(FeedError):
        download.fetch_text("https://a/playlist")
    assert len(calls) == feeds.RETRY_ATTEMPTS


def test_fetch_text_does_not_retry_a_bad_url(monkeypatch):
    calls = []

    def boom(url, timeout=30):
        calls.append(url)
        raise ValueError("unknown url type")

    monkeypatch.setattr(download, "urlopen", boom)

    with pytest.raises(FeedError):
        download.fetch_text("gopher://a")
    assert len(calls) == 1


def test_download_temp_retry_does_not_append_to_a_partial_file(monkeypatch):
    """A download cut off halfway must not leave its first bytes in the file."""
    calls = []

    def flaky(url, timeout=30):
        calls.append(url)
        if len(calls) == 1:
            return _HalfWayResponse(b"partial-")
        return _FakeResponse(b"whole-file")

    monkeypatch.setattr(download, "urlopen", flaky)

    with download_temp("https://a/ep1.mp3") as path:
        with open(path, "rb") as fh:
            assert fh.read() == b"whole-file"


class _HalfWayResponse(_FakeResponse):
    """Hands over some bytes, then dies the way a reset connection does."""

    def read(self, size=-1):
        chunk = super().read(size)
        if not chunk:
            raise ConnectionResetError("connection reset by peer")
        return chunk


def test_reachable_when_the_host_answers(monkeypatch):
    seen = {}

    def head(request, timeout=30):
        seen["method"] = request.get_method()
        seen["url"] = request.full_url
        return _FakeResponse(b"")

    monkeypatch.setattr(download, "urlopen", head)

    assert probe("https://www.youtube.com/") is True
    # Headers only — the probe must not pull the page down to answer.
    assert seen["method"] == "HEAD"
    assert seen["url"] == "https://www.youtube.com/"


def test_reachable_counts_an_http_error_as_an_answer(monkeypatch):
    def refused(request, timeout=30):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(download, "urlopen", refused)

    # 403 is the host talking, which is the whole question being asked.
    assert probe("https://www.youtube.com/") is True


@pytest.mark.parametrize(
    "error",
    [
        URLError(socket.gaierror("nodename nor servname provided")),
        ConnectionRefusedError("connection refused"),
        TimeoutError("timed out"),
    ],
)
def test_reachable_is_false_when_no_connection_can_be_made(monkeypatch, error):
    def boom(request, timeout=30):
        raise error

    monkeypatch.setattr(download, "urlopen", boom)

    assert probe("https://www.youtube.com/") is False


def test_reachable_does_not_retry(monkeypatch):
    """It exists to fail fast; retrying it would defeat the point."""
    calls = []

    def boom(request, timeout=30):
        calls.append(request.full_url)
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(download, "urlopen", boom)

    assert probe("https://www.youtube.com/") is False
    assert len(calls) == 1
