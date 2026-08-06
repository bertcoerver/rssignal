"""Tests for keeping one signal-cli alive across a run.

The daemon exists only to be faster, so the thing worth testing hardest is that
it never becomes a way for a run to fail: a daemon that won't start, dies
mid-run, or is switched off must leave the one-shot path doing exactly what it
did before. The rest checks that a request comes out as the JSON-RPC signal-cli
documents — camelCase of each cli flag — since a wrong parameter name is a
message that silently never arrives.
"""

from __future__ import annotations

import json

import pytest

from rssignal import signal_cli
from rssignal.signal_cli import LinkPreview, SignalError, send_msg, update_group


class FakeRpc:
    """Stands in for a running signal-cli, recording what it was asked."""

    def __init__(self, result=None):
        self.calls: list[tuple[str, dict]] = []
        self.result = result if result is not None else []

    def request(self, method, params=None, *, timeout):
        self.calls.append((method, params or {}))
        return self.result


@pytest.fixture
def running(monkeypatch):
    """Install a fake daemon as the one in use, and hand it back."""
    fake = FakeRpc()
    monkeypatch.setattr(signal_cli, "_daemon", fake)
    return fake


def test_a_group_message_goes_out_as_group_id(running):
    send_msg("hello", recipient="group:AbC123=", account="+31600000000")

    method, params = running.calls[0]
    assert method == "send"
    assert params["groupId"] == "AbC123="
    assert params["message"] == "hello"
    assert "recipient" not in params


def test_a_number_recipient_goes_out_as_a_list(running):
    send_msg("hello", recipient="+31611111111", account="+31600000000")

    _, params = running.calls[0]
    assert params["recipient"] == ["+31611111111"]
    assert "groupId" not in params


def test_a_voice_note_with_attachments(running, tmp_path):
    audio = tmp_path / "ep.mp3"
    audio.write_bytes(b"x")

    send_msg(
        "Episode",
        recipient="group:G=",
        account="+31600000000",
        attachments=[str(audio)],
        voice_note=True,
    )

    _, params = running.calls[0]
    assert params["attachments"] == [str(audio)]
    assert params["voiceNote"] is True


def test_a_preview_uses_the_camelcase_names(running, tmp_path):
    image = tmp_path / "art.jpg"
    image.write_bytes(b"x")

    send_msg(
        "See https://example.com/x",
        recipient="group:G=",
        account="+31600000000",
        preview=LinkPreview(
            url="https://example.com/x",
            title="A title",
            description="A description",
            image=str(image),
        ),
    )

    _, params = running.calls[0]
    assert params["previewUrl"] == "https://example.com/x"
    assert params["previewTitle"] == "A title"
    assert params["previewDescription"] == "A description"
    assert params["previewImage"] == str(image)


def test_an_empty_preview_field_is_left_out(running):
    send_msg(
        "See https://example.com/x",
        recipient="group:G=",
        account="+31600000000",
        preview=LinkPreview(
            url="https://example.com/x", title="T", description="", image=""
        ),
    )

    _, params = running.calls[0]
    assert "previewDescription" not in params
    assert "previewImage" not in params


def test_update_group_sends_the_description(running):
    update_group("G=", description="a blurb", account="+31600000000")

    method, params = running.calls[0]
    assert method == "updateGroup"
    assert params == {"groupId": "G=", "description": "a blurb"}


def test_a_bad_recipient_still_raises_before_anything_is_sent(running):
    with pytest.raises(ValueError):
        send_msg("hi", recipient="not-a-number", account="+31600000000")
    assert running.calls == []


def test_list_groups_reads_the_daemons_objects(monkeypatch):
    monkeypatch.setattr(
        signal_cli,
        "_daemon",
        FakeRpc(
            result=[
                {
                    "id": "G=",
                    "name": "A Feed",
                    "isMember": True,
                    "isBlocked": False,
                    "description": "blurb",
                }
            ]
        ),
    )
    groups = signal_cli.list_groups(account="+31600000000")

    assert len(groups) == 1
    assert groups[0].id == "G="
    assert groups[0].name == "A Feed"
    assert groups[0].description == "blurb"
    assert groups[0].active is True


def test_receive_makes_no_request_in_daemon_mode(running, monkeypatch):
    """The daemon receives on its own; only the settle window remains."""
    slept: list[float] = []
    monkeypatch.setattr(signal_cli.time, "sleep", lambda s: slept.append(s))

    signal_cli.receive(account="+31600000000", timeout=3)

    assert running.calls == []
    assert slept == [3]


def test_the_daemon_can_be_switched_off(monkeypatch):
    monkeypatch.setenv(signal_cli.DAEMON_ENV, "0")
    with signal_cli.daemon(account="+31600000000") as d:
        assert d is None
        assert signal_cli._daemon is None


def test_a_daemon_that_will_not_start_falls_back_quietly(monkeypatch):
    monkeypatch.delenv(signal_cli.DAEMON_ENV, raising=False)
    monkeypatch.setattr(
        signal_cli._JsonRpc,
        "start",
        lambda self: (_ for _ in ()).throw(SignalError("no binary")),
    )
    # The point: a run gets a working (slower) path, not an exception.
    with signal_cli.daemon(account="+31600000000") as d:
        assert d is None
        assert signal_cli._daemon is None


def test_a_request_is_well_formed_json_rpc(monkeypatch):
    """The wire format, checked against a pipe rather than a real signal-cli."""
    written: list[str] = []

    class FakeStdin:
        def write(self, text):
            written.append(text)

        def flush(self):
            pass

    rpc = signal_cli._JsonRpc("+31600000000")

    class FakeProc:
        stdin = FakeStdin()

    rpc._proc = FakeProc()

    # Answer the request the moment it is written, as the reader thread would.
    def answer(text):
        written.append(text)
        key = json.loads(text)["id"]
        rpc._pending[key] = {"jsonrpc": "2.0", "id": key, "result": "ok"}
        rpc._events[key].set()

    FakeProc.stdin.write = answer

    assert rpc.request("listGroups", {"groupId": "G="}, timeout=5) == "ok"

    sent = json.loads(written[0])
    assert sent["jsonrpc"] == "2.0"
    assert sent["method"] == "listGroups"
    assert sent["params"] == {"groupId": "G="}
    assert sent["id"]


def test_an_error_reply_becomes_a_signal_error(monkeypatch):
    rpc = signal_cli._JsonRpc("+31600000000")

    class FakeProc:
        class stdin:
            @staticmethod
            def write(text):
                key = json.loads(text)["id"]
                rpc._pending[key] = {
                    "id": key,
                    "error": {"code": -1, "message": "No recipients given"},
                }
                rpc._events[key].set()

            @staticmethod
            def flush():
                pass

    rpc._proc = FakeProc()

    with pytest.raises(signal_cli.SignalSendError, match="No recipients given"):
        rpc.request("send", {}, timeout=5)
