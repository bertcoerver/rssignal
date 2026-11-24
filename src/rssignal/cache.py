"""Remembered answers to questions whose answers don't change.

Strictly a speed cache, and the distinction matters: nothing here is ever
consulted to decide *what to send*. How far a feed got is remembered in its
Signal group's description and nowhere else (see :mod:`rssignal.watermark`),
which is what lets rssignal run from anywhere, on a machine that keeps no state,
and still never send an item twice. Deleting this file costs at most a
redundant upload and changes nothing about what is sent. That property is worth
protecting: if something ever needs to be cached that *would* change what gets
sent, it does not belong in here. One thing does, and doesn't — see
:mod:`rssignal.pending`, which keeps its own file beside this one.

Since fetchit took over reading the feeds, what is left is small: the picture
rssignal last put on each group (:mod:`rssignal.artwork`), so a group is only
told "changed the group picture" when it really did.

Entries are namespaced and carry the time they were stored. The whole file is
read once at the start of a run and written once at the end.

Corruption is not an error condition. An unreadable, truncated or half-written
cache is treated as an empty one and simply refilled.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any

# Where the cache lives when nothing says otherwise. Under the user's cache
# directory rather than beside feeds.json, because that is the directory whose
# whole contract is "safe to delete, will be rebuilt".
CACHE_ENV = "RSSIGNAL_CACHE"

_lock = threading.Lock()
_data: dict[str, dict[str, Any]] | None = None
_dirty = False


def path() -> str:
    """Where the cache file is, honouring :data:`CACHE_ENV` then XDG."""
    override = os.environ.get(CACHE_ENV, "").strip()
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("XDG_CACHE_HOME", "").strip() or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(base, "rssignal", "cache.json")


def _loaded() -> dict[str, dict[str, Any]]:
    """The cache contents, reading the file the first time. Call under ``_lock``."""
    global _data
    if _data is None:
        try:
            with open(path(), encoding="utf-8") as fh:
                raw = json.load(fh)
            _data = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            # Missing, unreadable, or garbage: all mean "nothing remembered".
            _data = {}
    return _data


def get(namespace: str, key: str, *, max_age: float | None = None) -> Any | None:
    """Return the remembered value, or ``None`` if there isn't a usable one.

    ``max_age`` is in seconds; ``None`` means an answer of this kind never goes
    stale. An entry stored in the future — a clock that moved — is treated as
    fresh rather than discarded, since the alternative helps nobody.
    """
    with _lock:
        entry = _loaded().get(namespace, {}).get(key)
        if not isinstance(entry, dict) or "value" not in entry:
            return None
        if max_age is not None:
            stored_at = entry.get("stored_at")
            if not isinstance(stored_at, (int, float)):
                return None
            if time.time() - stored_at > max_age:
                return None
        return entry["value"]


def put(namespace: str, key: str, value: Any) -> None:
    """Remember ``value`` under ``namespace``/``key`` for later runs."""
    global _dirty
    with _lock:
        _loaded().setdefault(namespace, {})[key] = {
            "value": value,
            "stored_at": time.time(),
        }
        _dirty = True


def save() -> None:
    """Write the cache out, if anything changed. Failing to is not an error.

    Written to a temporary file in the same directory and renamed over the real
    one, so a run interrupted mid-write leaves the previous cache intact rather
    than a half-written file for the next run to trip over.
    """
    global _dirty
    with _lock:
        if not _dirty or _data is None:
            return
        target = path()
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(target), prefix=".cache-", suffix=".json"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(_data, fh)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except (OSError, TypeError, ValueError):
            # A cache that can't be written is a cache that isn't there, which
            # is a slower next run and nothing worse. Not worth a word.
            return
        _dirty = False


def clear() -> None:
    """Forget everything, in memory. For tests, and for a caller that must."""
    global _data, _dirty
    with _lock:
        _data = {}
        _dirty = True
