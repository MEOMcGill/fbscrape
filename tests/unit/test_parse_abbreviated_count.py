"""Unit tests for _parse_abbreviated_count.

Regression coverage for a bug where an unabbreviated exact count (e.g.
"827 members") was inflated 1,000,000x — the parser's suffix regex allowed
whitespace before the K/M/B letter, so it greedily matched the leading "m"
of the trailing word "members" as if it meant "million".
"""

import pytest

from fbscrape.response import _parse_abbreviated_count


@pytest.mark.parametrize("text, expected", [
    ("121M followers", 121_000_000),
    ("22K", 22_000),
    ("1 following", 1),
    ("120.4K members", 120_400),
    # Exact, unabbreviated counts — the regression case. A space precedes
    # the trailing word, so its leading letter (m/t/f/...) must NOT be
    # mistaken for a K/M/B suffix.
    ("827 members", 827),
    ("827 total members", 827),
    ("581 total members", 581),
    ("120,445 total members", 120_445),
    ("0 members", 0),
])
def test_parses_expected_value(text, expected):
    assert _parse_abbreviated_count(text) == expected


@pytest.mark.parametrize("value", [None, "", "not a number", 42])
def test_returns_none_on_unrecognized_input(value):
    assert _parse_abbreviated_count(value) is None
