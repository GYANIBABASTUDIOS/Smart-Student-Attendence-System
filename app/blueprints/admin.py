"""Admin-only dashboard and teacher management routes."""

from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db, limiter
from app.models import (
    ClassSection,
    Department,
    Teacher,
    TeacherClassAssignment,
    User,
)
from app.services.admin_service import (
    ReportFilters,
    admin_reports_csv,
    admin_reports_data,
    dashboard_data,
)
from app.services.analytics_service import DateRange
from app.services.structure_service import (
    StructureValidationError,
    assign_teacher,
    remove_teacher_assignment,
    save_class_section,
    save_department,
    set_class_section_active,
    set_department_active,
)
from app.services.teacher_service import TeacherValidationError, save_teacher, set_teacher_active
from app.services.user_service import UserManagementError
from app.utils.security import admin_required
from app.utils.time import today

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.before_request
def _require_admin_role():
    """Fail closed for every Admin endpoint, including future routes.

    The per-view ``@admin_required`` decorators remain useful documentation and
    defense in depth. This blueprint guard prevents a newly added route from being
    exposed if its decorator is accidentally omitted. Anonymous requests continue to
    the view's ``@login_required`` handler and retain the normal login redirect.
    """
    if current_user.is_authenticated and not current_user.is_admin:
        abort(403)


@admin_bp.route("/", methods=["GET"])
@login_required
@admin_required
def index():
    """Render the administrative overview."""
    return render_template("admin/dashboard.html", **dashboard_data())


@admin_bp.route("/reports", methods=["GET"])
@login_required
@admin_required
def reports():
    """Admin-only aggregate reports; invalid filters are returned as a safe 400."""
    filters, error = _report_filters()
    departments = db.session.execute(select(Department).order_by(Department.name)).scalars().all()
    classes_query = select(ClassSection).options(joinedload(ClassSection.department))
    if filters and filters.department_id is not None:
        classes_query = classes_query.where(ClassSection.department_id == filters.department_id)
    classes = db.session.execute(classes_query.order_by(ClassSection.name)).scalars().all()
    data = admin_reports_data(filters) if filters else None
    return render_template(
        "admin/reports.html",
        filters=filters,
        error=error,
        data=data,
        departments=departments,
        classes=classes,
    ), 400 if error else 200


@admin_bp.route("/reports/export.csv", methods=["GET"])
@login_required
@admin_required
@limiter.limit("30 per hour")
def export_reports():
    """Download filtered aggregate trends; never exports student-level details."""
    filters, error = _report_filters()
    if error:
        return error, 400
    data = admin_reports_data(filters)
    return Response(
        admin_reports_csv(data),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="attendance-report.csv"',
            "Cache-Control": "no-store",
        },
    )


def _report_filters() -> tuple[ReportFilters | None, str | None]:
    reference = today()
    default_start = reference - timedelta(days=29)
    raw_start = request.args.get("start_date")
    raw_end = request.args.get("end_date")
    try:
        start = date.fromisoformat(raw_start) if raw_start else default_start
        end = date.fromisoformat(raw_end) if raw_end else reference
    except (TypeError, ValueError):
        return None, "Enter valid start and end dates."
    if start > end:
        return None, "Start date must be on or before end date."
    if end > reference:
        return None, "Future dates are not available in attendance reports."
    if (end - start).days > 365:
        return None, "Choose a date range of 366 days or fewer."

    raw_department = request.args.get("department_id", "").strip()
    raw_class = request.args.get("class_section_id", "").strip()
    try:
        department_id = int(raw_department) if raw_department else None
        class_section_id = int(raw_class) if raw_class else None
    except ValueError:
        return None, "Choose a valid department and class section."
    if department_id is not None and db.session.get(Department, department_id) is None:
        return None, "The selected department does not exist."
    class_section = db.session.get(ClassSection, class_section_id) if class_section_id is not None else None
    if class_section_id is not None and class_section is None:
        return None, "The selected class section does not exist."
    if class_section and department_id and class_section.department_id != department_id:
        return None, "The selected class section is outside the selected department."
    if class_section:
        department_id = class_section.department_id
    return ReportFilters(
        window=DateRange(start=start, end=end),
        department_id=department_id,
        class_section_id=class_section_id,
    ), None


@admin_bp.route("/departments", methods=["GET"])
@login_required
@admin_required
def departments():
    """Search and filter academic departments with related record counts."""
    query = select(Department)
    search = (request.args.get("q") or "").strip()
    status = request.args.get("status", "active")
    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(Department.name.ilike(pattern), Department.code.ilike(pattern))
        )
    if status == "active":
        query = query.where(Department.is_active.is_(True))
    elif status == "inactive":
        query = query.where(Department.is_active.is_(False))
    else:
        status = "all"
    rows = db.session.execute(query.order_by(Department.name)).scalars().all()
    teacher_counts = dict(
        db.session.execute(
            select(Teacher.department_id, func.count(Teacher.id))
            .where(Teacher.department_id.is_not(None))
            .group_by(Teacher.department_id)
        ).all()
    )
    class_counts = dict(
        db.session.execute(
            select(ClassSection.department_id, func.count(ClassSection.id))
            .group_by(ClassSection.department_id)
        ).all()
    )
    return render_template(
        "admin/departments.html",
        departments=rows,
        teacher_counts=teacher_counts,
        class_counts=class_counts,
        search=search,
        status=status,
    )


@admin_bp.route("/departments/new", methods=["GET", "POST"])
@login_required
@admin_required
@limiter.limit("30 per hour", methods=["POST"])
def create_department():
    if request.method == "POST":
        try:
            department = save_department(None, request.form)
        except StructureValidationError as exc:
            return _render_department_form(
                "Add Department", request.form.to_dict(), None, exc.errors
            ), 400
        flash(f"Department {department.name} was created.", "success")
        return redirect(url_for("admin.departments"))
    return _render_department_form("Add Department", {}, None, {})


@admin_bp.route("/departments/<int:department_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
@limiter.limit("60 per hour", methods=["POST"])
def edit_department(department_id: int):
    department = db.get_or_404(Department, department_id)
    if request.method == "POST":
        try:
            department = save_department(department, request.form)
        except StructureValidationError as exc:
            return _render_department_form(
                "Edit Department", request.form.to_dict(), department, exc.errors
            ), 400
        flash(f"Department {department.name} was updated.", "success")
        return redirect(url_for("admin.departments"))
    values = {
        "name": department.name,
        "code": department.code,
        "description": department.description or "",
    }
    return _render_department_form("Edit Department", values, department, {})


@admin_bp.route("/departments/<int:department_id>/status", methods=["POST"])
@login_required
@admin_required
@limiter.limit("30 per minute")
def update_department_status(department_id: int):
    department = db.get_or_404(Department, department_id)
    active = _requested_active_state()
    set_department_active(department, active)
    flash(
        f"Department {department.name} was {'activated' if active else 'deactivated'}.",
        "success",
    )
    return redirect(url_for("admin.departments"))


@admin_bp.route("/classes", methods=["GET"])
@login_required
@admin_required
def classes():
    """Search and filter class sections with their current teachers."""
    query = select(ClassSection).options(
        joinedload(ClassSection.department),
        selectinload(ClassSection.teacher_assignments)
        .joinedload(TeacherClassAssignment.teacher)
        .joinedload(Teacher.user),
    )
    search = (request.args.get("q") or "").strip()
    status = request.args.get("status", "active")
    department_id = request.args.get("department_id", type=int)
    if search:
        pattern = f"%{search}%"
        query = query.join(ClassSection.department).where(
            or_(
                ClassSection.name.ilike(pattern),
                ClassSection.year.ilike(pattern),
                ClassSection.section.ilike(pattern),
                Department.name.ilike(pattern),
            )
        )
    if status == "active":
        query = query.where(ClassSection.is_active.is_(True))
    elif status == "inactive":
        query = query.where(ClassSection.is_active.is_(False))
    else:
        status = "all"
    if department_id:
        query = query.where(ClassSection.department_id == department_id)
    rows = db.session.execute(query.order_by(ClassSection.name, ClassSection.year, ClassSection.section)).unique().scalars().all()
    departments = db.session.execute(select(Department).order_by(Department.name)).scalars().all()
    return render_template(
        "admin/classes.html",
        classes=rows,
        departments=departments,
        search=search,
        status=status,
        department_id=department_id,
    )


@admin_bp.route("/classes/new", methods=["GET", "POST"])
@login_required
@admin_required
@limiter.limit("30 per hour", methods=["POST"])
def create_class():
    if request.method == "POST":
        try:
            class_section = save_class_section(None, request.form)
        except StructureValidationError as exc:
            return _render_class_form(
                "Add Class / Section", request.form.to_dict(), None, exc.errors
            ), 400
        flash(f"Class section {class_section.name} was created.", "success")
        return redirect(url_for("admin.classes"))
    return _render_class_form("Add Class / Section", {}, None, {})


@admin_bp.route("/classes/<int:class_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
@limiter.limit("60 per hour", methods=["POST"])
def edit_class(class_id: int):
    class_section = db.get_or_404(ClassSection, class_id)
    if request.method == "POST":
        try:
            class_section = save_class_section(class_section, request.form)
        except StructureValidationError as exc:
            return _render_class_form(
                "Edit Class / Section", request.form.to_dict(), class_section, exc.errors
            ), 400
        flash(f"Class section {class_section.name} was updated.", "success")
        return redirect(url_for("admin.classes"))
    values = {
        "name": class_section.name,
        "year": class_section.year,
        "section": class_section.section,
        "department_id": str(class_section.department_id),
    }
    return _render_class_form("Edit Class / Section", values, class_section, {})


@admin_bp.route("/classes/<int:class_id>/status", methods=["POST"])
@login_required
@admin_required
@limiter.limit("30 per minute")
def update_class_status(class_id: int):
    class_section = db.get_or_404(ClassSection, class_id)
    active = _requested_active_state()
    set_class_section_active(class_section, active)
    flash(
        f"Class section {class_section.name} was {'activated' if active else 'deactivated'}.",
        "success",
    )
    return redirect(url_for("admin.classes"))


@admin_bp.route("/classes/<int:class_id>/assignments", methods=["POST"])
@login_required
@admin_required
@limiter.limit("60 per hour")
def add_class_teacher(class_id: int):
    class_section = db.get_or_404(ClassSection, class_id)
    try:
        teacher_id = int(request.form.get("teacher_id", ""))
        assign_teacher(class_section, teacher_id)
    except (ValueError, StructureValidationError) as exc:
        message = exc.errors.get("teacher_id", "Choose a valid teacher.") if isinstance(exc, StructureValidationError) else "Choose a valid teacher."
        flash(message, "error")
        return redirect(url_for("admin.edit_class", class_id=class_id))
    flash("Teacher assigned to the class section.", "success")
    return redirect(url_for("admin.edit_class", class_id=class_id))


@admin_bp.route(
    "/classes/<int:class_id>/assignments/<int:assignment_id>/remove", methods=["POST"]
)
@login_required
@admin_required
@limiter.limit("60 per hour")
def remove_class_teacher(class_id: int, assignment_id: int):
    class_section = db.get_or_404(ClassSection, class_id)
    try:
        remove_teacher_assignment(class_section, assignment_id)
    except LookupError:
        flash("That teacher assignment is no longer active.", "error")
    else:
        flash("Teacher assignment was removed.", "success")
    return redirect(url_for("admin.edit_class", class_id=class_id))


@admin_bp.route("/teachers", methods=["GET"])
@login_required
@admin_required
def teachers():
    """Search and filter the teacher directory."""
    query = select(Teacher).options(
        joinedload(Teacher.user),
        joinedload(Teacher.department),
        selectinload(Teacher.class_assignments).joinedload(TeacherClassAssignment.class_section),
    )
    search = (request.args.get("q") or "").strip()
    status = request.args.get("status", "active")
    department_id = request.args.get("department_id", type=int)

    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(
                Teacher.name.ilike(pattern),
                Teacher.employee_id.ilike(pattern),
                Teacher.user.has(or_(User.username.ilike(pattern), User.email.ilike(pattern))),
            )
        )
    if status == "active":
        query = query.where(Teacher.is_active.is_(True), Teacher.user.has(is_active_flag=True))
    elif status == "inactive":
        query = query.where(
            or_(Teacher.is_active.is_(False), Teacher.user.has(is_active_flag=False))
        )
    else:
        status = "all"
    if department_id:
        query = query.where(Teacher.department_id == department_id)

    rows = db.session.execute(query.order_by(Teacher.name, Teacher.id)).unique().scalars().all()
    departments = db.session.execute(
        select(Department).order_by(Department.name)
    ).scalars().all()
    return render_template(
        "admin/teachers.html",
        teachers=rows,
        departments=departments,
        search=search,
        status=status,
        department_id=department_id,
        teacher_count=len(rows),
    )


@admin_bp.route("/teachers/new", methods=["GET", "POST"])
@login_required
@admin_required
@limiter.limit("30 per hour", methods=["POST"])
def create_teacher():
    """Create a teacher profile with its teacher-role login account."""
    if request.method == "POST":
        try:
            teacher = save_teacher(None, request.form)
        except TeacherValidationError as exc:
            return _render_teacher_form(
                "Add Teacher", request.form.to_dict(), None, exc.errors
            ), 400
        flash(f"Teacher account for {teacher.name} was created.", "success")
        return redirect(url_for("admin.teachers"))

    return _render_teacher_form("Add Teacher", {}, None, {})


@admin_bp.route("/teachers/<int:teacher_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
@limiter.limit("60 per hour", methods=["POST"])
def edit_teacher(teacher_id: int):
    """Edit a teacher's profile, login details, and available assignments."""
    teacher = db.get_or_404(Teacher, teacher_id)
    if request.method == "POST":
        try:
            teacher = save_teacher(teacher, request.form)
        except TeacherValidationError as exc:
            return _render_teacher_form(
                "Edit Teacher",
                request.form.to_dict(),
                teacher,
                exc.errors,
                [int(value) for value in request.form.getlist("class_ids") if value.isdigit()],
            ), 400
        flash(f"Teacher account for {teacher.name} was updated.", "success")
        return redirect(url_for("admin.teachers"))

    values = {
        "name": teacher.name,
        "employee_id": teacher.employee_id,
        "username": teacher.user.username,
        "email": teacher.user.email,
        "phone": teacher.phone or "",
        "department_id": str(teacher.department_id or ""),
    }
    selected_class_ids = [
        row.class_section_id for row in teacher.class_assignments if row.is_active
    ]
    return _render_teacher_form("Edit Teacher", values, teacher, {}, selected_class_ids)


@admin_bp.route("/teachers/<int:teacher_id>/status", methods=["POST"])
@login_required
@admin_required
@limiter.limit("30 per minute")
def update_teacher_status(teacher_id: int):
    """Activate or deactivate both the teacher profile and its login account."""
    teacher = db.get_or_404(Teacher, teacher_id)
    raw_active = request.form.get("active")
    if raw_active not in {"true", "false"}:
        abort(400)
    active = raw_active == "true"
    try:
        set_teacher_active(teacher, active)
    except UserManagementError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.teachers"))
    state = "activated" if active else "deactivated"
    flash(f"Teacher account for {teacher.name} was {state}.", "success")
    return redirect(url_for("admin.teachers"))


def _render_teacher_form(
    heading: str,
    values: dict,
    teacher: Teacher | None,
    errors: dict[str, str],
    selected_class_ids: list[int] | None = None,
):
    departments = db.session.execute(
        select(Department).order_by(Department.name)
    ).scalars().all()
    classes = db.session.execute(
        select(ClassSection)
        .options(joinedload(ClassSection.department))
        .where(ClassSection.is_active.is_(True))
        .order_by(ClassSection.name)
    ).scalars().all()
    if selected_class_ids is None and teacher is not None:
        selected_class_ids = [
            row.class_section_id for row in teacher.class_assignments if row.is_active
        ]
    return render_template(
        "admin/teacher_form.html",
        heading=heading,
        values=values,
        teacher=teacher,
        errors=errors,
        departments=departments,
        classes=classes,
        selected_class_ids=set(selected_class_ids or []),
    )


def _requested_active_state() -> bool:
    raw_active = request.form.get("active")
    if raw_active not in {"true", "false"}:
        abort(400)
    return raw_active == "true"


def _render_department_form(
    heading: str, values: dict, department: Department | None, errors: dict[str, str]
):
    return render_template(
        "admin/department_form.html",
        heading=heading,
        values=values,
        department=department,
        errors=errors,
    )


def _render_class_form(
    heading: str, values: dict, class_section: ClassSection | None, errors: dict[str, str]
):
    department_query = select(Department).where(Department.is_active.is_(True))
    if class_section and not class_section.department.is_active:
        department_query = select(Department).where(
            or_(Department.is_active.is_(True), Department.id == class_section.department_id)
        )
    departments = db.session.execute(
        department_query.order_by(Department.name)
    ).scalars().all()

    assignments = []
    available_teachers = []
    if class_section:
        assignments = [
            assignment
            for assignment in class_section.teacher_assignments
            if assignment.is_active
        ]
        assigned_ids = {assignment.teacher_id for assignment in assignments}
        available_teachers = db.session.execute(
            select(Teacher)
            .options(joinedload(Teacher.user), joinedload(Teacher.department))
            .where(Teacher.is_active.is_(True), Teacher.user.has(is_active_flag=True))
            .order_by(Teacher.name)
        ).scalars().all()
        available_teachers = [
            teacher for teacher in available_teachers if teacher.id not in assigned_ids
        ]

    return render_template(
        "admin/class_form.html",
        heading=heading,
        values=values,
        class_section=class_section,
        errors=errors,
        departments=departments,
        assignments=assignments,
        available_teachers=available_teachers,
    )
