from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.schemas.balances import Paise
from app.schemas.business import BodyID, RequestModel, Rupees, RupeeString
from app.services.settlement_planning import Strategy


class SuggestedTransfer(BaseModel):
    from_user_id: int
    from_user_name: str
    to_user_id: int
    to_user_name: str
    amount_paise: Annotated[int, Field(strict=True, gt=0)]
    amount: RupeeString


class SettlementSuggestions(BaseModel):
    group_id: int
    currency: Literal["INR"] = "INR"
    strategy: Strategy
    payments_recorded: Literal[False] = Field(
        False,
        deprecated=True,
        description="This GET records no payments; not a claim about historical repayments",
    )
    suggestions_recorded: Literal[False] = False
    has_recorded_settlements: bool = False
    debt_basis: Literal["expense_pairwise", "net_after_repayments"] = "expense_pairwise"
    pairwise_transfer_count: Annotated[
        int,
        Field(
            strict=True,
            ge=0,
            description=(
                "Count of current debt_basis baseline, "
                "not historical expense edges after repayments"
            ),
        ),
    ]
    suggested_transfer_count: Annotated[int, Field(strict=True, ge=0)]
    transfers_avoided: Annotated[int, Field(strict=True, ge=0)]
    total_suggested_transfer_paise: Paise
    total_suggested_transfer_amount: RupeeString
    transfers: list[SuggestedTransfer]


class SettlementInput(RequestModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"from_user_id": 2, "to_user_id": 1, "amount": "150.00"}]}
    )
    from_user_id: BodyID
    to_user_id: BodyID
    amount: Rupees


class SettlementOutput(BaseModel):
    id: int
    group_id: int
    from_user_id: int
    from_user_name: str
    to_user_id: int
    to_user_name: str
    amount_paise: Annotated[int, Field(strict=True, gt=0, description="Positive INR paise")]
    amount: RupeeString
    idempotency_key: str
    created_at: AwareDatetime
    status: Literal["payment_reported"] = "payment_reported"
    payment_verified: Literal[False] = False
