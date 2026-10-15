from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from app.schemas.business import NamedResource


def create_user(session: Session, name: str) -> NamedResource:
    with session.begin():
        user = User(name=name)
        session.add(user)
        session.flush()
        result = NamedResource.model_validate(user)
    return result


def list_users(session: Session) -> list[NamedResource]:
    with session.begin():
        return [
            NamedResource.model_validate(user)
            for user in session.scalars(select(User).order_by(User.id))
        ]
