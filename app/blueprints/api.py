"""JSON API.

Same paths as before so the existing dashboard JavaScript keeps working, but each
endpoint now requires a session and the analytics queries are aggregates rather than
per-row loops.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app.extensions import db
from app.models import AttendanceRecord, LeaveRequest, Student
from app.services import analytics_service, teacher_student_service
from app.services.analytics_service import DateRange
from app.utils.security import active_student_profile, active_teacher_profile, roles_required
from app.utils.time import today

api_bp = Blueprint("api", __name__, url_prefix="/api")

MAX_DAYS = 365
MAX_LIMIT = 200


def _teacher_global_data_denied():
    """Legacy aggregate endpoints are system-wide; Teachers use scoped panel routes."""
    if current_user.has_role("teacher"):
        return jsonify({"success": False, "error": "Use class-scoped Teacher Panel data."}), 403
    return None


def _window(default_days: int = 30) -> DateRange:
    raw = request.args.get("days", default_days, type=int) or default_days
    days = max(1, min(raw, MAX_DAYS))
    reference = today()
    return DateRange(start=reference - timedelta(days=days - 1), end=reference)


def _limit(default: int = 10) -> int:
    raw = request.args.get("limit", default, type=int) or default
    return max(1, min(raw, MAX_LIMIT))


# --- students ------------------------------------------------------------------
@api_bp.route("/student/<int:student_id>")
@login_required
def student_detail(student_id: int):
    if current_user.has_role("student"):
        student = active_student_profile()
        if student is None:
            return jsonify({"success": False, "error": "Forbidden"}), 403
        if student.id != student_id:
            return jsonify({"success": False, "error": "Student not found"}), 404
    elif current_user.has_role("teacher"):
        teacher = active_teacher_profile()
        student = (
            teacher_student_service.get_scoped_student(teacher.id, student_id)
            if teacher is not None
            else None
        )
        if teacher is None:
            return jsonify({"success": False, "error": "Forbidden"}), 403
    elif current_user.has_role("admin"):
        student = db.session.get(Student, student_id)
    else:
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if student is None:
        return jsonify({"success": False, "error": "Student not found"}), 404
    return jsonify(student.to_dict())


# --- attendance ----------------------------------------------------------------
@api_bp.route("/attendance_summary")
@login_required
@roles_required("admin", "teacher")
def attendance_summary():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    reference = today()
    return jsonify(analytics_service.summarise(DateRange(start=reference, end=reference)))


@api_bp.route("/today_attendance")
@login_required
@roles_required("admin", "teacher")
def today_attendance():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    reference = today()
    records = (
        db.session.execute(
            db.select(AttendanceRecord)
            .options(db.joinedload(AttendanceRecord.student))
            .where(AttendanceRecord.date == reference)
            .order_by(AttendanceRecord.created_at.desc())
            .limit(_limit(10))
        )
        .unique()
        .scalars()
        .all()
    )
    return jsonify(
        {
            "date": reference.isoformat(),
            "total_present": sum(1 for r in records if r.status in ("Present", "Late")),
            "records": [
                {
                    "student_name": record.student.name if record.student else "Unknown",
                    "student_id": record.student.student_id if record.student else "N/A",
                    "time": record.time_in.isoformat() if record.time_in else None,
                    "status": record.status,
                }
                for record in records
            ],
        }
    )


# --- leave ---------------------------------------------------------------------
@api_bp.route("/leave/<int:leave_id>")
@login_required
@roles_required("admin", "teacher")
def leave_detail(leave_id: int):
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    request_row = db.session.get(LeaveRequest, leave_id)
    if request_row is None:
        return jsonify({"success": False, "error": "Leave request not found"}), 404
    return jsonify(request_row.to_dict())


@api_bp.route("/students_on_leave")
@login_required
@roles_required("admin", "teacher")
def students_on_leave():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    from app.services import leave_service

    rows = leave_service.on_leave_today()
    return jsonify({"count": len(rows), "students": [row.to_dict() for row in rows]})


# --- recognition ---------------------------------------------------------------
@api_bp.route("/face_recognition_status")
@login_required
@roles_required("admin", "teacher")
def face_recognition_status():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    from app.recognition import get_pipeline

    pipeline = get_pipeline()
    status = pipeline.status()
    # Keys the old endpoint returned, kept so existing callers do not break.
    status.update(
        {
            "available": pipeline.detector.available,
            "active": status["recognizing"],
            "camera_active": status["running"],
        }
    )
    return jsonify(status)


@api_bp.route("/recognition/status")
@login_required
@roles_required("admin", "teacher")
def recognition_status():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    from app.recognition import get_pipeline

    return jsonify(get_pipeline().status())


# --- analytics -----------------------------------------------------------------
@api_bp.route("/analytics/trend")
@login_required
@roles_required("admin", "teacher")
def analytics_trend():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    return jsonify(analytics_service.trend(_window()))


@api_bp.route("/analytics/department")
@login_required
@roles_required("admin", "teacher")
def analytics_department():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    return jsonify(analytics_service.department_stats(_window()))


@api_bp.route("/analytics/status_distribution")
@login_required
@roles_required("admin", "teacher")
def analytics_status_distribution():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    return jsonify(analytics_service.status_distribution(_window()))


@api_bp.route("/analytics/top_students")
@login_required
@roles_required("admin", "teacher")
def analytics_top_students():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    return jsonify(analytics_service.top_students(_window(), limit=_limit(10)))


@api_bp.route("/analytics/at_risk")
@login_required
@roles_required("admin", "teacher")
def analytics_at_risk():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    threshold = request.args.get("threshold", 75.0, type=float) or 75.0
    threshold = max(0.0, min(threshold, 100.0))
    return jsonify(
        analytics_service.at_risk_students(_window(), threshold=threshold, limit=_limit(10))
    )


@api_bp.route("/analytics/weekly_heatmap")
@login_required
@roles_required("admin", "teacher")
def analytics_weekly_heatmap():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    weeks = request.args.get("weeks", 4, type=int) or 4
    return jsonify(analytics_service.weekly_heatmap(max(1, min(weeks, 26)), today()))


@api_bp.route("/analytics/recent_activity")
@login_required
@roles_required("admin", "teacher")
def analytics_recent_activity():
    denied = _teacher_global_data_denied()
    if denied is not None:
        return denied
    return jsonify(analytics_service.recent_activity(limit=_limit(20)))
