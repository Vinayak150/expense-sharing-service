from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.money import paise_to_rupees
from app.models import Group
from app.schemas.balances import BilateralDebt, GroupBalances, MemberBalance
from app.services.bilateral import Debt, cancel_pairwise
from app.services.errors import AccountingIntegrityError, NotFound
from app.services.ledger_queries import (
    invalid_expense_statement,
    member_totals_statement,
    obligations_statement,
)
from app.services.settlement_planning import suggest_greedy_settlements


def calculate_group_balances(session: Session, group_id: int) -> GroupBalances:
    with session.begin():
        # Must precede the first SQL statement. Reset by SQLAlchemy on pool return;
        # write services retain READ COMMITTED and read/write transactions.
        session.connection(
            execution_options={
                "isolation_level": "REPEATABLE READ",
                "postgresql_readonly": True,
            }
        )
        result = calculate_in_transaction(session, group_id)
    return result


def calculate_in_transaction(session: Session, group_id: int) -> GroupBalances:
    """No transaction ownership. Caller must hold RR snapshot OR the group write lock in RC."""
    if session.scalar(select(Group.id).where(Group.id == group_id)) is None:
        raise NotFound("group_not_found", "Group does not exist")
    if session.scalar(invalid_expense_statement(group_id)) is not None:
        raise AccountingIntegrityError()
    members = []
    for row in session.execute(member_totals_statement(group_id)):
        # PostgreSQL SUM(bigint) yields exact NUMERIC/Decimal, never float.
        paid, share = int(row.paid), int(row.share)
        sent, received = int(row.sent), int(row.received)
        net = paid - share + sent - received
        members.append(
            MemberBalance(
                user_id=row.id,
                name=row.name,
                total_paid_paise=paid,
                total_paid_amount=paise_to_rupees(paid),
                total_share_paise=share,
                total_share_amount=paise_to_rupees(share),
                total_settlements_sent_paise=sent,
                total_settlements_received_paise=received,
                net_balance_paise=net,
                net_balance_amount=("-" if net < 0 else "") + paise_to_rupees(abs(net)),
            )
        )
    total_sent = sum(member.total_settlements_sent_paise for member in members)
    total_received = sum(member.total_settlements_received_paise for member in members)
    if total_sent != total_received:
        raise AccountingIntegrityError()
    if total_sent:
        debts = [
            Debt(t.from_user_id, t.to_user_id, t.amount_paise)
            for t in suggest_greedy_settlements({m.user_id: m.net_balance_paise for m in members})
        ]
    else:
        debts = cancel_pairwise(
            Debt(row.user_id, row.paid_by, int(row.amount))
            for row in session.execute(obligations_statement(group_id))
        )
    names = {member.user_id: member.name for member in members}
    positions = dict.fromkeys(names, 0)
    for debt in debts:
        if debt.from_user_id not in names or debt.to_user_id not in names:
            raise AccountingIntegrityError()
        positions[debt.from_user_id] -= debt.amount_paise
        positions[debt.to_user_id] += debt.amount_paise
    total = sum(member.total_paid_paise for member in members)
    if total != sum(member.total_share_paise for member in members) or any(
        positions[member.user_id] != member.net_balance_paise for member in members
    ):
        raise AccountingIntegrityError()
    result = GroupBalances(
        group_id=group_id,
        debt_basis="net_after_repayments" if total_sent else "expense_pairwise",
        total_expenses_paise=total,
        total_expenses_amount=paise_to_rupees(total),
        balances=members,
        debts=[
            BilateralDebt(
                from_user_id=debt.from_user_id,
                from_user_name=names[debt.from_user_id],
                to_user_id=debt.to_user_id,
                to_user_name=names[debt.to_user_id],
                amount_paise=debt.amount_paise,
                amount=paise_to_rupees(debt.amount_paise),
            )
            for debt in debts
        ],
    )
    return result
