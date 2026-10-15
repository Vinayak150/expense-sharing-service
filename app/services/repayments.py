"""Append-only, user-reported transfers. Group lock protects validation and replay."""

import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.core.money import paise_to_rupees, rupees_to_paise
from app.models import Group, GroupMember, Settlement, User
from app.schemas.settlements import SettlementInput, SettlementOutput
from app.services import balances, groups
from app.services.errors import Conflict, InvalidSettlement, NotFound


def history_statement(group_id):
    sender, receiver = aliased(User), aliased(User)
    return (
        select(Settlement, sender.name, receiver.name)
        .join(sender, sender.id == Settlement.from_user_id)
        .join(receiver, receiver.id == Settlement.to_user_id)
        .where(Settlement.group_id == group_id)
    )


def output(record, sender, receiver):
    return SettlementOutput(
        id=record.id,
        group_id=record.group_id,
        from_user_id=record.from_user_id,
        from_user_name=sender,
        to_user_id=record.to_user_id,
        to_user_name=receiver,
        amount_paise=record.amount_paise,
        amount=paise_to_rupees(record.amount_paise),
        idempotency_key=record.idempotency_key,
        created_at=record.created_at,
    )


def find_replay(session, group_id, key):
    return session.execute(
        history_statement(group_id).where(Settlement.idempotency_key == key)
    ).first()


def record_settlement(
    session: Session, group_id: int, data: SettlementInput, key: str
) -> tuple[SettlementOutput, bool]:
    if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key) is None:
        raise InvalidSettlement(
            "invalid_idempotency_key",
            "Idempotency-Key must be 1–128 ASCII letters, digits, . _ : -",
        )
    amount = rupees_to_paise(data.amount)
    try:
        return _record_once(session, group_id, data, key, amount)
    except IntegrityError as error:
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if constraint != "uq_settlements_group_key":
            raise
        # Rollback has completed. A final DB uniqueness conflict is replayed under a
        # fresh owned transaction/lock, never on the failed Session transaction.
        return _record_once(session, group_id, data, key, amount)


def _record_once(session, group_id, data, key, amount):
    with session.begin():
        groups.lock_group(session, group_id)  # first query; READ COMMITTED, read/write
        replay = find_replay(session, group_id, key)
        if replay is not None:
            record, sender, receiver = replay
            if (record.from_user_id, record.to_user_id, record.amount_paise) != (
                data.from_user_id,
                data.to_user_id,
                amount,
            ):
                raise Conflict(
                    "idempotency_conflict", "Key already used for a different settlement"
                )
            result = output(record, sender, receiver)
            created = False
        else:
            participants = {}
            for user_id in (data.from_user_id, data.to_user_id):
                user = session.get(User, user_id)
                if user is None:
                    raise NotFound("user_not_found", "Settlement participant does not exist")
                if session.get(GroupMember, (group_id, user_id)) is None:
                    raise InvalidSettlement(
                        "participant_not_member", "Both participants must belong to the group"
                    )
                participants[user_id] = user.name
            if data.from_user_id == data.to_user_id:
                raise InvalidSettlement("self_settlement", "Sender and receiver must differ")
            # Fresh statements AFTER locking. Cooperating group writers cannot change
            # these facts until commit; never call the RR/read-only public service here.
            snapshot = balances.calculate_in_transaction(session, group_id)
            nets = {m.user_id: m.net_balance_paise for m in snapshot.balances}
            if nets[data.from_user_id] >= 0 or nets[data.to_user_id] <= 0:
                raise InvalidSettlement(
                    "invalid_payment_direction", "Sender must owe and receiver must be owed money"
                )
            if amount > -nets[data.from_user_id] or amount > nets[data.to_user_id]:
                raise InvalidSettlement("overpayment", "Amount exceeds outstanding debt or credit")
            record = Settlement(
                group_id=group_id,
                from_user_id=data.from_user_id,
                to_user_id=data.to_user_id,
                amount_paise=amount,
                idempotency_key=key,
            )
            session.add(record)
            session.flush()
            result = output(record, participants[data.from_user_id], participants[data.to_user_id])
            created = True
    return result, created


def list_settlements(session: Session, group_id: int) -> list[SettlementOutput]:
    with session.begin():
        if session.get(Group, group_id) is None:
            raise NotFound("group_not_found", "Group does not exist")
        rows = session.execute(
            history_statement(group_id).order_by(Settlement.created_at.desc(), Settlement.id.desc())
        )
        return [output(record, sender, receiver) for record, sender, receiver in rows]
