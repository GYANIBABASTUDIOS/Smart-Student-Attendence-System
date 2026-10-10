"""Read-only, teacher-scoped dashboard data."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    AttendanceRecord,
    ClassSection,
    Student,
    Teacher,
    TeacherClassAssignment,
)
from app.services.analytics_service import PRESENT_STATUSES
from app.utils.time import today


def dashboard_data(teacher: Teacher, reference=None) -> dict:
    """Return classes and attendance belonging to this teacher's active assignments.

    Attendance has no class foreign key. Records are therefore included only when
    their student's current ``class_section_id`` points to a section assigned to
    this teacher. Legacy year/section text is never used to infer access.
    """
    reference = reference or today()
    week_start = reference - timedelta(days=6)
    classes = list(
        db.session.execute(
            db.select(ClassSection)
            .join(
                TeacherClassAssignment,
                TeacherClassAssignment.class_section_id == ClassSection.id,
            )
            .options(joinedload(ClassSection.department))
            .where(
                TeacherClassAssignment.teacher_id == teacher.id,
                TeacherClassAssignment.is_active.is_(True),
                ClassSection.is_active.is_(True),
            )
            .order_by(ClassSection.name, ClassSection.year, ClassSection.section)
        ).unique().scalars().all()
    )
    class_ids = [row.id for row in classes]
    student_counts: dict[int, int] = {}
    attendance_by_day: dict = defaultdict(dict)
    recent_activity = []

    if class_ids:
        student_counts = dict(
            db.session.execute(
                db.select(Student.class_section_id, func.count(Student.id))
                .where(
                    Student.class_section_id.in_(class_ids),
                    Student.is_active.is_(True),
                )
                .group_by(Student.class_section_id)
            ).all()
        )
        attendance_rows = db.session.execute(
            db.select(
                AttendanceRecord.date,
                AttendanceRecord.status,
                func.count(AttendanceRecord.id),
            )
            .join(Student, Student.id == AttendanceRecord.student_id)
            .where(
                Student.class_section_id.in_(class_ids),
                Student.is_active.is_(True),
                AttendanceRecord.date >= week_start,
                AttendanceRecord.date <= reference,
            )
            .group_by(AttendanceRecord.date, AttendanceRecord.status)
        ).all()
        for day, status, count in attendance_rows:
            attendance_by_day[day][status] = int(count)

        recent_activity = list(
            db.session.execute(
                db.select(AttendanceRecord)
                .join(Student, Student.id == AttendanceRecord.student_id)
                .options(
                    joinedload(AttendanceRecord.student).joinedload(Student.class_section)
                )
                .where(
                    Student.class_section_id.in_(class_ids),
                    Student.is_active.is_(True),
                )
                .order_by(AttendanceRecord.created_at.desc(), AttendanceRecord.id.desc())
                .limit(8)
            ).unique().scalars().all()
        )

    class_rows = [
        {
            "class_section": section,
            "student_count": int(student_counts.get(section.id, 0)),
        }
        for section in classes
    ]
    trend = []
    totals: dict[str, int] = defaultdict(int)
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        counts = attendance_by_day.get(day, {})
        for status, count in counts.items():
            totals[status] += count
        present_all = sum(int(counts.get(status, 0)) for status in PRESENT_STATUSES)
        present_only = int(counts.get("Present", 0))
        late = int(counts.get("Late", 0))
        absent = int(counts.get("Absent", 0))
        denominator = present_all + absent
        trend.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%b %d"),
                "present": present_only,
                "late": late,
                "absent": absent,
                "rate": round(present_all * 100 / denominator, 1) if denominator else 0.0,
            }
        )

    present = sum(totals[status] for status in PRESENT_STATUSES if status != "Late")
    late = totals["Late"]
    absent = totals["Absent"]
    denominator = present + late + absent
    return {
        "teacher": teacher,
        "classes": class_rows,
        "total_classes": len(classes),
        "total_students": sum(row["student_count"] for row in class_rows),
        "attendance": {
            "present": present + late,
            "absent": absent,
            "rate": round((present + late) * 100 / denominator, 1) if denominator else 0.0,
            "recorded_marks": denominator,
            "today": trend[-1],
            "trend": trend,
        },
        "recent_activity": recent_activity,
        "has_assignments": bool(classes),
    }
