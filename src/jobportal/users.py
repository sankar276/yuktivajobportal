"""The person the app acts for.

The first build is single-user: one row in ``users``, kept in step with
``profile.yaml``. Every user-owned table already carries ``user_id``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.config import Profile
from jobportal.models import User


def get_default_user(session: Session, profile: Profile | None = None) -> User:
    user = session.scalar(select(User).order_by(User.id).limit(1))
    if user is None:
        user = User(
            email=profile.email if profile else "me@localhost",
            name=profile.name if profile else "",
        )
        session.add(user)
        session.flush()
    elif profile is not None and (user.email, user.name) != (profile.email, profile.name):
        user.email, user.name = profile.email, profile.name
        session.flush()
    return user
