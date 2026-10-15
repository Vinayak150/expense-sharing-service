"""Independent event-fold oracle; no production query/planner computes expected values."""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from app.core.database import create_session_factory
from app.core.money import MAX_AMOUNT_PAISE, paise_to_rupees
from app.models import Expense, ExpenseSplit, Group, GroupMember, Settlement, User
from app.schemas.business import CustomSplitInput, ExpenseInput
from app.schemas.settlements import SettlementInput
from app.services import balances, expenses, groups, repayments, settlements


def facts(engine, group):
    with engine.connect() as c:
        parents = (
            c.execute(select(Expense.__table__).where(Expense.group_id == group)).mappings().all()
        )
        ids = [e["id"] for e in parents]
        shares = (
            c.execute(select(ExpenseSplit.__table__).where(ExpenseSplit.expense_id.in_(ids)))
            .mappings()
            .all()
        )
        payments = (
            c.execute(select(Settlement.__table__).where(Settlement.group_id == group))
            .mappings()
            .all()
        )
        members = c.scalars(select(GroupMember.user_id).where(GroupMember.group_id == group)).all()
    return parents, shares, payments, members


def oracle(engine, group):
    parents, shares, payments, members = facts(engine, group)
    net = dict.fromkeys(members, 0)
    for e in parents:
        allocations = [s for s in shares if s["expense_id"] == e["id"]]
        assert sum(s["share_paise"] for s in allocations) == e["amount_paise"]
        net[e["paid_by"]] += e["amount_paise"]
        for s in allocations:
            net[s["user_id"]] -= s["share_paise"]
    for p in payments:
        net[p["from_user_id"]] += p["amount_paise"]
        net[p["to_user_id"]] -= p["amount_paise"]
    assert sum(net.values()) == 0
    return net


def check(engine, group):
    before = facts(engine, group)
    net = oracle(engine, group)
    factory = create_session_factory(engine)
    with factory() as s:
        actual = balances.calculate_group_balances(s, group)
    assert {m.user_id: m.net_balance_paise for m in actual.balances} == net
    incoming_outgoing = dict.fromkeys(net, 0)
    for debt in actual.debts:
        incoming_outgoing[debt.from_user_id] -= debt.amount_paise
        incoming_outgoing[debt.to_user_id] += debt.amount_paise
    assert incoming_outgoing == net
    with factory() as s:
        plan = settlements.suggest_group_settlements(s, group)
    remaining = net.copy()
    assert plan.suggested_transfer_count == len(plan.transfers) <= len(actual.debts)
    assert len(plan.transfers) <= max(0, sum(v != 0 for v in net.values()) - 1)
    assert plan.total_suggested_transfer_paise == sum(t.amount_paise for t in plan.transfers)
    for t in plan.transfers:
        assert type(t.amount_paise) is int and t.amount_paise > 0
        assert t.from_user_id != t.to_user_id
        assert remaining[t.from_user_id] < 0 < remaining[t.to_user_id]
        assert t.amount_paise <= min(-remaining[t.from_user_id], remaining[t.to_user_id])
        remaining[t.from_user_id] += t.amount_paise
        remaining[t.to_user_id] -= t.amount_paise
    assert not any(remaining.values())
    with factory() as s:
        assert settlements.suggest_group_settlements(s, group) == plan
    assert facts(engine, group) == before
    return net


@st.composite
def ledgers(draw):
    n = draw(st.integers(2, 6))
    rows = []
    for count in sorted(draw(st.lists(st.integers(1, n), min_size=3, max_size=8))):
        amount = draw(st.one_of(st.integers(1, 5), st.integers(1, MAX_AMOUNT_PAISE)))
        payer = draw(st.integers(0, count - 1))
        subset = draw(st.sets(st.integers(0, count - 1), min_size=1))
        custom = draw(st.booleans())
        if custom:
            participants = sorted(subset)
            cuts = sorted(
                draw(
                    st.lists(
                        st.integers(0, amount),
                        min_size=len(participants) - 1,
                        max_size=len(participants) - 1,
                    )
                )
            )
            boundaries = [0, *cuts, amount]
            allocations = dict(
                zip(
                    participants,
                    [b - a for a, b in zip(boundaries[:-1], boundaries[1:], strict=True)],
                    strict=True,
                )
            )
        else:
            allocations = {i: amount // count + (i < amount % count) for i in range(count)}
        rows.append((count, payer, amount, custom, allocations))
    return n, rows


@settings(
    max_examples=25,
    derandomize=True,
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(ledgers())
def test_independent_persisted_event_fold_with_large_tiny_and_subset_ledgers(business_engine, case):
    n, rows = case
    with business_engine.begin() as c:
        users = [
            c.scalar(User.__table__.insert().values(name="Duplicate").returning(User.id))
            for _ in range(n)
        ]
        group = c.scalar(Group.__table__.insert().values(name="Audit").returning(Group.id))
        other = c.scalar(Group.__table__.insert().values(name="Other").returning(Group.id))
    factory = create_session_factory(business_engine)
    for u in users:
        with factory() as s:
            groups.add_member(s, other, u)
    with factory() as s:
        expenses.create_expense(
            s,
            ExpenseInput(
                group_id=other, paid_by=users[-1], amount="1000000000", description="Isolation"
            ),
        )
    other_facts = facts(business_engine, other)
    joined = 0
    expected = {}
    for step, (count, payer, amount, custom, allocations) in enumerate(rows):
        for u in users[joined:count]:
            with factory() as s:
                groups.add_member(s, group, u)
            expected[u] = 0
        joined = count
        old_expenses, old_shares, old_payments, _ = facts(business_engine, group)
        with factory() as s:
            expenses.create_expense(
                s,
                ExpenseInput(
                    group_id=group,
                    paid_by=users[payer],
                    amount=paise_to_rupees(amount),
                    description="Audit",
                    splits=[
                        CustomSplitInput(user_id=users[i], amount=paise_to_rupees(v))
                        for i, v in reversed(list(allocations.items()))
                    ]
                    if custom
                    else None,
                ),
            )
        expected[users[payer]] += amount
        for i, v in allocations.items():
            expected[users[i]] -= v
        assert check(business_engine, group) == expected
        # Choose a valid partial repayment independently of production suggestions.
        negative = [u for u, v in expected.items() if v < 0]
        positive = [u for u, v in expected.items() if v > 0]
        if negative:
            sender, receiver = negative[-1], positive[0]
            paid = max(1, min(-expected[sender], expected[receiver], MAX_AMOUNT_PAISE) // 2)
            payload = SettlementInput(
                from_user_id=sender, to_user_id=receiver, amount=paise_to_rupees(paid)
            )
            with factory() as s:
                record, created = repayments.record_settlement(s, group, payload, f"partial-{step}")
            assert created
            with factory() as s:
                replay, created = repayments.record_settlement(s, group, payload, f"partial-{step}")
            assert not created and replay == record
            expected[sender] += paid
            expected[receiver] -= paid
        assert check(business_engine, group) == expected
        parents, shares, payments, _ = facts(business_engine, group)
        assert all(r in parents for r in old_expenses)
        assert all(r in shares for r in old_shares)
        assert all(r in payments for r in old_payments)
        assert facts(business_engine, other) == other_facts
        check(business_engine, other)
    while any(expected.values()):
        sender = next(u for u, v in expected.items() if v < 0)
        receiver = next(u for u, v in expected.items() if v > 0)
        paid = min(-expected[sender], expected[receiver], MAX_AMOUNT_PAISE)
        with factory() as s:
            repayments.record_settlement(
                s,
                group,
                SettlementInput(
                    from_user_id=sender, to_user_id=receiver, amount=paise_to_rupees(paid)
                ),
                f"finish-{len(facts(business_engine, group)[2])}",
            )
        expected[sender] += paid
        expected[receiver] -= paid
    assert check(business_engine, group) == expected
    if facts(business_engine, group)[2]:
        with factory() as s:
            assert balances.calculate_group_balances(s, group).debts == []
