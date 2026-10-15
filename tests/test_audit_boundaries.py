"""Adversarial boundary regressions: no rounding before validation."""

import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.money import rupees_to_paise
from app.models import Expense
from tests.test_business_api import expense_request


@pytest.mark.parametrize("value", ["1e-1000100", Decimal("1e-1000100")])
def test_underflowing_nonzero_share_is_not_silently_rounded(value):
    with pytest.raises(ValueError, match="fractional paise"):
        rupees_to_paise(value, allow_zero=True)


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("as_number", [False, True])
async def test_underflowing_custom_share_rejected_before_write(
    api, seed, business_engine, as_number
):
    data = expense_request(
        seed,
        amount="1",
        splits=[
            {"user_id": seed["payer"], "amount": "1"},
            {"user_id": seed["users"][1], "amount": "1e-1000100"},
        ],
    )
    body = json.dumps(data)
    if as_number:
        body = body.replace('"1e-1000100"', "1e-1000100")
    response = await api.post(
        "/expenses", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    with business_engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(Expense)) == 0


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw", ["1e999999999999999999999999999999", "0e-99999999999999999999999999999"]
)
async def test_unrepresentable_json_exponent_has_validation_response(api, seed, raw):
    body = json.dumps(expense_request(seed)).replace('"amount": 1200', '"amount": ' + raw)
    response = await api.post(
        "/expenses", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)
    assert len(response.content) < 512


@pytest.mark.postgres
@pytest.mark.anyio
async def test_large_suggestion_is_exact_and_can_be_reported_in_bounded_partials(api, seed):
    # The preview aggregates routes; the documented per-POST amount cap still applies.
    for _ in range(2):
        response = await api.post(
            "/expenses",
            json=expense_request(
                seed,
                amount="1000000000",
                splits=[{"user_id": seed["users"][1], "amount": "1000000000"}],
            ),
        )
        assert response.status_code == 201
    path = f"/groups/{seed['group']}"
    plan = (await api.get(path + "/settlement-suggestions")).json()
    assert len(plan["transfers"]) == 1
    transfer = plan["transfers"][0]
    assert transfer["amount_paise"] == 200_000_000_000
    assert transfer["amount"] == "2000000000.00"
    payload = {
        "from_user_id": transfer["from_user_id"],
        "to_user_id": transfer["to_user_id"],
        "amount": transfer["amount"],
    }
    assert (
        await api.post(
            path + "/settlements", json=payload, headers={"Idempotency-Key": "too-large"}
        )
    ).status_code == 422
    for key in ["part-one", "part-two"]:
        response = await api.post(
            path + "/settlements",
            json={**payload, "amount": "1000000000"},
            headers={"Idempotency-Key": key},
        )
        assert response.status_code == 201
    result = (await api.get(path + "/balances")).json()
    assert all(m["net_balance_paise"] == 0 for m in result["balances"])
    assert result["debts"] == []
    assert (await api.get(path + "/settlement-suggestions")).json()["transfers"] == []


@pytest.mark.postgres
@pytest.mark.anyio
async def test_openapi_explains_aggregate_routes_and_per_report_limit(api):
    schema = (await api.get("/openapi.json")).json()
    description = schema["paths"]["/groups/{group_id}/settlement-suggestions"]["get"]["description"]
    assert "1000000000.00" in description and "partial" in description
