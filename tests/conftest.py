"""Shared fixtures.

The error log defaults to ``./rssignal.log``, which under pytest is the repo
working directory — so without this every test that exercises a failure path
would leave a file behind in the checkout. Pointing it at ``tmp_path`` for every
test keeps that from happening whether or not the test cares about logging.
"""

from contextlib import contextmanager

import pytest

from rssignal import (
    cache,
    config,
    library,
    pending,
    run,
    signal_cli,
)
from rssignal.errorlog import DEFAULT_LOG_PATH


@pytest.fixture(autouse=True)
def own_library(tmp_path, monkeypatch):
    """Point every test at a library of its own, empty until the test fills it.

    The real one — fetchit's folder on this machine, or the add-on's
    ``/media/fetchit`` — must never be what a test happens to read.
    """
    root = tmp_path / "library"
    monkeypatch.setenv(library.LIBRARY_ENV, str(root))
    return root


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
    # :mod:`rssignal.pending` defaults to the cache's directory, so it is already
    # inside tmp_path by the line above — but it decides what gets sent, and a
    # suite that wrote to the real one could suppress a real preview card. Said
    # outright rather than inherited.
    monkeypatch.setenv(pending.PENDING_ENV, str(tmp_path / "pending.json"))
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
def own_run_lock(tmp_path, monkeypatch):
    """Give every test its own run lock.

    :func:`rssignal.run.single_run` takes an exclusive lock so two runs can't
    send the same items twice. Under ``pytest -n`` the workers are separate
    processes, and on the shared default path they would take that seriously:
    tests would block on each other, or fail with AlreadyRunning, depending on
    the timing — which is the lock working exactly as designed, on the one
    workload that must not have it.
    """
    monkeypatch.setenv(run.LOCK_ENV, str(tmp_path / "run.lock"))


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

    Every test that exercises a failing send goes through
    :func:`rssignal.signal_cli._sending`, which would otherwise sit out its
    backoff and add seconds to the suite for nothing. The retries still happen
    — only the sleeping is removed — so the tests still cover the loop.
    """
    monkeypatch.setattr(signal_cli.time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def no_splitting(monkeypatch):
    """Hand every downloaded file to the send as one piece, unmeasured.

    :func:`rssignal.parts.split_temp` reads the file's size to decide whether to
    cut it, and most run tests have no real file at all — they hand
    ``_handle_item`` a made-up path like ``/tmp/fake-ep.mp3``. Splitting is
    covered in tests/test_parts.py; run tests about it put the real one back
    with ``monkeypatch.setattr(run, "split_temp", parts.split_temp)`` or a fake
    of their own.
    """

    @contextmanager
    def whole(path, **kwargs):
        yield [path]

    monkeypatch.setattr(run, "split_temp", whole)
