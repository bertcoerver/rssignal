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

import re
import shutil
import subprocess
from dataclasses import dataclass

from .config import get_config

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
    is not unique and may be empty.
    """

    id: str
    name: str
    active: bool = True
    blocked: bool = False

    @property
    def recipient(self) -> str:
        """The value to use as a recipient in a config or ``--to``."""
        return f"{GROUP_PREFIX}{self.id}"


# listGroups prints one group per line:
#   Id: <base64> Name: <name>  Active: true Blocked: false
# The name can contain spaces (and the odd emoji), so it is matched lazily up to
# the next known field label.
_GROUP_LINE = re.compile(
    r"^Id:\s*(?P<id>\S+)\s+Name:\s*(?P<name>.*?)\s+"
    r"(?:Description:.*?\s+)?Active:\s*(?P<active>\S+)\s+Blocked:\s*(?P<blocked>\S+)"
)


def list_groups(*, account: str | None = None) -> list[SignalGroup]:
    """Return the groups the account belongs to, newest signal-cli order.

    Parses ``signal-cli listGroups``. Groups you have left show up with
    ``active=False``; they are kept here and filtered at the call site so the
    caller can decide what to show.
    """
    binary = find_signal_cli()
    if account is None:
        account = get_config().account

    result = subprocess.run(
        [binary, "-a", account, "listGroups"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SignalSendError(
            "`signal-cli listGroups` failed.",
            returncode=result.returncode,
            stderr=result.stderr.strip(),
        )

    groups: list[SignalGroup] = []
    for line in result.stdout.splitlines():
        match = _GROUP_LINE.match(line.strip())
        if match:
            groups.append(
                SignalGroup(
                    id=match["id"],
                    name=match["name"].strip(),
                    active=match["active"].lower() == "true",
                    blocked=match["blocked"].lower() == "true",
                )
            )
    return groups


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
    argv.extend(["-m", msg])
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
