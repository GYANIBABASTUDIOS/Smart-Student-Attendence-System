"""Account-management safeguards shared by CLI and future admin actions."""

from __future__ import annotations

from app.extensions import db
from app.models import Role, User


class UserManagementError(Exception):
    """Raised when an account change would violate user-management rules."""


def ensure_active_admin_remains(
    user: User, *, new_role: str | None = None, new_is_active: bool | None = None
) -> None:
    """Reject a change that would remove the final active administrator."""
    resulting_role = new_role if new_role is not None else user.role
    resulting_active = new_is_active if new_is_active is not None else user.is_active
    currently_active_admin = user.role == Role.ADMIN.value and user.is_active
    remains_active_admin = resulting_role == Role.ADMIN.value and resulting_active

    if not currently_active_admin or remains_active_admin:
        return

    other_admins = db.session.execute(
        db.select(db.func.count(User.id)).where(
            User.role == Role.ADMIN.value,
            User.is_active_flag.is_(True),
            User.id != user.id,
        )
    ).scalar_one()
    if other_admins == 0:
        raise UserManagementError("The last active administrator cannot be removed or demoted.")


def deactivate_user_account(user: User) -> None:
    """Deactivate an account while preserving at least one active administrator."""
    ensure_active_admin_remains(user, new_is_active=False)
    user.is_active_flag = False
    db.session.commit()


def change_user_role(user: User, role: str) -> None:
    """Change an account role without allowing the last active admin to be demoted."""
    valid_roles = {member.value for member in Role}
    if role not in valid_roles:
        raise UserManagementError(f"Unknown role {role!r}.")

    ensure_active_admin_remains(user, new_role=role)
    user.role = role
    db.session.commit()
