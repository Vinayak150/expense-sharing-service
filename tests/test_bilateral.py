import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.bilateral import Debt, cancel_pairwise
from tests.ledger_reference import reference


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([], []),
        ([(1, 1, 8), (1, 2, 0)], []),
        ([(2, 1, 100), (2, 1, 200), (1, 2, 100)], [(2, 1, 200)]),
        ([(2, 1, 9), (1, 2, 9)], []),
        ([(1, 2, 10), (2, 3, 10), (3, 1, 10)], [(1, 2, 10), (2, 3, 10), (3, 1, 10)]),
        ([(2, 1, 4), (4, 3, 8)], [(2, 1, 4), (4, 3, 8)]),
    ],
)
def test_pairwise_contract(raw, expected):
    assert cancel_pairwise(Debt(*d) for d in reversed(raw)) == [Debt(*d) for d in expected]


@pytest.mark.parametrize(
    "debt", [Debt(0, 1, 1), Debt(1, -1, 1), Debt(1, 2, -1), Debt(1, 2, True), Debt(1, 2, 1.0)]
)
def test_invalid_obligations(debt):
    with pytest.raises(ValueError):
        cancel_pairwise([debt])


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(
    st.lists(st.tuples(st.integers(1, 8), st.integers(1, 8), st.integers(0, 10**24)), max_size=80)
)
def test_pairwise_matches_independent_matrix(raw):
    history = [(creditor, {debtor: amount}) for debtor, creditor, amount in raw]
    _, expected = reference(range(1, 9), history)
    result = cancel_pairwise(Debt(*d) for d in raw)
    assert [(d.from_user_id, d.to_user_id, d.amount_paise) for d in result] == expected
    assert result == cancel_pairwise(Debt(*d) for d in reversed(raw))
