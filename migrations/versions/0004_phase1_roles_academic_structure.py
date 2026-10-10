"""Add Phase 1 role profiles and academic structure.

Revision ID: 0004_phase1_roles_academic_structure
Revises: 0003_auth_and_constraints

All links back to existing records are nullable where existing rows need to remain
unlinked.  Legacy department/year/section and marked_by strings are retained.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_phase1_roles_academic_structure"
down_revision = "0003_auth_and_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_departments"),
        sa.UniqueConstraint("name", name="uq_departments_name"),
        sa.UniqueConstraint("code", name="uq_departments_code"),
    )

    op.create_table(
        "class_sections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("year", sa.String(length=10), nullable=False),
        sa.Column("section", sa.String(length=5), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"],
            name="fk_class_sections_department_id_departments", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_class_sections"),
        sa.UniqueConstraint(
            "department_id", "year", "section",
            name="uq_class_sections_department_year_section"
        ),
    )
    op.create_index("ix_class_sections_department_id", "class_sections", ["department_id"])

    op.create_table(
        "teachers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("phone", sa.String(length=15), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"],
            name="fk_teachers_department_id_departments", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_teachers_user_id_users", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_teachers"),
        sa.UniqueConstraint("user_id", name="uq_teachers_user_id"),
        sa.UniqueConstraint("employee_id", name="uq_teachers_employee_id"),
    )
    op.create_index("ix_teachers_department_id", "teachers", ["department_id"])

    op.create_table(
        "teacher_class_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("teacher_id", sa.Integer(), nullable=False),
        sa.Column("class_section_id", sa.Integer(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.ForeignKeyConstraint(
            ["class_section_id"], ["class_sections.id"],
            name="fk_teacher_class_assignments_class_section_id_class_sections",
            ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"], ["teachers.id"],
            name="fk_teacher_class_assignments_teacher_id_teachers", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_teacher_class_assignments"),
    )
    op.create_index(
        "ix_teacher_class_assignments_teacher_id",
        "teacher_class_assignments", ["teacher_id"]
    )
    op.create_index(
        "ix_teacher_class_assignments_class_section_id",
        "teacher_class_assignments", ["class_section_id"]
    )
    op.create_index(
        "uq_teacher_class_assignments_active",
        "teacher_class_assignments", ["teacher_id", "class_section_id"],
        unique=True, sqlite_where=sa.text("is_active = 1")
    )

    # Do not batch-rebuild students: attendance and leave reference it with ON DELETE
    # CASCADE, and SQLite's enforced foreign keys would treat a table rebuild's DROP as
    # a real delete of every student and cascade away their history.
    op.execute(
        "ALTER TABLE students ADD COLUMN user_id INTEGER "
        "CONSTRAINT fk_students_user_id_users "
        "REFERENCES users (id) ON DELETE SET NULL"
    )
    op.create_index("uq_students_user_id", "students", ["user_id"], unique=True)
    op.execute(
        "ALTER TABLE students ADD COLUMN class_section_id INTEGER "
        "CONSTRAINT fk_students_class_section_id_class_sections "
        "REFERENCES class_sections (id) ON DELETE SET NULL"
    )
    op.create_index("ix_students_class_section_id", "students", ["class_section_id"])

    # These are nullable REFERENCES columns, which SQLite permits to be added in place
    # without rebuilding the existing attendance table or touching its records.
    op.execute(
        "ALTER TABLE attendance_records ADD COLUMN marked_by_user_id INTEGER "
        "CONSTRAINT fk_attendance_records_marked_by_user_id_users "
        "REFERENCES users (id) ON DELETE SET NULL"
    )
    op.create_index(
        "ix_attendance_records_marked_by_user_id",
        "attendance_records", ["marked_by_user_id"]
    )


def downgrade() -> None:
    # This revision is additive and reversible because it introduces no backfilled
    # data.  Downgrade drops only the newly introduced schema objects.
    op.drop_index("ix_attendance_records_marked_by_user_id", table_name="attendance_records")
    op.drop_column("attendance_records", "marked_by_user_id")
    op.drop_index("ix_students_class_section_id", table_name="students")
    op.drop_index("uq_students_user_id", table_name="students")
    op.drop_column("students", "class_section_id")
    op.drop_column("students", "user_id")

    op.drop_index("uq_teacher_class_assignments_active", table_name="teacher_class_assignments")
    op.drop_index(
        "ix_teacher_class_assignments_class_section_id", table_name="teacher_class_assignments"
    )
    op.drop_index("ix_teacher_class_assignments_teacher_id", table_name="teacher_class_assignments")
    op.drop_table("teacher_class_assignments")

    op.drop_index("ix_teachers_department_id", table_name="teachers")
    op.drop_table("teachers")

    op.drop_index("ix_class_sections_department_id", table_name="class_sections")
    op.drop_table("class_sections")
    op.drop_table("departments")
