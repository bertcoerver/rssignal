"""Read feed configuration, parse RSS/Atom feeds, and filter items by recency.

This module is pure with respect to Signal: it turns a JSON config plus the
feeds it names into a list of :class:`FeedItem` objects and helpers for
filtering and formatting them. Sending is left to :mod:`rssignal.run`.

Parsing is delegated to `feedparser <https://feedparser.readthedocs.io>`_, which
handles the many RSS/Atom dialects, malformed markup, date formats, and podcast
enclosures that show up in real-world feeds.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser

import feedparser

# The two feed kinds rssignal knows how to turn into messages.
FEED_TYPES = ("regular", "podcast")

# Suffixes that turn a feed config key into a filter, e.g. "title_contains".
FILTER_OPS = ("contains", "excludes", "matches")

# Config keys that are settings rather than filters.
KNOWN_KEYS = (
    "url",
    "type",
    "name",
    "recipient",
    "max_age_hours",
    "max_age_days",
    "message_template",
)

# The field names every item has, in the order item_fields lists them.
CORE_FIELDS = (
    "title",
    "description",
    "link",
    "published",
    "published_date",
    "enclosure_url",
    "enclosure_type",
    "author",
    "categories",
    "feed_name",
)

# Entry keys already mapped onto FeedItem, so they don't repeat in ``extra``.
_MAPPED_ENTRY_KEYS = frozenset(
    {
        "title",
        "summary",
        "description",
        "link",
        "published",
        "published_parsed",
        "enclosures",
        "author",
        "tags",
    }
)


class FeedError(Exception):
    """Raised when feed configuration or a feed itself cannot be read."""


@dataclass(frozen=True)
class FeedFilter:
    """One field test from the config, e.g. ``"title_contains": "Ukraine"``.

    ``values`` holds the alternatives for the test: ``contains`` and ``matches``
    keep an item when *any* value hits, ``excludes`` keeps it when *none* do.
    """

    field: str
    op: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class FeedConfig:
    """Configuration for a single feed, as read from ``feeds.json``."""

    url: str
    type: str
    name: str | None = None
    recipient: str | None = None
    max_age: timedelta | None = None
    message_template: str | None = None
    filters: tuple[FeedFilter, ...] = ()


@dataclass(frozen=True)
class FeedItem:
    """A single normalized entry from a feed.

    The named fields are the ones every feed kind is expected to have; anything
    else the feed happens to publish (``itunes_duration``, ``id``, …) lands in
    ``extra``. Use :func:`item_fields` to get the flat, stringified view that
    templates and filters see.
    """

    title: str
    description: str
    link: str | None = None
    published: datetime | None = None
    enclosure_url: str | None = None
    enclosure_type: str | None = None
    author: str | None = None
    categories: tuple[str, ...] = ()
    feed_name: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def load_feeds(path: str = "feeds.json") -> list[FeedConfig]:
    """Read ``path`` and return the list of :class:`FeedConfig` it describes.

    Expects ``{"feeds": [ {...}, ... ]}``. Each feed needs a ``url`` and a
    ``type`` (one of :data:`FEED_TYPES`); ``name`` and ``recipient`` are
    optional, and recency comes from optional ``max_age_hours`` /
    ``max_age_days`` keys (summed). Raises :class:`FeedError` on a missing file,
    invalid JSON, or a malformed / unknown-type entry.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise FeedError(
            f"Feed config not found at {path!r}. Copy feeds.example.json to "
            "feeds.json and edit it, or pass --config."
        ) from exc
    except json.JSONDecodeError as exc:
        raise FeedError(f"Feed config {path!r} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("feeds"), list):
        raise FeedError(
            f"Feed config {path!r} must be an object with a \"feeds\" array."
        )

    feeds: list[FeedConfig] = []
    for index, raw in enumerate(data["feeds"]):
        feeds.append(_build_feed_config(raw, index))
    return feeds


def _build_feed_config(raw: object, index: int) -> FeedConfig:
    """Validate one raw feed entry and turn it into a :class:`FeedConfig`."""
    where = f"feeds[{index}]"
    if not isinstance(raw, dict):
        raise FeedError(f"{where} must be an object.")

    url = raw.get("url")
    if not isinstance(url, str) or not url:
        raise FeedError(f"{where} is missing a non-empty \"url\".")

    feed_type = raw.get("type")
    if feed_type not in FEED_TYPES:
        raise FeedError(
            f"{where} has type {feed_type!r}; expected one of {FEED_TYPES}."
        )

    hours = raw.get("max_age_hours", 0) or 0
    days = raw.get("max_age_days", 0) or 0
    if not isinstance(hours, (int, float)) or not isinstance(days, (int, float)):
        raise FeedError(f"{where} max_age_hours/max_age_days must be numbers.")
    max_age = timedelta(hours=hours, days=days) or None

    template = raw.get("message_template")
    if template is not None and not isinstance(template, str):
        raise FeedError(f"{where} message_template must be a string.")

    return FeedConfig(
        url=url,
        type=feed_type,
        name=raw.get("name"),
        recipient=raw.get("recipient"),
        max_age=max_age,
        message_template=template,
        filters=_build_filters(raw, where),
    )


def _build_filters(raw: dict, where: str) -> tuple[FeedFilter, ...]:
    """Collect the ``<field>_<op>`` keys of ``raw`` into :class:`FeedFilter`s.

    Any key that is neither a known setting nor a filter suffix is an error —
    silently ignoring a typo like ``max_age_hour`` would quietly send the wrong
    items.
    """
    filters: list[FeedFilter] = []
    for key, value in raw.items():
        if key in KNOWN_KEYS:
            continue

        field_name, _, op = key.rpartition("_")
        if not field_name or op not in FILTER_OPS:
            raise FeedError(
                f"{where} has unknown key {key!r}. Expected one of {KNOWN_KEYS}, "
                f"or a filter named <field>_<op> with op in {FILTER_OPS}."
            )

        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not all(
            isinstance(v, str) for v in values
        ):
            raise FeedError(f"{where} {key!r} must be a string or a list of strings.")
        if op == "matches":
            for pattern in values:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise FeedError(
                        f"{where} {key!r} has an invalid regex {pattern!r}: {exc}"
                    ) from exc

        filters.append(FeedFilter(field=field_name, op=op, values=tuple(values)))
    return tuple(filters)


def parse_feed(cfg: FeedConfig) -> list[FeedItem]:
    """Fetch and parse ``cfg.url``, returning its entries as :class:`FeedItem`."""
    parsed = feedparser.parse(cfg.url)
    # feedparser doesn't raise on network/parse trouble; it records it instead.
    if getattr(parsed, "bozo", False) and not parsed.entries:
        exc = getattr(parsed, "bozo_exception", "unknown error")
        raise FeedError(f"Could not read feed {cfg.url!r}: {exc}")

    feed_name = cfg.name or ""
    return [_build_feed_item(entry, feed_name) for entry in parsed.entries]


def _build_feed_item(entry: object, feed_name: str = "") -> FeedItem:
    """Map a single feedparser entry to a :class:`FeedItem`."""
    get = entry.get if isinstance(entry, dict) else lambda k, d=None: getattr(entry, k, d)

    title = (get("title") or "").strip()
    description = strip_html(get("summary") or get("description") or "")
    link = get("link") or None

    published = None
    published_parsed = get("published_parsed")
    if published_parsed is not None:
        # published_parsed is a time.struct_time in UTC.
        published = datetime(*published_parsed[:6], tzinfo=timezone.utc)

    enclosure_url = None
    enclosure_type = None
    enclosures = get("enclosures") or []
    if enclosures:
        first = enclosures[0]
        fget = first.get if isinstance(first, dict) else lambda k, d=None: getattr(first, k, d)
        enclosure_url = fget("href") or fget("url") or None
        enclosure_type = fget("type") or None

    # feedparser exposes categories as tags: [{"term": "news", ...}, ...].
    categories = []
    for tag in get("tags") or []:
        tget = tag.get if isinstance(tag, dict) else lambda k, d=None: getattr(tag, k, d)
        term = tget("term")
        if term:
            categories.append(str(term))

    return FeedItem(
        title=title,
        description=description,
        link=link,
        published=published,
        enclosure_url=enclosure_url,
        enclosure_type=enclosure_type,
        author=get("author") or None,
        categories=tuple(categories),
        feed_name=feed_name,
        extra=_extra_fields(entry),
    )


def _extra_fields(entry: object) -> dict[str, str]:
    """Collect the feed's own scalar keys that FeedItem doesn't name.

    Feeds carry all sorts of extra data (``itunes_duration``, ``id``, episode
    numbers). Keeping the scalar ones makes them available to templates and
    filters; the structured ``*_detail`` / ``*_parsed`` mirrors feedparser adds
    are skipped, since they have no useful string form.
    """
    items = entry.items() if hasattr(entry, "items") else vars(entry).items()
    extra: dict[str, str] = {}
    for key, value in items:
        if key in _MAPPED_ENTRY_KEYS or key.endswith(("_detail", "_parsed")):
            continue
        if isinstance(value, str):
            extra[key] = strip_html(value) if "<" in value else value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            extra[key] = str(value)
    return extra


class _TextExtractor(HTMLParser):
    """Collect text content from HTML, dropping tags."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def strip_html(text: str) -> str:
    """Return ``text`` with HTML tags removed and entities unescaped.

    Feed descriptions are frequently HTML; a plain-text rendering reads far
    better as a Signal message. Whitespace is collapsed to single spaces.
    """
    if not text:
        return ""
    extractor = _TextExtractor()
    extractor.feed(text)
    plain = unescape(extractor.text())
    return " ".join(plain.split())


def filter_recent(
    items: list[FeedItem],
    max_age: timedelta | None,
    *,
    now: datetime | None = None,
) -> list[FeedItem]:
    """Return items published within ``max_age`` of ``now``.

    With ``max_age`` unset, all items pass through unchanged. When it is set,
    items without a publication date are dropped (their freshness can't be
    confirmed). ``now`` defaults to the current UTC time and is injectable for
    tests.
    """
    if max_age is None:
        return list(items)

    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - max_age

    recent: list[FeedItem] = []
    for item in items:
        if item.published is None:
            continue
        if item.published >= cutoff:
            recent.append(item)
    return recent


def item_fields(item: FeedItem) -> dict[str, str]:
    """Return ``item`` as a flat mapping of field name to string value.

    This is the single view of an item shared by message templates, filters, and
    the ``rssignal fields`` command, so what that command prints is exactly what
    the other two can use. Named fields come first; the feed's own extras fill
    in behind them without overwriting a named field.
    """
    fields: dict[str, str] = {
        "title": item.title,
        "description": item.description,
        "link": item.link or "",
        "published": item.published.isoformat() if item.published else "",
        "published_date": item.published.strftime("%Y-%m-%d") if item.published else "",
        "enclosure_url": item.enclosure_url or "",
        "enclosure_type": item.enclosure_type or "",
        "author": item.author or "",
        "categories": ", ".join(item.categories),
        "feed_name": item.feed_name,
    }
    for key, value in item.extra.items():
        fields.setdefault(key, value)
    return fields


def apply_filters(
    items: list[FeedItem], filters: tuple[FeedFilter, ...]
) -> list[FeedItem]:
    """Return the items passing every filter (filters are ANDed).

    Matching is case-insensitive throughout. A field an item doesn't have counts
    as empty, so ``contains`` drops it while ``excludes`` keeps it.
    """
    if not filters:
        return list(items)
    return [
        item
        for item in items
        if all(_passes(item_fields(item), f) for f in filters)
    ]


def _passes(fields: dict[str, str], flt: FeedFilter) -> bool:
    """Test one item's ``fields`` against a single filter."""
    text = fields.get(flt.field, "")
    if flt.op == "matches":
        return any(re.search(v, text, re.IGNORECASE) for v in flt.values)

    lowered = text.lower()
    hit = any(v.lower() in lowered for v in flt.values)
    return hit if flt.op == "contains" else not hit


class _DefaultingFields(dict):
    """Field mapping that renders unknown ``{placeholders}`` as empty text.

    A field can be present on one item and missing from the next, so an unknown
    name shouldn't abort a run mid-feed. ``rssignal fields`` is how you check
    which names a feed really offers.
    """

    def __missing__(self, key: str) -> str:
        return ""


def render_message(item: FeedItem, cfg: FeedConfig) -> str:
    """Build the Signal message text for ``item`` under ``cfg``.

    Without a ``message_template`` this is the built-in layout from
    :func:`format_message`. With one, ``{field}`` placeholders are filled from
    :func:`item_fields`.
    """
    if cfg.message_template is None:
        return format_message(item, cfg.type)

    try:
        text = cfg.message_template.format_map(_DefaultingFields(item_fields(item)))
    except (ValueError, IndexError) as exc:
        label = cfg.name or cfg.url
        raise FeedError(
            f"message_template for feed {label!r} is malformed: {exc}"
        ) from exc

    # Placeholders that resolved to nothing would otherwise leave blank gaps.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def format_message(item: FeedItem, feed_type: str) -> str:
    """Build the default Signal message text for ``item``.

    For ``regular`` feeds the link is appended; for ``podcast`` feeds it is
    omitted, since the audio enclosure is sent as an attachment instead.
    """
    parts = [item.title, item.description]
    if feed_type == "regular":
        parts.append(item.link or "")
    return "\n\n".join(part for part in parts if part)
