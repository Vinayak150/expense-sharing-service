from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin


class User(CreatedAtMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("name ~ '[^[:space:]]'", name="name_nonblank"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)


class Group(CreatedAtMixin, Base):
    __tablename__ = "groups"
    __table_args__ = (CheckConstraint("name ~ '[^[:space:]]'", name="name_nonblank"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Expense(CreatedAtMixin, Base):
    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        CheckConstraint("description ~ '[^[:space:]]'", name="description_nonblank"),
        ForeignKeyConstraint(
            ["group_id", "paid_by"],
            ["group_members.group_id", "group_members.user_id"],
            name="fk_expenses_group_payer_membership",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id", ondelete="RESTRICT"), nullable=False
    )
    paid_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)


Index("ix_expenses_group_history", Expense.group_id, Expense.created_at.desc(), Expense.id.desc())


class ExpenseSplit(Base):
    __tablename__ = "expense_splits"
    __table_args__ = (CheckConstraint("share_paise >= 0", name="share_nonnegative"),)

    expense_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("expenses.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    share_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Settlement(CreatedAtMixin, Base):
    __tablename__ = "settlements"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        CheckConstraint("from_user_id <> to_user_id", name="participants_distinct"),
        CheckConstraint("idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'", name="key_valid"),
        ForeignKeyConstraint(
            ["group_id", "from_user_id"],
            ["group_members.group_id", "group_members.user_id"],
            name="fk_settlements_group_sender_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["group_id", "to_user_id"],
            ["group_members.group_id", "group_members.user_id"],
            name="fk_settlements_group_receiver_membership",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("group_id", "idempotency_key", name="uq_settlements_group_key"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id", ondelete="RESTRICT"), nullable=False
    )
    from_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)


Index(
    "ix_settlements_group_history",
    Settlement.group_id,
    Settlement.created_at.desc(),
    Settlement.id.desc(),
)
