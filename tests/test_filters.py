"""Tests for per-field filtering (rssignal.feeds.apply_filters)."""

from rssignal.feeds import FeedFilter, FeedItem, apply_filters

INTERVIEW = FeedItem(title="Episode 12: The Interview", description="A long talk")
RERUN = FeedItem(title="Episode 13: Special", description="This is a RERUN")
PLAIN = FeedItem(title="Episode 14", description="Something else")
ALL = [INTERVIEW, RERUN, PLAIN]


def _filter(field, op, *values):
    return (FeedFilter(field=field, op=op, values=values),)


def test_no_filters_passes_everything():
    assert apply_filters(ALL, ()) == ALL


def test_contains_keeps_matching_items():
    assert apply_filters(ALL, _filter("title", "contains", "Interview")) == [INTERVIEW]


def test_contains_is_any_of():
    result = apply_filters(ALL, _filter("title", "contains", "Interview", "Special"))
    assert result == [INTERVIEW, RERUN]


def test_contains_is_case_insensitive():
    assert apply_filters(ALL, _filter("title", "contains", "iNtErViEw")) == [INTERVIEW]


def test_excludes_drops_matching_items():
    result = apply_filters(ALL, _filter("description", "excludes", "rerun"))
    assert result == [INTERVIEW, PLAIN]


def test_excludes_is_none_of():
    result = apply_filters(ALL, _filter("description", "excludes", "rerun", "long"))
    assert result == [PLAIN]


def test_matches_uses_regex():
    result = apply_filters(ALL, _filter("title", "matches", r"^Episode 1[23]:"))
    assert result == [INTERVIEW, RERUN]


def test_multiple_filters_are_anded():
    filters = (
        FeedFilter(field="title", op="contains", values=("Episode",)),
        FeedFilter(field="description", op="excludes", values=("rerun",)),
        FeedFilter(field="title", op="matches", values=(r"\d{2}",)),
    )
    assert apply_filters(ALL, filters) == [INTERVIEW, PLAIN]


def test_missing_field_counts_as_empty():
    # No item has an "itunes_duration", so contains drops all and excludes keeps all.
    assert apply_filters(ALL, _filter("itunes_duration", "contains", "42")) == []
    assert apply_filters(ALL, _filter("itunes_duration", "excludes", "42")) == ALL


def test_filters_can_read_extra_fields():
    long_ep = FeedItem(title="Long", description="", extra={"itunes_duration": "01:20:00"})
    short_ep = FeedItem(title="Short", description="", extra={"itunes_duration": "00:05:00"})
    result = apply_filters(
        [long_ep, short_ep], _filter("itunes_duration", "matches", r"^01:")
    )
    assert result == [long_ep]
