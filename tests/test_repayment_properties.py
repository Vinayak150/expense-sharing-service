import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.database import create_session_factory
from app.core.money import paise_to_rupees
from app.models import Group, User
from app.schemas.business import CustomSplitInput, ExpenseInput
from app.schemas.settlements import SettlementInput
from app.services import expenses, groups, repayments
from app.services.settlement_planning import suggest_greedy_settlements
from tests.test_balances import read
from tests.test_repayments import reconcile

pytestmark = pytest.mark.postgres


@st.composite
def histories(draw):
    n = draw(st.integers(2, 6))
    stages = sorted(draw(st.lists(st.integers(1, n), min_size=2, max_size=6)))
    events = []
    for count in stages:
        payer = draw(st.integers(0, count - 1))
        custom = draw(st.booleans())
        if custom:
            shares = draw(st.lists(st.integers(0, 10000), min_size=count, max_size=count))
            if not sum(shares):
                shares[0] = 1
        else:
            amount = draw(st.integers(1, 10000))
            shares = [amount // count + int(i < amount % count) for i in range(count)]
        events.append((count, payer, shares, custom))
    return n, events


@settings(
    max_examples=20,
    derandomize=True,
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(histories())
def test_generated_ledger_and_full_partial_repayments(business_engine, case):
    n, events = case
    with business_engine.begin() as c:
        users = [
            c.scalar(User.__table__.insert().values(name="Duplicate").returning(User.id))
            for _ in range(n)
        ]
        g = c.scalar(Group.__table__.insert().values(name="Generated ledger").returning(Group.id))
    net = dict.fromkeys(users, 0)
    joined = 0
    factory = create_session_factory(business_engine)
    for step, (count, payer, shares, custom) in enumerate(events):
        for u in users[joined:count]:
            with factory() as s:
                groups.add_member(s, g, u)
        joined = count
        total = sum(shares)
        with factory() as s:
            record = expenses.create_expense(
                s,
                ExpenseInput(
                    group_id=g,
                    paid_by=users[payer],
                    amount=paise_to_rupees(total),
                    description="Generated",
                    splits=[
                        CustomSplitInput(user_id=users[i], amount=paise_to_rupees(v))
                        for i, v in enumerate(shares)
                    ]
                    if custom
                    else None,
                ),
            )
        assert [a.share_paise for a in record.splits] == shares
        net[users[payer]] += total
        for i, v in enumerate(shares):
            net[users[i]] -= v
        plan = suggest_greedy_settlements({u: net[u] for u in users[:joined]})
        if plan:
            t = plan[0]
            partial = max(1, t.amount_paise // 2)
            with factory() as s:
                repayments.record_settlement(
                    s,
                    g,
                    SettlementInput(
                        from_user_id=t.from_user_id,
                        to_user_id=t.to_user_id,
                        amount=paise_to_rupees(partial),
                    ),
                    f"partial-{step}",
                )
            net[t.from_user_id] += partial
            net[t.to_user_id] -= partial
        actual = read(business_engine, g)
        reconcile(actual)
        assert {m.user_id: m.net_balance_paise for m in actual.balances} == {
            u: net[u] for u in users[:joined]
        }
    for u in users[joined:]:
        with factory() as s:
            groups.add_member(s, g, u)
    plan = suggest_greedy_settlements(net)
    for i, t in enumerate(plan):
        with factory() as s:
            repayments.record_settlement(
                s,
                g,
                SettlementInput(
                    from_user_id=t.from_user_id,
                    to_user_id=t.to_user_id,
                    amount=paise_to_rupees(t.amount_paise),
                ),
                f"finish-{i}",
            )
    final = read(business_engine, g)
    reconcile(final)
    assert all(m.net_balance_paise == 0 for m in final.balances)
    if final.debt_basis == "net_after_repayments":
        assert final.debts == []
