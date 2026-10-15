import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, func, select, text

from app.models import Expense, ExpenseSplit, GroupMember

pytestmark = [pytest.mark.postgres, pytest.mark.anyio]


def expense_request(seed, **changes):
    return {
        "group_id": seed["group"],
        "paid_by": seed["payer"],
        "amount": 1200,
        "description": " Dinner ",
        **changes,
    }


async def test_users_and_groups_empty_order_and_duplicate_names(api):
    assert (await api.get("/users")).json() == []
    users = [
        (await api.post("/users", json={"name": name})).json() for name in [" Mohit ", "Mohit"]
    ]
    assert users[0]["name"] == "Mohit" and users[0]["id"] < users[1]["id"]
    assert (await api.get("/users")).json() == users
    groups = []
    for _ in range(2):
        response = await api.post("/groups", json={"name": " Goa Trip "})
        assert response.status_code == 201
        group = response.json()
        assert group["name"] == "Goa Trip"
        assert (await api.get(f"/groups/{group['id']}/members")).json() == []
        assert (await api.get(f"/groups/{group['id']}/expenses")).json() == []
        groups.append(group["id"])
    assert groups[0] != groups[1]
    for group in groups:
        for user in reversed(users):
            response = await api.post(f"/groups/{group}/members", json={"user_id": user["id"]})
            assert response.status_code == 201
        members = (await api.get(f"/groups/{group}/members")).json()
        assert members == [{"user_id": user["id"], "name": "Mohit"} for user in users]
        assert (
            await api.post(f"/groups/{group}/members", json={"user_id": users[0]["id"]})
        ).status_code == 409


@pytest.mark.parametrize("endpoint", ["/users", "/groups"])
@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": " \t\n"},
        {"name": "a" * 101},
        {"name": 123},
        {"name": True},
        {"name": None},
        {},
        {"name": "Valid", "extra": 1},
    ],
)
async def test_invalid_names(api, endpoint, payload):
    response = await api.post(endpoint, json=payload)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


async def test_missing_resources_and_member_input(api, seed):
    for path in ["/groups/9223372036854775807/members", "/groups/9223372036854775807/expenses"]:
        assert (await api.get(path)).status_code == 404
    assert (
        await api.post("/groups/9223372036854775807/members", json={"user_id": seed["payer"]})
    ).status_code == 404
    path = f"/groups/{seed['group']}/members"
    response = await api.post(path, json={"user_id": 9223372036854775807})
    assert response.status_code == 404 and response.json()["detail"]["code"] == "user_not_found"
    for payload in [
        {"user_id": True},
        {"user_id": 0},
        {"user_id": "1"},
        {"user_id": 1.0},
        {"user_id": seed["outsider"], "extra": 1},
        {},
    ]:
        assert (await api.post(path, json=payload)).status_code == 422
    assert (await api.get("/groups/0/members")).status_code == 422
    assert (await api.get("/groups/9223372036854775808/expenses")).status_code == 422


async def test_three_member_expense_is_persisted_exactly(api, seed, business_engine):
    response = await api.post("/expenses", json=expense_request(seed))
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["amount"] == "1200.00" and data["amount_paise"] == 120000
    assert data["description"] == "Dinner" and data["paid_by_name"] == "Mohit"
    assert [s["user_id"] for s in data["splits"]] == seed["users"][:3]
    assert [s["share_paise"] for s in data["splits"]] == [40000] * 3
    assert [s["share_amount"] for s in data["splits"]] == ["400.00"] * 3
    assert datetime.fromisoformat(
        data["created_at"].replace("Z", "+00:00")
    ).utcoffset() == timedelta(0)
    with business_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Expense)) == 1
        assert connection.scalar(select(func.count()).select_from(ExpenseSplit)) == 3
        assert connection.scalar(select(func.sum(ExpenseSplit.share_paise))) == 120000
    assert (await api.get(f"/groups/{seed['group']}/expenses")).json() == [data]


@pytest.mark.parametrize(
    "raw",
    [
        "0",
        "-1",
        "1.001",
        "1000000000.01",
        "NaN",
        "Infinity",
        "true",
        '"bad"',
        '"NaN"',
        "null",
        "1e999999",
        "0.10000000000000001",
    ],
)
async def test_invalid_raw_money_is_422_without_writes(api, seed, business_engine, raw):
    payload = json.dumps(expense_request(seed))
    payload = payload.replace('"amount": 1200', '"amount": ' + raw)
    response = await api.post(
        "/expenses", content=payload, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    with business_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Expense)) == 0


async def test_oversized_numeric_json_reaches_validation(api, seed):
    payload = json.dumps(expense_request(seed)).replace('"amount": 1200', '"amount": ' + "9" * 5000)
    response = await api.post(
        "/expenses", content=payload, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert len(response.content) < 512  # raw oversized input is never echoed


@pytest.mark.parametrize("raw", ["1200.50", '"1200.50"', "1.2300", "1e2", "0.01"])
async def test_decimal_json_and_string_money(api, seed, raw):
    body = json.dumps(expense_request(seed)).replace('"amount": 1200', '"amount": ' + raw)
    response = await api.post(
        "/expenses", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 201, response.text
    assert (
        sum(s["share_paise"] for s in response.json()["splits"]) == response.json()["amount_paise"]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"description": " "},
        {"description": "x" * 501},
        {"description": 1},
        {"group_id": True},
        {"paid_by": 1.0},
        {"extra": "unexpected"},
    ],
)
async def test_expense_request_validation(api, seed, changes):
    assert (await api.post("/expenses", json=expense_request(seed, **changes))).status_code == 422


async def test_missing_fields_and_malformed_json(api, seed):
    for field in ["amount", "paid_by", "group_id", "description"]:
        payload = expense_request(seed)
        del payload[field]
        assert (await api.post("/expenses", json=payload)).status_code == 422
    assert (
        await api.post("/expenses", content="{", headers={"Content-Type": "application/json"})
    ).status_code == 422


async def test_expense_domain_errors(api, seed):
    for changes, status, code in [
        ({"group_id": 9223372036854775807}, 404, "group_not_found"),
        ({"paid_by": 9223372036854775807}, 404, "user_not_found"),
        ({"paid_by": seed["outsider"]}, 422, "payer_not_member"),
    ]:
        response = await api.post("/expenses", json=expense_request(seed, **changes))
        assert response.status_code == status and response.json()["detail"]["code"] == code
    group = (await api.post("/groups", json={"name": "Empty"})).json()["id"]
    response = await api.post("/expenses", json=expense_request(seed, group_id=group))
    assert response.status_code == 422 and response.json()["detail"]["code"] == "empty_group"


async def test_snapshot_history_order_and_group_isolation(api, seed, business_engine):
    first = (await api.post("/expenses", json=expense_request(seed, amount=100))).json()
    assert [s["share_paise"] for s in first["splits"]] == [3334, 3333, 3333]
    await api.post(f"/groups/{seed['group']}/members", json={"user_id": seed["outsider"]})
    second = (
        await api.post(
            "/expenses", json=expense_request(seed, paid_by=seed["outsider"], amount="0.01")
        )
    ).json()
    assert len(second["splits"]) == 4
    assert [s["share_paise"] for s in second["splits"]] == [1, 0, 0, 0]
    with business_engine.begin() as connection:
        connection.execute(text("UPDATE expenses SET created_at='2026-01-01T00:00:00Z'"))
    history = (await api.get(f"/groups/{seed['group']}/expenses")).json()
    assert [e["id"] for e in history] == [second["id"], first["id"]]
    assert history[1]["splits"] == first["splits"]
    other = (await api.post("/groups", json={"name": "Other"})).json()["id"]
    assert (await api.get(f"/groups/{other}/expenses")).json() == []


async def test_single_member_and_same_display_names(api):
    group = (await api.post("/groups", json={"name": "Trip"})).json()["id"]
    people = []
    for count in [1, 2]:
        person = (await api.post("/users", json={"name": "Same"})).json()["id"]
        people.append(person)
        await api.post(f"/groups/{group}/members", json={"user_id": person})
        response = await api.post(
            "/expenses",
            json={"group_id": group, "paid_by": people[0], "amount": "0.01", "description": "Tiny"},
        )
        assert response.status_code == 201
        assert len(response.json()["splits"]) == count
        assert response.json()["splits"][0]["share_paise"] == 1


async def test_real_database_split_failure_rolls_back_and_next_request_works(
    api, seed, business_engine
):
    # The API has its own engine, so attach to Engine class; remove in finally.
    from sqlalchemy import Engine

    triggered = []

    def fail_split(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO expense_splits") and not triggered:
            triggered.append(connection.scalar(select(func.count()).select_from(Expense)))
            connection.exec_driver_sql("SELECT 1 / 0")  # actual PostgreSQL transaction failure

    event.listen(Engine, "before_cursor_execute", fail_split)
    try:
        response = await api.post("/expenses", json=expense_request(seed))
        assert response.status_code == 500
        assert response.json() == {
            "detail": {"code": "internal_error", "message": "Internal server error"}
        }
    finally:
        event.remove(Engine, "before_cursor_execute", fail_split)
    assert triggered == [1]  # expense had been inserted, before failure in split insertion
    with business_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Expense)) == 0
        assert connection.scalar(select(func.count()).select_from(ExpenseSplit)) == 0
    assert (await api.post("/expenses", json=expense_request(seed))).status_code == 201


async def test_history_and_members_have_bounded_queries(api, seed, business_engine):
    from sqlalchemy import Engine

    for _ in range(5):
        assert (await api.post("/expenses", json=expense_request(seed))).status_code == 201
    selects = []

    def count(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(Engine, "before_cursor_execute", count)
    try:
        assert len((await api.get(f"/groups/{seed['group']}/expenses")).json()) == 5
        assert len(selects) == 3
        selects.clear()
        assert len((await api.get(f"/groups/{seed['group']}/members")).json()) == 3
        assert len(selects) == 2
    finally:
        event.remove(Engine, "before_cursor_execute", count)


async def test_full_assignment_workflow_and_openapi(api):
    people = [
        (await api.post("/users", json={"name": name})).json()
        for name in ["Mohit", "Rahul", "Ankit"]
    ]
    group = (await api.post("/groups", json={"name": "Goa Trip"})).json()["id"]
    for person in people:
        assert (
            await api.post(f"/groups/{group}/members", json={"user_id": person["id"]})
        ).status_code == 201
    data = {"group_id": group, "paid_by": people[0]["id"], "amount": 1200, "description": "Dinner"}
    dinner = (await api.post("/expenses", json=data)).json()
    assert [split["share_amount"] for split in dinner["splits"]] == ["400.00"] * 3
    assert (await api.get(f"/groups/{group}/expenses")).json() == [dinner]
    priya = (await api.post("/users", json={"name": "Priya"})).json()["id"]
    await api.post(f"/groups/{group}/members", json={"user_id": priya})
    later = (await api.post("/expenses", json={**data, "paid_by": priya})).json()
    history = (await api.get(f"/groups/{group}/expenses")).json()
    assert history == [later, dinner]
    assert len(later["splits"]) == 4 and len(dinner["splits"]) == 3
    schema = (await api.get("/openapi.json")).json()
    assert schema["components"]["schemas"]["ExpenseInput"]["additionalProperties"] is False
    amount_schema = schema["components"]["schemas"]["ExpenseInput"]["properties"]["amount"]
    numeric = amount_schema["anyOf"][0]
    assert numeric == {
        "type": "number",
        "minimum": 0.01,
        "maximum": 1_000_000_000,
        "multipleOf": 0.01,
    }
    assert "gt" not in amount_schema and "le" not in amount_schema
    assert (
        schema["components"]["schemas"]["ExpenseOutput"]["properties"]["amount"]["type"] == "string"
    )
    assert set(schema["paths"]) == {
        "/health",
        "/health/ready",
        "/users",
        "/groups",
        "/groups/{group_id}/members",
        "/expenses",
        "/groups/{group_id}/expenses",
        "/balances",
        "/groups/{group_id}/balances",
        "/groups/{group_id}/settlement-suggestions",
        "/groups/{group_id}/settlements",
    }


async def test_populated_groups_are_isolated_and_payers_cannot_cross_groups(api, seed):
    original = (await api.post("/expenses", json=expense_request(seed))).json()
    other = (await api.post("/groups", json={"name": "Other"})).json()["id"]
    assert (
        await api.post(f"/groups/{other}/members", json={"user_id": seed["outsider"]})
    ).status_code == 201
    data = expense_request(seed, group_id=other, paid_by=seed["outsider"])
    different = (await api.post("/expenses", json=data)).json()
    assert (await api.get(f"/groups/{other}/expenses")).json() == [different]
    assert (await api.get(f"/groups/{seed['group']}/expenses")).json() == [original]
    for payload in [
        expense_request(seed, paid_by=seed["outsider"]),
        {**data, "paid_by": seed["payer"]},
    ]:
        response = await api.post("/expenses", json=payload)
        assert (
            response.status_code == 422 and response.json()["detail"]["code"] == "payer_not_member"
        )


async def test_actual_unique_violation_is_translated_and_rolled_back(api, seed, monkeypatch):
    from sqlalchemy.orm import Session

    original = Session.get

    def stale_membership_lookup(session, entity, identity, *args, **kwargs):
        if entity is GroupMember:
            return None  # force the duplicate INSERT to reach the genuine PostgreSQL PK
        return original(session, entity, identity, *args, **kwargs)

    monkeypatch.setattr(Session, "get", stale_membership_lookup)
    response = await api.post(f"/groups/{seed['group']}/members", json={"user_id": seed["payer"]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "membership_exists"
    assert (
        await api.post(f"/groups/{seed['group']}/members", json={"user_id": seed["outsider"]})
    ).status_code == 201
