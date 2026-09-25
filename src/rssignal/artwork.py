"""Keep each feed group's picture in step with its source's artwork.

A group takes its picture from the feed when rssignal creates it (see
:class:`rssignal.run._GroupResolver`), and after that nothing looks again — but
podcasts rebrand, channels change their avatar, and a group left showing last
year's cover is quietly wrong. :func:`refresh` walks the feeds, fetches each
source's artwork as it is now, and puts it on the feed's group.

It is not worth doing every run. The artwork changes a few times a year at most,
and asking costs a feed read and an image download per feed — a yt-dlp launch,
for YouTube. So a run does it by chance (:func:`refresh_due`): one run in
:data:`DEFAULT_REFRESH_ONE_IN`, which at fifteen runs a day is about once a
month. A dice roll rather than a date because it needs no state kept anywhere,
and because a run that misses its turn — asleep, killed, offline — simply leaves
it to one of the next few hundred.

Setting a group's picture is not silent: every member sees "changed the group
picture" in the chat. So an image is only uploaded when it differs from the one
rssignal last put there, which is remembered by content in the cache. Losing
the cache costs one redundant upload per group, nothing worse.
"""

from __future__ import annotations

import hashlib
import os
import random
import sys
from collections.abc import Callable

from . import cache
from .download import download_temp
from .errorlog import log_line
from .feeds import FeedConfig, feed_image
from .signal_cli import SignalGroup, match_group, update_group

# About once a month at fifteen runs a day. The Home Assistant add-on works this
# out from its own schedule instead; see addon/rootfs/usr/local/lib/rssignal-env.sh.
DEFAULT_REFRESH_ONE_IN = 450
REFRESH_ONE_IN_ENV = "RSSIGNAL_ARTWORK_REFRESH_ONE_IN"

# What rssignal last set as each group's picture, keyed by group id.
_AVATAR_NS = "group-avatar"


def refresh_one_in() -> int:
    """How many runs, on average, between two refreshes. 0 means never."""
    raw = os.environ.get(REFRESH_ONE_IN_ENV, "").strip()
    if not raw:
        return DEFAULT_REFRESH_ONE_IN
    try:
        return max(0, int(raw))
    except ValueError:
        print(
            f"{REFRESH_ONE_IN_ENV}={raw!r} is not a whole number; using "
            f"{DEFAULT_REFRESH_ONE_IN}.",
            file=sys.stderr,
        )
        return DEFAULT_REFRESH_ONE_IN


def refresh_due(roll: Callable[[], float] = random.random) -> bool:
    """Whether this run is the one in :func:`refresh_one_in` that refreshes."""
    one_in = refresh_one_in()
    return one_in > 0 and roll() * one_in < 1


def refresh(
    feeds: list[FeedConfig], groups: list[SignalGroup], *, dry_run: bool = False
) -> int:
    """Put each feed's current artwork on its group. Returns how many changed.

    Only feeds whose group already exists are looked at: a group is created on
    a feed's first send, with its picture, and creating one here would be the
    stray empty group that rule exists to prevent. A source with no artwork
    leaves its group's picture alone rather than clearing it.

    Each feed is its own attempt. One whose source is down or whose image won't
    download is reported and skipped, and the rest carry on — a picture is
    decoration, and nothing about it is worth losing the others over.
    """
    changed = 0
    for cfg in feeds:
        label = cfg.name or cfg.url
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
    url = feed_image(cfg)
    if not url:
        return False

    with download_temp(url, default_suffix=".jpg") as path:
        digest = _digest(path)
        if cache.get(_AVATAR_NS, group.id) == digest:
            return False
        if dry_run:
            print(f"[{label}] group image would be updated from {url}")
            return True
        update_group(group.id, avatar=path)

    cache.put(_AVATAR_NS, group.id, digest)
    print(f"[{label}] group image updated.")
    return True


def _digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()
