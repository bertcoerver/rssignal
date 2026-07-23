"""Remembering how far a feed got, inside its Signal group's description.

rssignal keeps no local state file, and Signal offers nowhere obvious to put one:
``signal-cli`` has no way to read back sent messages (the server hands each
message over once, and a linked secondary device never receives its own sends),
and of everything ``listGroups`` reports, ``description`` is the only free-text
field that is writable, readable back, and stored server-side.

So the watermark lives there, on its own line after the feed's blurb::

    Iedere werkdag van 13.30 tot 14.00 uur op NPO Radio 1 met nieuws en
    achtergronden uit het buitenland.

    [rssignal 2026-07-23T10:03:00+00:00]

The stamp is the publication date of the newest item that was actually sent, not
the time of the run: it is compared against feed timestamps, so it has to be one
of them. Everything here is pure string and datetime work — the reading and
writing of the description itself is :mod:`rssignal.signal_cli`'s job.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# The marker is matched anywhere, not anchored, so a description that has been
# reflowed or edited around still parses. The last match wins: a feed blurb that
# happens to quote the marker shouldn't beat the one rssignal appended.
WATERMARK_RE = re.compile(r"\[rssignal ([^\]\s]+)\]")


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


def strip_watermark(description: str) -> str:
    """Return ``description`` without its marker, trailing blank line and all.

    A description that never had one comes back unchanged apart from surrounding
    whitespace, so this is safe to run over a blurb somebody typed themselves.
    """
    return WATERMARK_RE.sub("", description or "").strip()


def format_watermark(when: datetime) -> str:
    """Render ``when`` as the marker line, normalised to UTC."""
    stamp = when.astimezone(timezone.utc) if when.tzinfo else when
    return f"[rssignal {stamp.isoformat()}]"


def compose_description(blurb: str, when: datetime, *, limit: int) -> str:
    """Join ``blurb`` and the marker for ``when``, within ``limit`` characters.

    The marker is the part that must survive: it is what the next run reads. So
    the blurb is what gets shortened — on a word where possible — and the marker
    is appended afterwards rather than clipped along with it.
    """
    marker = format_watermark(when)
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
