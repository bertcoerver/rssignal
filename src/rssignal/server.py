"""``rssignal serve``: one long-lived process that runs the feeds, shows a GUI,
and sends what other things ask it to.

Why one process: signal-cli allows a single process per account. A scheduled
run holds it for as long as the run takes, so a notification sent through a
second ``signal-cli`` — "the washing machine is done" — would wait behind the
run, or trip over its lock. Here one ``signal-cli jsonRpc`` is started when the
service starts and kept for its whole life (restarted if it dies), and the
scheduled runs, the buttons in the GUI and the send API all go through it. A
notification then waits at most for the one upload that is in flight, not for
the run.

What it serves, on one port:

``/``
    The GUI: status, feeds, run buttons, a send form, the log, and linking a
    Signal account by QR code. Only for clients in :data:`GUI_FROM_ENV`, which
    under Home Assistant is its ingress proxy — HA has already checked who you
    are by the time a request arrives.
``/api/send``
    ``{"message": "...", "to": "Laundry"}`` (``to`` may also be a list). For
    ``rest_command``, scripts, anything that can POST JSON.
``/v1/about``, ``/v2/send``, ``/v1/send``
    The parts of `signal-cli-rest-api <https://github.com/bbernhard/signal-cli-rest-api>`_
    that Home Assistant's own **Signal Messenger** integration uses, so it works
    unchanged with this as its ``url``.

The send endpoints answer clients in :data:`API_FROM_ENV`, or any client that
presents :data:`API_TOKEN_ENV` as a bearer token. A recipient is a phone
number, ``group:<id>``, ``group.<id>`` the way signal-cli-rest-api writes it,
or simply the name of a group.
"""

from __future__ import annotations

import base64
import binascii
import io
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

from . import __version__, signal_cli
from .config import ConfigError, get_config
from .episodic import next_release
from .errorlog import log_exception, log_line, log_path
from .feeds import (
    FeedError,
    SourceMissing,
    apply_extracts,
    apply_filters,
    filter_since,
    load_feeds,
)
from .library import library_root, read_feed
from .run import AlreadyRunning, refresh_group_images, run_feeds
from .signal_cli import (
    GROUP_PREFIX,
    SignalError,
    SignalGroup,
    list_accounts,
    list_groups,
    match_group,
    send_msg,
)
from .watermark import read_pace, read_watermark

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099

SCHEDULE_ENV = "RSSIGNAL_SCHEDULE"
GUI_FROM_ENV = "RSSIGNAL_GUI_FROM"
API_FROM_ENV = "RSSIGNAL_API_FROM"
API_TOKEN_ENV = "RSSIGNAL_API_TOKEN"

# Only this machine, unless told otherwise. The add-on sets both: the GUI to
# Home Assistant's ingress proxy, the API to Home Assistant itself.
DEFAULT_FROM = "127.0.0.1,::1"

# The group listing costs a signal-cli round trip; the GUI asks for it often.
GROUPS_TTL = 60

# What a request body may be, attachments and all. Home Assistant caps what it
# downloads for an attachment at 50 MiB; base64 adds a third.
MAX_BODY = 80 * 1024 * 1024

# How long a notification may wait for its message to go out.
SEND_TIMEOUT = 120

# Bytes a file starts with -> the extension signal-cli should see on it.
_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF8", ".gif"),
    (b"%PDF", ".pdf"),
    (b"RIFF", ".webp"),
    (b"ID3", ".mp3"),
    (b"OggS", ".ogg"),
)
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "text/plain": ".txt",
}


class SendRefused(ValueError):
    """A send request that is wrong in itself: say so, don't log a traceback."""


# --- who is speaking --------------------------------------------------------


def parse_networks(value: str) -> list[ipaddress._BaseNetwork]:
    """``"172.30.32.1, 10.0.0.0/8"`` as networks; nonsense entries are skipped."""
    nets = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            print(f"warning: {part!r} is not an address or network; ignored.", file=sys.stderr)
    return nets


def allowed(address: str, networks: list) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in networks if net.version == ip.version)


# --- recipients and attachments --------------------------------------------


def resolve_recipient(to: str, groups: list[SignalGroup]) -> str:
    """Turn what a caller wrote into something :func:`send_msg` takes.

    A phone number and ``group:<id>`` are used as they are. ``group.<id>`` is
    signal-cli-rest-api's spelling, whose ``<id>`` is the group id base64'd
    once more; either form of the id is accepted. Anything else is the name of
    a group, matched the way feeds match theirs: ignoring case and space, never
    a group that has been left.
    """
    to = (to or "").strip()
    if not to:
        raise SendRefused("A recipient is empty.")
    if to.startswith("+"):
        return to
    if to.startswith(GROUP_PREFIX):
        return to
    if to.startswith("group."):
        raw = to[len("group."):]
        known = {g.id for g in groups}
        try:
            decoded = base64.b64decode(raw, validate=True).decode("ascii")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            decoded = ""
        if decoded and (decoded in known or raw not in known):
            return GROUP_PREFIX + decoded
        return GROUP_PREFIX + raw
    group = match_group(groups, to)
    if group is None:
        raise SendRefused(
            f"No group called {to!r}. Create it in Signal (or with `rssignal "
            "create-group`) first — rssignal only sends to groups it already has."
        )
    return group.recipient


def decode_attachment(value: str, index: int) -> tuple[bytes, str]:
    """``(bytes, extension)`` from base64 or a ``data:`` URI.

    signal-cli-rest-api takes ``data:<mime>;filename=<name>;base64,<data>`` as
    well as bare base64, and so does this. signal-cli goes by the file name, so
    the extension comes from the name, the type, or the bytes themselves.
    """
    ext = ""
    if value.startswith("data:"):
        header, _, value = value.partition(",")
        meta = header[5:].split(";")
        mime = meta[0].strip().lower()
        ext = _MIME_EXT.get(mime, "")
        for part in meta[1:]:
            key, _, name = part.partition("=")
            if key.strip() == "filename" and "." in name:
                ext = os.path.splitext(name.strip())[1].lower() or ext
    try:
        data = base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise SendRefused(f"Attachment {index + 1} is not base64: {exc}") from exc
    if not data:
        raise SendRefused(f"Attachment {index + 1} is empty.")
    if not ext:
        ext = next((e for magic, e in _MAGIC if data.startswith(magic)), "")
        if not ext and data[4:8] == b"ftyp":
            ext = ".mp4"
    return data, ext or ".bin"


# --- the schedule -----------------------------------------------------------


def parse_schedule(value: str | None) -> list[tuple[int, int]]:
    """``"01:30, 06:30"`` as ``[(1, 30), (6, 30)]``; nonsense entries are refused."""
    slots = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        hour, sep, minute = part.partition(":")
        try:
            h, m = int(hour), int(minute)
        except ValueError:
            h = m = -1
        if not sep or not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"{part!r} is not a time like 06:30.")
        slots.append((h, m))
    return sorted(set(slots))


def next_slot(slots: list[tuple[int, int]], now: datetime) -> datetime | None:
    """The first slot after ``now``, in ``now``'s time zone, or ``None`` with none."""
    if not slots:
        return None
    for days in (0, 1):
        day = now.date() + timedelta(days=days)
        for h, m in slots:
            when = datetime(day.year, day.month, day.day, h, m, tzinfo=now.tzinfo)
            if when > now:
                return when
    return None  # pragma: no cover - two days always hold a slot


# --- capturing what a job prints --------------------------------------------


class _Router(io.TextIOBase):
    """``sys.stdout``/``sys.stderr`` that a job's thread can borrow.

    A run reports by printing, as it always has. A thread that registers a
    buffer gets its own output back to show in the GUI; every other thread
    writes through to the real stream as before.
    """

    def __init__(self, target) -> None:
        self._target = target
        self._local = threading.local()

    def capture(self, sink: list[str] | None) -> None:
        self._local.sink = sink

    def write(self, text: str) -> int:
        sink = getattr(self._local, "sink", None)
        if sink is not None:
            sink.append(text)
        self._target.write(text)
        return len(text)

    def flush(self) -> None:
        self._target.flush()

    def __getattr__(self, name):
        return getattr(self._target, name)


@dataclass
class Job:
    id: str
    kind: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    ok: bool | None = None
    result: str = ""
    output: list[str] = field(default_factory=list)

    def view(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "started": _iso(self.started),
            "finished": _iso(self.finished) if self.finished else None,
            "running": self.finished is None,
            "ok": self.ok,
            "result": self.result,
            "output": "".join(self.output)[-200_000:],
        }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


# --- the service ------------------------------------------------------------


class Service:
    """Everything ``rssignal serve`` does, apart from speaking HTTP."""

    def __init__(
        self,
        config_path: str = "feeds.json",
        *,
        schedule: list[tuple[int, int]] | None = None,
        api_token: str | None = None,
        gui_from: str = DEFAULT_FROM,
        api_from: str = DEFAULT_FROM,
    ) -> None:
        self.config_path = config_path
        self.schedule = schedule or []
        self.api_token = api_token or None
        self.gui_from = parse_networks(gui_from)
        self.api_from = parse_networks(api_from)
        self.activity: deque[dict] = deque(maxlen=200)
        self.jobs: dict[str, Job] = {}
        self._jobs_order: deque[str] = deque()
        self._lock = threading.Lock()
        self._daemon_lock = threading.Lock()
        self._groups: tuple[float, list[SignalGroup]] | None = None
        self._accounts_seen: tuple[float, list[str]] | None = None
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._link: dict = {"state": "idle"}
        self._stdout = self._stderr = None

    # life cycle

    def start(self) -> None:
        self._stdout, self._stderr = _Router(sys.stdout), _Router(sys.stderr)
        sys.stdout, sys.stderr = self._stdout, self._stderr
        self.ensure_daemon()
        if self.schedule:
            self._scheduler = threading.Thread(target=self._schedule_loop, daemon=True, name="rssignal-schedule")
            self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        daemon = signal_cli._daemon
        signal_cli._daemon = None
        if daemon is not None:
            daemon.stop()
        if self._stdout is not None:
            sys.stdout, sys.stderr = self._stdout._target, self._stderr._target

    def ensure_daemon(self) -> bool:
        """Keep one ``signal-cli jsonRpc`` up, if there is a linked account for it.

        Started at most once at a time; a daemon that died is noticed (its
        wrappers retire it) and a new one started on the next use. Without a
        linked account there is nothing to start, and everything falls back to
        one-shot calls, which say why they fail.
        """
        if not signal_cli.daemon_enabled():
            return False
        with self._daemon_lock:
            current = signal_cli._daemon
            if current is not None and current._proc is not None and current._proc.poll() is None:
                return True
            if current is not None:
                signal_cli._daemon = None
                current.stop()
            try:
                account = get_config().account
                if account not in self.accounts():
                    return False
            except (ConfigError, SignalError):
                return False
            started = signal_cli._JsonRpc(account)
            try:
                started.start()
            except (SignalError, OSError):
                return False
            signal_cli._daemon = started
            return True

    def accounts(self, *, refresh: bool = False) -> list[str]:
        """The linked accounts, asked of signal-cli at most once a minute.

        ``listAccounts`` is a JVM start of its own, and the GUI polls.
        """
        cached = self._accounts_seen
        if cached and not refresh and time.time() - cached[0] < GROUPS_TTL:
            return cached[1]
        accounts = list_accounts()
        self._accounts_seen = (time.time(), accounts)
        return accounts

    # jobs

    def start_job(self, kind: str, work) -> Job:
        """Run ``work()`` on a thread of its own, capturing what it prints."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self.jobs[job.id] = job
            self._jobs_order.append(job.id)
            while len(self._jobs_order) > 20:
                self.jobs.pop(self._jobs_order.popleft(), None)

        def body() -> None:
            for router in (self._stdout, self._stderr):
                if router is not None:
                    router.capture(job.output)
            try:
                self.ensure_daemon()
                job.result = str(work())
                job.ok = True
            except Exception as exc:  # reported, not raised: nobody is waiting on it
                job.ok = False
                job.result = f"{type(exc).__name__}: {exc}"
                job.output.append("\n" + traceback.format_exc())
                log_exception(f"serve: {kind}", exc)
            finally:
                job.finished = time.time()
                for router in (self._stdout, self._stderr):
                    if router is not None:
                        router.capture(None)
                self.note(kind, job.result or ("done" if job.ok else "failed"), ok=bool(job.ok))

        threading.Thread(target=body, daemon=True, name=f"rssignal-{kind}").start()
        return job

    def running(self) -> Job | None:
        with self._lock:
            jobs = [self.jobs[i] for i in self._jobs_order]
        return next((j for j in reversed(jobs) if j.finished is None and j.kind in ("run", "dry-run")), None)

    def run(self, *, dry_run: bool = False) -> Job:
        busy = self.running()
        if busy is not None and not dry_run:
            return busy

        def work() -> str:
            try:
                sent = run_feeds(self.config_path, dry_run=dry_run)
            except AlreadyRunning as exc:
                return f"Nothing to do: {exc}."
            self._groups = None
            return f"{sent} item(s) {'would be sent' if dry_run else 'sent'}."

        return self.start_job("dry-run" if dry_run else "run", work)

    def refresh_images(self, *, dry_run: bool = False) -> Job:
        def work() -> str:
            changed = refresh_group_images(self.config_path, dry_run=dry_run)
            return f"{changed} group picture(s) {'would be ' if dry_run else ''}updated."

        return self.start_job("refresh-images", work)

    def _schedule_loop(self) -> None:
        log_line(f"serve: scheduled at {', '.join(f'{h:02d}:{m:02d}' for h, m in self.schedule)}")
        while not self._stop.is_set():
            when = next_slot(self.schedule, datetime.now().astimezone())
            if when is None:
                return
            while not self._stop.is_set():
                left = (when - datetime.now().astimezone()).total_seconds()
                if left <= 0:
                    break
                self._stop.wait(min(left, 60))
            if self._stop.is_set():
                return
            job = self.run()
            while job.finished is None and not self._stop.is_set():
                self._stop.wait(5)
            self._stop.wait(1)

    def next_run(self) -> str | None:
        when = next_slot(self.schedule, datetime.now().astimezone())
        return when.isoformat(timespec="minutes") if when else None

    # what the GUI shows

    def note(self, kind: str, text: str, *, ok: bool = True, **extra) -> None:
        self.activity.appendleft({"when": _iso(time.time()), "kind": kind, "text": text, "ok": ok, **extra})

    def groups(self, *, refresh: bool = False) -> list[SignalGroup]:
        cached = self._groups
        if cached and not refresh and time.time() - cached[0] < GROUPS_TTL:
            return cached[1]
        self.ensure_daemon()
        groups = [g for g in list_groups() if g.active and not g.blocked]
        self._groups = (time.time(), groups)
        return groups

    def status(self) -> dict:
        try:
            account = get_config().account
        except ConfigError as exc:
            account, problem = None, str(exc)
        else:
            problem = None
        try:
            linked = bool(account) and account in self.accounts()
        except SignalError as exc:
            linked, problem = False, str(exc)
        daemon = signal_cli._daemon
        busy = self.running()
        return {
            "version": __version__,
            "account": account,
            "linked": linked,
            "problem": problem,
            "daemon": daemon is not None,
            "library": library_root(),
            "library_exists": os.path.isdir(library_root()),
            "config": os.path.abspath(self.config_path),
            "log": os.path.abspath(log_path()),
            "schedule": [f"{h:02d}:{m:02d}" for h, m in self.schedule],
            "next_run": self.next_run(),
            "running": busy.view() if busy else None,
            "hostname": socket.gethostname(),
            "api_token": bool(self.api_token),
            "link": dict(self._link),
        }

    def feeds(self, *, refresh: bool = False) -> list[dict]:
        """Every feed, with what its group and the library say about it."""
        feeds = load_feeds(self.config_path)
        try:
            groups = self.groups(refresh=refresh)
            group_error = None
        except SignalError as exc:
            groups, group_error = [], str(exc)
        now = datetime.now(timezone.utc)
        out = []
        for cfg in feeds:
            row: dict = {"name": cfg.name, "source": cfg.library_name, "episodic": cfg.episodic.describe() if cfg.episodic else None}
            group = match_group(groups, cfg.name)
            row["group"] = group.recipient if group else None
            if group_error:
                row["group_error"] = group_error
            mark = read_watermark(group.description) if group else None
            row["watermark"] = mark.isoformat(timespec="minutes") if mark else None
            try:
                parsed = read_feed(cfg)
            except SourceMissing:
                row.update(items=0, waiting=0, note="nothing in the library yet")
                out.append(row)
                continue
            except FeedError as exc:
                row.update(items=0, waiting=0, note=str(exc))
                out.append(row)
                continue
            items = apply_filters([apply_extracts(i, cfg.extract) for i in parsed.items], cfg.filters)
            row["items"] = len(items)
            dated = filter_since(items, None)
            row["newest"] = dated[-1].title if dated else None
            row["newest_date"] = dated[-1].published.date().isoformat() if dated else None
            start = mark or cfg.start
            if start is not None:
                row["waiting"] = len(filter_since(items, start))
            else:
                row["waiting"] = len(dated) if cfg.episodic else min(1, len(dated))
            if cfg.episodic and group is not None:
                due = next_release(cfg.episodic, read_pace(group.description))
                row["next_episode"] = (
                    "now" if due is None or due <= now else due.astimezone().isoformat(timespec="minutes")
                )
            out.append(row)
        return out

    def log_tail(self, lines: int = 200) -> str:
        try:
            with open(log_path(), encoding="utf-8", errors="replace") as fh:
                return "".join(deque(fh, maxlen=lines))
        except OSError:
            return ""

    # sending

    def send(self, message: str, to: str | list[str], attachments: list[str] | None = None, *, via: str) -> list[str]:
        """Send ``message`` to every recipient in ``to``. Returns where it went."""
        targets = [to] if isinstance(to, str) else list(to or [])
        if not targets:
            raise SendRefused("No recipient: give \"to\" (or \"recipients\").")
        if not (message or "").strip() and not attachments:
            raise SendRefused("Nothing to send: the message is empty.")
        self.ensure_daemon()
        groups: list[SignalGroup] = []
        if any(not t.strip().startswith(("+", GROUP_PREFIX)) for t in targets):
            groups = self.groups()
            if not all(_resolvable(t, groups) for t in targets):
                groups = self.groups(refresh=True)
        recipients = [resolve_recipient(t, groups) for t in targets]

        with tempfile.TemporaryDirectory(prefix="rssignal-send-") as folder:
            paths = []
            for index, value in enumerate(attachments or []):
                data, ext = decode_attachment(value, index)
                path = os.path.join(folder, f"attachment-{index + 1}{ext}")
                with open(path, "wb") as fh:
                    fh.write(data)
                paths.append(path)
            for target, recipient in zip(targets, recipients):
                try:
                    send_msg(message or "", recipient=recipient, attachments=paths or None, timeout=SEND_TIMEOUT)
                except Exception as exc:
                    self.note("send", f"to {target}: {exc}", ok=False, via=via)
                    raise
                self.note("send", f"to {target}: {_short(message)}", via=via)
        return recipients

    # linking

    def link(self, device_name: str = "rssignal") -> dict:
        """Start linking this machine to a Signal account; the GUI shows the QR code."""
        if self._link.get("state") == "waiting":
            return dict(self._link)
        binary = signal_cli.find_signal_cli()
        proc = subprocess.Popen([binary, "link", "-n", device_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        uri = proc.stdout.readline().strip()
        if not uri.startswith("sgnl://"):
            proc.kill()
            self._link = {"state": "failed", "error": uri or "signal-cli printed no link"}
            return dict(self._link)
        self._link = {"state": "waiting", "uri": uri, "svg": _qr_svg(uri)}

        def wait() -> None:
            code = proc.wait()
            if code == 0:
                self._link = {"state": "linked"}
                self._accounts_seen = None
                self.note("link", "linked to Signal")
                log_line("serve: linked to Signal")
                self.ensure_daemon()
            else:
                err = (proc.stderr.read() if proc.stderr else "").strip().splitlines()
                self._link = {"state": "failed", "error": err[-1] if err else f"signal-cli link exited {code}"}

        threading.Thread(target=wait, daemon=True, name="rssignal-link").start()
        return dict(self._link)


def _resolvable(target: str, groups: list[SignalGroup]) -> bool:
    try:
        resolve_recipient(target, groups)
        return True
    except SendRefused:
        return False


def _short(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _qr_svg(text: str) -> str | None:
    """The link as an SVG QR code, if ``qrencode`` is installed."""
    binary = shutil.which("qrencode")
    if binary is None:
        return None
    try:
        result = subprocess.run([binary, "-t", "SVG", "-o", "-", "-m", "2", text], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 and "<svg" in result.stdout else None


# --- HTTP -------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    service: Service  # set on the subclass the server makes
    server_version = f"rssignal/{__version__}"

    # Quiet: the add-on log is for runs and sends, not for every poll.
    def log_message(self, format, *args):  # noqa: A002 - the stdlib's name
        pass

    # plumbing

    def _client(self) -> str:
        return self.client_address[0]

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise SendRefused("The request is too big.")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise SendRefused(f"The body is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SendRefused("The body must be a JSON object.")
        return data

    def _gui_ok(self) -> bool:
        if allowed(self._client(), self.service.gui_from):
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "The GUI is not open to this address."})
        return False

    def _api_ok(self) -> bool:
        svc = self.service
        token = svc.api_token
        given = self.headers.get("Authorization", "")
        if token and given == f"Bearer {token}":
            return True
        client = self._client()
        if allowed(client, svc.api_from) or allowed(client, svc.gui_from):
            return True
        svc.note("refused", f"a send from {client}", ok=False)
        log_line(
            f"serve: refused a send from {client} — add it to the allowed "
            "addresses, or send the API token."
        )
        self._json(HTTPStatus.FORBIDDEN, {"error": f"Sending is not open to {client}."})
        return False

    # routes

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        url = urlsplit(self.path)
        path, query = url.path.rstrip("/") or "/", parse_qs(url.query)
        svc = self.service
        try:
            if path == "/v1/about":
                if self._api_ok():
                    self._json(HTTPStatus.OK, {
                        "versions": ["v1", "v2"],
                        "build": 2,
                        "mode": "normal",
                        "version": f"rssignal-{__version__}",
                        "capabilities": {"v2/send": []},
                    })
                return
            if not self._gui_ok():
                return
            if path in ("/", "/index.html"):
                page = resources.files("rssignal").joinpath("ui/index.html").read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif path == "/api/status":
                self._json(HTTPStatus.OK, svc.status())
            elif path == "/api/feeds":
                self._json(HTTPStatus.OK, {"feeds": svc.feeds(refresh="refresh" in query)})
            elif path == "/api/groups":
                groups = svc.groups(refresh="refresh" in query)
                self._json(HTTPStatus.OK, {"groups": [{"name": g.name, "recipient": g.recipient} for g in groups]})
            elif path == "/api/activity":
                self._json(HTTPStatus.OK, {"activity": list(svc.activity)})
            elif path == "/api/log":
                self._json(HTTPStatus.OK, {"log": svc.log_tail()})
            elif path.startswith("/api/jobs/"):
                job = svc.jobs.get(path.rsplit("/", 1)[-1])
                if job is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "No such job."})
                else:
                    self._json(HTTPStatus.OK, job.view())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except (FeedError, SignalError, ConfigError) as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
        path = urlsplit(self.path).path.rstrip("/")
        svc = self.service
        try:
            if path in ("/v2/send", "/v1/send"):
                if not self._api_ok():
                    return
                body = self._body()
                attachments = body.get("base64_attachments") or []
                if isinstance(attachments, str):
                    attachments = [attachments]
                if path == "/v1/send" and body.get("base64_attachment"):
                    attachments = [body["base64_attachment"]]
                svc.send(str(body.get("message") or ""), body.get("recipients") or [], attachments, via="signal-cli-rest-api")
                self._json(HTTPStatus.CREATED, {"timestamp": str(int(time.time() * 1000))})
                return
            if path == "/api/send":
                if not self._api_ok():
                    return
                body = self._body()
                to = body.get("to", body.get("recipients"))
                via = "gui" if allowed(self._client(), svc.gui_from) else "api"
                sent = svc.send(str(body.get("message") or ""), to, body.get("attachments") or [], via=via)
                self._json(HTTPStatus.CREATED, {"sent": sent})
                return
            if not self._gui_ok():
                return
            body = self._body()
            if path == "/api/run":
                self._json(HTTPStatus.ACCEPTED, svc.run(dry_run=bool(body.get("dry_run"))).view())
            elif path == "/api/refresh-images":
                self._json(HTTPStatus.ACCEPTED, svc.refresh_images(dry_run=bool(body.get("dry_run"))).view())
            elif path == "/api/link":
                self._json(HTTPStatus.OK, svc.link())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except SendRefused as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (ValueError, SignalError, FeedError, ConfigError) as exc:
            log_exception(f"serve: {path}", exc)
            self._json(HTTPStatus.BAD_GATEWAY if isinstance(exc, SignalError) else HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def make_server(service: Service, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service})
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server_class = type("Server", (ThreadingHTTPServer,), {"address_family": family, "daemon_threads": True})
    return server_class((host, port), handler)


def serve(
    config_path: str = "feeds.json",
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    on_start: str = "wait",
) -> None:
    """Run the service until interrupted. See the module docstring."""
    schedule = parse_schedule(os.environ.get(SCHEDULE_ENV, ""))
    service = Service(
        config_path,
        schedule=schedule,
        api_token=os.environ.get(API_TOKEN_ENV, "").strip() or None,
        gui_from=os.environ.get(GUI_FROM_ENV, "").strip() or DEFAULT_FROM,
        api_from=os.environ.get(API_FROM_ENV, "").strip() or DEFAULT_FROM,
    )
    try:
        server = make_server(service, host, port)
    except OSError as exc:
        raise SystemExit(f"error: cannot listen on {host}:{port}: {exc.strerror or exc}.")
    service.start()
    when = service.next_run()
    print(
        f"rssignal {__version__} serving on http://{host}:{server.server_address[1]}/ — "
        + (f"next run {when}." if when else "no schedule; runs only when asked.")
    )
    log_line(f"serve: started on {host}:{server.server_address[1]}")
    if on_start in ("run", "dry-run"):
        service.run(dry_run=on_start == "dry-run")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.stop()
        log_line("serve: stopped")
