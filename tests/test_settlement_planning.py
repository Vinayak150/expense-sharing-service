from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.errors import AccountingIntegrityError
from app.services.settlement_planning import (
    Transfer,
    select_plan,
    suggest_greedy_settlements,
    validate_plan,
)


def independent_reconciliation(nets, transfers):
    # Independent per-user scans of immutable transfer rows, not the production replay.
    return {
        u: net
        + sum(t.amount_paise for t in transfers if t.from_user_id == u)
        - sum(t.amount_paise for t in transfers if t.to_user_id == u)
        for u, net in nets.items()
    }


@pytest.mark.parametrize(
    "nets,expected",
    [
        ({}, ()),
        ({1: 0, 2: 0}, ()),
        ({1: 80000, 2: -40000, 3: -40000}, (Transfer(2, 1, 40000), Transfer(3, 1, 40000))),
        ({1: 50000, 2: -10000, 3: -40000}, (Transfer(2, 1, 10000), Transfer(3, 1, 40000))),
        ({1: -1, 2: 1}, (Transfer(1, 2, 1),)),
        ({1: 2**80, 2: -(2**80)}, (Transfer(2, 1, 2**80),)),
        ({1: 2, 2: -3, 3: 1, 4: 0}, (Transfer(2, 1, 2), Transfer(2, 3, 1))),
    ],
)
def test_greedy_reference_examples(nets, expected):
    before = nets.copy()
    assert suggest_greedy_settlements(nets) == expected
    assert nets == before
    assert all(v == 0 for v in independent_reconciliation(nets, expected).values())


@pytest.mark.parametrize(
    "nets",
    [
        {1: 1},
        {1: True, 2: -1},
        {1: 1.0, 2: -1},
        {1: Decimal(1), 2: -1},
        {True: 0},
        {0: 0},
        {"1": 0},
        None,
        [0],
        {1: False},
    ],
)
def test_invalid_net_inputs(nets):
    with pytest.raises(AccountingIntegrityError):
        suggest_greedy_settlements(nets)
    with pytest.raises(AccountingIntegrityError):
        validate_plan(nets, [])


@pytest.mark.parametrize(
    "transfers",
    [
        (),
        (Transfer(1, 2, 1),),
        (Transfer(2, 1, 0),),
        (Transfer(2, 1, -1),),
        (Transfer(2, 1, True),),
        (Transfer(2, 1, 1.0),),
        (Transfer(2, 1, Decimal(1)),),
        (Transfer(2, 2, 1),),
        (Transfer(3, 1, 1),),
        (Transfer(True, 2, 1),),
        (Transfer(2, "1", 1),),
        (Transfer(2, 1, 2),),
        (Transfer(2, 1, 1), Transfer(2, 1, 1)),
        ((2, 1, 1),),
        None,
        {},
        "invalid",
    ],
)
def test_invalid_or_incomplete_plan(transfers):
    with pytest.raises(AccountingIntegrityError):
        validate_plan({1: 1, 2: -1, 4: 0}, transfers)


def test_duplicate_directed_entries_rejected_even_when_they_clear():
    with pytest.raises(AccountingIntegrityError):
        validate_plan({1: 2, 2: -2}, [Transfer(2, 1, 1), Transfer(2, 1, 1)])


def test_validator_includes_zero_members_and_allows_intermediaries():
    validate_plan({1: 1, 2: 0, 3: -1}, [Transfer(3, 2, 1), Transfer(2, 1, 1)])
    validate_plan({1: 0, 2: 0, 3: 0}, [Transfer(1, 2, 1), Transfer(2, 3, 1), Transfer(3, 1, 1)])
    with pytest.raises(AccountingIntegrityError):
        validate_plan({1: 1, 2: 0, 3: -1}, [Transfer(3, 2, 1)])


def test_real_pairwise_fallback_and_complete_tie():
    nets = {1: 1, 2: -2, 3: 2, 4: -1}
    pairwise = [Transfer(2, 3, 2), Transfer(4, 1, 1)]
    greedy = suggest_greedy_settlements(nets)
    assert len(greedy) == 3
    plan = select_plan(nets, pairwise)
    assert plan.strategy == "pairwise" and plan.transfers == tuple(pairwise)
    validate_plan(nets, greedy)
    validate_plan(nets, plan.transfers)
    assert select_plan({1: 1, 2: -1}, [Transfer(2, 1, 1)]).strategy == "netted_greedy"
    assert select_plan({1: 0}, []).strategy == "netted_greedy"


def test_equal_count_prefers_lower_total_movement():
    nets = {1: 2, 2: 1, 3: -1, 4: -2}
    pairwise = [Transfer(3, 4, 1), Transfer(4, 1, 3), Transfer(1, 2, 1)]
    greedy = suggest_greedy_settlements(nets)
    validate_plan(nets, pairwise)
    assert len(greedy) == len(pairwise) == 3
    assert sum(t.amount_paise for t in greedy) == 3
    assert sum(t.amount_paise for t in pairwise) == 5
    assert select_plan(nets, pairwise).strategy == "netted_greedy"


def test_lexicographic_tiebreak_can_choose_either_strategy():
    nets = {1: 2, 2: 2, 3: -1, 4: -3}
    pairwise = [Transfer(3, 2, 1), Transfer(4, 1, 2), Transfer(4, 2, 1)]
    plan = select_plan(nets, pairwise)
    assert plan.strategy == "netted_greedy"
    assert plan.transfers == (Transfer(3, 1, 1), Transfer(4, 1, 1), Transfer(4, 2, 2))
    nets = {1: 2, 2: 2, 3: -3, 4: -1}
    pairwise = [Transfer(3, 1, 1), Transfer(3, 2, 2), Transfer(4, 1, 1)]
    assert len(pairwise) == len(suggest_greedy_settlements(nets)) == 3
    assert sum(t.amount_paise for t in pairwise) == 4
    plan = select_plan(nets, pairwise)
    assert plan.strategy == "pairwise" and plan.transfers == tuple(pairwise)


def test_zero_net_cycle_needs_no_suggested_payments():
    nets = {1: 0, 2: 0, 3: 0}
    pairwise = [Transfer(1, 2, 1), Transfer(2, 3, 1), Transfer(3, 1, 1)]
    plan = select_plan(nets, pairwise)
    assert plan.strategy == "netted_greedy" and plan.transfers == ()


def test_both_candidates_must_be_valid(monkeypatch):
    from app.services import settlement_planning

    with pytest.raises(AccountingIntegrityError):
        select_plan({1: 1, 2: -1}, [])
    monkeypatch.setattr(settlement_planning, "suggest_greedy_settlements", lambda nets: ())
    with pytest.raises(AccountingIntegrityError):
        select_plan({1: 1, 2: -1}, [Transfer(2, 1, 1)])


def test_counterexample_to_global_transfer_minimum():
    nets = {1: 5, 2: 4, 3: -4, 4: -5}
    greedy = suggest_greedy_settlements(nets)
    plan = select_plan(nets, greedy)
    assert len(plan.transfers) == 3
    exact = (Transfer(3, 2, 4), Transfer(4, 1, 5))
    validate_plan(nets, exact)
    assert all(v == 0 for v in independent_reconciliation(nets, exact).values())
    # Two distinct positive creditors require at least two incoming transfers; certificate=2.
    assert len(exact) == 2


@st.composite
def valid_inputs(draw):
    count = draw(st.integers(2, 15))
    ids = draw(st.lists(st.integers(1, 10000), min_size=count, max_size=count, unique=True))
    raw = draw(
        st.lists(
            st.tuples(st.integers(0, count - 1), st.integers(0, count - 1), st.integers(1, 10**12)),
            max_size=40,
        )
    )
    # Construct valid financial facts independently, combining each directed pair.
    obligations = {}
    for a, b, amount in raw:
        if a != b:
            pair = (ids[a], ids[b])
            obligations[pair] = obligations.get(pair, 0) + amount
    pairs = [
        Transfer(a, b, v - obligations.get((b, a), 0))
        for (a, b), v in obligations.items()
        if v > obligations.get((b, a), 0)
    ]
    nets = {
        u: sum(t.amount_paise for t in pairs if t.to_user_id == u)
        - sum(t.amount_paise for t in pairs if t.from_user_id == u)
        for u in ids
    }
    return nets, pairs


@settings(max_examples=300, derandomize=True, database=None, deadline=None)
@given(valid_inputs())
def test_generated_candidates_clear_every_member(case):
    nets, pairwise = case
    before = nets.copy(), pairwise.copy()
    greedy = suggest_greedy_settlements(nets)
    chosen = select_plan(nets, pairwise)
    n = sum(v != 0 for v in nets.values())
    assert len(greedy) <= max(0, n - 1)
    assert len(chosen.transfers) <= len(pairwise)
    assert len(chosen.transfers) <= max(0, n - 1)
    assert all(t.amount_paise > 0 and type(t.amount_paise) is int for t in chosen.transfers)
    assert all(
        t.from_user_id != t.to_user_id and t.from_user_id in nets and t.to_user_id in nets
        for t in chosen.transfers
    )
    assert all(v == 0 for v in independent_reconciliation(nets, chosen.transfers).values())
    assert chosen == select_plan(dict(reversed(list(nets.items()))), list(reversed(pairwise)))
    assert nets == before[0] and pairwise == before[1]

    def score(ts):
        return (len(ts), sum(t.amount_paise for t in ts), tuple(sorted(ts)))

    assert score(chosen.transfers) == min(score(pairwise), score(greedy))


def test_intermediary_plan_becomes_recordable_without_extra_transfers():
    from app.services.settlement_planning import select_recordable_plan

    nets = {1: 4, 2: 5, 3: 6, 4: -10, 5: -5}
    pairwise = [Transfer(4, 3, 10), Transfer(3, 1, 4), Transfer(5, 2, 5)]
    assert select_plan(nets, pairwise).strategy == "pairwise"
    assert len(suggest_greedy_settlements(nets)) == 4
    chosen = select_recordable_plan(nets, pairwise)
    assert chosen.strategy == "component_netting" and len(chosen.transfers) == 3
    assert chosen.transfers == (Transfer(4, 1, 4), Transfer(4, 3, 6), Transfer(5, 2, 5))
    assert all(v == 0 for v in independent_reconciliation(nets, chosen.transfers).values())


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(valid_inputs())
def test_recordable_candidates_retain_bounds(case):
    from app.services.settlement_planning import select_recordable_plan

    nets, pairs = case
    selected = select_recordable_plan(nets, pairs)
    assert len(selected.transfers) <= len(pairs)
    assert len(selected.transfers) <= max(0, sum(v != 0 for v in nets.values()) - 1)
    assert all(nets[t.from_user_id] < 0 < nets[t.to_user_id] for t in selected.transfers)
    assert all(v == 0 for v in independent_reconciliation(nets, selected.transfers).values())
    assert selected == select_recordable_plan(
        dict(reversed(list(nets.items()))), list(reversed(pairs))
    )
