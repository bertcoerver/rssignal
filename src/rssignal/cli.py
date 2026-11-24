"""Command-line interface for rssignal setup and testing.

Subcommands:
    doctor          check the signal-cli install, linked accounts, and config
    link [--name]   link this machine to your Signal account (scan a QR code)
    send MESSAGE    send a text message (uses configured account/recipient)
    run [--config]  read each feed from the library and send whatever is new
                    to the Signal group named after it (created if missing)
    fields          show the fields an item exposes, for writing templates
    groups          list the Signal groups you can send to
    create-group    create a new group containing only you
    refresh-images  update each feed group's picture from the library's artwork
    serve           run on a schedule, with a web GUI and an API to send through
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone

from dataclasses import replace

from . import timing
from .config import ConfigError, get_config
from .errorlog import log_exception, log_line, log_path
from .feeds import (
    CORE_FIELDS,
    FeedConfig,
    FeedError,
    apply_extracts,
    item_fields,
    load_feeds,
    render_message,
)
from .library import LIBRARY_ENV, library_root, read_feed
from .run import AlreadyRunning, refresh_group_images, run_feeds
from .signal_cli import (
    SignalError,
    create_group,
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

    # Only files too big for one message need it, so its absence is worth
    # reporting but is not a broken install.
    ffmpeg = shutil.which("ffmpeg")
    print(f"ffmpeg: {ffmpeg or 'not found (only needed to split files over 95 MB)'}")

    # Where to look after an unattended run went wrong, printed whether or not
    # the file exists yet — the point is knowing where it will be.
    print(f"error log: {os.path.abspath(log_path())}")

    # Where the items come from. A library that isn't there is the one thing
    # that makes every feed quietly send nothing, so it is said out loud.
    root = library_root()
    if os.path.isdir(root):
        kinds = [k for k in ("shows", "podcasts", "articles") if os.path.isdir(os.path.join(root, k))]
        print(f"library: {root} ({', '.join(kinds) or 'empty so far'})")
    else:
        print(f"library: {root} does not exist (set {LIBRARY_ENV} to fetchit's library)")

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
    # Only `send` uses this; `run` sends to each feed's own group.
    print(f"config recipient (for `send`): {config.recipient or '(none set)'}")

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
    if getattr(args, "timings", False):
        timing.enable()
    try:
        try:
            count = run_feeds(
                args.config,
                dry_run=args.dry_run,
                to=args.to,
                since=_parse_since(args.since),
            )
        except AlreadyRunning as exc:
            # Exit 0: the last run being slow is not this run's failure, and an
            # unattended caller — cron, a Shortcut — should not start reporting
            # errors because a video took a while. Standing aside *is* the
            # correct outcome, and the log line says it happened.
            print(f"Nothing to do: {exc}.", file=sys.stderr)
            log_line(f"run skipped: {exc}")
            return 0
    finally:
        # Reported even when the run blew up: a run that died after forty
        # seconds is precisely the one you want the breakdown for.
        timing.report()
    if args.dry_run:
        print(f"{count} item(s) would be sent (dry run).")
    else:
        print(f"{count} item(s) sent.")
    return 0


def _parse_since(value: str | None) -> datetime | None:
    """Read ``--since`` as a UTC datetime, or ``None`` when it wasn't given.

    A bare date means midnight, and a timestamp without a zone is read as UTC —
    feed publication dates are compared in UTC, so a naive one would raise on
    the comparison rather than here.
    """
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise SystemExit(
            f"--since {value!r} is not an ISO timestamp. Try 2026-07-01 or "
            "2026-07-01T09:00+02:00."
        )
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _resolve_feed(args: argparse.Namespace) -> FeedConfig:
    """Pick the feed the ``fields`` command should sample.

    ``--source`` names a feed in the library directly, which is handy before
    it's in feeds.json at all; otherwise ``--feed`` matches a configured feed by
    name or source, and with neither the first configured feed is used.
    """
    if args.source:
        return FeedConfig(source=args.source, name=args.source)

    feeds = load_feeds(args.config)
    if not feeds:
        raise FeedError(f"No feeds configured in {args.config!r}.")

    if not args.feed:
        return feeds[0]

    needle = args.feed.lower()
    for cfg in feeds:
        if needle in (cfg.name or "").lower() or needle in cfg.library_name.lower():
            return cfg
    known = ", ".join(repr(cfg.name or cfg.library_name) for cfg in feeds)
    raise FeedError(f"No feed matching {args.feed!r}. Configured feeds: {known}.")


def _cmd_fields(args: argparse.Namespace) -> int:
    cfg = _resolve_feed(args)
    # Newest first, the way a feed lists them: item 1 is the latest.
    items = [apply_extracts(i, cfg.extract) for i in reversed(read_feed(cfg).items)]
    if not items:
        print(f"Feed {cfg.name or cfg.library_name!r} has no items in the library.")
        return 1
    if not 1 <= args.item <= len(items):
        raise ValueError(
            f"--item {args.item} is out of range; the feed has {len(items)} item(s)."
        )

    item = items[args.item - 1]
    fields = item_fields(item)
    print(f"Fields for {cfg.name or cfg.library_name} (item {args.item} of {len(items)}, a {item.kind or 'text'} item):\n")

    width = max(len(name) for name in fields)
    for name, value in fields.items():
        flat = " ".join(value.split())
        if len(flat) > 70:
            flat = flat[:69] + "…"
        suffix = "  (extra)" if name not in CORE_FIELDS else ""
        print(f"  {name:<{width}}  {flat}{suffix}")

    print(f"\nUse any name above in a template, e.g. \"{{{next(iter(fields))}}}\".")

    if args.template:
        preview = render_message(item, replace(cfg, message_template=args.template))
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
        "\nUse a value above with `rssignal send --to`, `rssignal run --to`, or "
        "as RSSIGNAL_RECIPIENT.\nFeeds pick their group by name, so they don't "
        "need one of these."
    )
    if not args.refresh:
        print(_REFRESH_HINT)
    return 0


def _cmd_create_group(args: argparse.Namespace) -> int:
    """Create a group containing only this account, and print its recipient."""
    group = create_group(
        args.name,
        description=args.description,
        avatar=args.avatar,
        announcement_only=args.announcement,
    )
    if args.quiet:
        print(group.recipient)
        return 0

    print(f"Created group {group.name!r} with only you in it.")
    print(f"\n  {group.recipient}\n")
    print(
        "Name a feed after this group and it will send here on its own.\n"
        "Add people from Signal on your phone — rssignal never adds members."
    )
    return 0


def _cmd_refresh_images(args: argparse.Namespace) -> int:
    try:
        count = refresh_group_images(args.config, dry_run=args.dry_run)
    except AlreadyRunning as exc:
        print(f"Nothing to do: {exc}.", file=sys.stderr)
        return 0
    if args.dry_run:
        print(f"{count} group image(s) would be updated (dry run).")
    else:
        print(f"{count} group image(s) updated.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(args.config, host=args.host, port=args.port, on_start=args.on_start)
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
        "run", help="read each feed from the library and send whatever is new"
    )
    run.add_argument(
        "--config",
        default="feeds.json",
        help="path to the feeds JSON config (default: feeds.json)",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent without sending or creating anything",
    )
    run.add_argument(
        "--to",
        default=None,
        help=(
            "send everything to this recipient instead of each feed's own group, "
            "and create no groups (use your own number to try a real send)"
        ),
    )
    run.add_argument(
        "--since",
        default=None,
        metavar="TIMESTAMP",
        help=(
            "send items published after this ISO timestamp, ignoring what each "
            "group remembers (e.g. 2026-07-01, or 2026-07-01T09:00+02:00)"
        ),
    )
    run.add_argument(
        "--timings",
        action="store_true",
        help="report on stderr how long each part of the run took",
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
        help="configured feed to sample, by name or source (default: the first one)",
    )
    fields.add_argument(
        "--source",
        default=None,
        help="sample this feed in the library directly, without adding it to the config",
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

    create = subparsers.add_parser(
        "create-group",
        help="create a new group containing only you",
        description=(
            "Create a Signal group with you as its only member and admin. "
            "rssignal never adds anyone else — invite people from Signal on "
            "your phone."
        ),
    )
    create.add_argument("name", help="the group name")
    create.add_argument(
        "--description",
        default=None,
        help="group description",
    )
    create.add_argument(
        "--avatar",
        default=None,
        help="group image: a path to a local image file (not a URL)",
    )
    create.add_argument(
        "--announcement",
        action="store_true",
        help="only admins may send messages (useful for a feed-only group)",
    )
    create.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print only the recipient value",
    )
    create.set_defaults(func=_cmd_create_group)

    refresh = subparsers.add_parser(
        "refresh-images",
        help="update each feed group's picture from the library's artwork",
        description=(
            "Set every feed's artwork in the library as the picture of the "
            "feed's group, where it has changed. `rssignal run` does this by "
            "itself after sending."
        ),
    )
    refresh.add_argument(
        "--config",
        default="feeds.json",
        help="path to the feeds JSON config (default: feeds.json)",
    )
    refresh.add_argument(
        "--dry-run",
        action="store_true",
        help="report which groups would get a new picture without changing any",
    )
    refresh.set_defaults(func=_cmd_refresh_images)

    serve = subparsers.add_parser(
        "serve",
        help="run on a schedule, with a web GUI and an API to send through",
        description=(
            "Keep one signal-cli open and run the feeds at the times in "
            "RSSIGNAL_SCHEDULE (e.g. 01:30,06:30,15:00), serve the GUI, and "
            "accept messages to send — including from Home Assistant's Signal "
            "Messenger integration. See the README."
        ),
    )
    serve.add_argument("--config", default="feeds.json", help="path to the feeds JSON config (default: feeds.json)")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8099, help="port to listen on (default: 8099)")
    serve.add_argument(
        "--on-start",
        choices=("wait", "dry-run", "run"),
        default="wait",
        help="what to do at once, before the schedule takes over (default: wait)",
    )
    serve.set_defaults(func=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``rssignal`` console script.

    Anything that gets this far ends the command, so it is worth a traceback in
    the log even though stderr only gets the message: this is the error someone
    will come asking about tomorrow, when stderr is long gone. An unexpected
    exception is re-raised after being logged — a crash should still look like a
    crash — while the known ones exit 1 with their message.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SignalError, ConfigError, FeedError, ValueError) as exc:
        logged = log_exception(f"rssignal {args.command}", exc)
        where = f" (traceback in {logged})" if logged else ""
        print(f"error: {exc}{where}", file=sys.stderr)
        return 1
    except Exception as exc:
        log_exception(f"rssignal {args.command}", exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
