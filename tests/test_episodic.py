"""Tests for episodic feeds: the cadence, the config keys, and a paced run.

The YouTube archive that makes an episodic YouTube feed possible is tested in
test_youtube.py, next to the rest of the yt-dlp plumbing it belongs to.
"""

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from rssignal import run
from rssignal.episodic import Cadence, allowance, next_release, parse_cadence
from rssignal.feeds import FeedConfig, FeedError, FeedItem, load_feeds
from rssignal.run import run_feeds
from rssignal.signal_cli import SignalGroup
from rssignal.watermark import format_pace, format_watermark, read_pace, read_watermark

from .test_run import _capture_sends, _patch_groups, _patch_video

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


# --- the cadence grammar ---------------------------------------------------


@pytest.mark.parametrize(
    "value, count, days",
    [
        ("daily", 1, 1),
        ("weekly", 1, 7),
        ("monthly", 1, 30),
        ("2-weekly", 2, 7),
        ("3-daily", 3, 1),
        ("10-monthly", 10, 30),
        ("2-WEEKLY", 2, 7),
        ("  weekly  ", 1, 7),
    ],
)
def test_parse_cadence_reads_the_forms(value, count, days):
    cadence = parse_cadence(value, "feeds[0]")
    assert cadence.count == count
    assert cadence.period == timedelta(days=days)


def test_two_weekly_is_one_every_three_and_a_half_days():
    """The rate is applied as an interval — a series, not a weekly dump."""
    assert parse_cadence("2-weekly", "feeds[0]").interval == timedelta(days=3.5)


def test_true_is_episodic_but_unpaced():
    cadence = parse_cadence(True, "feeds[0]")
    assert cadence.interval is None
    assert allowance(cadence, NOW, NOW) > 1


@pytest.mark.parametrize(
    "value",
    ["2 weekly", "fortnightly", "weekly-2", "2-yearly", "", "0-weekly", 7, None, []],
)
def test_parse_cadence_rejects_the_rest(value):
    with pytest.raises(ValueError, match="feeds\\[0\\] episodic"):
        parse_cadence(value, "feeds[0]")


# --- what the cadence permits ----------------------------------------------


def test_allowance_lets_the_first_episode_straight_through():
    """Nothing released yet means nothing to wait for — that starts the clock."""
    assert allowance(parse_cadence("weekly", "f"), None, NOW) == 1


def test_allowance_holds_until_the_interval_has_passed():
    cadence = parse_cadence("2-weekly", "f")  # one every 3.5 days
    released = NOW - timedelta(days=3)

    assert allowance(cadence, released, NOW) == 0
    assert allowance(cadence, released, NOW + timedelta(hours=12)) == 1


def test_allowance_never_owes_a_backlog():
    """A month off does not buy eight episodes at once. See the module docstring."""
    cadence = parse_cadence("2-weekly", "f")
    assert allowance(cadence, NOW - timedelta(days=30), NOW) == 1


def test_allowance_lets_the_same_run_tomorrow_through():
    """Stamped seconds into last night's run, due at tonight's same run anyway.

    A strict 24 hours holds tonight's run, and if the next run that can reach the
    source is tomorrow night's, "daily" quietly turns into every other day.
    """
    cadence = parse_cadence("daily", "f")
    released = NOW - timedelta(days=1) + timedelta(seconds=8)
    assert allowance(cadence, released, NOW) == 1
    # A machine that slept and released at 04:25 still makes the 01:30 run.
    assert allowance(cadence, NOW - timedelta(hours=21, minutes=5), NOW) == 1


def test_allowance_never_releases_two_in_one_period():
    daily = parse_cadence("daily", "f")
    assert allowance(daily, NOW - timedelta(hours=19), NOW) == 0
    # Capped at a quarter of the interval, so a short cadence isn't halved.
    twice = parse_cadence("2-daily", "f")
    assert allowance(twice, NOW - timedelta(hours=8, minutes=59), NOW) == 0
    assert allowance(twice, NOW - timedelta(hours=9), NOW) == 1


def test_next_release_says_when():
    cadence = parse_cadence("2-weekly", "f")
    assert next_release(cadence, NOW) == NOW + timedelta(days=3.5, hours=-4)
    assert next_release(cadence, None) is None
    assert next_release(Cadence(), NOW) is None


def test_describe_reads_as_a_rate_and_an_interval():
    assert "one every 3.5d" in parse_cadence("2-weekly", "f").describe()
    assert parse_cadence(True, "f").describe() == "every run"


# --- the config keys -------------------------------------------------------


def _write(tmp_path, feed):
    path = tmp_path / "feeds.json"
    path.write_text(json.dumps({"feeds": [feed]}), encoding="utf-8")
    return str(path)


def test_load_feeds_reads_episodic_and_start(tmp_path):
    path = _write(
        tmp_path,
        {
            "name": "Show",
            "url": "https://a",
            "episodic": "2-weekly",
            "start": "2019-01-01",
        },
    )
    cfg = load_feeds(path)[0]

    assert cfg.episodic.interval == timedelta(days=3.5)
    assert cfg.start == datetime(2019, 1, 1, tzinfo=timezone.utc)


def test_load_feeds_reads_a_full_start_timestamp(tmp_path):
    path = _write(
        tmp_path,
        {
            "name": "Show",
            "url": "https://a",
            "episodic": True,
            "start": "2019-06-01T09:30:00+02:00",
        },
    )
    assert load_feeds(path)[0].start.hour == 9


def test_a_feed_without_episodic_has_neither(tmp_path):
    cfg = load_feeds(_write(tmp_path, {"name": "Blog", "url": "https://a"}))[0]
    assert cfg.episodic is None and cfg.start is None


def test_start_without_episodic_is_an_error(tmp_path):
    path = _write(tmp_path, {"name": "Blog", "url": "https://a", "start": "2019-01-01"})
    with pytest.raises(FeedError, match="not \"episodic\""):
        load_feeds(path)


def test_a_bad_cadence_names_the_feed_and_the_value(tmp_path):
    path = _write(tmp_path, {"name": "Show", "url": "https://a", "episodic": "often"})
    with pytest.raises(FeedError, match="feeds\\[0\\] episodic .*'often'"):
        load_feeds(path)


def test_a_bad_start_says_what_it_wanted(tmp_path):
    path = _write(
        tmp_path,
        {"name": "S", "url": "https://a", "episodic": True, "start": "last summer"},
    )
    with pytest.raises(FeedError, match="2019-01-01"):
        load_feeds(path)


# --- a paced run -----------------------------------------------------------

WEEKLY = parse_cadence("weekly", "f")

# Four episodes, oldest first, spread over four years.
EPISODES = [
    FeedItem(
        title=f"Episode {n}",
        description=f"d{n}",
        link=f"https://a/{n}",
        published=datetime(2014 + n, 5, 4, tzinfo=timezone.utc),
    )
    for n in range(1, 5)
]


def _patch(monkeypatch, cfg, items, groups):
    """Wire up one feed whose parse is fixed and whose group is given."""
    monkeypatch.setattr(run, "load_feeds", lambda path: [cfg])
    monkeypatch.setattr(
        run,
        "parse_feed",
        lambda c: dataclasses.replace(
            run.ParsedFeed(items=list(items)), description="blurb"
        ),
    )
    monkeypatch.setattr(run, "_now", lambda: NOW)
    return _patch_groups(monkeypatch, groups)


def test_an_episodic_feed_opens_at_the_beginning(monkeypatch):
    """Not `newest`, which is what any other feed would have sent."""
    cfg = FeedConfig(url="https://a", name="Show", episodic=WEEKLY)
    group = SignalGroup(id="g=", name="Show", description="blurb")
    calls = _patch(monkeypatch, cfg, EPISODES, [group])
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 1
    assert sends[0]["text"].startswith("Episode 1")

    # Both markers go up, in one write, before the episode goes out.
    written = calls["updated"][0]["description"]
    assert read_watermark(written) == EPISODES[0].published
    assert read_pace(written) == NOW


def test_a_skipped_episode_does_not_spend_the_pace_slot(monkeypatch):
    """A Short nobody was sent is not this interval's episode.

    The archive replay hits one sooner or later, and spending the day on it
    would mean a paced feed going quiet for a release that never happened.
    """
    shorts = [
        dataclasses.replace(ep, link="https://www.youtube.com/watch?v=BFcjfKZ0BeI")
        for ep in EPISODES
    ]
    earlier = NOW - timedelta(days=8)
    cfg = FeedConfig(url="https://a", name="Show", episodic=WEEKLY)
    group = SignalGroup(
        id="g=",
        name="Show",
        description=f"blurb\n\n{format_watermark(shorts[0].published)}\n"
        f"{format_pace(earlier)}",
    )
    calls = _patch(monkeypatch, cfg, shorts, [group])
    sends = _capture_sends(monkeypatch)
    _patch_video(monkeypatch, resolve_fail=run.VideoTooShort("47s — a Short"))

    assert run_feeds("feeds.json") == 0
    assert sends == []

    # The watermark is past the Short — it is not offered again — but the clock
    # reads what it read before, so the next run may send episode 3 at once.
    written = calls["updated"][-1]["description"]
    assert read_watermark(written) == shorts[1].published
    assert read_pace(written) == earlier


def test_a_paced_feed_holds_the_next_episode_back(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Show", episodic=WEEKLY)
    group = SignalGroup(
        id="g=",
        name="Show",
        description=(
            f"blurb\n\n{format_watermark(EPISODES[0].published)}\n"
            f"{format_pace(NOW - timedelta(days=2))}"
        ),
    )
    _patch(monkeypatch, cfg, EPISODES, [group])
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 0
    assert sends == []


def test_the_next_episode_goes_once_the_interval_is_up(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Show", episodic=WEEKLY)
    group = SignalGroup(
        id="g=",
        name="Show",
        description=(
            f"blurb\n\n{format_watermark(EPISODES[0].published)}\n"
            f"{format_pace(NOW - timedelta(days=8))}"
        ),
    )
    _patch(monkeypatch, cfg, EPISODES, [group])
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 1
    assert sends[0]["text"].startswith("Episode 2")


def test_a_start_date_skips_the_years_before_it(monkeypatch):
    cfg = FeedConfig(
        url="https://a",
        name="Show",
        episodic=WEEKLY,
        start=datetime(2017, 1, 1, tzinfo=timezone.utc),
    )
    group = SignalGroup(id="g=", name="Show", description="blurb")
    _patch(monkeypatch, cfg, EPISODES, [group])
    sends = _capture_sends(monkeypatch)

    # Episodes 1 and 2 went up in 2015 and 2016, so the series opens at 3.
    assert run_feeds("feeds.json") == 1
    assert sends[0]["text"].startswith("Episode 3")


@pytest.mark.parametrize("groups", [[SignalGroup(id="g=", name="Show", description="blurb")], []])
def test_an_unpaced_episodic_feed_sends_the_lot_oldest_first(monkeypatch, groups):
    """Including on the very first run, before the group exists."""
    cfg = FeedConfig(url="https://a", name="Show", episodic=Cadence())
    _patch(monkeypatch, cfg, EPISODES, groups)
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 4
    assert [s["text"].split("\n")[0] for s in sends] == [
        f"Episode {n}" for n in range(1, 5)
    ]


def test_to_sends_the_next_episode_and_touches_no_group(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Show", episodic=WEEKLY)
    calls = _patch(monkeypatch, cfg, EPISODES, [])
    sends = _capture_sends(monkeypatch)

    assert run_feeds("feeds.json", to="+3312345678") == 1
    assert sends[0]["text"].startswith("Episode 1")
    assert calls["updated"] == [] and calls["created"] == []


def test_since_overrides_the_pace(monkeypatch):
    """An explicit replay is not rationed."""
    cfg = FeedConfig(url="https://a", name="Show", episodic=WEEKLY)
    group = SignalGroup(
        id="g=",
        name="Show",
        description=f"blurb\n\n{format_pace(NOW)}",  # released seconds ago
    )
    _patch(monkeypatch, cfg, EPISODES, [group])
    sends = _capture_sends(monkeypatch)

    count = run_feeds(
        "feeds.json", since=datetime(2015, 6, 1, tzinfo=timezone.utc)
    )

    # Three episodes at once, the moment after one was released: neither the
    # clock nor the watermark got a say.
    assert count == 3
    assert sends[0]["text"].startswith("Episode 2")


def test_a_failed_send_puts_both_markers_back(monkeypatch):
    cfg = FeedConfig(url="https://a", name="Show", episodic=WEEKLY)
    group = SignalGroup(id="g=", name="Show", description="blurb")
    calls = _patch(monkeypatch, cfg, EPISODES, [group])

    def boom(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(run, "send_msg", boom)

    assert run_feeds("feeds.json") == 0
    # Marker up, then the rollback back to the description it started with.
    assert calls["updated"][-1]["description"] == "blurb"


def test_a_paced_feed_with_nothing_due_is_not_even_fetched(monkeypatch):
    """The gate is the point: no channel listing, no dating, on most runs."""
    cfg = FeedConfig(url="https://a", name="Show", episodic=WEEKLY)
    group = SignalGroup(
        id="g=",
        name="Show",
        description=f"blurb\n\n{format_pace(NOW - timedelta(hours=1))}",
    )
    _patch_groups(monkeypatch, [group])
    monkeypatch.setattr(run, "load_feeds", lambda path: [cfg])
    monkeypatch.setattr(run, "_now", lambda: NOW)

    def fail(cfg):
        raise AssertionError("nothing is due; the feed should not be read")

    monkeypatch.setattr(run, "parse_feed", fail)
    _capture_sends(monkeypatch)

    assert run_feeds("feeds.json") == 0


def test_a_dry_run_lifts_the_gate_and_says_where_the_feed_stands(
    monkeypatch, capsys
):
    cfg = FeedConfig(url="https://a", name="Show", episodic=WEEKLY)
    group = SignalGroup(
        id="g=",
        name="Show",
        description=f"blurb\n\n{format_pace(NOW - timedelta(hours=1))}",
    )
    _patch(monkeypatch, cfg, EPISODES, [group])
    _capture_sends(monkeypatch)

    assert run_feeds("feeds.json", dry_run=True) == 0
    out = capsys.readouterr().out
    assert "4 episode(s) waiting" in out
    assert "next one due" in out
