"""Group-filtered exact aggregates. No commits or ORM graph loading."""

from sqlalchemy import and_, func, literal, or_, select, union_all

from app.models import Expense, ExpenseSplit, GroupMember, Settlement, User


def invalid_expense_statement(group_id: int):
    # Outer join detects expenses without splits, not just mismatched existing rows.
    return (
        select(Expense.id)
        .outerjoin(ExpenseSplit, ExpenseSplit.expense_id == Expense.id)
        .outerjoin(
            GroupMember,
            and_(
                GroupMember.group_id == Expense.group_id,
                GroupMember.user_id == ExpenseSplit.user_id,
            ),
        )
        .where(Expense.group_id == group_id)
        .group_by(Expense.id)
        .having(
            or_(
                func.coalesce(func.sum(ExpenseSplit.share_paise), 0) != Expense.amount_paise,
                func.bool_or(GroupMember.user_id.is_(None)),
            )
        )
        .limit(1)
    )


def member_totals_statement(group_id: int):
    contributions = union_all(
        select(
            Expense.paid_by.label("user_id"),
            Expense.amount_paise.label("paid"),
            literal(0).label("share"),
            literal(0).label("sent"),
            literal(0).label("received"),
        ).where(Expense.group_id == group_id),
        select(ExpenseSplit.user_id, literal(0), ExpenseSplit.share_paise, literal(0), literal(0))
        .join(Expense, Expense.id == ExpenseSplit.expense_id)
        .where(Expense.group_id == group_id),
        select(
            Settlement.from_user_id, literal(0), literal(0), Settlement.amount_paise, literal(0)
        ).where(Settlement.group_id == group_id),
        select(
            Settlement.to_user_id, literal(0), literal(0), literal(0), Settlement.amount_paise
        ).where(Settlement.group_id == group_id),
    ).cte("contributions")
    totals = (
        select(
            contributions.c.user_id,
            func.sum(contributions.c.paid).label("paid"),
            func.sum(contributions.c.share).label("share"),
            func.sum(contributions.c.sent).label("sent"),
            func.sum(contributions.c.received).label("received"),
        )
        .group_by(contributions.c.user_id)
        .cte("totals")
    )
    return (
        select(
            User.id,
            User.name,
            func.coalesce(totals.c.paid, 0).label("paid"),
            func.coalesce(totals.c.share, 0).label("share"),
            func.coalesce(totals.c.sent, 0).label("sent"),
            func.coalesce(totals.c.received, 0).label("received"),
        )
        .join(GroupMember, GroupMember.user_id == User.id)
        .outerjoin(totals, totals.c.user_id == User.id)
        .where(GroupMember.group_id == group_id)
        .order_by(User.id)
    )


def obligations_statement(group_id: int):
    return (
        select(
            ExpenseSplit.user_id,
            Expense.paid_by,
            func.sum(ExpenseSplit.share_paise).label("amount"),
        )
        .join(Expense, Expense.id == ExpenseSplit.expense_id)
        .where(Expense.group_id == group_id, ExpenseSplit.user_id != Expense.paid_by)
        .group_by(ExpenseSplit.user_id, Expense.paid_by)
    )
