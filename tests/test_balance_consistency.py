from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.services import groups
from tests.test_balances import create, read

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("pause_after", ["existence", "totals"])
@pytest.mark.parametrize("write", ["expense", "membership"])
def test_one_response_has_one_snapshot(business_engine, seed, pause_after, write):
    g = seed["group"]
    create(business_engine, g, seed["payer"], "1200")
    before = read(business_engine, g)
    held, release = Event(), Event()
    reader_pid = []

    def pause(c, cursor, statement, params, context, many):
        marker = "SELECT groups.id" if pause_after == "existence" else "WITH contributions AS"
        if statement.startswith(marker) and not reader_pid:
            reader_pid.append(c.connection.driver_connection.info.backend_pid)
            held.set()
            assert release.wait(10), "Reader release timed out"

    def reader():
        return read(business_engine, g)

    event.listen(business_engine, "after_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(reader)
            try:
                assert held.wait(10), "Snapshot statement was not reached"
                with create_session_factory(business_engine)() as session:
                    # Writer must commit before releasing the reader; no reader row lock.
                    if write == "expense":
                        from app.schemas.business import ExpenseInput
                        from app.services import expenses

                        expenses.create_expense(
                            session,
                            ExpenseInput(
                                group_id=g,
                                paid_by=seed["users"][1],
                                amount="0.01",
                                description="Race",
                            ),
                        )
                    else:
                        groups.add_member(session, g, seed["outsider"])
                # Confirm the independent writer committed while the read is paused.
                with business_engine.connect() as c:
                    assert c.scalar(text("SELECT pg_backend_pid()")) != reader_pid[0]
            finally:
                release.set()
            assert future.result(timeout=10) == before
    finally:
        event.remove(business_engine, "after_cursor_execute", pause)
    after = read(business_engine, g)
    if write == "expense":
        assert after.total_expenses_paise == 120001
        assert after != before
    else:
        assert len(after.balances) == 4
        assert after.balances[-1].net_balance_paise == 0
        assert after.debts == before.debts


def test_failed_write_absent_then_committed_write_visible(business_engine, seed):
    before = read(business_engine, seed["group"])

    def fail(c, cursor, statement, params, context, many):
        if statement.startswith("INSERT INTO expense_splits"):
            c.exec_driver_sql("SELECT 1/0")

    event.listen(business_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(DBAPIError):
            create(business_engine, seed["group"], seed["payer"], "1200")
    finally:
        event.remove(business_engine, "before_cursor_execute", fail)
    assert read(business_engine, seed["group"]) == before
    create(business_engine, seed["group"], seed["payer"], "1200")
    assert read(business_engine, seed["group"]).total_expenses_paise == 120000


@pytest.mark.anyio
async def test_real_connection_outage_and_recovery(api, business_engine, seed):
    path = f"/groups/{seed['group']}/balances"
    before = await api.get(path)
    assert before.status_code == 200
    # Only this generated disposable database is disabled; observer remains connected.
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


async def check_outage(api, admin, name, path, before):
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
        observer.exec_driver_sql(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false')
        try:
            observer.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:name AND pid<>pg_backend_pid()"
                ),
                {"name": name},
            )
            response = await api.get(path)
            assert response.status_code == 500
            assert response.json() == {
                "detail": {"code": "internal_error", "message": "Internal server error"}
            }
            assert (await api.get("/health/ready")).status_code == 503
        finally:
            observer.exec_driver_sql(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
    assert (await api.get(path)).json() == before
    assert (await api.get("/health/ready")).status_code == 200
