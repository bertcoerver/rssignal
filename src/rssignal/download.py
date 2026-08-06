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
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import timing
from .feeds import FeedError, retrying

# A reachability probe is answering one question — is this host there — and must
# not become the slowest part of finding out that it isn't.
PROBE_TIMEOUT = 5


def reachable(url: str, *, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether ``url``'s host answers a HEAD request right now.

    The cheap equivalent of ``curl -sI``: headers only, one attempt, no retry —
    this exists to find out quickly that something is unreachable, so retrying it
    would defeat the point.

    An HTTP error status counts as reachable. A 403 or a 404 is the host
    answering, which is all that is being asked; only a connection that cannot be
    made at all — a name that won't resolve, a refused port, a timeout — is a
    "no".
    """
    try:
        with urlopen(Request(url, method="HEAD"), timeout=timeout):
            return True
    except HTTPError:
        return True
    except (URLError, OSError, ValueError):
        return False


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
    endpoint the same way they treat a dead feed. A fetch that fails for a
    network reason is retried first; see :func:`~rssignal.feeds.retrying`.
    """

    def once() -> str:
        try:
            with urlopen(url, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (URLError, OSError, ValueError) as exc:
            raise FeedError(f"Could not fetch {url!r}: {exc}") from exc

    with timing.step("http fetch", urlparse(url).netloc):
        return retrying(once)


@contextmanager
def download_temp(
    url: str, *, timeout: float = 30, default_suffix: str = ".mp3"
) -> Iterator[str]:
    """Download ``url`` to a temp file, yield its path, and delete it on exit.

    ``default_suffix`` is used when the url has no usable extension; signal-cli
    goes by the file name, so artwork downloads pass an image suffix.

    Raises :class:`FeedError` if the download fails. The file is always removed
    when the ``with`` block ends, whether or not sending succeeded. A download
    cut short by the network is retried from the top — the partial bytes are
    thrown away first, so a retry never appends to half a file.
    """
    fd, path = tempfile.mkstemp(suffix=_suffix_for(url, default_suffix))
    try:
        # os.fdopen takes ownership of fd, closing it when ``out`` closes.
        with os.fdopen(fd, "wb") as out:

            def once() -> None:
                out.seek(0)
                out.truncate()
                try:
                    with urlopen(url, timeout=timeout) as response:
                        while chunk := response.read(64 * 1024):
                            out.write(chunk)
                except (URLError, OSError, ValueError) as exc:
                    raise FeedError(f"Could not download {url!r}: {exc}") from exc

            with timing.step("http download", urlparse(url).netloc):
                retrying(once)
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
