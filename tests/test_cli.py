"""Tests for rssignal.cli."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from rssignal import cli
from rssignal.config import ConfigError
from rssignal.feeds import FeedConfig, FeedItem
from rssignal.signal_cli import SignalSendError


def test_send_success(monkeypatch, capsys):
    captured = {}

    def fake_send(message, recipient=None):
        captured["message"] = message
        captured["recipient"] = recipient

    monkeypatch.setattr(cli, "send_msg", fake_send)

    rc = cli.main(["send", "hello", "--to", "+31611111111"])

    assert rc == 0
    assert captured == {"message": "hello", "recipient": "+31611111111"}
    assert "Message sent." in capsys.readouterr().out


def test_send_error_returns_1(monkeypatch, capsys):
    def fake_send(message, recipient=None):
        raise SignalSendError("failed", returncode=1, stderr="bad")

    monkeypatch.setattr(cli, "send_msg", fake_send)

    rc = cli.main(["send", "hello"])

    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_doctor_reports_ok(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_signal_cli", lambda: "/bin/signal-cli")
    monkeypatch.setattr(cli, "list_accounts", lambda: ["+31600000000"])
    monkeypatch.setattr(
        cli,
        "get_config",
        lambda: SimpleNamespace(account="+31600000000", recipient="+31611111111"),
    )

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "/bin/signal-cli" in out
    assert "+31600000000" in out


def test_doctor_warns_when_account_not_linked(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_signal_cli", lambda: "/bin/signal-cli")
    monkeypatch.setattr(cli, "list_accounts", lambda: [])
    monkeypatch.setattr(
        cli,
        "get_config",
        lambda: SimpleNamespace(account="+31600000000", recipient=None),
    )

    rc = cli.main(["doctor"])

    assert rc == 1
    assert "not linked" in capsys.readouterr().out


def test_doctor_config_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_signal_cli", lambda: "/bin/signal-cli")
    monkeypatch.setattr(cli, "list_accounts", lambda: [])

    def raise_config():
        raise ConfigError("no account")

    monkeypatch.setattr(cli, "get_config", raise_config)

    rc = cli.main(["doctor"])

    assert rc == 1


def test_link_invokes_link_device(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        cli, "link_device", lambda name: captured.setdefault("name", name)
    )

    rc = cli.main(["link", "--name", "mybot"])

    assert rc == 0
    assert captured["name"] == "mybot"
    assert "Linking complete." in capsys.readouterr().out


def test_no_command_errors():
    with pytest.raises(SystemExit):
        cli.main([])


# --- fields ----------------------------------------------------------------

_ITEM = FeedItem(
    title="Episode 402",
    description="Show notes",
    link="https://a/402",
    published=datetime(2026, 7, 22, 6, 0, 0, tzinfo=timezone.utc),
    enclosure_url="https://a/402.mp3",
    enclosure_type="audio/mpeg",
    author="Example Media",
    categories=("news",),
    feed_name="Pod",
    extra={"itunes_duration": "00:42:11"},
)

_CONFIGS = [
    FeedConfig(url="https://a/rss", type="podcast", name="Pod"),
    FeedConfig(url="https://b/rss", type="regular", name="Blog"),
]


@pytest.fixture
def patched_feeds(monkeypatch):
    monkeypatch.setattr(cli, "load_feeds", lambda path: _CONFIGS)
    monkeypatch.setattr(cli, "parse_feed", lambda cfg: [_ITEM, _ITEM])


def test_fields_lists_core_and_extra_fields(patched_feeds, capsys):
    rc = cli.main(["fields"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Pod" in out  # defaults to the first configured feed
    assert "itunes_duration" in out
    assert "(extra)" in out
    assert "published_date" in out
    assert "2026-07-22" in out


def test_fields_selects_feed_by_name(patched_feeds, capsys):
    assert cli.main(["fields", "--feed", "blog"]) == 0
    assert "Blog" in capsys.readouterr().out


def test_fields_unknown_feed_errors(patched_feeds, capsys):
    assert cli.main(["fields", "--feed", "nope"]) == 1
    assert "No feed matching" in capsys.readouterr().err


def test_fields_url_bypasses_the_config(monkeypatch, capsys):
    def fail(path):
        raise AssertionError("--url should not read the config")

    monkeypatch.setattr(cli, "load_feeds", fail)
    monkeypatch.setattr(cli, "parse_feed", lambda cfg: [_ITEM])

    assert cli.main(["fields", "--url", "https://c/rss"]) == 0
    assert "https://c/rss" in capsys.readouterr().out


def test_fields_item_out_of_range_errors(patched_feeds, capsys):
    assert cli.main(["fields", "--item", "9"]) == 1
    assert "out of range" in capsys.readouterr().err


def test_fields_renders_template_preview(patched_feeds, capsys):
    rc = cli.main(["fields", "--template", "{title} - {itunes_duration}"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "rendered message" in out
    assert "Episode 402 - 00:42:11" in out
