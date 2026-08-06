"""Shared fixtures.

The error log defaults to ``./rssignal.log``, which under pytest is the repo
working directory — so without this every test that exercises a failure path
would leave a file behind in the checkout. Pointing it at ``tmp_path`` for every
test keeps that from happening whether or not the test cares about logging.
"""

import pytest

from rssignal import cache, config, download, feeds, signal_cli
from rssignal.errorlog import DEFAULT_LOG_PATH


@pytest.fixture(autouse=True)
def clean_cache(tmp_path, monkeypatch):
    """Give every test its own empty cache, and never the real one.

    :mod:`rssignal.cache` reads a file under the user's cache directory, which a
    test must neither be influenced by nor write to — a remembered channel id or
    ETag would otherwise silently skip the very lookup the test is about, and
    pass for the wrong reason. Both the file and the in-memory copy are reset,
    since the module keeps what it read.
    """
    monkeypatch.setenv(cache.CACHE_ENV, str(tmp_path / "cache.json"))
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def no_signal_daemon(monkeypatch):
    """Never let a test start a real ``signal-cli``.

    :func:`rssignal.run.run_feeds` keeps one alive for the length of a run, which
    is a genuine process launched against a real account — the last thing a test
    run should do, and easy not to notice, since the daemon falls back so quietly
    that the suite passes either way. Tests that mean to exercise the daemon
    build a fake one and turn this off for themselves.
    """
    monkeypatch.setenv(signal_cli.DAEMON_ENV, "0")


@pytest.fixture(autouse=True)
def fresh_dotenv():
    """Don't let one test's ``.env`` be remembered into the next one.

    :func:`rssignal.config.get_config` reads the file once per path and keeps the
    result, which is what a run wants and the opposite of what a suite does.
    """
    config.forget_dotenv()
    yield
    config.forget_dotenv()


@pytest.fixture(autouse=True)
def error_log(tmp_path, monkeypatch):
    """Redirect the traceback log into ``tmp_path``, and yield its path."""
    path = tmp_path / DEFAULT_LOG_PATH
    monkeypatch.setenv("RSSIGNAL_LOG", str(path))
    return path


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch):
    """Keep the network retry, drop its wait.

    Every test that exercises a failing fetch goes through
    :func:`rssignal.feeds.retrying`, which would otherwise sit out its backoff
    and add seconds to the suite for nothing. The retries still happen — only
    the sleeping is removed — so the tests still cover the loop.
    """
    monkeypatch.setattr(feeds.time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def source_reachable(monkeypatch):
    """Answer the source reachability probe without a network.

    :func:`rssignal.youtube.check_available` runs ahead of every feed parse, and
    the probe behind it is a real request — which under pytest would mean the
    whole suite quietly depending on YouTube being up. Tests that care about the
    blocked path override this with ``lambda url, **kw: False``.
    """
    monkeypatch.setattr(download, "reachable", lambda url, **kwargs: True)
