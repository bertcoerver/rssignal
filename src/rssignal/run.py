"""Orchestration: read feeds, filter by recency, and send one message per item.

This ties the pieces together: :func:`rssignal.feeds.load_feeds` /
:func:`~rssignal.feeds.parse_feed` produce items, :func:`~rssignal.feeds.filter_recent`
and :func:`~rssignal.feeds.apply_filters` narrow them, :func:`~rssignal.feeds.render_message`
turns each survivor into text, and it is sent with :func:`rssignal.signal_cli.send_msg`.
Podcast items download their audio enclosure and send it as a voice note, plus a
link preview card whose artwork is downloaded alongside it. Those go out as two
messages: Signal drops a preview card from any message carrying an attachment.

Each feed sends to a Signal group named after it, created on the first send if it
doesn't exist yet — see :class:`_GroupResolver`.

There is no de-duplication yet: running twice within a feed's ``max_age`` window
resends the same items. Seen-tracking is the next milestone.
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
    filter_recent,
    load_feeds,
    parse_feed,
    preview_fields,
    render_message,
)
from .signal_cli import (
    LinkPreview,
    SignalGroup,
    create_group,
    list_groups,
    match_group,
    receive,
    send_msg,
)


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
    now: datetime | None = None,
) -> int:
    """Process every feed in ``config_path`` and send its recent items.

    Each feed goes to the Signal group named after it, which is created on the
    first send if it doesn't exist. ``to`` overrides that for every feed at once
    and touches no group — the safe way to try a real send against your own
    number.

    Returns the number of items sent (or, in ``dry_run`` mode, that would be
    sent). With ``dry_run`` set nothing is sent, downloaded, or created — each
    candidate is printed instead.
    """
    feeds = load_feeds(config_path)
    resolver = _GroupResolver()
    sent = 0
    for cfg in feeds:
        parsed: ParsedFeed = parse_feed(cfg)
        items = filter_recent(parsed.items, cfg.max_age, now=now)
        items = apply_filters(items, cfg.filters)
        if not items:
            # No group is resolved, let alone created, for a feed with nothing to
            # say: an unused feed shouldn't leave an empty group behind.
            continue

        recipient = _recipient_for(cfg, parsed, resolver, to=to, dry_run=dry_run)
        for item in items:
            _handle_item(cfg, item, recipient, dry_run=dry_run)
            sent += 1
    return sent


def _recipient_for(
    cfg: FeedConfig,
    parsed: ParsedFeed,
    resolver: _GroupResolver,
    *,
    to: str | None,
    dry_run: bool,
) -> str:
    """Work out where ``cfg``'s items go, without side effects in a dry run."""
    if to:
        return to
    if dry_run:
        # Looking is safe; creating a group is not. The queue is still drained,
        # so what a dry run reports is what a real run would actually find.
        group = resolver.find(cfg.name)
        return group.recipient if group else f"(group {cfg.name!r} would be created)"
    return resolver.resolve(cfg.name, parsed).recipient


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
