"""Preserve JSON numeric tokens before request validation; never pass money through float."""

import json
from collections.abc import Callable, Coroutine
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute


def _parse_integer(token: str) -> int | Decimal:
    # Legal IDs and amounts fit in 20 characters. Oversized tokens must still
    # reach field validation, without changing Python's global int-string limit.
    return int(token) if len(token) <= 20 else Decimal(token)


def _parse_decimal(token: str) -> Decimal:
    try:
        return Decimal(token)
    except InvalidOperation:
        # A syntactically valid JSON number may exceed Decimal's exponent range.
        # Use the same sanitized 422 schema as other JSON validation failures.
        raise json.JSONDecodeError("Number exponent is out of range", token, 0) from None


class DecimalRequest(Request):
    async def json(self) -> Any:
        if not hasattr(self, "_json"):
            self._json = json.loads(
                await self.body(),
                parse_float=_parse_decimal,
                parse_int=_parse_integer,
                parse_constant=Decimal,
            )
        return self._json


class DecimalRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def decimal_handler(request: Request) -> Response:
            return await handler(DecimalRequest(request.scope, request.receive))

        return decimal_handler
