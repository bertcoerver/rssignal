"""Tests for rssignal.signal_cli (all subprocess calls are mocked)."""

import subprocess
from types import SimpleNamespace

import pytest

from rssignal import signal_cli
from rssignal.signal_cli import (
    AccountNotLinked,
    LinkPreview,
    SignalCliNotFound,
    SignalGroup,
    SignalSendError,
    check_account,
    find_signal_cli,
    is_account_registered,
    link_device,
    list_accounts,
    list_groups,
    receive,
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


def test_send_msg_with_voice_note_builds_correct_argv(have_binary, monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _completed(returncode=0)

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)

    send_msg(
        "episode text",
        recipient="+31611111111",
        account="+31600000000",
        attachments=["/tmp/ep.mp3"],
        voice_note=True,
    )

    assert calls["argv"] == [
        FAKE_BIN,
        "-a",
        "+31600000000",
        "send",
        "--attachment",
        "/tmp/ep.mp3",
        "--voice-note",
        "-m",
        "episode text",
        "+31611111111",
    ]
    # Recipient stays the final positional, not swallowed by --attachment.
    assert calls["argv"][-1] == "+31611111111"


def test_send_msg_no_attachments_argv_unchanged(have_binary, monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _completed(returncode=0)

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)

    send_msg("hello", recipient="+31611111111", account="+31600000000")

    assert "--attachment" not in calls["argv"]
    assert "--voice-note" not in calls["argv"]
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


# --- groups ----------------------------------------------------------------

# Made-up output in signal-cli's real shape: the INFO line it writes on a cold
# start, and names with spaces, an ampersand, and an emoji. The ids are fake but
# use the same base64 alphabet (including / and +) as real ones.
GROUPS_OUTPUT = """INFO  AccountHelper - The Signal protocol expects that incoming messages are regularly received.
Id: AAAA1111bbbb+cccc/dddd2222eeee3333ffff4444g= Name: Book club & friends  Active: true Blocked: false
Id: BBBB2222cccc/dddd+eeee3333ffff4444gggg5555h= Name: 🎉 party 3.0  Active: true Blocked: false
Id: CCCC3333dddd+eeee/ffff4444gggg5555hhhh6666i= Name: Old crew  Active: false Blocked: false
Id: DDDD4444eeee/ffff+gggg5555hhhh6666iiii7777j= Name: Spam group  Active: true Blocked: true
"""


def _patch_groups_output(monkeypatch, output=GROUPS_OUTPUT, returncode=0):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _completed(returncode=returncode, stdout=output, stderr="boom")

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)
    return calls


def test_list_groups_parses_output(have_binary, monkeypatch):
    calls = _patch_groups_output(monkeypatch)

    groups = list_groups(account="+31600000000")

    assert calls["argv"] == [FAKE_BIN, "-a", "+31600000000", "listGroups"]
    assert groups[0] == SignalGroup(
        id="AAAA1111bbbb+cccc/dddd2222eeee3333ffff4444g=",
        name="Book club & friends",
        active=True,
        blocked=False,
    )
    # Log lines are ignored, not mistaken for groups.
    assert len(groups) == 4
    assert groups[1].name == "🎉 party 3.0"
    assert groups[2].active is False
    assert groups[3].blocked is True


def test_list_groups_recipient_is_prefixed(have_binary, monkeypatch):
    _patch_groups_output(monkeypatch)
    group = list_groups(account="+31600000000")[0]
    assert group.recipient == f"group:{group.id}"


def test_list_groups_uses_configured_account(have_binary, monkeypatch):
    monkeypatch.setattr(
        signal_cli,
        "get_config",
        lambda: SimpleNamespace(account="+31600000000", recipient=None),
    )
    calls = _patch_groups_output(monkeypatch)

    list_groups()

    assert calls["argv"][2] == "+31600000000"


def test_list_groups_failure_raises(have_binary, monkeypatch):
    _patch_groups_output(monkeypatch, output="", returncode=1)
    with pytest.raises(SignalSendError):
        list_groups(account="+31600000000")


def test_receive_builds_correct_argv(have_binary, monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _completed(returncode=0, stdout="Envelope from: ...")

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)

    receive(account="+31600000000", timeout=5)

    assert calls["argv"] == [
        FAKE_BIN,
        "-a",
        "+31600000000",
        "receive",
        "--timeout",
        "5",
    ]


def test_receive_failure_raises(have_binary, monkeypatch):
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda *a, **k: _completed(returncode=1, stderr="boom"),
    )
    with pytest.raises(SignalSendError):
        receive(account="+31600000000")


def test_send_msg_to_group_uses_g_flag(have_binary, monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _completed(returncode=0)

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)

    send_msg("hi", recipient="group:abc123=", account="+31600000000")

    assert calls["argv"] == [
        FAKE_BIN,
        "-a",
        "+31600000000",
        "send",
        "-m",
        "hi",
        "-g",
        "abc123=",
    ]


def test_send_msg_to_group_with_voice_note(have_binary, monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _completed(returncode=0)

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)

    send_msg(
        "episode",
        recipient="group:abc123=",
        account="+31600000000",
        attachments=["/tmp/ep.mp3"],
        voice_note=True,
    )

    # -g stays last, so --attachment's greedy arg list can't swallow it.
    assert calls["argv"][-2:] == ["-g", "abc123="]
    assert "--voice-note" in calls["argv"]


def test_send_msg_bare_group_id_raises(have_binary, monkeypatch):
    monkeypatch.setattr(signal_cli.subprocess, "run", lambda *a, **k: _completed())
    with pytest.raises(ValueError) as excinfo:
        send_msg("hi", recipient="AAAA1111bbbb=", account="+31600000000")
    assert "group:" in str(excinfo.value)


# --- link previews ---------------------------------------------------------


def _capture_argv(monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _completed(returncode=0)

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)
    return calls


def test_send_msg_with_full_preview(have_binary, monkeypatch):
    calls = _capture_argv(monkeypatch)

    send_msg(
        "Episode 402\n\nhttps://a/402",
        recipient="+31611111111",
        account="+31600000000",
        preview=LinkPreview(
            url="https://a/402",
            title="Episode 402",
            description="Show notes",
            image="/tmp/art.jpg",
        ),
    )

    assert calls["argv"] == [
        FAKE_BIN,
        "-a",
        "+31600000000",
        "send",
        "--preview-url",
        "https://a/402",
        "--preview-title",
        "Episode 402",
        "--preview-description",
        "Show notes",
        "--preview-image",
        "/tmp/art.jpg",
        "-m",
        "Episode 402\n\nhttps://a/402",
        "+31611111111",
    ]


def test_send_msg_omits_empty_preview_options(have_binary, monkeypatch):
    calls = _capture_argv(monkeypatch)

    send_msg(
        "hi https://a/1",
        recipient="+31611111111",
        account="+31600000000",
        preview=LinkPreview(url="https://a/1", title="One"),
    )

    assert "--preview-description" not in calls["argv"]
    assert "--preview-image" not in calls["argv"]
    assert "--preview-url" in calls["argv"]


def test_send_msg_preview_with_voice_note_to_group(have_binary, monkeypatch):
    calls = _capture_argv(monkeypatch)

    send_msg(
        "Episode https://a/402",
        recipient="group:abc123=",
        account="+31600000000",
        attachments=["/tmp/ep.mp3"],
        voice_note=True,
        preview=LinkPreview(url="https://a/402", title="Ep", image="/tmp/art.jpg"),
    )

    argv = calls["argv"]
    # The preview flags are fixed-arity and sit past --attachment's greedy list,
    # so the mp3 isn't swallowed and -g still ends the line.
    assert argv[argv.index("--attachment") + 1] == "/tmp/ep.mp3"
    assert argv[-2:] == ["-g", "abc123="]
    assert argv.index("--preview-url") > argv.index("--voice-note")
    assert argv.index("--preview-image") < argv.index("-m")


def test_link_preview_requires_a_url_and_title():
    with pytest.raises(ValueError):
        LinkPreview(url="", title="Ep")
    with pytest.raises(ValueError):
        LinkPreview(url="https://a/1", title="")


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
