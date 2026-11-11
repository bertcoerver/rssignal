"""Orchestration: read each feed from the library, work out what's new, and send it.

This ties the pieces together: :func:`rssignal.feeds.load_feeds` names the
feeds, :func:`rssignal.library.read_feed` reads what fetchit has filed for each
one, :func:`~rssignal.feeds.apply_filters` and
:func:`~rssignal.feeds.filter_since` narrow it, :func:`~rssignal.feeds.render_message`
turns each survivor into text, and it is sent with
:func:`rssignal.signal_cli.send_msg`.

What an item is, fetchit has decided already, and says so in the item's record.
An audio item goes out as a voice note, plus a link preview card with the
item's own picture. Those go out as two messages: Signal drops a preview card
from any message carrying an attachment. The voice note repeats the episode
title as its body, so the chat list names the episode instead of saying "Voice
Message". Two messages is also the one place an item can end up half sent,
which :mod:`rssignal.pending` exists to finish rather than repeat.

A video item has its file attached to the message. That is one message, not
two: these items get no preview card by default, so nothing is there for the
attachment to displace. Text goes out as text plus its link.

Media too big for one Signal message — video or audio — goes out as the fewest
parts that each fit, one message per part (see :mod:`rssignal.parts`); every
part after the first is captioned ``Title (k/N)``. More messages are more places
to be interrupted, and :mod:`rssignal.pending` finishes an item that was, rather
than repeating what already arrived. A file too big even for
:data:`~rssignal.parts.MAX_PARTS` parts is stepped over for good, watermark and
all, so it is not reconsidered every run.

Each feed sends to a Signal group named after it, created on the first send if it
doesn't exist yet — see :class:`_GroupResolver`.

Feeds are independent of each other, and a run says so: one that fails is
reported and left for the next run, and the ones after it are processed anyway.
A run is usually unattended — a scheduled job — where losing every other feed
to one bad upload is the worst of the possible outcomes.

How far a feed got is remembered in that group's own description, so a feed is
never sent twice and running more often costs nothing. See
:mod:`rssignal.watermark` for why the description, of all places, and
:func:`_send_prepared` for the order things happen in.
"""

from __future__ import annotations

import fcntl
import os
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from . import artwork, cache, pending, timing
from .episodic import allowance, next_release
from .errorlog import log_exception, log_line
from .feeds import (
    FeedConfig,
    FeedItem,
    ParsedFeed,
    SourceMissing,
    apply_extracts,
    apply_filters,
    filter_since,
    is_audio_item,
    is_video_item,
    load_feeds,
    newest,
    preview_fields,
    render_message,
)
from .library import read_feed
from .parts import MAX_BYTES, VideoTooBig, parts_needed, split_temp
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
from .watermark import (
    compose_description,
    read_pace,
    read_watermark,
    strip_watermark,
)


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

    def find(self, name: str) -> SignalGroup | None:
        """Return the group called ``name``, or ``None`` if there isn't one."""
        return match_group(self._listing(), name)

    def groups(self) -> list[SignalGroup]:
        """Every group, including any this run created."""
        return self._listing()

    def resolve(self, cfg: FeedConfig, parsed: ParsedFeed) -> SignalGroup:
        """Return the group called ``cfg.name``, creating it if it doesn't exist.

        A new group holds only this account, is announcement-only (it exists to
        receive a feed, not to be a chat), and takes the feed's own artwork and
        blurb from the library as its picture and description.
        """
        name = cfg.name
        group = self.find(name)
        if group is not None:
            return group

        group = create_group(
            name,
            description=parsed.description or None,
            avatar=parsed.image or None,
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
    sent). With ``dry_run`` set nothing is sent, created, or recorded — each
    candidate is printed instead.

    After sending, each feed group's picture is brought in line with the feed's
    artwork in the library, where that has changed (see :mod:`rssignal.artwork`)
    — never on a dry run or with ``to``.

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

    started = time.monotonic()
    started_wall = time.time()
    kind = " (dry run)" if dry_run else ""
    log_line(f"run started{kind}: {len(feeds)} feed(s)")
    try:
        with signal_daemon():
            sent = _run_prepared(
                feeds, resolver, dry_run=dry_run, to=to, since=since
            )
            # After sending, so it never holds an episode up, and inside the
            # daemon, so the uploads don't each pay for a JVM. --to touches no
            # group and a dry run changes nothing, so neither does this.
            if not dry_run and to is None:
                _refresh_artwork(feeds, resolver)
    except BaseException as exc:
        # Including KeyboardInterrupt and SystemExit: a run stopped by hand or
        # by a scheduler shutting it down is exactly the case this line exists
        # for, and it should not look the same as one that was killed outright.
        log_line(
            f"run aborted after {_elapsed(started, started_wall)}: {exc!r}"
            f"{_sleep_note(started, started_wall)}"
        )
        raise

    log_line(
        f"run finished in {_elapsed(started, started_wall)}: "
        f"{sent} item(s) sent{kind}"
        f"{_sleep_note(started, started_wall, sent)}"
    )
    return sent


def _refresh_artwork(feeds: list[FeedConfig], resolver: _GroupResolver) -> None:
    """The group picture refresh, which must never fail a run.

    Everything that could go wrong for one feed is already contained by
    :func:`~rssignal.artwork.refresh`; this is for what's left — the group
    listing itself failing — which is still no reason to report a run that sent
    its episodes as broken.
    """
    try:
        changed = artwork.refresh(feeds, resolver.groups())
    except Exception as exc:
        print(f"group image refresh failed: {exc}", file=sys.stderr)
        log_exception("group image refresh", exc)
        return
    cache.save()
    if changed:
        log_line(f"group images refreshed: {changed} updated")


def refresh_group_images(config_path: str = "feeds.json", *, dry_run: bool = False) -> int:
    """Bring every feed group's picture in line with the library, now.

    What a run does on its own after sending, on demand. Returns how many groups
    got a new picture — or, with ``dry_run``, how many would have.

    Takes the run lock, like a run: both talk to the same signal-cli account, and
    a second signal-cli can't open an account the first one holds.
    """
    feeds = load_feeds(config_path)
    with ExitStack() as outer:
        if not dry_run:
            outer.enter_context(single_run())
        with signal_daemon():
            resolver = _GroupResolver()
            changed = artwork.refresh(feeds, resolver.groups(), dry_run=dry_run)
    cache.save()
    return changed


# How far the two clocks have to drift apart before the gap is worth reporting.
# Small differences are the clocks themselves, not a sleeping machine.
SLEEP_THRESHOLD = 60


def _slept(started: float, started_wall: float | None) -> bool:
    """Whether the machine suspended partway through this run.

    macOS stops the monotonic clock while the machine is asleep and leaves the
    wall clock running, so the two drifting apart is the suspension itself,
    measured. Anything under :data:`SLEEP_THRESHOLD` is the clocks disagreeing
    with each other rather than a machine that went away.
    """
    if started_wall is None:
        return False
    awake = int(time.monotonic() - started)
    wall = int(time.time() - started_wall)
    return wall - awake > SLEEP_THRESHOLD


def _elapsed(started: float, started_wall: float | None = None) -> str:
    """How long a run took, as ``2m38s``, or both clocks if it spent time asleep.

    On a laptop the monotonic clock answers "how long was this run awake for"
    rather than "how long did it take". Those are different numbers and both are
    worth having: the first is the run's real cost, and the second is why a run
    that started at 15:00 delivered its messages at 18:37.

    ``started_wall`` omitted keeps the plain duration, for callers that have no
    second clock to compare against.
    """
    awake = int(time.monotonic() - started)
    if not _slept(started, started_wall):
        return _duration(awake)
    assert started_wall is not None
    return f"{_duration(awake)} awake, {_duration(int(time.time() - started_wall))} wall"


def _sleep_note(
    started: float, started_wall: float | None, sent: int | None = None
) -> str:
    """The sentence that stops a suspended run from reading as a quiet one.

    ``0 item(s) sent`` is the same line whether there was nothing to send or
    whether every send died with the network when the machine suspended. Those
    are opposite things, so a run that slept says which it was. The reassurance
    at the end is the real guarantee rather than a hope — a feed that fails
    rolls its watermark back (see :func:`_send_prepared`), so the items are
    still queued, not lost.

    ``sent`` omitted is the aborted-run case, where the count means nothing.
    """
    if not _slept(started, started_wall):
        return ""
    if sent is None:
        cause = "the machine slept partway, which is very likely why"
    elif sent:
        cause = "the machine slept partway, so more may have been due"
    else:
        cause = (
            "the machine slept partway, so this may be \"not delivered\" "
            "rather than \"nothing to deliver\""
        )
    return (
        f" — {cause}. Whatever failed kept its place in the feed, and the next "
        "run that stays awake will send it."
    )


def _duration(total: int) -> str:
    """``38s``, ``2m38s``, ``3h37m`` — the two largest units that say anything."""
    hours, rest = divmod(max(total, 0), 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


def _run_prepared(
    feeds: list[FeedConfig],
    resolver: _GroupResolver,
    *,
    dry_run: bool,
    to: str | None,
    since: datetime | None,
) -> int:
    """The body of a run, with one ``signal-cli`` already up and waiting.

    --since is an explicit replay of a particular stretch of a feed, so it
    overrides a pace the same way it overrides a watermark: the gate comes off
    and an episodic feed is read and sent like any other. A dry run lifts it
    too — it is somebody sitting and watching, who is owed the real answer for
    a paced feed ("four waiting, next one Thursday") rather than silence. --to
    touches no group, so there is no clock to read.
    """
    gate = since is None and not dry_run and to is None

    sent = 0
    failed = 0
    missing = 0
    logged: str | None = None
    for cfg in feeds:
        try:
            prep = _prepare_feed(cfg, resolver, gate=gate)
            if prep.error is not None:
                raise prep.error
            sent += _send_prepared(
                cfg, prep, resolver, dry_run=dry_run, to=to, since=since
            )
        except SourceMissing as exc:
            # Nothing went wrong yet: fetchit has not filed anything for this
            # feed. No traceback, but a line in the log every run — a feed whose
            # source is misspelled looks exactly like this, and waits for ever.
            missing += 1
            name = cfg.name or cfg.library_name
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
    if missing:
        print(
            f"{missing} of {len(feeds)} feed(s) skipped: nothing in the library "
            "for them yet.",
            file=sys.stderr,
        )

    # Written once, at the end, rather than on every lookup. Nothing depends on
    # it — see :mod:`rssignal.cache` — so a run that dies before here simply
    # leaves the next one as slow as this one was.
    cache.save()
    return sent


def _report_feed_failure(cfg: FeedConfig, exc: Exception) -> str | None:
    """Explain why a feed was given up on, and carry on with the others.

    A feed is a self-contained unit of work: an upload that won't go through or
    a group that can't be written to are reasons to lose *that* feed for one
    run, and no reason at all to lose the ones after it. Whatever went wrong,
    the next run starts again from the watermark this one didn't move.

    stderr gets one line, because a run that skips a feed is not a run anyone
    wants a screenful about, and because stderr is usually nowhere at all. The
    traceback goes to :mod:`rssignal.errorlog` instead, where it is still there
    tomorrow. Returns the log's path, for the summary line to point at.
    """
    label = cfg.name or cfg.library_name
    print(f"[{label}] skipped: {exc}", file=sys.stderr)
    return log_exception(f"feed {label!r}", exc)


@dataclass
class _Prepared:
    """One feed's items as read and filtered, or the exception that stopped it."""

    parsed: ParsedFeed | None = None
    items: list[FeedItem] = field(default_factory=list)
    error: Exception | None = None


def _prepare_feed(
    cfg: FeedConfig, resolver: _GroupResolver, *, gate: bool
) -> _Prepared:
    """Read and filter one feed from the library, catching whatever went wrong.

    A paced feed with nothing due yet is not read at all; see :func:`_holding`.
    ``gate`` is off for the three cases that have no business being rationed:
    ``--to`` (no group, so no clock), ``--since`` (an explicit replay) and
    ``--dry-run`` (somebody watching, who is owed the real answer).

    A :class:`~rssignal.feeds.SourceMissing` is let through rather than
    caught, so the caller can tell "nothing there yet" from a failure.
    """
    try:
        with timing.step("library read", cfg.name or cfg.library_name):
            if gate and _holding(cfg, resolver):
                return _Prepared(parsed=ParsedFeed(items=[]))
            parsed = read_feed(cfg)
            items = [apply_extracts(item, cfg.extract) for item in parsed.items]
            items = apply_filters(items, cfg.filters)
        return _Prepared(parsed=parsed, items=items)
    except SourceMissing:
        raise
    except Exception as exc:
        return _Prepared(error=exc)


def _holding(cfg: FeedConfig, resolver: _GroupResolver) -> bool:
    """Whether ``cfg`` is a paced feed whose next episode isn't due yet.

    A feed with no group yet has never released anything, so it is never holding:
    its first episode goes out on this run and starts the clock.
    """
    if cfg.episodic is None or cfg.episodic.interval is None:
        return False
    group = resolver.find(cfg.name)
    if group is None:
        return False
    return allowance(cfg.episodic, read_pace(group.description), _now()) < 1


def _now() -> datetime:
    """The current moment, in UTC. One place, so a test can move it."""
    return datetime.now(timezone.utc)


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
    if mark is None and cfg.start is not None:
        # A series told where to begin. The start date stands in for the
        # watermark this feed would have had if it had already got that far.
        mark = cfg.start

    if mark is None:
        if cfg.episodic is not None:
            # A series, opening at its beginning: the whole back catalogue is
            # ahead of us, oldest first. What keeps that from arriving all at
            # once is the pace, applied below.
            due = filter_since(items, None)
        else:
            # Nothing remembered yet. One item, not the whole back catalogue.
            latest = newest(items)
            due = [latest] if latest else []
    else:
        due = filter_since(items, mark)

    if cfg.episodic is not None and since is None:
        due = _paced(cfg, group, due, to)
    if not due:
        return 0

    if dry_run:
        where = to or (group.recipient if group else f"(group {cfg.name!r} would be created)")
        # Counted the same way a real run counts: an item a real run would step
        # over is described, and then not counted, so the tally a dry run
        # reports is the tally the evening would actually produce.
        return sum(_handle_item(cfg, item, where, dry_run=True) for item in due)

    if to:
        recipient = to
    else:
        # The one place a group is created, and only now that there is something
        # to put in it: a typo in a feed's name can't leave a stray group behind.
        group = group or resolver.resolve(cfg, parsed)
        recipient = group.recipient

    # due is oldest-first: each item's marker goes up, then the item itself, so
    # a send that fails part-way leaves the ones behind it untouched and they
    # are retried next run. `current` tracks what the group's description says
    # now, since SignalGroup is frozen and its own copy goes stale immediately.
    current = "" if to else group.description
    sent = 0
    # The clock is stamped with the moment the episode is released, alongside the
    # watermark and in the same write, so the two can't disagree — and so a
    # rollback puts both back together.
    paced = _now() if cfg.episodic is not None else None
    for item in due:
        before = current
        marked = (
            current if to else _record_progress(group, parsed, item, current, paced)
        )
        try:
            delivered = _handle_item(cfg, item, recipient, dry_run=False)
        except Exception:
            # The marker promised an item that never arrived. Put the old
            # description back so the next run tries again.
            if marked != before:
                _rollback(group, before, item)
            raise
        # The watermark stands either way: an item deliberately stepped over is
        # done with, and should not come round again tomorrow. The clock is the
        # other half of the promise, though, and an item nobody was sent is not
        # a release — so a skip hands the cadence slot back, and the next run
        # offers the episode after it rather than making a paced feed sit out a
        # day for nothing.
        if not delivered and paced is not None and marked != before:
            marked = _record_progress(group, parsed, item, before, read_pace(before))
        current = marked
        sent += delivered

    return sent


def _paced(
    cfg: FeedConfig, group: SignalGroup | None, due: list[FeedItem], to: str | None
) -> list[FeedItem]:
    """Cut ``due`` down to what this feed's cadence allows right now.

    ``due`` is oldest-first, so this takes from the front: the next episode in
    the series, not the newest thing the source has posted.

    ``--to`` gets exactly one episode however the feed is paced. It touches no
    group by design, so there is no clock to read and nothing to write one to —
    and an unpaced feed emptying its whole archive onto a phone number is not
    what anyone typing a one-off test means.

    A feed that has no group *yet* is a different thing: it has simply never
    released anything, which is the case the cadence already answers.
    """
    if to is not None:
        return due[:1]

    released = read_pace(group.description) if group is not None else None
    permitted = allowance(cfg.episodic, released, _now())
    if permitted < 1 and due:
        when = next_release(cfg.episodic, released)
        print(
            f"[{cfg.name}] {len(due)} episode(s) waiting; next one due "
            f"{when:%Y-%m-%d %H:%M} UTC ({cfg.episodic.describe()})."
        )
    return due[:permitted]


def _record_progress(
    group: SignalGroup,
    parsed: ParsedFeed,
    item: FeedItem,
    current: str,
    paced: datetime | None = None,
) -> str:
    """Move ``group``'s watermark to ``item``, and return the description now on it.

    Called just before ``item`` is sent, so the group-detail line Signal puts in
    the chat appears above the item rather than after it. ``current`` is what the
    description says at this point in the run — not ``group.description``, which
    is a snapshot from the listing and goes stale after the first item.

    ``paced`` is the release time for an episodic feed, written in the same
    update. The two markers answer different questions — how far through we are,
    and when we were last given a piece of it — and both are needed to work out
    what happens next.

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
        blurb, item.published, limit=GROUP_DESCRIPTION_MAX_CHARS, paced=paced
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
    cfg: FeedConfig,
    item: FeedItem,
    recipient: str,
    *,
    dry_run: bool,
) -> bool:
    """Send (or, in dry-run, describe) a single item from feed ``cfg``.

    Returns whether anything actually went to the group. Not every item that
    gets this far becomes a message — a file too big for any number of parts
    is stepped over — and a run that counted those would report items the
    group never received, which is a worse lie than a quiet evening.
    """
    is_podcast = is_audio_item(item)
    is_video = not is_podcast and is_video_item(item)
    card = preview_fields(item, cfg)
    label = cfg.name or cfg.library_name
    # Clipped here rather than left to send_msg, so a dry run reports the text
    # that would actually go out — long show notes are cut, not printed whole.
    text = clip_body(render_message(item, cfg), card["url"] if card else None)

    if (is_podcast or is_video) and not item.media:
        # fetchit files every audio and video item with its file, so this is an
        # item somebody built by hand, or whose file has gone. Nothing to attach,
        # and an episode that arrives as a bare caption is no episode.
        print(f"[{label}] skipped: {item.title!r} has no file in the library", file=sys.stderr)
        log_line(f"[{label}] skipped: {item.title!r} has no file in the library")
        return False

    if dry_run:
        first_line = text.splitlines()[0] if text else "(no text)"
        print(f"[{label}] -> {recipient}: {first_line}")
        if card:
            print(f"    preview: {card['title']} <{card['url']}>")
            if card["image"]:
                print(f"    preview image: {card['image']}")
        if is_podcast or is_video:
            what = "voice note" if is_podcast else "video"
            suffix = f" (second message, captioned {item.title!r})" if card and is_podcast else ""
            print(f"    {what}{suffix}: {_describe_file(item.media)}")
        return True

    with ExitStack() as stack:
        # What Signal gets: the file itself, or — when it is over the limit — as
        # few parts as will each fit. See :mod:`rssignal.parts`. The library's
        # file is never changed: parts are cut into a temporary directory.
        pieces: list[str] = []
        if is_podcast or is_video:
            assert item.media is not None
            try:
                pieces = stack.enter_context(split_temp(item.media))
            except VideoTooBig as exc:
                # Too big for even the most parts rssignal will send — which it
                # would be again next run. Nothing goes out, and the item isn't
                # reconsidered.
                print(f"[{label}] skipped: {exc}", file=sys.stderr)
                log_line(f"[{label}] skipped: {exc}")
                return False

        preview = None
        if card:
            preview = LinkPreview(
                url=card["url"],
                title=card["title"],
                description=card["description"],
                image=card["image"],
            )

        _send_all(
            _messages(item, text, pieces, preview, voice_note=is_podcast),
            recipient=recipient,
            item=item,
            label=label,
        )
    return True


def _describe_file(path: str | None) -> str:
    """``"<path> (~120 MB, in 2 parts)"``, for a dry run."""
    if not path:
        return "(no file)"
    try:
        size = os.path.getsize(path)
    except OSError:
        return path
    parts = parts_needed(size, MAX_BYTES)
    extra = f", in {parts} parts" if parts > 1 else ""
    return f"{path} (~{size / (1024 * 1024):.0f} MB{extra})"


@dataclass(frozen=True)
class _Message:
    """One Signal message of an item: its body, and what goes with it."""

    text: str
    attachments: list[str] | None = None
    voice_note: bool = False
    preview: LinkPreview | None = None


def _messages(
    item: FeedItem,
    text: str,
    pieces: list[str],
    preview: LinkPreview | None,
    *,
    voice_note: bool,
) -> list[_Message]:
    """The messages ``item`` goes out as, in order.

    Text alone is one message. Media is one message per piece, and a preview
    card is a message of its own ahead of them: Signal drops the card from any
    message carrying an attachment.

    Every piece after the first says which one it is, ``"Title (2/3)"``, so a
    chat list reads as one episode arriving rather than several. After a card,
    even the first piece carries the title rather than an empty body: a
    chat-list row shows the message's own text, falling back to a bare "Voice
    Message" when there is none — and the last message is what the list shows
    for the whole feed. The title is repeated from the card above it, which is a
    small price for a legible chat list.
    """
    total = len(pieces)
    if not pieces:
        return [_Message(text, voice_note=voice_note, preview=preview)]

    def caption(k: int) -> str:
        return item.title if total == 1 else f"{item.title} ({k}/{total})"

    rest = [
        _Message(caption(k), [piece], voice_note)
        for k, piece in enumerate(pieces, start=1)
    ]
    if preview is not None:
        return [_Message(text, preview=preview), *rest]
    # No card, so the first piece travels with the text itself.
    return [_Message(text, [pieces[0]], voice_note), *rest[1:]]


def _send_all(
    messages: list[_Message], *, recipient: str, item: FeedItem, label: str
) -> None:
    """Send ``messages`` in order, never sending one twice across retries.

    More than one message is more than one chance to fail, and a failure between
    them is the one moment in a run where an item is neither sent nor unsent.
    Each message but the last is noted as sent the instant it lands, so if a
    later one never follows — the watermark rolls back and the item comes round
    again — the next run sends only what is actually missing instead of posting
    a second copy of the rest. See :mod:`rssignal.pending`.

    The first message's note is the item's own key, the one it has always had,
    so a card owed from before an upgrade is still recognised. The others carry
    their position and the total: a retry that splits the file differently has
    different pieces to send, and must not mistake them for the old ones.
    """
    if len(messages) == 1:
        only = messages[0]
        send_msg(
            only.text,
            recipient=recipient,
            attachments=only.attachments,
            voice_note=only.voice_note,
            preview=only.preview,
        )
        return

    key = pending.item_key(recipient, item)
    total = len(messages)
    keys = [key] + [f"{key}\n{k}/{total}" for k in range(2, total + 1)]

    for k, (message, note) in enumerate(zip(messages, keys), start=1):
        last = k == total
        if not last and pending.owed(note):
            what = (
                "card"
                if message.preview and not message.attachments
                else f"message {k}/{total}"
            )
            print(
                f"[{label}] {item.title!r}: {what} already sent, sending only "
                "the rest.",
                file=sys.stderr,
            )
            continue
        send_msg(
            message.text,
            recipient=recipient,
            attachments=message.attachments,
            voice_note=message.voice_note,
            preview=message.preview,
        )
        if not last:
            pending.remember(note)

    for note in keys:
        pending.forget(note)
