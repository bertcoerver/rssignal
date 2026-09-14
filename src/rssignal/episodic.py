"""Feeds you watch from the beginning, at a pace you set.

Most feeds are tickers: what matters is what arrived since you last looked, and
rssignal's watermark (:mod:`rssignal.watermark`) is exactly the cursor for that.
A series is the other thing. Its back catalogue is not a backlog to be cleared —
it is the point — and the order to take it in is the order it was made.

Marking a feed ``"episodic"`` changes two things about it. It starts at the
oldest item rather than the newest, and it is *paced*: the cadence says how often
one episode is released, so a channel with nine hundred videos behind it delivers
a series rather than an avalanche.

The cadence reads as a rate — ``"2-weekly"`` is two a week — and is applied as an
interval, that rate turned upside down: two a week is one every three and a half
days. That is the difference between a series and a weekly dump of two episodes,
and the dump is not what anyone means by it.

There is deliberately no catch-up. A machine that was off for a fortnight owes
four episodes on the reading above, and sending four at once is the avalanche in
miniature — the very thing the pace was set to avoid. It releases one and picks
the rhythm back up. Nothing is lost by that: the queue is not going anywhere, and
an episodic feed is not in a hurry by definition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# The cadence grammar: an optional count, then a period. "weekly" is "1-weekly".
CADENCE_RE = re.compile(r"^(?:(\d+)-)?(daily|weekly|monthly)$")

# What each period name is worth. A month is thirty days rather than a calendar
# one: the whole point is to divide the period into equal intervals, and calendar
# months have no equal division to offer — "twice monthly" would mean 14 days in
# February and 15.5 in March.
PERIODS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
}

# How early an episode may go out. Runs come at fixed times of day, and the clock
# is stamped when an episode is sent — a few seconds into its run, or hours into
# it on a machine that slept. Measured strictly, tomorrow's run at the same time
# is those seconds short of a day and holds; if the only run that can reach the
# source is that one (YouTube blocked by day, say), a "daily" feed becomes an
# every-other-day one. Four hours absorbs that, and is capped at a quarter of the
# interval so it never lets a short cadence release two where it meant one.
EARLY = timedelta(hours=4)

# What an unpaced episodic feed is allowed per run: everything it has. Used
# rather than a bare `math.inf` so the caller can slice a list with it.
UNPACED = 1_000_000


@dataclass(frozen=True)
class Cadence:
    """How often an episodic feed releases an episode.

    ``interval`` is ``None`` for an unpaced feed — ``"episodic": true``, which
    asks for oldest-first order and no rate limit at all. That is the honest
    primitive underneath the cadences, and it is occasionally what someone wants
    for a short series, but on a feed with a real archive it will send the lot in
    one run. The string forms are the ones to reach for.
    """

    count: int | None = None
    period: timedelta | None = None

    @property
    def interval(self) -> timedelta | None:
        """The gap between episodes, or ``None`` if this cadence is unpaced."""
        if self.count is None or self.period is None:
            return None
        return self.period / self.count

    def describe(self) -> str:
        """The cadence as a phrase, for a dry run and for error messages."""
        if self.interval is None:
            return "every run"
        hours = self.interval.total_seconds() / 3600
        every = f"{hours / 24:.3g}d" if hours >= 24 else f"{hours:.3g}h"
        return f"{self.count} per {self.period.days}d — one every {every}"


def parse_cadence(value: object, where: str) -> Cadence:
    """Read an ``"episodic"`` config value, or raise :class:`ValueError`.

    ``where`` names the feed for the message. Raising ValueError rather than a
    FeedError keeps this module free of the import — :mod:`rssignal.feeds` turns
    it into one, in the voice the rest of its validation uses.
    """
    if value is True:
        return Cadence()
    if not isinstance(value, str) or not CADENCE_RE.match(value.strip().lower()):
        raise ValueError(
            f"{where} episodic must be a cadence like \"2-weekly\" (two episodes "
            "a week, one every 3.5 days), \"daily\", \"weekly\" or \"monthly\" — "
            f"or true for no pacing at all. Got {value!r}. A month counts as 30 "
            "days, so that a rate divides into equal intervals."
        )

    count, period = CADENCE_RE.match(value.strip().lower()).groups()
    number = int(count) if count else 1
    if number < 1:
        raise ValueError(
            f"{where} episodic {value!r} asks for no episodes at all. Leave the "
            "key off instead."
        )
    return Cadence(count=number, period=PERIODS[period])


def allowance(cadence: Cadence, last_release: datetime | None, now: datetime) -> int:
    """How many episodes ``cadence`` permits right now. Zero, one, or unpaced.

    ``last_release`` is when this feed last let one through, read back off its
    Signal group. ``None`` means never — the feed's first episode goes out
    immediately, which is what starts the clock.

    Never more than one, however long the gap: see this module's docstring on why
    the missed ones are not owed. Due a little before the full interval, too:
    see :data:`EARLY`.
    """
    if cadence.interval is None:
        return UNPACED
    if last_release is None:
        return 1
    return 1 if now >= next_release(cadence, last_release) else 0


def next_release(cadence: Cadence, last_release: datetime | None) -> datetime | None:
    """When the next episode is due, or ``None`` if one is due now.

    Only for saying so out loud — in a dry run, and in the line a paced feed
    prints when it holds an episode back, and for :func:`allowance`, so the two
    can't disagree.
    """
    interval = cadence.interval
    if interval is None or last_release is None:
        return None
    return last_release + interval - min(EARLY, interval / 4)
