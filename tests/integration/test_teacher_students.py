from __future__ import annotations

from pathlib import Path

import pytest

from app.models import (
    AttendanceRecord,
    ClassSection,
    Department,
    LeaveRequest,
    Role,
    Student,
    Teacher,
    TeacherClassAssignment,
    User,
)
from app.services import student_service
from tests.conftest import ADMIN_PASSWORD, TEACHER_PASSWORD, app_today


def _department_and_classes(db):
    department = Department(name="Teacher Management", code="TM")
    db.session.add(department)
    db.session.flush()
    first = ClassSection(name="TM 1-A", year="1", section="A", department_id=department.id)
    second = ClassSection(name="TM 2-B", year="2", section="B", department_id=department.id)
    other = ClassSection(name="TM 3-C", year="3", section="C", department_id=department.id)
    db.session.add_all([first, second, other])
    db.session.flush()
    return department, first, second, other


def _add_profile(db, user, employee_id: str, *classes):
    teacher = Teacher(user=user, employee_id=employee_id, name=f"Profile {user.username}")
    db.session.add(teacher)
    db.session.flush()
    db.session.add_all(
        TeacherClassAssignment(teacher_id=teacher.id, class_section_id=section.id)
        for section in classes
    )
    db.session.flush()
    return teacher


def _student(db, class_section, student_id="TST-001", name="Managed Student"):
    student = Student(
        student_id=student_id,
        name=name,
        email=f"{student_id.lower()}@example.test",
        department=class_section.department.name,
        year=class_section.year,
        section=class_section.section,
        class_section_id=class_section.id,
    )
    db.session.add(student)
    db.session.flush()
    return student


def _form(student_id="TST-999", class_section_id="1", **updates):
    data = {
        "student_id": student_id,
        "name": "New Teacher Student",
        "email": "new-teacher-student@example.com",
        "username": "new-teacher-student",
        "password": "NewStudentPass123!",
        "password_confirmation": "NewStudentPass123!",
        "phone": "9876543210",
        "class_section_id": class_section_id,
    }
    data.update(updates)
    return data


def test_teacher_student_routes_require_an_active_teacher_profile(client, db, admin, teacher):
    assert client.get("/teacher/students").status_code == 302
    admin_client = client
    assert admin_client.post(
        "/login", data={"username": admin.username, "password": ADMIN_PASSWORD}
    ).status_code == 302
    assert admin_client.get("/teacher/students").status_code == 403

    student_user = User(username="panel-student", email="panel-student@example.test", role=Role.STUDENT.value)
    student_user.set_password(ADMIN_PASSWORD)
    db.session.add(student_user)
    db.session.commit()
    student_client = client.application.test_client()
    student_client.post("/login", data={"username": student_user.username, "password": ADMIN_PASSWORD})
    assert student_client.get("/teacher/students").status_code == 403

    # The generic teacher fixture has no Teacher row; a role string alone is insufficient.
    teacher_client = client.application.test_client()
    teacher_client.post("/login", data={"username": teacher.username, "password": TEACHER_PASSWORD})
    assert teacher_client.get("/teacher/students").status_code == 403


def test_assigned_teacher_can_list_search_and_paginate_only_own_students(teacher_client, teacher, db):
    _, own_section, _, other_section = _department_and_classes(db)
    _add_profile(db, teacher, "EMP-LIST", own_section)
    other_user = User(username="teacher-b", email="teacher-b@example.test", role=Role.TEACHER.value)
    other_user.set_password(TEACHER_PASSWORD)
    db.session.add(other_user)
    db.session.flush()
    _add_profile(db, other_user, "EMP-OTHER", other_section)
    _student(db, own_section, "OWN-100", "Own Roster Student")
    _student(db, other_section, "OTH-100", "Other Roster Student")
    db.session.commit()

    page = teacher_client.get("/teacher/students")
    assert page.status_code == 200
    assert b"Own Roster Student" in page.data
    assert b"Other Roster Student" not in page.data
    assert b"/teacher/students/new" in page.data
    assert b"Face enrollment" not in page.data
    assert b"/register_student" not in page.data

    search = teacher_client.get("/teacher/students?search=OTH-100")
    assert search.status_code == 200
    assert b"Other Roster Student" not in search.data
    assert b"No matching students" in search.data


def test_teacher_without_assignments_sees_empty_states(teacher_client, teacher, db):
    _add_profile(db, teacher, "EMP-EMPTY")
    db.session.commit()
    assert b"No active class assignments" in teacher_client.get("/teacher/students").data
    assert b"No active class assignments" in teacher_client.get("/teacher/students/new").data


def test_creation_uses_assigned_class_and_creates_student_login_without_face_enrollment(
    teacher_client, teacher, db
):
    _, own_section, _, other_section = _department_and_classes(db)
    _add_profile(db, teacher, "EMP-CREATE", own_section)
    other_user = User(username="teacher-b", email="teacher-b@example.test", role=Role.TEACHER.value)
    other_user.set_password(TEACHER_PASSWORD)
    db.session.add(other_user)
    db.session.flush()
    _add_profile(db, other_user, "EMP-OTHER", other_section)
    db.session.commit()

    before_users = db.session.execute(db.select(db.func.count()).select_from(User)).scalar_one()
    response = teacher_client.post("/teacher/students/new", data=_form(class_section_id=str(own_section.id)))
    assert response.status_code == 302
    student = db.session.execute(db.select(Student).filter_by(student_id="TST-999")).scalar_one()
    assert student.class_section_id == own_section.id
    assert student.department == own_section.department.name
    assert student.year == own_section.year and student.section == own_section.section
    assert student.image_path is None
    assert student.user_id is not None
    assert student.user.role == Role.STUDENT.value
    assert student.user.email == student.email
    assert student.user.check_password("NewStudentPass123!")
    assert student.user.password_hash != "NewStudentPass123!"
    assert student.face_samples_count == 0
    assert db.session.execute(db.select(db.func.count()).select_from(User)).scalar_one() == before_users + 1

    student_client = teacher_client.application.test_client()
    login = student_client.post(
        "/login", data={"username": "new-teacher-student", "password": "NewStudentPass123!"}
    )
    assert login.status_code == 302
    assert login.headers["Location"].endswith("/student/")
    assert student_client.get("/student/").status_code == 200

    rejected = teacher_client.post(
        "/teacher/students/new", data=_form(student_id="TST-998", class_section_id=str(other_section.id))
    )
    assert rejected.status_code == 403
    assert db.session.execute(db.select(Student).filter_by(student_id="TST-998")).first() is None


def test_student_creation_validates_password_username_and_email(teacher_client, teacher, db):
    _, own_section, _, _ = _department_and_classes(db)
    _add_profile(db, teacher, "EMP-CREDENTIALS", own_section)
    existing = User(username="taken-name", email="taken@example.test", role=Role.STUDENT.value)
    existing.set_password(ADMIN_PASSWORD)
    db.session.add(existing)
    db.session.commit()

    mismatch = teacher_client.post(
        "/teacher/students/new",
        data=_form(student_id="PASS-001", class_section_id=str(own_section.id),
                   password_confirmation="DifferentPassword123!"),
    )
    assert mismatch.status_code == 400
    assert b"Passwords do not match" in mismatch.data
    assert db.session.execute(db.select(Student).filter_by(student_id="PASS-001")).first() is None

    duplicate_username = teacher_client.post(
        "/teacher/students/new",
        data=_form(student_id="PASS-002", class_section_id=str(own_section.id), username="taken-name"),
    )
    assert duplicate_username.status_code == 400
    assert b"username is already in use" in duplicate_username.data

    duplicate_email = teacher_client.post(
        "/teacher/students/new",
        data=_form(student_id="PASS-003", class_section_id=str(own_section.id), email="taken@example.test"),
    )
    assert duplicate_email.status_code == 400
    assert b"email is already in use" in duplicate_email.data


def test_duplicate_identifier_and_invalid_class_are_rejected(teacher_client, teacher, db):
    _, own_section, _, other_section = _department_and_classes(db)
    _add_profile(db, teacher, "EMP-DUP", own_section)
    _student(db, own_section, "DUP-001")
    db.session.commit()

    duplicate = teacher_client.post(
        "/teacher/students/new", data=_form(student_id="DUP-001", class_section_id=str(own_section.id))
    )
    assert duplicate.status_code == 400
    assert b"already exists" in duplicate.data
    assert teacher_client.post(
        "/teacher/students/new", data=_form(student_id="BAD-001", class_section_id="not-an-id")
    ).status_code == 400
    assert teacher_client.post(
        "/teacher/students/new", data=_form(student_id="BAD-002", class_section_id=str(other_section.id))
    ).status_code == 403


def test_edit_is_scoped_and_destination_must_be_assigned(teacher_client, teacher, db):
    _, own_section, second_section, other_section = _department_and_classes(db)
    _add_profile(db, teacher, "EMP-EDIT", own_section, second_section)
    own_student = _student(db, own_section, "EDIT-001", "Before Edit")
    other_student = _student(db, other_section, "EDIT-OTH", "Outside Student")
    db.session.commit()

    assert teacher_client.get(f"/teacher/students/{other_student.id}/edit").status_code == 404
    before = teacher_client.get(f"/teacher/students/{own_student.id}/edit")
    assert before.status_code == 200
    assert b"EDIT-001" in before.data

    changed = teacher_client.post(
        f"/teacher/students/{own_student.id}/edit",
        data=_form(
            student_id="ATTACK-CHANGE",
            class_section_id=str(second_section.id),
            name="Edited Student",
            email="edited@example.com",
        ),
    )
    assert changed.status_code == 302
    db.session.refresh(own_student)
    assert own_student.student_id == "EDIT-001"
    assert own_student.name == "Edited Student"
    assert own_student.class_section_id == second_section.id
    assert own_student.department == second_section.department.name

    rejected = teacher_client.post(
        f"/teacher/students/{own_student.id}/edit",
        data=_form(class_section_id=str(other_section.id), name="Forbidden Move", email="move@example.com"),
    )
    assert rejected.status_code == 403
    db.session.refresh(own_student)
    assert own_student.name == "Edited Student"


def test_deactivation_is_reversible_and_preserves_attendance_links_and_face_files(
    teacher_client, teacher, db, app, monkeypatch
):
    _, own_section, _, other_section = _department_and_classes(db)
    _add_profile(db, teacher, "EMP-STATUS", own_section)
    student = _student(db, own_section, "STATUS-001")
    student_user = User(username="linked-student", email="linked-student@example.test", role=Role.STUDENT.value)
    student_user.set_password(ADMIN_PASSWORD)
    db.session.add(student_user)
    db.session.flush()
    student.user_id = student_user.id
    attendance = AttendanceRecord(student_id=student.id, date=app_today(), status="Present", marked_by="fixture")
    leave = LeaveRequest(
        student_id=student.id,
        leave_type="Sick",
        start_date=app_today(),
        end_date=app_today(),
        reason="Existing leave record",
    )
    db.session.add_all([attendance, leave])
    db.session.commit()

    sample_dir = Path(app.config["FACE_DATA_FOLDER"]) / "samples" / str(student.id)
    sample_dir.mkdir(parents=True, exist_ok=True)
    sample = sample_dir / "001.png"
    sample.write_bytes(b"existing face sample marker")
    rebuild_calls = []
    monkeypatch.setattr(student_service, "rebuild_face_model", lambda: rebuild_calls.append(True) or 1)

    denied = teacher_client.post(
        f"/teacher/students/{student.id}/status", data={"active": "false"}
    )
    assert denied.status_code == 302
    db.session.refresh(student)
    assert student.is_active is False
    assert sample.exists()
    assert db.session.get(AttendanceRecord, attendance.id) is not None
    assert db.session.get(LeaveRequest, leave.id) is not None
    assert student.user_id == student_user.id

    reactivated = teacher_client.post(
        f"/teacher/students/{student.id}/status", data={"active": "true"}
    )
    assert reactivated.status_code == 302
    db.session.refresh(student)
    assert student.is_active is True
    assert sample.exists()
    assert len(rebuild_calls) == 2

    outside = _student(db, other_section, "STATUS-OTH")
    db.session.commit()
    assert teacher_client.post(
        f"/teacher/students/{outside.id}/status", data={"active": "false"}
    ).status_code == 404


def test_legacy_teacher_paths_redirect_to_scoped_panel(teacher_client, teacher, db):
    _, own_section, _, _ = _department_and_classes(db)
    _add_profile(db, teacher, "EMP-ALIAS", own_section)
    student = _student(db, own_section, "ALIAS-001")
    db.session.commit()
    assert teacher_client.get("/students").headers["Location"].endswith("/teacher/students")
    assert teacher_client.get("/register_student").headers["Location"].endswith("/teacher/students/new")
    assert teacher_client.get(f"/edit_student/{student.id}").headers["Location"].endswith(
        f"/teacher/students/{student.id}/edit"
    )
    assert teacher_client.post("/register_student", data=_form()).status_code == 303
