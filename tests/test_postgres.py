from datetime import timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import BigInteger, DateTime, inspect, select, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.core.database import create_session_factory
from app.models import Base, Expense, ExpenseSplit, GroupMember, User
from tests.conftest import migration_config

pytestmark = pytest.mark.postgres


def test_migration_matches_metadata_and_catalog(db):
    assert compare_metadata(MigrationContext.configure(db), Base.metadata) == []
    inspector = inspect(db)
    assert set(inspector.get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
    for table in Base.metadata.sorted_tables:
        pk = inspector.get_pk_constraint(table.name)
        assert pk["name"] == table.primary_key.name
        assert pk["constrained_columns"] == [c.name for c in table.primary_key.columns]
        checks = inspector.get_check_constraints(table.name)
        assert {c["name"] for c in checks} == {
            c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"
        }
        fks = inspector.get_foreign_keys(table.name)
        assert {c["name"] for c in fks} == {c.name for c in table.foreign_key_constraints}
        assert all(c["options"].get("ondelete") == "RESTRICT" for c in fks)
        for column in inspector.get_columns(table.name):
            assert not column["nullable"]
            if column["name"].endswith("_at"):
                assert isinstance(column["type"], DateTime) and column["type"].timezone
                assert column["default"] == "now()"
            elif column["name"] != "name" and column["name"] not in (
                "description",
                "idempotency_key",
            ):
                assert isinstance(column["type"], BigInteger)
    index = inspector.get_indexes("expenses")
    assert len(index) == 1
    assert index[0]["name"] == "ix_expenses_group_history"
    assert index[0]["column_names"] == ["group_id", "created_at", "id"]
    assert index[0]["column_sorting"] == {"created_at": ("desc",), "id": ("desc",)}


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (
            "INSERT INTO group_members(group_id,user_id) VALUES (:group,:payer)",
            "pk_group_members",
        ),
        (
            "INSERT INTO expenses(group_id,paid_by,amount_paise,description) "
            "VALUES (:group,:outsider,100,'Meal')",
            "fk_expenses_group_payer_membership",
        ),
        (
            "INSERT INTO expense_splits(expense_id,user_id,share_paise) VALUES (-1,:payer,0)",
            "fk_expense_splits_expense_id_expenses",
        ),
        (
            "INSERT INTO expense_splits(expense_id,user_id,share_paise) VALUES (:expense,-1,0)",
            "fk_expense_splits_user_id_users",
        ),
        (
            "INSERT INTO expense_splits(expense_id,user_id,share_paise) VALUES (:expense,:payer,0)",
            "pk_expense_splits",
        ),
        (
            "UPDATE expenses SET amount_paise=0 WHERE id=:expense",
            "ck_expenses_amount_positive",
        ),
        (
            "UPDATE expenses SET amount_paise=-1 WHERE id=:expense",
            "ck_expenses_amount_positive",
        ),
        (
            "UPDATE expense_splits SET share_paise=-1 WHERE expense_id=:expense",
            "ck_expense_splits_share_nonnegative",
        ),
        ("UPDATE users SET name=E' \\t\\n' WHERE id=:payer", "ck_users_name_nonblank"),
        ("UPDATE groups SET name='' WHERE id=:group", "ck_groups_name_nonblank"),
        (
            "UPDATE expenses SET description=E'\\t' WHERE id=:expense",
            "ck_expenses_description_nonblank",
        ),
    ],
)
def test_database_rejects_invalid_rows(db, ledger, statement, constraint):
    with pytest.raises(IntegrityError) as error, db.begin_nested():
        db.execute(text(statement), ledger)
    assert error.value.orig.diag.constraint_name == constraint


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM users WHERE id=:payer",
        "DELETE FROM groups WHERE id=:group",
        "DELETE FROM group_members WHERE group_id=:group AND user_id=:payer",
        "DELETE FROM expenses WHERE id=:expense",
    ],
)
def test_restrictive_deletes_preserve_history(db, ledger, statement):
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text(statement), ledger)
    assert db.scalar(text("SELECT count(*) FROM expenses WHERE id=:expense"), ledger) == 1
    assert (
        db.scalar(text("SELECT count(*) FROM expense_splits WHERE expense_id=:expense"), ledger)
        == 2
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE users SET name=NULL WHERE id=:payer",
        "UPDATE groups SET name=repeat('x',101) WHERE id=:group",
        "UPDATE expenses SET description=repeat('x',501) WHERE id=:expense",
        "UPDATE expenses SET amount_paise=9223372036854775808 WHERE id=:expense",
    ],
)
def test_required_fields_lengths_and_bigint_range(db, ledger, statement):
    from sqlalchemy.exc import DataError

    with pytest.raises((IntegrityError, DataError)), db.begin_nested():
        db.execute(text(statement), ledger)


def test_duplicate_names_utc_defaults_and_snapshot_after_join(db, ledger):
    rows = db.execute(select(User.__table__)).mappings().all()
    assert len({r["id"] for r in rows}) == 3
    assert [r["name"] for r in rows].count("Same name") == 2
    assert all(r["created_at"].utcoffset() == timedelta(0) for r in rows)
    joined_at = db.scalar(select(GroupMember.joined_at))
    assert joined_at.utcoffset() == timedelta(0)
    created_at = db.scalar(select(Expense.created_at))
    assert created_at.utcoffset() == timedelta(0)
    before = db.execute(select(ExpenseSplit.__table__)).all()
    db.execute(
        GroupMember.__table__.insert().values(group_id=ledger["group"], user_id=ledger["outsider"])
    )
    assert db.execute(select(ExpenseSplit.__table__)).all() == before


def test_cross_row_limits_are_explicit_not_false_database_guarantees(db, ledger):
    # This valid SQL intentionally violates the future service contract: an outsider
    # can get a split, and persisted totals need not match without service checks.
    db.execute(
        ExpenseSplit.__table__.insert().values(
            expense_id=ledger["expense"], user_id=ledger["outsider"], share_paise=1
        )
    )
    assert db.scalar(text("SELECT sum(share_paise) FROM expense_splits")) == 101
    db.execute(text("UPDATE expense_splits SET share_paise=0"))
    assert db.scalar(text("SELECT sum(share_paise) FROM expense_splits")) == 0


def test_session_ownership_commits_rolls_back_and_closes_uncommitted_work(pg_engine):
    factory = create_session_factory(pg_engine)
    committed_id = None
    try:
        with factory() as session, session.begin():
            user = User(name="Committed")
            session.add(user)
            session.flush()
            committed_id = user.id
        with pytest.raises(RuntimeError), factory() as session, session.begin():
            rolled_back = User(name="Rolled back")
            session.add(rolled_back)
            session.flush()
            rolled_back_id = rolled_back.id
            raise RuntimeError("Abort transaction")
        with factory() as session:
            unfinished = User(name="No commit")
            session.add(unfinished)
            session.flush()
            unfinished_id = unfinished.id
        with factory() as session:
            assert session.get(User, committed_id).name == "Committed"
            assert session.get(User, rolled_back_id) is None
            assert session.get(User, unfinished_id) is None
    finally:
        with factory() as session, session.begin():
            if committed_id is not None:
                session.execute(User.__table__.delete().where(User.id == committed_id))


def test_migration_downgrade_upgrade_on_disposable_database(database_factory):
    with database_factory() as engine:
        with engine.begin() as connection:
            config = migration_config(connection)
            command.upgrade(config, "head")
            connection.execute(User.__table__.insert().values(name="Disposable data"))
            command.downgrade(config, "base")
            assert set(inspect(connection).get_table_names()) == {"alembic_version"}
            command.upgrade(config, "head")
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
            assert connection.scalar(select(User.id)) is None


@pytest.mark.anyio
async def test_real_application_readiness(pg_engine):
    from httpx import ASGITransport, AsyncClient

    from app.core.config import Settings
    from app.main import create_app

    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False), _env_file=None
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/health/ready")).status_code == 200
