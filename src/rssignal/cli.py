"""Command-line interface for rssignal setup and testing.

Subcommands:
    doctor          check the signal-cli install, linked accounts, and config
    link [--name]   link this machine to your Signal account (scan a QR code)
    send MESSAGE    send a text message (uses configured account/recipient)
    run [--config]  parse configured feeds and send their recent items
"""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, get_config
from .feeds import FeedError
from .run import run_feeds
from .signal_cli import (
    SignalError,
    find_signal_cli,
    link_device,
    list_accounts,
    send_msg,
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
