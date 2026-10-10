"""Academic structure shared by students, teachers, and attendance sessions."""

from __future__ import annotations

from app.extensions import db
from app.utils.time import utcnow


class Department(db.Model):
    __tablename__ = "departments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    code = db.Column(db.String(20), nullable=False, unique=True)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    teachers = db.relationship("Teacher", back_populates="department")
    class_sections = db.relationship("ClassSection", back_populates="department")


class ClassSection(db.Model):
    __tablename__ = "class_sections"
    __table_args__ = (
        db.UniqueConstraint(
            "department_id", "year", "section", name="uq_class_sections_department_year_section"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    year = db.Column(db.String(10), nullable=False)
    section = db.Column(db.String(5), nullable=False)
    department_id = db.Column(
        db.Integer,
        db.ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    department = db.relationship("Department", back_populates="class_sections")
    students = db.relationship("Student", back_populates="class_section")
    teacher_assignments = db.relationship(
        "TeacherClassAssignment", back_populates="class_section"
    )
