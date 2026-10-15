from decimal import Decimal
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
)

from app.core.money import paise_to_rupees, rupees_to_paise

MAX_ID = 2**63 - 1
BodyID = Annotated[int, Field(strict=True, gt=0, le=MAX_ID)]
Name = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100)
]
Description = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)
]


def validate_rupees(value):
    return Decimal(paise_to_rupees(rupees_to_paise(value)))


Rupees = Annotated[
    Decimal,
    BeforeValidator(validate_rupees),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "number", "minimum": 0.01, "maximum": 1_000_000_000, "multipleOf": 0.01},
                {
                    "type": "string",
                    "maxLength": 128,
                    "pattern": r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$",
                },
            ]
        },
        mode="validation",
    ),
    Field(
        description="INR rupees, exact paise only; number or decimal string, max 1000000000.00",
        examples=[1200, "1200.50"],
    ),
]
RupeeString = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]{2}$", description="Exact INR rupees")]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NameInput(RequestModel):
    name: Name


class NamedResource(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class MemberInput(RequestModel):
    user_id: BodyID


class MemberOutput(BaseModel):
    user_id: int
    name: str


def validate_share_rupees(value):
    return Decimal(paise_to_rupees(rupees_to_paise(value, allow_zero=True)))


ShareRupees = Annotated[
    Decimal,
    BeforeValidator(validate_share_rupees),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "number", "minimum": 0, "maximum": 1_000_000_000, "multipleOf": 0.01},
                {
                    "type": "string",
                    "maxLength": 128,
                    "pattern": r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$",
                },
            ]
        },
        mode="validation",
    ),
    Field(description="Nonnegative INR rupees, exact paise; maximum 1000000000.00"),
]


class CustomSplitInput(RequestModel):
    user_id: BodyID
    amount: ShareRupees


class ExpenseInput(RequestModel):
    group_id: BodyID
    paid_by: BodyID
    amount: Rupees
    description: Description
    splits: list[CustomSplitInput] | None = Field(
        None,
        description=(
            "Omitted/null=equal all current members; nonempty custom subset may exclude payer"
        ),
    )


class SplitOutput(BaseModel):
    user_id: int
    name: str
    share_paise: int
    share_amount: RupeeString


class ExpenseOutput(BaseModel):
    id: int
    group_id: int
    paid_by: int
    paid_by_name: str
    amount: RupeeString
    amount_paise: int
    description: str
    created_at: AwareDatetime
    splits: list[SplitOutput]


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class ValidationIssue(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ValidationResponse(BaseModel):
    detail: list[ValidationIssue]
