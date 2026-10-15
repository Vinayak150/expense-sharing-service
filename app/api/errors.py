import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.services.errors import (
    AccountingIntegrityError,
    Conflict,
    DomainError,
    InvalidExpense,
    InvalidSettlement,
    NotFound,
)

logger = logging.getLogger(__name__)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError) -> JSONResponse:
        status = {NotFound: 404, Conflict: 409, InvalidExpense: 422, InvalidSettlement: 422}[
            type(error)
        ]
        return JSONResponse(
            status_code=status, content={"detail": {"code": error.code, "message": error.message}}
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # Avoid echoing raw inputs/exception context (including NaN, arbitrary payloads
        # and potentially private data) into JSON responses.
        issues = [{key: issue[key] for key in ("loc", "msg", "type")} for issue in error.errors()]
        return JSONResponse(status_code=422, content={"detail": issues})

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        # SQLAlchemy exception strings can embed SQL parameters. Log the error class
        # without request bodies, database credentials, statements or parameter values.
        logger.error("Request failed: %s", type(error).__name__)
        return JSONResponse(
            status_code=500,
            content={"detail": {"code": "internal_error", "message": "Internal server error"}},
        )

    # A specific handler is handled by ExceptionMiddleware instead of being
    # re-raised by ServerErrorMiddleware for an external server traceback.
    app.add_exception_handler(SQLAlchemyError, unexpected_error)
    app.add_exception_handler(AccountingIntegrityError, unexpected_error)
