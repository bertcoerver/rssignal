"""rssignal package."""

from importlib.metadata import version, PackageNotFoundError

from .config import Config, ConfigError, get_config
from .feeds import (
    FeedConfig,
    FeedError,
    FeedItem,
    load_feeds,
    parse_feed,
)
from .run import run_feeds
from .signal_cli import (
    AccountNotLinked,
    SignalCliNotFound,
    SignalError,
    SignalSendError,
    is_account_registered,
    link_device,
    list_accounts,
    send_msg,
)

try:
    __version__ = version("rssignal")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "__version__",
    "Config",
    "ConfigError",
    "get_config",
    "send_msg",
    "link_device",
    "list_accounts",
    "is_account_registered",
    "SignalError",
    "SignalCliNotFound",
    "AccountNotLinked",
    "SignalSendError",
    "run_feeds",
    "load_feeds",
    "parse_feed",
    "FeedConfig",
    "FeedItem",
    "FeedError",
]
