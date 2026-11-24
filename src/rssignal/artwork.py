"""Keep each feed group's picture in step with the feed's artwork in the library.

A group takes its picture from the feed when rssignal creates it (see
:class:`rssignal.run._GroupResolver`), but podcasts rebrand and channels change
their avatar, and a group left showing last year's cover is quietly wrong.
fetchit keeps the library's copy current — a series' ``poster.jpg``, a
podcast's ``cover.jpg`` — and :func:`refresh` puts that on the feed's group.

Asking costs nothing now: the picture is a file on disk, so every run looks,
after it has sent everything. Setting a group's picture is not silent, though —
every member sees "changed the group picture" in the chat — so an image is only
uploaded when it differs from the one rssignal last put there, which is
remembered by content in the cache. Losing the cache costs one redundant upload
per group, nothing worse.

A feed whose source has no fixed artwork — ARTE's picture for *Le Dessous des
Cartes* is a still from the current episode — is best told so in fetchit
(``"refresh_image": false`` there keeps the poster as it was first fetched).
``"refresh_image": false`` here leaves the group's picture alone whatever the
library holds.
"""

from __future__ import annotations

import hashlib
import sys

from . import cache
from .errorlog import log_line
from .feeds import FeedConfig
from .library import feed_image
from .signal_cli import SignalGroup, match_group, update_group

# What rssignal last set as each group's picture, keyed by group id.
_AVATAR_NS = "group-avatar"


def refresh(
    feeds: list[FeedConfig], groups: list[SignalGroup], *, dry_run: bool = False
) -> int:
    """Put each feed's artwork from the library on its group. Returns how many changed.

    Only feeds whose group already exists are looked at: a group is created on
    a feed's first send, with its picture, and creating one here would be the
    stray empty group that rule exists to prevent. A feed with no artwork in
    the library leaves its group's picture alone rather than clearing it, and
    so does a feed with ``refresh_image`` turned off.

    Each feed is its own attempt. One whose picture won't upload is reported and
    skipped, and the rest carry on — a picture is decoration, and nothing about
    it is worth losing the others over.
    """
    changed = 0
    for cfg in feeds:
        label = cfg.name or cfg.library_name
        if not cfg.refresh_image:
            continue
        try:
            group = match_group(groups, cfg.name)
            if group is None:
                continue
            changed += _refresh_one(cfg, group, label, dry_run=dry_run)
        except Exception as exc:
            print(f"[{label}] group image not refreshed: {exc}", file=sys.stderr)
            log_line(f"[{label}] group image not refreshed: {exc}")
    return changed


def _refresh_one(
    cfg: FeedConfig, group: SignalGroup, label: str, *, dry_run: bool
) -> bool:
    """Refresh one group's picture. Whether it changed (or, dry, would have)."""
    path = feed_image(cfg)
    if not path:
        return False

    digest = _digest(path)
    if cache.get(_AVATAR_NS, group.id) == digest:
        return False
    if dry_run:
        print(f"[{label}] group image would be updated from {path}")
        return True
    update_group(group.id, avatar=path)

    cache.put(_AVATAR_NS, group.id, digest)
    print(f"[{label}] group image updated.")
    return True


def _digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()
