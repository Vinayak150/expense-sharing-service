from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.balances import router as balances_router
from app.api.business import router as business_router
from app.api.errors import register_error_handlers
from app.api.health import router as health_router
from app.api.settlements import router as settlements_router
from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configuration = settings if settings is not None else Settings()
        engine = create_database_engine(configuration)
        app.state.session_factory = create_session_factory(engine)
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="Expense Sharing Service",
        version="0.5.0",
        description="Exact expenses, custom splits, reported repayments and settlement previews.",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(business_router)
    app.include_router(balances_router)
    app.include_router(settlements_router)
    register_error_handlers(app)
    return app


app = create_app()
