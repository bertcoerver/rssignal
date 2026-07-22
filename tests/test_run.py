"""Tests for rssignal.run (feeds, sending, and downloads are monkeypatched)."""

from contextlib import contextmanager
from datetime import datetime, timezone

from rssignal import run
from rssignal.feeds import FeedConfig, FeedError, FeedFilter, FeedItem
from rssignal.run import run_feeds
from rssignal.signal_cli import LinkPreview

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def _patch_feeds(monkeypatch, configs, items_by_url):
    monkeypatch.setattr(run, "load_feeds", lambda path: configs)
    monkeypatch.setattr(run, "parse_feed", lambda cfg: items_by_url[cfg.url])


def _capture_sends(monkeypatch):
    sends = []

    def fake_send(
        text, recipient=None, attachments=None, voice_note=False, preview=None
    ):
        sends.append(
            {
                "text": text,
                "recipient": recipient,
                "attachments": attachments,
                "voice_note": voice_note,
                "preview": preview,
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


def test_run_uses_message_template(monkeypatch):
    cfg = FeedConfig(
        url="https://a",
        type="regular",
        name="Blog",
        message_template="📰 {title} [{feed_name}]\n\n{link}",
    )
    item = FeedItem(title="One", description="d1", link="https://a/1", feed_name="Blog")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json", now=NOW)

    assert sends[0]["text"] == "📰 One [Blog]\n\nhttps://a/1"


def test_run_applies_field_filters(monkeypatch):
    cfg = FeedConfig(
        url="https://a",
        type="regular",
        filters=(FeedFilter("title", "excludes", ("sponsored",)),),
    )
    items = [
        FeedItem(title="Real post", description="d1"),
        FeedItem(title="Sponsored post", description="d2"),
    ]
    _patch_feeds(monkeypatch, [cfg], {"https://a": items})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", now=NOW)

    assert count == 1
    assert sends[0]["text"].startswith("Real post")


# --- link previews ---------------------------------------------------------

_EPISODE = FeedItem(
    title="Ep",
    description="notes",
    link="https://a/ep.mp3",
    enclosure_url="https://a/ep.mp3",
    image_url="https://a/show.jpg",
)


def _fake_downloads(monkeypatch, fail_on=None):
    """Patch download_temp, recording urls and optionally failing on one."""
    got = []

    @contextmanager
    def fake_download(url, **kwargs):
        got.append(url)
        if url == fail_on:
            raise FeedError(f"Could not download {url!r}: nope")
        yield f"/tmp/fake-{url.rsplit('/', 1)[-1]}"

    monkeypatch.setattr(run, "download_temp", fake_download)
    return got


def test_run_podcast_splits_card_and_voice_note(monkeypatch):
    # Signal drops a preview card from a message that has an attachment, so the
    # card and the audio have to be two messages.
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    sends = _capture_sends(monkeypatch)
    downloaded = _fake_downloads(monkeypatch)

    count = run_feeds("feeds.json", now=NOW)

    assert downloaded == ["https://a/ep.mp3", "https://a/show.jpg"]
    assert count == 1  # one item, even though it took two messages
    assert len(sends) == 2

    card, audio = sends
    assert card["preview"] == LinkPreview(
        url="https://a/ep.mp3",
        title="Ep",
        description="notes",
        image="/tmp/fake-show.jpg",
    )
    # signal-cli needs the preview url in the body of the message carrying it.
    assert "https://a/ep.mp3" in card["text"]
    assert card["attachments"] is None
    assert card["voice_note"] is False

    assert audio["attachments"] == ["/tmp/fake-ep.mp3"]
    assert audio["voice_note"] is True
    assert audio["preview"] is None
    # The text already went out with the card; repeating it would be noise.
    assert audio["text"] == ""


def test_run_podcast_without_a_card_stays_one_message(monkeypatch):
    cfg = FeedConfig(url="https://a", type="podcast", link_preview=False)
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    sends = _capture_sends(monkeypatch)
    _fake_downloads(monkeypatch)

    run_feeds("feeds.json", now=NOW)

    assert len(sends) == 1
    assert sends[0]["voice_note"] is True
    assert sends[0]["text"].startswith("Ep")


def test_run_sends_without_artwork_when_its_download_fails(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    sends = _capture_sends(monkeypatch)
    _fake_downloads(monkeypatch, fail_on="https://a/show.jpg")

    count = run_feeds("feeds.json", now=NOW)

    # The episode still goes out; only the card's image is lost.
    assert count == 1
    assert sends[0]["preview"].image == ""
    assert sends[0]["preview"].title == "Ep"
    assert sends[1]["attachments"] == ["/tmp/fake-ep.mp3"]
    assert "preview image skipped" in capsys.readouterr().err


def test_run_regular_feed_sends_no_preview(monkeypatch):
    cfg = FeedConfig(url="https://a", type="regular")
    item = FeedItem(title="One", description="d1", link="https://a/1")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    run_feeds("feeds.json", now=NOW)

    assert sends[0]["preview"] is None


def test_run_dry_run_describes_preview_without_downloading(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", type="podcast", name="Pod")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [_EPISODE]})
    _capture_sends(monkeypatch)

    def fail(url, **kwargs):
        raise AssertionError("dry run must not download")

    monkeypatch.setattr(run, "download_temp", fail)

    run_feeds("feeds.json", dry_run=True, now=NOW)

    out = capsys.readouterr().out
    assert "preview: Ep <https://a/ep.mp3>" in out
    assert "preview image: https://a/show.jpg" in out


def test_run_dry_run_sends_nothing(monkeypatch, capsys):
    cfg = FeedConfig(url="https://a", type="regular", name="Blog")
    item = FeedItem(title="One", description="d1", link="https://a/1")
    _patch_feeds(monkeypatch, [cfg], {"https://a": [item]})
    sends = _capture_sends(monkeypatch)

    count = run_feeds("feeds.json", dry_run=True, now=NOW)

    assert count == 1
    assert sends == []
    assert "Blog" in capsys.readouterr().out
