from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from app.api.decimal_route import DecimalRoute
from app.core.database import get_session
from app.schemas.business import (
    MAX_ID,
    ErrorResponse,
    ExpenseInput,
    ExpenseOutput,
    MemberInput,
    MemberOutput,
    NamedResource,
    NameInput,
    ValidationResponse,
)
from app.services import expenses, groups, users

SessionDependency = Annotated[Session, Depends(get_session)]
GroupID = Annotated[int, Path(gt=0, le=MAX_ID)]
router = APIRouter(
    route_class=DecimalRoute,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse | ValidationResponse},
        500: {"model": ErrorResponse},
    },
)


@router.post("/users", response_model=NamedResource, status_code=201, tags=["users"])
def create_user(data: NameInput, session: SessionDependency) -> NamedResource:
    return users.create_user(session, data.name)


@router.get("/users", response_model=list[NamedResource], tags=["users"])
def list_users(session: SessionDependency) -> list[NamedResource]:
    return users.list_users(session)


@router.post("/groups", response_model=NamedResource, status_code=201, tags=["groups"])
def create_group(data: NameInput, session: SessionDependency) -> NamedResource:
    return groups.create_group(session, data.name)


@router.post(
    "/groups/{group_id}/members", response_model=MemberOutput, status_code=201, tags=["groups"]
)
def add_member(group_id: GroupID, data: MemberInput, session: SessionDependency) -> MemberOutput:
    return groups.add_member(session, group_id, data.user_id)


@router.get("/groups/{group_id}/members", response_model=list[MemberOutput], tags=["groups"])
def list_members(group_id: GroupID, session: SessionDependency) -> list[MemberOutput]:
    return groups.list_members(session, group_id)


@router.post("/expenses", response_model=ExpenseOutput, status_code=201, tags=["expenses"])
def create_expense(data: ExpenseInput, session: SessionDependency) -> ExpenseOutput:
    return expenses.create_expense(session, data)


@router.get("/groups/{group_id}/expenses", response_model=list[ExpenseOutput], tags=["expenses"])
def list_expenses(group_id: GroupID, session: SessionDependency) -> list[ExpenseOutput]:
    return expenses.list_expenses(session, group_id)
