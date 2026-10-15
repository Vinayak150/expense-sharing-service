from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Debt:
    from_user_id: int
    to_user_id: int
    amount_paise: int


def cancel_pairwise(obligations: Iterable[Debt]) -> list[Debt]:
    """Net only reverse obligations of the same pair; preserve cycles and intermediaries."""
    pairs: dict[tuple[int, int], int] = {}
    for debt in obligations:
        if (
            any(
                type(value) is not int
                for value in (debt.from_user_id, debt.to_user_id, debt.amount_paise)
            )
            or min(debt.from_user_id, debt.to_user_id) <= 0
            or debt.amount_paise < 0
        ):
            raise ValueError("Obligations require positive integer IDs and nonnegative paise")
        if debt.from_user_id == debt.to_user_id or debt.amount_paise == 0:
            continue
        low, high = sorted((debt.from_user_id, debt.to_user_id))
        sign = 1 if debt.from_user_id == low else -1
        pairs[low, high] = pairs.get((low, high), 0) + sign * debt.amount_paise
    return sorted(
        Debt(low, high, amount) if amount > 0 else Debt(high, low, -amount)
        for (low, high), amount in pairs.items()
        if amount
    )
