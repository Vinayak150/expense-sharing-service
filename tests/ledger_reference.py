"""Deliberately simple independent reference: dense directed matrix, per-user scans."""


def reference(users, history):
    # history entries are (payer, {participant: persisted_share}).
    paid = {u: sum(sum(s.values()) for p, s in history if p == u) for u in users}
    shares = {u: sum(s.get(u, 0) for _, s in history) for u in users}
    raw = {
        (a, b): sum(s.get(a, 0) for p, s in history if p == b)
        for a in users
        for b in users
        if a != b
    }
    debts = [(a, b, raw[a, b] - raw[b, a]) for a, b in raw if raw[a, b] > raw[b, a]]
    return {u: (paid[u], shares[u], paid[u] - shares[u]) for u in users}, sorted(debts)


def assert_balances(result, users, history):
    expected, debts = reference(users, history)
    actual = {
        b.user_id: (b.total_paid_paise, b.total_share_paise, b.net_balance_paise)
        for b in result.balances
    }
    assert actual == expected
    assert [b.user_id for b in result.balances] == sorted(users)
    assert [(d.from_user_id, d.to_user_id, d.amount_paise) for d in result.debts] == debts
    assert sum(b.net_balance_paise for b in result.balances) == 0
    assert result.total_expenses_paise == sum(sum(s.values()) for _, s in history)
    for u in users:
        incoming = sum(d.amount_paise for d in result.debts if d.to_user_id == u)
        outgoing = sum(d.amount_paise for d in result.debts if d.from_user_id == u)
        assert incoming - outgoing == actual[u][2]


def persist_history(engine, group, history):
    from app.models import Expense, ExpenseSplit

    with engine.begin() as c:
        for payer, shares in history:
            expense = c.scalar(
                Expense.__table__.insert()
                .values(
                    group_id=group,
                    paid_by=payer,
                    amount_paise=sum(shares.values()),
                    description="Persisted reference snapshot",
                )
                .returning(Expense.id)
            )
            c.execute(
                ExpenseSplit.__table__.insert(),
                [
                    {"expense_id": expense, "user_id": u, "share_paise": v}
                    for u, v in shares.items()
                ],
            )
