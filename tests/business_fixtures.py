from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine

from alembic import command
from app.core.config import Settings
from app.main import create_app
from app.models import Group, GroupMember, User
from tests.conftest import migration_config


@pytest.fixture
def business_engine(database_factory) -> Iterator[Engine]:
    # API services commit; a separate disposable DB per test also allows real concurrency.
    with database_factory() as engine:
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        yield engine


@pytest.fixture
async def api(business_engine):
    app = create_app(
        Settings(
            database_url=business_engine.url.render_as_string(hide_password=False), _env_file=None
        )
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.fixture
def seed(business_engine):
    with business_engine.begin() as connection:
        users = [
            connection.scalar(User.__table__.insert().values(name=name).returning(User.id))
            for name in ["Mohit", "Rahul", "Ankit", "Outsider"]
        ]
        group = connection.scalar(Group.__table__.insert().values(name="Trip").returning(Group.id))
        connection.execute(
            GroupMember.__table__.insert(),
            [{"group_id": group, "user_id": user_id} for user_id in users[:3]],
        )
    return {"users": users, "group": group, "payer": users[0], "outsider": users[3]}
