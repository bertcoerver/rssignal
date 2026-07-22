"""Tests for rssignal.run (feeds, sending, and downloads are monkeypatched)."""

from contextlib import contextmanager
from datetime import datetime, timezone

from rssignal import run
from rssignal.feeds import FeedConfig, FeedItem
from rssignal.run import run_feeds

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def _patch_feeds(monkeypatch, configs, items_by_url):
    monkeypatch.setattr(run, "load_feeds", lambda path: configs)
    monkeypatch.setattr(run, "parse_feed", lambda cfg: items_by_url[cfg.url])


def _capture_sends(monkeypatch):
    sends = []

    def fake_send(text, recipient=None, attachments=None, voice_note=False):
        sends.append(
            {
                "text": text,
                "recipient": recipient,
                "attachments": attachments,
                "voice_note": voice_note,
            }
        )

    monkeypatch.setattr(run, "send_msg", fake_send)
    return sends


def test_run_regular_sends_text_per_item(monkeypatch):
    cfg = FeedConfig(url="https://a", type="regular")
    items = [
        FeedItem(title="One", description="d1", link="https://a/1"),
        FeedItem(title="Two", description="d2", link="https://a/2"),
    ]
    _patch_feeds(monkeypatch, [cfg], {"https://a": items})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", now=NOW)

    assert count == 2
    assert sends[0]["text"] == "One\n\nd1\n\nhttps://a/1"
    assert all(s["voice_note"] is False for s in sends)


def test_run_podcast_downloads_and_sends_voice_note(monkeypatch):
    cfg = FeedConfig(url="https://a", type="podcast", recipient="+31699999999")
    item = FeedItem(
        title="Ep", description="notes", enclosure_url="https://a/ep.mp3"
    )
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    @contextmanager
    def fake_download(url, **kwargs):
        assert url == "https://a/ep.mp3"
        yield "/tmp/fake-ep.mp3"

    monkeypatch.setattr(run, "download_temp", fake_download)

    count = run_feeds("feeds.json", now=NOW)

    assert count == 1
    assert sends[0]["voice_note"] is True
    assert sends[0]["attachments"] == ["/tmp/fake-ep.mp3"]
    assert sends[0]["recipient"] == "+31699999999"
    assert sends[0]["text"] == "Ep\n\nnotes"


def test_run_podcast_without_enclosure_falls_back_to_text(monkeypatch):
    cfg = FeedConfig(url="https://a", type="podcast")
    item = FeedItem(title="Ep", description="notes", enclosure_url=None)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", now=NOW)

    assert count == 1
    assert sends[0]["voice_note"] is False
    assert sends[0]["attachments"] is None


def test_run_dry_run_sends_nothing(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", type="regular", name="Blog")
    item = FeedItem(title="One", description="d1", link="https://a/1")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", dry_run=True, now=NOW)

    assert count == 1
    assert sends == []
    assert "Blog" in capsys.readouterr().out
