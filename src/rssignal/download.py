"""Fetch things over HTTP: attachments to a temporary path, small text inline.

Every network read outside feedparser goes through this module's ``urlopen``, so
there is one place to patch in tests and one place to change if this ever needs a
session, a user agent, or a retry.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from .feeds import FeedError


def _suffix_for(url: str, default: str = ".mp3") -> str:
    """Return a filename suffix for ``url`` (e.g. ``.mp3``), or ``default``."""
    path = urlparse(url).path
    _, dot, ext = path.rpartition(".")
    if dot and ext and len(ext) <= 5 and "/" not in ext:
        return "." + ext
    return default


def fetch_text(url: str, *, timeout: float = 30) -> str:
    """Fetch ``url`` and return its body decoded as UTF-8.

    For the small text resources that describe media rather than being it — an
    API response, an HLS playlist. Anything big enough to be worth streaming to
    disk wants :func:`download_temp` instead.

    Raises :class:`FeedError` if the fetch fails, so callers can treat a dead
    endpoint the same way they treat a dead feed.
    """
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (URLError, OSError, ValueError) as exc:
        raise FeedError(f"Could not fetch {url!r}: {exc}") from exc


@contextmanager
def download_temp(
    url: str, *, timeout: float = 30, default_suffix: str = ".mp3"
) -> Iterator[str]:
    """Download ``url`` to a temp file, yield its path, and delete it on exit.

    ``default_suffix`` is used when the url has no usable extension; signal-cli
    goes by the file name, so artwork downloads pass an image suffix.

    Raises :class:`FeedError` if the download fails. The file is always removed
    when the ``with`` block ends, whether or not sending succeeded.
    """
    fd, path = tempfile.mkstemp(suffix=_suffix_for(url, default_suffix))
    try:
        # os.fdopen takes ownership of fd, closing it when ``out`` closes.
        with os.fdopen(fd, "wb") as out:
            try:
                with urlopen(url, timeout=timeout) as response:
                    while chunk := response.read(64 * 1024):
                        out.write(chunk)
            except (URLError, OSError, ValueError) as exc:
                raise FeedError(f"Could not download {url!r}: {exc}") from exc
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
