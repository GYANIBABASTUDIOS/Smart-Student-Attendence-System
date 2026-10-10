"""Admin route access and protection against removing the last active admin."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import Role, User
from app.services.user_service import (
    UserManagementError,
    change_user_role,
    deactivate_user_account,
)


def test_admin_can_open_dashboard(admin_client):
    response = admin_client.get("/admin/")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Admin Dashboard" in page
    assert "Total students" in page
    assert "weekly-attendance-chart" in page
    assert "Reports &amp; Analytics" in page
    assert "Teachers" in page and "Soon" in page
    assert "Departments" in page
    assert "Classes &amp; Sections" in page
    assert "Settings" in page
    assert "Logout" in page
    assert "Manage students" not in page
    assert "Add student" not in page
    assert 'href="/students"' not in page
    assert 'href="/register_student"' not in page
    assert 'href="/attendance"' not in page
    assert 'href="/leave"' not in page


def test_dashboard_handles_empty_data(admin_client):
    page = admin_client.get("/admin/").get_data(as_text=True)
    # The trend service returns seven zero-filled days, so the chart remains visible
    # with a useful zero state even when no attendance rows exist.
    assert "weekly-attendance-chart" in page
    assert "weekly-attendance-data" in page
    assert "No departments to report yet" in page
    assert "Recent attendance activity" not in page


def test_teacher_cannot_open_admin_area(teacher_client):
    assert teacher_client.get("/admin/").status_code == 403


def test_student_cannot_open_admin_area(client, db):
    student = User(username="student", email="student@example.test", role=Role.STUDENT.value)
    student.set_password("student-password-1234")
    db.session.add(student)
    db.session.commit()

    response = client.post(
        "/login", data={"username": "student", "password": "student-password-1234"}
    )
    assert response.status_code == 302
    assert client.get("/admin/").status_code == 403


def test_anonymous_user_is_redirected_to_login(client):
    response = client.get("/admin/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_last_active_admin_cannot_be_deactivated(admin):
    with pytest.raises(UserManagementError, match="last active administrator"):
        deactivate_user_account(admin)

    db.session.refresh(admin)
    assert admin.is_active is True


def test_last_active_admin_cannot_be_demoted(admin):
    with pytest.raises(UserManagementError, match="last active administrator"):
        change_user_role(admin, Role.TEACHER.value)

    db.session.refresh(admin)
    assert admin.role == Role.ADMIN.value


def test_admin_can_be_deactivated_when_another_active_admin_exists(admin, db):
    second = User(username="admin2", email="admin2@example.test", role=Role.ADMIN.value)
    second.set_password("another-admin-password-1234")
    db.session.add(second)
    db.session.commit()

    deactivate_user_account(admin)
    assert admin.is_active is False


def test_admin_can_be_demoted_when_another_active_admin_exists(admin, db):
    second = User(username="admin2", email="admin2@example.test", role=Role.ADMIN.value)
    second.set_password("another-admin-password-1234")
    db.session.add(second)
    db.session.commit()

    change_user_role(admin, Role.TEACHER.value)
    assert admin.role == Role.TEACHER.value
