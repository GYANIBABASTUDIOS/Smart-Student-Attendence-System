"""Read-only aggregation for the Admin dashboard."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
import csv
import io

from app.extensions import db
from app.models import (
    AttendanceRecord,
    ClassSection,
    Department,
    LeaveRequest,
    Student,
    Teacher,
    TeacherClassAssignment,
)
from app.services import analytics_service
from app.services.analytics_service import DateRange
from app.utils.time import today
from sqlalchemy import case, func


def dashboard_data(reference: date_type | None = None) -> dict:
    """Collect aggregate campus and attendance metrics for Admin reporting."""
    reference = reference or today()
    week = DateRange(start=reference - timedelta(days=6), end=reference)
    trend = analytics_service.trend(week)["trend"]
    today_data = trend[-1] if trend else {"present": 0, "late": 0, "absent": 0, "rate": 0.0}
    return {
        "reference_date": reference,
        "total_students": _count(Student),
        "total_teachers": _count(Teacher),
        "total_departments": _count(Department),
        "total_classes": _count(ClassSection),
        "present_today": int(today_data.get("present", 0)) + int(today_data.get("late", 0)),
        "absent_today": int(today_data.get("absent", 0)),
        "attendance_rate": float(today_data.get("rate", 0.0)),
        "weekly_trend": trend,
        "department_attendance": _department_attendance(week),
    }


def _count(model) -> int:
    return int(
        db.session.execute(db.select(db.func.count()).select_from(model)).scalar_one() or 0
    )


def _department_attendance(window: DateRange) -> list[dict]:
    """Group existing analytics by department for aggregate reporting."""
    grouped: dict[str, dict[str, int]] = defaultdict(lambda: {"students": 0, "present_days": 0})
    for student in analytics_service.student_attendance_rates(window):
        department = (student["department"] or "").strip() or "No department"
        grouped[department]["students"] += 1
        grouped[department]["present_days"] += int(student["present_days"])

    rows = []
    for name, values in grouped.items():
        denominator = values["students"] * window.days
        rate = round(min(values["present_days"] / denominator, 1.0) * 100, 1) if denominator else 0.0
        rows.append(
            {
                "name": name,
                "students": values["students"],
                "present_days": values["present_days"],
                "rate": rate,
            }
        )
    rows.sort(key=lambda row: (-row["rate"], row["name"].casefold()))
    return rows


@dataclass(frozen=True)
class ReportFilters:
    """Validated filters for the Admin aggregate reports."""

    window: DateRange
    department_id: int | None = None
    class_section_id: int | None = None


def admin_reports_data(filters: ReportFilters) -> dict:
    """Return aggregate-only Admin reporting from attendance and academic records.

    Rates use recorded Present/Late/Absent rows only: (Present + Late) /
    (Present + Late + Absent). Missing marks are not inferred as absences, while
    Excused/On Leave rows are excluded from the attendance denominator.
    """
    window = filters.window
    attendance_query = db.select(
        AttendanceRecord.date,
        AttendanceRecord.status,
        func.count(AttendanceRecord.id),
    ).join(Student, Student.id == AttendanceRecord.student_id)
    attendance_query = _apply_student_scope(attendance_query, filters)
    attendance_rows = db.session.execute(
        attendance_query.where(
            AttendanceRecord.date >= window.start,
            AttendanceRecord.date <= window.end,
        ).group_by(AttendanceRecord.date, AttendanceRecord.status)
    ).all()

    counts_by_day: dict[date_type, dict[str, int]] = defaultdict(dict)
    totals: dict[str, int] = defaultdict(int)
    for day, status, count in attendance_rows:
        counts_by_day[day][status] = int(count)
        totals[status] += int(count)

    trend_rows = []
    for day in window.each_day():
        counts = counts_by_day.get(day, {})
        present = counts.get("Present", 0)
        late = counts.get("Late", 0)
        absent = counts.get("Absent", 0)
        denominator = present + late + absent
        trend_rows.append({
            "date": day.isoformat(),
            "present": present,
            "late": late,
            "absent": absent,
            "rate": round((present + late) * 100 / denominator, 1) if denominator else 0.0,
        })

    present = totals["Present"]
    late = totals["Late"]
    absent = totals["Absent"]
    denominator = present + late + absent
    entity_counts = _report_entity_counts(filters)
    return {
        **entity_counts,
        "present_count": present + late,
        "absent_count": absent,
        "attendance_rate": round((present + late) * 100 / denominator, 1) if denominator else 0.0,
        "attendance_denominator": denominator,
        "trend": trend_rows,
        "department_rows": _department_report(filters),
        "class_rows": _class_report(filters),
        "leave_counts": _leave_report_counts(filters),
        "attendance_total": sum(totals.values()),
    }


def _report_entity_counts(filters: ReportFilters) -> dict[str, int]:
    students_query = db.select(func.count(func.distinct(Student.id))).select_from(Student)
    students_query = _apply_student_scope(students_query, filters)
    total_students = int(db.session.execute(students_query).scalar_one() or 0)

    teachers_query = db.select(func.count(func.distinct(Teacher.id))).select_from(Teacher)
    if filters.class_section_id is not None:
        teachers_query = teachers_query.join(
            TeacherClassAssignment,
            TeacherClassAssignment.teacher_id == Teacher.id,
        ).where(
            TeacherClassAssignment.class_section_id == filters.class_section_id,
            TeacherClassAssignment.is_active.is_(True),
        )
    elif filters.department_id is not None:
        teachers_query = teachers_query.where(Teacher.department_id == filters.department_id)
    total_teachers = int(db.session.execute(teachers_query).scalar_one() or 0)

    department_query = db.select(func.count()).select_from(Department)
    class_query = db.select(func.count()).select_from(ClassSection)
    if filters.class_section_id is not None:
        department_query = department_query.where(Department.id == filters.department_id)
        class_query = class_query.where(ClassSection.id == filters.class_section_id)
    elif filters.department_id is not None:
        department_query = department_query.where(Department.id == filters.department_id)
        class_query = class_query.where(ClassSection.department_id == filters.department_id)
    return {
        "total_students": total_students,
        "total_teachers": total_teachers,
        "total_departments": int(db.session.execute(department_query).scalar_one() or 0),
        "total_classes": int(db.session.execute(class_query).scalar_one() or 0),
    }


def _apply_student_scope(query, filters: ReportFilters):
    if filters.class_section_id is not None:
        query = query.where(Student.class_section_id == filters.class_section_id)
    elif filters.department_id is not None:
        query = query.join(ClassSection, Student.class_section_id == ClassSection.id).where(
            ClassSection.department_id == filters.department_id
        )
    return query


def _department_report(filters: ReportFilters) -> dict:
    """Return canonical linked departments and legacy labels as separate groups."""
    base = db.select(
        Student.department,
        ClassSection.department_id,
        func.count(func.distinct(Student.id)).label("students"),
        func.sum(case((AttendanceRecord.status.in_(analytics_service.PRESENT_STATUSES), 1), else_=0)).label("present"),
        func.sum(case((AttendanceRecord.status == "Absent", 1), else_=0)).label("absent"),
    ).select_from(Student).outerjoin(ClassSection, Student.class_section_id == ClassSection.id).outerjoin(
        AttendanceRecord,
        (AttendanceRecord.student_id == Student.id)
        & (AttendanceRecord.date >= filters.window.start)
        & (AttendanceRecord.date <= filters.window.end),
    ).where(Student.is_active.is_(True))
    if filters.class_section_id is not None:
        base = base.where(Student.class_section_id == filters.class_section_id)
    elif filters.department_id is not None:
        base = base.where(ClassSection.department_id == filters.department_id)
    rows = db.session.execute(base.group_by(Student.department, ClassSection.department_id)).all()
    department_names = {
        department.id: f"{department.name} ({department.code})"
        for department in db.session.execute(
            db.select(Department).order_by(Department.name)
        ).scalars()
    }
    linked: dict[int, dict] = {}
    legacy: dict[str, dict] = {}
    for legacy_name, department_id, students, present_records, absent_records in rows:
        values = {
            "students": int(students or 0),
            "present": int(present_records or 0),
            "absent": int(absent_records or 0),
        }
        label = department_names.get(department_id, "Unlinked class department")
        _merge_report_counts(linked, department_id, label, values)
        raw_label = (legacy_name or "").strip() or "No department"
        # Keep the legacy namespace visibly separate from canonical Department rows.
        _merge_report_counts(legacy, raw_label, raw_label, values)
    return {
        "linked": [_report_rate(key, value) for key, value in sorted(linked.items(), key=lambda item: item[1]["name"].casefold())],
        "legacy": [_report_rate(key, value) for key, value in sorted(legacy.items(), key=lambda item: item[1]["name"].casefold())],
    }


def _merge_report_counts(target: dict, key, name: str, values: dict) -> None:
    row = target.setdefault(key, {"name": name, "students": 0, "present": 0, "absent": 0})
    for field in ("students", "present", "absent"):
        row[field] += values[field]


def _report_rate(key, values: dict) -> dict:
    denominator = values["present"] + values["absent"]
    return {**values, "key": key, "rate": round(values["present"] * 100 / denominator, 1) if denominator else 0.0}


def _class_report(filters: ReportFilters) -> list[dict]:
    query = db.select(
        ClassSection.id,
        ClassSection.name,
        Department.name,
        Department.code,
        func.count(func.distinct(Student.id)).label("students"),
        func.sum(case((AttendanceRecord.status.in_(analytics_service.PRESENT_STATUSES), 1), else_=0)).label("present"),
        func.sum(case((AttendanceRecord.status == "Absent", 1), else_=0)).label("absent"),
    ).join(Department, Department.id == ClassSection.department_id).outerjoin(
        Student, (Student.class_section_id == ClassSection.id) & Student.is_active.is_(True)
    ).outerjoin(
        AttendanceRecord,
        (AttendanceRecord.student_id == Student.id)
        & (AttendanceRecord.date >= filters.window.start)
        & (AttendanceRecord.date <= filters.window.end),
    )
    if filters.class_section_id is not None:
        query = query.where(ClassSection.id == filters.class_section_id)
    if filters.department_id is not None:
        query = query.where(ClassSection.department_id == filters.department_id)
    rows = db.session.execute(query.group_by(ClassSection.id, ClassSection.name, Department.name, Department.code).order_by(Department.name, ClassSection.name)).all()
    return [
        _report_rate(class_id, {"name": f"{department} ({code}) · {name}", "students": int(students or 0), "present": int(present or 0), "absent": int(absent or 0)})
        for class_id, name, department, code, students, present, absent in rows
    ]


def _leave_report_counts(filters: ReportFilters) -> dict[str, int]:
    query = db.select(LeaveRequest.status, func.count()).select_from(LeaveRequest).join(
        Student, Student.id == LeaveRequest.student_id
    ).where(
        LeaveRequest.start_date <= filters.window.end,
        LeaveRequest.end_date >= filters.window.start,
    )
    query = _apply_student_scope(query, filters)
    rows = db.session.execute(query.group_by(LeaveRequest.status)).all()
    counts = {"Pending": 0, "Approved": 0, "Rejected": 0}
    for status, count in rows:
        counts[status] = int(count)
    return counts


def admin_reports_csv(data: dict) -> bytes:
    """Create an aggregate-only CSV with fixed columns and formula-safe cells."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(("Date", "Present (includes late)", "Absent", "Attendance rate (%)"))
    for row in data["trend"]:
        writer.writerow(tuple(_safe_csv_cell(row[key]) for key in ("date", "present", "absent", "rate")))
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def _safe_csv_cell(value) -> str:
    text = str(value)
    formula_prefix = text.lstrip(" \t\r\n")
    if formula_prefix and formula_prefix[0] in {"=", "+", "-", "@"}:
        return "'" + text
    return text
