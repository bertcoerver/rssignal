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

A video also gets small files beside it (:func:`annotate`): its title, synopsis
and date as XML, and its picture. The file name is a slug, which is enough for a
file listing and not much to look at on a television; a player such as Infuse
reads the pair and shows the episode as the source described it.

An item that says which episode of which season it is goes one step further and
is filed the way a media server expects a series: ``<feed>/Season 02/…-s02e28``,
with an ``.nfo`` of its own and a ``tvshow.nfo`` and artwork for the series above
it (:func:`annotate_show`). Jellyfin and Emby build a series out of exactly that,
with no database to look it up in — which matters, because a daily broadcast is
in none of them.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
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

# What `annotate` puts beside a video: Infuse's own metadata format, the Kodi
# one that Jellyfin and Emby read, and the picture, all under the video's name.
# Only image types a player is sure to read — an episode with no picture looks
# better than one whose picture is a broken tile.
METADATA_EXT = ".xml"
NFO_EXT = ".nfo"
IMAGE_EXTS = (".jpg", ".jpeg", ".png")
SIDECAR_EXTS = (METADATA_EXT, NFO_EXT, *IMAGE_EXTS)

# What `annotate_show` keeps in a series' own folder, above its seasons. The
# names are the ones Kodi settled on and every media server since has read.
SHOW_NFO = "tvshow.nfo"
SHOW_IMAGES = ("poster", "fanart")

# XML 1.0 has no way to write these at all, escaped or not, and a synopsis
# pasted from somewhere odd occasionally carries one. One of them makes the
# whole file unreadable.
_XML_UNSAFE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


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

    An item that knows its place in a series (:func:`numbered`) is filed by that
    instead: ``<root>/<feed>/Season 02/<feed>-s02e28<ext>``. ``Season NN`` and
    ``sNNeNN`` are spelled the way media servers look for them, and nothing
    follows the number on purpose — a title that begins with a figure, slugged
    onto the end, reads to them as a range of episodes.

    ``ext`` is passed in rather than derived here because the sources do not
    agree on an extension until the download is done — which is why
    :func:`rssignal.video.video_temp` hands out a directory instead of a path in
    the first place, and why a dry run can only say ``.*``.
    """
    place = numbered(item)
    if place is not None:
        season, episode = place
        show = _slug(feed)
        name = f"{show}-s{season:02d}e{episode:02d}{ext}"
        return os.path.join(root, show, f"Season {season:02d}", name)

    when = item.published or datetime.now().astimezone()
    name = f"{when.strftime('%Y-%m-%d')}-{_slug(item.title)}{ext}"
    return os.path.join(root, _slug(feed), name)


def numbered(item: FeedItem) -> tuple[int, int] | None:
    """``(season, episode)`` if ``item`` says which episode of a series it is.

    A source that knows puts both in the item's ``extra``, as ``season`` and
    ``episode``; NPO does. Anything else — a podcast, an upload, a film — has no
    such place, and is filed by its date.
    """
    try:
        season, episode = int(item.extra["season"]), int(item.extra["episode"])
    except (KeyError, ValueError):
        return None
    return (season, episode) if season >= 0 and episode >= 0 else None


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
        dest = _destination(root, feed, item, ext)
        if numbered(item) is None:
            dest = _unique(dest)
        elif os.path.exists(dest):
            # The same episode of the same season is the same episode, sent
            # again. A second copy named `…e28-2` would read as two episodes.
            os.unlink(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _link(path, dest)
        return dest
    except OSError as exc:
        log_line(f"archive: could not keep {os.path.basename(path)}: {exc}")
        return None


def _link(path: str, dest: str) -> None:
    """Give ``path`` the second name ``dest``, copying only if it can't be linked."""
    try:
        os.link(path, dest)
    except OSError:
        # Different filesystem, or one without hard links. Costs a full
        # second write of the file, which is why staging lives where it does.
        shutil.copy2(path, dest)


def annotate(dest: str, item: FeedItem, *, image: str = "") -> None:
    """Describe the archived file ``dest`` to a media player, in files beside it.

    ``<name>.xml`` carries the item's title, synopsis, date and genres in the
    format Infuse reads for a video it can't look up anywhere — which is every
    video here: these are broadcasts and uploads, not films with a database
    entry. An episode with a place in a series (:func:`numbered`) gets a
    ``<name>.nfo`` as well, which is where a season and an episode number can be
    said: Infuse's format has no word for either, and Jellyfin and Emby read
    this one.

    ``image`` is a **local file path**, the item's own picture, and gets the same
    name with its own extension; it is linked like the video was, and for the
    same reason must still exist when this is called. Empty, or a type a player
    may not read, and the episode simply has no picture.

    ``dest`` is what :func:`keep` returned. Like :func:`keep` this never raises:
    the video is archived either way, and a missing caption is not worth more
    than a line in the log.
    """
    stem, _ = os.path.splitext(dest)
    aired = item.published.strftime("%Y-%m-%d") if item.published else ""
    try:
        root = ET.Element("media", type="Other")
        _child(root, "title", item.title)
        _child(root, "description", item.description)
        _child(root, "published", aired)
        if item.categories:
            genres = ET.SubElement(root, "genres")
            for genre in item.categories:
                _child(genres, "genre", genre)
        _write(root, stem + METADATA_EXT)

        place = numbered(item)
        if place is not None:
            season, episode = place
            root = ET.Element("episodedetails")
            _child(root, "title", item.title)
            _child(root, "showtitle", item.extra.get("show_title", ""))
            _child(root, "season", str(season))
            _child(root, "episode", str(episode))
            _child(root, "plot", item.description)
            _child(root, "aired", aired)
            _child(root, "runtime", _minutes(item.extra.get("duration_seconds")))
            for genre in item.categories:
                _child(root, "genre", genre)
            _child(root, "studio", item.author or "")
            _locked(root)
            _write(root, stem + NFO_EXT)

        ext = os.path.splitext(image)[1].lower()
        if image and ext in IMAGE_EXTS:
            _link(image, stem + ext)
    except OSError as exc:
        log_line(f"archive: could not describe {os.path.basename(dest)}: {exc}")


def annotate_show(
    dest: str,
    item: FeedItem,
    *,
    description: str = "",
    fetch_image: Callable[[], str] | None = None,
) -> None:
    """Describe the series the archived episode ``dest`` belongs to.

    Does nothing unless ``item`` has a place in one (:func:`numbered`). Then the
    series' folder — two up from the episode, past its season — gets a
    ``tvshow.nfo`` with the show's name and ``description``, and the show's
    picture as both ``poster`` and ``fanart``: the tile a media server lists the
    series by, and the backdrop behind its page.

    The picture is only fetched when one of the two is missing, which is why it
    arrives as ``fetch_image`` — something to call for a **local file path** —
    rather than as the path: a download per episode for a file that is already
    there would be a waste, and one that is there may have been put there by
    hand. A source's artwork is as wide as a television and a poster is meant to
    be tall, so replacing ``poster.jpg`` with a better one is a reasonable thing
    to do and it is left alone if you do.

    Everything here is dated afresh each time, so it lasts as long as the newest
    episode does. Never raises, like the rest.
    """
    if numbered(item) is None:
        return
    show = os.path.dirname(os.path.dirname(dest))
    try:
        root = ET.Element("tvshow")
        _child(root, "title", item.extra.get("show_title") or item.feed_name)
        _child(root, "plot", description)
        for genre in item.categories:
            _child(root, "genre", genre)
        _child(root, "studio", item.author or "")
        _locked(root)
        _write(root, os.path.join(show, SHOW_NFO))

        have = {name: _show_image(show, name) for name in SHOW_IMAGES}
        for path in filter(None, have.values()):
            os.utime(path)
        if fetch_image is not None and not all(have.values()):
            image = fetch_image()
            ext = os.path.splitext(image)[1].lower()
            if image and ext in IMAGE_EXTS:
                for name, path in have.items():
                    if path is None:
                        _link(image, os.path.join(show, name + ext))
    except OSError as exc:
        log_line(f"archive: could not describe the series in {show}: {exc}")


def _show_image(show: str, name: str) -> str | None:
    """The series picture called ``name`` in ``show``, whatever type it is."""
    for ext in IMAGE_EXTS:
        path = os.path.join(show, name + ext)
        if os.path.exists(path):
            return path
    return None


def _child(parent: ET.Element, tag: str, text: str) -> None:
    """Add ``<tag>text</tag>`` to ``parent`` — unless there is nothing to say."""
    text = _XML_UNSAFE_RE.sub("", text).strip()
    if text:
        ET.SubElement(parent, tag).text = text


def _locked(root: ET.Element) -> None:
    """Tell a media server this file is the whole story, and not to go looking.

    Left to itself Jellyfin or Emby searches its online sources for a series of
    this name, and for a broadcast that is in none of them the nearest match is
    somebody else's programme.
    """
    ET.SubElement(root, "lockdata").text = "true"


def _write(root: ET.Element, path: str) -> None:
    ET.indent(root, space="    ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _minutes(seconds: str | None) -> str:
    """A length in seconds as whole minutes, which is what an ``.nfo`` counts in."""
    try:
        return str(max(1, round(int(seconds or "") / 60)))
    except ValueError:
        return ""


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

    What :func:`annotate` wrote goes with the episode it describes, in the same
    pass, and is not counted: the number returned is of episodes. Left to their
    own dates the caption and picture would outlive the video by a run, being a
    little newer than it, and a title with nothing to play is worse than neither.
    A series' own files go the same way, once its last season has.

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
        names = set(filenames)
        described = {
            stem
            for stem, ext in map(os.path.splitext, filenames)
            if not _is_sidecar(ext)
        }
        for name in filenames:
            stem, ext = os.path.splitext(name)
            if _is_sidecar(ext) and stem in described:
                # Goes when its episode does, below. One with no episode beside
                # it is just a file, and ages out like any other.
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(full) >= cutoff:
                    continue
                os.unlink(full)
                removed += 1
            except OSError:
                continue
            for side in SIDECAR_EXTS:
                if stem + side in names:
                    try:
                        os.unlink(os.path.join(dirpath, stem + side))
                    except OSError:
                        pass
        if dirpath != root:
            _drop_empty_show(dirpath)
        # topdown=False means the children have already been visited, so a
        # directory emptied by this pass is seen as empty now rather than next run.
        if dirpath != root:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    return removed


def _is_sidecar(ext: str) -> bool:
    return ext.lower() in SIDECAR_EXTS


def _drop_empty_show(dirpath: str) -> None:
    """Remove what :func:`annotate_show` wrote, if it is all ``dirpath`` holds."""
    show_files = {SHOW_NFO} | {n + e for n in SHOW_IMAGES for e in IMAGE_EXTS}
    try:
        left = os.listdir(dirpath)
        if SHOW_NFO in left and set(left) <= show_files:
            for name in left:
                os.unlink(os.path.join(dirpath, name))
    except OSError:
        pass
