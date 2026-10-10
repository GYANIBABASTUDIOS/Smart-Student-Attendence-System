"""Admin management for departments, class sections, and their assignments."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import ClassSection, Department, Teacher, TeacherClassAssignment


@dataclass
class StructureValidationError(Exception):
    errors: dict[str, str]


def save_department(department: Department | None, form) -> Department:
    """Create or update a department without deleting linked academic records."""
    name = (form.get("name") or "").strip()
    code = (form.get("code") or "").strip().upper()
    description = (form.get("description") or "").strip() or None
    errors: dict[str, str] = {}

    if not name:
        errors["name"] = "Department name is required."
    elif len(name) > 100:
        errors["name"] = "Department name must be 100 characters or fewer."
    if not code:
        errors["code"] = "Department code is required."
    elif len(code) > 20 or not re.fullmatch(r"[A-Z0-9_-]+", code):
        errors["code"] = "Use up to 20 letters, numbers, hyphens, or underscores."

    if name:
        query = select(Department.id).where(func.lower(Department.name) == name.lower())
        if department:
            query = query.where(Department.id != department.id)
        if db.session.execute(query.limit(1)).first():
            errors["name"] = "A department with this name already exists."
    if code:
        query = select(Department.id).where(func.upper(Department.code) == code)
        if department:
            query = query.where(Department.id != department.id)
        if db.session.execute(query.limit(1)).first():
            errors["code"] = "A department with this code already exists."

    if errors:
        raise StructureValidationError(errors)

    try:
        if department is None:
            department = Department(name=name, code=code, description=description)
            db.session.add(department)
        else:
            department.name = name
            department.code = code
            department.description = description
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise StructureValidationError(
            {"form": "A department with that name or code already exists."}
        ) from exc
    return department


def set_department_active(department: Department, active: bool) -> None:
    """Soft-change department status while retaining teacher and class links."""
    department.is_active = active
    db.session.commit()


def save_class_section(class_section: ClassSection | None, form) -> ClassSection:
    """Create or edit a class section with an existing department relationship."""
    name = (form.get("name") or "").strip()
    year = (form.get("year") or "").strip()
    section = (form.get("section") or "").strip()
    raw_department_id = form.get("department_id") or ""
    errors: dict[str, str] = {}

    for field, value, label, limit in (
        ("name", name, "Class/section name", 100),
        ("year", year, "Year", 10),
        ("section", section, "Section", 5),
    ):
        if not value:
            errors[field] = f"{label} is required."
        elif len(value) > limit:
            errors[field] = f"{label} must be {limit} characters or fewer."

    try:
        department_id = int(raw_department_id)
        if department_id < 1:
            raise ValueError
    except (TypeError, ValueError):
        department_id = None
        errors["department_id"] = "Choose a valid department."

    department = db.session.get(Department, department_id) if department_id else None
    if department_id and department is None:
        errors["department_id"] = "Choose a valid department."
    elif department is not None and not department.is_active:
        same_existing_department = class_section and class_section.department_id == department.id
        if not same_existing_department:
            errors["department_id"] = "New class sections must use an active department."

    if department_id and year and section:
        query = select(ClassSection.id).where(
            ClassSection.department_id == department_id,
            ClassSection.year == year,
            ClassSection.section == section,
        )
        if class_section:
            query = query.where(ClassSection.id != class_section.id)
        if db.session.execute(query.limit(1)).first():
            errors["section"] = "This year and section already exist in that department."

    if errors:
        raise StructureValidationError(errors)

    try:
        if class_section is None:
            class_section = ClassSection(
                name=name, year=year, section=section, department_id=department_id
            )
            db.session.add(class_section)
        else:
            class_section.name = name
            class_section.year = year
            class_section.section = section
            class_section.department_id = department_id
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise StructureValidationError(
            {"form": "That class/section already exists in the selected department."}
        ) from exc
    return class_section


def set_class_section_active(class_section: ClassSection, active: bool) -> None:
    """Soft-change class status; student and teacher links are retained."""
    class_section.is_active = active
    db.session.commit()


def assign_teacher(class_section: ClassSection, teacher_id: int) -> TeacherClassAssignment:
    """Add an active teacher assignment, respecting the existing unique index."""
    errors: dict[str, str] = {}
    teacher = db.session.get(Teacher, teacher_id)
    if teacher is None:
        errors["teacher_id"] = "Choose a valid teacher."
    elif not class_section.is_active:
        errors["teacher_id"] = "Teachers cannot be assigned to an inactive class section."
    elif not teacher.is_active or not teacher.user.is_active:
        errors["teacher_id"] = "Choose an active teacher account."

    existing = db.session.execute(
        select(TeacherClassAssignment).where(
            TeacherClassAssignment.teacher_id == teacher_id,
            TeacherClassAssignment.class_section_id == class_section.id,
            TeacherClassAssignment.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if existing is not None:
        errors["teacher_id"] = "This teacher is already assigned to this class."
    if errors:
        raise StructureValidationError(errors)

    previous = db.session.execute(
        select(TeacherClassAssignment)
        .where(
            TeacherClassAssignment.teacher_id == teacher_id,
            TeacherClassAssignment.class_section_id == class_section.id,
        )
        .order_by(TeacherClassAssignment.id)
    ).scalars().first()
    try:
        if previous is not None:
            previous.is_active = True
            assignment = previous
        else:
            assignment = TeacherClassAssignment(
                teacher_id=teacher_id, class_section_id=class_section.id
            )
            db.session.add(assignment)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise StructureValidationError(
            {"teacher_id": "This teacher is already assigned to this class."}
        ) from exc
    return assignment


def remove_teacher_assignment(
    class_section: ClassSection, assignment_id: int
) -> None:
    """Deactivate an assignment instead of deleting its history."""
    assignment = db.session.execute(
        select(TeacherClassAssignment).where(
            TeacherClassAssignment.id == assignment_id,
            TeacherClassAssignment.class_section_id == class_section.id,
            TeacherClassAssignment.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise LookupError("That active teacher assignment was not found.")
    assignment.is_active = False
    db.session.commit()
