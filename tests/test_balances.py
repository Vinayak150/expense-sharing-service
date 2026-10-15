import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import event, select, text

from app.core.database import create_session_factory
from app.models import Expense, ExpenseSplit, Group, GroupMember, User
from app.schemas.balances import GroupBalances
from app.schemas.business import ExpenseInput
from app.services import balances, expenses, groups
from tests.ledger_reference import assert_balances, persist_history

pytestmark = pytest.mark.postgres


def read(engine, group):
    with create_session_factory(engine)() as session:
        return balances.calculate_group_balances(session, group)


def create(engine, group, payer, amount):
    with create_session_factory(engine)() as session:
        return expenses.create_expense(
            session,
            ExpenseInput(
                group_id=group,
                paid_by=payer,
                amount=amount,
                description="Meal",
            ),
        )


@pytest.mark.anyio
async def test_empty_and_inactive_members_and_alias(api, seed):
    empty = (await api.post("/groups", json={"name": "Empty"})).json()["id"]
    result = (await api.get(f"/groups/{empty}/balances")).json()
    assert result["balances"] == result["debts"] == []
    assert result["total_expenses_paise"] == 0 and result["total_expenses_amount"] == "0.00"
    result = await api.get(f"/groups/{seed['group']}/balances")
    assert result.status_code == 200
    value = GroupBalances.model_validate(result.json())
    assert_balances(value, seed["users"][:3], [])
    assert (await api.get("/balances", params={"group_id": seed["group"]})).json() == result.json()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/balances",
        "/balances?group_id=0",
        "/balances?group_id=no",
        "/balances?group_id=9223372036854775808",
        "/groups/0/balances",
        "/groups/no/balances",
    ],
)
async def test_balance_validation(api, path):
    assert (await api.get(path)).status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/balances?group_id=999", "/groups/999/balances"])
async def test_missing_group(api, path):
    response = await api.get(path)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "group_not_found"


@pytest.mark.anyio
async def test_dinner_and_multi_payer_acceptance(api, seed):
    g = seed["group"]
    m, r, a = seed["users"][:3]

    async def expense(payer, amount):
        response = await api.post(
            "/expenses",
            json={
                "group_id": g,
                "paid_by": payer,
                "amount": amount,
                "description": "Reference",
            },
        )
        assert response.status_code == 201
        return response.json()

    dinner = await expense(m, "1200")
    result = GroupBalances.model_validate((await api.get(f"/groups/{g}/balances")).json())
    assert_balances(result, [m, r, a], [(m, {m: 40000, r: 40000, a: 40000})])
    assert [b.net_balance_amount for b in result.balances] == ["800.00", "-400.00", "-400.00"]
    # Independent second group uses the exact Hotel/Taxi acceptance scenario.
    other = (await api.post("/groups", json={"name": "Hotel/Taxi"})).json()["id"]
    for u in [m, r, a]:
        assert (await api.post(f"/groups/{other}/members", json={"user_id": u})).status_code == 201
    for payer, amount in [(m, "900"), (r, "300")]:
        assert (
            await api.post(
                "/expenses",
                json={
                    "group_id": other,
                    "paid_by": payer,
                    "amount": amount,
                    "description": "Journey",
                },
            )
        ).status_code == 201
    multi = GroupBalances.model_validate((await api.get(f"/groups/{other}/balances")).json())
    assert [b.net_balance_paise for b in multi.balances] == [50000, -10000, -40000]
    assert [(d.from_user_id, d.to_user_id, d.amount_paise) for d in multi.debts] == [
        (r, m, 20000),
        (a, m, 30000),
        (a, r, 10000),
    ]
    assert (await api.get(f"/groups/{g}/balances")).json() == result.model_dump()
    assert (await api.get(f"/groups/{g}/expenses")).json() == [dinner]
    schema = (await api.get("/openapi.json")).json()
    for path in ["/balances", "/groups/{group_id}/balances"]:
        assert schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"].endswith("/GroupBalances")
    assert schema["paths"]["/balances"]["get"]["parameters"][0]["required"] is True


@pytest.mark.parametrize(
    "amount,payer_index", [("0.01", 2), ("0.02", 1), ("1.00", 0), ("1000000000.00", 2)]
)
def test_precision_and_zero_shares(business_engine, seed, amount, payer_index):
    e = create(business_engine, seed["group"], seed["users"][payer_index], amount)
    assert_balances(
        read(business_engine, seed["group"]),
        seed["users"][:3],
        [(e.paid_by, {s.user_id: s.share_paise for s in e.splits})],
    )


def test_single_member_self_share(business_engine, seed):
    with business_engine.begin() as c:
        g = c.scalar(Group.__table__.insert().values(name="Solo").returning(Group.id))
        c.execute(GroupMember.__table__.insert().values(group_id=g, user_id=seed["payer"]))
    create(business_engine, g, seed["payer"], "123.45")
    result = read(business_engine, g)
    assert result.balances[0].net_balance_paise == 0 and result.debts == []
    assert result.balances[0].total_share_paise == 12345


def test_membership_snapshots_and_duplicate_names(business_engine, seed):
    g, users = seed["group"], seed["users"]
    first = create(business_engine, g, users[0], "1200")
    with create_session_factory(business_engine)() as session:
        groups.add_member(session, g, users[3])
    with business_engine.begin() as c:
        c.execute(User.__table__.update().values(name="Same display name"))
    result = read(business_engine, g)
    assert_balances(result, users, [(users[0], {s.user_id: s.share_paise for s in first.splits})])
    assert result.balances[3].net_balance_paise == 0
    later = create(business_engine, g, users[3], "100")
    assert_balances(
        read(business_engine, g),
        users,
        [
            (first.paid_by, {s.user_id: s.share_paise for s in first.splits}),
            (later.paid_by, {s.user_id: s.share_paise for s in later.splits}),
        ],
    )
    with business_engine.connect() as c:
        old = c.execute(
            select(ExpenseSplit.user_id, ExpenseSplit.share_paise).where(
                ExpenseSplit.expense_id == first.id
            )
        ).all()
        assert sorted(old) == [(u, 40000) for u in users[:3]]


@pytest.mark.parametrize(
    "kind", ["repeat", "cancel", "cycle", "disconnected", "many_positions", "long", "above_bigint"]
)
def test_persisted_ledger_scenarios(business_engine, seed, kind):
    users = seed["users"][:3]
    if kind in ("disconnected", "many_positions"):
        with business_engine.begin() as c:
            for _ in range(5):
                u = c.scalar(User.__table__.insert().values(name="Same").returning(User.id))
                c.execute(GroupMember.__table__.insert().values(group_id=seed["group"], user_id=u))
                users.append(u)
    a, b, c = users[:3]
    histories = {
        "repeat": [(a, {a: 1, b: 2, c: 0})] * 7,
        "cancel": [(a, {a: 1, b: 19, c: 0}), (b, {a: 19, b: 1, c: 0})],
        "cycle": [(a, {a: 0, b: 10, c: 0}), (b, {a: 0, b: 0, c: 10}), (c, {a: 10, b: 0, c: 0})],
        "long": [(users[i % 3], {a: 1, b: 0, c: 0}) for i in range(400)],
        # Two individually valid BIGINT rows produce NUMERIC sums beyond BIGINT.
        "above_bigint": [(a, {a: 0, b: 2**63 - 1, c: 0})] * 2,
    }
    if kind == "disconnected":
        history = [(a, {a: 0, b: 10}), (users[2], {users[2]: 0, users[3]: 12})]
    elif kind == "many_positions":
        history = [(u, {u: 0, users[i + 3]: 10 + i}) for i, u in enumerate(users[:3])]
    else:
        history = histories[kind]
    if kind in ("disconnected", "many_positions"):
        history = [(payer, {u: shares.get(u, 0) for u in users}) for payer, shares in history]
    persist_history(business_engine, seed["group"], history)
    result = read(business_engine, seed["group"])
    assert_balances(result, users, history)
    if kind == "cycle":
        assert all(b.net_balance_paise == 0 for b in result.balances)
        assert len(result.debts) == 3
    if kind == "above_bigint":
        assert result.total_expenses_paise == 2 * (2**63 - 1)
        assert result.total_expenses_amount == "184467440737095516.14"
        assert result.balances[1].net_balance_amount == "-184467440737095516.14"
        assert GroupBalances.model_validate_json(result.model_dump_json()) == result


@pytest.mark.anyio
@pytest.mark.parametrize(
    "corruption", ["missing", "wrong_sum", "offsetting_errors", "outsider", "no_payer"]
)
async def test_corrupt_ledger_is_safe_500(api, business_engine, seed, corruption):
    g = seed["group"]
    a, b, c = seed["users"][:3]
    persist_history(business_engine, g, [(a, {a: 10, b: 10, c: 10}), (b, {a: 10, b: 10, c: 10})])
    with business_engine.begin() as connection:
        ids = connection.scalars(select(Expense.id).order_by(Expense.id)).all()
        if corruption == "missing":
            connection.execute(
                ExpenseSplit.__table__.delete().where(ExpenseSplit.expense_id == ids[0])
            )
        elif corruption in ("wrong_sum", "offsetting_errors"):
            for index, delta in enumerate([1, -1] if corruption == "offsetting_errors" else [1]):
                connection.execute(
                    Expense.__table__.update()
                    .where(Expense.id == ids[index])
                    .values(amount_paise=Expense.amount_paise + delta)
                )
        elif corruption == "outsider":
            connection.execute(
                ExpenseSplit.__table__.update()
                .where(ExpenseSplit.expense_id == ids[0], ExpenseSplit.user_id == c)
                .values(user_id=seed["outsider"])
            )
        else:
            connection.execute(
                ExpenseSplit.__table__.delete().where(
                    ExpenseSplit.expense_id == ids[0], ExpenseSplit.user_id == a
                )
            )
            connection.execute(
                ExpenseSplit.__table__.update()
                .where(ExpenseSplit.expense_id == ids[0], ExpenseSplit.user_id == b)
                .values(share_paise=19)
            )
    for path in [f"/groups/{g}/balances", f"/balances?group_id={g}"]:
        response = await api.get(path)
        assert response.status_code == 500
        assert response.json() == {
            "detail": {"code": "internal_error", "message": "Internal server error"}
        }


def test_fixed_query_count_readonly_and_connection_reset(business_engine, seed):
    history = [(seed["payer"], {u: 1 for u in seed["users"][:3]})] * 30
    persist_history(business_engine, seed["group"], history)
    statements, settings_seen = [], []

    def observe(c, cursor, statement, params, context, many):
        statements.append(statement)
        settings_seen.append(
            (
                c.connection.driver_connection.isolation_level.name,
                c.connection.driver_connection.read_only,
            )
        )

    event.listen(business_engine, "before_cursor_execute", observe)
    try:
        assert_balances(read(business_engine, seed["group"]), seed["users"][:3], history)
    finally:
        event.remove(business_engine, "before_cursor_execute", observe)
    assert len(statements) == 4
    assert set(settings_seen) == {("REPEATABLE_READ", True)}
    assert all(
        "FOR UPDATE" not in s and not s.startswith(("INSERT", "UPDATE", "DELETE"))
        for s in statements
    )
    with business_engine.connect() as c:
        assert c.scalar(text("SHOW transaction_isolation")) == "read committed"
        assert c.scalar(text("SHOW transaction_read_only")) == "off"
    create(business_engine, seed["group"], seed["payer"], "1")


@st.composite
def ledger_histories(draw):
    count = draw(st.integers(1, 7))
    stages = sorted(draw(st.lists(st.integers(1, count), min_size=1, max_size=12)))
    history = []
    for n in stages:
        amount = draw(st.integers(1, 137))
        payer = draw(st.integers(0, n - 1))
        # Independent allocation reference assigns individual paise in cyclic order.
        shares = {u: sum(i % n == u for i in range(amount)) for u in range(n)}
        history.append((payer, shares))
    return count, history


@settings(
    max_examples=35,
    derandomize=True,
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(ledger_histories())
def test_generated_persisted_snapshots(business_engine, case):
    count, generated = case
    with business_engine.begin() as c:
        users = [
            c.scalar(User.__table__.insert().values(name="Duplicate").returning(User.id))
            for _ in range(count)
        ]
        g = c.scalar(Group.__table__.insert().values(name="Generated").returning(Group.id))
        c.execute(GroupMember.__table__.insert(), [{"group_id": g, "user_id": u} for u in users])
    history = [(users[p], {users[u]: v for u, v in s.items()}) for p, s in generated]
    persist_history(business_engine, g, history)
    assert_balances(read(business_engine, g), users, history)


@pytest.mark.anyio
async def test_large_totals_keep_exact_json_and_rupee_strings(api, business_engine, seed):
    a, b, c = seed["users"][:3]
    amount = 2**63 - 1
    persist_history(business_engine, seed["group"], [(a, {a: 0, b: amount, c: 0})] * 2)
    response = await api.get(f"/groups/{seed['group']}/balances")
    assert response.status_code == 200
    value = response.json()
    assert value["total_expenses_paise"] == 2 * amount
    assert value["total_expenses_amount"] == "184467440737095516.14"
    assert value["balances"][1]["net_balance_paise"] == -2 * amount
    assert value["balances"][1]["net_balance_amount"] == "-184467440737095516.14"
    assert value["debts"][0]["amount_paise"] == 2 * amount
    assert value["debts"][0]["amount"] == "184467440737095516.14"
    assert (await api.get("/balances", params={"group_id": seed["group"]})).json() == value
    # Neither SQL SUM conversion nor formatting depends on the caller's Decimal context.
    from decimal import localcontext

    with localcontext() as context:
        context.prec = 3
        assert read(business_engine, seed["group"]).model_dump() == value


@pytest.mark.anyio
async def test_individual_reconciliation_failure_is_safe_500(
    api, business_engine, seed, monkeypatch
):
    create(business_engine, seed["group"], seed["payer"], "1200")
    monkeypatch.setattr(balances, "cancel_pairwise", lambda rows: [])
    response = await api.get(f"/groups/{seed['group']}/balances")
    assert response.status_code == 500
    assert response.json() == {
        "detail": {"code": "internal_error", "message": "Internal server error"}
    }
