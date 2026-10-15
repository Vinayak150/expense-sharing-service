import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from app.core.config import Settings
from app.core.database import create_session_factory
from app.main import create_app
from app.schemas.settlements import SettlementInput
from app.services import repayments
from app.services.errors import InvalidSettlement
from tests.test_balances import create, read
from tests.test_settlement_api import database_rows

pytestmark = [pytest.mark.postgres, pytest.mark.anyio]


async def pay(api, seed, amount="400", key="repay-1", sender=None, receiver=None):
    return await api.post(
        f"/groups/{seed['group']}/settlements",
        headers={"Idempotency-Key": key},
        json={
            "from_user_id": sender or seed["users"][1],
            "to_user_id": receiver or seed["payer"],
            "amount": amount,
        },
    )


def reconcile(value):
    nets = {m.user_id: m.net_balance_paise for m in value.balances}
    assert sum(nets.values()) == 0
    for m in value.balances:
        assert (
            m.net_balance_paise
            == m.total_paid_paise
            - m.total_share_paise
            + m.total_settlements_sent_paise
            - m.total_settlements_received_paise
        )
        assert nets[m.user_id] == sum(
            d.amount_paise for d in value.debts if d.to_user_id == m.user_id
        ) - sum(d.amount_paise for d in value.debts if d.from_user_id == m.user_id)
    assert all(d.amount_paise > 0 and d.from_user_id != d.to_user_id for d in value.debts)


@pytest.mark.parametrize("scenario", ["dinner", "hotel_taxi", "custom_excluded"])
async def test_full_repayment_clears_every_view(api, business_engine, seed, scenario):
    a, b, c = seed["users"][:3]
    g = seed["group"]
    if scenario == "dinner":
        create(business_engine, g, a, "1200")
    elif scenario == "hotel_taxi":
        create(business_engine, g, a, "900")
        create(business_engine, g, b, "300")
    else:
        r = await api.post(
            "/expenses",
            json={
                "group_id": g,
                "paid_by": a,
                "amount": "1000",
                "description": "Subset",
                "splits": [{"user_id": b, "amount": 600}, {"user_id": c, "amount": 400}],
            },
        )
        assert r.status_code == 201
    history = (await api.get(f"/groups/{g}/expenses")).json()
    preview = (await api.get(f"/groups/{g}/settlement-suggestions")).json()
    records = []
    for i, t in enumerate(preview["transfers"]):
        r = await pay(api, seed, t["amount"], f"full-{i}", t["from_user_id"], t["to_user_id"])
        assert r.status_code == 201, r.text
        record = r.json()
        assert record["status"] == "payment_reported" and record["payment_verified"] is False
        records.append(record)
        reconcile(read(business_engine, g))
    final = read(business_engine, g)
    assert final.debt_basis == "net_after_repayments"
    assert all(m.net_balance_paise == 0 for m in final.balances) and final.debts == []
    out = (await api.get(f"/groups/{g}/settlement-suggestions")).json()
    assert out["transfers"] == [] and out["suggestions_recorded"] is False
    assert out["has_recorded_settlements"] is True and out["payments_recorded"] is False
    assert (await api.get(f"/groups/{g}/expenses")).json() == history
    assert (await api.get(f"/groups/{g}/settlements")).json() == list(reversed(records))
    rows = database_rows(business_engine)
    assert (await api.get(f"/groups/{g}/settlement-suggestions")).status_code == 200
    assert database_rows(business_engine) == rows


async def test_partial_repeated_keys_replay_restart_and_conflict(api, business_engine, seed):
    g = seed["group"]
    create(business_engine, g, seed["payer"], "1200")
    first = await pay(api, seed, "150.00", "first")
    assert first.status_code == 201
    value = read(business_engine, g)
    assert value.balances[1].net_balance_paise == -25000
    assert value.total_expenses_paise == 120000
    assert value.balances[1].total_settlements_sent_paise == 15000
    assert value.balances[0].total_settlements_received_paise == 15000
    assert (await pay(api, seed, "1.5e2", "first")).json() == first.json()
    for change in [
        ("151", None, None),
        ("150", seed["users"][2], None),
        ("150", None, seed["users"][2]),
    ]:
        assert (await pay(api, seed, change[0], "first", change[1], change[2])).status_code == 409
    second = await pay(api, seed, "150", "second")
    assert second.status_code == 201 and second.json()["id"] != first.json()["id"]
    assert read(business_engine, g).balances[1].net_balance_paise == -10000
    final = await pay(api, seed, "100", "final")
    assert final.status_code == 201
    assert (await pay(api, seed, "100.00", "final")).json() == final.json()
    app = create_app(
        Settings(
            database_url=business_engine.url.render_as_string(hide_password=False), _env_file=None
        )
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as restarted,
    ):
        assert (await pay(restarted, seed, "100", "final")).json() == final.json()
    assert len((await api.get(f"/groups/{g}/settlements")).json()) == 3


@pytest.mark.parametrize(
    "amount,sender,receiver,status",
    [
        ("0", 1, 0, 422),
        ("-1", 1, 0, 422),
        ("0.001", 1, 0, 422),
        ("1000000000.01", 1, 0, 422),
        ("401", 1, 0, 422),
        ("1", 0, 1, 422),
        ("1", 1, 2, 422),
        ("1", 1, 1, 422),
        ("1", 3, 0, 422),
        ("1", 1, 3, 422),
        ("1", 99, 0, 404),
        ("1", 1, 99, 404),
    ],
)
async def test_invalid_repayments(api, business_engine, seed, amount, sender, receiver, status):
    create(business_engine, seed["group"], seed["payer"], "1200")
    ids = seed["users"]
    r = await pay(
        api,
        seed,
        amount,
        sender=ids[sender] if sender < 4 else 999,
        receiver=ids[receiver] if receiver < 4 else 999,
    )
    assert r.status_code == status, r.text
    assert (await api.get(f"/groups/{seed['group']}/settlements")).json() == []


@pytest.mark.parametrize("key", [None, "", "bad key", "a" * 129, "unicode-₹"])
async def test_required_key(api, business_engine, seed, key):
    create(business_engine, seed["group"], seed["payer"], "1200")
    # HTTP headers are ASCII; non-ASCII case checks the service boundary below.
    if key == "unicode-₹":
        with create_session_factory(business_engine)() as s, pytest.raises(InvalidSettlement):
            repayments.record_settlement(
                s,
                seed["group"],
                SettlementInput(
                    from_user_id=seed["users"][1], to_user_id=seed["payer"], amount="1"
                ),
                key,
            )
        return
    headers = {} if key is None else {"Idempotency-Key": key}
    r = await api.post(
        f"/groups/{seed['group']}/settlements",
        headers=headers,
        json={"from_user_id": seed["users"][1], "to_user_id": seed["payer"], "amount": 1},
    )
    assert r.status_code == 422


async def test_credit_cap_unknown_groups_isolation_and_same_key(api, business_engine, seed):
    a, b, c = seed["users"][:3]
    create(business_engine, seed["group"], a, "900")
    create(business_engine, seed["group"], b, "300")
    assert (await pay(api, seed, "200", receiver=b, sender=c)).status_code == 422  # b is debtor
    other = (await api.post("/groups", json={"name": "Other"})).json()["id"]
    for u in [a, b, c]:
        await api.post(f"/groups/{other}/members", json={"user_id": u})
    create(business_engine, other, a, "1200")
    before = read(business_engine, other)
    assert (await pay(api, seed, "100", "shared")).status_code == 201
    assert read(business_engine, other) == before
    r = await api.post(
        f"/groups/{other}/settlements",
        headers={"Idempotency-Key": "shared"},
        json={"from_user_id": b, "to_user_id": a, "amount": 100},
    )
    assert r.status_code == 201
    assert (await api.get("/groups/999/settlements")).status_code == 404
    assert (
        await api.post(
            "/groups/999/settlements",
            headers={"Idempotency-Key": "x"},
            json={"from_user_id": b, "to_user_id": a, "amount": 1},
        )
    ).status_code == 404
    assert (await api.get(f"/groups/{other}/settlements")).json() == [r.json()]


@pytest.mark.parametrize("amount", ["0.01", "1000000000.00"])
async def test_tiny_large_settlement_payer_excluded(api, business_engine, seed, amount):
    r = await api.post(
        "/expenses",
        json={
            "group_id": seed["group"],
            "paid_by": seed["payer"],
            "amount": amount,
            "description": "Full debt",
            "splits": [{"user_id": seed["users"][1], "amount": amount}],
        },
    )
    assert r.status_code == 201
    paid = await pay(api, seed, amount)
    assert paid.status_code == 201
    assert read(business_engine, seed["group"]).debts == []


async def test_insert_failure_and_uniqueness_recovery(api, business_engine, seed, monkeypatch):
    create(business_engine, seed["group"], seed["payer"], "1200")
    engine = api._transport.app.state.session_factory.kw["bind"]

    def fail(c, cursor, sql, params, ctx, many):
        if sql.startswith("INSERT INTO settlements"):
            c.exec_driver_sql("SELECT 1/0")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        assert (await pay(api, seed)).status_code == 500
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert (await api.get(f"/groups/{seed['group']}/settlements")).json() == []
    first = await pay(api, seed)
    assert first.status_code == 201
    original = repayments.find_replay
    calls = []

    def miss_once(*args):
        calls.append(1)
        return None if len(calls) == 1 else original(*args)

    # Actual unique violation is reached without prior debt validation failing.
    # Use a partial original payment so a duplicate insertion could otherwise validate.
    create(business_engine, seed["group"], seed["payer"], "1200")
    monkeypatch.setattr(repayments, "find_replay", miss_once)
    replay = await pay(api, seed)
    assert replay.status_code == 200 and replay.json() == first.json() and len(calls) == 2


async def test_settlement_schema_and_unsupported_mutations(api, business_engine, seed):
    create(business_engine, seed["group"], seed["payer"], "1200")
    r = await pay(api, seed)
    assert r.status_code == 201
    assert r.json()["created_at"].endswith("Z")
    path = f"/groups/{seed['group']}/settlements"
    assert (await api.patch(path, json={"amount": 1})).status_code == 405
    assert (await api.delete(path)).status_code == 405
    schema = (await api.get("/openapi.json")).json()["paths"]["/groups/{group_id}/settlements"]
    assert set(schema) == {"get", "post"}
    assert {"200", "201", "409", "422"}.issubset(schema["post"]["responses"])
    assert any(
        p["name"] == "idempotency-key" and p["required"] for p in schema["post"]["parameters"]
    )


async def test_intermediary_preview_can_be_recorded(api, business_engine, seed):
    from app.models import GroupMember, User

    g = seed["group"]
    users = seed["users"][:3]
    with business_engine.begin() as c:
        for _ in range(2):
            u = c.scalar(User.__table__.insert().values(name="Same").returning(User.id))
            c.execute(GroupMember.__table__.insert().values(group_id=g, user_id=u))
            users.append(u)
    for payer, debtor, paise in [
        (users[2], users[3], 10),
        (users[0], users[2], 4),
        (users[1], users[4], 5),
    ]:
        r = await api.post(
            "/expenses",
            json={
                "group_id": g,
                "paid_by": payer,
                "amount": f"0.{paise:02d}",
                "description": "Intermediary",
                "splits": [{"user_id": debtor, "amount": f"0.{paise:02d}"}],
            },
        )
        assert r.status_code == 201
    preview = (await api.get(f"/groups/{g}/settlement-suggestions")).json()
    assert preview["strategy"] == "component_netting" and preview["suggested_transfer_count"] == 3
    for i, t in enumerate(preview["transfers"]):
        assert (
            await pay(api, seed, t["amount"], f"component-{i}", t["from_user_id"], t["to_user_id"])
        ).status_code == 201
    assert read(business_engine, g).debts == []


async def test_receiver_credit_limit_and_decimal_token(api, business_engine, seed):
    a, b, c = seed["users"][:3]
    for payer, amount in [(a, "1000"), (b, "100")]:
        assert (
            await api.post(
                "/expenses",
                json={
                    "group_id": seed["group"],
                    "paid_by": payer,
                    "amount": amount,
                    "description": "Credit cap",
                    "splits": [{"user_id": c, "amount": amount}],
                },
            )
        ).status_code == 201
    r = await pay(api, seed, "200", sender=c, receiver=b)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "overpayment"
    path = f"/groups/{seed['group']}/settlements"
    r = await api.post(
        path,
        content='{"from_user_id":'
        + str(c)
        + ',"to_user_id":'
        + str(b)
        + ',"amount":0.10000000000000001}',
        headers={"Idempotency-Key": "fractional", "Content-Type": "application/json"},
    )
    assert r.status_code == 422


async def test_many_partial_repayments_history_queries_and_readonly(api, business_engine, seed):
    g = seed["group"]
    assert (
        await api.post(
            "/expenses",
            json={
                "group_id": g,
                "paid_by": seed["payer"],
                "amount": "1.00",
                "description": "100 partials",
                "splits": [{"user_id": seed["users"][1], "amount": "1.00"}],
            },
        )
    ).status_code == 201
    factory = create_session_factory(business_engine)
    with factory() as s:
        for i in range(100):
            record, new = repayments.record_settlement(
                s,
                g,
                SettlementInput(
                    from_user_id=seed["users"][1], to_user_id=seed["payer"], amount="0.01"
                ),
                f"cent-{i}",
            )
            assert new and record.amount_paise == 1
    before = database_rows(business_engine)
    queries = []

    def observe(c, cursor, sql, params, ctx, many):
        queries.append(sql)

    engine = api._transport.app.state.session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", observe)
    try:
        assert (await api.get(f"/groups/{g}/settlement-suggestions")).json()["transfers"] == []
        history = (await api.get(f"/groups/{g}/settlements")).json()
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert len(history) == 100 and sum(r["amount_paise"] for r in history) == 100
    assert [(r["created_at"], r["id"]) for r in history] == sorted(
        [(r["created_at"], r["id"]) for r in history], reverse=True
    )
    assert len(queries) == 5 and all(q.startswith(("SELECT", "WITH")) for q in queries)
    assert database_rows(business_engine) == before
