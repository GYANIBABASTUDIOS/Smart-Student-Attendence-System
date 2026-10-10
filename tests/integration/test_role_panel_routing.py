from __future__ import annotations

from app.models import Role, Student, Teacher, User
from tests.conftest import ADMIN_PASSWORD, TEACHER_PASSWORD


def _login(client, user: User, password: str):
    return client.post("/login", data={"username": user.username, "password": password})


def test_role_login_and_shared_routes_land_on_role_panels(app, db, admin, teacher):
    teacher_profile = Teacher(
        user=teacher,
        employee_id="ROUTE-TEACHER",
        name="Route Teacher",
    )
    student_user = User(
        username="route-student",
        email="route-student@example.test",
        role=Role.STUDENT.value,
    )
    student_user.set_password("student-password-123")
    student_profile = Student(
        user=student_user,
        student_id="ROUTE-STUDENT",
        name="Route Student",
        email="route-student.profile@example.test",
    )
    db.session.add_all([teacher_profile, student_user, student_profile])
    db.session.commit()
    assert admin.role == Role.ADMIN.value
    assert teacher.role == Role.TEACHER.value
    assert student_user.role == Role.STUDENT.value

    cases = (
        (admin, ADMIN_PASSWORD, "/admin/", "/admin/"),
        (teacher, TEACHER_PASSWORD, "/teacher/", "/teacher/"),
        (student_user, "student-password-123", "/student/", "/student/"),
    )
    for user, password, login_target, root_target in cases:
        client = app.test_client()
        login = _login(client, user, password)
        assert login.status_code == 302
        with client.session_transaction() as session:
            assert session.get("_user_id") == str(user.id)
        assert login.headers["Location"].endswith(login_target)
        assert client.get("/login").headers["Location"].endswith(login_target)
        root = client.get("/")
        assert root.status_code == 302
        assert root.headers["Location"].endswith(root_target)
        assert client.get(login_target).status_code == 200

    admin_client = app.test_client()
    _login(admin_client, admin, ADMIN_PASSWORD)
    assert admin_client.get("/analytics").headers["Location"].endswith("/admin/reports")
    assert admin_client.get("/reports").headers["Location"].endswith("/admin/reports")

    teacher_client = app.test_client()
    teacher_login = _login(teacher_client, teacher, TEACHER_PASSWORD)
    with teacher_client.session_transaction() as session:
        assert session.get("_user_id") == str(teacher.id)
    assert teacher_login.headers["Location"].endswith("/teacher/")
    assert teacher_client.get("/analytics").headers["Location"].endswith(
        "/teacher/attendance"
    )

    student_client = app.test_client()
    _login(student_client, student_user, "student-password-123")
    assert student_client.get("/analytics").headers["Location"].endswith("/student/")


def test_anonymous_users_are_redirected_to_login_for_all_panels(app):
    client = app.test_client()
    for path in ("/", "/admin/", "/teacher/", "/student/"):
        response = client.get(path)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]


def test_valid_posted_credentials_replace_an_existing_role_session(app, admin, teacher):
    client = app.test_client()
    admin_login = _login(client, admin, ADMIN_PASSWORD)
    assert admin_login.headers["Location"].endswith("/admin/")

    teacher_login = _login(client, teacher, TEACHER_PASSWORD)
    assert teacher_login.headers["Location"].endswith("/teacher/")
    with client.session_transaction() as session:
        assert session.get("_user_id") == str(teacher.id)
    assert client.get("/admin/").status_code == 403


def test_roles_cannot_open_other_role_panels(client, admin, teacher, db):
    student_user = User(
        username="panel-denial-student",
        email="panel-denial-student@example.test",
        role=Role.STUDENT.value,
    )
    student_user.set_password("student-password-123")
    student = Student(
        user=student_user,
        student_id="PANEL-DENIAL",
        name="Panel Denial Student",
        email="panel-denial.profile@example.test",
    )
    db.session.add(student_user)
    db.session.add(student)
    db.session.commit()

    admin_client = client.application.test_client()
    _login(admin_client, admin, ADMIN_PASSWORD)
    assert admin_client.get("/teacher/").status_code == 403
    assert admin_client.get("/student/").status_code == 403

    teacher_client = client.application.test_client()
    assert teacher.role == Role.TEACHER.value
    teacher_login = _login(teacher_client, teacher, TEACHER_PASSWORD)
    with teacher_client.session_transaction() as session:
        assert session.get("_user_id") == str(teacher.id)
    assert teacher_login.headers["Location"].endswith("/teacher/")
    assert teacher_client.get("/admin/").status_code == 403
    assert teacher_client.get("/student/").status_code == 403

    student_client = client.application.test_client()
    _login(student_client, student_user, "student-password-123")
    assert student_client.get("/admin/").status_code == 403
    assert student_client.get("/teacher/").status_code == 403
