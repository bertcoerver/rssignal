"""The half of a two-message item that has not gone out yet.

An episode with a preview card is two Signal messages, not one: Signal drops the
card from any message carrying an attachment, so the text and its card go first
and the voice note follows on its own (see :func:`rssignal.run._handle_item`).
That is one item and two chances to fail, and the gap between them is the only
place in a run where "sent" is neither true nor false.

What used to happen there: the card arrived, the voice note didn't, the item's
watermark was rolled back so the episode wasn't lost — and the next run sent the
card again. A flaky upload retried three times left three identical cards in the
group and one episode.

So the first message writes down what it still owes before the second is
attempted, and a later run that finds an item already owed sends only the voice
note. The rollback is unchanged and still the thing that brings the item back;
this only stops it bringing the card back with it.

Small and deliberately fragile-tolerant. The file is written the moment the card
lands, because the case it exists for includes the process being killed a second
later. If it can't be read or written, everything degrades to exactly the old
behaviour — a duplicate card, never a lost episode — so nothing in here is ever
allowed to raise.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

from . import cache

# Where the note lives. Beside the cache, for the same reason: a directory whose
# whole contract is "local, disposable, never synced". ``RSSIGNAL_PENDING`` moves it.
PENDING_ENV = "RSSIGNAL_PENDING"
DEFAULT_PENDING_NAME = "pending.json"

# How long an unfinished item stays unfinished. An episode whose audio can never
# be sent — a file that 404s now, a format signal-cli won't take — would
# otherwise suppress its own card forever. A week is far longer than any real
# retry and short enough that the debt is eventually written off.
TTL = 7 * 24 * 60 * 60


def path() -> str:
    """Where the note is, honouring :data:`PENDING_ENV`."""
    override = os.environ.get(PENDING_ENV, "").strip()
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.dirname(cache.path()), DEFAULT_PENDING_NAME)


def item_key(recipient: str, item) -> str:
    """A stable name for ``item`` in ``recipient``, across runs and machines.

    Whatever the feed uses for identity, in the order it can be trusted: its own
    id, then the link, then the enclosure, and the title only if a feed offers
    nothing better. Getting this wrong in either direction is survivable — an
    unrecognised item sends its card again, a wrongly matched one skips a card —
    which is why it can afford to be this simple.
    """
    identity = (
        item.extra.get("id")
        or item.link
        or item.enclosure_url
        or item.title
    )
    return f"{recipient}\n{identity}"


def owed(key: str) -> bool:
    """Whether ``key``'s first message already went out and its second didn't."""
    stored_at = _read().get(key)
    if not isinstance(stored_at, (int, float)):
        return False
    return time.time() - stored_at <= TTL


def remember(key: str) -> None:
    """Note that ``key``'s first message is out and the rest of it is not."""
    entries = _read()
    entries[key] = time.time()
    _write(entries)


def forget(key: str) -> None:
    """Note that ``key`` is fully sent — or is never going to be."""
    entries = _read()
    if entries.pop(key, None) is None:
        return  # Nothing owed, so nothing to write: the common case by far.
    _write(entries)


def _read() -> dict[str, float]:
    try:
        with open(path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}  # Missing, unreadable, or garbage: nothing is owed.
    return raw if isinstance(raw, dict) else {}


def _write(entries: dict[str, float]) -> None:
    """Replace the file, dropping anything past its :data:`TTL` on the way.

    Written to a temporary file and renamed over the real one, so a run killed
    mid-write leaves the previous note intact rather than a truncated file.
    """
    cutoff = time.time() - TTL
    keep = {
        key: stored_at
        for key, stored_at in entries.items()
        if isinstance(stored_at, (int, float)) and stored_at >= cutoff
    }
    target = path()
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(target) or ".", prefix=".pending-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(keep, fh)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError):
        # A note that can't be written costs a duplicate card one day. Not worth
        # a word, and certainly not worth failing a send over.
        return
