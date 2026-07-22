"""Tests for rssignal.cli."""

from types import SimpleNamespace

import pytest

from rssignal import cli
from rssignal.config import ConfigError
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
