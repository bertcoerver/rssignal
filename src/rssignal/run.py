"""Orchestration: read feeds, work out what's new, and send one message per item.

This ties the pieces together: :func:`rssignal.feeds.load_feeds` /
:func:`~rssignal.feeds.parse_feed` produce items, :func:`~rssignal.feeds.apply_filters`
and :func:`~rssignal.feeds.filter_since` narrow them,
:func:`~rssignal.feeds.render_message` turns each survivor into text, and it is
sent with :func:`rssignal.signal_cli.send_msg`. Podcast items download their audio
enclosure and send it as a voice note, plus a link preview card whose artwork is
downloaded alongside it. Those go out as two messages: Signal drops a preview card
from any message carrying an attachment.

Each feed sends to a Signal group named after it, created on the first send if it
doesn't exist yet — see :class:`_GroupResolver`.

How far a feed got is remembered in that group's own description, so a feed is
never sent twice and running more often costs nothing. See
:mod:`rssignal.watermark` for why the description, of all places, and
:func:`_send_feed` for the order things happen in.
"""

from __future__ import annotations

import sys
from contextlib import ExitStack
from datetime import datetime

from .download import download_temp
from .feeds import (
    FeedConfig,
    FeedError,
    FeedItem,
    ParsedFeed,
    apply_filters,
    filter_since,
    load_feeds,
    newest,
    parse_feed,
    preview_fields,
    render_message,
)
from .signal_cli import (
    GROUP_DESCRIPTION_MAX_CHARS,
    LinkPreview,
    SignalError,
    SignalGroup,
    create_group,
    list_groups,
    match_group,
    receive,
    send_msg,
    update_group,
)
from .watermark import compose_description, read_watermark, strip_watermark


class _GroupResolver:
    """Maps a feed's name to the group it sends to, creating it if needed.

    The listing is fetched once and reused: every ``signal-cli`` call pays a JVM
    startup, so resolving N feeds should not cost N listings. A group created
    here is added to the cached listing rather than triggering a re-read.

    ``receive`` runs once, before that first listing. rssignal is a linked
    secondary device, so ``listGroups`` reads local state that only catches up
    when the sync queue is drained. Refreshing lazily — on a miss — is not
    enough: a group you have *left* still reads as active until then, and
    matching it means sending into a group you are no longer in. signal-cli
    exits 0 doing that, so the failure is completely silent.
    """

    def __init__(self) -> None:
        self._groups: list[SignalGroup] | None = None

    def _listing(self) -> list[SignalGroup]:
        if self._groups is None:
            receive()
            self._groups = list_groups()
        return self._groups

    def find(self, name: str) -> SignalGroup | None:
        """Return the group called ``name``, or ``None`` if there isn't one."""
        return match_group(self._listing(), name)

    def resolve(self, name: str, parsed: ParsedFeed) -> SignalGroup:
        """Return the group called ``name``, creating it if it doesn't exist.

        A new group holds only this account, is announcement-only (it exists to
        receive a feed, not to be a chat), and takes the feed's own artwork and
        blurb as its picture and description.
        """
        group = self.find(name)
        if group is not None:
            return group

        with ExitStack() as stack:
            avatar = _local_image(parsed.image_url, stack, name, what="group image")
            group = create_group(
                name,
                description=parsed.description or None,
                avatar=avatar or None,
                announcement_only=True,
            )

        # Creating a group is not something to do quietly: a typo in a feed's
        # name would otherwise leave a stray group behind without a word.
        print(f"Created group {group.name!r} for this feed.")
        self._listing().append(group)
        return group


def run_feeds(
    config_path: str = "feeds.json",
    *,
    dry_run: bool = False,
    to: str | None = None,
    since: datetime | None = None,
) -> int:
    """Process every feed in ``config_path`` and send whatever is new.

    Each feed goes to the Signal group named after it, which is created on the
    first send if it doesn't exist. ``to`` overrides that for every feed at once
    and touches no group — the safe way to try a real send against your own
    number. ``since`` overrides every stored watermark, for replaying a stretch
    of a feed by hand.

    Returns the number of items sent (or, in ``dry_run`` mode, that would be
    sent). With ``dry_run`` set nothing is sent, downloaded, created, or
    recorded — each candidate is printed instead.
    """
    feeds = load_feeds(config_path)
    resolver = _GroupResolver()
    return sum(
        _send_feed(cfg, resolver, dry_run=dry_run, to=to, since=since)
        for cfg in feeds
    )


def _send_feed(
    cfg: FeedConfig,
    resolver: _GroupResolver,
    *,
    dry_run: bool,
    to: str | None,
    since: datetime | None,
) -> int:
    """Send one feed's new items, and record how far it got. Returns the count.

    The order here is deliberate. The group is looked up *before* deciding what
    to send, because the watermark lives on it — but it is only ever *created*
    once there is something to send, so a typo in a feed's name can't leave a
    stray group behind. The watermark is written last, from the items that
    actually went out.
    """
    parsed: ParsedFeed = parse_feed(cfg)
    items = apply_filters(parsed.items, cfg.filters)
    if not items:
        return 0

    # With --to, no group is touched at all: nothing to read a watermark from,
    # and nothing to write one to. That leaves --to what it has always been —
    # a send that has no effect on rssignal's idea of where a feed got to.
    group = None if to else resolver.find(cfg.name)
    mark = since or (read_watermark(group.description) if group else None)

    if mark is None:
        # Nothing remembered yet. One item, not the whole back catalogue.
        latest = newest(items)
        due = [latest] if latest else []
    else:
        due = filter_since(items, mark)
    if not due:
        return 0

    if dry_run:
        where = to or (group.recipient if group else f"(group {cfg.name!r} would be created)")
        for item in due:
            _handle_item(cfg, item, where, dry_run=True)
        return len(due)

    if to:
        recipient = to
    else:
        # The one place a group is created, and only now that there is something
        # to put in it: a typo in a feed's name can't leave a stray group behind.
        group = group or resolver.resolve(cfg.name, parsed)
        recipient = group.recipient

    # due is oldest-first, so a send that fails part-way leaves the watermark on
    # the last item that made it and the rest are retried next run. The finally
    # is what makes that true even when send_msg raises.
    done: FeedItem | None = None
    sent = 0
    try:
        for item in due:
            _handle_item(cfg, item, recipient, dry_run=False)
            done = item
            sent += 1
    finally:
        if done is not None and not to:
            _record_progress(group, parsed, done)

    return sent


def _record_progress(group: SignalGroup, parsed: ParsedFeed, done: FeedItem) -> None:
    """Move ``group``'s watermark to ``done``, keeping its blurb.

    The blurb kept is the group's own, not the feed's: edit a group description
    in Signal and rssignal moves the marker around your text instead of pasting
    the feed's boilerplate back over it every run. The feed's blurb is only the
    fallback, for a group that hasn't got one.

    Failing to record is reported but not raised. The items really were sent;
    turning that into a failed run would only mean sending them again.
    """
    blurb = strip_watermark(group.description) or parsed.description
    description = compose_description(
        blurb, done.published, limit=GROUP_DESCRIPTION_MAX_CHARS
    )
    try:
        update_group(group.id, description=description)
    except SignalError as exc:
        print(
            f"[{group.name}] sent, but recording how far the feed got failed: "
            f"{exc}\nThose items will be sent again on the next run.",
            file=sys.stderr,
        )


def _handle_item(
    cfg: FeedConfig, item: FeedItem, recipient: str, *, dry_run: bool
) -> None:
    """Send (or, in dry-run, describe) a single item from feed ``cfg``."""
    text = render_message(item, cfg)
    is_podcast = cfg.type == "podcast" and bool(item.enclosure_url)
    card = preview_fields(item, cfg)
    label = cfg.name or cfg.url

    if dry_run:
        first_line = text.splitlines()[0] if text else "(no text)"
        print(f"[{label}] -> {recipient}: {first_line}")
        if card:
            print(f"    preview: {card['title']} <{card['url']}>")
            if card["image_url"]:
                print(f"    preview image: {card['image_url']}")
        if is_podcast:
            suffix = " (second message)" if card else ""
            print(f"    voice note{suffix}: {item.enclosure_url}")
        return

    with ExitStack() as stack:
        attachments = None
        if is_podcast:
            attachments = [stack.enter_context(download_temp(item.enclosure_url))]

        preview = None
        if card:
            preview = LinkPreview(
                url=card["url"],
                title=card["title"],
                description=card["description"],
                image=_local_image(card["image_url"], stack, label),
            )

        if attachments and preview:
            # Signal drops the preview card when the same message carries an
            # attachment, so the card and the audio go out separately: the text
            # and card first, then the voice note on its own.
            send_msg(text, recipient=recipient, preview=preview)
            send_msg(
                "",
                recipient=recipient,
                attachments=attachments,
                voice_note=True,
            )
        else:
            send_msg(
                text,
                recipient=recipient,
                attachments=attachments,
                voice_note=is_podcast,
                preview=preview,
            )


def _local_image(
    url: str | None, stack: ExitStack, label: str, *, what: str = "preview image"
) -> str:
    """Download an image, returning its local path or ``""``.

    Artwork is decoration: if it can't be fetched the message (or the group)
    should still happen, so a failed download degrades to no image.
    """
    if not url:
        return ""
    try:
        return stack.enter_context(download_temp(url, default_suffix=".jpg"))
    except FeedError as exc:
        print(f"[{label}] {what} skipped: {exc}", file=sys.stderr)
        return ""
