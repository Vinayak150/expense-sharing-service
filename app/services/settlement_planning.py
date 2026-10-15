"""Exact advisory payments. Pairwise fallback and greedy netting are candidates, not an optimum."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.services.errors import AccountingIntegrityError

Strategy = Literal["netted_greedy", "pairwise", "component_netting"]


@dataclass(frozen=True, order=True)
class Transfer:
    from_user_id: int
    to_user_id: int
    amount_paise: int


@dataclass(frozen=True)
class SelectedPlan:
    strategy: Strategy
    transfers: tuple[Transfer, ...]


def _copy_balances(net_balances: Mapping[int, int]) -> dict[int, int]:
    if not isinstance(net_balances, Mapping) or any(
        type(user_id) is not int or user_id <= 0 or type(amount) is not int
        for user_id, amount in net_balances.items()
    ):
        raise AccountingIntegrityError("Balances require positive integer IDs and integer paise")
    balances = dict(net_balances)
    if sum(balances.values()) != 0:
        raise AccountingIntegrityError("Net balances do not conserve value")
    return balances


def validate_plan(net_balances: Mapping[int, int], transfers: Sequence[Transfer]) -> None:
    """Replay payments independently: sender += amount, receiver -= amount; all must end at zero.

    Intermediaries and zero-net participants are valid in the pairwise candidate. Duplicate
    directed pairs are not: each output entry represents one aggregated proposed payment.
    """
    remaining = _copy_balances(net_balances)
    if not isinstance(transfers, Sequence):
        raise AccountingIntegrityError("Plan must be a sequence of transfers")
    pairs = set()
    for transfer in transfers:
        if not isinstance(transfer, Transfer) or any(
            type(value) is not int
            for value in (transfer.from_user_id, transfer.to_user_id, transfer.amount_paise)
        ):
            raise AccountingIntegrityError("Malformed suggested transfer")
        sender, receiver, amount = (
            transfer.from_user_id,
            transfer.to_user_id,
            transfer.amount_paise,
        )
        if (
            sender not in remaining
            or receiver not in remaining
            or sender == receiver
            or amount <= 0
            or (sender, receiver) in pairs
        ):
            raise AccountingIntegrityError("Invalid suggested transfer")
        pairs.add((sender, receiver))
        remaining[sender] += amount
        remaining[receiver] -= amount
    if any(remaining.values()):
        raise AccountingIntegrityError("Suggested plan does not clear every balance")


def suggest_greedy_settlements(net_balances: Mapping[int, int]) -> tuple[Transfer, ...]:
    balances = _copy_balances(net_balances)
    debtors = sorted([u, -amount] for u, amount in balances.items() if amount < 0)
    creditors = sorted([u, amount] for u, amount in balances.items() if amount > 0)
    transfers = []
    debtor = creditor = 0
    while debtor < len(debtors) and creditor < len(creditors):
        sender, owes = debtors[debtor]
        receiver, receives = creditors[creditor]
        amount = min(owes, receives)
        transfers.append(Transfer(sender, receiver, amount))
        debtors[debtor][1] -= amount
        creditors[creditor][1] -= amount
        if debtors[debtor][1] == 0:
            debtor += 1
        if creditors[creditor][1] == 0:
            creditor += 1
    result = tuple(sorted(transfers))
    validate_plan(balances, result)
    return result


def select_plan(
    net_balances: Mapping[int, int],
    pairwise: Sequence[Transfer],
) -> SelectedPlan:
    # Validate BOTH candidates, even when the other candidate appears preferable.
    validate_plan(net_balances, pairwise)
    greedy = suggest_greedy_settlements(net_balances)
    validate_plan(net_balances, greedy)
    candidates = (
        SelectedPlan("netted_greedy", greedy),
        SelectedPlan("pairwise", tuple(sorted(pairwise))),
    )
    # Transfer dataclass ordering is exactly (from_user_id, to_user_id, amount_paise).
    # Identical canonical plans deterministically report netted_greedy (first candidate).
    return min(
        candidates,
        key=lambda plan: (
            len(plan.transfers),
            sum(t.amount_paise for t in plan.transfers),
            plan.transfers,
        ),
    )


def select_recordable_plan(
    net_balances: Mapping[int, int], pairwise: Sequence[Transfer]
) -> SelectedPlan:
    """Retain Phase 4 ranking for debtor-to-creditor routes; otherwise net weak components.

    An aggregated route above the HTTP per-report cap needs multiple partial reports.

    A connected component with V vertices has at least V-1 edges. Greedy on its N<=V
    nonzero positions uses <=N-1 edges. Thus component netting never exceeds the source
    pairwise count; comparing global greedy retains the global N-1 bound as well.
    """
    chosen = select_plan(net_balances, pairwise)
    if all(net_balances[t.from_user_id] < 0 < net_balances[t.to_user_id] for t in chosen.transfers):
        return chosen
    adjacency = {u: set() for u in net_balances}
    for t in pairwise:
        adjacency[t.from_user_id].add(t.to_user_id)
        adjacency[t.to_user_id].add(t.from_user_id)
    seen = set()
    component_transfers = []
    for root in sorted(adjacency):
        if root in seen:
            continue
        stack = [root]
        seen.add(root)
        members = []
        while stack:
            u = stack.pop()
            members.append(u)
            for v in adjacency[u] - seen:
                seen.add(v)
                stack.append(v)
        component_transfers.extend(
            suggest_greedy_settlements({u: net_balances[u] for u in members})
        )
    component = tuple(sorted(component_transfers))
    validate_plan(net_balances, component)
    global_plan = SelectedPlan("netted_greedy", suggest_greedy_settlements(net_balances))
    return min(
        (global_plan, SelectedPlan("component_netting", component)),
        key=lambda p: (len(p.transfers), sum(t.amount_paise for t in p.transfers), p.transfers),
    )
