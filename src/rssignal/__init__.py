"""rssignal package."""

from importlib.metadata import version, PackageNotFoundError

from .config import Config, ConfigError, get_config
from .feeds import (
    FeedConfig,
    FeedError,
    FeedFilter,
    FeedItem,
    FieldExtract,
    ParsedFeed,
    apply_extracts,
    apply_filters,
    filter_since,
    is_audio_item,
    item_fields,
    load_feeds,
    newest,
    parse_feed,
    preview_fields,
    render_message,
)
from .run import run_feeds
from .signal_cli import (
    AccountNotLinked,
    LinkPreview,
    SignalCliNotFound,
    SignalError,
    SignalGroup,
    SignalSendError,
    create_group,
    find_group,
    is_account_registered,
    match_group,
    link_device,
    list_accounts,
    list_groups,
    receive,
    send_msg,
    set_group_avatar,
    update_group,
)
from .video import (
    VIDEO_MAX_BYTES,
    VideoPlan,
    arte_collection_id,
    is_video_item,
    parse_arte_collection,
    resolve_video,
    video_temp,
)
from .watermark import compose_description, read_watermark, strip_watermark

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
    "create_group",
    "set_group_avatar",
    "update_group",
    "find_group",
    "match_group",
    "receive",
    "SignalGroup",
    "LinkPreview",
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
    "ParsedFeed",
    "FeedError",
    "FeedFilter",
    "FieldExtract",
    "apply_filters",
    "filter_since",
    "is_audio_item",
    "is_video_item",
    "arte_collection_id",
    "parse_arte_collection",
    "resolve_video",
    "video_temp",
    "VideoPlan",
    "VIDEO_MAX_BYTES",
    "newest",
    "apply_extracts",
    "item_fields",
    "render_message",
    "preview_fields",
    "read_watermark",
    "strip_watermark",
    "compose_description",
]
