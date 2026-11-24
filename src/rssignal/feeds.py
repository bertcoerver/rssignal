"""Read feed configuration, and filter and format the items a feed has.

This module is pure with respect to Signal: it turns a JSON config into
:class:`FeedConfig` objects, and :class:`FeedItem` objects into message text
and link-preview cards. Where the items come from is :mod:`rssignal.library` —
the folder fetchit fills — and sending is left to :mod:`rssignal.run`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .episodic import Cadence, parse_cadence

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

# The kinds of item fetchit files, and what each goes out as.
AUDIO = "audio"
VIDEO = "video"
TEXT = "text"

# Suffixes that turn a feed config key into a filter, e.g. "title_contains".
FILTER_OPS = ("contains", "excludes", "matches", "min", "max")

# The ops that read their field as a number rather than as text, so they take
# one bound rather than a list of alternatives.
NUMERIC_OPS = ("min", "max")

# Day names for the published_weekday field. Written out rather than taken from
# strftime("%A"), which answers in whatever language the machine happens to be
# set to — a filter in feeds.json must mean the same thing everywhere it runs.
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# Config keys that are settings rather than filters.
KNOWN_KEYS = (
    "name",
    "source",
    "message_template",
    "extract",
    "link_preview",
    "preview_url",
    "preview_title",
    "preview_description",
    "episodic",
    "start",
    "refresh_image",
)

# Keys that decide what is downloaded, which is fetchit's business now. Each
# gets a message saying where it went rather than "unknown key".
FETCHIT_KEYS = ("url", "urls", "since", "max_per_run", "quality", "keep_days", "language")

# The field names every item has, in the order item_fields lists them.
CORE_FIELDS = (
    "title",
    "description",
    "link",
    "published",
    "published_date",
    "published_weekday",
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


class FeedError(Exception):
    """Raised when feed configuration or a feed's items cannot be read."""


class SourceMissing(FeedError):
    """Raised when the library has nothing for a feed yet.

    Not a failure: a feed newly added to both configs has no folder until
    fetchit's next run files its first item, and a run before that has simply
    nothing to send. So this is reported as a skip, with no traceback — but
    every run, because a feed whose ``source`` is misspelled looks exactly the
    same, and that one would wait for ever.
    """


@dataclass(frozen=True)
class FeedFilter:
    """One field test from the config, e.g. ``"title_contains": "Ukraine"``.

    ``values`` holds the alternatives for the test: ``contains`` and ``matches``
    keep an item when *any* value hits, ``excludes`` keeps it when *none* do.
    The numeric ops (:data:`NUMERIC_OPS`) compare rather than match, so they take
    a single bound — alternatives mean nothing to them.
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

    ``source`` is the feed in the library this one sends from — its ``name`` in
    fetchit's config. Left empty, it is ``name``: one name for the feed in
    both places, which is the usual case. See :attr:`library_name`.

    ``episodic`` set makes this a series rather than a ticker: oldest item first,
    released at the cadence's pace. ``start`` is where that walk begins, for a
    back catalogue you don't want all of — it acts as the watermark the feed
    would have had, so everything published before it is behind us already.

    ``refresh_image`` off leaves the group's picture as it is when pictures are
    refreshed (see :mod:`rssignal.artwork`).
    """

    source: str = ""
    name: str = ""
    message_template: str | None = None
    filters: tuple[FeedFilter, ...] = ()
    extract: tuple[FieldExtract, ...] = ()
    link_preview: bool | None = None
    preview_url: str | None = None
    preview_title: str | None = None
    preview_description: str | None = None
    episodic: Cadence | None = None
    start: datetime | None = None
    refresh_image: bool = True

    @property
    def library_name(self) -> str:
        """The feed in the library this one reads: ``source``, or else ``name``."""
        return self.source or self.name


@dataclass(frozen=True)
class FeedItem:
    """A single item of a feed, as the library holds it.

    The named fields are the ones every item is expected to have; anything else
    the source happened to publish (``itunes_duration``, ``id``, …) lands in
    ``extra``. Use :func:`item_fields` to get the flat, stringified view that
    templates and filters see.

    ``kind`` is what fetchit filed it as — ``"audio"``, ``"video"`` or
    ``"text"`` — and ``media`` and ``image`` are the paths of its file and its
    picture in the library, where it has them.
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
    kind: str | None = None
    media: str | None = None
    image: str | None = None


@dataclass(frozen=True)
class ParsedFeed:
    """A feed's items plus what it says about itself.

    ``image`` is the path of the feed's own artwork in the library — a series'
    poster, a podcast's cover — and ``description`` is what the feed says about
    itself. Items deliberately never inherit either: on a preview card the same
    logo and blurb every time say nothing. On the *group* they are exactly
    right, which is the only thing they are used for.
    """

    items: list[FeedItem]
    image: str | None = None
    description: str = ""
    title: str = ""


def is_audio_item(item: FeedItem) -> bool:
    """Whether ``item`` carries audio to send — rssignal's idea of an episode.

    An item that says yes goes out as a voice note with a preview card; one that
    says no goes out as text with its link, or with its video. fetchit has
    already decided, per item, and says so in ``kind``. An item with no ``kind``
    — built by hand rather than read from the library — is judged by its
    enclosure, the way fetchit judges it: the declared type when there is one,
    and the URL when the feed didn't declare anything useful.
    """
    if item.kind is not None:
        return item.kind == AUDIO
    if not item.enclosure_url:
        return False

    mime = (item.enclosure_type or "").strip().lower()
    if mime.startswith("audio/"):
        return True
    if mime and not mime.startswith("application/"):
        return False
    return urlsplit(item.enclosure_url).path.lower().endswith(AUDIO_SUFFIXES)


def is_video_item(item: FeedItem) -> bool:
    """Whether ``item`` is a video to attach, as fetchit filed it."""
    return item.kind == VIDEO


def load_feeds(path: str = "feeds.json") -> list[FeedConfig]:
    """Read ``path`` and return the list of :class:`FeedConfig` it describes.

    Expects ``{"feeds": [ {...}, ... ]}``. Each feed needs a ``name`` — the
    Signal group the feed sends to — and reads the feed of the same name in the
    library unless ``source`` names another. There is no recency setting: how
    far a feed got is remembered in its group's description (see
    :mod:`rssignal.watermark`). Raises :class:`FeedError` on a missing file,
    invalid JSON, or a malformed entry.
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

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise FeedError(
            f"{where} is missing a non-empty \"name\". The name is the Signal "
            "group this feed sends to; rssignal creates it if it doesn't exist."
        )

    source = raw.get("source")
    if source is not None and (not isinstance(source, str) or not source.strip()):
        raise FeedError(
            f"{where} source must be the name of a feed in fetchit's config."
        )

    template = raw.get("message_template")
    if template is not None and not isinstance(template, str):
        raise FeedError(f"{where} message_template must be a string.")

    link_preview = raw.get("link_preview")
    if link_preview is not None and not isinstance(link_preview, bool):
        raise FeedError(f"{where} link_preview must be true or false.")

    refresh_image = raw.get("refresh_image", True)
    if not isinstance(refresh_image, bool):
        raise FeedError(f"{where} refresh_image must be true or false.")

    previews = {}
    for key in ("preview_url", "preview_title", "preview_description"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise FeedError(f"{where} {key} must be a string.")
        previews[key] = value

    episodic = None
    if raw.get("episodic") is not None:
        try:
            episodic = parse_cadence(raw["episodic"], where)
        except ValueError as exc:
            raise FeedError(str(exc)) from exc

    start = _build_start(raw.get("start"), where, episodic)

    return FeedConfig(
        source=source.strip() if isinstance(source, str) else "",
        name=name.strip(),
        message_template=template,
        filters=_build_filters(raw, where),
        extract=_build_extracts(raw.get("extract"), where),
        link_preview=link_preview,
        episodic=episodic,
        start=start,
        refresh_image=refresh_image,
        **previews,
    )


def _build_start(raw: object, where: str, episodic: Cadence | None) -> datetime | None:
    """Validate the ``start`` date an episodic feed begins its walk at.

    Only meaningful on an episodic feed: on a ticker the first run sends the
    newest item and everything before it is behind us by definition, so a
    ``start`` there would be a setting that quietly did nothing.

    A bare date means midnight UTC, which is what somebody writing ``2019-01-01``
    means by it. The stamp is compared against feed timestamps, so it has to be
    aware; a naive one is read as UTC, as everywhere else here.
    """
    if raw is None:
        return None
    if episodic is None:
        raise FeedError(
            f"{where} has a \"start\" but is not \"episodic\". start says where a "
            "series begins; a feed without a cadence starts at its newest item "
            "and moves forward, so there is nothing for it to say. Add "
            "\"episodic\", or drop \"start\" and replay with `rssignal run "
            "--since` instead."
        )
    if not isinstance(raw, str):
        raise FeedError(f"{where} start must be a date like \"2019-01-01\".")

    try:
        when = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise FeedError(
            f"{where} start {raw!r} is not a date rssignal can read. Use "
            "\"2019-01-01\", or a full \"2019-01-01T12:00:00+00:00\"."
        ) from exc
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


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
        if key == "url":
            raise FeedError(
                f"{where} still has a \"url\". rssignal no longer reads feeds "
                "itself: fetchit downloads them into a library, and rssignal "
                "sends from there. Move this feed's url — and any filter that "
                "decides what is worth downloading — to a feed of the same name "
                "in fetchit.json (or give it another name there, and set "
                "\"source\" here to that name). Then drop the key."
            )
        if key in FETCHIT_KEYS:
            raise FeedError(
                f"{where} has {key!r}, which is about downloading rather than "
                "sending. It belongs on this feed in fetchit.json."
            )
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
                f"{where} still has a \"type\". What an item is now comes from "
                "the library: fetchit files each one as audio, video or text, "
                "and rssignal sends it accordingly. Drop the key. To turn a "
                "preview card on or off by hand, use \"link_preview\"."
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

        if op in NUMERIC_OPS:
            filters.append(_numeric_filter(field_name, op, value, where, key))
            continue

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


def _numeric_filter(
    field_name: str, op: str, value: object, where: str, key: str
) -> FeedFilter:
    """Validate one ``<field>_min`` / ``<field>_max`` bound into a filter.

    The bound is a number of seconds, or a duration written with colons, and
    JSON may give either as a number or a string. It is checked here rather than
    per item so ``"duration_seconds_max": "half an hour"`` fails the run up front
    instead of quietly dropping every item.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise FeedError(
            f"{where} {key!r} must be a single bound, not a list — a number of "
            "seconds, or a duration like \"30:00\"."
        )

    text = str(value)
    if _as_number(text) is None:
        raise FeedError(
            f"{where} {key!r} is not a number or a duration: {value!r}."
        )
    return FeedFilter(field=field_name, op=op, values=(text,))


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
    the other two can use. Named fields come first; the source's own extras fill
    in behind them without overwriting a named field.

    The three ``published*`` fields are the one date in three shapes: the full
    timestamp, the day it fell on, and the name of that day — which is there so
    a show with a broadcast week can be filtered down to it. All three are UTC,
    like every date rssignal reads, so a late-evening upload can belong to the
    next day.
    """
    published = item.published.astimezone(timezone.utc) if item.published else None
    fields: dict[str, str] = {
        "title": item.title,
        "description": item.description,
        "link": item.link or "",
        "published": published.isoformat() if published else "",
        "published_date": published.strftime("%Y-%m-%d") if published else "",
        "published_weekday": WEEKDAYS[published.weekday()] if published else "",
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
    or ``link``, and it reads the item as it came out of the library —
    extracts do not chain into one another.

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
    as empty, so ``contains`` drops it while ``excludes`` keeps it — and ``min``
    and ``max`` drop it too, since nothing is not a number.
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
    if flt.op in NUMERIC_OPS:
        value = _as_number(text)
        bound = _as_number(flt.values[0])
        if value is None or bound is None:
            return False
        return value >= bound if flt.op == "min" else value <= bound

    if flt.op == "matches":
        return any(re.search(v, text, re.IGNORECASE) for v in flt.values)

    lowered = text.lower()
    hit = any(v.lower() in lowered for v in flt.values)
    return hit if flt.op == "contains" else not hit


def _as_number(text: str) -> float | None:
    """Read ``text`` as a plain number, or as a clock duration like ``00:42:11``.

    Feeds write a length both ways — YouTube and ARTE report seconds, podcasts
    usually write ``itunes:duration`` with colons — and a ``min`` / ``max``
    filter shouldn't care which, in either the field or the bound it is compared
    against. ``None`` means "not a number", which is how a missing field and a
    field holding prose both end up failing the test.
    """
    text = (text or "").strip()
    if not text:
        return None

    parts = text.split(":")
    if len(parts) > 3:
        return None

    total = 0.0
    for part in parts:
        try:
            total = total * 60 + float(part)
        except ValueError:
            return None
    return total


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
        label = cfg.name or cfg.library_name
        raise FeedError(f"{what} for feed {label!r} is malformed: {exc}") from exc


def preview_fields(item: FeedItem, cfg: FeedConfig) -> dict[str, str] | None:
    """Return the link-preview card for ``item``, or ``None`` for no preview.

    The card is rendered from the same fields as message templates, so anything
    ``rssignal fields`` lists can go in it. ``image`` is the item's own picture
    in the library — a local file, which is what signal-cli wants — or empty.

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
        "image": item.image or "",
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
