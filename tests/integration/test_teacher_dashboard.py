from __future__ import annotations

from datetime import timedelta

from app.models import (
    AttendanceRecord,
    ClassSection,
    Department,
    Role,
    Student,
    Teacher,
    TeacherClassAssignment,
    User,
)
from tests.conftest import ADMIN_PASSWORD, app_today


def _teacher_account(db, username: str, employee_id: str) -> Teacher:
    user = User(
        username=username,
        email=f"{username}@example.test",
        role=Role.TEACHER.value,
    )
    user.set_password(ADMIN_PASSWORD)
    teacher = Teacher(user=user, employee_id=employee_id, name=username.title())
    db.session.add(teacher)
    db.session.flush()
    return teacher


def _login(client, username: str):
    return client.post(
        "/login",
        data={"username": username, "password": ADMIN_PASSWORD},
    )


def test_teacher_dashboard_is_role_and_profile_protected(client, db, admin):
    assert client.get("/teacher/").status_code == 302
    admin_client = client
    assert _login(admin_client, admin.username).status_code == 302
    assert admin_client.get("/teacher/").status_code == 403

    student = User(username="panel-student", email="panel-student@example.test", role=Role.STUDENT.value)
    student.set_password(ADMIN_PASSWORD)
    db.session.add(student)
    db.session.commit()
    student_client = client.application.test_client()
    assert _login(student_client, student.username).status_code == 302
    assert student_client.get("/teacher/").status_code == 403


def test_teacher_role_without_active_profile_is_forbidden(client, db):
    user = User(username="profileless", email="profileless@example.test", role=Role.TEACHER.value)
    user.set_password(ADMIN_PASSWORD)
    db.session.add(user)
    db.session.commit()
    assert _login(client, user.username).status_code == 302
    assert client.get("/teacher/").status_code == 403


def test_inactive_teacher_profile_is_forbidden(client, db):
    teacher = _teacher_account(db, "inactive-profile", "EMP-INACTIVE")
    teacher.is_active = False
    db.session.commit()
    assert _login(client, teacher.user.username).status_code == 302
    assert client.get("/teacher/").status_code == 403


def test_teacher_dashboard_shows_only_owned_assignment_data(client, db):
    department = Department(name="Engineering", code="ENG")
    db.session.add(department)
    db.session.flush()
    own_section = ClassSection(name="Engineering 1-A", year="1", section="A", department_id=department.id)
    other_section = ClassSection(name="Engineering 2-B", year="2", section="B", department_id=department.id)
    db.session.add_all([own_section, other_section])
    teacher = _teacher_account(db, "scope-teacher", "EMP-SCOPE")
    other_teacher = _teacher_account(db, "other-teacher", "EMP-OTHER")
    db.session.flush()
    db.session.add_all([
        TeacherClassAssignment(teacher_id=teacher.id, class_section_id=own_section.id),
        TeacherClassAssignment(teacher_id=other_teacher.id, class_section_id=other_section.id),
    ])
    own_student = Student(student_id="OWN-01", name="Owned Student", email="owned@example.test", class_section_id=own_section.id)
    other_student = Student(student_id="OTHER-01", name="Other Student", email="other@example.test", class_section_id=other_section.id)
    db.session.add_all([own_student, other_student])
    db.session.flush()
    day = app_today() - timedelta(days=1)
    db.session.add_all([
        AttendanceRecord(student_id=own_student.id, date=day, status="Present", marked_by="fixture"),
        AttendanceRecord(student_id=other_student.id, date=day, status="Absent", marked_by="fixture"),
    ])
    db.session.commit()

    assert _login(client, teacher.user.username).status_code == 302
    response = client.get(f"/teacher/?teacher_id={other_teacher.id}")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Engineering 1-A" in page
    assert "Owned Student" in page
    assert "Engineering 2-B" not in page
    assert "Other Student" not in page
    assert "No active class assignments" not in page


def test_teacher_without_assignments_gets_clear_empty_state(client, db):
    teacher = _teacher_account(db, "unassigned", "EMP-EMPTY")
    db.session.commit()
    assert _login(client, teacher.user.username).status_code == 302
    response = client.get("/teacher/")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "No active class assignments" in page
    assert "0" in page
    assert "TEACHER PANEL" in page
