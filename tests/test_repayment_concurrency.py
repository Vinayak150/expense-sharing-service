import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError

from app.core.database import create_session_factory
from app.schemas.business import CustomSplitInput, ExpenseInput
from app.schemas.settlements import SettlementInput
from app.services import expenses, groups, repayments
from app.services.errors import InvalidExpense, InvalidSettlement
from tests.test_balances import create, read
from tests.test_concurrency import wait_for_database_block
from tests.test_repayments import pay, reconcile

pytestmark = pytest.mark.postgres


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["overpayment", "duplicate", "conflict"])
async def test_competing_http_writes(api, business_engine, seed, monkeypatch, case):
    create(business_engine, seed["group"], seed["payer"], "1200")
    barrier = Barrier(2, timeout=10)
    original = groups.lock_group
    pids = []

    def lock(session, g):
        conn = session.connection().connection.driver_connection
        assert conn.isolation_level.name == "READ_COMMITTED" and not conn.read_only
        pids.append(conn.info.backend_pid)
        barrier.wait()
        return original(session, g)

    monkeypatch.setattr(groups, "lock_group", lock)
    keys = ["a", "b"] if case == "overpayment" else ["same", "same"]
    amounts = ["300", "300"] if case != "conflict" else ["150", "200"]
    responses = await asyncio.gather(
        *[pay(api, seed, amount, k) for amount, k in zip(amounts, keys, strict=True)]
    )
    assert len(set(pids)) == 2
    assert (
        sorted(r.status_code for r in responses)
        == {"overpayment": [201, 422], "duplicate": [200, 201], "conflict": [201, 409]}[case]
    )
    history = (await api.get(f"/groups/{seed['group']}/settlements")).json()
    assert len(history) == 1 and history[0]["amount_paise"] <= 40000
    reconcile(read(business_engine, seed["group"]))


@pytest.mark.parametrize("first", ["expense", "settlement", "membership", "custom"])
def test_writers_observe_commits_after_group_lock(business_engine, seed, monkeypatch, first):
    create(business_engine, seed["group"], seed["payer"], "1200")
    second = {
        "expense": "settlement",
        "settlement": "expense",
        "membership": "custom",
        "custom": "membership",
    }[first]
    held, attempting, release = Event(), Event(), Event()
    pids = {}
    original = groups.lock_group
    factory = create_session_factory(business_engine)

    def lock(session, g):
        role = session.info["role"]
        pids[role] = session.connection().connection.driver_connection.info.backend_pid
        if role != first:
            attempting.set()
        group = original(session, g)
        if role == first:
            held.set()
            assert release.wait(10)
        return group

    def operation(role):
        with factory() as s:
            s.info["role"] = role
            try:
                if role == "membership":
                    return groups.add_member(s, seed["group"], seed["outsider"])
                if role == "settlement":
                    return repayments.record_settlement(
                        s,
                        seed["group"],
                        SettlementInput(
                            from_user_id=seed["users"][1], to_user_id=seed["payer"], amount="300"
                        ),
                        "race",
                    )
                custom = role == "custom"
                return expenses.create_expense(
                    s,
                    ExpenseInput(
                        group_id=seed["group"],
                        paid_by=seed["payer"] if custom else seed["users"][1],
                        amount="1200",
                        description="Race",
                        splits=[CustomSplitInput(user_id=seed["outsider"], amount="1200")]
                        if custom
                        else None,
                    ),
                )
            except (InvalidSettlement, InvalidExpense) as e:
                return e

    monkeypatch.setattr(groups, "lock_group", lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(operation, first)
        try:
            assert held.wait(10)
            waiter = pool.submit(operation, second)
            assert attempting.wait(10)
            wait_for_database_block(business_engine, pids[second], pids[first])
        finally:
            release.set()
        a, b = owner.result(timeout=10), waiter.result(timeout=10)
    assert len(set(pids.values())) == 2
    if first == "expense":
        assert isinstance(b, InvalidSettlement)
    elif first == "custom":
        assert isinstance(a, InvalidExpense) and not isinstance(b, Exception)
    else:
        assert not isinstance(a, Exception) and not isinstance(b, Exception)
    reconcile(read(business_engine, seed["group"]))


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["balances", "settlement-suggestions"])
async def test_repayment_commit_during_read(api, business_engine, seed, route):
    create(business_engine, seed["group"], seed["payer"], "1200")
    path = f"/groups/{seed['group']}/{route}"
    before = (await api.get(path)).json()
    held, release = Event(), Event()
    engine = api._transport.app.state.session_factory.kw["bind"]

    def pause(c, cursor, sql, params, ctx, many):
        if sql.startswith("WITH contributions AS") and c.connection.driver_connection.read_only:
            held.set()
            assert release.wait(10)

    event.listen(engine, "after_cursor_execute", pause)
    reader = asyncio.create_task(api.get(path))
    try:
        assert await asyncio.to_thread(held.wait, 10)
        assert (await asyncio.wait_for(pay(api, seed, "150"), timeout=5)).status_code == 201
    finally:
        release.set()
        response = await asyncio.wait_for(reader, 10)
        event.remove(engine, "after_cursor_execute", pause)
    assert response.status_code == 200 and response.json() == before
    assert (await api.get(path)).json() != before
    reconcile(read(business_engine, seed["group"]))


def test_db_error_then_reuse_session_after_read(business_engine, seed):
    create(business_engine, seed["group"], seed["payer"], "1200")
    factory = create_session_factory(business_engine)
    failed = []

    def fail(c, cursor, sql, params, ctx, many):
        if sql.startswith("INSERT INTO settlements") and not failed:
            failed.append(True)
            c.exec_driver_sql("SELECT 1/0")

    event.listen(business_engine, "before_cursor_execute", fail)
    try:
        with factory() as session:
            from app.services.balances import calculate_group_balances

            calculate_group_balances(session, seed["group"])
            data = SettlementInput(
                from_user_id=seed["users"][1], to_user_id=seed["payer"], amount="150"
            )
            with pytest.raises(DBAPIError):
                repayments.record_settlement(session, seed["group"], data, "reuse")
            assert not session.in_transaction()
            assert repayments.record_settlement(session, seed["group"], data, "reuse")[1] is True
    finally:
        event.remove(business_engine, "before_cursor_execute", fail)
