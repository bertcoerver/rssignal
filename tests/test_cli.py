"""Tests for rssignal.cli."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from rssignal import cli
from rssignal.config import ConfigError
from rssignal.feeds import FeedConfig, FeedItem, ParsedFeed
from rssignal.signal_cli import SignalGroup, SignalSendError


def test_send_success(monkeypatch, capsys):
    captured = {}

    def fake_send(message, recipient=None):
        captured["message"] = message
        captured["recipient"] = recipient

    monkeypatch.setattr(cli, "send_msg", fake_send)

    rc = cli.main(["send", "hello", "--to", "+31611111111"])

    assert rc == 0
    assert captured == {"message": "hello", "recipient": "+31611111111"}
    assert "Message sent." in capsys.readouterr().out


def test_send_error_returns_1(monkeypatch, capsys):
    def fake_send(message, recipient=None):
        raise SignalSendError("failed", returncode=1, stderr="bad")

    monkeypatch.setattr(cli, "send_msg", fake_send)

    rc = cli.main(["send", "hello"])

    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_doctor_reports_ok(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_signal_cli", lambda: "/bin/signal-cli")
    monkeypatch.setattr(cli, "list_accounts", lambda: ["+31600000000"])
    monkeypatch.setattr(
        cli,
        "get_config",
        lambda: SimpleNamespace(account="+31600000000", recipient="+31611111111"),
    )

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "/bin/signal-cli" in out
    assert "+31600000000" in out


def test_doctor_warns_when_account_not_linked(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_signal_cli", lambda: "/bin/signal-cli")
    monkeypatch.setattr(cli, "list_accounts", lambda: [])
    monkeypatch.setattr(
        cli,
        "get_config",
        lambda: SimpleNamespace(account="+31600000000", recipient=None),
    )

    rc = cli.main(["doctor"])

    assert rc == 1
    assert "not linked" in capsys.readouterr().out


def test_doctor_config_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_signal_cli", lambda: "/bin/signal-cli")
    monkeypatch.setattr(cli, "list_accounts", lambda: [])

    def raise_config():
        raise ConfigError("no account")

    monkeypatch.setattr(cli, "get_config", raise_config)

    rc = cli.main(["doctor"])

    assert rc == 1


def test_link_invokes_link_device(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        cli, "link_device", lambda name: captured.setdefault("name", name)
    )

    rc = cli.main(["link", "--name", "mybot"])

    assert rc == 0
    assert captured["name"] == "mybot"
    assert "Linking complete." in capsys.readouterr().out


def test_no_command_errors():
    with pytest.raises(SystemExit):
        cli.main([])


# --- fields ----------------------------------------------------------------

_ITEM = FeedItem(
    title="Episode 402",
    description="Show notes",
    link="https://a/402",
    published=datetime(2026, 7, 22, 6, 0, 0, tzinfo=timezone.utc),
    enclosure_url="https://a/402.mp3",
    enclosure_type="audio/mpeg",
    author="Example Media",
    categories=("news",),
    feed_name="Pod",
    extra={"itunes_duration": "00:42:11"},
)

_CONFIGS = [
    FeedConfig(url="https://a/rss", type="podcast", name="Pod"),
    FeedConfig(url="https://b/rss", type="regular", name="Blog"),
]


@pytest.fixture
def patched_feeds(monkeypatch):
    monkeypatch.setattr(cli, "load_feeds", lambda path: _CONFIGS)
    monkeypatch.setattr(cli, "parse_feed", lambda cfg: ParsedFeed([_ITEM, _ITEM]))


def test_fields_lists_core_and_extra_fields(patched_feeds, capsys):
    rc = cli.main(["fields"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Pod" in out  # defaults to the first configured feed
    assert "itunes_duration" in out
    assert "(extra)" in out
    assert "published_date" in out
    assert "2026-07-22" in out


def test_fields_selects_feed_by_name(patched_feeds, capsys):
    assert cli.main(["fields", "--feed", "blog"]) == 0
    assert "Blog" in capsys.readouterr().out


def test_fields_unknown_feed_errors(patched_feeds, capsys):
    assert cli.main(["fields", "--feed", "nope"]) == 1
    assert "No feed matching" in capsys.readouterr().err


def test_fields_url_bypasses_the_config(monkeypatch, capsys):
    def fail(path):
        raise AssertionError("--url should not read the config")

    monkeypatch.setattr(cli, "load_feeds", fail)
    monkeypatch.setattr(cli, "parse_feed", lambda cfg: ParsedFeed([_ITEM]))

    assert cli.main(["fields", "--url", "https://c/rss"]) == 0
    assert "https://c/rss" in capsys.readouterr().out


def test_fields_item_out_of_range_errors(patched_feeds, capsys):
    assert cli.main(["fields", "--item", "9"]) == 1
    assert "out of range" in capsys.readouterr().err


def test_fields_renders_template_preview(patched_feeds, capsys):
    rc = cli.main(["fields", "--template", "{title} - {itunes_duration}"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "rendered message" in out
    assert "Episode 402 - 00:42:11" in out


# --- groups ----------------------------------------------------------------

_GROUPS = [
    SignalGroup(id="aaa=", name="Book club", active=True, blocked=False),
    SignalGroup(id="bbb=", name="Old crew", active=False, blocked=False),
    SignalGroup(id="ccc=", name="Spam group", active=True, blocked=True),
]


@pytest.fixture
def patched_groups(monkeypatch):
    monkeypatch.setattr(cli, "list_groups", lambda: _GROUPS)
    # Guard: listing must not touch the network unless --refresh asks it to.
    monkeypatch.setattr(
        cli,
        "receive",
        lambda: pytest.fail("groups should not receive without --refresh"),
    )


def test_groups_lists_active_groups(patched_groups, capsys):
    rc = cli.main(["groups"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Book club" in out
    assert "group:aaa=" in out
    # Left and blocked groups are hidden unless asked for.
    assert "Old crew" not in out
    assert "Spam group" not in out


def test_groups_all_includes_inactive_and_blocked(patched_groups, capsys):
    rc = cli.main(["groups", "--all"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Old crew" in out and "(inactive)" in out
    assert "Spam group" in out and "(blocked)" in out


def test_groups_quiet_prints_only_recipients(patched_groups, capsys):
    rc = cli.main(["groups", "--quiet"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "group:aaa="


def test_groups_hints_at_refresh(patched_groups, capsys):
    cli.main(["groups"])
    assert "--refresh" in capsys.readouterr().out


def test_groups_quiet_output_stays_pipeable(patched_groups, capsys):
    # The hint would corrupt the output if it leaked into --quiet.
    cli.main(["groups", "--quiet"])
    assert "--refresh" not in capsys.readouterr().out


def test_groups_refresh_receives_first(monkeypatch, capsys):
    order = []
    monkeypatch.setattr(cli, "receive", lambda: order.append("receive"))
    monkeypatch.setattr(
        cli, "list_groups", lambda: (order.append("list"), _GROUPS)[1]
    )

    rc = cli.main(["groups", "--refresh"])

    assert rc == 0
    assert order == ["receive", "list"]
    # Already refreshed, so the hint would be noise.
    assert "--refresh" not in capsys.readouterr().out


def test_groups_none_found_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(cli, "list_groups", list)
    monkeypatch.setattr(cli, "receive", lambda: None)

    assert cli.main(["groups"]) == 1
    out = capsys.readouterr().out
    assert "No groups found" in out
    assert "--refresh" in out


def test_groups_signal_error_returns_1(monkeypatch, capsys):
    def boom():
        raise SignalSendError("listGroups failed", returncode=1, stderr="x")

    monkeypatch.setattr(cli, "list_groups", boom)

    assert cli.main(["groups"]) == 1
    assert "error:" in capsys.readouterr().err


# --- create-group ----------------------------------------------------------


@pytest.fixture
def patched_create(monkeypatch):
    calls = {}

    def fake_create(name, *, description=None, avatar=None, announcement_only=False):
        calls.update(
            name=name,
            description=description,
            avatar=avatar,
            announcement_only=announcement_only,
        )
        return SignalGroup(id="new=", name=name)

    monkeypatch.setattr(cli, "create_group", fake_create)
    return calls


def test_create_group_prints_the_recipient(patched_create, capsys):
    rc = cli.main(["create-group", "Feed group"])

    out = capsys.readouterr().out
    assert rc == 0
    assert patched_create["name"] == "Feed group"
    assert "group:new=" in out
    assert "only you" in out


def test_create_group_passes_options_through(patched_create):
    rc = cli.main(
        [
            "create-group",
            "Pod",
            "--description",
            "episodes",
            "--avatar",
            "art.jpg",
            "--announcement",
        ]
    )

    assert rc == 0
    assert patched_create == {
        "name": "Pod",
        "description": "episodes",
        "avatar": "art.jpg",
        "announcement_only": True,
    }


def test_create_group_quiet_output_stays_pipeable(patched_create, capsys):
    rc = cli.main(["create-group", "Pod", "--quiet"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "group:new="


def test_create_group_has_no_way_to_add_members(capsys):
    # A --member flag must not exist: adding someone to a group is not
    # something rssignal is allowed to do on anyone's behalf.
    with pytest.raises(SystemExit):
        cli.main(["create-group", "Pod", "--member", "+31611111111"])
    assert "unrecognized arguments" in capsys.readouterr().err


def test_create_group_error_returns_1(monkeypatch, capsys):
    def boom(name, **kwargs):
        raise SignalSendError("updateGroup failed", returncode=1, stderr="x")

    monkeypatch.setattr(cli, "create_group", boom)

    assert cli.main(["create-group", "Pod"]) == 1
    assert "error:" in capsys.readouterr().err


def test_run_to_is_threaded_through(monkeypatch):
    captured = {}

    def fake_run_feeds(config, *, dry_run=False, to=None):
        captured.update(config=config, dry_run=dry_run, to=to)
        return 0

    monkeypatch.setattr(cli, "run_feeds", fake_run_feeds)

    assert cli.main(["run", "--to", "+31611111111"]) == 0
    assert captured == {
        "config": "feeds.json",
        "dry_run": False,
        "to": "+31611111111",
    }
