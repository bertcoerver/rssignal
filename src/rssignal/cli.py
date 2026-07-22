"""Command-line interface for rssignal setup and testing.

Subcommands:
    doctor          check the signal-cli install, linked accounts, and config
    link [--name]   link this machine to your Signal account (scan a QR code)
    send MESSAGE    send a text message (uses configured account/recipient)
    run [--config]  parse configured feeds and send their recent items
    fields          show the fields an item exposes, for writing templates
    groups          list the Signal groups you can send to
"""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, get_config
from .feeds import (
    CORE_FIELDS,
    FeedConfig,
    FeedError,
    item_fields,
    load_feeds,
    parse_feed,
    render_message,
)
from .run import run_feeds
from .signal_cli import (
    SignalError,
    find_signal_cli,
    link_device,
    list_accounts,
    list_groups,
    receive,
    send_msg,
)

# Groups reach a linked device as sync messages, so a freshly created one stays
# invisible until the incoming queue is drained at least once.
_REFRESH_HINT = (
    "Missing a group you just created? Run `rssignal groups --refresh` to "
    "receive pending messages first."
)


def _cmd_doctor(args: argparse.Namespace) -> int:
    binary = find_signal_cli()
    print(f"signal-cli: {binary}")

    accounts = list_accounts()
    if accounts:
        print("linked accounts: " + ", ".join(accounts))
    else:
        print("linked accounts: none (run `rssignal link`)")

    try:
        config = get_config()
    except ConfigError as exc:
        print(f"config: {exc}")
        return 1

    print(f"config account: {config.account}")
    print(f"config recipient: {config.recipient or '(none set)'}")

    if config.account not in accounts:
        print(
            f"warning: configured account {config.account} is not linked. "
            "Run `rssignal link`."
        )
        return 1
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    link_device(args.name)
    print("Linking complete.")
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    send_msg(args.message, recipient=args.to)
    print("Message sent.")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    count = run_feeds(args.config, dry_run=args.dry_run)
    if args.dry_run:
        print(f"{count} item(s) would be sent (dry run).")
    else:
        print(f"{count} item(s) sent.")
    return 0


def _resolve_feed(args: argparse.Namespace) -> FeedConfig:
    """Pick the feed the ``fields`` command should sample.

    ``--url`` names a feed directly, which is handy before it's in the config at
    all; otherwise ``--feed`` matches a configured feed by name or URL substring,
    and with neither the first configured feed is used.
    """
    if args.url:
        return FeedConfig(url=args.url, type="regular", name=args.url)

    feeds = load_feeds(args.config)
    if not feeds:
        raise FeedError(f"No feeds configured in {args.config!r}.")

    if not args.feed:
        return feeds[0]

    needle = args.feed.lower()
    for cfg in feeds:
        if needle in (cfg.name or "").lower() or needle in cfg.url.lower():
            return cfg
    known = ", ".join(repr(cfg.name or cfg.url) for cfg in feeds)
    raise FeedError(f"No feed matching {args.feed!r}. Configured feeds: {known}.")


def _cmd_fields(args: argparse.Namespace) -> int:
    cfg = _resolve_feed(args)
    items = parse_feed(cfg)
    if not items:
        print(f"Feed {cfg.name or cfg.url!r} has no items.")
        return 1
    if not 1 <= args.item <= len(items):
        raise ValueError(
            f"--item {args.item} is out of range; the feed has {len(items)} item(s)."
        )

    item = items[args.item - 1]
    fields = item_fields(item)
    print(f"Fields for {cfg.name or cfg.url} (item {args.item} of {len(items)}):\n")

    width = max(len(name) for name in fields)
    for name, value in fields.items():
        flat = " ".join(value.split())
        if len(flat) > 70:
            flat = flat[:69] + "…"
        suffix = "  (extra)" if name not in CORE_FIELDS else ""
        print(f"  {name:<{width}}  {flat}{suffix}")

    print(f"\nUse any name above in a template, e.g. \"{{{next(iter(fields))}}}\".")

    if args.template:
        preview = render_message(
            item,
            FeedConfig(url=cfg.url, type=cfg.type, name=cfg.name,
                       message_template=args.template),
        )
        print("\n--- rendered message ---")
        print(preview)
    return 0


def _cmd_groups(args: argparse.Namespace) -> int:
    """Print the groups this account can send to, with copy-pasteable ids."""
    if args.refresh:
        print("Receiving pending messages…")
        receive()

    groups = list_groups()
    if not args.all:
        groups = [g for g in groups if g.active and not g.blocked]

    if not groups:
        print("No groups found. (Groups you have left need --all.)")
        if not args.refresh:
            print(_REFRESH_HINT)
        return 1

    if args.quiet:
        for group in groups:
            print(group.recipient)
        return 0

    width = max(len(g.name) for g in groups)
    for group in groups:
        flags = "".join(
            [" (inactive)" if not group.active else "", " (blocked)" if group.blocked else ""]
        )
        print(f"  {group.name:<{width}}  {group.recipient}{flags}")

    print(
        "\nUse a value above as a `recipient` in feeds.json, as `--to`, or as "
        "RSSIGNAL_RECIPIENT."
    )
    if not args.refresh:
        print(_REFRESH_HINT)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rssignal", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="check signal-cli install, accounts, and config"
    )
    doctor.set_defaults(func=_cmd_doctor)

    link = subparsers.add_parser(
        "link", help="link this machine to your Signal account"
    )
    link.add_argument(
        "--name",
        default="rssignal",
        help="device name shown in Signal (default: rssignal)",
    )
    link.set_defaults(func=_cmd_link)

    send = subparsers.add_parser("send", help="send a text message")
    send.add_argument("message", help="the message text to send")
    send.add_argument(
        "--to",
        default=None,
        help="recipient number or group id (default: RSSIGNAL_RECIPIENT)",
    )
    send.set_defaults(func=_cmd_send)

    run = subparsers.add_parser(
        "run", help="parse configured feeds and send their recent items"
    )
    run.add_argument(
        "--config",
        default="feeds.json",
        help="path to the feeds JSON config (default: feeds.json)",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent without sending or downloading",
    )
    run.set_defaults(func=_cmd_run)

    fields = subparsers.add_parser(
        "fields", help="show the fields an item exposes, for writing templates"
    )
    fields.add_argument(
        "--config",
        default="feeds.json",
        help="path to the feeds JSON config (default: feeds.json)",
    )
    fields.add_argument(
        "--feed",
        default=None,
        help="configured feed to sample, by name or URL (default: the first one)",
    )
    fields.add_argument(
        "--url",
        default=None,
        help="sample this feed URL directly, without adding it to the config",
    )
    fields.add_argument(
        "--item",
        type=int,
        default=1,
        help="which item to sample, 1-based (default: 1)",
    )
    fields.add_argument(
        "--template",
        default=None,
        help="also render this message_template against the sampled item",
    )
    fields.set_defaults(func=_cmd_fields)

    groups = subparsers.add_parser(
        "groups", help="list the Signal groups you can send to"
    )
    groups.add_argument(
        "--all",
        action="store_true",
        help="also show groups you have left or blocked",
    )
    groups.add_argument(
        "--refresh",
        action="store_true",
        help="receive pending messages first, to pick up newly created groups",
    )
    groups.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print only the recipient values, one per line",
    )
    groups.set_defaults(func=_cmd_groups)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``rssignal`` console script."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SignalError, ConfigError, FeedError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
