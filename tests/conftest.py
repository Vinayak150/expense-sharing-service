import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine

from alembic import command
from app.core.config import Settings
from app.core.database import create_database_engine
from app.models import Expense, ExpenseSplit, Group, GroupMember, User

ROOT = Path(__file__).resolve().parents[1]
pytest_plugins = ["tests.business_fixtures"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def migration_config(connection: Connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture(scope="session")
def database_factory():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to run real PostgreSQL integration tests")
    admin = create_database_engine(Settings(database_url=database_url, _env_file=None))

    @contextmanager
    def create() -> Iterator[Engine]:
        # Identifier is generated locally, never interpolated from user input.
        name = f"expense_test_{uuid4().hex}"
        engine = None
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        try:
            url = admin.url.set(database=name).render_as_string(hide_password=False)
            engine = create_database_engine(Settings(database_url=url, _env_file=None))
            yield engine
        finally:
            if engine is not None:
                engine.dispose()
            with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')

    yield create
    admin.dispose()


@pytest.fixture(scope="session")
def pg_engine(database_factory) -> Iterator[Engine]:
    with database_factory() as engine:
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        yield engine


@pytest.fixture
def db(pg_engine: Engine) -> Iterator[Connection]:
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def ledger(db: Connection) -> dict[str, int]:
    def insert_id(model, **values):
        return db.scalar(model.__table__.insert().values(**values).returning(model.id))

    payer = insert_id(User, name="Same name")
    participant = insert_id(User, name="Same name")
    outsider = insert_id(User, name="Outsider")
    group = insert_id(Group, name="Trip")
    other_group = insert_id(Group, name="Trip")
    db.execute(
        GroupMember.__table__.insert(),
        [
            {"group_id": group, "user_id": payer},
            {"group_id": group, "user_id": participant},
            {"group_id": other_group, "user_id": outsider},
        ],
    )
    expense = insert_id(
        Expense, group_id=group, paid_by=payer, amount_paise=100, description="Dinner"
    )
    db.execute(
        ExpenseSplit.__table__.insert(),
        [
            {"expense_id": expense, "user_id": payer, "share_paise": 50},
            {"expense_id": expense, "user_id": participant, "share_paise": 50},
        ],
    )
    return {
        "payer": payer,
        "participant": participant,
        "outsider": outsider,
        "group": group,
        "other_group": other_group,
        "expense": expense,
    }
