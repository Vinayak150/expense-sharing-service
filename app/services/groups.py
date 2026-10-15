from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Group, GroupMember, User
from app.schemas.business import MemberOutput, NamedResource
from app.services.errors import Conflict, NotFound


def lock_group(session: Session, group_id: int) -> Group:
    """First business query for every membership-dependent write, inside owned transaction."""
    group = session.scalar(select(Group).where(Group.id == group_id).with_for_update())
    if group is None:
        raise NotFound("group_not_found", "Group does not exist")
    return group


def create_group(session: Session, name: str) -> NamedResource:
    with session.begin():
        group = Group(name=name)
        session.add(group)
        session.flush()
        result = NamedResource.model_validate(group)
    return result


def add_member(session: Session, group_id: int, user_id: int) -> MemberOutput:
    try:
        with session.begin():
            lock_group(session, group_id)
            user = session.get(User, user_id)
            if user is None:
                raise NotFound("user_not_found", "User does not exist")
            if session.get(GroupMember, (group_id, user_id)) is not None:
                raise Conflict("membership_exists", "User is already a group member")
            session.add(GroupMember(group_id=group_id, user_id=user_id))
            session.flush()
            result = MemberOutput(user_id=user.id, name=user.name)
    except IntegrityError as error:
        # The transaction context has already rolled back before error translation.
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if constraint == "pk_group_members":
            raise Conflict("membership_exists", "User is already a group member") from None
        if constraint == "fk_group_members_user_id_users":
            raise NotFound("user_not_found", "User does not exist") from None
        raise
    return result


def list_members(session: Session, group_id: int) -> list[MemberOutput]:
    with session.begin():
        if session.get(Group, group_id) is None:
            raise NotFound("group_not_found", "Group does not exist")
        rows = session.execute(
            select(User.id, User.name)
            .join(GroupMember, GroupMember.user_id == User.id)
            .where(GroupMember.group_id == group_id)
            .order_by(User.id)
        )
        return [MemberOutput(user_id=row.id, name=row.name) for row in rows]
