from sqlalchemy.orm import Session

from app.core.money import paise_to_rupees
from app.schemas.settlements import SettlementSuggestions, SuggestedTransfer
from app.services import balances
from app.services.settlement_planning import Transfer, select_recordable_plan


def suggest_group_settlements(session: Session, group_id: int) -> SettlementSuggestions:
    # Exactly one call owns the repeatable-read/read-only transaction. Both financial
    # views and names are detached facts from that same snapshot; no further SQL follows.
    snapshot = balances.calculate_group_balances(session, group_id)
    positions = {member.user_id: member.net_balance_paise for member in snapshot.balances}
    names = {member.user_id: member.name for member in snapshot.balances}
    plan = select_recordable_plan(
        positions,
        [
            Transfer(debt.from_user_id, debt.to_user_id, debt.amount_paise)
            for debt in snapshot.debts
        ],
    )
    total = sum(transfer.amount_paise for transfer in plan.transfers)
    return SettlementSuggestions(
        group_id=group_id,
        debt_basis=snapshot.debt_basis,
        has_recorded_settlements=snapshot.debt_basis == "net_after_repayments",
        strategy=plan.strategy,
        pairwise_transfer_count=len(snapshot.debts),
        suggested_transfer_count=len(plan.transfers),
        transfers_avoided=len(snapshot.debts) - len(plan.transfers),
        total_suggested_transfer_paise=total,
        total_suggested_transfer_amount=paise_to_rupees(total),
        transfers=[
            SuggestedTransfer(
                from_user_id=t.from_user_id,
                from_user_name=names[t.from_user_id],
                to_user_id=t.to_user_id,
                to_user_name=names[t.to_user_id],
                amount_paise=t.amount_paise,
                amount=paise_to_rupees(t.amount_paise),
            )
            for t in plan.transfers
        ],
    )
