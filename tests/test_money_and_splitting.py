from decimal import Decimal, localcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.money import MAX_AMOUNT_PAISE, paise_to_rupees, rupees_to_paise
from app.services.splitting import equal_split


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1200, 120000),
        (Decimal("1200.50"), 120050),
        ("0.01", 1),
        ("1.2300", 123),
        ("1e2", 10000),
        ("1000000000.00", MAX_AMOUNT_PAISE),
    ],
)
def test_exact_money(value, expected):
    assert rupees_to_paise(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        "0.001",
        "1.001",
        "1000000000.01",
        "1e999999",
        "1e-999999",
        "nan",
        "NaN",
        "Infinity",
        Decimal("NaN"),
        Decimal("Infinity"),
        True,
        False,
        None,
        1.23,
        [],
        {},
        "bad",
        "",
        "1_000",
        " 1.00",
        "1" * 129,
    ],
)
def test_invalid_money(value):
    with pytest.raises(ValueError):
        rupees_to_paise(value)


def test_conversion_is_independent_of_decimal_context():
    with localcontext() as context:
        context.prec = 3
        context.Emax = 3
        assert rupees_to_paise("123456789.12") == 12345678912
        with pytest.raises(ValueError, match="fractional"):
            rupees_to_paise("123456789.120000000001")


@pytest.mark.parametrize(
    ("amount", "people", "expected"),
    [
        (120000, [3, 1, 2], {1: 40000, 2: 40000, 3: 40000}),
        (10000, [1, 2, 3], {1: 3334, 2: 3333, 3: 3333}),
        (1, [3, 2, 1], {1: 1, 2: 0, 3: 0}),
        (17, [4], {4: 17}),
        (3, [9, 2], {2: 2, 9: 1}),
    ],
)
def test_equal_split_examples(amount, people, expected):
    assert equal_split(amount, people) == expected


@pytest.mark.parametrize(
    ("amount", "people"),
    [
        (0, [1]),
        (-1, [1]),
        (True, [1]),
        (1.0, [1]),
        (1, []),
        (1, [1, 1]),
        (1, [True]),
        (1, [0]),
        (1, ["1"]),
    ],
)
def test_split_rejects_invalid_input(amount, people):
    with pytest.raises(ValueError):
        equal_split(amount, people)


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(
    st.integers(1, MAX_AMOUNT_PAISE),
    st.lists(st.integers(1, 2**63 - 1), min_size=1, max_size=30, unique=True),
)
def test_split_conservation_and_order(amount, people):
    shares = equal_split(amount, people)
    assert sum(shares.values()) == amount
    assert set(shares) == set(people)
    assert all(type(share) is int and share >= 0 for share in shares.values())
    assert max(shares.values()) - min(shares.values()) <= 1
    assert shares == equal_split(amount, reversed(people))
    assert list(shares) == sorted(people)
    assert rupees_to_paise(paise_to_rupees(amount)) == amount
