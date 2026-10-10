"""Attendance routes: listing, marking, reports and export."""

from __future__ import annotations

from datetime import timedelta

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db, limiter
from app.models import AttendanceRecord, Student
from app.services import attendance_service, export_service, teacher_student_service
from app.services.analytics_service import DateRange
from app.services.attendance_service import AttendanceError
from app.utils.security import active_teacher_profile, roles_required
from app.utils.time import today, utcnow
from app.utils.validators import clamp_page_size

attendance_bp = Blueprint("attendance", __name__)


@attendance_bp.route("/attendance")
@login_required
@roles_required("admin", "teacher")
def list_attendance():
    teacher = _current_teacher()
    if teacher is not None:
        return redirect(url_for("teacher.attendance"))
    page = request.args.get("page", 1, type=int)
    per_page = clamp_page_size(
        request.args.get("per_page", type=int),
        current_app.config["ATTENDANCE_PER_PAGE"],
        current_app.config["MAX_PER_PAGE"],
    )
    date_filter = request.args.get("date", today().isoformat())
    department = request.args.get("department", "")
    year = request.args.get("year", "")
    status = request.args.get("status", "")
    search = (request.args.get("search") or "").strip()

    query = db.select(AttendanceRecord).options(db.joinedload(AttendanceRecord.student))

    # The old route called .join(Student) once per active filter, so combining
    # department + year + search produced three joins of the same table and SQLAlchemy
    # raised a cartesian-product warning. Join once.
    needs_join = bool(search or department or year)
    if needs_join:
        query = query.join(Student, AttendanceRecord.student_id == Student.id)

    if date_filter:
        query = query.where(AttendanceRecord.date == date_filter)
    if search:
        pattern = f"%{search}%"
        query = query.where(db.or_(Student.name.ilike(pattern), Student.student_id.ilike(pattern)))
    if department:
        query = query.where(Student.department == department)
    if year:
        query = query.where(Student.year == year)
    if status:
        query = query.where(AttendanceRecord.status == status)

    pagination = db.paginate(
        query.order_by(AttendanceRecord.created_at.desc()),
        page=page,
        per_page=per_page,
        error_out=False,
    )

    facets = db.session.execute(db.select(Student.department, Student.year).distinct()).all()

    return render_template(
        "attendance_clean.html",
        records=pagination.items,
        pagination=pagination,
        departments=sorted({row[0] for row in facets if row[0]}),
        years=sorted({row[1] for row in facets if row[1]}),
        statuses=list(current_app.config["ATTENDANCE_STATUSES"]),
        current_date=date_filter,
        current_department=department,
        current_year=year,
        current_status=status,
        current_search=search,
        per_page=per_page,
    )


@attendance_bp.route("/mark_attendance")
@login_required
@roles_required("admin", "teacher")
def mark_page():
    if _current_teacher() is not None:
        return redirect(url_for("teacher.attendance"))
    return render_template("mark_attendance_clean.html")


@attendance_bp.route("/mark_manual_attendance", methods=["POST"])
@login_required
@roles_required("admin", "teacher")
@limiter.limit("60 per minute")
def mark_manual():
    teacher = _current_teacher()
    roll = (request.form.get("student_id") or "").strip()
    if not roll:
        flash("Student ID is required", "error")
        return redirect(url_for("attendance.mark_page"))

    try:
        if teacher is not None:
            student = db.session.execute(
                teacher_student_service.scoped_student_query(teacher.id)
                .where(Student.student_id == roll, Student.is_active.is_(True))
            ).unique().scalar_one_or_none()
            if student is None:
                raise AttendanceError("No active student in your assigned classes has that ID")
            result = attendance_service.mark_attendance(
                student,
                marked_by=f"Teacher/{current_user.username}"[:64],
                marked_by_user_id=current_user.id,
            )
        else:
            result = attendance_service.mark_by_roll_number(
                roll, marked_by=f"Manual/{current_user.username}", confidence=1.0
            )
    except AttendanceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("attendance.mark_page"))

    flash(result.message, "success" if result.created else "warning")
    return redirect(url_for("attendance.mark_page"))


@attendance_bp.route("/mark_student_present", methods=["POST"])
@login_required
@roles_required("admin", "teacher")
@limiter.limit("120 per minute")
def mark_student_present():
    """Mark a single detected student, from the recognition UI."""
    # Teachers use the class-bound recognition endpoints below /teacher/attendance;
    # this legacy endpoint accepts an arbitrary student ID and cannot establish that
    # the identity came from their assigned-class pipeline.
    if _current_teacher() is not None:
        return jsonify({"success": False, "message": "Use class-scoped Teacher attendance recognition."}), 403
    payload = request.get_json(silent=True) or {}
    student_pk = payload.get("student_id")
    if not student_pk:
        return jsonify({"success": False, "message": "Student ID required"}), 400

    student = db.session.get(Student, int(student_pk))
    if student is None:
        return jsonify({"success": False, "message": "Student not found"}), 404

    result = attendance_service.mark_attendance(
        student,
        confidence=payload.get("confidence"),
        marked_by=f"Face Recognition/{current_user.username}",
    )
    return jsonify(
        {
            "success": result.created,
            "message": result.message,
            "student_name": student.name,
            "status": result.record.status,
            "already_marked": not result.created,
        }
    )


@attendance_bp.route("/auto_mark_attendance", methods=["POST"])
@login_required
@roles_required("admin", "teacher")
@limiter.limit("120 per minute")
def auto_mark():
    """Mark every student the pipeline has *confirmed*.

    The old version marked anything the histogram matcher scored above 0.3 in a single
    frame.  This only writes students that survived the consecutive-frame and margin
    checks, and it drains the confirmed set so the same confirmation cannot be
    double-counted by two polling clients.
    """
    if _current_teacher() is not None:
        return jsonify({"success": False, "message": "Use class-scoped Teacher attendance recognition."}), 403

    from app.recognition import get_pipeline

    pipeline = get_pipeline()
    if not pipeline.is_recognizing:
        return jsonify({"success": False, "message": "Face recognition is not running"}), 409

    confirmed = pipeline.take_confirmed()
    if not confirmed:
        return jsonify(
            {"success": False, "message": "No confirmed students to mark", "marked_students": []}
        )

    students = (
        db.session.execute(db.select(Student).where(Student.id.in_(list(confirmed))))
        .scalars()
        .all()
    )

    marked = []
    for student in students:
        result = attendance_service.mark_attendance(
            student,
            confidence=confirmed.get(student.id),
            marked_by="Face Recognition",
        )
        if result.created:
            marked.append(
                {
                    "name": student.name,
                    "student_id": student.student_id,
                    "status": result.record.status,
                    "confidence": confirmed.get(student.id),
                }
            )

    return jsonify(
        {
            "success": bool(marked),
            "message": (
                f"Marked {len(marked)} student(s) present"
                if marked
                else "Confirmed students were already marked today"
            ),
            "marked_students": marked,
        }
    )


@attendance_bp.route("/update_attendance_status", methods=["POST"])
@login_required
@roles_required("admin", "teacher")
@limiter.limit("60 per minute")
def update_status():
    teacher = _current_teacher()
    if teacher is not None:
        return jsonify({"success": False, "message": "Attendance status changes must use the class roster workflow."}), 403
    payload = request.get_json(silent=True) or {}
    record_id = payload.get("record_id")
    status = payload.get("status")
    if not record_id or not status:
        return jsonify({"success": False, "message": "Record ID and status are required"}), 400

    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Record ID must be an integer"}), 400
    existing = db.session.get(AttendanceRecord, record_id)
    if existing is None:
        return jsonify({"success": False, "message": "Attendance record not found"}), 404
    _require_teacher_record_scope(teacher, existing.student_id)

    try:
        record = attendance_service.update_status(
            record_id, status, marked_by=f"Manual/{current_user.username}"
        )
    except AttendanceError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    _require_teacher_record_scope(teacher, record.student_id)

    return jsonify(
        {
            "success": True,
            "message": f"Updated to {record.status}",
            "student_name": record.student.name if record.student else None,
            "status": record.status,
        }
    )


@attendance_bp.route("/mark_student_status/<int:student_id>/<status>", methods=["POST"])
@login_required
@roles_required("admin", "teacher")
@limiter.limit("120 per minute")
def mark_status(student_id: int, status: str):
    """Quick toggle from the students table.

    This route used to pass ``marked_by='Manual'`` to a model that had no such column,
    raising a TypeError on every call that created a new record.
    """
    teacher = _current_teacher()
    student = (
        teacher_student_service.get_scoped_student(teacher.id, student_id)
        if teacher is not None
        else db.session.get(Student, student_id)
    )
    if student is None or (teacher is not None and not student.is_active):
        return jsonify({"success": False, "message": "Student not found"}), 404

    try:
        result = attendance_service.mark_attendance(
            student,
            status=status,
            marked_by=(
                f"Teacher/{current_user.username}"
                if teacher is not None
                else f"Manual/{current_user.username}"
            )[:64],
            marked_by_user_id=current_user.id,
            overwrite=teacher is None,
        )
    except AttendanceError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    return jsonify(
        {
            "success": True,
            "message": result.message,
            "student_name": student.name,
            "status": result.record.status,
        }
    )


@attendance_bp.route("/mark_time_out/<int:record_id>", methods=["POST"])
@login_required
@roles_required("admin", "teacher")
@limiter.limit("60 per minute")
def time_out(record_id: int):
    teacher = _current_teacher()
    existing = db.session.get(AttendanceRecord, record_id)
    if existing is None:
        return jsonify({"success": False, "message": "Attendance record not found"}), 404
    _require_teacher_record_scope(teacher, existing.student_id)
    try:
        record = attendance_service.mark_time_out(record_id)
    except AttendanceError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    _require_teacher_record_scope(teacher, record.student_id)
    return jsonify(
        {"success": True, "message": f"Time out recorded at {record.time_out.isoformat()}"}
    )


@attendance_bp.route("/delete_attendance/<int:record_id>", methods=["POST"])
@login_required
@roles_required("admin", "teacher")
@limiter.limit("30 per minute")
def delete_record(record_id: int):
    teacher = _current_teacher()
    if teacher is not None:
        return jsonify({"success": False, "message": "Teachers cannot delete attendance records."}), 403
    record = db.session.get(AttendanceRecord, record_id)
    if record is None:
        return jsonify({"success": False, "message": "Attendance record not found"}), 404
    _require_teacher_record_scope(teacher, record.student_id)
    try:
        name = attendance_service.delete_record(record_id)
    except AttendanceError as exc:
        return jsonify({"success": False, "message": str(exc)}), 404
    current_app.logger.info(
        "Attendance record %s for %s deleted by %s", record_id, name, current_user.username
    )
    return jsonify({"success": True, "message": f"Record for {name} deleted"})


@attendance_bp.route("/reports")
@login_required
@roles_required("admin", "teacher")
def reports():
    if _current_teacher() is not None:
        return redirect(url_for("teacher.attendance"))
    if current_user.has_role("admin"):
        return redirect(url_for("admin.reports"))
    return jsonify({"success": False, "message": "Reports are unavailable for this account."}), 403


@attendance_bp.route("/export_attendance")
@login_required
@roles_required("admin", "teacher")
@limiter.limit("10 per minute")
def export():
    """Stream a CSV or XLSX export.

    Previously this wrote a timestamped file into ``exports/`` on every request and
    served it from disk, with nothing ever deleting them, and ran an unbounded query.
    """
    if _current_teacher() is not None:
        return redirect(url_for("teacher.attendance"))
    fmt = request.args.get("format", "csv")
    reference = today()
    window = DateRange(
        start=_iso_or(request.args.get("date_from"), reference - timedelta(days=29)),
        end=_iso_or(request.args.get("date_to"), reference),
    )
    limit = current_app.config["EXPORT_MAX_ROWS"]

    records = (
        db.session.execute(
            db.select(AttendanceRecord)
            .options(db.joinedload(AttendanceRecord.student))
            .where(AttendanceRecord.date >= window.start, AttendanceRecord.date <= window.end)
            .order_by(AttendanceRecord.date.desc(), AttendanceRecord.id.desc())
            .limit(limit)
        )
        .unique()
        .scalars()
        .all()
    )

    if not records:
        flash("No records found for that date range", "warning")
        return redirect(url_for("attendance.list_attendance"))

    if len(records) == limit:
        current_app.logger.warning("Export truncated at %d rows", limit)

    payload, mimetype, extension = export_service.build_export(records, fmt)
    filename = f"attendance_{window.start.isoformat()}_to_{window.end.isoformat()}.{extension}"
    current_app.logger.info(
        "Export of %d records (%s) by %s", len(records), extension, current_user.username
    )
    return Response(
        payload,
        mimetype=mimetype,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Rows": str(len(records)),
            "X-Export-Generated": utcnow().isoformat(),
        },
    )


def _iso_or(raw: str | None, fallback):
    from app.utils.time import parse_date

    return parse_date(raw, fallback)


def _current_teacher():
    """Resolve Teacher authority from the authenticated session, never request data."""
    if not current_user.has_role("teacher"):
        return None
    teacher = active_teacher_profile()
    if teacher is None:
        abort(403)
    return teacher


def _require_teacher_record_scope(teacher, student_id: int) -> None:
    if teacher is not None and teacher_student_service.get_scoped_student(
        teacher.id, student_id
    ) is None:
        abort(404)
