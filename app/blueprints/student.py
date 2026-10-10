"""Student-owned dashboard, profile, attendance, and leave requests."""

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required
from sqlalchemy import func

from app.extensions import db, limiter
from app.models import AttendanceRecord, LeaveRequest
from app.services import leave_service, student_service
from app.services.leave_service import LeaveError
from app.services.student_service import StudentError
from app.utils.images import ImageValidationError, decode_data_url
from app.utils.security import student_required
from app.utils.time import today
from app.utils.validators import PHONE_RE, sanitize_input, validate_leave_request_data

student_bp = Blueprint("student", __name__, url_prefix="/student")


@student_bp.route("/", methods=["GET"])
@login_required
@student_required
def index():
    student = g.student_profile
    counts = _attendance_counts(student.id)
    recent_attendance = _recent_attendance(student.id)
    recent_leaves = _recent_leaves(student.id, limit=5)
    denominator = sum(counts.get(status, 0) for status in ("Present", "Late", "Absent"))
    present = counts.get("Present", 0) + counts.get("Late", 0)
    summary = {
        "recorded": sum(counts.values()),
        "present": present,
        "absent": counts.get("Absent", 0),
        "rate": round(present * 100 / denominator, 1) if denominator else None,
    }
    return render_template(
        "student/dashboard.html",
        student=student,
        attendance=summary,
        recent_attendance=recent_attendance,
        recent_leaves=recent_leaves,
    )


@student_bp.route("/attendance", methods=["GET"])
@login_required
@student_required
def attendance():
    student = g.student_profile
    records = db.session.execute(
        db.select(AttendanceRecord)
        .where(AttendanceRecord.student_id == student.id)
        .order_by(AttendanceRecord.date.desc(), AttendanceRecord.id.desc())
        .limit(100)
    ).scalars().all()
    return render_template("student/attendance.html", student=student, records=records)


@student_bp.route("/profile", methods=["GET", "POST"])
@login_required
@student_required
@limiter.limit("20 per minute", methods=["POST"])
def profile():
    student = g.student_profile
    if request.method == "POST":
        phone = sanitize_input(request.form.get("phone") or "")
        if phone and not PHONE_RE.fullmatch(phone):
            flash("Phone number must be 7-15 characters of digits and separators.", "error")
            return render_template("student/profile.html", student=student), 400
        student.phone = phone or None
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("student.profile"))
    return render_template("student/profile.html", student=student)


@student_bp.route("/profile/face-registration", methods=["GET", "POST"])
@login_required
@student_required
@limiter.limit("5 per minute", methods=["POST"])
def face_registration():
    student = g.student_profile
    errors: list[str] = []
    enrolled = student.has_face_enrolment
    if request.method == "POST":
        if request.form.get("consent") != "yes":
            errors.append("Please read and accept the face data consent before continuing.")
        photo_bytes = None
        if not errors:
            try:
                captured = request.form.get("captured_image")
                upload = request.files.get("image")
                if captured:
                    photo_bytes = decode_data_url(captured)
                elif upload and upload.filename:
                    photo_bytes = upload.read()
                else:
                    errors.append("Capture a photo or choose an image file to continue.")
                if photo_bytes:
                    student_service.enroll_student_face(student, photo_bytes)
            except (ImageValidationError, StudentError) as exc:
                errors.append(str(exc))
            except Exception:
                current_app.logger.exception(
                    "Unexpected self-service face enrollment error for student %s", student.id
                )
                errors.append("Face registration could not be completed. Please try again.")
            else:
                if not errors:
                    flash("Face registration completed successfully.", "success")
                    return redirect(url_for("student.profile"))

        enrolled = student.has_face_enrolment
        return render_template(
            "student/face_registration.html",
            student=student,
            errors=errors,
            enrolled=enrolled,
        ), 400
    return render_template(
        "student/face_registration.html", student=student, errors=errors, enrolled=enrolled
    )


@student_bp.route("/leaves", methods=["GET", "POST"])
@login_required
@student_required
@limiter.limit("20 per minute", methods=["POST"])
def leaves():
    student = g.student_profile
    errors: list[str] = []
    form_data = {
        "leave_type": "",
        "start_date": "",
        "end_date": "",
        "reason": "",
    }

    if request.method == "POST":
        # The browser does not choose the target student. Ignore any submitted
        # student_id and bind this request to the authenticated profile.
        form_data = {field: request.form.get(field, "") for field in form_data}
        data = {**form_data, "student_id": str(student.id)}
        errors, data = validate_leave_request_data(
            data,
            allowed_types=leave_service.allowed_types(),
            today_value=today(),
        )
        if not errors:
            try:
                leave_service.apply_for_leave(data)
            except LeaveError as exc:
                errors.append(str(exc))
            else:
                flash("Leave request submitted for review.", "success")
                return redirect(url_for("student.leaves"))

    requests_list = _recent_leaves(student.id, limit=100)
    return render_template(
        "student/leaves.html",
        student=student,
        leave_requests=requests_list,
        leave_types=leave_service.allowed_types(),
        form_data=form_data,
        errors=errors,
        today=today().isoformat(),
    ), (400 if errors else 200)


def _attendance_counts(student_id: int) -> dict[str, int]:
    rows = db.session.execute(
        db.select(AttendanceRecord.status, func.count(AttendanceRecord.id))
        .where(AttendanceRecord.student_id == student_id)
        .group_by(AttendanceRecord.status)
    ).all()
    return {status: count for status, count in rows}


def _recent_attendance(student_id: int) -> list[AttendanceRecord]:
    return list(
        db.session.execute(
            db.select(AttendanceRecord)
            .where(AttendanceRecord.student_id == student_id)
            .order_by(AttendanceRecord.date.desc(), AttendanceRecord.id.desc())
            .limit(8)
        ).scalars()
    )


def _recent_leaves(student_id: int, limit: int) -> list[LeaveRequest]:
    return list(
        db.session.execute(
            db.select(LeaveRequest)
            .where(LeaveRequest.student_id == student_id)
            .order_by(LeaveRequest.created_at.desc(), LeaveRequest.id.desc())
            .limit(limit)
        ).scalars()
    )
