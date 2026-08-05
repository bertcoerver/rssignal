"""Shared fixtures.

The error log defaults to ``./rssignal.log``, which under pytest is the repo
working directory — so without this every test that exercises a failure path
would leave a file behind in the checkout. Pointing it at ``tmp_path`` for every
test keeps that from happening whether or not the test cares about logging.
"""

import pytest

from rssignal.errorlog import DEFAULT_LOG_PATH


@pytest.fixture(autouse=True)
def error_log(tmp_path, monkeypatch):
    """Redirect the traceback log into ``tmp_path``, and yield its path."""
    path = tmp_path / DEFAULT_LOG_PATH
    monkeypatch.setenv("RSSIGNAL_LOG", str(path))
    return path
