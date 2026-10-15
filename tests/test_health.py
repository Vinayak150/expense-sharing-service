from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_session
from app.main import create_app

pytestmark = pytest.mark.anyio


@pytest.fixture
def app():
    return create_app(
        Settings(database_url="postgresql+psycopg://demo:demo@localhost/demo", _env_file=None)
    )


async def test_liveness_and_openapi_without_database(app):
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/redoc")).status_code == 200
        schema = await client.get("/openapi.json")
        assert schema.status_code == 200
        assert {"/health", "/health/ready"}.issubset(schema.json()["paths"])
        assert {"/balances", "/groups/{group_id}/balances"}.issubset(schema.json()["paths"])
        assert (await client.post("/users", json={"name": " "})).status_code == 422


@pytest.mark.parametrize("available", [True, False])
async def test_readiness_result_and_no_connection_detail_leak(app, available):
    session = Mock(spec=Session)
    if not available:
        session.execute.side_effect = OperationalError("private connection info", {}, Exception())
    app.dependency_overrides[get_session] = lambda: session
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/health/ready")
    assert response.status_code == (200 if available else 503)
    assert response.json() == (
        {"status": "ok"} if available else {"detail": "Database unavailable"}
    )
    session.execute.assert_called_once()
