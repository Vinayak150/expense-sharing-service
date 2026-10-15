"""Create the five core tables with restrictive financial references.

Revision ID: 0001_foundation
Revises: None
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.CheckConstraint("name ~ '[^[:space:]]'", name=op.f("ck_users_name_nonblank")),
    )
    op.create_table(
        "groups",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_groups"),
        sa.CheckConstraint("name ~ '[^[:space:]]'", name=op.f("ck_groups_name_nonblank")),
    )
    op.create_table(
        "group_members",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("group_id", "user_id", name="pk_group_members"),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name="fk_group_members_group_id_groups",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_group_members_user_id_users", ondelete="RESTRICT"
        ),
    )
    op.create_table(
        "expenses",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("paid_by", sa.BigInteger(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expenses"),
        sa.CheckConstraint("amount_paise > 0", name=op.f("ck_expenses_amount_positive")),
        sa.CheckConstraint(
            "description ~ '[^[:space:]]'", name=op.f("ck_expenses_description_nonblank")
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name="fk_expenses_group_id_groups", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["paid_by"], ["users.id"], name="fk_expenses_paid_by_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "paid_by"],
            ["group_members.group_id", "group_members.user_id"],
            name="fk_expenses_group_payer_membership",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_expenses_group_history",
        "expenses",
        ["group_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_table(
        "expense_splits",
        sa.Column("expense_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("share_paise", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("expense_id", "user_id", name="pk_expense_splits"),
        sa.CheckConstraint("share_paise >= 0", name=op.f("ck_expense_splits_share_nonnegative")),
        sa.ForeignKeyConstraint(
            ["expense_id"],
            ["expenses.id"],
            name="fk_expense_splits_expense_id_expenses",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_expense_splits_user_id_users", ondelete="RESTRICT"
        ),
    )


def downgrade() -> None:
    # Destructive by definition: only use on disposable databases or with a backup.
    op.drop_table("expense_splits")
    op.drop_index("ix_expenses_group_history", table_name="expenses")
    op.drop_table("expenses")
    op.drop_table("group_members")
    op.drop_table("groups")
    op.drop_table("users")
