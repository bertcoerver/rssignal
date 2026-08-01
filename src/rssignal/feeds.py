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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import feedparser

# Extensions that mark an enclosure as audio when its declared type doesn't.
AUDIO_SUFFIXES = (
    ".mp3",
    ".m4a",
    ".m4b",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".flac",
)

# Suffixes that turn a feed config key into a filter, e.g. "title_contains".
FILTER_OPS = ("contains", "excludes", "matches")

# Config keys that are settings rather than filters.
KNOWN_KEYS = (
    "url",
    "name",
    "message_template",
    "extract",
    "link_preview",
    "preview_url",
    "preview_title",
    "preview_description",
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
    "image_url",
    "author",
    "categories",
    "feed_name",
)

# Defaults for the link preview card, as {field} templates.
DEFAULT_PREVIEW_URL = "{link}"
DEFAULT_PREVIEW_TITLE = "{title}"

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
        "image",
        "media_thumbnail",
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
class FieldExtract:
    """A new field cut out of an existing one, e.g. an id buried in a URL.

    ``pattern`` is matched against the ``source`` field; the new field takes the
    first capture group, or the whole match when the regex has no groups. Feeds
    routinely bury the only per-item identifier inside a media URL, and this is
    what makes it addressable as ``{name}`` in a template or a filter.
    """

    name: str
    source: str
    pattern: str


@dataclass(frozen=True)
class FeedConfig:
    """Configuration for a single feed, as read from ``feeds.json``.

    ``name`` doubles as the name of the Signal group this feed sends to, so
    :func:`load_feeds` requires it. It defaults to empty here only so that the
    pure rendering helpers can be exercised without one.
    """

    url: str
    name: str = ""
    message_template: str | None = None
    filters: tuple[FeedFilter, ...] = ()
    extract: tuple[FieldExtract, ...] = ()
    link_preview: bool | None = None
    preview_url: str | None = None
    preview_title: str | None = None
    preview_description: str | None = None


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
    image_url: str | None = None
    author: str | None = None
    categories: tuple[str, ...] = ()
    feed_name: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedFeed:
    """A parsed feed: its entries plus the channel-level data rssignal uses.

    ``image_url`` is the show's / site's own artwork and ``description`` is what
    the feed says about itself. Items deliberately never inherit either (see
    :func:`_build_feed_item`) — on a preview card the same logo and blurb every
    time say nothing. On the *group* they are exactly right, which is the only
    thing they are used for.
    """

    items: list[FeedItem]
    image_url: str | None = None
    description: str = ""


def is_audio_item(item: FeedItem) -> bool:
    """Whether ``item`` carries audio to send — rssignal's idea of an episode.

    This is asked per item rather than per feed, and it is what a podcast *is*
    as far as rssignal is concerned: there is no ``type`` setting to declare.
    An item that says yes goes out as a voice note with a preview card; one that
    says no goes out as text with its link. A show that posts the occasional
    written note therefore gets that note as a readable message rather than a
    linkless stub.

    An enclosure alone is not enough — plenty of ordinary feeds attach an image
    or a PDF. The declared type decides when there is one, and the URL only gets
    a say when the feed didn't declare anything useful, which is common enough:
    ``application/octet-stream`` on an mp3 is a real thing feeds do.
    """
    if not item.enclosure_url:
        return False

    mime = (item.enclosure_type or "").strip().lower()
    if mime.startswith("audio/"):
        return True
    if mime and not mime.startswith("application/"):
        # image/, video/, text/ — declared, and declared as something else.
        # Video included: it isn't a voice note, so the item keeps its link.
        return False

    # Split the URL first: podcast CDNs bolt tracking parameters onto every
    # enclosure, and "…/ep.mp3?source=rss" doesn't end in ".mp3".
    return urlsplit(item.enclosure_url).path.lower().endswith(AUDIO_SUFFIXES)


def load_feeds(path: str = "feeds.json") -> list[FeedConfig]:
    """Read ``path`` and return the list of :class:`FeedConfig` it describes.

    Expects ``{"feeds": [ {...}, ... ]}``. Each feed needs a ``url`` and a
    ``name`` — the name is the Signal group the feed sends to. There is no
    setting for what kind of feed it is: :func:`is_audio_item` works that out
    per item. There is no recency setting either: how far a feed got is
    remembered in its group's description (see :mod:`rssignal.watermark`).
    Raises :class:`FeedError` on a missing file, invalid JSON, or a malformed
    entry.
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

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise FeedError(
            f"{where} is missing a non-empty \"name\". The name is the Signal "
            "group this feed sends to; rssignal creates it if it doesn't exist."
        )

    template = raw.get("message_template")
    if template is not None and not isinstance(template, str):
        raise FeedError(f"{where} message_template must be a string.")

    link_preview = raw.get("link_preview")
    if link_preview is not None and not isinstance(link_preview, bool):
        raise FeedError(f"{where} link_preview must be true or false.")

    previews = {}
    for key in ("preview_url", "preview_title", "preview_description"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise FeedError(f"{where} {key} must be a string.")
        previews[key] = value

    return FeedConfig(
        url=url,
        name=name.strip(),
        message_template=template,
        filters=_build_filters(raw, where),
        extract=_build_extracts(raw.get("extract"), where),
        link_preview=link_preview,
        **previews,
    )


def _build_extracts(raw: object, where: str) -> tuple[FieldExtract, ...]:
    """Validate the ``extract`` block into :class:`FieldExtract` rules.

    Patterns are compiled here rather than per item: a typo in a regex should
    fail the run up front, not halfway through sending a feed.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise FeedError(
            f"{where} extract must be an object mapping a field name to "
            "{\"from\": ..., \"pattern\": ...}."
        )

    rules: list[FieldExtract] = []
    for name, spec in raw.items():
        at = f"{where} extract[{name!r}]"
        if not isinstance(spec, dict):
            raise FeedError(f"{at} must be an object with \"from\" and \"pattern\".")

        source = spec.get("from")
        pattern = spec.get("pattern")
        for key, value in (("from", source), ("pattern", pattern)):
            if not isinstance(value, str) or not value:
                raise FeedError(f"{at} is missing a non-empty string {key!r}.")

        unknown = set(spec) - {"from", "pattern"}
        if unknown:
            raise FeedError(f"{at} has unknown key(s) {sorted(unknown)}.")

        try:
            re.compile(pattern)
        except re.error as exc:
            raise FeedError(f"{at} has an invalid regex {pattern!r}: {exc}") from exc

        rules.append(FieldExtract(name=name, source=source, pattern=pattern))
    return tuple(rules)


def _build_filters(raw: dict, where: str) -> tuple[FeedFilter, ...]:
    """Collect the ``<field>_<op>`` keys of ``raw`` into :class:`FeedFilter`s.

    Any key that is neither a known setting nor a filter suffix is an error —
    silently ignoring a typo like ``title_contain`` would quietly send the wrong
    items.
    """
    filters: list[FeedFilter] = []
    for key, value in raw.items():
        if key in KNOWN_KEYS:
            continue
        if key in ("max_age_hours", "max_age_days"):
            raise FeedError(
                f"{where} still has a {key!r}. rssignal no longer works from a "
                "fixed window: it remembers how far each feed got in that feed's "
                "Signal group description, and sends what is newer. Drop the "
                "key. To replay from a particular moment, use "
                "`rssignal run --since`."
            )
        if key == "type":
            raise FeedError(
                f"{where} still has a \"type\". rssignal now works this out from "
                "the feed itself: an item with an audio enclosure is sent as a "
                "voice note, anything else as text plus its link. Drop the key. "
                "To turn a preview card on or off by hand, use \"link_preview\"."
            )
        if key == "recipient":
            raise FeedError(
                f"{where} still has a \"recipient\". Feeds now send to a Signal "
                "group named after the feed's \"name\", created on the first "
                "send if it doesn't exist yet, so drop the key. To send "
                "somewhere else for a test, use `rssignal run --to`."
            )

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


def parse_feed(cfg: FeedConfig) -> ParsedFeed:
    """Fetch and parse ``cfg.url`` into a :class:`ParsedFeed`.

    Usually that means RSS or Atom, and a video source's own page counts as
    either a substitute for one or a signpost to one: an ARTE collection page is
    read directly, since ARTE publishes no feed at all, and a YouTube channel
    page is swapped for the feed YouTube publishes at an address nobody would
    guess. Either way, following a show needs nothing in the config but the
    address of its page.
    """
    # Imported here, not at the top: rssignal.video builds FeedItems and so
    # imports this module. Deferring it keeps that one-way and leaves feeds.py
    # free of any knowledge of what video sources exist.
    from .video import channel_feed_url, collection_feed

    collection = collection_feed(cfg)
    if collection is not None:
        return collection

    feed_url = channel_feed_url(cfg.url)
    if feed_url:
        cfg = replace(cfg, url=feed_url)

    parsed = feedparser.parse(cfg.url)
    # feedparser doesn't raise on network/parse trouble; it records it instead.
    if getattr(parsed, "bozo", False) and not parsed.entries:
        exc = getattr(parsed, "bozo_exception", "unknown error")
        raise FeedError(f"Could not read feed {cfg.url!r}: {exc}")

    channel = getattr(parsed, "feed", None) or {}
    cget = channel.get if hasattr(channel, "get") else lambda k, d=None: getattr(channel, k, d)

    # "summary" is the channel's <description>; "subtitle" is the shorter
    # <itunes:subtitle>, which some feeds fill in instead.
    description = strip_html(cget("summary") or cget("subtitle") or "")

    return ParsedFeed(
        items=[
            apply_extracts(_build_feed_item(entry, cfg.name), cfg.extract)
            for entry in parsed.entries
        ],
        image_url=_href(cget("image")),
        description=description,
    )


def _href(value: object) -> str | None:
    """Pull a URL out of a feedparser image/thumbnail mapping, if there is one."""
    if not value:
        return None
    get = value.get if hasattr(value, "get") else lambda k, d=None: getattr(value, k, d)
    url = get("href") or get("url")
    return str(url) if url else None


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

    # Episode artwork only: <itunes:image> lands in "image", media RSS in
    # "media_thumbnail". The channel's artwork is deliberately not a fallback —
    # the same show logo on every item makes a preview card less informative,
    # not more.
    thumbnails = get("media_thumbnail") or []
    image_url = _href(get("image")) or _href(thumbnails[0] if thumbnails else None)

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
        image_url=image_url,
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


def filter_since(items: list[FeedItem], since: datetime | None) -> list[FeedItem]:
    """Return the items published strictly after ``since``, oldest first.

    The comparison is strict so an item that *is* the watermark isn't sent
    again — the watermark is the publication date of the last item that went
    out, not the moment it went out.

    Items without a publication date are dropped: there is no way to tell
    whether they are new, and guessing wrong means either resending forever or
    never sending at all. With ``since`` unset every dated item passes through.
    """
    dated = [item for item in items if item.published is not None]
    if since is not None:
        dated = [item for item in dated if item.published > since]
    return sorted(dated, key=lambda item: item.published)


def newest(items: list[FeedItem]) -> FeedItem | None:
    """Return the most recently published item, or ``None`` if there is none.

    This is what a feed sends on its very first run, before there is a watermark
    to compare against: one item, so a new group doesn't open with the feed's
    entire back catalogue.
    """
    dated = filter_since(items, None)
    return dated[-1] if dated else None


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
        "image_url": item.image_url or "",
        "author": item.author or "",
        "categories": ", ".join(item.categories),
        "feed_name": item.feed_name,
    }
    for key, value in item.extra.items():
        fields.setdefault(key, value)
    return fields


def apply_extracts(item: FeedItem, rules: tuple[FieldExtract, ...]) -> FeedItem:
    """Return ``item`` with the fields ``rules`` cut out of it added to ``extra``.

    Landing them in ``extra`` is what makes them ordinary fields: templates,
    filters, and ``rssignal fields`` all read :func:`item_fields`, which merges
    extras behind the named fields. A rule can therefore never shadow ``title``
    or ``link``, and it reads the item as it came off the feed — extracts do not
    chain into one another.

    A pattern that doesn't match yields an empty field rather than an error, in
    keeping with how templates treat a missing placeholder.
    """
    if not rules:
        return item

    fields = item_fields(item)
    extra = dict(item.extra)
    for rule in rules:
        match = re.search(rule.pattern, fields.get(rule.source, ""))
        if match is None:
            extra[rule.name] = ""
        else:
            extra[rule.name] = match.group(1) if match.re.groups else match.group(0)
    return replace(item, extra=extra)


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


def _render_template(template: str, item: FeedItem, cfg: FeedConfig, what: str) -> str:
    """Fill ``{field}`` placeholders in ``template`` from ``item``.

    ``what`` names the config key, so a malformed template says which one.
    """
    try:
        return template.format_map(_DefaultingFields(item_fields(item)))
    except (ValueError, IndexError) as exc:
        label = cfg.name or cfg.url
        raise FeedError(f"{what} for feed {label!r} is malformed: {exc}") from exc


def preview_fields(item: FeedItem, cfg: FeedConfig) -> dict[str, str] | None:
    """Return the link-preview card for ``item``, or ``None`` for no preview.

    The card is rendered from the same fields as message templates, so anything
    ``rssignal fields`` lists can go in it. ``image_url`` is the *remote* URL —
    signal-cli wants a local file, so :mod:`rssignal.run` downloads it.

    The card carries no description unless ``preview_description`` asks for one:
    the body of the message is right underneath it, so repeating the item's text
    on the card only crowds it.

    Returns ``None`` when previews are off for this item, or when the url or
    title render empty: signal-cli requires both, and a card with no title is
    not worth sending.

    Whether they are on is decided per item, not per feed: an episode gets a
    card by default, because that is what makes it recognizable next to its
    voice note, while an item that already shows its link in the body does not.
    ``link_preview`` overrides both ways.
    """
    enabled = cfg.link_preview if cfg.link_preview is not None else is_audio_item(item)
    if not enabled:
        return None

    url = _render_template(
        cfg.preview_url or DEFAULT_PREVIEW_URL, item, cfg, "preview_url"
    ).strip()
    title = _render_template(
        cfg.preview_title or DEFAULT_PREVIEW_TITLE, item, cfg, "preview_title"
    ).strip()
    if not url or not title:
        return None

    description = ""
    if cfg.preview_description is not None:
        description = _render_template(
            cfg.preview_description, item, cfg, "preview_description"
        ).strip()

    return {
        "url": url,
        "title": title,
        "description": description,
        "image_url": item.image_url or "",
    }


def render_message(item: FeedItem, cfg: FeedConfig) -> str:
    """Build the Signal message text for ``item`` under ``cfg``.

    Without a ``message_template`` this is the built-in layout from
    :func:`format_message`. With one, ``{field}`` placeholders are filled from
    :func:`item_fields`.

    When the feed sends a link preview, the preview url is appended unless the
    text already contains it — signal-cli requires the url to appear in the body,
    and the built-in podcast layout has no link at all.

    A card already shows the title, so the built-in layout drops it in that case
    rather than printing it twice in a row. A ``message_template`` is left alone:
    if you wrote the layout, you decide what is in it.
    """
    preview = preview_fields(item, cfg)

    if cfg.message_template is None:
        text = format_message(item, include_title=preview is None)
    else:
        text = _render_template(
            cfg.message_template, item, cfg, "message_template"
        )
        # Placeholders that resolved to nothing would otherwise leave blank gaps.
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if preview and preview["url"] not in text:
        text = f"{text}\n\n{preview['url']}".strip()
    return text


def format_message(item: FeedItem, *, include_title: bool = True) -> str:
    """Build the default Signal message text for ``item``.

    The link is appended unless the item carries audio (see
    :func:`is_audio_item`), in which case the enclosure is sent as an attachment
    and the link would only duplicate what the preview card already links to.
    Clear ``include_title`` when something else in the message already shows it
    — a link preview card, for instance.
    """
    parts = [item.title if include_title else "", item.description]
    if not is_audio_item(item):
        parts.append(item.link or "")
    return "\n\n".join(part for part in parts if part)
