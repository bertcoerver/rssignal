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
from types import SimpleNamespace

import pytest

from rssignal import signal_cli
from rssignal.signal_cli import LinkPreview, SignalError, send_msg, update_group

FAKE_BIN = "/usr/local/bin/signal-cli"


@pytest.fixture
def have_binary(monkeypatch):
    """Pretend signal-cli is installed, for the fall-back path to reach for."""
    monkeypatch.setattr(signal_cli.shutil, "which", lambda name: FAKE_BIN)


class FakeRpc:
    """Stands in for a running signal-cli, recording what it was asked."""

    def __init__(self, result=None):
        self.calls: list[tuple[str, dict]] = []
        self.result = result if result is not None else []

    def request(self, method, params=None, *, timeout):
        self.calls.append((method, params or {}))
        return self.result


@pytest.fixture
def running(monkeypatch, have_binary):
    """Install a fake daemon as the one in use, and hand it back.

    A send looks for the binary even with a daemon up, to have the one-shot
    path ready should the daemon die — so without ``have_binary`` these pass
    only on a machine that happens to have signal-cli installed.
    """
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


# --- a daemon that dies mid-run --------------------------------------------


class DeadRpc:
    """A daemon whose process has gone, as every call after that would see it."""

    def __init__(self) -> None:
        self.calls = 0
        self.stopped = False

    def request(self, method, params=None, *, timeout):
        self.calls += 1
        raise signal_cli.DaemonGone(
            f"`signal-cli {method}` got no answer — signal-cli stopped."
        )

    def stop(self) -> None:
        self.stopped = True


def test_a_waiter_released_with_no_reply_is_a_dead_daemon():
    # What _read does when signal-cli's stdout closes: wake everyone waiting,
    # with nothing to give them. That is the JVM having died, not a slow send —
    # and it must be told apart from the timeout below, which is a live daemon
    # taking too long.
    rpc = signal_cli._JsonRpc("+31600000000")

    class FakeProc:
        class stdin:
            @staticmethod
            def write(text):
                key = json.loads(text)["id"]
                rpc._events[key].set()  # Released, but nothing in _pending.

            @staticmethod
            def flush():
                pass

    rpc._proc = FakeProc()

    with pytest.raises(signal_cli.DaemonGone):
        rpc.request("send", {}, timeout=5)


def test_a_daemon_that_is_merely_slow_is_not_declared_dead():
    rpc = signal_cli._JsonRpc("+31600000000")

    class FakeProc:
        class stdin:
            @staticmethod
            def write(text):
                pass  # Alive, just not answering yet.

            @staticmethod
            def flush():
                pass

    rpc._proc = FakeProc()

    with pytest.raises(signal_cli.SignalSendError) as excinfo:
        rpc.request("send", {}, timeout=0.01)
    assert not isinstance(excinfo.value, signal_cli.DaemonGone)


def test_a_send_falls_back_to_one_shot_when_the_daemon_dies(
    have_binary, monkeypatch, capsys
):
    dead = DeadRpc()
    monkeypatch.setattr(signal_cli, "_daemon", dead)
    argv_used = []
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda argv, **k: argv_used.append(argv)
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    send_msg("hi", recipient="+31611111111", account="+31600000000")

    # The message went out the slow way rather than being lost.
    assert argv_used and "send" in argv_used[0]
    assert dead.stopped
    assert "Carrying on with one signal-cli per call" in capsys.readouterr().err


def test_the_dead_daemon_is_not_consulted_again(have_binary, monkeypatch, capsys):
    # The reason this matters: without retiring it, every feed left in the run
    # rediscovers the same corpse and fails on it.
    dead = DeadRpc()
    monkeypatch.setattr(signal_cli, "_daemon", dead)
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    send_msg("one", recipient="+31611111111", account="+31600000000")
    send_msg("two", recipient="+31611111111", account="+31600000000")

    assert dead.calls == 1
    assert signal_cli._daemon is None
    capsys.readouterr()


def test_a_dead_daemon_does_not_make_a_send_retry(have_binary, monkeypatch, capsys):
    # DaemonGone is a SignalSendError, so it goes past the retry loop — which
    # must not treat it as a blip and ask the dead pipe twice more.
    dead = DeadRpc()
    monkeypatch.setattr(signal_cli, "_daemon", dead)
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    send_msg("hi", recipient="+31611111111", account="+31600000000")

    assert dead.calls == 1
    capsys.readouterr()


def test_update_group_falls_back_to_one_shot_when_the_daemon_dies(
    have_binary, monkeypatch, capsys
):
    # The watermark rides on updateGroup, so losing this one loses a feed's
    # place — the one call in a run that must not quietly fail.
    dead = DeadRpc()
    monkeypatch.setattr(signal_cli, "_daemon", dead)
    argv_used = []
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda argv, **k: argv_used.append(argv)
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    update_group("G=", description="a blurb", account="+31600000000")

    assert argv_used and "updateGroup" in argv_used[0]
    assert "--description" in argv_used[0]
    capsys.readouterr()


def test_list_groups_falls_back_to_one_shot_when_the_daemon_dies(
    have_binary, monkeypatch, capsys
):
    dead = DeadRpc()
    monkeypatch.setattr(signal_cli, "_daemon", dead)
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [{"id": "G=", "name": "A Feed", "isMember": True, "description": "b"}]
            ),
            stderr="",
        ),
    )

    groups = signal_cli.list_groups(account="+31600000000")

    assert [g.name for g in groups] == ["A Feed"]
    capsys.readouterr()


# --- creating a group ------------------------------------------------------
#
# Unlike everything above, create_group going through the daemon is not a speed
# measure. signal-cli opens the account exclusively, so a one-shot `updateGroup`
# launched while the run's own jsonRpc process is up blocks on a lock held until
# the run ends and dies on its timeout. That is a bug rssignal shipped: every
# feed whose group did not already exist failed for as long as the daemon lived.


def _no_groups(monkeypatch, appearing=()):
    """Make list_groups return nothing, then `appearing` on later calls."""
    answers = iter([[], list(appearing)])

    def listing(*a, **k):
        try:
            return next(answers)
        except StopIteration:
            return list(appearing)

    monkeypatch.setattr(signal_cli, "list_groups", listing)


def _group(group_id, name="3Blue1Brown"):
    return signal_cli.SignalGroup(id=group_id, name=name, description="")


def test_creating_a_group_goes_through_the_daemon(have_binary, monkeypatch):
    fake = FakeRpc(result={"groupId": "NEW="})
    monkeypatch.setattr(signal_cli, "_daemon", fake)
    _no_groups(monkeypatch)
    monkeypatch.setattr(signal_cli, "update_group", lambda *a, **k: None)
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda *a, **k: pytest.fail("a one-shot signal-cli would deadlock here"),
    )

    group = signal_cli.create_group("3Blue1Brown", account="+31600000000")

    assert group.id == "NEW="
    method, params = fake.calls[0]
    assert method == "updateGroup"
    assert params["name"] == "3Blue1Brown"


def test_the_announcement_flag_survives_the_daemon(have_binary, monkeypatch):
    # camelCase of --set-permission-send-messages. Wrong here and every feed
    # group silently lets anyone post.
    fake = FakeRpc(result={"groupId": "NEW="})
    monkeypatch.setattr(signal_cli, "_daemon", fake)
    _no_groups(monkeypatch)
    monkeypatch.setattr(signal_cli, "update_group", lambda *a, **k: None)

    signal_cli.create_group(
        "3Blue1Brown", announcement_only=True, account="+31600000000"
    )

    _, params = fake.calls[0]
    assert params["setPermissionSendMessages"] == "only-admins"


def test_a_created_group_with_an_unreadable_id_is_found_by_elimination(
    have_binary, monkeypatch
):
    fake = FakeRpc(result=None)
    monkeypatch.setattr(signal_cli, "_daemon", fake)
    _no_groups(monkeypatch, appearing=[_group("FOUND=")])
    monkeypatch.setattr(signal_cli, "update_group", lambda *a, **k: None)
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda *a, **k: pytest.fail("the group already exists; asking again duplicates it"),
    )

    group = signal_cli.create_group("3Blue1Brown", account="+31600000000")

    assert group.id == "FOUND="


def test_a_daemon_that_dies_creating_a_group_does_not_create_a_second(
    have_binary, monkeypatch, capsys
):
    # The request may have been acted on before the process went. Asking the
    # listing is the only way to tell, and a blind retry would leave the user
    # with two groups of the same name and a feed watermarking one of them.
    dead = DeadRpc()
    monkeypatch.setattr(signal_cli, "_daemon", dead)
    _no_groups(monkeypatch, appearing=[_group("MADE=")])
    monkeypatch.setattr(signal_cli, "update_group", lambda *a, **k: None)
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda *a, **k: pytest.fail("it was already created; this makes a duplicate"),
    )

    group = signal_cli.create_group("3Blue1Brown", account="+31600000000")

    assert group.id == "MADE="
    capsys.readouterr()


def test_a_daemon_that_died_before_creating_falls_back_to_one_shot(
    have_binary, monkeypatch, capsys
):
    dead = DeadRpc()
    monkeypatch.setattr(signal_cli, "_daemon", dead)
    _no_groups(monkeypatch)
    monkeypatch.setattr(signal_cli, "update_group", lambda *a, **k: None)
    argv_used = []
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda argv, **k: argv_used.append(argv)
        or SimpleNamespace(
            returncode=0, stdout=json.dumps({"groupId": "SLOW="}), stderr=""
        ),
    )

    group = signal_cli.create_group("3Blue1Brown", account="+31600000000")

    assert group.id == "SLOW="
    assert argv_used and "--name" in argv_used[0]


def test_without_a_daemon_creating_a_group_is_unchanged(have_binary, monkeypatch):
    monkeypatch.setattr(signal_cli, "_daemon", None)
    _no_groups(monkeypatch)
    monkeypatch.setattr(signal_cli, "update_group", lambda *a, **k: None)
    argv_used = []
    monkeypatch.setattr(
        signal_cli.subprocess,
        "run",
        lambda argv, **k: argv_used.append(argv)
        or SimpleNamespace(
            returncode=0, stdout=json.dumps({"groupId": "OLD="}), stderr=""
        ),
    )

    group = signal_cli.create_group("3Blue1Brown", account="+31600000000")

    assert group.id == "OLD="
    assert "--name" in argv_used[0]
