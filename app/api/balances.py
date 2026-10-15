from typing import Annotated

from fastapi import APIRouter, Query

from app.api.business import GroupID, SessionDependency
from app.schemas.balances import GroupBalances
from app.schemas.business import MAX_ID, ErrorResponse, ValidationResponse
from app.services.balances import calculate_group_balances

router = APIRouter(
    tags=["balances"],
    responses={
        404: {"model": ErrorResponse},
        422: {"model": ValidationResponse},
        500: {"model": ErrorResponse},
    },
)


@router.get("/groups/{group_id}/balances", response_model=GroupBalances)
def group_balances(group_id: GroupID, session: SessionDependency) -> GroupBalances:
    """Exact ledger positions and pairwise debts; positive net means money to receive."""
    return calculate_group_balances(session, group_id)


@router.get("/balances", response_model=GroupBalances)
def balances(
    group_id: Annotated[int, Query(gt=0, le=MAX_ID)],
    session: SessionDependency,
) -> GroupBalances:
    """Assignment-compatible alias. An explicit group is required; no cross-group pooling."""
    return calculate_group_balances(session, group_id)
