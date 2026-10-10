"""Authorisation helpers and response hardening."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar, cast

from flask import Response, abort, g
from flask_login import current_user

F = TypeVar("F", bound=Callable[..., Any])


def roles_required(*roles: str) -> Callable[[F], F]:
    """Require an authenticated user holding one of ``roles``.

    Stacks under ``@login_required`` -- this only checks the role, so the login
    decorator still handles the unauthenticated redirect.
    """

    def decorator(view: F) -> F:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not current_user.is_authenticated:
                abort(401)
            if not current_user.has_role(*roles):
                abort(403)
            return view(*args, **kwargs)

        return cast(F, wrapper)

    return decorator


def admin_required(view: F) -> F:
    """Require the explicit administrator identity for an Admin operation."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not current_user.is_authenticated:
            abort(401)
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return cast(F, wrapper)


def active_teacher_profile():
    """Return the authenticated user's active Teacher profile, if it is valid."""
    if (
        not current_user.is_authenticated
        or not current_user.is_active
        or not current_user.has_role("teacher")
    ):
        return None

    from app.extensions import db
    from app.models import Teacher

    return db.session.execute(
        db.select(Teacher).where(
            Teacher.user_id == current_user.id,
            Teacher.is_active.is_(True),
        )
    ).scalar_one_or_none()


def teacher_required(view: F) -> F:
    """Require a teacher role backed by an active teacher profile.

    The authenticated user ID is the only profile lookup key. Callers must never
    supply a teacher ID to establish the current teacher's authority.
    """

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not current_user.is_authenticated:
            abort(401)
        teacher = active_teacher_profile()
        if teacher is None:
            abort(403)
        g.teacher_profile = teacher
        return view(*args, **kwargs)

    return cast(F, wrapper)


def active_student_profile():
    """Return the active Student row linked to the authenticated student account."""
    if (
        not current_user.is_authenticated
        or not current_user.is_active
        or not current_user.has_role("student")
    ):
        return None

    from app.extensions import db
    from app.models import Student

    return db.session.execute(
        db.select(Student).where(
            Student.user_id == current_user.id,
            Student.is_active.is_(True),
        )
    ).scalar_one_or_none()


def student_required(view: F) -> F:
    """Require a student role backed by that user's own active Student profile."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not current_user.is_authenticated:
            abort(401)
        student = active_student_profile()
        if student is None:
            abort(403)
        g.student_profile = student
        return view(*args, **kwargs)

    return cast(F, wrapper)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    # The templates load jQuery, Chart.js and Font Awesome from CDNs and use inline
    # event handlers throughout, so 'unsafe-inline' is required until the frontend is
    # reworked.  Documented as a known limitation rather than omitting the header.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "script-src 'self' 'unsafe-inline' "
        "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://code.jquery.com; "
        "style-src 'self' 'unsafe-inline' "
        "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
        "font-src 'self' data: https://cdnjs.cloudflare.com https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    ),
}


def apply_security_headers(response: Response) -> Response:
    """Attach hardening headers, without clobbering ones a view already set."""
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response
