import pytest
from sqlalchemy import event, select

from app.models import Base, GroupMember, User
from app.services import balances
from app.services.settlement_planning import Transfer
from tests.ledger_reference import persist_history
from tests.test_balances import create
from tests.test_settlement_planning import independent_reconciliation

pytestmark = [pytest.mark.postgres, pytest.mark.anyio]


def assert_preview(preview, nets, pairwise_count):
    transfers = [
        Transfer(t["from_user_id"], t["to_user_id"], t["amount_paise"])
        for t in preview["transfers"]
    ]
    assert transfers == sorted(transfers)
    assert all(
        type(t.amount_paise) is int and t.amount_paise > 0 and t.from_user_id != t.to_user_id
        for t in transfers
    )
    assert len({(t.from_user_id, t.to_user_id) for t in transfers}) == len(transfers)
    assert all(t.from_user_id in nets and t.to_user_id in nets for t in transfers)
    assert all(v == 0 for v in independent_reconciliation(nets, transfers).values())
    assert preview["pairwise_transfer_count"] == pairwise_count
    assert preview["suggested_transfer_count"] == len(transfers) <= pairwise_count
    assert len(transfers) <= max(0, sum(n != 0 for n in nets.values()) - 1)
    assert preview["transfers_avoided"] == pairwise_count - len(transfers) >= 0
    assert preview["total_suggested_transfer_paise"] == sum(t.amount_paise for t in transfers)
    assert preview["payments_recorded"] is False and preview["currency"] == "INR"


async def preview_and_check(api, g):
    snapshot = (await api.get(f"/groups/{g}/balances")).json()
    result = await api.get(f"/groups/{g}/settlement-suggestions")
    assert result.status_code == 200, result.text
    value = result.json()
    assert_preview(
        value,
        {m["user_id"]: m["net_balance_paise"] for m in snapshot["balances"]},
        len(snapshot["debts"]),
    )
    return value


async def test_empty_inactive_and_solo(api, business_engine, seed):
    g = (await api.post("/groups", json={"name": "Empty"})).json()["id"]
    value = await preview_and_check(api, g)
    assert value["transfers"] == [] and value["total_suggested_transfer_amount"] == "0.00"
    assert value["strategy"] == "netted_greedy"
    assert (await preview_and_check(api, seed["group"]))["transfers"] == []
    await api.post(f"/groups/{g}/members", json={"user_id": seed["payer"]})
    create(business_engine, g, seed["payer"], "123.45")
    assert (await preview_and_check(api, g))["transfers"] == []


@pytest.mark.parametrize(
    "value,status",
    [("999", 404), ("0", 422), ("-1", 422), ("no", 422), ("9223372036854775808", 422)],
)
async def test_unknown_and_invalid_group(api, value, status):
    r = await api.get(f"/groups/{value}/settlement-suggestions")
    assert r.status_code == status
    if status == 404:
        assert r.json()["detail"]["code"] == "group_not_found"


@pytest.mark.parametrize(
    "scenario", ["dinner", "hotel_taxi", "circular", "repeated", "tiny", "large"]
)
async def test_reference_scenarios(api, business_engine, seed, scenario):
    g = seed["group"]
    a, b, c = seed["users"][:3]
    plans = {
        "dinner": [(a, "1200")],
        "hotel_taxi": [(a, "900"), (b, "300")],
        "circular": [(a, "300"), (b, "300"), (c, "300")],
        "repeated": [(a, "100")] * 3 + [(b, "60")],
        "tiny": [(b, "0.01")],
        "large": [(a, "1000000000.00")] * 3,
    }
    for payer, amount in plans[scenario]:
        create(business_engine, g, payer, amount)
    before = (await api.get(f"/groups/{g}/balances")).json()
    result = await preview_and_check(api, g)
    if scenario == "dinner":
        assert [
            (t["from_user_id"], t["to_user_id"], t["amount_paise"]) for t in result["transfers"]
        ] == [(b, a, 40000), (c, a, 40000)]
    if scenario == "hotel_taxi":
        assert result["strategy"] == "netted_greedy"
        assert result["pairwise_transfer_count"] == 3 and result["suggested_transfer_count"] == 2
        assert (
            result["transfers_avoided"] == 1 and result["total_suggested_transfer_paise"] == 50000
        )
        assert [t["amount_paise"] for t in result["transfers"]] == [10000, 40000]
    if scenario == "circular":
        assert result["transfers"] == []
    if scenario == "tiny":
        assert result["transfers"][0]["amount"] == "0.01"
    assert await preview_and_check(api, g) == result
    assert (await api.get(f"/groups/{g}/balances")).json() == before


async def test_equal_split_pairwise_fallback(api, business_engine, seed):
    g = seed["group"]
    a, b, c, d = seed["users"]
    await api.post(f"/groups/{g}/members", json={"user_id": d})
    for payer, amount in [(a, "0.07"), (c, "0.03"), (c, "0.03"), (b, "0.02"), (b, "0.02")]:
        create(business_engine, g, payer, amount)
    result = await preview_and_check(api, g)
    assert result["strategy"] == "pairwise"
    assert result["suggested_transfer_count"] == result["pairwise_transfer_count"] == 2
    assert result["transfers_avoided"] == 0
    assert [
        (t["from_user_id"], t["to_user_id"], t["amount_paise"]) for t in result["transfers"]
    ] == [(b, c, 2), (d, a, 1)]


async def test_duplicates_isolation_and_historical_membership(api, business_engine, seed):
    g = seed["group"]
    a, b, c, d = seed["users"]
    original = create(business_engine, g, a, "1200")
    before = await preview_and_check(api, g)
    await api.post(f"/groups/{g}/members", json={"user_id": d})
    with business_engine.begin() as connection:
        connection.execute(User.__table__.update().values(name="Same"))
    joined = await preview_and_check(api, g)
    assert [
        (t["from_user_id"], t["to_user_id"], t["amount_paise"]) for t in joined["transfers"]
    ] == [(b, a, 40000), (c, a, 40000)]
    create(business_engine, g, d, "100")
    after = await preview_and_check(api, g)
    assert any(d in (t["from_user_id"], t["to_user_id"]) for t in after["transfers"])
    history = (await api.get(f"/groups/{g}/expenses")).json()
    assert [(s["user_id"], s["share_paise"]) for s in history[-1]["splits"]] == [
        (s.user_id, s.share_paise) for s in original.splits
    ]
    other = (await api.post("/groups", json={"name": "Isolated"})).json()["id"]
    for u in [b, d]:
        await api.post(f"/groups/{other}/members", json={"user_id": u})
    create(business_engine, other, d, "15")
    assert (await preview_and_check(api, other))["transfers"][0]["amount_paise"] == 750
    assert await preview_and_check(api, g) == after
    assert before["transfers"][0]["from_user_id"] == b


async def test_large_aggregate_json_precision(api, business_engine, seed):
    a, b, c = seed["users"][:3]
    persist_history(business_engine, seed["group"], [(a, {a: 0, b: 2**63 - 1, c: 0})] * 2)
    result = await preview_and_check(api, seed["group"])
    assert result["total_suggested_transfer_paise"] == 2 * (2**63 - 1)
    assert result["total_suggested_transfer_amount"] == "184467440737095516.14"
    assert result["transfers"][0]["amount"] == "184467440737095516.14"


async def test_openapi_contract(api):
    schema = (await api.get("/openapi.json")).json()
    route = schema["paths"]["/groups/{group_id}/settlement-suggestions"]
    assert set(route) == {"get"}
    assert "not recorded repayments" in route["get"]["description"]
    response = schema["components"]["schemas"]["SettlementSuggestions"]["properties"]
    assert response["strategy"]["enum"] == ["netted_greedy", "pairwise", "component_netting"]
    assert response["payments_recorded"]["const"] is False
    assert response["total_suggested_transfer_paise"]["type"] == "integer"
    assert response["total_suggested_transfer_amount"]["type"] == "string"


def database_rows(engine):
    with engine.connect() as c:
        return {
            t.name: c.execute(select(t).order_by(*t.primary_key.columns)).all()
            for t in Base.metadata.sorted_tables
        }


@pytest.mark.parametrize("members,history_size", [(3, 1), (40, 120), (60, 400)])
async def test_one_snapshot_four_reads_no_database_changes(
    api,
    business_engine,
    seed,
    monkeypatch,
    members,
    history_size,
):
    users = seed["users"][:3]
    with business_engine.begin() as c:
        for _ in range(members - 3):
            u = c.scalar(User.__table__.insert().values(name="Zero").returning(User.id))
            c.execute(GroupMember.__table__.insert().values(group_id=seed["group"], user_id=u))
            users.append(u)
    # Assignment-scale history with many zero-share, zero-net members.
    persist_history(
        business_engine,
        seed["group"],
        [(users[i % 3], {u: int(u == users[0]) for u in users}) for i in range(history_size)],
    )
    before = database_rows(business_engine)
    calls = []
    original = balances.calculate_group_balances

    def once(session, g):
        calls.append(g)
        return original(session, g)

    monkeypatch.setattr(balances, "calculate_group_balances", once)
    engine = api._transport.app.state.session_factory.kw["bind"]
    statements = []

    def observe(c, cursor, statement, params, context, many):
        statements.append(statement)
        assert c.connection.driver_connection.read_only is True
        assert c.connection.driver_connection.isolation_level.name == "REPEATABLE_READ"

    event.listen(engine, "before_cursor_execute", observe)
    try:
        result = await api.get(f"/groups/{seed['group']}/settlement-suggestions")
        assert result.status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert calls == [seed["group"]]
    assert len(statements) == 4
    assert all(s.startswith(("SELECT", "WITH")) and "FOR UPDATE" not in s for s in statements)
    assert database_rows(business_engine) == before


@pytest.mark.parametrize(
    "fault", ["missing_splits", "invalid_pairwise", "invalid_greedy", "bad_net"]
)
async def test_integrity_faults_return_safe_500(api, business_engine, seed, monkeypatch, fault):
    from app.models import ExpenseSplit
    from app.services import settlement_planning

    g = seed["group"]
    create(business_engine, g, seed["payer"], "1200")
    if fault == "missing_splits":
        with business_engine.begin() as c:
            c.execute(ExpenseSplit.__table__.delete())
    elif fault == "invalid_greedy":
        monkeypatch.setattr(settlement_planning, "suggest_greedy_settlements", lambda nets: ())
    else:
        original = balances.calculate_group_balances

        def damaged(session, g):
            snapshot = original(session, g)
            if fault == "invalid_pairwise":
                snapshot.debts = []
            else:
                snapshot.balances[0].net_balance_paise += 1
            return snapshot

        monkeypatch.setattr(balances, "calculate_group_balances", damaged)
    result = await api.get(f"/groups/{g}/settlement-suggestions")
    assert result.status_code == 500
    assert result.json() == {
        "detail": {"code": "internal_error", "message": "Internal server error"}
    }
