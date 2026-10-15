import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import event

from app.services.splitting import validate_custom_split
from tests.test_balances import read


@pytest.mark.parametrize(
    "allocations", [[(1, 50), (2, 30), (3, 20)], [(2, 60), (3, 40)], [(2, 100)], [(1, 0), (2, 100)]]
)
def test_explicit_allocations(allocations):
    original = allocations.copy()
    assert validate_custom_split(100, allocations, [1, 2, 3], 1) == dict(allocations)
    assert allocations == original


@pytest.mark.parametrize(
    "total,allocations,payer",
    [
        (100, [], 1),
        (100, [(2, 50), (2, 50)], 1),
        (100, [(4, 100)], 1),
        (100, [(2, -1), (3, 101)], 1),
        (100, [(2, 99)], 1),
        (100, [(2, 101)], 1),
        (100, [(2, 100)], 4),
        (100, [(True, 100)], 1),
        (100, [(2, 100.0)], 1),
        (True, [(2, 1)], 1),
        (10**11 + 1, [(2, 10**11 + 1)], 1),
        (100, [(2, False)], 1),
    ],
)
def test_invalid_custom_allocations(total, allocations, payer):
    with pytest.raises(ValueError):
        validate_custom_split(total, allocations, [1, 2, 3], payer)


@settings(max_examples=150, derandomize=True, database=None, deadline=None)
@given(st.lists(st.integers(0, 10**8), min_size=1, max_size=15).filter(lambda xs: sum(xs) > 0))
def test_custom_conservation(shares):
    allocations = list(enumerate(shares, 1))
    result = validate_custom_split(
        sum(shares), allocations, list(range(1, len(shares) + 2)), len(shares) + 1
    )
    assert result == dict(allocations) and sum(result.values()) == sum(shares)


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["all", "subset", "single", "zero", "null", "omitted"])
async def test_custom_http_and_history(api, business_engine, seed, mode):
    a, b, c = seed["users"][:3]
    variants = {
        "all": [(a, 500), (b, 300), (c, 200)],
        "subset": [(b, 600), (c, 400)],
        "single": [(b, 1000)],
        "zero": [(a, 0), (b, 1000)],
    }
    data = {"group_id": seed["group"], "paid_by": a, "amount": "1000", "description": "Custom"}
    if mode in variants:
        data["splits"] = [{"user_id": u, "amount": str(v)} for u, v in variants[mode]]
    elif mode == "null":
        data["splits"] = None
    r = await api.post("/expenses", json=data)
    assert r.status_code == 201, r.text
    e = r.json()
    assert sum(s["share_paise"] for s in e["splits"]) == 100000
    if mode in variants:
        assert [(s["user_id"], s["share_paise"]) for s in e["splits"]] == [
            (u, v * 100) for u, v in variants[mode]
        ]
    value = read(business_engine, seed["group"])
    assert sum(m.net_balance_paise for m in value.balances) == 0
    preview = await api.get(f"/groups/{seed['group']}/settlement-suggestions")
    assert preview.status_code == 200
    assert (
        await api.post(f"/groups/{seed['group']}/members", json={"user_id": seed["outsider"]})
    ).status_code == 201
    assert (await api.get(f"/groups/{seed['group']}/expenses")).json() == [e]
    if mode == "subset":
        assert [m.net_balance_paise for m in value.balances] == [100000, -60000, -40000]
        assert len(value.debts) == 2


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize(
    "splits",
    [
        [],
        [{"user_id": 2, "amount": 1000}, {"user_id": 2, "amount": 0}],
        [{"user_id": 4, "amount": 1000}],
        [{"user_id": 999, "amount": 1000}],
        [{"user_id": 2, "amount": -1}],
        [{"user_id": 2, "amount": "0.001"}],
        [{"user_id": 2, "amount": 999}],
        [{"user_id": 2, "amount": 1001}],
        [{"user_id": True, "amount": 1000}],
        [{"user_id": 2, "amount": 1000, "extra": 1}],
        [{"user_id": 2, "amount": "1000000000.01"}],
        [{"user_id": 2, "amount": False}],
    ],
)
async def test_invalid_custom_request_leaves_no_expense(api, seed, splits):
    r = await api.post(
        "/expenses",
        json={
            "group_id": seed["group"],
            "paid_by": seed["payer"],
            "amount": 1000,
            "description": "Invalid",
            "splits": splits,
        },
    )
    assert r.status_code == 422, r.text
    assert (await api.get(f"/groups/{seed['group']}/expenses")).json() == []


@pytest.mark.postgres
@pytest.mark.anyio
async def test_custom_database_failure_rolls_back(api, business_engine, seed):
    engine = api._transport.app.state.session_factory.kw["bind"]

    def fail(c, cursor, sql, params, ctx, many):
        if sql.startswith("INSERT INTO expense_splits"):
            c.exec_driver_sql("SELECT 1/0")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        r = await api.post(
            "/expenses",
            json={
                "group_id": seed["group"],
                "paid_by": seed["payer"],
                "amount": "1",
                "description": "Rollback",
                "splits": [{"user_id": seed["users"][1], "amount": 1}],
            },
        )
        assert r.status_code == 500
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert (await api.get(f"/groups/{seed['group']}/expenses")).json() == []
