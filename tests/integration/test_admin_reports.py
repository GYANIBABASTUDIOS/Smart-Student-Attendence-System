from __future__ import annotations

import csv
import io
from datetime import timedelta

import pytest

from app.models import AttendanceRecord, ClassSection, Department, Role, Student, User
from tests.conftest import ADMIN_PASSWORD, TEACHER_PASSWORD, app_today


def test_admin_can_view_reports_and_dashboard_link_targets_it(admin_client, attendance_history):  # noqa: ARG002
    response = admin_client.get("/admin/reports")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Reports &amp; Analytics" in page
    assert "Daily attendance trend" in page
    assert "Legacy student department text" in page
    assert "Attendance rate = recorded Present + Late" in page
    assert 'href="/admin/reports"' in admin_client.get("/admin/").get_data(as_text=True)


def test_reports_require_admin_role(teacher_client):
    assert teacher_client.get("/admin/reports").status_code == 403


def test_student_cannot_view_reports(client, db):
    student_user = User(username="report-student", email="report-student@example.test", role=Role.STUDENT.value)
    student_user.set_password(ADMIN_PASSWORD)
    db.session.add(student_user)
    db.session.commit()
    assert client.post("/login", data={"username": student_user.username, "password": ADMIN_PASSWORD}).status_code == 302
    assert client.get("/admin/reports").status_code == 403


def test_anonymous_is_redirected(client):
    response = client.get("/admin/reports")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_date_filters_and_rate_use_only_recorded_classifiable_marks(admin_client, db, students):
    day = app_today() - timedelta(days=1)
    fourth = Student(
        student_id="S003", name="Student 3", email="student3@example.test",
        department="ECE", year="3", section="A",
    )
    db.session.add(fourth)
    db.session.flush()
    db.session.add_all([
        AttendanceRecord(student_id=students[0].id, date=day, status="Present", marked_by="test"),
        AttendanceRecord(student_id=students[1].id, date=day, status="Late", marked_by="test"),
        AttendanceRecord(student_id=students[2].id, date=day, status="Absent", marked_by="test"),
        AttendanceRecord(student_id=fourth.id, date=day, status="On Leave", marked_by="test"),
    ])
    db.session.commit()

    response = admin_client.get(f"/admin/reports?start_date={day}&end_date={day}")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "66.7%" in page
    assert "On Leave" in page


@pytest.mark.parametrize("query", [
    "start_date=bad&end_date=2025-01-01",
    "start_date=2025-02-02&end_date=2025-02-01",
    "start_date=2099-01-01&end_date=2099-01-02",
])
def test_invalid_dates_are_safe_400(admin_client, query):
    response = admin_client.get(f"/admin/reports?{query}")
    assert response.status_code == 400
    assert "alert" in response.get_data(as_text=True)


def test_empty_report_and_export_are_well_formed(admin_client):
    start = app_today() - timedelta(days=2)
    response = admin_client.get(f"/admin/reports?start_date={start}&end_date={start}")
    assert response.status_code == 200
    assert "No attendance records were found" in response.get_data(as_text=True)
    export = admin_client.get(f"/admin/reports/export.csv?start_date={start}&end_date={start}")
    assert export.status_code == 200
    assert "attachment" in export.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(export.data.decode("utf-8-sig"))))
    assert rows[0] == ["Date", "Present (includes late)", "Absent", "Attendance rate (%)"]
    assert rows[1][0] == start.isoformat()


def test_department_and_class_summaries_use_explicit_class_relationships(admin_client, db, students):
    department = Department(name="Computing", code="COMP")
    db.session.add(department)
    db.session.flush()
    section = ClassSection(name="Computing 1-A", year="1", section="A", department_id=department.id)
    db.session.add(section)
    db.session.flush()
    students[0].class_section_id = section.id
    # Legacy label intentionally differs: it must remain a separate report category.
    students[0].department = "Legacy Computing"
    db.session.commit()

    response = admin_client.get("/admin/reports")
    page = response.get_data(as_text=True)
    assert "Computing (COMP)" in page
    assert "Legacy Computing" in page
    assert "Computing 1-A" in page
    assert "legacy year and section text is not mapped" not in page


def test_export_is_admin_only_and_contains_no_personal_fields(app, teacher, admin):
    # Each account must have its own cookie jar. The shared ``client`` fixture used by
    # admin_client/teacher_client otherwise leaves both aliases signed in as whichever
    # fixture logs in last, so the authorization assertion would exercise an Admin
    # session while calling it teacher_client.
    teacher_client = app.test_client()
    admin_client = app.test_client()
    teacher_login = teacher_client.post(
        "/login", data={"username": teacher.username, "password": TEACHER_PASSWORD}
    )
    admin_login = admin_client.post(
        "/login", data={"username": admin.username, "password": ADMIN_PASSWORD}
    )
    assert teacher_login.headers["Location"].endswith("/teacher/")
    assert admin_login.headers["Location"].endswith("/admin/")

    assert teacher_client.get("/admin/reports/export.csv").status_code == 403
    payload = admin_client.get("/admin/reports/export.csv").get_data(as_text=True)
    assert "Student ID" not in payload
    assert "Student Name" not in payload
    assert "Password" not in payload
