"""Teacher account and profile management for the Admin panel."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    ClassSection,
    Department,
    Role,
    Teacher,
    TeacherClassAssignment,
    User,
)
from app.services.user_service import ensure_active_admin_remains
from app.utils.validators import normalize_email


@dataclass
class TeacherValidationError(Exception):
    errors: dict[str, str]


def save_teacher(teacher: Teacher | None, form) -> Teacher:
    """Create or update a teacher profile and its role-based login account."""
    creating = teacher is None
    user = teacher.user if teacher else None
    errors: dict[str, str] = {}

    values = {
        "name": (form.get("name") or "").strip(),
        "employee_id": (form.get("employee_id") or "").strip(),
        "username": (form.get("username") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "phone": (form.get("phone") or "").strip() or None,
    }
    for field, label in (
        ("name", "Name"),
        ("employee_id", "Employee ID"),
        ("username", "Username"),
        ("email", "Email"),
    ):
        if not values[field]:
            errors[field] = f"{label} is required."

    limits = {"name": 100, "employee_id": 32, "username": 64, "email": 120, "phone": 15}
    for field, value in values.items():
        if value and len(value) > limits[field]:
            errors[field] = f"{field.replace('_', ' ').title()} must be {limits[field]} characters or fewer."

    email, email_error = normalize_email(values["email"])
    if values["email"] and email_error:
        errors["email"] = email_error
    elif email:
        values["email"] = email
        if len(email) > limits["email"]:
            errors["email"] = f"Email must be {limits['email']} characters or fewer."

    password = form.get("password") or ""
    confirmation = form.get("password_confirmation") or ""
    if creating and not password:
        errors["password"] = "A password is required."
    if password and len(password) < 12:
        errors["password"] = "Password must be at least 12 characters."
    if password and password != confirmation:
        errors["password_confirmation"] = "Passwords do not match."
    if not password and confirmation:
        errors["password_confirmation"] = "Enter a new password before confirming it."

    for field, model, value in (
        ("employee_id", Teacher, values["employee_id"]),
        ("username", User, values["username"]),
        ("email", User, values["email"]),
    ):
        if not value:
            continue
        column = getattr(model, field)
        query = select(model.id).where(column == value)
        if field == "employee_id" and teacher:
            query = query.where(Teacher.id != teacher.id)
        elif field in {"username", "email"} and user:
            query = query.where(User.id != user.id)
        if db.session.execute(query.limit(1)).first():
            errors[field] = f"That {field.replace('_', ' ')} is already in use."

    department_id = _optional_id(form.get("department_id"), "department_id", errors)
    if department_id is not None and db.session.get(Department, department_id) is None:
        errors["department_id"] = "Choose a valid department."

    class_ids = []
    for raw_id in form.getlist("class_ids"):
        class_id = _optional_id(raw_id, "class_ids", errors)
        if class_id is not None:
            class_ids.append(class_id)
    class_ids = sorted(set(class_ids))
    if class_ids:
        classes = db.session.execute(
            select(ClassSection).where(ClassSection.id.in_(class_ids))
        ).scalars().all()
        if len(classes) != len(class_ids) or any(not row.is_active for row in classes):
            errors["class_ids"] = "One or more selected classes are unavailable."
        elif department_id is not None and any(
            row.department_id != department_id for row in classes
        ):
            errors["class_ids"] = "Selected classes must belong to the chosen department."

    if errors:
        raise TeacherValidationError(errors)

    try:
        if creating:
            user = User(
                username=values["username"],
                email=values["email"],
                role=Role.TEACHER.value,
            )
            user.set_password(password)
            teacher = Teacher(
                user=user,
                employee_id=values["employee_id"],
                name=values["name"],
                phone=values["phone"],
                department_id=department_id,
            )
            db.session.add(teacher)
            db.session.flush()
        else:
            assert teacher is not None and user is not None
            user.username = values["username"]
            user.email = values["email"]
            if password:
                user.set_password(password)
            teacher.employee_id = values["employee_id"]
            teacher.name = values["name"]
            teacher.phone = values["phone"]
            teacher.department_id = department_id

        _sync_class_assignments(teacher, class_ids)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise TeacherValidationError(
            {"form": "A teacher with that username, email, employee ID, or class assignment already exists."}
        ) from exc

    return teacher


def set_teacher_active(teacher: Teacher, active: bool) -> None:
    """Keep a teacher profile and its login account active state in sync."""
    if not active:
        ensure_active_admin_remains(teacher.user, new_is_active=False)
    teacher.is_active = active
    teacher.user.is_active_flag = active
    db.session.commit()


def _sync_class_assignments(teacher: Teacher, selected_ids: list[int]) -> None:
    assignments = db.session.execute(
        select(TeacherClassAssignment).where(TeacherClassAssignment.teacher_id == teacher.id)
    ).scalars().all()
    by_class = {}
    for assignment in assignments:
        by_class.setdefault(assignment.class_section_id, []).append(assignment)

    for class_id, rows in by_class.items():
        active = next((row for row in rows if row.is_active), None)
        if class_id in selected_ids:
            if active is None:
                rows[0].is_active = True
        elif active is not None:
            active.is_active = False

    existing_ids = set(by_class)
    db.session.add_all(
        TeacherClassAssignment(teacher_id=teacher.id, class_section_id=class_id)
        for class_id in selected_ids
        if class_id not in existing_ids
    )


def _optional_id(raw_value: str | None, field: str, errors: dict[str, str]) -> int | None:
    if not raw_value:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        errors[field] = "Choose a valid option."
        return None
    if value < 1:
        errors[field] = "Choose a valid option."
        return None
    return value
