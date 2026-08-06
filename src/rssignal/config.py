"""Configuration for rssignal, sourced from environment variables / a `.env` file."""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def load_dotenv(path: str = ".env", *, override: bool = False) -> dict[str, str]:
    """Load ``KEY=VALUE`` pairs from a `.env` file into ``os.environ``.

    Lines that are blank or start with ``#`` are ignored. Values may be wrapped
    in matching single or double quotes, which are stripped. A missing file is
    not an error and yields an empty result.

    By default existing environment variables win (so real env vars set by the
    shell or a cloud secret store are not clobbered); pass ``override=True`` to
    let the file take precedence.

    Returns the mapping that was parsed from the file.
    """
    parsed: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return parsed

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        parsed[key] = value
        if override or key not in os.environ:
            os.environ[key] = value

    return parsed


@functools.lru_cache(maxsize=None)
def _load_dotenv_once(path: str) -> None:
    """:func:`load_dotenv`, but only the first time for a given path.

    Loading is idempotent — it writes into ``os.environ`` and existing variables
    win — so skipping repeats changes nothing except how often the file is read.
    """
    load_dotenv(path)


def forget_dotenv() -> None:
    """Let the next :func:`get_config` read the ``.env`` file again."""
    _load_dotenv_once.cache_clear()


@dataclass(frozen=True)
class Config:
    """Runtime configuration for rssignal."""

    account: str
    recipient: str | None = None


def get_config(env_file: str = ".env") -> Config:
    """Build a :class:`Config` from the environment, loading ``env_file`` first.

    Reads ``RSSIGNAL_ACCOUNT`` (required, E.164) and ``RSSIGNAL_RECIPIENT``
    (optional default recipient). Raises :class:`ConfigError` if the account is
    not set.

    The ``.env`` file is read once per path rather than on every call. Every
    ``signal-cli`` wrapper asks for the config, so a run was re-opening and
    re-parsing the same small file dozens of times to reach the same answer —
    and, now that feeds are fetched in parallel, doing it from several threads
    onto the same :data:`os.environ`. Call :func:`forget_dotenv` if the file
    changes underneath a long-lived process.
    """
    _load_dotenv_once(env_file)

    account = os.environ.get("RSSIGNAL_ACCOUNT", "").strip()
    if not account:
        raise ConfigError(
            "RSSIGNAL_ACCOUNT is not set. Set it in your environment or a .env "
            "file to the E.164 number you linked with `rssignal link` "
            "(e.g. RSSIGNAL_ACCOUNT=+31600000000)."
        )

    recipient = os.environ.get("RSSIGNAL_RECIPIENT", "").strip() or None
    return Config(account=account, recipient=recipient)
