"""Remembering how far a feed got, inside its Signal group's description.

rssignal keeps no local state file, and Signal offers nowhere obvious to put one:
``signal-cli`` has no way to read back sent messages (the server hands each
message over once, and a linked secondary device never receives its own sends),
and of everything ``listGroups`` reports, ``description`` is the only free-text
field that is writable, readable back, and stored server-side.

So the watermark lives there, on its own line after the feed's blurb::

    Two people argue about films they have not seen. New episode every Thursday.

    [rssignal 2026-07-23T10:03:00+00:00]

The stamp is the publication date of the item being sent, not the time of the
run: it is compared against feed timestamps, so it has to be one of them. It is
written just before that item's messages go out, so the group-detail line Signal
shows in the chat sits above the item rather than after it. Everything here is pure string and datetime work — the reading and
writing of the description itself is :mod:`rssignal.signal_cli`'s job.

An episodic feed (:mod:`rssignal.episodic`) keeps a second marker beside the
first::

    [rssignal 2014-05-04T22:30:00+00:00]
    [rssignal-paced 2026-08-08T09:12:44+00:00]

That one *is* a wall-clock time — the moment the last episode was released — and
it is the only thing a pace can be measured from. The two answer different
questions and neither can be derived from the other: how far through the series
we are, and when we were last given a piece of it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# The marker is matched anywhere, not anchored, so a description that has been
# reflowed or edited around still parses. The last match wins: a feed blurb that
# happens to quote the marker shouldn't beat the one rssignal appended.
#
# The space after the name is what keeps the two markers apart: "[rssignal-paced"
# cannot match a pattern that wants "rssignal" followed by a space.
WATERMARK_RE = re.compile(r"\[rssignal ([^\]\s]+)\]")
PACE_RE = re.compile(r"\[rssignal-paced ([^\]\s]+)\]")


def read_watermark(description: str) -> datetime | None:
    """Return the watermark in ``description``, or ``None`` if there isn't one.

    A marker that can't be parsed as a timestamp counts as absent. Somebody
    editing the group description by hand should cost one duplicate item, not a
    crashed run. A stamp without a timezone is read as UTC, which is what
    rssignal writes.
    """
    matches = WATERMARK_RE.findall(description or "")
    if not matches:
        return None
    try:
        when = datetime.fromisoformat(matches[-1])
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def read_pace(description: str) -> datetime | None:
    """Return the pace marker in ``description``, or ``None`` if there isn't one.

    Absent means "never released an episode", which is what lets a feed's first
    one go out immediately. So an unparseable stamp costs one early episode and
    then the rhythm resumes — the same shape of forgiveness
    :func:`read_watermark` extends, and for the same reason.
    """
    matches = PACE_RE.findall(description or "")
    if not matches:
        return None
    try:
        when = datetime.fromisoformat(matches[-1])
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def strip_watermark(description: str) -> str:
    """Return ``description`` without rssignal's markers, blank lines and all.

    Both markers go: this is what the blurb is recovered with before a new one
    is composed, and a pace marker left behind would be pasted back in below the
    fresh one every run until it crowded the description out.

    A description that never had either comes back unchanged apart from
    surrounding whitespace, so this is safe to run over a blurb somebody typed
    themselves.
    """
    # Paced first — it is the longer name, and stripping "[rssignal …]" out of
    # "[rssignal-paced …]" is not possible anyway, but order makes that explicit.
    without = PACE_RE.sub("", description or "")
    return WATERMARK_RE.sub("", without).strip()


def format_watermark(when: datetime) -> str:
    """Render ``when`` as the marker line, normalised to UTC."""
    stamp = when.astimezone(timezone.utc) if when.tzinfo else when
    return f"[rssignal {stamp.isoformat()}]"


def format_pace(when: datetime) -> str:
    """Render ``when`` as the pace marker line, normalised to UTC."""
    stamp = when.astimezone(timezone.utc) if when.tzinfo else when
    return f"[rssignal-paced {stamp.isoformat()}]"


def compose_description(
    blurb: str, when: datetime, *, limit: int, paced: datetime | None = None
) -> str:
    """Join ``blurb`` and the marker(s) within ``limit`` characters.

    The markers are the part that must survive: they are what the next run reads.
    So the blurb is what gets shortened — on a word where possible — and they are
    appended afterwards rather than clipped along with them.

    ``paced`` adds the release-time marker, for an episodic feed. Two markers
    cost about a hundred characters of a group description's four hundred and
    eighty, which is the blurb's to give.
    """
    markers = [format_watermark(when)]
    if paced is not None:
        markers.append(format_pace(paced))
    marker = "\n".join(markers)

    blurb = strip_watermark(blurb)
    room = limit - len(marker) - 2  # the two newlines between them

    if len(blurb) > room:
        blurb = shorten(blurb, room)
    return f"{blurb}\n\n{marker}" if blurb else marker


def shorten(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, on a word where one is close by."""
    text = text.strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ""
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.—-") + "…"
