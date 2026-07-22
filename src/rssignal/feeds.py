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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser

import feedparser

# The two feed kinds rssignal knows how to turn into messages.
FEED_TYPES = ("regular", "podcast")


class FeedError(Exception):
    """Raised when feed configuration or a feed itself cannot be read."""


@dataclass(frozen=True)
class FeedConfig:
    """Configuration for a single feed, as read from ``feeds.json``."""

    url: str
    type: str
    name: str | None = None
    recipient: str | None = None
    max_age: timedelta | None = None


@dataclass(frozen=True)
class FeedItem:
    """A single normalized entry from a feed."""

    title: str
    description: str
    link: str | None = None
    published: datetime | None = None
    enclosure_url: str | None = None
    enclosure_type: str | None = None


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

    return FeedConfig(
        url=url,
        type=feed_type,
        name=raw.get("name"),
        recipient=raw.get("recipient"),
        max_age=max_age,
    )


def parse_feed(cfg: FeedConfig) -> list[FeedItem]:
    """Fetch and parse ``cfg.url``, returning its entries as :class:`FeedItem`."""
    parsed = feedparser.parse(cfg.url)
    # feedparser doesn't raise on network/parse trouble; it records it instead.
    if getattr(parsed, "bozo", False) and not parsed.entries:
        exc = getattr(parsed, "bozo_exception", "unknown error")
        raise FeedError(f"Could not read feed {cfg.url!r}: {exc}")

    return [_build_feed_item(entry) for entry in parsed.entries]


def _build_feed_item(entry: object) -> FeedItem:
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

    return FeedItem(
        title=title,
        description=description,
        link=link,
        published=published,
        enclosure_url=enclosure_url,
        enclosure_type=enclosure_type,
    )


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


def format_message(item: FeedItem, feed_type: str) -> str:
    """Build the Signal message text for ``item``.

    For ``regular`` feeds the link is appended; for ``podcast`` feeds it is
    omitted, since the audio enclosure is sent as an attachment instead.
    """
    parts = [item.title, item.description]
    if feed_type == "regular":
        parts.append(item.link or "")
    return "\n\n".join(part for part in parts if part)
