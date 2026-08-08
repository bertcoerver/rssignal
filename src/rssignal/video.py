"""Fetch the video behind an item that only links to one.

Some feeds are about video but carry none: the item links to a player page and
the video itself lives behind a streaming manifest or a format catalogue. ARTE
and YouTube are the two rssignal knows; :mod:`rssignal.arte` and
:mod:`rssignal.youtube` know how to talk to each, and this module is the front
door that decides which — so nothing outside has to know how many sources exist
or which one an item came from.

Like :func:`rssignal.feeds.is_audio_item`, that decision is made per item from
the link, with nothing to configure: an item that looks like an ARTE programme
or a YouTube video gets its video, one that doesn't goes out as ordinary text.
A video feed that posts the occasional article therefore still reads as an
article.

The two things every source has in common live here: the size budget, and the
:class:`VideoPlan` — what *would* be downloaded, worked out without downloading
it, so a dry run can report the resolution and size an item will really arrive
at rather than promising "a video".
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol, runtime_checkable

from .feeds import FeedConfig, FeedError, FeedItem, ParsedFeed

# Signal rejects an attachment over 100 MB outright. The budget is set below
# that because the size of a rendition is an estimate until it has been
# downloaded, and a file that turns out too big has cost the whole download by
# the time anyone finds out.
VIDEO_MAX_BYTES = 95 * 1024 * 1024

# Bandwidth figures are averages, so a talky ten minutes lands under one and an
# action-heavy one over. The headroom absorbs that, plus container overhead.
SIZE_ESTIMATE_MARGIN = 1.1

# Generous: this is a whole video pulled a segment at a time, and the
# alternative to waiting is losing the episode.
VIDEO_TIMEOUT = 900


class VideoTooShort(FeedError):
    """Raised for a video not worth sending at all — a Short, or a teaser.

    Distinct from a plain :class:`~rssignal.feeds.FeedError` because it means
    something different to the caller: a `FeedError` is "the video couldn't be
    had, send the text anyway", this is "there is nothing here worth a message".
    """


@runtime_checkable
class VideoPlan(Protocol):
    """What a source would download for an item, decided before downloading it.

    Working this out costs a couple of small requests, which buys two things: a
    dry run that names the real resolution and size, and the chance to refuse an
    item that can't fit before spending a download on finding out.
    """

    def describe(self) -> str:
        """One line for a dry run, e.g. ``"640x360, ~63 MB"``."""

    def fetch(self, into: str, *, timeout: float) -> str:
        """Download into directory ``into`` and return the file's path."""


def is_video_item(item: FeedItem) -> bool:
    """Whether ``item`` links to a video rssignal knows how to fetch.

    The counterpart to :func:`rssignal.feeds.is_audio_item`, and asked the same
    way: per item, from what the item already says, with nothing to configure.
    """
    from .arte import arte_program_id
    from .youtube import youtube_video_id

    link = item.link
    return arte_program_id(link) is not None or youtube_video_id(link) is not None


def resolve_video(item: FeedItem, *, max_bytes: int = VIDEO_MAX_BYTES) -> VideoPlan:
    """Work out what ``item``'s video would be fetched as, without fetching it.

    Raises :class:`~rssignal.feeds.FeedError` if the item is not a video, its
    source has nothing usable, or every quality is too big to send, and
    :class:`VideoTooShort` if the video isn't worth sending.
    """
    from . import arte, youtube

    if arte.arte_program_id(item.link):
        return arte.resolve(item, max_bytes=max_bytes)
    if youtube.youtube_video_id(item.link):
        return youtube.resolve(item, max_bytes=max_bytes)
    raise FeedError(f"Not a video link: {item.link!r}")


@contextmanager
def video_temp(
    item: FeedItem,
    *,
    max_bytes: int = VIDEO_MAX_BYTES,
    timeout: float = VIDEO_TIMEOUT,
    plan: VideoPlan | None = None,
) -> Iterator[str]:
    """Download ``item``'s video to a temp file, yield its path, delete it after.

    Pass ``plan`` to reuse an already-resolved :func:`resolve_video` result
    rather than asking the source the same questions twice.

    A whole directory is handed to the source rather than a path, because they
    don't all agree on the extension until the download is done. It goes away
    with everything in it when the ``with`` block ends, whether or not sending
    succeeded — the same contract as :func:`rssignal.download.download_temp`, so
    callers can hold both on one ``ExitStack``.

    Raises :class:`~rssignal.feeds.FeedError` if anything along the way fails.
    """
    if plan is None:
        plan = resolve_video(item, max_bytes=max_bytes)

    with tempfile.TemporaryDirectory(prefix="rssignal-video-") as into:
        path = plan.fetch(into, timeout=timeout)

        size = os.path.getsize(path)
        if size > max_bytes:
            # The estimate was optimistic. Better to say so than to hand
            # signal-cli a file Signal will refuse.
            raise FeedError(
                f"Video is {size / 1024 / 1024:.0f} MB, over the "
                f"{max_bytes / 1024 / 1024:.0f} MB limit"
            )
        yield path


def collection_feed(cfg: FeedConfig) -> ParsedFeed | None:
    """Read ``cfg.url`` as a video source's own listing, if it is one.

    ``None`` means "not one of those" and the url should be parsed as a feed.
    """
    from .arte import arte_collection_id, parse_arte_collection

    if arte_collection_id(cfg.url):
        return parse_arte_collection(cfg)
    return None


def annotate_durations(feed_url: str, items: list[FeedItem]) -> list[FeedItem]:
    """Fill in how long each item's video is, for the source that doesn't say.

    ARTE's own listing carries a duration and puts it on the item as it reads it
    (see :func:`~rssignal.arte.parse_arte_collection`); YouTube's Atom feed has
    none, so it is looked up. Either way an item ends up with the same
    ``duration_seconds`` field, which is what makes ``duration_seconds_min`` a
    filter you can write without knowing where the item came from.

    Items from anything else come back untouched — a feed that has no video in
    it pays nothing for this.
    """
    from .youtube import with_durations

    return with_durations(feed_url, items)


def check_source_available(url: str) -> None:
    """Fail fast if ``url``'s source can't be reached from this machine at all.

    Sources differ in what they cost to find that out. ARTE is read over plain
    HTTP with a short timeout, so an unreachable one announces itself in seconds
    and needs nothing here. YouTube is read through yt-dlp, several requests
    deep, each willing to wait a minute — so it gets a probe first. See
    :func:`~rssignal.youtube.check_available`.

    Raises :class:`~rssignal.feeds.SourceBlocked`, and raises nothing at all for
    a url whose source has no such check.
    """
    from .youtube import check_available

    check_available(url)


def channel_feed_url(url: str) -> str | None:
    """The feed url behind a channel page, if ``url`` is one rssignal knows.

    Where :func:`collection_feed` is for a source with no feed at all, this is
    for one that has a perfectly good feed at an address nobody would guess —
    YouTube's. Rewriting the url means everything downstream sees plain Atom.
    """
    from .youtube import youtube_feed_url

    return youtube_feed_url(url)


def with_archive(
    feed_url: str, items: list[FeedItem], *, feed_name: str = ""
) -> list[FeedItem]:
    """Put a channel's back catalogue in front of ``items``, if it has one.

    Asked only for an episodic feed (:mod:`rssignal.episodic`), which is the only
    kind with any use for what a source published years ago. Anything but a
    YouTube channel feed comes back untouched — ARTE's collections already carry
    as much of their own history as they carry.
    """
    from .youtube import archive_items

    return archive_items(feed_url, items, feed_name=feed_name)
