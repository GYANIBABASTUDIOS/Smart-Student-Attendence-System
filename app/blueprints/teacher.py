"""Teacher Panel foundation routes."""

from __future__ import annotations

import secrets
import threading
from datetime import date, timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.extensions import db, limiter
from app.models import AttendanceRecord, Student, User
from app.services import attendance_service, student_service, teacher_student_service
from app.services.attendance_service import AttendanceError
from app.services.student_service import StudentError
from app.services.teacher_dashboard_service import dashboard_data
from app.utils.security import active_teacher_profile, teacher_required
from app.utils.time import today
from app.utils.validators import clamp_page_size, validate_student_data

teacher_bp = Blueprint("teacher", __name__, url_prefix="/teacher")


@teacher_bp.route("/", methods=["GET"])
@login_required
@teacher_required
def index():
    """Render a dashboard scoped to the authenticated teacher's active assignments."""
    return render_template(
        "teacher/dashboard.html",
        **dashboard_data(g.teacher_profile),
    )


@teacher_bp.route("/attendance", methods=["GET"])
@login_required
@teacher_required
def attendance():
    """Show one assigned class roster and its daily attendance history."""
    teacher = g.teacher_profile
    classes = teacher_student_service.assigned_classes(teacher.id)
    class_section_id = _requested_assigned_class_id(teacher.id, request.args.get("class_section_id"))
    selected_date = _parse_attendance_date(request.args.get("date"))
    students = []
    records_by_student = {}
    history = []

    if class_section_id is not None:
        students = list(
            db.session.execute(
                db.select(Student)
                .where(
                    Student.class_section_id == class_section_id,
                    Student.is_active.is_(True),
                )
                .order_by(Student.name, Student.id)
            ).scalars()
        )
        records = db.session.execute(
            db.select(AttendanceRecord).where(
                AttendanceRecord.student_id.in_([student.id for student in students]),
                AttendanceRecord.date == selected_date,
            )
        ).scalars().all() if students else []
        records_by_student = {record.student_id: record for record in records}

        # AttendanceRecord has no historical class/session foreign key. This view
        # therefore reports history for students currently linked to this class;
        # it does not infer past assignments.
        history = db.session.execute(
            db.select(AttendanceRecord)
            .join(Student, Student.id == AttendanceRecord.student_id)
            .options(joinedload(AttendanceRecord.student))
            .where(
                Student.class_section_id == class_section_id,
                AttendanceRecord.date <= selected_date,
                AttendanceRecord.date >= selected_date - timedelta(days=90),
            )
            .order_by(AttendanceRecord.date.desc(), AttendanceRecord.time_in.desc())
            .limit(100)
        ).unique().scalars().all()

    return render_template(
        "teacher/attendance.html",
        classes=classes,
        current_class_id=class_section_id,
        selected_date=selected_date.isoformat(),
        students=students,
        records_by_student=records_by_student,
        history=history,
        statuses=current_app.config["ATTENDANCE_STATUSES"],
        recognition_active=_teacher_recognition_owned_by_current_user(),
        today=today().isoformat(),
    )


@teacher_bp.route(
    "/attendance/<int:class_section_id>/students/<int:student_id>", methods=["POST"]
)
@login_required
@teacher_required
@limiter.limit("60 per minute")
def mark_class_student_attendance(class_section_id: int, student_id: int):
    """Mark one student only after checking the active class assignment and membership."""
    section = teacher_student_service.assigned_class(g.teacher_profile.id, class_section_id)
    if section is None:
        abort(404)
    student = db.session.execute(
        db.select(Student).where(
            Student.id == student_id,
            Student.class_section_id == section.id,
            Student.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if student is None:
        abort(404)

    try:
        mark_date = _parse_attendance_date(request.form.get("date"))
        status = attendance_service.validate_status(request.form.get("status", ""))
        result = attendance_service.mark_attendance(
            student,
            status=status,
            on_date=mark_date,
            marked_by=f"Teacher/{current_user.username}"[:64],
            marked_by_user_id=current_user.id,
        )
    except AttendanceError as exc:
        flash(str(exc), "error")
        return redirect(
            url_for("teacher.attendance", class_section_id=section.id, date=request.form.get("date"))
        )
    flash(
        result.message if result.created else f"{result.message}. No duplicate record was created.",
        "success" if result.created else "warning",
    )
    return redirect(
        url_for("teacher.attendance", class_section_id=section.id, date=mark_date.isoformat())
    )


@teacher_bp.route("/attendance/recognition/start", methods=["POST"])
@login_required
@teacher_required
@limiter.limit("10 per minute")
def start_class_recognition():
    """Start the existing LBPH pipeline with only this assigned class's valid labels."""
    class_id = request.form.get("class_section_id") or (request.get_json(silent=True) or {}).get(
        "class_section_id"
    )
    try:
        class_id = int(class_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Select a valid assigned class."}), 400
    section = teacher_student_service.assigned_class(g.teacher_profile.id, class_id)
    if section is None:
        return jsonify({"success": False, "message": "That class is not actively assigned to you."}), 403

    from app.recognition import CameraError, KnownStudent, get_pipeline

    pipeline = get_pipeline()
    if not pipeline.recognizer.is_trained:
        return jsonify(
            {"success": False, "message": "The saved face model is unavailable. Ask an administrator to verify enrollment."}
        ), 409

    students = db.session.execute(
        db.select(Student).where(
            Student.class_section_id == section.id,
            Student.is_active.is_(True),
        )
    ).scalars().all()
    label_map = pipeline.recognizer.label_map
    known = [
        KnownStudent(pk=student.id, name=student.name, roll=student.student_id)
        for student in students
        if student.face_samples_count > 0
        and student.face_model_label is not None
        and label_map.get(student.face_model_label) == student.id
    ]
    if not known:
        return jsonify(
            {
                "success": False,
                "message": "No students in this class have face samples with a valid saved model label. No model was retrained.",
            }
        ), 409

    lock, owner = _recognition_control()
    with lock:
        owner = current_app.extensions.get("teacher_recognition_owner")
        if pipeline.is_running:
            if (
                _owner_matches(owner, g.teacher_profile.id)
                and owner["class_section_id"] == section.id
            ):
                return jsonify({"success": True, "message": "Recognition is already running for this class."})
            return jsonify({"success": False, "message": "The shared camera is already in use."}), 409
        pipeline.load_known(known)
        try:
            pipeline.start(preview_only=False)
        except CameraError as exc:
            return jsonify({"success": False, "message": str(exc)}), 503
        owner_token = secrets.token_urlsafe(32)
        current_app.extensions["teacher_recognition_owner"] = {
            "user_id": current_user.id,
            "teacher_id": g.teacher_profile.id,
            "class_section_id": section.id,
            "started_on": today().isoformat(),
            "session_token": owner_token,
        }
        session["teacher_recognition_token"] = owner_token
    missing = len(students) - len(known)
    message = f"Recognition started for {len(known)} enrolled student(s)."
    if missing:
        message += f" {missing} student(s) lack a valid saved enrollment/label and will remain unrecognized."
    return jsonify({"success": True, "message": message, "status": pipeline.status()})


@teacher_bp.route("/attendance/recognition/stop", methods=["POST"])
@login_required
@teacher_required
@limiter.limit("20 per minute")
def stop_class_recognition():
    from app.recognition import get_pipeline

    lock, owner = _recognition_control()
    with lock:
        owner = current_app.extensions.get("teacher_recognition_owner")
        if not _owner_matches(owner, g.teacher_profile.id):
            return jsonify({"success": False, "message": "You do not own the active recognition session."}), 403
        get_pipeline().stop()
        current_app.extensions.pop("teacher_recognition_owner", None)
        session.pop("teacher_recognition_token", None)
    return jsonify({"success": True, "message": "Class recognition stopped."})


@teacher_bp.route("/attendance/recognition/faces", methods=["GET"])
@login_required
@teacher_required
def class_recognition_faces():
    from app.recognition import get_pipeline

    owner_status = _teacher_recognition_owner_status()
    if owner_status is not None:
        return jsonify({"success": False, "message": "No accessible recognition session."}), owner_status
    if not _teacher_recognition_owned_by_current_user():
        return jsonify({"success": False, "message": "No active recognition session for this teacher."}), 409
    pipeline = get_pipeline()
    return jsonify(
        {"faces": pipeline.detected_faces(), "confirm_frames": pipeline.confirm_frames}
    )


@teacher_bp.route("/attendance/recognition/feed", methods=["GET"])
@login_required
@teacher_required
def class_recognition_feed():
    from app.recognition import get_pipeline

    pipeline = get_pipeline()
    owner_status = _teacher_recognition_owner_status()
    if owner_status is not None:
        return jsonify({"success": False, "message": "No accessible recognition session."}), owner_status
    if not _teacher_recognition_owned_by_current_user() or not pipeline.is_running:
        return jsonify({"success": False, "message": "No active recognition session for this teacher."}), 409
    return Response(
        pipeline.mjpeg_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@teacher_bp.route("/attendance/recognition/mark", methods=["POST"])
@login_required
@teacher_required
@limiter.limit("60 per minute")
def mark_class_confirmations():
    """Consume confirmed IDs only when still active in the owned assigned class."""
    from app.recognition import get_pipeline

    lock, owner = _recognition_control()
    with lock:
        owner = current_app.extensions.get("teacher_recognition_owner")
        if not _owner_matches(owner, g.teacher_profile.id):
            return jsonify({"success": False, "message": "No active recognition session for this teacher."}), 403
        if owner["started_on"] != today().isoformat():
            return jsonify({"success": False, "message": "Recognition session date has expired; restart it."}), 409
        section = teacher_student_service.assigned_class(
            g.teacher_profile.id, owner["class_section_id"]
        )
        if section is None:
            return jsonify({"success": False, "message": "The class assignment is no longer active."}), 403
        pipeline = get_pipeline()
        if not pipeline.is_recognizing:
            return jsonify({"success": False, "message": "Recognition is not running."}), 409
        confirmed = pipeline.take_confirmed()

    marked = []
    for student_id, confidence in confirmed.items():
        student = db.session.execute(
            db.select(Student).where(
                Student.id == student_id,
                Student.class_section_id == section.id,
                Student.is_active.is_(True),
                Student.face_samples_count > 0,
                Student.face_model_label.is_not(None),
            )
        ).scalar_one_or_none()
        if (
            student is None
            or get_pipeline().recognizer.label_map.get(student.face_model_label) != student.id
        ):
            continue
        result = attendance_service.mark_attendance(
            student,
            confidence=confidence,
            marked_by=f"Face/Teacher/{current_user.username}"[:64],
            marked_by_user_id=current_user.id,
        )
        marked.append(
            {
                "student_id": student.student_id,
                "name": student.name,
                "status": result.record.status,
                "created": result.created,
                "message": result.message,
            }
        )
    return jsonify({"success": True, "marked_students": marked})


@teacher_bp.route("/students", methods=["GET"])
@login_required
@teacher_required
def students():
    """Search the current teacher's active assignments only."""
    teacher = g.teacher_profile
    classes = teacher_student_service.assigned_classes(teacher.id)
    class_ids = {row.id for row in classes}
    raw_class_id = (request.args.get("class_section_id") or "").strip()
    class_section_id = None
    if raw_class_id:
        try:
            class_section_id = int(raw_class_id)
        except ValueError:
            abort(400)
        if class_section_id not in class_ids:
            abort(404)

    status = request.args.get("status", "active")
    if status not in {"active", "inactive", "all"}:
        abort(400)
    search = (request.args.get("search") or "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = clamp_page_size(
        request.args.get("per_page", type=int),
        current_app.config["STUDENTS_PER_PAGE"],
        current_app.config["MAX_PER_PAGE"],
    )

    query = teacher_student_service.scoped_student_query(teacher.id)
    if status == "active":
        query = query.where(Student.is_active.is_(True))
    elif status == "inactive":
        query = query.where(Student.is_active.is_(False))
    if class_section_id is not None:
        query = query.where(Student.class_section_id == class_section_id)
    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(Student.name.ilike(pattern), Student.student_id.ilike(pattern))
        )

    pagination = db.paginate(
        query.order_by(Student.name.asc(), Student.id.asc()),
        page=page,
        per_page=per_page,
        error_out=False,
    )
    return render_template(
        "teacher/students.html",
        students=pagination.items,
        pagination=pagination,
        classes=classes,
        search=search,
        current_status=status,
        current_class_id=class_section_id,
        per_page=per_page,
    )


@teacher_bp.route("/students/new", methods=["GET", "POST"])
@login_required
@teacher_required
@limiter.limit("20 per minute", methods=["POST"])
def create_student():
    teacher = g.teacher_profile
    classes = teacher_student_service.assigned_classes(teacher.id)
    if request.method == "GET":
        return _render_student_form("Register Student", {}, None, classes, [])

    class_section, class_error = _requested_class(teacher.id, request.form.get("class_section_id"))
    if class_error:
        return _render_student_form(
            "Register Student", request.form.to_dict(), None, classes, [class_error]
        ), 400
    if class_section is None:
        abort(403)

    errors, data = _student_form_data(request.form, class_section)
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    password_confirmation = request.form.get("password_confirmation") or ""
    if not username:
        errors.append("Username is required.")
    elif len(username) > 64:
        errors.append("Username must be 64 characters or fewer.")
    if not password:
        errors.append("A password is required.")
    elif len(password) < 12:
        errors.append("Password must be at least 12 characters.")
    if password and password != password_confirmation:
        errors.append("Passwords do not match.")
    if username and db.session.execute(
        db.select(User.id).filter_by(username=username).limit(1)
    ).first():
        errors.append("That username is already in use.")
    if data.get("email") and db.session.execute(
        db.select(User.id).filter_by(email=data["email"]).limit(1)
    ).first():
        errors.append("That email is already in use.")
    if errors:
        return _render_student_form(
            "Register Student", request.form.to_dict(), None, classes, errors
        ), 400
    try:
        student = student_service.register_student_account(
            data, class_section.id, username=username, password=password
        )
    except StudentError as exc:
        return _render_student_form(
            "Register Student", request.form.to_dict(), None, classes, [str(exc)]
        ), 400

    current_app.logger.info(
        "Teacher %s registered student %s in assigned class %s",
        teacher.id,
        student.id,
        class_section.id,
    )
    flash(f"{student.name} was added to {class_section.name}.", "success")
    return redirect(url_for("teacher.students"))


@teacher_bp.route("/students/<int:student_id>/edit", methods=["GET", "POST"])
@login_required
@teacher_required
@limiter.limit("30 per minute", methods=["POST"])
def edit_student(student_id: int):
    teacher = g.teacher_profile
    student = teacher_student_service.get_scoped_student(teacher.id, student_id)
    if student is None:
        abort(404)
    classes = teacher_student_service.assigned_classes(teacher.id)
    if request.method == "GET":
        values = {
            "student_id": student.student_id,
            "name": student.name,
            "email": student.email,
            "phone": student.phone or "",
            "class_section_id": str(student.class_section_id),
        }
        return _render_student_form("Edit Student", values, student, classes, [])

    class_section, class_error = _requested_class(teacher.id, request.form.get("class_section_id"))
    if class_error:
        values = request.form.to_dict()
        values["student_id"] = student.student_id
        return _render_student_form("Edit Student", values, student, classes, [class_error]), 400
    if class_section is None:
        abort(403)

    errors, data = _student_form_data(request.form, class_section, student=student)
    if errors:
        values = request.form.to_dict()
        values["student_id"] = student.student_id
        return _render_student_form("Edit Student", values, student, classes, errors), 400
    try:
        student_service.update_student(
            student.id, data, class_section_id=class_section.id
        )
    except StudentError as exc:
        values = request.form.to_dict()
        values["student_id"] = student.student_id
        return _render_student_form("Edit Student", values, student, classes, [str(exc)]), 400

    current_app.logger.info(
        "Teacher %s updated student %s in assigned class %s",
        teacher.id,
        student.id,
        class_section.id,
    )
    flash(f"{student.name} was updated.", "success")
    return redirect(url_for("teacher.students"))


@teacher_bp.route("/students/<int:student_id>/status", methods=["POST"])
@login_required
@teacher_required
@limiter.limit("20 per minute")
def update_student_status(student_id: int):
    teacher = g.teacher_profile
    student = teacher_student_service.get_scoped_student(teacher.id, student_id)
    if student is None:
        abort(404)
    raw_active = request.form.get("active")
    if raw_active not in {"true", "false"}:
        abort(400)
    target_active = raw_active == "true"
    if student.is_active == target_active:
        flash(f"{student.name} already has that status.", "info")
        return redirect(url_for("teacher.students"))
    try:
        name = (
            student_service.reactivate_student(student.id)
            if target_active
            else student_service.deactivate_student(student.id)
        )
    except StudentError as exc:
        abort(404, description=str(exc))
    current_app.logger.warning(
        "Teacher %s %s student %s",
        teacher.id,
        "reactivated" if target_active else "deactivated",
        student.id,
    )
    flash(f"{name} was {'reactivated' if target_active else 'deactivated'}.", "success")
    return redirect(url_for("teacher.students", status="inactive" if target_active else "active"))


def _requested_class(teacher_id: int, raw_class_id: str | None):
    if not raw_class_id:
        return None, "Choose an assigned class or section."
    try:
        class_id = int(raw_class_id)
    except (TypeError, ValueError):
        return None, "Choose a valid assigned class or section."
    section = teacher_student_service.assigned_class(teacher_id, class_id)
    return section, None


def _student_form_data(form, class_section, student: Student | None = None):
    department_name = class_section.department.name
    data = {
        "student_id": student.student_id if student else form.get("student_id"),
        "name": form.get("name"),
        "email": form.get("email"),
        "phone": form.get("phone"),
        # These legacy columns remain required by existing validation and are
        # derived from the selected canonical class to keep the values consistent.
        "department": department_name,
        "year": class_section.year,
        "section": class_section.section,
    }
    errors, data = validate_student_data(data)
    if len(data.get("email") or "") > 120:
        errors.append("Email address must be 120 characters or fewer.")
    if len(department_name) > 50:
        errors.append(
            "This department name exceeds the existing student department field limit. "
            "Ask an administrator to shorten it before registering students."
        )
    return errors, data


def _render_student_form(heading, values, student, classes, errors):
    return render_template(
        "teacher/student_form.html",
        heading=heading,
        values=values,
        student=student,
        classes=classes,
        errors=errors,
    )


def _requested_assigned_class_id(teacher_id: int, raw_class_id: str | None) -> int | None:
    if not raw_class_id:
        return None
    try:
        class_id = int(raw_class_id)
    except (TypeError, ValueError):
        abort(400)
    if teacher_student_service.assigned_class(teacher_id, class_id) is None:
        abort(404)
    return class_id


def _parse_attendance_date(raw_date: str | None) -> date:
    if not raw_date:
        return today()
    try:
        value = date.fromisoformat(raw_date)
    except (TypeError, ValueError):
        abort(400, description="Attendance date must use YYYY-MM-DD format.")
    if value > today():
        abort(400, description="Attendance cannot be marked for a future date.")
    return value


def _recognition_control():
    lock = current_app.extensions.setdefault(
        "teacher_recognition_lock", threading.RLock()
    )
    return lock, current_app.extensions.get("teacher_recognition_owner")


def _owner_matches(owner, teacher_id: int) -> bool:
    owner_token = owner.get("session_token") if owner else None
    request_token = session.get("teacher_recognition_token")
    return bool(
        owner
        and owner.get("user_id") == current_user.id
        and owner.get("teacher_id") == teacher_id
        and isinstance(owner_token, str)
        and isinstance(request_token, str)
        and secrets.compare_digest(owner_token, request_token)
    )


def _teacher_recognition_owned_by_current_user() -> bool:
    lock, owner = _recognition_control()
    with lock:
        owner = current_app.extensions.get("teacher_recognition_owner")
        if not _owner_matches(owner, active_teacher_profile().id):
            return False
        return (
            teacher_student_service.assigned_class(
                owner["teacher_id"], owner["class_section_id"]
            )
            is not None
        )


def _teacher_recognition_owner_status() -> int | None:
    # Read the shared camera owner and its assignment under the same lock used by
    # start/stop. Otherwise a concurrent stop/start could expose the replacement
    # session's faces to a request that checked a stale owner value.
    lock, _owner = _recognition_control()
    with lock:
        owner = current_app.extensions.get("teacher_recognition_owner")
        if owner is None:
            return 409
        profile = active_teacher_profile()
        if profile is None or not _owner_matches(owner, profile.id):
            return 403
        if teacher_student_service.assigned_class(
            profile.id, owner.get("class_section_id")
        ) is None:
            return 403
        return None
