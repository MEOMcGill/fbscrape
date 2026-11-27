"""classify_account / is_group_anonymous_id: the helpers that turn endpoint
results into an account type."""

import pytest

from fbscrape.response import classify_account, is_group_anonymous_id


@pytest.mark.parametrize("delegate_page_id, page_type, expected", [
    (None, None, "personal_profile"),
    ("", "PAGE", "personal_profile"),
    ("400746616446096", "PROFILE", "professional_profile"),
    ("400746616446096", "PAGE", "classic_page"),
    ("400746616446096", None, "linked_page_unknown_type"),
])
def test_classify_account(delegate_page_id, page_type, expected):
    assert classify_account(delegate_page_id, page_type) == expected


@pytest.mark.parametrize("author_id, expected", [
    ("1063488396479041", True),   # 16 digits: alias
    ("12345678901234567", True),  # 17 digits: alias
    ("885761977581602", True),    # 15 digits, not "100": alias
    ("100018012103307", False),   # 15 digits starting "100": user
    ("61560733847965", False),    # 14 digits: modern user id
    ("1279361653", False),        # 10 digits: classic user id
    (1063488396479041, True),     # ints accepted
    (None, False),
])
def test_is_group_anonymous_id(author_id, expected):
    assert is_group_anonymous_id(author_id) == expected
