"""Teacher profiles and their class assignments."""

from __future__ import annotations

from sqlalchemy import text

from app.extensions import db
from app.utils.time import utcnow


class Teacher(db.Model):
    __tablename__ = "teachers"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    employee_id = db.Column(db.String(32), nullable=False, unique=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(15))
    department_id = db.Column(
        db.Integer,
        db.ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    user = db.relationship("User", back_populates="teacher_profile", foreign_keys=[user_id])
    department = db.relationship("Department", back_populates="teachers")
    class_assignments = db.relationship(
        "TeacherClassAssignment", back_populates="teacher"
    )


class TeacherClassAssignment(db.Model):
    __tablename__ = "teacher_class_assignments"
    __table_args__ = (
        db.Index(
            "uq_teacher_class_assignments_active",
            "teacher_id",
            "class_section_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(
        db.Integer,
        db.ForeignKey("teachers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    class_section_id = db.Column(
        db.Integer,
        db.ForeignKey("class_sections.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    assigned_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    teacher = db.relationship("Teacher", back_populates="class_assignments")
    class_section = db.relationship(
        "ClassSection", back_populates="teacher_assignments"
    )
