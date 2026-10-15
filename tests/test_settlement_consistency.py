import asyncio
from threading import Event

import pytest
from sqlalchemy import event

from app.core.config import Settings
from app.core.database import create_database_engine
from app.services import groups
from tests.test_balance_consistency import check_outage
from tests.test_balances import create
from tests.test_settlement_api import assert_preview

pytestmark = [pytest.mark.postgres, pytest.mark.anyio]


async def test_preview_proceeds_while_writer_holds_group_lock(
    api, business_engine, seed, monkeypatch
):
    g = seed["group"]
    create(business_engine, g, seed["payer"], "1200")
    path = f"/groups/{g}/settlement-suggestions"
    before = (await api.get(path)).json()
    nets = {seed["users"][0]: 80000, seed["users"][1]: -40000, seed["users"][2]: -40000}
    held, release = Event(), Event()
    writer_pid = []
    reader_pids = []
    original = groups.lock_group

    def hold(session, g):
        group = original(session, g)
        writer_pid.append(session.connection().connection.driver_connection.info.backend_pid)
        held.set()
        assert release.wait(10), "Writer release timed out"
        return group

    def observe(c, cursor, statement, params, context, many):
        if c.connection.driver_connection.read_only:
            reader_pids.append(c.connection.driver_connection.info.backend_pid)

    monkeypatch.setattr(groups, "lock_group", hold)
    engine = api._transport.app.state.session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", observe)
    writer = asyncio.create_task(
        api.post(
            "/expenses",
            json={
                "group_id": g,
                "paid_by": seed["users"][1],
                "amount": "300",
                "description": "Pending",
            },
        )
    )
    try:
        assert await asyncio.to_thread(held.wait, 10)
        reader = await asyncio.wait_for(api.get(path), timeout=5)
        assert reader.status_code == 200 and reader.json() == before
        assert_preview(reader.json(), nets, 2)
        assert set(reader_pids).isdisjoint(writer_pid)
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", observe)
        result = await asyncio.wait_for(writer, timeout=10)
    assert result.status_code == 201
    after = (await api.get(path)).json()
    assert after != before
    assert_preview(
        after, {seed["users"][0]: 70000, seed["users"][1]: -20000, seed["users"][2]: -50000}, 3
    )


async def test_writer_commits_between_settlement_snapshot_queries(api, business_engine, seed):
    g = seed["group"]
    create(business_engine, g, seed["payer"], "1200")
    path = f"/groups/{g}/settlement-suggestions"
    before = (await api.get(path)).json()
    held, release = Event(), Event()
    engine = api._transport.app.state.session_factory.kw["bind"]

    def pause(c, cursor, statement, params, context, many):
        if statement.startswith("WITH contributions AS"):
            held.set()
            assert release.wait(10), "Read release timed out"

    event.listen(engine, "after_cursor_execute", pause)
    reader = asyncio.create_task(api.get(path))
    try:
        assert await asyncio.to_thread(held.wait, 10)
        # Commit a new expense before the reader's bilateral query runs.
        writer = await asyncio.wait_for(
            api.post(
                "/expenses",
                json={
                    "group_id": g,
                    "paid_by": seed["users"][1],
                    "amount": "300",
                    "description": "Committed",
                },
            ),
            timeout=5,
        )
        assert writer.status_code == 201
    finally:
        release.set()
        result = await asyncio.wait_for(reader, timeout=10)
        event.remove(engine, "after_cursor_execute", pause)
    assert result.status_code == 200 and result.json() == before
    after = (await api.get(path)).json()
    assert after != before
    assert_preview(
        after, {seed["users"][0]: 70000, seed["users"][1]: -20000, seed["users"][2]: -50000}, 3
    )


async def test_real_database_outage_and_recovery(api, business_engine, seed):
    create(business_engine, seed["group"], seed["payer"], "1200")
    path = f"/groups/{seed['group']}/settlement-suggestions"
    before = await api.get(path)
    assert before.status_code == 200 and before.json()["transfers"]
    name = business_engine.url.database
    assert name.startswith("expense_test_") and name.replace("_", "").isalnum()
    admin = create_database_engine(
        Settings(
            database_url=business_engine.url.set(database="postgres").render_as_string(
                hide_password=False
            ),
            _env_file=None,
        )
    )
    try:
        await check_outage(api, admin, name, path, before.json())
    finally:
        admin.dispose()
