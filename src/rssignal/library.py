"""Read the library fetchit fills: what each feed has, as :class:`FeedItem`\\ s.

rssignal does not read feeds any more. `fetchit <https://github.com/bertcoerver/fetchit>`_
downloads them into a library on disk — one folder per feed and kind of item,
a ``.json`` record beside every download — and this module reads that, and
nothing else, so rssignal makes no network request of its own outside Signal::

    <library>/
        shows/<Feed>/feed.json              video
        shows/<Feed>/Season 2026/<…>.mp4    + <…>.json, <…>-thumb.jpg
        podcasts/<Feed>/feed.json           audio
        podcasts/<Feed>/<…>.mp3             + <…>.json, <…>.jpg
        articles/<Feed>/<…>.json            text

A feed in feeds.json reads the folders whose ``feed.json`` carries its
:attr:`~rssignal.feeds.FeedConfig.library_name`, in whichever of the three
kinds it has items. An item exists when its record does: fetchit moves the
record into place last and prunes it first, so a record is never seen before
its media or after it — and one whose media is missing anyway is skipped
rather than sent half.

The library is read-only to rssignal. Nothing here writes to it, which is what
lets the two run on their own schedules, against the same folder, without
coordinating.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .config import load_dotenv
from .feeds import CORE_FIELDS, FeedConfig, FeedError, FeedItem, ParsedFeed, SourceMissing

LIBRARY_ENV = "RSSIGNAL_LIBRARY"
DEFAULT_LIBRARY = "~/fetchit"

# The newest version of fetchit's layout this reads. A record from a newer one
# is refused rather than guessed at.
FORMAT = 1

KIND_DIRS = ("shows", "podcasts", "articles")
FEED_FILE = "feed.json"
RECORD_EXT = ".json"


def library_root() -> str:
    """Where the library is: ``$RSSIGNAL_LIBRARY``, or ``~/fetchit``.

    ``.env`` is loaded first, so the setting can live there with the rest of
    rssignal's configuration.
    """
    try:
        load_dotenv()
    except OSError:
        pass
    root = os.environ.get(LIBRARY_ENV, "").strip() or DEFAULT_LIBRARY
    return os.path.abspath(os.path.expanduser(root))


def read_feed(cfg: FeedConfig, root: str | None = None) -> ParsedFeed:
    """Every item the library holds for ``cfg``, oldest first.

    Raises :class:`~rssignal.feeds.SourceMissing` if the library has no folder
    for it at all, and :class:`~rssignal.feeds.FeedError` if one of its
    records is from a newer fetchit than this rssignal can read.
    """
    root = root or library_root()
    name = cfg.library_name
    folders = feed_folders(root, name)
    if not folders:
        if not os.path.isdir(root):
            raise SourceMissing(
                f"there is no library at {root} — set {LIBRARY_ENV} to the folder "
                "fetchit downloads into."
            )
        raise SourceMissing(
            f"the library at {root} has nothing for {name!r} yet. If fetchit has "
            "run since this feed was added, check that a feed in fetchit.json "
            f"is called {name!r}, or set \"source\" to the name it has there."
        )

    items: list[FeedItem] = []
    image = None
    description = ""
    title = ""
    for folder, meta in folders:
        description = description or str(meta.get("description") or "")
        title = title or str(meta.get("title") or "")
        picture = meta.get("image")
        if image is None and isinstance(picture, str):
            path = os.path.join(folder, os.path.basename(picture))
            image = path if os.path.isfile(path) else None
        for path, record in _records(folder):
            item = _item(record, path, cfg.name)
            if item is not None:
                items.append(item)

    items.sort(key=lambda i: (i.published or datetime.min.replace(tzinfo=timezone.utc)))
    return ParsedFeed(items=items, image=image, description=description, title=title)


def feed_image(cfg: FeedConfig, root: str | None = None) -> str | None:
    """The path of ``cfg``'s artwork in the library, or ``None`` if it has none."""
    for folder, meta in feed_folders(root or library_root(), cfg.library_name):
        picture = meta.get("image")
        if isinstance(picture, str):
            path = os.path.join(folder, os.path.basename(picture))
            if os.path.isfile(path):
                return path
    return None


def feed_folders(root: str, name: str) -> list[tuple[str, dict]]:
    """``(folder, feed.json)`` for every folder of the feed called ``name``.

    Matched on the name ``feed.json`` records, ignoring case, rather than on the
    folder's own name: fetchit makes the name safe for a filesystem, and how it
    does that is its business.
    """
    wanted = name.strip().casefold()
    found = []
    for kind_dir in KIND_DIRS:
        base = os.path.join(root, kind_dir)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for entry in names:
            folder = os.path.join(base, entry)
            meta = _read_json(os.path.join(folder, FEED_FILE))
            if meta is None:
                continue
            if str(meta.get("name") or entry).strip().casefold() == wanted:
                found.append((folder, meta))
    return found


def _records(folder: str):
    """``(path, record)`` for every item record below ``folder``."""
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(RECORD_EXT) or name == FEED_FILE or name.startswith("."):
                continue
            path = os.path.join(dirpath, name)
            record = _read_json(path)
            if record is not None and "id" in record:
                yield path, record


def _item(record: dict, path: str, feed_name: str) -> FeedItem | None:
    """The :class:`FeedItem` a record describes, or ``None`` if it can't be sent yet."""
    version = record.get("format", 1)
    if not isinstance(version, int) or version > FORMAT:
        raise FeedError(
            f"{path} is in fetchit's layout version {version}, and this rssignal "
            f"reads up to version {FORMAT}. Update rssignal."
        )

    folder = os.path.dirname(path)
    media = _beside(folder, record.get("media"))
    if record.get("media") and media is None:
        return None  # being pruned, or not all there
    image = _beside(folder, record.get("image"))

    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    extra = {
        str(k): str(v)
        for k, v in fields.items()
        if k not in CORE_FIELDS and isinstance(v, (str, int, float))
    }
    for key in ("season", "episode", "number"):
        if isinstance(record.get(key), int):
            extra.setdefault(key, str(record[key]))
    if isinstance(record.get("duration"), int):
        extra.setdefault("duration_seconds", str(record["duration"]))
    # What the item is called in the library, should a template want it.
    extra.setdefault("library_id", str(record["id"]))

    categories = record.get("categories")
    return FeedItem(
        title=str(record.get("title") or ""),
        description=str(record.get("description") or ""),
        link=record.get("link") or fields.get("link") or None,
        published=_when(record.get("published")),
        enclosure_url=fields.get("enclosure_url") or None,
        enclosure_type=fields.get("enclosure_type") or None,
        image_url=fields.get("image_url") or None,
        author=record.get("author") or fields.get("author") or None,
        categories=tuple(str(c) for c in categories) if isinstance(categories, list) else (),
        feed_name=feed_name,
        extra=extra,
        kind=str(record.get("kind") or "text"),
        media=media,
        image=image,
    )


def _beside(folder: str, name: object) -> str | None:
    """The file ``name`` next to a record, if it is a name and it is there."""
    if not isinstance(name, str) or not name:
        return None
    path = os.path.join(folder, os.path.basename(name))
    return path if os.path.isfile(path) else None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
