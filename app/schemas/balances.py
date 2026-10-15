from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.schemas.business import RupeeString

Paise = Annotated[int, Field(strict=True, ge=0, description="Exact integer INR paise")]
SignedRupees = Annotated[str, Field(pattern=r"^-?[0-9]+\.[0-9]{2}$")]


class MemberBalance(BaseModel):
    user_id: int
    name: str
    total_paid_paise: Paise
    total_paid_amount: RupeeString
    total_share_paise: Paise
    total_share_amount: RupeeString
    total_settlements_sent_paise: Paise = 0
    total_settlements_received_paise: Paise = 0
    net_balance_paise: Annotated[
        int,
        Field(
            strict=True,
            description="Outstanding INR paise after recorded repayments; positive=receive",
        ),
    ]
    net_balance_amount: SignedRupees


class BilateralDebt(BaseModel):
    from_user_id: int
    from_user_name: str
    to_user_id: int
    to_user_name: str
    amount_paise: Annotated[int, Field(strict=True, gt=0)]
    amount: RupeeString


class GroupBalances(BaseModel):
    group_id: int
    currency: Literal["INR"] = "INR"
    total_expenses_paise: Paise
    total_expenses_amount: RupeeString
    debt_basis: Literal["expense_pairwise", "net_after_repayments"] = Field(
        "expense_pairwise",
        description=(
            "Expense pair obligations before repayments; "
            "canonical net-position plan after any repayment"
        ),
    )
    balances: list[MemberBalance]
    debts: list[BilateralDebt]
