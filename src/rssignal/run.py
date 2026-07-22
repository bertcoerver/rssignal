"""Orchestration: read feeds, filter by recency, and send one message per item.

This ties the pieces together: :func:`rssignal.feeds.load_feeds` /
:func:`~rssignal.feeds.parse_feed` produce items, :func:`~rssignal.feeds.filter_recent`
and :func:`~rssignal.feeds.apply_filters` narrow them, :func:`~rssignal.feeds.render_message`
turns each survivor into text, and it is sent with :func:`rssignal.signal_cli.send_msg`.
Podcast items download their audio enclosure and send it as a voice note.

There is no de-duplication yet: running twice within a feed's ``max_age`` window
resends the same items. Seen-tracking is the next milestone.
"""

from __future__ import annotations

from datetime import datetime

from .download import download_temp
from .feeds import (
    FeedConfig,
    FeedItem,
    apply_filters,
    filter_recent,
    load_feeds,
    parse_feed,
    render_message,
)
from .signal_cli import send_msg


def run_feeds(
    config_path: str = "feeds.json",
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> int:
    """Process every feed in ``config_path`` and send its recent items.

    Returns the number of items sent (or, in ``dry_run`` mode, that would be
    sent). With ``dry_run`` set, nothing is sent or downloaded — each candidate
    is printed instead, which is the safe way to check a config locally.
    """
    feeds = load_feeds(config_path)
    sent = 0
    for cfg in feeds:
        items = filter_recent(parse_feed(cfg), cfg.max_age, now=now)
        items = apply_filters(items, cfg.filters)
        label = cfg.name or cfg.url
        for item in items:
            _handle_item(cfg, item, label, dry_run=dry_run)
            sent += 1
    return sent


def _handle_item(
    cfg: FeedConfig, item: FeedItem, label: str, *, dry_run: bool
) -> None:
    """Send (or, in dry-run, describe) a single item from feed ``cfg``."""
    text = render_message(item, cfg)
    is_podcast = cfg.type == "podcast" and bool(item.enclosure_url)

    if dry_run:
        first_line = text.splitlines()[0] if text else "(no text)"
        print(f"[{label}] -> {cfg.recipient or '(default recipient)'}: {first_line}")
        if is_podcast:
            print(f"    voice note: {item.enclosure_url}")
        return

    if is_podcast:
        with download_temp(item.enclosure_url) as path:
            send_msg(
                text,
                recipient=cfg.recipient,
                attachments=[path],
                voice_note=True,
            )
    else:
        send_msg(text, recipient=cfg.recipient)
