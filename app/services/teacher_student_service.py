"""Teacher-scoped student queries and class authorization."""

from __future__ import annotations

from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import ClassSection, Student, TeacherClassAssignment


def assigned_classes(teacher_id: int) -> list[ClassSection]:
    """List only active sections linked through this teacher's active assignments."""
    return list(
        db.session.execute(
            db.select(ClassSection)
            .join(
                TeacherClassAssignment,
                TeacherClassAssignment.class_section_id == ClassSection.id,
            )
            .options(joinedload(ClassSection.department))
            .where(
                TeacherClassAssignment.teacher_id == teacher_id,
                TeacherClassAssignment.is_active.is_(True),
                ClassSection.is_active.is_(True),
            )
            .order_by(ClassSection.name, ClassSection.year, ClassSection.section)
        ).unique().scalars().all()
    )


def assigned_class(teacher_id: int, class_section_id: int) -> ClassSection | None:
    """Resolve a destination class only if it is currently assigned and active."""
    return db.session.execute(
        db.select(ClassSection)
        .join(
            TeacherClassAssignment,
            TeacherClassAssignment.class_section_id == ClassSection.id,
        )
        .options(joinedload(ClassSection.department))
        .where(
            ClassSection.id == class_section_id,
            ClassSection.is_active.is_(True),
            TeacherClassAssignment.teacher_id == teacher_id,
            TeacherClassAssignment.is_active.is_(True),
        )
    ).scalar_one_or_none()


def scoped_student_query(teacher_id: int):
    """Base student query restricted to current active assigned class sections."""
    return (
        db.select(Student)
        .join(
            TeacherClassAssignment,
            TeacherClassAssignment.class_section_id == Student.class_section_id,
        )
        .join(ClassSection, ClassSection.id == Student.class_section_id)
        .options(
            joinedload(Student.class_section).joinedload(ClassSection.department)
        )
        .where(
            TeacherClassAssignment.teacher_id == teacher_id,
            TeacherClassAssignment.is_active.is_(True),
            ClassSection.is_active.is_(True),
        )
        .distinct()
    )


def get_scoped_student(teacher_id: int, student_id: int) -> Student | None:
    """Return any active or inactive student only within this teacher's current scope."""
    return db.session.execute(
        scoped_student_query(teacher_id).where(Student.id == student_id)
    ).unique().scalar_one_or_none()
