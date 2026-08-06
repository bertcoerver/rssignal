"""Tests for rssignal.signal_cli (all subprocess calls are mocked)."""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from rssignal import signal_cli
from rssignal.signal_cli import (
    AccountNotLinked,
    LinkPreview,
    SignalCliNotFound,
    SignalError,
    SignalGroup,
    SignalSendError,
    check_account,
    create_group,
    find_group,
    find_signal_cli,
    is_account_registered,
    link_device,
    list_accounts,
    list_groups,
    match_group,
    receive,
    send_msg,
    set_group_avatar,
    update_group,
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
        calls.setdefault("argvs", []).append(argv)
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
        calls.setdefault("argvs", []).append(argv)
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


def test_send_msg_retries_a_network_failure(have_binary, monkeypatch):
    # The failure the podcast feeds actually hit: Signal resetting the
    # connection partway through an upload. The message never arrived, so
    # asking again is both safe and usually enough.
    reset = (
        "Failed to send message: java.net.SocketException: Connection reset "
        "(PushNetworkException) (UnexpectedErrorException)"
    )
    calls = []

    def fake_run(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            return _completed(returncode=1, stderr=reset)
        return _completed()

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)
    send_msg("hi", recipient="+31611111111", account="+31600000000")
    assert len(calls) == 2


def test_send_msg_gives_up_after_the_last_attempt(have_binary, monkeypatch):
    reset = "java.net.SocketException: Connection reset (PushNetworkException)"
    calls = []

    def fake_run(*a, **k):
        calls.append(a)
        return _completed(returncode=1, stderr=reset)

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)
    with pytest.raises(SignalSendError):
        send_msg("hi", recipient="+31611111111", account="+31600000000")
    assert len(calls) == signal_cli.SEND_RETRY_ATTEMPTS


def test_send_msg_does_not_retry_a_real_refusal(have_binary, monkeypatch):
    # A group that won't take the message will not take it on the third try
    # either — retrying that is three times the wait for the same answer.
    calls = []

    def fake_run(*a, **k):
        calls.append(a)
        return _completed(returncode=1, stderr="Unknown group id")

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)
    with pytest.raises(SignalSendError):
        send_msg("hi", recipient="+31611111111", account="+31600000000")
    assert len(calls) == 1


def test_send_msg_timeout_raises(have_binary, monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="signal-cli", timeout=60)

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)
    with pytest.raises(SignalSendError):
        send_msg("hi", recipient="+31611111111", account="+31600000000")


# --- groups ----------------------------------------------------------------

# Made-up output in signal-cli's real JSON shape: names with spaces, an
# ampersand, and an emoji, a group left (isMember false) and one blocked. The
# ids are fake but use the same base64 alphabet (including / and +) as real ones.
GROUPS_JSON = json.dumps(
    [
        {
            "id": "AAAA1111bbbb+cccc/dddd2222eeee3333ffff4444g=",
            "name": "Book club & friends",
            "description": "Six people, one book, no discipline.",
            "isMember": True,
            "isBlocked": False,
        },
        {
            "id": "BBBB2222cccc/dddd+eeee3333ffff4444gggg5555h=",
            "name": "\U0001f389 party 3.0",
            "description": "",
            "isMember": True,
            "isBlocked": False,
        },
        {
            "id": "CCCC3333dddd+eeee/ffff4444gggg5555hhhh6666i=",
            "name": "Old crew",
            "description": None,
            "isMember": False,
            "isBlocked": False,
        },
        {
            "id": "DDDD4444eeee/ffff+gggg5555hhhh6666iiii7777j=",
            "name": "Spam group",
            "isMember": True,
            "isBlocked": True,
        },
    ]
)


def _patch_groups_output(monkeypatch, output=GROUPS_JSON, returncode=0):
    calls = {}

    def fake_run(argv, **kwargs):
        calls.setdefault("argvs", []).append(argv)
        calls["argv"] = argv
        return _completed(returncode=returncode, stdout=output, stderr="boom")

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)
    return calls


def test_list_groups_parses_output(have_binary, monkeypatch):
    calls = _patch_groups_output(monkeypatch)

    groups = list_groups(account="+31600000000")

    # JSON, not the plain listing: the plain one omits the description entirely,
    # and a watermarked description is multi-line.
    assert calls["argv"] == [
        FAKE_BIN, "-o", "json", "-a", "+31600000000", "listGroups"
    ]
    assert groups[0] == SignalGroup(
        id="AAAA1111bbbb+cccc/dddd2222eeee3333ffff4444g=",
        name="Book club & friends",
        active=True,
        blocked=False,
        description="Six people, one book, no discipline.",
    )
    assert len(groups) == 4
    assert groups[1].name == "\U0001f389 party 3.0"
    assert groups[2].active is False
    assert groups[3].blocked is True


def test_list_groups_tolerates_a_missing_description(have_binary, monkeypatch):
    # signal-cli sends null for a group with no blurb, and older versions omit
    # the key. Neither should become the string "None".
    _patch_groups_output(monkeypatch)
    groups = list_groups(account="+31600000000")
    assert groups[2].description == ""  # explicit null
    assert groups[3].description == ""  # key absent


def test_list_groups_of_no_groups_is_empty(have_binary, monkeypatch):
    _patch_groups_output(monkeypatch, output="[]")
    assert list_groups(account="+31600000000") == []


def test_list_groups_unreadable_output_raises(have_binary, monkeypatch):
    _patch_groups_output(monkeypatch, output="not json at all")
    with pytest.raises(SignalError):
        list_groups(account="+31600000000")


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

    assert "+31600000000" in calls["argv"]


def test_list_groups_failure_raises(have_binary, monkeypatch):
    _patch_groups_output(monkeypatch, output="", returncode=1)
    with pytest.raises(SignalSendError):
        list_groups(account="+31600000000")


def test_receive_builds_correct_argv(have_binary, monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls.setdefault("argvs", []).append(argv)
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
        calls.setdefault("argvs", []).append(argv)
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
        calls.setdefault("argvs", []).append(argv)
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
        calls.setdefault("argvs", []).append(argv)
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


# --- preview images ---------------------------------------------------------
#
# Unlike a group avatar, a link preview's image is *not* size-limited: 1920x1920
# artwork was measured arriving intact on a card, so it goes out untouched
# rather than needlessly downscaled.


def test_send_msg_sends_a_preview_image_at_its_original_size(
    have_binary, monkeypatch, tmp_path
):
    big = str(tmp_path / "art.jpg")
    Image.new("RGB", (1920, 1920), "red").save(big, "JPEG")
    calls = _capture_argv(monkeypatch)

    send_msg(
        "Episode\n\nhttps://a/1",
        recipient="+31611111111",
        account="+31600000000",
        preview=LinkPreview(url="https://a/1", title="Episode", image=big),
    )

    sent = calls["argv"][calls["argv"].index("--preview-image") + 1]
    assert sent == big
    with Image.open(sent) as img:
        assert img.size == (1920, 1920)


def test_send_msg_keeps_the_preview_image_ahead_of_the_message(
    have_binary, monkeypatch, tmp_path
):
    # The flag has to sit past --attachment's greedy arg list and ahead of -m.
    big = str(tmp_path / "art.jpg")
    Image.new("RGB", (1920, 1920), "red").save(big, "JPEG")
    calls = _capture_argv(monkeypatch)

    send_msg(
        "Episode\n\nhttps://a/1",
        recipient="group:ZZZ=",
        account="+31600000000",
        attachments=["/tmp/ep.mp3"],
        voice_note=True,
        preview=LinkPreview(url="https://a/1", title="Episode", image=big),
    )

    argv = calls["argv"]
    assert argv.index("--attachment") < argv.index("--preview-image") < argv.index("-m")


# --- over-long message bodies ----------------------------------------------


def test_send_msg_leaves_a_body_within_the_limit_alone(have_binary, monkeypatch):
    calls = _capture_argv(monkeypatch)
    text = "Episode 402\n\nhttps://a/402"

    send_msg(text, recipient="+31611111111", account="+31600000000")

    assert calls["argv"][calls["argv"].index("-m") + 1] == text


def test_send_msg_shortens_an_over_long_body(have_binary, monkeypatch):
    calls = _capture_argv(monkeypatch)

    send_msg("word " * 600, recipient="+31611111111", account="+31600000000")

    body = calls["argv"][calls["argv"].index("-m") + 1]
    assert len(body.encode()) <= signal_cli.MESSAGE_MAX_BYTES
    assert body.endswith("…")


def test_send_msg_keeps_the_preview_url_in_an_over_long_body(have_binary, monkeypatch):
    # Signal has nothing to draw the card from once the url is gone, so a long
    # set of show notes loses its tail rather than its link.
    calls = _capture_argv(monkeypatch)
    url = "https://a/sons/a-very-long-episode"

    send_msg(
        f"{'word ' * 600}\n\n{url}",
        recipient="+31611111111",
        account="+31600000000",
        preview=LinkPreview(url=url, title="A very long episode"),
    )

    body = calls["argv"][calls["argv"].index("-m") + 1]
    assert body.endswith(url)
    assert len(body.encode()) <= signal_cli.MESSAGE_MAX_BYTES


def test_send_msg_counts_bytes_not_characters(have_binary, monkeypatch):
    # The bug this replaced: French show notes clipped to 1997 *characters* went
    # out at 2105 bytes and lost their card. Curly quotes cost three bytes each.
    calls = _capture_argv(monkeypatch)
    url = "https://a/1"

    send_msg(
        f"{'“mot” ' * 500}\n\n{url}",
        recipient="+31611111111",
        account="+31600000000",
        preview=LinkPreview(url=url, title="Un épisode"),
    )

    body = calls["argv"][calls["argv"].index("-m") + 1]
    assert len(body.encode()) <= signal_cli.MESSAGE_MAX_BYTES
    assert len(body) < signal_cli.MESSAGE_MAX_BYTES  # far fewer characters fit
    assert body.endswith(url)


def test_clip_body_counts_the_url_against_the_budget(monkeypatch):
    # The url is reserved out of the limit rather than added on top of it.
    monkeypatch.setattr(signal_cli, "MESSAGE_MAX_BYTES", 60)
    url = "https://a/some-episode"

    body = signal_cli.clip_body(
        f"{'word ' * 40}\n\n{url}", url
    )

    assert body.endswith(url)
    assert len(body.encode()) <= 60


def test_clip_body_counts_a_non_ascii_url_in_bytes(monkeypatch):
    # An internationalised url costs more than its character count, and the
    # budget has to notice or the body lands back over the limit.
    monkeypatch.setattr(signal_cli, "MESSAGE_MAX_BYTES", 60)
    url = "https://a/épisode-très-écouté"

    body = signal_cli.clip_body(
        f"{'word ' * 40}\n\n{url}", url
    )

    assert body.endswith(url)
    assert len(body.encode()) <= 60


def test_clip_body_shortens_on_a_word_boundary(monkeypatch):
    monkeypatch.setattr(signal_cli, "MESSAGE_MAX_BYTES", 14)
    assert signal_cli.clip_body("alpha beta gamma delta", None) == "alpha beta…"


def test_clip_body_hard_cuts_when_the_url_leaves_no_room(monkeypatch):
    # Nothing sensible survives, but the url still has to: a card with no body
    # beats no card at all.
    monkeypatch.setattr(signal_cli, "MESSAGE_MAX_BYTES", 20)
    url = "https://a/an-episode-with-a-long-path"

    body = signal_cli.clip_body(f"notes\n\n{url}", url)

    assert body == url


def test_clip_body_falls_back_when_the_url_is_missing_from_the_body(monkeypatch):
    # signal-cli rejects this preview anyway; there is nothing to preserve.
    monkeypatch.setattr(signal_cli, "MESSAGE_MAX_BYTES", 14)

    body = signal_cli.clip_body(
        "alpha beta gamma delta", "https://a/1"
    )

    assert body == "alpha beta…"


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


# --- create_group ----------------------------------------------------------


def _patch_create(monkeypatch, *, stdout="", returncode=0, existing=(), after=None):
    """Patch subprocess.run and list_groups around create_group."""
    calls = {}

    def fake_run(argv, **kwargs):
        calls.setdefault("argvs", []).append(argv)
        calls["argv"] = argv
        return _completed(returncode=returncode, stdout=stdout, stderr="boom")

    seen = list(existing)
    listings = [seen, list(after) if after is not None else seen]

    def fake_list_groups(*, account=None):
        calls.setdefault("list_accounts", []).append(account)
        return listings.pop(0) if listings else seen

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(signal_cli, "list_groups", fake_list_groups)
    return calls


def test_create_group_builds_correct_argv(have_binary, monkeypatch):
    calls = _patch_create(monkeypatch, stdout='{"groupId": "ZZZ="}')

    group = create_group("Feed group", account="+31600000000")

    assert calls["argvs"][0] == [
        FAKE_BIN,
        "-o",
        "json",
        "-a",
        "+31600000000",
        "updateGroup",
        "--name",
        "Feed group",
    ]
    assert group == SignalGroup(id="ZZZ=", name="Feed group")
    assert group.recipient == "group:ZZZ="


def test_create_group_never_adds_members(have_binary, monkeypatch):
    # The whole point of this command: it must not be able to add anyone.
    calls = _patch_create(monkeypatch, stdout='{"groupId": "ZZZ="}')

    create_group("Just me", description="d", account="+31600000000")

    argv = calls["argv"]
    assert "-m" not in argv
    assert "--member" not in argv
    assert "--admin" not in argv
    # Nor does it read the contact store to find anyone.
    assert "listContacts" not in argv


def test_create_group_omits_group_id_so_signal_cli_creates_one(have_binary, monkeypatch):
    calls = _patch_create(monkeypatch, stdout='{"groupId": "ZZZ="}')
    create_group("New", account="+31600000000")
    # Passing -g would edit an existing group instead of creating one.
    create_argv = calls["argvs"][0]
    assert "-g" not in create_argv
    assert "--group-id" not in create_argv


def test_create_group_passes_description_avatar_and_announcement(
    have_binary, monkeypatch, tmp_path
):
    avatar = tmp_path / "art.jpg"
    avatar.write_bytes(b"jpg")
    calls = _patch_create(monkeypatch, stdout='{"groupId": "ZZZ="}')

    create_group(
        "Pod",
        description="episodes land here",
        avatar=str(avatar),
        announcement_only=True,
        account="+31600000000",
    )

    # The name and permission take effect on the creating call...
    create_argv, decorate_argv = calls["argvs"]
    assert (
        create_argv[create_argv.index("--set-permission-send-messages") + 1]
        == "only-admins"
    )
    assert "--avatar" not in create_argv
    assert "--description" not in create_argv

    # ...the description and picture go in one second call, against the new id.
    assert decorate_argv[decorate_argv.index("--group-id") + 1] == "ZZZ="
    assert decorate_argv[decorate_argv.index("--description") + 1] == "episodes land here"
    assert decorate_argv[decorate_argv.index("--avatar") + 1] == str(avatar)


def test_create_group_always_sets_disappearing_messages(have_binary, monkeypatch):
    # Even with nothing to decorate, the second call still runs: a feed group is
    # a stream, not an archive, and it must not grow without limit.
    calls = _patch_create(monkeypatch, stdout='{"groupId": "ZZZ="}')

    create_group("Pod", announcement_only=True, account="+31600000000")

    create_argv, decorate_argv = calls["argvs"]
    assert "--expiration" not in create_argv
    assert decorate_argv[decorate_argv.index("--expiration") + 1] == str(
        signal_cli.GROUP_EXPIRATION_SECONDS
    )
    assert signal_cli.GROUP_EXPIRATION_SECONDS == 7 * 24 * 60 * 60


def test_create_group_keeps_the_group_when_the_avatar_fails(
    have_binary, monkeypatch, tmp_path, capsys
):
    # The picture is applied by a second command. Losing it is not a reason to
    # lose the group that was already created.
    avatar = tmp_path / "art.jpg"
    avatar.write_bytes(b"jpg")
    seen = {"n": 0}

    def fake_run(argv, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            return _completed(stdout='{"groupId": "ZZZ="}')
        return _completed(returncode=1, stderr="avatar upload failed")

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(signal_cli, "list_groups", lambda *, account=None: [])

    group = create_group("Pod", avatar=str(avatar), account="+31600000000")

    assert group == SignalGroup(id="ZZZ=", name="Pod")
    assert "setting its details failed" in capsys.readouterr().err


def test_set_group_avatar_builds_correct_argv(have_binary, monkeypatch, tmp_path):
    avatar = tmp_path / "art.jpg"
    avatar.write_bytes(b"jpg")
    calls = _patch_create(monkeypatch)

    set_group_avatar("ZZZ=", str(avatar), account="+31600000000")

    assert calls["argv"] == [
        FAKE_BIN,
        "-a",
        "+31600000000",
        "updateGroup",
        "--group-id",
        "ZZZ=",
        "--avatar",
        str(avatar),
    ]


def test_set_group_avatar_rejects_a_url(have_binary, monkeypatch):
    _patch_create(monkeypatch)
    with pytest.raises(ValueError, match="not a file"):
        set_group_avatar("ZZZ=", "https://example.com/a.jpg", account="+31600000000")


def test_create_group_rejects_an_avatar_that_is_not_a_file(have_binary, monkeypatch):
    _patch_create(monkeypatch, stdout='{"groupId": "ZZZ="}')
    with pytest.raises(ValueError, match="not a file"):
        create_group("Pod", avatar="https://example.com/art.jpg", account="+31600000000")


def test_create_group_rejects_a_blank_name(have_binary, monkeypatch):
    _patch_create(monkeypatch)
    with pytest.raises(ValueError, match="needs a name"):
        create_group("   ", account="+31600000000")


def test_create_group_falls_back_to_diffing_the_group_list(have_binary, monkeypatch):
    # Older signal-cli builds may not print the id; the new group is then the
    # one that wasn't there before.
    old = SignalGroup(id="aaa=", name="Book club")
    new = SignalGroup(id="bbb=", name="Pod")
    _patch_create(monkeypatch, stdout="Created new group.", existing=[old],
                  after=[old, new])

    assert create_group("Pod", account="+31600000000") == new


def test_create_group_reports_when_the_id_cannot_be_found(have_binary, monkeypatch):
    _patch_create(monkeypatch, stdout="", existing=[], after=[])
    with pytest.raises(SignalSendError, match="could not be determined"):
        create_group("Pod", account="+31600000000")


def test_create_group_failure_raises(have_binary, monkeypatch):
    _patch_create(monkeypatch, returncode=1)
    with pytest.raises(SignalSendError, match="updateGroup"):
        create_group("Pod", account="+31600000000")


def test_create_group_uses_configured_account(have_binary, monkeypatch):
    monkeypatch.setattr(
        signal_cli,
        "get_config",
        lambda: SimpleNamespace(account="+31600000000", recipient=None),
    )
    calls = _patch_create(monkeypatch, stdout='{"groupId": "ZZZ="}')

    create_group("Pod")

    assert calls["argv"][calls["argv"].index("-a") + 1] == "+31600000000"


# --- find_group ------------------------------------------------------------

_BOOK = SignalGroup(id="book=", name="Book club")


def _patch_lookup(monkeypatch, *listings):
    """Return successive listings from list_groups, recording receive calls."""
    calls = {"list": 0, "receive": 0}
    pages = [list(page) for page in listings]

    def fake_list_groups(*, account=None):
        calls["list"] += 1
        return pages[min(calls["list"] - 1, len(pages) - 1)]

    def fake_receive(*, account=None):
        calls["receive"] += 1

    monkeypatch.setattr(signal_cli, "list_groups", fake_list_groups)
    monkeypatch.setattr(signal_cli, "receive", fake_receive)
    return calls


def test_match_group_is_case_and_whitespace_insensitive():
    groups = [SignalGroup(id="x=", name="  Book Club  ")]
    assert match_group(groups, "book club").id == "x="


def test_match_group_skips_groups_you_left_or_blocked():
    groups = [
        SignalGroup(id="old=", name="Book club", active=False),
        SignalGroup(id="bad=", name="Book club", blocked=True),
    ]
    assert match_group(groups, "Book club") is None


def test_match_group_returns_none_when_absent():
    assert match_group([_BOOK], "Weekend plans") is None


def test_match_group_refuses_to_guess_between_duplicates():
    groups = [SignalGroup(id="a=", name="Dupe"), SignalGroup(id="b=", name="Dupe")]
    with pytest.raises(SignalError, match="2 groups are called"):
        match_group(groups, "Dupe")


def test_find_group_hits_without_receiving(monkeypatch):
    calls = _patch_lookup(monkeypatch, [_BOOK])

    assert find_group("Book club") == _BOOK
    # Nothing was missing, so there was no reason to drain the queue.
    assert calls == {"list": 1, "receive": 0}


def test_find_group_receives_then_looks_again(monkeypatch):
    # A group created on the phone only reaches a linked device via sync
    # messages, so a miss is worth one receive before believing it.
    calls = _patch_lookup(monkeypatch, [], [_BOOK])

    assert find_group("Book club") == _BOOK
    assert calls == {"list": 2, "receive": 1}


def test_find_group_returns_none_when_still_missing(monkeypatch):
    calls = _patch_lookup(monkeypatch, [], [])

    assert find_group("Nope") is None
    assert calls["receive"] == 1


def test_find_group_without_refresh_never_receives(monkeypatch):
    calls = _patch_lookup(monkeypatch, [])

    assert find_group("Book club", refresh=False) is None
    assert calls == {"list": 1, "receive": 0}


# --- avatar sizing ---------------------------------------------------------
#
# Signal drops an oversized group avatar silently: the upload is accepted,
# signal-cli exits 0, and the group keeps no picture. Measured against a real
# feed, 1400x1400 vanished and 512x512 arrived.


def _jpeg(path, size):
    Image.new("RGB", size, "red").save(path, "JPEG")
    return str(path)


def _avatar_sent(calls):
    """The path signal-cli was actually handed."""
    argv = calls["argv"]
    return argv[argv.index("--avatar") + 1]


def test_set_group_avatar_shrinks_an_oversized_image(
    have_binary, monkeypatch, tmp_path
):
    big = _jpeg(tmp_path / "big.jpg", (1400, 1400))
    calls = _patch_create(monkeypatch)
    sizes = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        # The temp file is deleted on the way out, so measure it while it lives.
        with Image.open(argv[argv.index("--avatar") + 1]) as img:
            sizes["sent"] = img.size
        return _completed()

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)

    set_group_avatar("ZZZ=", big, account="+31600000000")

    assert sizes["sent"] == (512, 512)
    assert _avatar_sent(calls) != big


def test_set_group_avatar_keeps_the_aspect_ratio(have_binary, monkeypatch, tmp_path):
    wide = _jpeg(tmp_path / "wide.jpg", (1400, 700))
    sizes = {}

    def fake_run(argv, **kwargs):
        with Image.open(argv[argv.index("--avatar") + 1]) as img:
            sizes["sent"] = img.size
        return _completed()

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)

    set_group_avatar("ZZZ=", wide, account="+31600000000")

    assert sizes["sent"] == (512, 256)


def test_set_group_avatar_leaves_a_small_image_alone(
    have_binary, monkeypatch, tmp_path
):
    small = _jpeg(tmp_path / "small.jpg", (256, 256))
    calls = _patch_create(monkeypatch)

    set_group_avatar("ZZZ=", small, account="+31600000000")

    # Re-encoding a picture that already fits would only lose quality.
    assert _avatar_sent(calls) == small


def test_set_group_avatar_removes_the_resized_temp_file(
    have_binary, monkeypatch, tmp_path
):
    big = _jpeg(tmp_path / "big.jpg", (1400, 1400))
    calls = _patch_create(monkeypatch)

    set_group_avatar("ZZZ=", big, account="+31600000000")

    assert not os.path.exists(_avatar_sent(calls))


def test_set_group_avatar_passes_through_what_it_cannot_read(
    have_binary, monkeypatch, tmp_path
):
    # Not an image rssignal understands; let signal-cli have its say rather than
    # refusing outright.
    odd = tmp_path / "odd.jpg"
    odd.write_bytes(b"not really a jpeg")
    calls = _patch_create(monkeypatch)

    set_group_avatar("ZZZ=", str(odd), account="+31600000000")

    assert _avatar_sent(calls) == str(odd)


def test_create_group_shrinks_the_avatar_too(have_binary, monkeypatch, tmp_path):
    big = _jpeg(tmp_path / "big.jpg", (1400, 1400))
    calls = _patch_create(monkeypatch, stdout='{"groupId": "ZZZ="}')
    sizes = {}

    def fake_run(argv, **kwargs):
        calls.setdefault("argvs", []).append(argv)
        calls["argv"] = argv
        if "--avatar" in argv:
            with Image.open(argv[argv.index("--avatar") + 1]) as img:
                sizes["sent"] = img.size
        return _completed(stdout='{"groupId": "ZZZ="}')

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)

    create_group("Pod", avatar=big, account="+31600000000")

    assert sizes["sent"] == (512, 512)


# --- group descriptions ----------------------------------------------------


def test_update_group_sets_a_description(have_binary, monkeypatch):
    calls = _patch_create(monkeypatch)

    update_group("ZZZ=", description="A daily podcast.", account="+31600000000")

    assert calls["argv"] == [
        FAKE_BIN,
        "-a",
        "+31600000000",
        "updateGroup",
        "--group-id",
        "ZZZ=",
        "--description",
        "A daily podcast.",
    ]


def test_update_group_sets_description_and_avatar_in_one_call(
    have_binary, monkeypatch, tmp_path
):
    avatar = _jpeg(tmp_path / "a.jpg", (100, 100))
    calls = _patch_create(monkeypatch)

    update_group("ZZZ=", description="Blurb", avatar=avatar, account="+31600000000")

    # Two attributes, one JVM start.
    assert len(calls["argvs"]) == 1
    assert "--description" in calls["argv"] and "--avatar" in calls["argv"]


def test_update_group_with_nothing_to_change_runs_nothing(have_binary, monkeypatch):
    calls = _patch_create(monkeypatch)
    update_group("ZZZ=", account="+31600000000")
    assert calls.get("argvs") is None


def test_update_group_sets_the_expiry(have_binary, monkeypatch):
    calls = _patch_create(monkeypatch)

    update_group("ZZZ=", expiration=604800, account="+31600000000")

    assert calls["argv"][calls["argv"].index("--expiration") + 1] == "604800"


def test_update_group_can_turn_the_expiry_off(have_binary, monkeypatch):
    # 0 is off, and is not the same as None, which means "leave it alone".
    calls = _patch_create(monkeypatch)

    update_group("ZZZ=", expiration=0, account="+31600000000")

    assert calls["argv"][calls["argv"].index("--expiration") + 1] == "0"


def test_update_group_rejects_a_negative_expiry(have_binary, monkeypatch):
    calls = _patch_create(monkeypatch)

    with pytest.raises(ValueError):
        update_group("ZZZ=", expiration=-1, account="+31600000000")

    assert calls.get("argvs") is None


def test_update_group_clips_a_long_description(have_binary, monkeypatch):
    calls = _patch_create(monkeypatch)

    update_group("ZZZ=", description="word " * 200, account="+31600000000")

    sent = calls["argv"][calls["argv"].index("--description") + 1]
    assert len(sent) <= signal_cli.GROUP_DESCRIPTION_MAX_CHARS
    assert sent.endswith("…")


def test_clip_leaves_a_short_description_alone():
    assert signal_cli._clip("A short blurb about nothing much.") == (
        "A short blurb about nothing much."
    )


def test_clip_cuts_on_a_word_boundary():
    clipped = signal_cli._clip("alpha beta gamma delta", limit=14)
    # Not "alpha beta ga…" — a cut mid-word reads like a typo.
    assert clipped == "alpha beta…"


def test_clip_falls_back_to_a_hard_cut_for_one_long_word():
    clipped = signal_cli._clip("x" * 40, limit=10)
    assert len(clipped) == 10 and clipped.endswith("…")


def test_clip_never_cuts_off_a_trailing_watermark():
    # The marker is what the next run reads to know how far a feed got; losing
    # it to a long blurb would silently resend everything.
    marker = "[rssignal 2026-07-23T10:03:00+00:00]"
    clipped = signal_cli._clip(f"{'word ' * 300}\n\n{marker}", limit=200)

    assert clipped.endswith(marker)
    assert len(clipped) <= 200
