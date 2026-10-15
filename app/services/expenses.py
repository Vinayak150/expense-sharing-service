from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.money import paise_to_rupees, rupees_to_paise
from app.models import Expense, ExpenseSplit, Group, GroupMember, User
from app.schemas.business import ExpenseInput, ExpenseOutput, SplitOutput
from app.services import groups
from app.services.errors import InvalidExpense, NotFound
from app.services.splitting import equal_split, validate_custom_split


def expense_output(expense: Expense, payer_name: str, splits: list[SplitOutput]) -> ExpenseOutput:
    return ExpenseOutput(
        id=expense.id,
        group_id=expense.group_id,
        paid_by=expense.paid_by,
        paid_by_name=payer_name,
        amount=paise_to_rupees(expense.amount_paise),
        amount_paise=expense.amount_paise,
        description=expense.description,
        created_at=expense.created_at,
        splits=splits,
    )


def create_expense(session: Session, data: ExpenseInput) -> ExpenseOutput:
    amount_paise = rupees_to_paise(data.amount)
    with session.begin():
        groups.lock_group(session, data.group_id)
        # Separate statement AFTER acquiring the lock: READ COMMITTED observes the
        # membership committed by any writer for which the lock had to wait.
        members = session.execute(
            select(User.id, User.name)
            .join(GroupMember, GroupMember.user_id == User.id)
            .where(GroupMember.group_id == data.group_id)
            .order_by(User.id)
        ).all()
        if not members:
            raise InvalidExpense("empty_group", "Cannot record an expense for an empty group")
        payer = session.get(User, data.paid_by)
        if payer is None:
            raise NotFound("user_not_found", "Payer does not exist")
        if data.paid_by not in {member.id for member in members}:
            raise InvalidExpense("payer_not_member", "Payer must belong to the expense group")
        member_ids = [member.id for member in members]
        if data.splits is None:
            allocations = equal_split(amount_paise, member_ids)
        else:
            try:
                allocations = validate_custom_split(
                    amount_paise,
                    [(s.user_id, rupees_to_paise(s.amount, allow_zero=True)) for s in data.splits],
                    member_ids,
                    data.paid_by,
                )
            except ValueError as error:
                raise InvalidExpense("invalid_splits", str(error)) from None
        if sum(allocations.values()) != amount_paise:
            raise RuntimeError("Allocation invariant violated")
        expense = Expense(
            group_id=data.group_id,
            paid_by=data.paid_by,
            amount_paise=amount_paise,
            description=data.description,
        )
        session.add(expense)
        session.flush()
        persisted = [
            ExpenseSplit(expense_id=expense.id, user_id=user_id, share_paise=share)
            for user_id, share in allocations.items()
        ]
        session.add_all(persisted)
        session.flush()
        names = {member.id: member.name for member in members}
        splits = [
            SplitOutput(
                user_id=split.user_id,
                name=names[split.user_id],
                share_paise=split.share_paise,
                share_amount=paise_to_rupees(split.share_paise),
            )
            for split in persisted
        ]
        result = expense_output(expense, payer.name, splits)
    # Context has committed successfully before the router can return HTTP 201.
    return result


def list_expenses(session: Session, group_id: int) -> list[ExpenseOutput]:
    with session.begin():
        if session.get(Group, group_id) is None:
            raise NotFound("group_not_found", "Group does not exist")
        expenses = session.execute(
            select(Expense, User.name)
            .join(User, User.id == Expense.paid_by)
            .where(Expense.group_id == group_id)
            .order_by(Expense.created_at.desc(), Expense.id.desc())
        ).all()
        splits: dict[int, list[SplitOutput]] = defaultdict(list)
        rows = session.execute(
            select(ExpenseSplit, User.name)
            .join(User, User.id == ExpenseSplit.user_id)
            .join(Expense, Expense.id == ExpenseSplit.expense_id)
            .where(Expense.group_id == group_id)
            .order_by(ExpenseSplit.expense_id, ExpenseSplit.user_id)
        )
        for split, name in rows:
            splits[split.expense_id].append(
                SplitOutput(
                    user_id=split.user_id,
                    name=name,
                    share_paise=split.share_paise,
                    share_amount=paise_to_rupees(split.share_paise),
                )
            )
        return [expense_output(expense, name, splits[expense.id]) for expense, name in expenses]
