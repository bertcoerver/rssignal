"""Tests for rssignal.signal_cli (all subprocess calls are mocked)."""

import subprocess
from types import SimpleNamespace

import pytest

from rssignal import signal_cli
from rssignal.signal_cli import (
    AccountNotLinked,
    SignalCliNotFound,
    SignalSendError,
    check_account,
    find_signal_cli,
    is_account_registered,
    link_device,
    list_accounts,
    send_msg,
)

FAKE_BIN = "/usr/local/bin/signal-cli"


@pytest.fixture
def have_binary(monkeypatch):
    monkeypatch.setattr(signal_cli.shutil, "which", lambda name: FAKE_BIN)


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_find_signal_cli_missing(monkeypatch):
    monkeypatch.setattr(signal_cli.shutil, "which", lambda name: None)
    with pytest.raises(SignalCliNotFound):
        find_signal_cli()


def test_find_signal_cli_found(have_binary):
    assert find_signal_cli() == FAKE_BIN


def test_list_accounts_parses_output(have_binary, monkeypatch):
    output = "Number: +31600000000\nNumber: +31611111111\n"
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda *a, **k: _completed(stdout=output),
    )
    assert list_accounts() == ["+31600000000", "+31611111111"]


def test_list_accounts_failure_raises(have_binary, monkeypatch):
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda *a, **k: _completed(returncode=1, stderr="boom"),
    )
    with pytest.raises(SignalSendError):
        list_accounts()


def test_is_account_registered(have_binary, monkeypatch):
    monkeypatch.setattr(
        signal_cli, "list_accounts", lambda: ["+31600000000"]
    )
    assert is_account_registered("+31600000000") is True
    assert is_account_registered("+31699999999") is False


def test_check_account_not_linked(have_binary, monkeypatch):
    monkeypatch.setattr(signal_cli, "list_accounts", lambda: [])
    with pytest.raises(AccountNotLinked):
        check_account("+31600000000")


def test_send_msg_builds_correct_argv(have_binary, monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["timeout"] = kwargs.get("timeout")
        return _completed(returncode=0)

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)

    send_msg("hello", recipient="+31611111111", account="+31600000000")

    assert calls["argv"] == [
        FAKE_BIN,
        "-a",
        "+31600000000",
        "send",
        "-m",
        "hello",
        "+31611111111",
    ]


def test_send_msg_uses_config_defaults(have_binary, monkeypatch):
    monkeypatch.setattr(
        signal_cli,
        "get_config",
        lambda: SimpleNamespace(account="+31600000000", recipient="+31622222222"),
    )
    captured = {}

    def fake_run(argv, **k):
        captured["argv"] = argv
        return _completed()

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)

    send_msg("hi")

    assert captured["argv"][2] == "+31600000000"
    assert captured["argv"][-1] == "+31622222222"


def test_send_msg_no_recipient_raises(have_binary, monkeypatch):
    monkeypatch.setattr(
        signal_cli,
        "get_config",
        lambda: SimpleNamespace(account="+31600000000", recipient=None),
    )
    with pytest.raises(ValueError):
        send_msg("hi")


def test_send_msg_nonzero_exit_raises(have_binary, monkeypatch):
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda *a, **k: _completed(returncode=1, stderr="nope"),
    )
    with pytest.raises(SignalSendError) as excinfo:
        send_msg("hi", recipient="+31611111111", account="+31600000000")
    assert excinfo.value.returncode == 1
    assert "nope" in excinfo.value.stderr


def test_send_msg_timeout_raises(have_binary, monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="signal-cli", timeout=60)

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)
    with pytest.raises(SignalSendError):
        send_msg("hi", recipient="+31611111111", account="+31600000000")


def test_link_device_prints_uri_when_no_qrencode(have_binary, monkeypatch, capsys):
    uri = "sgnl://linkdevice?uuid=abc&pub_key=xyz"

    class FakeProc:
        def __init__(self):
            self.stdout = SimpleNamespace(readline=lambda: uri + "\n")
            self.returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(signal_cli.subprocess, "Popen", lambda *a, **k: FakeProc())
    # qrencode absent, signal-cli present
    monkeypatch.setattr(
        signal_cli.shutil,
        "which",
        lambda name: FAKE_BIN if name == "signal-cli" else None,
    )

    link_device()

    out = capsys.readouterr().out
    assert uri in out
