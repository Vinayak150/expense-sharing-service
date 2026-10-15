"""Add append-only user-reported repayments with group-scoped idempotency.

Revision ID: 0002_settlements
Revises: 0001_foundation
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_settlements"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settlements",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("from_user_id", sa.BigInteger(), nullable=False),
        sa.Column("to_user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_settlements"),
        sa.UniqueConstraint("group_id", "idempotency_key", name="uq_settlements_group_key"),
        sa.CheckConstraint("amount_paise > 0", name=op.f("ck_settlements_amount_positive")),
        sa.CheckConstraint(
            "from_user_id <> to_user_id", name=op.f("ck_settlements_participants_distinct")
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'", name=op.f("ck_settlements_key_valid")
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name="fk_settlements_group_id_groups", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "from_user_id"],
            ["group_members.group_id", "group_members.user_id"],
            name="fk_settlements_group_sender_membership",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "to_user_id"],
            ["group_members.group_id", "group_members.user_id"],
            name="fk_settlements_group_receiver_membership",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_settlements_group_history",
        "settlements",
        ["group_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    # Disposable databases only: repayment facts would be lost, expenses stay intact.
    op.drop_index("ix_settlements_group_history", table_name="settlements")
    op.drop_table("settlements")
