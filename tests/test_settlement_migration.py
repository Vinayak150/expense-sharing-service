import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.models import Base, Expense, ExpenseSplit, Group, GroupMember, Settlement, User
from tests.conftest import migration_config

pytestmark = pytest.mark.postgres


def test_upgrade_populated_phase4_then_settlement_roundtrip(database_factory):
    with database_factory() as e:
        with e.begin() as c:
            cfg = migration_config(c)
            command.upgrade(cfg, "0001_foundation")
            users = [
                c.scalar(User.__table__.insert().values(name="Same").returning(User.id))
                for _ in range(2)
            ]
            g = c.scalar(Group.__table__.insert().values(name="Existing").returning(Group.id))
            c.execute(
                GroupMember.__table__.insert(), [{"group_id": g, "user_id": u} for u in users]
            )
            expense = c.scalar(
                Expense.__table__.insert()
                .values(group_id=g, paid_by=users[0], amount_paise=100, description="Old")
                .returning(Expense.id)
            )
            c.execute(
                ExpenseSplit.__table__.insert(),
                [{"expense_id": expense, "user_id": u, "share_paise": 50} for u in users],
            )
            old = c.execute(select(ExpenseSplit)).all()
            command.upgrade(cfg, "head")
            assert c.execute(select(ExpenseSplit)).all() == old
            c.execute(
                Settlement.__table__.insert().values(
                    group_id=g,
                    from_user_id=users[1],
                    to_user_id=users[0],
                    amount_paise=50,
                    idempotency_key="test",
                )
            )
            assert compare_metadata(MigrationContext.configure(c), Base.metadata) == []
            command.downgrade(cfg, "0001_foundation")
            assert "settlements" not in inspect(c).get_table_names()
            assert c.execute(select(ExpenseSplit)).all() == old
            command.upgrade(cfg, "head")
            assert c.scalar(select(Settlement.id)) is None
            assert c.execute(select(ExpenseSplit)).all() == old


@pytest.mark.parametrize(
    "changes,constraint",
    [
        ({"amount_paise": 0}, "ck_settlements_amount_positive"),
        ({"amount_paise": -1}, "ck_settlements_amount_positive"),
        ({"from_user_id": 999}, "fk_settlements_group_sender_membership"),
        ({"to_user_id": 999}, "fk_settlements_group_receiver_membership"),
        ({"from_user_id": 1}, "ck_settlements_participants_distinct"),
        ({"idempotency_key": ""}, "ck_settlements_key_valid"),
    ],
)
def test_settlement_database_guards(db, ledger, changes, constraint):
    data = {
        "group_id": ledger["group"],
        "from_user_id": ledger["participant"],
        "to_user_id": ledger["payer"],
        "amount_paise": 50,
        "idempotency_key": "guard",
    }
    if constraint == "ck_settlements_participants_distinct":
        changes = {"from_user_id": ledger["payer"]}
    with pytest.raises(IntegrityError) as error, db.begin_nested():
        db.execute(Settlement.__table__.insert().values(**(data | changes)))
    assert error.value.orig.diag.constraint_name == constraint


def test_settlement_uniqueness_deletion_indexes(db, ledger):
    data = {
        "group_id": ledger["group"],
        "from_user_id": ledger["participant"],
        "to_user_id": ledger["payer"],
        "amount_paise": 50,
        "idempotency_key": "guard",
    }
    db.execute(Settlement.__table__.insert().values(**data))
    with pytest.raises(IntegrityError) as error, db.begin_nested():
        db.execute(Settlement.__table__.insert().values(**data))
    assert error.value.orig.diag.constraint_name == "uq_settlements_group_key"
    for query in [
        "DELETE FROM group_members WHERE group_id=:group AND user_id=:participant",
        "DELETE FROM groups WHERE id=:group",
        "DELETE FROM users WHERE id=:payer",
    ]:
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(text(query), ledger)
    inspector = inspect(db)
    index = [
        i
        for i in inspector.get_indexes("settlements")
        if i["name"] == "ix_settlements_group_history"
    ][0]
    assert index["column_names"] == ["group_id", "created_at", "id"]
    assert index["column_sorting"] == {"created_at": ("desc",), "id": ("desc",)}
    assert all(
        f["options"]["ondelete"] == "RESTRICT" for f in inspector.get_foreign_keys("settlements")
    )
