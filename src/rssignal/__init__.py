"""rssignal package."""

from importlib.metadata import version, PackageNotFoundError

from .config import Config, ConfigError, get_config
from .feeds import (
    FeedConfig,
    FeedError,
    FeedFilter,
    FeedItem,
    apply_filters,
    item_fields,
    load_feeds,
    parse_feed,
    render_message,
)
from .run import run_feeds
from .signal_cli import (
    AccountNotLinked,
    SignalCliNotFound,
    SignalError,
    SignalGroup,
    SignalSendError,
    is_account_registered,
    link_device,
    list_accounts,
    list_groups,
    receive,
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
    "list_groups",
    "receive",
    "SignalGroup",
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
    "FeedFilter",
    "apply_filters",
    "item_fields",
    "render_message",
]
