from typing import Annotated

from fastapi import APIRouter, Header, Response

from app.api.business import GroupID, SessionDependency
from app.api.decimal_route import DecimalRoute
from app.schemas.business import ErrorResponse, ValidationResponse
from app.schemas.settlements import SettlementInput, SettlementOutput, SettlementSuggestions
from app.services import repayments
from app.services.settlements import suggest_group_settlements

router = APIRouter(
    tags=["settlements"],
    route_class=DecimalRoute,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse | ValidationResponse},
        500: {"model": ErrorResponse},
    },
)


@router.get("/groups/{group_id}/settlement-suggestions", response_model=SettlementSuggestions)
def settlement_suggestions(group_id: GroupID, session: SessionDependency) -> SettlementSuggestions:
    """Advisory payment plan. Suggestions are not recorded repayments and do not modify balances.

    Compares pairwise and ID-ordered greedy plans; no claim of globally minimum transfers.
    Transfers aggregate routes: amounts above INR 1000000000.00 require multiple partial
    settlement reports, each with a distinct key. Counts describe routes, not POST requests.
    """
    return suggest_group_settlements(session, group_id)


@router.post(
    "/groups/{group_id}/settlements",
    response_model=SettlementOutput,
    status_code=201,
    responses={200: {"model": SettlementOutput, "description": "Idempotent replay"}},
)
def create_settlement(
    group_id: GroupID,
    data: SettlementInput,
    session: SessionDependency,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
            description="Required group-scoped retry key",
        ),
    ],
):
    """Record a user-reported repayment, not a verified bank transfer. Replay=200; new=201."""
    result, created = repayments.record_settlement(session, group_id, data, idempotency_key)
    response.status_code = 201 if created else 200
    return result


@router.get("/groups/{group_id}/settlements", response_model=list[SettlementOutput])
def list_settlements(group_id: GroupID, session: SessionDependency):
    """User-reported repayment history, newest creation time/ID first; no bank verification."""
    return repayments.list_settlements(session, group_id)
