import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from time import monotonic

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import create_session_factory
from app.models import Expense, ExpenseSplit, Group, GroupMember
from app.schemas.business import ExpenseInput
from app.services import expenses, groups
from app.services.errors import Conflict

pytestmark = pytest.mark.postgres


def data(seed, group_id=None):
    return ExpenseInput(
        group_id=group_id or seed["group"],
        paid_by=seed["payer"],
        amount="100.00",
        description="Concurrent",
    )


def wait_for_database_block(engine, blocked, blocker):
    deadline = monotonic() + 10
    with engine.connect() as connection:
        while monotonic() < deadline:
            pids = connection.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": blocked})
            if blocker in pids:
                return
            Event().wait(0.01)
    raise AssertionError("Expected PostgreSQL group-row lock wait was not observed")


@pytest.mark.anyio
async def test_two_simultaneous_membership_requests(api, seed, monkeypatch):
    barrier = Barrier(2, timeout=10)
    original = groups.lock_group
    pids = set()
    guard = Lock()

    def lock_group(session, group_id):
        with guard:
            pids.add(session.scalar(text("SELECT pg_backend_pid()")))
        barrier.wait()
        return original(session, group_id)

    monkeypatch.setattr(groups, "lock_group", lock_group)
    path = f"/groups/{seed['group']}/members"
    responses = await asyncio.gather(
        *[api.post(path, json={"user_id": seed["outsider"]}) for _ in range(2)]
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert len(pids) == 2
    members = (await api.get(path)).json()
    assert sum(m["user_id"] == seed["outsider"] for m in members) == 1


@pytest.mark.parametrize("first", ["membership", "expense"])
def test_membership_and_expense_serialize_both_orders(business_engine, seed, monkeypatch, first):
    factory = create_session_factory(business_engine)
    held, attempting, release = Event(), Event(), Event()
    pids = {}
    original = groups.lock_group

    def lock_group(session, group_id):
        role = session.info["test_role"]
        pids[role] = session.scalar(text("SELECT pg_backend_pid()"))
        if role != first:
            attempting.set()
        group = original(session, group_id)
        if role == first:
            held.set()
            assert release.wait(10), "Timed out waiting to release owning transaction"
        return group

    def operation(role):
        with factory() as session:
            session.info["test_role"] = role
            if role == "membership":
                return groups.add_member(session, seed["group"], seed["outsider"])
            return expenses.create_expense(session, data(seed))

    monkeypatch.setattr(groups, "lock_group", lock_group)
    second = "expense" if first == "membership" else "membership"
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(operation, first)
        try:
            assert held.wait(10)
            waiter = pool.submit(operation, second)
            assert attempting.wait(10)
            wait_for_database_block(business_engine, pids[second], pids[first])
        finally:
            release.set()
        results = {first: owner.result(timeout=10), second: waiter.result(timeout=10)}
    assert len(set(pids.values())) == 2
    expense = results["expense"]
    assert len(expense.splits) == (4 if first == "membership" else 3)
    assert (seed["outsider"] in [split.user_id for split in expense.splits]) == (
        first == "membership"
    )
    assert sum(split.share_paise for split in expense.splits) == 10000


def test_multiple_expenses_same_group_have_complete_allocations(business_engine, seed, monkeypatch):
    factory = create_session_factory(business_engine)
    barrier = Barrier(4, timeout=10)
    pids = set()
    guard = Lock()
    original = groups.lock_group

    def lock_group(session, group_id):
        with guard:
            pids.add(session.scalar(text("SELECT pg_backend_pid()")))
        barrier.wait()
        return original(session, group_id)

    def create():
        with factory() as session:
            return expenses.create_expense(session, data(seed))

    monkeypatch.setattr(groups, "lock_group", lock_group)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(create) for _ in range(4)]
        results = [future.result(timeout=15) for future in futures]
    assert len(pids) == 4
    assert len({result.id for result in results}) == 4
    assert all(len(result.splits) == 3 for result in results)
    assert all(sum(s.share_paise for s in result.splits) == 10000 for result in results)
    with business_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(ExpenseSplit)) == 12


def test_independent_group_progress_while_first_group_is_locked(business_engine, seed, monkeypatch):
    with business_engine.begin() as connection:
        other = connection.scalar(Group.__table__.insert().values(name="Other").returning(Group.id))
        connection.execute(
            GroupMember.__table__.insert().values(group_id=other, user_id=seed["payer"])
        )
    factory = create_session_factory(business_engine)
    held, release = Event(), Event()
    original = groups.lock_group

    def lock_group(session, group_id):
        group = original(session, group_id)
        if group_id == seed["group"]:
            held.set()
            assert release.wait(10)
        return group

    def create(group_id):
        with factory() as session:
            return expenses.create_expense(session, data(seed, group_id))

    monkeypatch.setattr(groups, "lock_group", lock_group)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(create, seed["group"])
        try:
            assert held.wait(10)
            second = pool.submit(create, other)
            result = second.result(timeout=5)  # must COMMIT before the first group is released
            assert result.group_id == other and len(result.splits) == 1
        finally:
            release.set()
        assert first.result(timeout=10).group_id == seed["group"]


def test_database_failure_rolls_back_and_same_session_is_reusable(business_engine, seed):
    factory = create_session_factory(business_engine)
    failed = []

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO expense_splits") and not failed:
            failed.append(True)
            connection.exec_driver_sql("SELECT 1 / 0")

    event.listen(business_engine, "before_cursor_execute", fail)
    try:
        with factory() as session:
            with pytest.raises(DBAPIError):
                expenses.create_expense(session, data(seed))
            assert not session.in_transaction()
            assert expenses.create_expense(session, data(seed)).id > 0
            with pytest.raises(Conflict):
                groups.add_member(session, seed["group"], seed["payer"])
            assert not session.in_transaction()
            assert (
                groups.add_member(session, seed["group"], seed["outsider"]).user_id
                == seed["outsider"]
            )
    finally:
        event.remove(business_engine, "before_cursor_execute", fail)
    with business_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Expense)) == 1
        assert connection.scalar(select(func.count()).select_from(ExpenseSplit)) == 3
