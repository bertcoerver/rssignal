"""Keep a copy of what was sent, for a while, where a media browser can see it.

Everything rssignal downloads is temporary by design: :func:`rssignal.download.download_temp`
unlinks its file in a ``finally``, and :func:`rssignal.video.video_temp` hands out
a directory that takes the file with it. That is the right default — a bridge to a
chat app has no business hoarding gigabytes — but it means an episode exists on
disk for exactly as long as it takes to upload it, and Signal's own copy is the
only one that survives.

When rssignal runs on a box that also runs Home Assistant, there is somewhere
better for it to go: the folder the Media browser shows. This module puts a
second name on the file just before its temporary one goes away, under a layout
that reads well in a media browser, and deletes it again after a fortnight.

Two properties are worth stating plainly, because both are deliberate:

**It is a link, not a copy.** :func:`keep` tries :func:`os.link` first. A hard
link costs no bytes and no second write — the file already on disk simply gains
a name, and when the temporary name is unlinked the archived one is what remains.
That matters on a Raspberry Pi writing to an SD card, where copying every 95 MB
video a second time is a measurable share of the device's life. It only works
within one filesystem, so the caller is expected to point ``TMPDIR`` inside the
archive directory; :func:`staging_dir` is that place, and it is dot-prefixed so a
media browser does not show half-downloaded files. When the link is impossible
anyway the copy is taken, because a slow archive beats none.

**It is off unless asked for.** No ``RSSIGNAL_MEDIA_DIR``, no archive, no
behaviour change anywhere. rssignal's contract is still that it keeps no state
worth protecting: this directory is as disposable as the cache, and deleting it
costs nothing but the files.

Expiry is counted from when a file was *archived*, not when the item was
published. An episodic feed (:mod:`rssignal.episodic`) releases a back catalogue
years after it went up, and a fortnight that had already expired before the
episode arrived would be a strange kind of archive.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import unicodedata
from datetime import datetime

from .errorlog import log_line
from .feeds import FeedItem

# Unset means the whole module does nothing. Read from the environment rather
# than feeds.json because it is a property of the machine rssignal runs on, not
# of any feed — the same config file should work on a laptop that has no media
# folder to put anything in.
MEDIA_DIR_ENV = "RSSIGNAL_MEDIA_DIR"
MEDIA_KEEP_DAYS_ENV = "RSSIGNAL_MEDIA_KEEP_DAYS"

DEFAULT_KEEP_DAYS = 14

# Downloads land here so that the hard link in `keep` stays within one
# filesystem. The leading dot keeps Home Assistant's media browser from
# listing partial files as though they were episodes.
STAGING_NAME = ".staging"

# Long enough to tell two episodes of the same show apart, short enough to
# survive the filesystems and browsers that still have opinions about path
# length. Titles run to hundreds of characters surprisingly often.
SLUG_MAX = 80


def media_dir() -> str | None:
    """The archive directory, or ``None`` when archiving is switched off.

    ``None`` is the normal answer everywhere except a host that was set up for
    this, and every other function here treats it as "do nothing".
    """
    return os.environ.get(MEDIA_DIR_ENV, "").strip() or None


def keep_days() -> int:
    """How long an archived file lives, in days. ``0`` disables expiry.

    A bad value is not worth failing a run over — an archive that keeps too much
    is a disk-space problem, an archive that deletes on a misparsed number is a
    data-loss one — so anything unreadable falls back to the default.
    """
    raw = os.environ.get(MEDIA_KEEP_DAYS_ENV, "").strip()
    if not raw:
        return DEFAULT_KEEP_DAYS
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_KEEP_DAYS
    return days if days >= 0 else DEFAULT_KEEP_DAYS


def staging_dir() -> str | None:
    """Where in-progress downloads should go, or ``None`` if archiving is off.

    The caller sets ``TMPDIR`` to this before downloading anything, which is the
    whole reason :func:`keep` can get away with a hard link. Creating it is the
    caller's business too — see :func:`prepare`.
    """
    root = media_dir()
    return os.path.join(root, STAGING_NAME) if root else None


def prepare() -> str | None:
    """Create the archive and staging directories, and point ``TMPDIR`` at staging.

    Called once at the start of a run. Returns the staging directory, or ``None``
    when archiving is off or the directories could not be made — in which case
    downloads go to the usual temporary directory and :func:`keep` will fall back
    to copying, which is exactly the degradation that should happen.
    """
    staging = staging_dir()
    if staging is None:
        return None
    try:
        os.makedirs(staging, exist_ok=True)
    except OSError as exc:
        log_line(f"archive: cannot create {staging}: {exc}")
        return None

    # Both, and the second is the one that actually matters. `tempfile` reads
    # TMPDIR once, the first time anything asks it for a directory, and caches
    # the answer for the life of the process — so setting only the environment
    # variable would be quietly ignored if anything had already used a temp
    # file, and every download would land on the wrong filesystem. Nothing
    # would break: `keep` would fall back to copying, and the only evidence
    # would be an SD card wearing out twice as fast. Set the module's own
    # variable too, which is the documented override and takes effect whatever
    # has happened already.
    os.environ["TMPDIR"] = staging
    tempfile.tempdir = staging

    return staging


def _slug(text: str, *, limit: int = SLUG_MAX) -> str:
    """A filename-safe version of ``text``: lowercase, ASCII, hyphen-separated.

    Aggressive on purpose. These names are read by humans in a media browser and
    typed by nobody, so losing an em dash or an accent costs nothing, while a
    slash or a colon reaching the filesystem costs a broken run.
    """
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    text = text[:limit].strip("-")
    # Every character was dropped — a title that was entirely emoji, or entirely
    # CJK. Better a dull name than an empty one, which would collide with the
    # next such item and produce a file called ".mp3".
    return text or "item"


def _destination(root: str, feed: str, item: FeedItem, ext: str) -> str:
    """Work out the archived path for ``item``, given the extension ``ext``.

    ``<root>/<feed>/<date>-<title><ext>`` — the feed as a directory because that
    is the grouping anyone browsing will want, the date in front because it is
    what makes a directory listing sort into a sensible order.

    ``ext`` is passed in rather than derived here because the sources do not
    agree on an extension until the download is done — which is why
    :func:`rssignal.video.video_temp` hands out a directory instead of a path in
    the first place, and why a dry run can only say ``.*``.
    """
    when = item.published or datetime.now().astimezone()
    name = f"{when.strftime('%Y-%m-%d')}-{_slug(item.title)}{ext}"
    return os.path.join(root, _slug(feed), name)


def _unique(dest: str) -> str:
    """``dest``, or the first ``name-2.ext``-style variant that is free.

    Two episodes of one show published on one day with the same title are rare
    but real — a re-upload, or a feed that titles everything after the show.
    Overwriting the first would quietly lose it.
    """
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(dest)
    for n in range(2, 100):
        candidate = f"{stem}-{n}{ext}"
        if not os.path.exists(candidate):
            return candidate
    return dest


def keep(path: str, *, feed: str, item: FeedItem) -> str | None:
    """Give ``path`` a second, lasting name under the archive directory.

    Must be called while the temporary file still exists — that is, inside
    whatever ``with`` block owns it — because the point is to link the file, not
    to resurrect it afterwards.

    Returns the archived path, or ``None`` if archiving is off or did not work.
    It never raises: an archive is a convenience, and a failure to write one must
    not be the reason an episode that was already sent gets reported as unsent.
    """
    root = media_dir()
    if root is None:
        return None

    try:
        _, ext = os.path.splitext(path)
        dest = _unique(_destination(root, feed, item, ext))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            os.link(path, dest)
        except OSError:
            # Different filesystem, or one without hard links. Costs a full
            # second write of the file, which is why staging lives where it does.
            shutil.copy2(path, dest)
        return dest
    except OSError as exc:
        log_line(f"archive: could not keep {os.path.basename(path)}: {exc}")
        return None


def describe(item: FeedItem, *, feed: str) -> str | None:
    """Where ``item`` would be archived, for a dry run to report.

    The extension is left off and shown as ``.*``: which one a real run settles
    on is not known without doing the download, and a dry run that guessed would
    be making something up. ``None`` when archiving is off.
    """
    root = media_dir()
    if root is None:
        return None
    return _destination(root, feed, item, ".*")


def prune(*, now: float | None = None) -> int:
    """Delete archived files older than :func:`keep_days`. Returns how many.

    Walks the whole archive, including the staging directory — a run killed
    mid-download leaves a part-file there that nothing will ever collect, and
    this is the only thing that comes looking. Directories left empty are removed
    so the media browser does not fill up with the names of shows that have
    nothing in them.

    Best-effort throughout: a file that cannot be deleted is skipped and the walk
    continues. Returns 0 when archiving is off.
    """
    root = media_dir()
    days = keep_days()
    if root is None or days <= 0 or not os.path.isdir(root):
        return 0

    cutoff = (time.time() if now is None else now) - days * 86400
    removed = 0

    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(full) < cutoff:
                    os.unlink(full)
                    removed += 1
            except OSError:
                continue
        # topdown=False means the children have already been visited, so a
        # directory emptied by this pass is seen as empty now rather than next run.
        if dirpath != root:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    return removed
