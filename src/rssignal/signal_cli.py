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
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from .config import get_config
from .watermark import compose_description, read_watermark, shorten

# Signal silently drops a group avatar that is too large: the upload is accepted,
# signal-cli exits 0, and the group simply keeps no picture. 1400x1400 is dropped,
# 512x512 goes through, so anything bigger is scaled down before it is sent.
# Link preview images are *not* subject to this: 1920x1920 artwork was measured
# arriving intact on a card, so previews are sent at their original size rather
# than needlessly degraded.
GROUP_AVATAR_MAX_PX = 512

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


def receive(*, account: str | None = None, timeout: float = 10) -> None:
    """Drain the incoming message queue, updating local state.

    rssignal runs as a linked secondary device, so it only learns about new
    groups (and profile or membership changes) from sync messages sitting in the
    queue. ``listGroups`` reads local state, so a group created on the phone
    stays invisible until those messages are received at least once.

    Incoming message content is discarded — this is called for its side effect on
    local state. ``timeout`` is how long signal-cli waits for more messages
    before returning.
    """
    binary = find_signal_cli()
    if account is None:
        account = get_config().account

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


def list_groups(*, account: str | None = None) -> list[SignalGroup]:
    """Return the groups the account belongs to, newest signal-cli order.

    Reads ``signal-cli -o json listGroups``. JSON rather than the plain-text
    listing for two reasons: the plain listing doesn't print the description at
    all, and a watermarked description is multi-line, which no line-based parse
    survives. In JSON mode listGroups is always detailed.

    Groups you have left show up with ``active=False``; they are kept here and
    filtered at the call site so the caller can decide what to show.
    """
    binary = find_signal_cli()
    if account is None:
        account = get_config().account

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
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
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
    argv.extend(["-m", clip_body(msg, preview.url if preview else None)])
    # Groups are addressed with -g rather than as a positional recipient.
    if recipient.startswith(GROUP_PREFIX):
        argv.extend(["-g", recipient[len(GROUP_PREFIX):]])
    elif recipient.startswith("+"):
        argv.append(recipient)
    else:
        raise ValueError(
            f"Recipient {recipient!r} is neither an E.164 number (starting with "
            f"'+') nor a group. To send to a group, prefix its id with "
            f"'{GROUP_PREFIX}' — `rssignal groups` prints ready-to-use values."
        )

    try:
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
