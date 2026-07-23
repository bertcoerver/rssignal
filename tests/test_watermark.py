"""Tests for rssignal.watermark (pure string/datetime work — nothing shells out).

Every description here is invented. Real group descriptions carry real feed
names, and those don't belong in a tracked file.
"""

from datetime import datetime, timezone

from rssignal.watermark import (
    compose_description,
    format_watermark,
    read_watermark,
    shorten,
    strip_watermark,
)

WHEN = datetime(2026, 7, 23, 10, 3, 0, tzinfo=timezone.utc)
BLURB = "A made-up show about made-up things."


def test_compose_then_read_round_trips():
    description = compose_description(BLURB, WHEN, limit=480)
    assert read_watermark(description) == WHEN
    assert strip_watermark(description) == BLURB


def test_composed_description_keeps_the_blurb_readable():
    # The marker goes on its own line so the blurb still reads as a blurb.
    assert compose_description(BLURB, WHEN, limit=480) == (
        f"{BLURB}\n\n[rssignal 2026-07-23T10:03:00+00:00]"
    )


def test_compose_normalises_to_utc():
    from datetime import timedelta

    local = WHEN.astimezone(timezone(timedelta(hours=2)))
    assert format_watermark(local) == format_watermark(WHEN)


def test_read_watermark_of_a_plain_blurb_is_none():
    assert read_watermark(BLURB) is None
    assert read_watermark("") is None


def test_read_watermark_of_a_corrupt_marker_is_none():
    # Somebody editing the description by hand should cost one duplicate item,
    # not a crashed run.
    assert read_watermark(f"{BLURB}\n\n[rssignal yesterday]") is None


def test_read_watermark_takes_the_last_marker():
    # A blurb that quotes the marker must not beat the real one.
    decoy = "[rssignal 2001-01-01T00:00:00+00:00]"
    assert read_watermark(f"{decoy}\n\n{format_watermark(WHEN)}") == WHEN


def test_read_watermark_assumes_utc_when_the_stamp_has_no_zone():
    assert read_watermark("[rssignal 2026-07-23T10:03:00]") == WHEN


def test_strip_watermark_leaves_a_markerless_blurb_alone():
    assert strip_watermark(BLURB) == BLURB


def test_strip_watermark_of_only_a_marker_is_empty():
    assert strip_watermark(format_watermark(WHEN)) == ""


def test_compose_clips_the_blurb_but_never_the_marker():
    long_blurb = "word " * 300
    description = compose_description(long_blurb, WHEN, limit=200)

    assert len(description) <= 200
    # The whole point: the next run can still read where the feed got to.
    assert read_watermark(description) == WHEN
    assert description.endswith(format_watermark(WHEN))
    assert "…" in description


def test_compose_drops_the_blurb_entirely_when_there_is_no_room():
    description = compose_description("word " * 300, WHEN, limit=len(format_watermark(WHEN)))
    assert description == format_watermark(WHEN)


def test_compose_replaces_an_existing_marker_rather_than_stacking_them():
    first = compose_description(BLURB, WHEN, limit=480)
    later = WHEN.replace(hour=18)
    second = compose_description(first, later, limit=480)

    assert second.count("[rssignal") == 1
    assert read_watermark(second) == later


def test_shorten_leaves_short_text_alone():
    assert shorten(BLURB, 480) == BLURB


def test_shorten_cuts_on_a_word_boundary():
    assert shorten("alpha beta gamma delta", 14) == "alpha beta…"


def test_shorten_hard_cuts_a_single_long_word():
    # No space to cut on, so it has to break the word rather than return nothing.
    assert shorten("a" * 40, 10) == "a" * 9 + "…"
