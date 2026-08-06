"""Orchestration: read feeds, work out what's new, and send one message per item.

This ties the pieces together: :func:`rssignal.feeds.load_feeds` /
:func:`~rssignal.feeds.parse_feed` produce items, :func:`~rssignal.feeds.apply_filters`
and :func:`~rssignal.feeds.filter_since` narrow them,
:func:`~rssignal.feeds.render_message` turns each survivor into text, and it is
sent with :func:`rssignal.signal_cli.send_msg`. An item carrying audio — decided
per item by :func:`~rssignal.feeds.is_audio_item`, with nothing to configure —
downloads its enclosure and sends it as a voice note, plus a link preview card
whose artwork is downloaded alongside it. Those go out as two messages: Signal
drops a preview card from any message carrying an attachment. The voice note
repeats the episode title as its body, so the chat list names the episode instead
of saying "Voice Message". Two messages is also the one place an item can end up
half sent, which :mod:`rssignal.pending` exists to finish rather than repeat.

An item that only *links* to a video — :func:`~rssignal.video.is_video_item`,
decided per item from the link in the same spirit — has its video fetched and
attached to the message. That is one message, not two: these items get no
preview card by default, so nothing is there for the attachment to displace. A
video that can't be fetched is a warning, not a failure: the item still goes out
as text and its link, because a readable message beats no message. The one video
that produces no message at all is one too short to be worth a message — a
YouTube Short — which is dropped where it stands, watermark and all, so it is
not reconsidered every run.

Each feed sends to a Signal group named after it, created on the first send if it
doesn't exist yet — see :class:`_GroupResolver`.

Feeds are independent of each other, and a run says so: one that fails is
reported and left for the next run, and the ones after it are processed anyway.
A run is usually unattended — a scheduled job, an Apple Shortcut — where losing
every other feed to one bad video is the worst of the possible outcomes.

How far a feed got is remembered in that group's own description, so a feed is
never sent twice and running more often costs nothing. See
:mod:`rssignal.watermark` for why the description, of all places, and
:func:`_send_prepared` for the order things happen in.

Feeds are *fetched* all at once and *sent* one at a time. Fetching is nearly all
waiting on other people's servers and no feed's fetch can affect another's, so a
run waits once instead of twelve times over; see :func:`_prepare_feeds`. Sending
stays strictly sequential, because that is where the ordering promises above live.
"""

from __future__ import annotations

import fcntl
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

from . import cache, pending, timing
from .download import download_temp
from .errorlog import log_exception, log_line
from .feeds import (
    FeedConfig,
    FeedError,
    FeedItem,
    ParsedFeed,
    SourceBlocked,
    apply_filters,
    filter_since,
    is_audio_item,
    load_feeds,
    newest,
    parse_feed,
    preview_fields,
    render_message,
)
from .signal_cli import (
    GROUP_DESCRIPTION_MAX_CHARS,
    LinkPreview,
    SignalError,
    SignalGroup,
    clip_body,
    create_group,
    daemon as signal_daemon,
    list_groups,
    match_group,
    receive,
    send_msg,
    update_group,
)
from .video import VideoTooShort, is_video_item, resolve_video, video_temp
from .watermark import compose_description, read_watermark, strip_watermark


# How long any one network read may sit there before it is given up on. This is
# feedparser's only timeout — it accepts none of its own, so it goes through the
# default socket timeout or it doesn't happen at all.
FEED_SOCKET_TIMEOUT = 30

# How many feeds are fetched at once. The work is nearly all waiting on other
# people's servers, so this is well above the core count on purpose; the ceiling
# is politeness to those servers rather than anything local.
DEFAULT_FEED_WORKERS = 8
FEED_WORKERS_ENV = "RSSIGNAL_FEED_WORKERS"

# Where the "one run at a time" lock lives. Beside the cache rather than beside
# feeds.json: the repo may sit in a synced folder, and a lock file is the last
# thing that should be replicated to another machine. ``RSSIGNAL_LOCK`` moves it.
LOCK_ENV = "RSSIGNAL_LOCK"
DEFAULT_LOCK_NAME = "run.lock"


class AlreadyRunning(Exception):
    """Raised when another rssignal run holds the lock."""


def lock_path() -> str:
    """Where the run lock is, honouring :data:`LOCK_ENV`."""
    override = os.environ.get(LOCK_ENV, "").strip()
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.dirname(cache.path()), DEFAULT_LOCK_NAME)


@contextmanager
def single_run() -> Iterator[None]:
    """Hold the run lock for the block, or raise :class:`AlreadyRunning`.

    A run is not safe to overlap with itself. How far a feed got is read from
    its group's description and written back within one run (see
    :func:`_send_prepared`), so two runs at once both read the same watermark,
    both decide the same items are new, and both send them. Nothing downstream
    can undo that: the duplicate is a real message in a real chat.

    Overlapping is easy to arrange by accident rather than exotic. A run that
    has videos to fetch takes minutes, and a scheduler firing every fifteen
    does not ask whether the last one has finished.

    The lock is an ``flock`` on a file, which the kernel releases when the
    process ends however it ends — including the kill this is most needed
    against. A pid file would need the dead run to clean up after itself, which
    is exactly what a killed run cannot do.
    """
    path = lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Opened, never truncated: the file is a handle to lock, not a place to
    # keep anything. Deleting it between runs is harmless.
    handle = open(path, "a")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning(
                f"another rssignal run is still going (lock held on {path})"
            ) from exc
        yield
    finally:
        handle.close()


class _GroupResolver:
    """Maps a feed's name to the group it sends to, creating it if needed.

    The listing is fetched once and reused: every ``signal-cli`` call pays a JVM
    startup, so resolving N feeds should not cost N listings. A group created
    here is added to the cached listing rather than triggering a re-read.

    ``receive`` runs once, before that first listing. rssignal is a linked
    secondary device, so ``listGroups`` reads local state that only catches up
    when the sync queue is drained. Refreshing lazily — on a miss — is not
    enough: a group you have *left* still reads as active until then, and
    matching it means sending into a group you are no longer in. signal-cli
    exits 0 doing that, so the failure is completely silent.
    """

    def __init__(self) -> None:
        self._groups: list[SignalGroup] | None = None
        self._lock = threading.Lock()

    def _listing(self) -> list[SignalGroup]:
        with self._lock:
            if self._groups is None:
                receive()
                self._groups = list_groups()
            return self._groups

    def prime(self) -> None:
        """Fetch the listing now, so a later :meth:`find` is free.

        Almost all of what this costs is spent waiting rather than working —
        ``receive`` sits for its whole timeout listening for messages that
        usually aren't coming — so :func:`run_feeds` starts it alongside the feed
        fetches rather than before them, and the wait comes out of a window that
        was going to be spent on the network anyway.

        A failure here is deliberately swallowed. Nothing is cached, so the first
        :meth:`find` in the sequential phase simply tries again, and the failure
        surfaces there — attached to a feed, reported like any other feed's
        failure, instead of coming out of a worker thread with nothing to pin it
        to.
        """
        try:
            self._listing()
        except Exception:
            pass

    def find(self, name: str) -> SignalGroup | None:
        """Return the group called ``name``, or ``None`` if there isn't one."""
        return match_group(self._listing(), name)

    def resolve(self, name: str, parsed: ParsedFeed) -> SignalGroup:
        """Return the group called ``name``, creating it if it doesn't exist.

        A new group holds only this account, is announcement-only (it exists to
        receive a feed, not to be a chat), and takes the feed's own artwork and
        blurb as its picture and description.
        """
        group = self.find(name)
        if group is not None:
            return group

        with ExitStack() as stack:
            avatar = _local_image(parsed.image_url, stack, name, what="group image")
            group = create_group(
                name,
                description=parsed.description or None,
                avatar=avatar or None,
                announcement_only=True,
            )

        # Creating a group is not something to do quietly: a typo in a feed's
        # name would otherwise leave a stray group behind without a word.
        print(f"Created group {group.name!r} for this feed.")
        self._listing().append(group)
        return group


def run_feeds(
    config_path: str = "feeds.json",
    *,
    dry_run: bool = False,
    to: str | None = None,
    since: datetime | None = None,
) -> int:
    """Process every feed in ``config_path`` and send whatever is new.

    Each feed goes to the Signal group named after it, which is created on the
    first send if it doesn't exist. ``to`` overrides that for every feed at once
    and touches no group — the safe way to try a real send against your own
    number. ``since`` overrides every stored watermark, for replaying a stretch
    of a feed by hand.

    Returns the number of items sent (or, in ``dry_run`` mode, that would be
    sent). With ``dry_run`` set nothing is sent, downloaded, created, or
    recorded — each candidate is printed instead.

    A feed that fails does not stop the others: it is reported on stderr and
    skipped, and its watermark is left where it was, so the next run picks it up
    from the same place. Only something that stops there being feeds at all —
    an unreadable config — raises.

    The run's start and end are written to the error log (see
    :func:`~rssignal.errorlog.log_line`), which is what a run killed partway
    leaves nothing else of.

    Only one run happens at a time; a second one raises
    :class:`AlreadyRunning` rather than sending everything twice. A dry run
    takes no lock — it sends nothing, so it cannot collide with anything, and
    it should stay usable while a real run is going.
    """
    with ExitStack() as outer:
        if not dry_run:
            outer.enter_context(single_run())
        return _locked_run(config_path, dry_run=dry_run, to=to, since=since)


def _locked_run(
    config_path: str,
    *,
    dry_run: bool,
    to: str | None,
    since: datetime | None,
) -> int:
    """The body of :func:`run_feeds`, with the run lock already held."""
    feeds = load_feeds(config_path)
    resolver = _GroupResolver()

    # feedparser takes no timeout of its own, and one unresponsive host would
    # otherwise hold the run open with no way out. Set here rather than at import
    # so it is a property of a run, not of having imported rssignal.
    socket.setdefaulttimeout(FEED_SOCKET_TIMEOUT)

    started = time.monotonic()
    kind = " (dry run)" if dry_run else ""
    log_line(f"run started{kind}: {len(feeds)} feed(s)")
    try:
        with signal_daemon():
            sent = _run_prepared(
                feeds, resolver, dry_run=dry_run, to=to, since=since
            )
    except BaseException as exc:
        # Including KeyboardInterrupt and SystemExit: a run stopped by hand or
        # by a scheduler shutting it down is exactly the case this line exists
        # for, and it should not look the same as one that was killed outright.
        log_line(f"run aborted after {_elapsed(started)}: {exc!r}")
        raise
    log_line(f"run finished in {_elapsed(started)}: {sent} item(s) sent{kind}")
    return sent


def _elapsed(started: float) -> str:
    """A monotonic start time as ``2m38s`` — how long, not how many seconds."""
    total = int(time.monotonic() - started)
    minutes, seconds = divmod(total, 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


def _run_prepared(
    feeds: list[FeedConfig],
    resolver: _GroupResolver,
    *,
    dry_run: bool,
    to: str | None,
    since: datetime | None,
) -> int:
    """The body of a run, with one ``signal-cli`` already up and waiting."""
    # Fetching is the slow part and feeds don't depend on each other, so it all
    # happens at once; sending stays sequential below. With --to no group is
    # touched at all, so there is no listing worth priming.
    prepared = _prepare_feeds(feeds, resolver, prime=to is None)

    sent = 0
    failed = 0
    blocked = 0
    logged: str | None = None
    for cfg, prep in zip(feeds, prepared):
        try:
            if prep.error is not None:
                raise prep.error
            sent += _send_prepared(
                cfg, prep, resolver, dry_run=dry_run, to=to, since=since
            )
        except SourceBlocked as exc:
            # Nothing went wrong, so this is not counted with the failures and
            # leaves no traceback: the source is deliberately unreachable right
            # now and the watermark is waiting for the next run. It does get a
            # line in the log, though — an unattended run throws stderr away, and
            # "that feed has gone quiet" is a question worth being able to answer
            # afterwards without guessing at which evenings the block was on.
            blocked += 1
            name = cfg.name or cfg.url
            print(f"[{name}] skipped: {exc}", file=sys.stderr)
            log_line(f"[{name}] skipped: {exc}")
        except Exception as exc:
            failed += 1
            logged = _report_feed_failure(cfg, exc) or logged

    if failed:
        where = f"; full tracebacks in {logged}" if logged else ""
        print(
            f"{failed} of {len(feeds)} feed(s) failed{where}.", file=sys.stderr
        )
    if blocked:
        print(
            f"{blocked} of {len(feeds)} feed(s) skipped: source blocked from "
            "this machine.",
            file=sys.stderr,
        )

    # Written once, at the end, rather than on every lookup. Nothing depends on
    # it — see :mod:`rssignal.cache` — so a run that dies before here simply
    # leaves the next one as slow as this one was.
    cache.save()
    return sent


def _report_feed_failure(cfg: FeedConfig, exc: Exception) -> str | None:
    """Explain why a feed was given up on, and carry on with the others.

    A feed is a self-contained unit of work: a video that won't fetch, a source
    that is down, a group that can't be written to are all reasons to lose *that*
    feed for one run, and no reason at all to lose the ones after it. Whatever
    went wrong, the next run starts again from the watermark this one didn't
    move.

    stderr gets one line, because a run that skips a feed is not a run anyone
    wants a screenful about, and because stderr is usually nowhere at all — this
    runs from cron and from Shortcuts. The traceback goes to
    :mod:`rssignal.errorlog` instead, where it is still there tomorrow. Returns
    the log's path, for the summary line to point at.
    """
    label = cfg.name or cfg.url
    print(f"[{label}] skipped: {exc}", file=sys.stderr)
    return log_exception(f"feed {label!r}", exc)


@dataclass
class _Prepared:
    """One feed's fetched-and-filtered items, or the exception that stopped it.

    The parallel phase can't report a failure itself — a worker thread has no
    turn of its own, and a traceback surfacing out of order would belong to no
    feed in particular. So it carries the exception back instead, and the
    sequential phase raises it where that feed's turn is, into the same handler
    that has always dealt with a feed going wrong.
    """

    parsed: ParsedFeed | None = None
    items: list[FeedItem] = field(default_factory=list)
    error: Exception | None = None


def _prepare_feeds(
    feeds: list[FeedConfig], resolver: _GroupResolver, *, prime: bool
) -> list[_Prepared]:
    """Fetch and filter every feed at once, in ``feeds`` order.

    This is the whole of the parallelism. It is safe to do here and nowhere else
    because fetching a feed touches nothing shared: it reads its own url, builds
    its own items, and hands them back. Everything with an ordering requirement —
    reading a watermark, creating a group, sending, moving the watermark on —
    stays in the sequential phase, where it always was.

    Results come back in configuration order however the threads finished, so a
    run's output reads the same as it ever did.
    """
    workers = _worker_count(len(feeds))
    with _whole_lines_stderr():
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="rssignal-feed"
        ) as pool:
            if prime:
                pool.submit(resolver.prime)
            futures = [pool.submit(_prepare_feed, cfg) for cfg in feeds]
            return [future.result() for future in futures]


def _prepare_feed(cfg: FeedConfig) -> _Prepared:
    """Fetch and filter one feed, catching whatever went wrong. Runs on a worker."""
    try:
        with timing.step("feed fetch", cfg.name or cfg.url):
            parsed = parse_feed(cfg)
            items = apply_filters(parsed.items, cfg.filters)
        return _Prepared(parsed=parsed, items=items)
    except Exception as exc:
        return _Prepared(error=exc)


def _worker_count(feeds: int) -> int:
    """How many feeds to fetch at once — never more than there are feeds."""
    try:
        configured = int(os.environ.get(FEED_WORKERS_ENV, "") or DEFAULT_FEED_WORKERS)
    except ValueError:
        configured = DEFAULT_FEED_WORKERS
    return max(1, min(configured, feeds or 1))


@contextmanager
def _whole_lines_stderr() -> Iterator[None]:
    """Keep threads from writing over each other's warnings, for this block.

    Several feeds fetching at once means several of them can have something to
    complain about at once, and ``print`` reaches stderr as more than one write —
    the text, then the newline — so two threads interleave *within* a line and
    produce something neither of them said. Buffering each thread's output until
    it has a full line, and writing that line under a lock, is enough to stop it:
    a warning may land in a surprising order, but it always lands whole.
    """
    real = sys.stderr
    sys.stderr = _LineSafeStderr(real)  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.stderr.flush()
        sys.stderr = real


class _LineSafeStderr:
    """Buffers per thread, emits whole lines under one lock. See above."""

    def __init__(self, target) -> None:
        self._target = target
        self._lock = threading.Lock()
        self._local = threading.local()

    def write(self, text: str) -> int:
        buffered = getattr(self._local, "buffer", "") + text
        head, newline, tail = buffered.rpartition("\n")
        if newline:
            with self._lock:
                self._target.write(head + newline)
                self._target.flush()
        self._local.buffer = tail
        return len(text)

    def flush(self) -> None:
        # A last line with no newline of its own still has to get out.
        leftover = getattr(self._local, "buffer", "")
        with self._lock:
            if leftover:
                self._target.write(leftover)
            self._target.flush()
        self._local.buffer = ""

    def __getattr__(self, name: str):
        return getattr(self._target, name)


def _send_prepared(
    cfg: FeedConfig,
    prep: _Prepared,
    resolver: _GroupResolver,
    *,
    dry_run: bool,
    to: str | None,
    since: datetime | None,
) -> int:
    """Send one feed's new items, and record how far it got. Returns the count.

    The order here is deliberate. The group is looked up *before* deciding what
    to send, because the watermark lives on it — but it is only ever *created*
    once there is something to send, so a typo in a feed's name can't leave a
    stray group behind.

    Each item's watermark is written *before* that item is sent, so the
    "changed the group description" line Signal adds to the chat sits above the
    item it belongs to rather than below it. A send that then fails puts the
    previous description back, so the item is retried next run instead of being
    lost to a marker for something that never arrived.
    """
    assert prep.parsed is not None  # a _Prepared without an error always has one
    parsed: ParsedFeed = prep.parsed
    items = prep.items
    if not items:
        return 0

    # With --to, no group is touched at all: nothing to read a watermark from,
    # and nothing to write one to. That leaves --to what it has always been —
    # a send that has no effect on rssignal's idea of where a feed got to.
    group = None if to else resolver.find(cfg.name)
    mark = since or (read_watermark(group.description) if group else None)

    if mark is None:
        # Nothing remembered yet. One item, not the whole back catalogue.
        latest = newest(items)
        due = [latest] if latest else []
    else:
        due = filter_since(items, mark)
    if not due:
        return 0

    if dry_run:
        where = to or (group.recipient if group else f"(group {cfg.name!r} would be created)")
        for item in due:
            _handle_item(cfg, item, where, dry_run=True)
        return len(due)

    if to:
        recipient = to
    else:
        # The one place a group is created, and only now that there is something
        # to put in it: a typo in a feed's name can't leave a stray group behind.
        group = group or resolver.resolve(cfg.name, parsed)
        recipient = group.recipient

    # due is oldest-first: each item's marker goes up, then the item itself, so
    # a send that fails part-way leaves the ones behind it untouched and they
    # are retried next run. `current` tracks what the group's description says
    # now, since SignalGroup is frozen and its own copy goes stale immediately.
    current = "" if to else group.description
    sent = 0
    for item in due:
        marked = current if to else _record_progress(group, parsed, item, current)
        try:
            _handle_item(cfg, item, recipient, dry_run=False)
        except Exception:
            # The marker promised an item that never arrived. Put the old
            # description back so the next run tries again.
            if marked != current:
                _rollback(group, current, item)
            raise
        current = marked
        sent += 1

    return sent


def _record_progress(
    group: SignalGroup, parsed: ParsedFeed, item: FeedItem, current: str
) -> str:
    """Move ``group``'s watermark to ``item``, and return the description now on it.

    Called just before ``item`` is sent, so the group-detail line Signal puts in
    the chat appears above the item rather than after it. ``current`` is what the
    description says at this point in the run — not ``group.description``, which
    is a snapshot from the listing and goes stale after the first item.

    The blurb kept is the group's own, not the feed's: edit a group description
    in Signal and rssignal moves the marker around your text instead of pasting
    the feed's boilerplate back over it every run. The feed's blurb is only the
    fallback, for a group that hasn't got one.

    Failing to record is reported but not raised, and ``current`` comes back
    unchanged. Sending the item is the point; a marker that didn't land only
    means it goes out again on the next run.
    """
    blurb = strip_watermark(current) or parsed.description
    description = compose_description(
        blurb, item.published, limit=GROUP_DESCRIPTION_MAX_CHARS
    )
    try:
        update_group(group.id, description=description)
    except SignalError as exc:
        print(
            f"[{group.name}] recording how far the feed got failed: {exc}\n"
            f"{item.title!r} is still being sent, and will be sent again on the "
            "next run.",
            file=sys.stderr,
        )
        return current
    return description


def _rollback(group: SignalGroup, description: str, item: FeedItem) -> None:
    """Put ``description`` back after ``item``'s send failed.

    The marker went up first so it would appear above the item in the chat; when
    the item never arrives, that marker is a promise about something that isn't
    there, and leaving it would mean the item is never sent at all. Undoing it
    hands the item back to the next run.

    A rollback that itself fails is only reported. The send failure is what gets
    raised — it is the real problem, and burying it under a second one would
    help nobody.
    """
    try:
        update_group(group.id, description=description)
    except SignalError as exc:
        print(
            f"[{group.name}] {item.title!r} failed to send, and undoing its "
            f"watermark failed too: {exc}\nIt will not be sent again — replay it "
            "with `rssignal run --since`.",
            file=sys.stderr,
        )


def _handle_item(
    cfg: FeedConfig, item: FeedItem, recipient: str, *, dry_run: bool
) -> None:
    """Send (or, in dry-run, describe) a single item from feed ``cfg``."""
    is_podcast = is_audio_item(item)
    # An item with an audio enclosure is already an episode; asking ARTE about
    # it as well would be a pointless round trip.
    is_video = not is_podcast and is_video_item(item)
    card = preview_fields(item, cfg)
    label = cfg.name or cfg.url
    # Clipped here rather than left to send_msg, so a dry run reports the text
    # that would actually go out — long show notes are cut, not printed whole.
    text = clip_body(render_message(item, cfg), card["url"] if card else None)

    if dry_run:
        first_line = text.splitlines()[0] if text else "(no text)"
        print(f"[{label}] -> {recipient}: {first_line}")
        if card:
            print(f"    preview: {card['title']} <{card['url']}>")
            if card["image_url"]:
                print(f"    preview image: {card['image_url']}")
        if is_podcast:
            suffix = f" (second message, captioned {item.title!r})" if card else ""
            print(f"    voice note{suffix}: {item.enclosure_url}")
        if is_video:
            # Resolving is a couple of small requests and no download, so a dry
            # run can report the resolution and size it would really arrive at.
            try:
                print(f"    video: {resolve_video(item).describe()}")
            except VideoTooShort as exc:
                print(f"    nothing sent: {exc}")
            except FeedError as exc:
                print(f"    video: unavailable ({exc})")
        return

    if is_video:
        # Resolved before anything is sent, because the answer can be "don't
        # send this at all" — a Short is not a message.
        try:
            plan = resolve_video(item)
        except VideoTooShort as exc:
            print(f"[{label}] skipped: {exc}", file=sys.stderr)
            return
        except FeedError as exc:
            print(f"[{label}] video skipped: {exc}", file=sys.stderr)
            plan = None

    with ExitStack() as stack:
        attachments = None
        if is_podcast:
            attachments = [stack.enter_context(download_temp(item.enclosure_url))]
        elif is_video and plan is not None:
            # The video is the best part of the message but not the whole of
            # it: if it can't be had, the text and its link still can.
            try:
                attachments = [stack.enter_context(video_temp(item, plan=plan))]
            except FeedError as exc:
                print(f"[{label}] video skipped: {exc}", file=sys.stderr)

        preview = None
        if card:
            preview = LinkPreview(
                url=card["url"],
                title=card["title"],
                description=card["description"],
                image=_local_image(card["image_url"], stack, label),
            )

        if attachments and preview:
            # Signal drops the preview card when the same message carries an
            # attachment, so the card and the audio go out separately: the text
            # and card first, then the voice note on its own.
            #
            # Two messages is two chances to fail, and a failure between them is
            # the one moment in a run where an item is neither sent nor unsent.
            # The card is noted as owed the instant it lands, so if the voice
            # note never follows — the watermark rolls back and the item comes
            # round again — this run sends what is actually missing instead of
            # posting a second copy of the card. See :mod:`rssignal.pending`.
            key = pending.item_key(recipient, item)
            if not pending.owed(key):
                send_msg(text, recipient=recipient, preview=preview)
                pending.remember(key)
            else:
                print(
                    f"[{label}] {item.title!r}: card already sent, sending only "
                    "the voice note.",
                    file=sys.stderr,
                )
            send_msg(
                # The title, not an empty body. A chat-list row shows the
                # message's own text, falling back to a bare "Voice Message"
                # when there is none — and the voice note is the last message
                # in the group, so that fallback is what the list would show
                # for the whole feed. The title is repeated from the card
                # above it, which is a small price for a legible chat list.
                item.title,
                recipient=recipient,
                attachments=attachments,
                voice_note=True,
            )
            pending.forget(key)
        else:
            send_msg(
                text,
                recipient=recipient,
                attachments=attachments,
                voice_note=is_podcast,
                preview=preview,
            )


def _local_image(
    url: str | None, stack: ExitStack, label: str, *, what: str = "preview image"
) -> str:
    """Download an image, returning its local path or ``""``.

    Artwork is decoration: if it can't be fetched the message (or the group)
    should still happen, so a failed download degrades to no image.
    """
    if not url:
        return ""
    try:
        return stack.enter_context(download_temp(url, default_suffix=".jpg"))
    except FeedError as exc:
        print(f"[{label}] {what} skipped: {exc}", file=sys.stderr)
        return ""
