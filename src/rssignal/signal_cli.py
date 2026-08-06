"""Thin wrapper around the Java ``signal-cli`` command-line tool.

rssignal shells out to ``signal-cli`` (install separately, e.g.
``brew install signal-cli``) rather than talking to Signal directly. This module
covers the pieces needed to get messages out: locating the binary, linking an
account, checking that an account is registered, and sending a text message.

Each message is sent with a one-shot ``signal-cli send`` invocation. That is
fine for low volume; a long-running JSON-RPC daemon would be the optimization to
reach for once the cloud service sends many messages per run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from . import timing
from .config import ConfigError, get_config
from .watermark import compose_description, read_watermark, shorten

# Signal silently drops a group avatar that is too large: the upload is accepted,
# signal-cli exits 0, and the group simply keeps no picture. 1400x1400 is dropped,
# 512x512 goes through, so anything bigger is scaled down before it is sent.
# Link preview images are *not* subject to this: 1920x1920 artwork was measured
# arriving intact on a card, so previews are sent at their original size rather
# than needlessly degraded.
GROUP_AVATAR_MAX_PX = 512

# How long `signal-cli receive` keeps listening after the queue falls quiet. See
# :func:`receive` — this is idle waiting, not work, so it is kept short enough to
# disappear into the window a run already spends fetching feeds.
DEFAULT_RECEIVE_TIMEOUT = 5.0
RECEIVE_TIMEOUT_ENV = "RSSIGNAL_RECEIVE_TIMEOUT"

# Set to 0 to make every signal-cli call start its own process again. See
# :func:`daemon_enabled`.
DAEMON_ENV = "RSSIGNAL_DAEMON"

# Signal caps group descriptions, but signal-cli documents no figure and rejects
# nothing locally. This is a conservative cap so a long feed blurb is shortened
# by rssignal — where it can be cut on a word — rather than by the server.
GROUP_DESCRIPTION_MAX_CHARS = 480

# Disappearing messages on every group rssignal creates. A feed group is a
# stream, not an archive: without this it grows without limit and the phone
# holding it keeps every episode's audio forever.
#
# It costs nothing rssignal depends on. How far a feed got is kept in the group
# *description* (see :mod:`rssignal.watermark`), which is group metadata rather
# than a message and does not expire — so a group can empty itself completely
# and the next run still knows exactly where it was. The "changed the group
# description" lines each sending run leaves behind expire along with the rest.
GROUP_EXPIRATION_SECONDS = 7 * 24 * 60 * 60

# Signal caps a message body at 2000 **bytes** of UTF-8, not 2000 characters —
# measured against a real feed, a body of 1997 bytes kept its link preview and
# one of 2105 lost it, both at 1997 characters. The difference is entirely
# punctuation: a curly quote costs three bytes, an em dash three, an accented
# letter two. A feed in English rarely notices; one in French blows past the
# limit while still looking short enough.
#
# signal-cli hands the string over whole (Signal's own clients would move the
# overflow into a `long-message.txt` attachment instead), and the receiving app
# responds by dropping the link preview — silently, card and title and all. So
# rssignal cuts the body itself, on a word, counting bytes.
MESSAGE_MAX_BYTES = 2000

# What a shortened body ends in. Three bytes itself, which is the whole point.
ELLIPSIS = "…"

# Default name shown in Signal's "Linked Devices" list.
DEFAULT_DEVICE_NAME = "rssignal"
# Where signal-cli keeps its state, for error hints.
_CONFIG_DIR_HINT = "~/.local/share/signal-cli"
# Marks a recipient as a group id rather than a phone number.
GROUP_PREFIX = "group:"

# How hard to lean on a send that failed for a reason the network caused. The
# mirror of :data:`rssignal.feeds.RETRY_ATTEMPTS` for fetching, and for the same
# reason: this runs unattended over a link that isn't always there. A reset
# halfway through an upload costs the feed its whole run and moves no watermark,
# so the next run downloads and uploads the same episode again to fail the same
# way. Two more tries, four seconds apart, is much the cheaper answer.
SEND_RETRY_ATTEMPTS = 3
SEND_RETRY_DELAY = 2.0

# What a transient send failure looks like coming back from signal-cli. There is
# no error *code* to test — the daemon hands back a Java exception chain as
# prose — so this matches on the names in it. Every one of these means the
# message never reached Signal's servers, which is what makes trying again safe:
# a failure that got far enough to be delivered would be a duplicate, not a
# retry. Anything not named here (a rejected group, a bad recipient, a file that
# won't upload) fails on the first attempt, as it should.
_TRANSIENT_SEND_MARKERS = (
    "PushNetworkException",
    "SocketException",
    "SocketTimeoutException",
    "UnknownHostException",
    "ConnectException",
    "Connection reset",
    "Connection refused",
)


class SignalError(Exception):
    """Base class for all signal-cli related errors."""


class SignalCliNotFound(SignalError):
    """Raised when the ``signal-cli`` binary cannot be located on PATH."""

    def __init__(self) -> None:
        super().__init__(
            "Could not find the `signal-cli` executable on your PATH. Install it "
            "(on macOS: `brew install signal-cli`), then link an account with "
            f"`rssignal link`. signal-cli stores its state in {_CONFIG_DIR_HINT}."
        )


class AccountNotLinked(SignalError):
    """Raised when the configured account is not registered with signal-cli."""

    def __init__(self, account: str) -> None:
        self.account = account
        super().__init__(
            f"Account {account} is not registered with signal-cli. "
            "Run `rssignal link` and scan the QR code from Signal "
            "(Settings -> Linked Devices) to link it."
        )


class SignalSendError(SignalError):
    """Raised when ``signal-cli send`` fails or times out."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(message)


class DaemonGone(SignalSendError):
    """Raised when the shared ``signal-cli jsonRpc`` process has died.

    Not a failure of the call that hit it: the daemon is an optimization, and
    the one-shot ``signal-cli`` path it replaces is still there. So this is
    caught at each call site, which retires the daemon and does the work the
    slow way — the JVM going away costs a run its speed, not its evening.
    """


def _is_transient_send(exc: SignalSendError) -> bool:
    """Whether ``exc`` is the network having a moment rather than a real refusal.

    A local timeout is deliberately *not* transient: retrying one costs another
    whole timeout, and a send slow enough to hit it is more likely to be a large
    upload crawling than a blip worth asking about again. Neither is a dead
    daemon — nothing is going to answer on that pipe however many times it is
    asked, and the caller has a better answer for it than retrying.
    """
    if isinstance(exc, DaemonGone):
        return False
    text = f"{exc} {exc.stderr}"
    return any(marker in text for marker in _TRANSIENT_SEND_MARKERS)


def _sending(work, *, what: str) -> None:
    """Run ``work``, trying again while it fails for a reason the network caused.

    ``what`` names the send in the warning each retry prints, so an unattended
    run leaves a trace of having recovered rather than looking as though nothing
    happened.
    """
    for attempt in range(1, SEND_RETRY_ATTEMPTS + 1):
        try:
            work()
            return
        except SignalSendError as exc:
            if attempt == SEND_RETRY_ATTEMPTS or not _is_transient_send(exc):
                raise
            print(
                f"warning: {what} failed ({exc}); retrying "
                f"({attempt + 1}/{SEND_RETRY_ATTEMPTS})",
                file=sys.stderr,
            )
            time.sleep(SEND_RETRY_DELAY * attempt)


@dataclass(frozen=True)
class LinkPreview:
    """The card Signal shows for a link, mirroring signal-cli's preview flags.

    ``url`` must also appear in the message body — signal-cli rejects a preview
    whose url is not in the text. ``title`` is mandatory; ``description`` and
    ``image`` are optional and omitted when empty. ``image`` is a **local file
    path**, not a URL: whoever builds the preview downloads the artwork first.

    Signal renders the card from these values rather than fetching the url, so a
    preview pointing at a media file still shows properly.
    """

    url: str
    title: str
    description: str = ""
    image: str = ""

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("A link preview needs a url.")
        if not self.title:
            raise ValueError("A link preview needs a title; signal-cli requires it.")


def find_signal_cli() -> str:
    """Return the path to the ``signal-cli`` binary, or raise :class:`SignalCliNotFound`."""
    path = shutil.which("signal-cli")
    if path is None:
        raise SignalCliNotFound()
    return path


def list_accounts() -> list[str]:
    """Return the E.164 numbers of accounts known to signal-cli.

    Parses ``signal-cli listAccounts`` output. ``listAccounts`` is used rather
    than ``receive`` because it has no side effects on the message queue.
    """
    binary = find_signal_cli()
    result = subprocess.run(
        [binary, "listAccounts"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SignalSendError(
            "`signal-cli listAccounts` failed.",
            returncode=result.returncode,
            stderr=result.stderr.strip(),
        )

    accounts: list[str] = []
    for line in result.stdout.splitlines():
        # Lines look like: "Number: +31600000000".
        _, sep, number = line.partition("Number:")
        if sep:
            number = number.strip()
            if number:
                accounts.append(number)
    return accounts


def receive(*, account: str | None = None, timeout: float | None = None) -> None:
    """Drain the incoming message queue, updating local state.

    rssignal runs as a linked secondary device, so it only learns about new
    groups (and profile or membership changes) from sync messages sitting in the
    queue. ``listGroups`` reads local state, so a group created on the phone
    stays invisible until those messages are received at least once.

    Incoming message content is discarded — this is called for its side effect on
    local state. ``timeout`` is how long signal-cli keeps listening for *further*
    messages once the queue has gone quiet, so it is idle waiting rather than
    work: whatever was queued has already arrived by the time it starts counting.
    That makes it the single longest thing an otherwise empty run does, which is
    why it defaults low and why :func:`rssignal.run.run_feeds` starts it
    alongside the feed fetches — at :data:`DEFAULT_RECEIVE_TIMEOUT` it finishes
    inside that window and costs the run nothing at all.

    Raise it with :data:`RECEIVE_TIMEOUT_ENV` on a slow or busy link, where sync
    messages may still be arriving after the queue first falls quiet.
    """
    if timeout is None:
        try:
            timeout = float(
                os.environ.get(RECEIVE_TIMEOUT_ENV, "") or DEFAULT_RECEIVE_TIMEOUT
            )
        except ValueError:
            timeout = DEFAULT_RECEIVE_TIMEOUT

    if _daemon is not None:
        # jsonRpc mode receives on its own from the moment it starts, so there
        # is no call to make here — but there is still a wait to keep. What the
        # timeout buys is the queue being *drained* before the group listing is
        # read, and the reason that matters is in
        # :class:`rssignal.run._GroupResolver`: a group you have left goes on
        # reading as active until the sync messages land, and sending into it
        # fails silently. So the window stays; only the JVM start goes away.
        # Nothing is lost to waiting here, because a run spends it fetching.
        with timing.step("signal-cli", "receive (daemon settle)"):
            time.sleep(timeout)
        return

    binary = find_signal_cli()
    if account is None:
        account = get_config().account

    with timing.step("signal-cli", "receive"):
        result = subprocess.run(
            [binary, "-a", account, "receive", "--timeout", str(timeout)],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        raise SignalSendError(
            "`signal-cli receive` failed.",
            returncode=result.returncode,
            stderr=result.stderr.strip(),
        )


@dataclass(frozen=True)
class SignalGroup:
    """One group from ``signal-cli listGroups``.

    ``id`` is the base64 group id to send to; ``name`` is the display name, which
    is not unique and may be empty. ``description`` is the group's blurb, which
    for a group rssignal manages also carries the feed's watermark — see
    :mod:`rssignal.watermark`.
    """

    id: str
    name: str
    active: bool = True
    blocked: bool = False
    description: str = ""

    @property
    def recipient(self) -> str:
        """The value to use as a recipient in a config or ``--to``."""
        return f"{GROUP_PREFIX}{self.id}"


def daemon_enabled() -> bool:
    """Whether a run may keep one ``signal-cli`` alive instead of many.

    On by default. Set :data:`DAEMON_ENV` to ``0`` to go back to a fresh
    ``signal-cli`` per call — slower, but it is the path rssignal used for its
    whole life before this, so it is the thing to try first if sending ever
    starts behaving oddly.
    """
    return os.environ.get(DAEMON_ENV, "").strip().lower() not in {"0", "false", "no"}


def _bad_recipient(recipient: str) -> str:
    """What to say about a recipient that is neither a number nor a group."""
    return (
        f"Recipient {recipient!r} is neither an E.164 number (starting with "
        f"'+') nor a group. To send to a group, prefix its id with "
        f"'{GROUP_PREFIX}' — `rssignal groups` prints ready-to-use values."
    )


class _JsonRpc:
    """One ``signal-cli jsonRpc`` process, taking commands on its stdin.

    Every one-shot ``signal-cli`` call pays a JVM start — a second and a half of
    it, measured — and a run that sends a podcast episode makes three of them for
    that one item. This starts the JVM once and keeps it, so the second call
    costs a round trip down a pipe instead.

    Requests are JSON-RPC, one object per line, answered on stdout. Replies can
    be interleaved with *notifications* — jsonRpc mode receives incoming messages
    on its own and reports them the same way — so a reader thread sorts them by
    the ``id`` a reply carries and a notification hasn't, and hands each answer to
    whoever is waiting for it. Incoming messages themselves are dropped: rssignal
    publishes to Signal and reads nothing back.

    Started in single-account mode (``-a ACCOUNT``), which is why no request here
    passes an ``account`` param — in that mode signal-cli rejects one.
    """

    def __init__(self, account: str) -> None:
        self.account = account
        self._next_id = 0
        self._pending: dict[str, dict] = {}
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        binary = find_signal_cli()
        with timing.step("signal-cli", "jsonRpc start"):
            try:
                self._proc = subprocess.Popen(
                    [binary, "-a", self.account, "jsonRpc"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                raise SignalError(f"Could not start `signal-cli jsonRpc`: {exc}")
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        """Sort replies from notifications until the process closes its stdout."""
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            key = message.get("id")
            if key is None:
                # A notification — an incoming Signal message. Not ours.
                continue
            with self._lock:
                self._pending[str(key)] = message
                event = self._events.get(str(key))
            if event is not None:
                event.set()

        # stdout closed: signal-cli is gone. Release anyone still waiting rather
        # than letting them sit out their whole timeout for an answer that can
        # never come.
        with self._lock:
            events = list(self._events.values())
        for event in events:
            event.set()

    def request(self, method: str, params: dict | None = None, *, timeout: float):
        """Call ``method`` and return its result, raising on error or timeout."""
        if self._proc is None or self._proc.stdin is None:
            raise SignalError("signal-cli jsonRpc is not running.")

        with self._lock:
            self._next_id += 1
            key = str(self._next_id)
            event = threading.Event()
            self._events[key] = event

        payload = {"jsonrpc": "2.0", "method": method, "id": key}
        if params:
            payload["params"] = params

        try:
            with timing.step("signal-cli", method):
                try:
                    self._proc.stdin.write(json.dumps(payload) + "\n")
                    self._proc.stdin.flush()
                except (OSError, ValueError) as exc:
                    # A pipe that won't take a write is a process that isn't
                    # there — the same condition as a reply that never comes,
                    # noticed one step earlier.
                    raise DaemonGone(
                        f"`signal-cli {method}` could not be sent: {exc}"
                    ) from exc

                if not event.wait(timeout):
                    raise SignalSendError(
                        f"`signal-cli {method}` timed out after {timeout}s."
                    )
            with self._lock:
                message = self._pending.pop(key, None)
        finally:
            with self._lock:
                self._events.pop(key, None)

        if message is None:
            raise DaemonGone(
                f"`signal-cli {method}` got no answer — signal-cli stopped."
            )
        if "error" in message:
            error = message["error"] or {}
            raise SignalSendError(
                f"`signal-cli {method}` failed: "
                f"{error.get('message') or error!r}"
            )
        return message.get("result")

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            self._proc.terminate()
            self._proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self._proc.kill()
        finally:
            self._proc = None


# The daemon in use, if a run has started one. Module-level because it is a
# property of the run rather than of any one call: the wrappers below check it,
# and everything outside a run keeps the one-shot behaviour it always had.
_daemon: _JsonRpc | None = None


def _retire_daemon(exc: DaemonGone) -> None:
    """Stop routing through the daemon: it has died, and won't be coming back.

    Called by every wrapper that catches :class:`DaemonGone`, before it redoes
    the work through a one-shot ``signal-cli``. Clearing the global is what
    keeps the *rest* of the run from rediscovering the same corpse feed by feed:
    without it, one JVM crash three feeds in costs every feed after it.

    Restarting it instead would be the obvious alternative, and is deliberately
    not done — a daemon that died once mid-run is not obviously worth trusting
    with the rest of it, and the slow path is known to work.
    """
    global _daemon
    if _daemon is None:
        return
    dead, _daemon = _daemon, None
    print(
        f"warning: {exc} Carrying on with one signal-cli per call.",
        file=sys.stderr,
    )
    dead.stop()


@contextmanager
def daemon(*, account: str | None = None):
    """Run one ``signal-cli`` for the whole block instead of one per call.

    Everything in :mod:`rssignal.signal_cli` routes through it while it is open
    and falls back to a fresh ``signal-cli`` per call when it is not, so nothing
    outside a run — ``rssignal send``, ``rssignal groups``, ``rssignal link`` —
    changes behaviour or needs to know this exists.

    A daemon that will not start is not an error. It is a speed measure, and the
    one-shot path it replaces still works, so a failure here logs nothing, warns
    nothing, and simply leaves the run to go the slow way.
    """
    global _daemon
    if _daemon is not None:
        yield _daemon
        return

    if not daemon_enabled():
        yield None
        return

    try:
        if account is None:
            account = get_config().account
    except ConfigError:
        yield None
        return

    started = _JsonRpc(account)
    try:
        started.start()
    except (SignalError, OSError):
        yield None
        return

    _daemon = started
    try:
        yield started
    finally:
        _daemon = None
        started.stop()


def list_groups(*, account: str | None = None) -> list[SignalGroup]:
    """Return the groups the account belongs to, newest signal-cli order.

    Reads ``signal-cli -o json listGroups``. JSON rather than the plain-text
    listing for two reasons: the plain listing doesn't print the description at
    all, and a watermarked description is multi-line, which no line-based parse
    survives. In JSON mode listGroups is always detailed.

    Groups you have left show up with ``active=False``; they are kept here and
    filtered at the call site so the caller can decide what to show.
    """
    raw = None
    if _daemon is not None:
        try:
            # Same objects as the -o json listing, so the parse below is shared.
            raw = _daemon.request("listGroups", timeout=120) or []
        except DaemonGone as exc:
            _retire_daemon(exc)

    if raw is None:
        binary = find_signal_cli()
        if account is None:
            account = get_config().account

        with timing.step("signal-cli", "listGroups"):
            result = subprocess.run(
                [binary, "-o", "json", "-a", account, "listGroups"],
                capture_output=True,
                text=True,
            )
        if result.returncode != 0:
            raise SignalSendError(
                "`signal-cli listGroups` failed.",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )

        try:
            raw = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise SignalError(f"Could not read `signal-cli listGroups` output: {exc}")

    return [
        SignalGroup(
            id=entry.get("id", ""),
            name=(entry.get("name") or "").strip(),
            # A group you have left reports isMember: false rather than
            # disappearing, which is how `rssignal groups` can still show it.
            active=bool(entry.get("isMember", True)),
            blocked=bool(entry.get("isBlocked", False)),
            description=entry.get("description") or "",
        )
        for entry in raw
        if entry.get("id")
    ]


def match_group(groups: list[SignalGroup], name: str) -> SignalGroup | None:
    """Pick the group called ``name`` out of ``groups``, or ``None``.

    Matching is case-insensitive on the trimmed name, and only groups you are
    still in and haven't blocked are considered. Two groups sharing a name raise
    :class:`SignalError` rather than picking one — sending a feed to the wrong
    group is worse than stopping to ask.

    Kept separate from :func:`find_group` so a caller resolving many names can
    list the groups once and match against that one listing.
    """
    needle = name.strip().lower()
    found = [
        group
        for group in groups
        if group.active
        and not group.blocked
        and group.name.strip().lower() == needle
    ]
    if len(found) > 1:
        raise SignalError(
            f"{len(found)} groups are called {name!r}. rssignal can't tell which "
            "one you mean — rename one in Signal, or point the feed at a "
            "different name."
        )
    return found[0] if found else None


def find_group(
    name: str, *, account: str | None = None, refresh: bool = True
) -> SignalGroup | None:
    """Return the group called ``name``, or ``None`` if there isn't one.

    On a miss, ``receive()`` is called once and the list re-read: rssignal runs
    as a linked secondary device, so a group created on your phone is invisible
    until the sync messages are drained. Skipping that step would make rssignal
    create a duplicate of a group you already have. Pass ``refresh=False`` for a
    read-only lookup that never touches the message queue.
    """
    group = match_group(list_groups(account=account), name)
    if group is None and refresh:
        receive(account=account)
        group = match_group(list_groups(account=account), name)
    return group


def _parse_new_group_id(stdout: str) -> str | None:
    """Pull the new group's id out of ``updateGroup -o json`` output.

    Returns ``None`` when the output isn't the expected JSON — the caller then
    falls back to diffing ``listGroups``, so the exact output format is not
    something rssignal has to be right about.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        group_id = payload.get("groupId")
        if isinstance(group_id, str) and group_id:
            return group_id
    return None


def create_group(
    name: str,
    *,
    description: str | None = None,
    avatar: str | None = None,
    announcement_only: bool = False,
    account: str | None = None,
    timeout: float = 120,
) -> SignalGroup:
    """Create a new Signal group containing only you, and return it.

    Runs ``signal-cli updateGroup`` with no ``--group-id``, which is how
    signal-cli creates a group rather than editing one. The account creating the
    group is its only member and its admin.

    **No one else is ever added.** There is deliberately no ``members``
    parameter: adding someone to a group is an action they cannot undo without
    leaving it, and rssignal exists to send feeds, not to manage anyone's
    contacts. Invite people from Signal on your phone once the group exists.

    ``avatar`` is a **local image file path** (signal-cli uploads it), not a URL.
    ``announcement_only`` restricts sending to admins, which is usually what you
    want for a group that exists to receive a feed.

    The description, avatar and message expiry are applied by a **second**
    ``updateGroup`` call once the group has an id. signal-cli accepts
    ``--avatar`` on the creating call, exits 0, and leaves the group with no
    picture; the same flag against an existing ``--group-id`` works. That was
    measured; whether ``--description`` and ``--expiration`` fare any better
    there was not, so they take the path known to work. ``--name`` and
    ``--set-permission-send-messages`` do take effect on creation and stay put.
    A group that can't be decorated is still returned, with a warning: losing
    the blurb, the picture or the timer is not a reason to lose the group.

    Every group starts with disappearing messages set to
    :data:`GROUP_EXPIRATION_SECONDS` — see there for why that costs rssignal
    nothing. Existing groups are never touched.

    The returned :class:`SignalGroup` carries the new base64 id; its
    :attr:`~SignalGroup.recipient` is ready to send to.
    """
    binary = find_signal_cli()
    if account is None:
        account = get_config().account

    if not name.strip():
        raise ValueError("A group needs a name.")
    if avatar is not None and not os.path.isfile(avatar):
        raise ValueError(
            f"Group avatar {avatar!r} is not a file. --avatar takes a local image "
            "path, not a URL — download the image first."
        )

    # Recorded up front so the new group can be identified by elimination if the
    # command's output can't be parsed.
    before = {group.id for group in list_groups(account=account)}

    # No -g means "create"; no -m means the group starts with only this account.
    # --avatar and --description are deliberately absent: see the docstring.
    argv = [binary, "-o", "json", "-a", account, "updateGroup", "--name", name]
    if announcement_only:
        argv.extend(["--set-permission-send-messages", "only-admins"])

    try:
        with timing.step("signal-cli", "updateGroup (create)"):
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout
            )
    except subprocess.TimeoutExpired as exc:
        raise SignalSendError(
            f"`signal-cli updateGroup` timed out after {timeout}s. The group may "
            "or may not have been created — check `rssignal groups`.",
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
        ) from exc

    if result.returncode != 0:
        raise SignalSendError(
            f"`signal-cli updateGroup` failed with status {result.returncode}: "
            f"{result.stderr.strip()}",
            returncode=result.returncode,
            stderr=result.stderr.strip(),
        )

    group_id = _parse_new_group_id(result.stdout)
    if group_id is None:
        created = [g for g in list_groups(account=account) if g.id not in before]
        if len(created) != 1:
            raise SignalSendError(
                "The group was created, but its id could not be determined from "
                "signal-cli's output. Run `rssignal groups` to find it."
            )
        group_id = created[0].id

    applied = ""
    try:
        update_group(
            group_id,
            description=description or None,
            avatar=avatar or None,
            expiration=GROUP_EXPIRATION_SECONDS,
            account=account,
            timeout=timeout,
        )
        applied = _clip(description) if description else ""
    except SignalError as exc:
        print(
            f"Group {name!r} was created, but setting its details failed: {exc}",
            file=sys.stderr,
        )

    # The description comes back on the group so the caller doesn't have to
    # re-list to find out what actually landed there — empty if it didn't.
    return SignalGroup(id=group_id, name=name, description=applied)


def _clip(text: str, limit: int = GROUP_DESCRIPTION_MAX_CHARS) -> str:
    """Shorten ``text`` to ``limit`` characters, cutting on a word where possible.

    A trailing rssignal watermark is never cut off, however long the blurb in
    front of it: the marker is what the next run reads to know how far the feed
    got, so losing it would silently resend items. The blurb is shortened
    instead. See :mod:`rssignal.watermark`.
    """
    mark = read_watermark(text)
    if mark is not None:
        return compose_description(text, mark, limit=limit)
    return shorten(text, limit)


def _shorten_bytes(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` UTF-8 bytes, on a word where one is close by.

    The byte-counting twin of :func:`rssignal.watermark.shorten`. Group
    descriptions are measured in characters and messages in bytes, and the two
    only agree while the text stays ASCII — which is exactly the case that hides
    the bug. See :data:`MESSAGE_MAX_BYTES`.
    """
    text = text.strip()
    if len(text.encode()) <= limit:
        return text
    if limit <= len(ELLIPSIS.encode()):
        return ""

    # Characters are 1-4 bytes each, so a character index is only a starting
    # guess; walk it back until the text and its ellipsis both fit.
    cut = text[:limit]
    while cut and len(cut.encode()) + len(ELLIPSIS.encode()) > limit:
        cut = cut[:-1]

    space = cut.rfind(" ")
    if space > len(cut) // 2:
        cut = cut[:space]
    return f"{cut.rstrip(' ,;:.—-')}{ELLIPSIS}" if cut else ""


def clip_body(text: str, url: str | None = None) -> str:
    """Shorten ``text`` to :data:`MESSAGE_MAX_BYTES`, cutting on a word if it can.

    ``url`` is a link preview's url, if the message carries one. It is never
    what gets cut, however long the text in front of it: signal-cli rejects a
    preview whose url is missing from the body, and Signal has nothing to draw
    the card from. The text is shortened instead — the same bargain
    :func:`_clip` strikes for a group description's watermark.

    A url that isn't in the body to begin with is left to signal-cli to complain
    about; there is nothing here worth preserving.

    Public so ``--dry-run`` can show what would actually be sent rather than the
    text before it was cut.
    """
    if len(text.encode()) <= MESSAGE_MAX_BYTES:
        return text
    if url is None:
        return _shorten_bytes(text, MESSAGE_MAX_BYTES)

    head, found, tail = text.rpartition(url)
    if not found:
        return _shorten_bytes(text, MESSAGE_MAX_BYTES)

    # Everything kept is measured in bytes, the url included: it is usually
    # ASCII, but an internationalised domain or an unescaped accent in the path
    # costs more than its character count and would put the body back over.
    # The 2 is the two newlines put back between the text and the url.
    room = MESSAGE_MAX_BYTES - len(url.encode()) - len(tail.encode()) - 2
    return f"{_shorten_bytes(head, room)}\n\n{url}{tail}".strip()


@contextmanager
def _avatar_within_limits(path: str):
    """Yield a path to ``path`` scaled down to what Signal will accept.

    Signal takes an oversized avatar, reports nothing, and then quietly doesn't
    use it — the group simply keeps no picture. So anything bigger than
    :data:`GROUP_AVATAR_MAX_PX` on its long edge is written to a temporary JPEG,
    removed on the way out.

    The original is yielded untouched when it already fits, and also when it
    can't be read as an image — a picture rssignal doesn't understand is better
    handed to signal-cli than rejected outright.
    """
    try:
        with Image.open(path) as img:
            if max(img.size) <= GROUP_AVATAR_MAX_PX:
                yield path
                return
            shrunk = img.convert("RGB")
            shrunk.thumbnail((GROUP_AVATAR_MAX_PX, GROUP_AVATAR_MAX_PX))
    except (UnidentifiedImageError, OSError):
        yield path
        return

    fd, small = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        shrunk.save(small, "JPEG", quality=85)
        yield small
    finally:
        os.unlink(small)


def update_group(
    group_id: str,
    *,
    description: str | None = None,
    avatar: str | None = None,
    expiration: int | None = None,
    account: str | None = None,
    timeout: float = 120,
) -> None:
    """Set an existing group's description, picture and/or message expiry.

    ``avatar`` is a **local image file path**, not a URL; images larger than
    :data:`GROUP_AVATAR_MAX_PX` are scaled down first, because Signal drops an
    oversized avatar without reporting anything. ``description`` is trimmed to
    :data:`GROUP_DESCRIPTION_MAX_CHARS`. ``expiration`` is the disappearing-message
    timer in **seconds**, or ``0`` to turn it off.

    Separate from :func:`create_group` because signal-cli does not honour
    ``--avatar`` on the call that creates a group. Anything left as ``None`` is
    not touched, and asking for no change at all runs nothing.

    Membership is deliberately not settable here, for the same reason
    :func:`create_group` has no ``members``: who is in a group is not rssignal's
    decision to make.
    """
    if avatar is not None and not os.path.isfile(avatar):
        raise ValueError(
            f"Group avatar {avatar!r} is not a file. An avatar is a local image "
            "path, not a URL — download the image first."
        )
    if expiration is not None and expiration < 0:
        raise ValueError(
            f"Message expiry must be a number of seconds, not {expiration!r}. "
            "Use 0 to turn disappearing messages off."
        )
    if description is None and avatar is None and expiration is None:
        return

    if _daemon is not None:
        try:
            with ExitStack() as stack:
                params: dict = {"groupId": group_id}
                if description is not None:
                    params["description"] = _clip(description)
                if avatar is not None:
                    params["avatar"] = stack.enter_context(_avatar_within_limits(avatar))
                if expiration is not None:
                    params["expiration"] = int(expiration)
                _daemon.request("updateGroup", params, timeout=timeout)
            return
        except DaemonGone as exc:
            # Falls through to the one-shot path below and does it again. Safe
            # to repeat: updateGroup sets fields to what they were asked to be
            # rather than changing them by a step, and the daemon attempt that
            # got here never reached signal-cli at all.
            _retire_daemon(exc)

    binary = find_signal_cli()
    if account is None:
        account = get_config().account

    with ExitStack() as stack:
        argv = [binary, "-a", account, "updateGroup", "--group-id", group_id]
        if description is not None:
            argv.extend(["--description", _clip(description)])
        if avatar is not None:
            argv.extend(["--avatar", stack.enter_context(_avatar_within_limits(avatar))])
        if expiration is not None:
            argv.extend(["--expiration", str(expiration)])

        try:
            with timing.step("signal-cli", "updateGroup"):
                result = subprocess.run(
                    argv, capture_output=True, text=True, timeout=timeout
                )
        except subprocess.TimeoutExpired as exc:
            raise SignalSendError(
                f"`signal-cli updateGroup` timed out after {timeout}s.",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            ) from exc

    if result.returncode != 0:
        raise SignalSendError(
            f"`signal-cli updateGroup` failed with status {result.returncode}: "
            f"{result.stderr.strip()}",
            returncode=result.returncode,
            stderr=result.stderr.strip(),
        )


def set_group_avatar(
    group_id: str,
    avatar: str,
    *,
    account: str | None = None,
    timeout: float = 120,
) -> None:
    """Set an existing group's picture. Shorthand for :func:`update_group`."""
    update_group(group_id, avatar=avatar, account=account, timeout=timeout)


def is_account_registered(account: str) -> bool:
    """Return True if ``account`` appears in signal-cli's list of accounts."""
    return account in list_accounts()


def check_account(account: str) -> None:
    """Raise :class:`AccountNotLinked` if ``account`` is not registered."""
    if not is_account_registered(account):
        raise AccountNotLinked(account)


def link_device(device_name: str = DEFAULT_DEVICE_NAME, *, print_qr: bool = True) -> None:
    """Link this machine to an existing Signal account as a secondary device.

    Runs ``signal-cli link -n <device_name>``, which prints an ``sgnl://`` URI.
    Scan it from Signal on your phone (Settings -> Linked Devices -> Link New
    Device). The call blocks until linking and the initial sync finish.

    If ``print_qr`` is set and ``qrencode`` is on PATH, the URI is rendered as a
    scannable QR code in the terminal; otherwise the raw URI is printed.
    """
    binary = find_signal_cli()
    proc = subprocess.Popen(
        [binary, "link", "-n", device_name],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None  # for type checkers; PIPE is set above

    # The first line of stdout is the sgnl:// linking URI.
    uri = proc.stdout.readline().strip()
    if uri:
        _render_link_uri(uri, print_qr=print_qr)

    proc.wait()
    if proc.returncode != 0:
        raise SignalError(
            f"`signal-cli link` exited with status {proc.returncode}. "
            "The device may not have been linked."
        )


def _render_link_uri(uri: str, *, print_qr: bool) -> None:
    """Print the linking URI, as a terminal QR code when possible."""
    if print_qr and shutil.which("qrencode"):
        print("Scan this QR code from Signal (Settings -> Linked Devices):\n")
        subprocess.run(["qrencode", "-t", "ansiutf8", uri])
        print()
    else:
        if print_qr:
            print(
                "Tip: install `qrencode` (brew install qrencode) to render this "
                "as a scannable QR code.\n"
            )
        print("Open this link on your phone, or paste into a QR generator:\n")
        print(uri + "\n")


def send_msg(
    msg: str,
    recipient: str | None = None,
    *,
    account: str | None = None,
    attachments: list[str] | None = None,
    voice_note: bool = False,
    preview: LinkPreview | None = None,
    timeout: float = 120,
) -> None:
    """Send ``msg`` as a Signal message, optionally with attachments.

    ``account`` (the sender) and ``recipient`` fall back to the configured
    ``RSSIGNAL_ACCOUNT`` / ``RSSIGNAL_RECIPIENT`` values when not given. A
    recipient is either an E.164 number (``+31600000000``) or a group, written
    ``group:<base64 id>`` — run ``rssignal groups`` to list the ids.

    ``attachments`` is a list of local file paths to attach; ``voice_note``
    flags a (single) audio attachment to be sent as a Signal voice note.
    ``preview`` adds a link preview card, whose url must appear in ``msg``. The
    default ``timeout`` is generous because attachment uploads take longer than
    plain text.

    A body over :data:`MESSAGE_MAX_BYTES` is shortened before it goes out, on a
    word where possible and never at the cost of the preview url — see
    :func:`clip_body`. A preview image is sent at whatever size it arrives:
    Signal renders a large one as a full-width card rather than a thumbnail, so
    downscaling it would cost the better-looking layout for nothing.

    A send that fails for a reason the network caused is tried again — see
    :func:`_sending`. Everything else fails on the first attempt.

    Raises :class:`SignalSendError` on a non-zero exit or timeout,
    :class:`SignalCliNotFound` if the binary is missing, and ``ValueError`` if no
    recipient can be resolved.
    """
    binary = find_signal_cli()

    if account is None or recipient is None:
        config = get_config()
        if account is None:
            account = config.account
        if recipient is None:
            recipient = config.recipient

    if not recipient:
        raise ValueError(
            "No recipient given and RSSIGNAL_RECIPIENT is not set. Pass "
            "`recipient=` or set a default in your environment / .env file."
        )

    body = clip_body(msg, preview.url if preview else None)

    if _daemon is not None:
        # The same options as the argv below, under the names jsonRpc gives
        # them: the camelCase of each long flag. Built separately rather than
        # translated from argv, because a mapping between two spellings of the
        # same thing is one more place for them to drift apart.
        params: dict = {"message": body}
        if attachments:
            params["attachments"] = list(attachments)
        if voice_note:
            params["voiceNote"] = True
        if preview is not None:
            params["previewUrl"] = preview.url
            params["previewTitle"] = preview.title
            if preview.description:
                params["previewDescription"] = preview.description
            if preview.image:
                params["previewImage"] = preview.image
        if recipient.startswith(GROUP_PREFIX):
            params["groupId"] = recipient[len(GROUP_PREFIX):]
        elif recipient.startswith("+"):
            params["recipient"] = [recipient]
        else:
            raise ValueError(_bad_recipient(recipient))
        try:
            _sending(
                lambda: _daemon.request("send", params, timeout=timeout),
                what="send",
            )
            return
        except DaemonGone as exc:
            # The one case where falling through cannot duplicate a message:
            # the daemon never carried this one. Either the pipe refused the
            # write, or signal-cli died without answering — and a send that had
            # actually gone out would have been answered, error or not.
            _retire_daemon(exc)

    # Options that take a fixed number of args go before ``-m msg``, so the
    # trailing positional recipient can't be swallowed by ``--attachment``'s
    # greedy arg list.
    argv = [binary, "-a", account, "send"]
    if attachments:
        argv.append("--attachment")
        argv.extend(attachments)
    if voice_note:
        argv.append("--voice-note")
    if preview is not None:
        # Each preview flag takes exactly one argument, so they are safe here:
        # past --attachment's greedy list, and still ahead of the recipient.
        argv.extend(["--preview-url", preview.url, "--preview-title", preview.title])
        if preview.description:
            argv.extend(["--preview-description", preview.description])
        if preview.image:
            argv.extend(["--preview-image", preview.image])
    argv.extend(["-m", body])
    # Groups are addressed with -g rather than as a positional recipient.
    if recipient.startswith(GROUP_PREFIX):
        argv.extend(["-g", recipient[len(GROUP_PREFIX):]])
    elif recipient.startswith("+"):
        argv.append(recipient)
    else:
        raise ValueError(_bad_recipient(recipient))

    def once() -> None:
        try:
            with timing.step("signal-cli", "send"):
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
        except subprocess.TimeoutExpired as exc:
            raise SignalSendError(
                f"`signal-cli send` timed out after {timeout}s.",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            ) from exc

        if result.returncode != 0:
            raise SignalSendError(
                f"`signal-cli send` failed with status {result.returncode}: "
                f"{result.stderr.strip()}",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )

    _sending(once, what="send")
