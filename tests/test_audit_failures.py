"""Real PostgreSQL commit and bounded resource failure injection."""

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError, TimeoutError
from sqlalchemy.pool import QueuePool

from app.core.database import create_session_factory
from app.models import Expense, ExpenseSplit, Settlement
from app.schemas.business import ExpenseInput
from app.services import expenses
from tests.test_balances import create
from tests.test_business_api import expense_request
from tests.test_repayments import pay

pytestmark = pytest.mark.postgres


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["expense", "settlement"])
async def test_deferred_database_failure_at_commit_is_atomic(api, seed, business_engine, operation):
    if operation == "settlement":
        create(business_engine, seed["group"], seed["payer"], "1200")
    table = "expense_splits" if operation == "expense" else "settlements"
    with business_engine.begin() as c:
        c.exec_driver_sql("""
            CREATE FUNCTION audit_fail_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected deferred commit failure' USING ERRCODE='23514';
            END $$
        """)
        c.exec_driver_sql(f"""
            CREATE CONSTRAINT TRIGGER audit_commit_failure AFTER INSERT ON {table}
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_fail_commit()
        """)

    async def request():
        if operation == "expense":
            return await api.post("/expenses", json=expense_request(seed))
        return await pay(api, seed, "150", "commit-retry")

    try:
        response = await request()
        assert response.status_code == 500
        assert response.json() == {
            "detail": {"code": "internal_error", "message": "Internal server error"}
        }
        with business_engine.connect() as c:
            assert c.scalar(select(func.count()).select_from(Settlement)) == 0
            if operation == "expense":
                assert c.scalar(select(func.count()).select_from(Expense)) == 0
                assert c.scalar(select(func.count()).select_from(ExpenseSplit)) == 0
    finally:
        with business_engine.begin() as c:
            c.exec_driver_sql(f"DROP TRIGGER audit_commit_failure ON {table}")
            c.exec_driver_sql("DROP FUNCTION audit_fail_commit()")
    assert (await request()).status_code == 201
    if operation == "settlement":
        assert (await request()).status_code == 200
        with business_engine.connect() as c:
            assert c.scalar(select(func.count()).select_from(Settlement)) == 1


def test_group_lock_timeout_rolls_back_and_session_recovers(business_engine, seed):
    factory = create_session_factory(business_engine)
    data = ExpenseInput(**expense_request(seed))
    with business_engine.connect() as owner, factory() as session:
        owner.execute(text("SELECT id FROM groups WHERE id=:g FOR UPDATE"), {"g": seed["group"]})

        def bounded_timeout(c):
            c.exec_driver_sql("SET LOCAL lock_timeout='100ms'")

        event.listen(business_engine, "begin", bounded_timeout)
        try:
            with pytest.raises(DBAPIError) as error:
                expenses.create_expense(session, data)
            assert error.value.orig.sqlstate == "55P03"
            assert not session.in_transaction()
        finally:
            event.remove(business_engine, "begin", bounded_timeout)
            owner.rollback()
        assert expenses.create_expense(session, data).amount_paise == 120000


def test_pool_exhaustion_is_bounded_and_recovers(business_engine):
    # A private pool with one slot; do not alter the app's or another test's pool.
    pool = QueuePool(business_engine.raw_connection, pool_size=1, max_overflow=0, timeout=0.05)
    held = pool.connect()
    try:
        with pytest.raises(TimeoutError):
            pool.connect()
    finally:
        held.close()
    with pool.connect() as recovered:
        assert recovered.dbapi_connection is not None
    pool.dispose()
