"""Admin teacher-management routes and account safeguards."""

from __future__ import annotations

from app.models import (
    ClassSection,
    Department,
    Role,
    Teacher,
    TeacherClassAssignment,
    User,
)


def _teacher_data(**overrides):
    values = {
        "name": "Morgan Lee",
        "employee_id": "EMP-1001",
        "username": "morgan.lee",
        "email": "morgan.lee@example.com",
        "phone": "5551234567",
        "password": "long-teacher-password-123",
        "password_confirmation": "long-teacher-password-123",
    }
    values.update(overrides)
    return values


def _add_teacher(db):
    user = User(username="existing.teacher", email="existing.teacher@example.com", role="teacher")
    user.set_password("existing-teacher-password")
    teacher = Teacher(user=user, employee_id="EMP-1000", name="Existing Teacher")
    db.session.add(teacher)
    db.session.commit()
    return teacher


def test_admin_can_list_and_create_teacher(admin_client, db, admin):
    assert admin_client.get("/admin/teachers").status_code == 200
    response = admin_client.post("/admin/teachers/new", data=_teacher_data())
    assert response.status_code == 302

    user = db.session.execute(db.select(User).filter_by(username="morgan.lee")).scalar_one()
    teacher = db.session.execute(db.select(Teacher).filter_by(user_id=user.id)).scalar_one()
    assert user.role == Role.TEACHER.value
    assert user.password_hash != "long-teacher-password-123"
    assert user.check_password("long-teacher-password-123")
    assert teacher.name == "Morgan Lee"
    assert admin.role == Role.ADMIN.value and admin.is_active


def test_teacher_can_be_assigned_department_and_existing_class(admin_client, db):
    department = Department(name="Science", code="SCI")
    db.session.add(department)
    db.session.flush()
    class_section = ClassSection(
        name="Year 1 A", year="1", section="A", department_id=department.id
    )
    db.session.add(class_section)
    db.session.commit()

    response = admin_client.post(
        "/admin/teachers/new",
        data=_teacher_data(department_id=str(department.id), class_ids=str(class_section.id)),
    )
    assert response.status_code == 302
    teacher = db.session.execute(db.select(Teacher)).scalar_one()
    assignment = db.session.execute(
        db.select(TeacherClassAssignment).where(
            TeacherClassAssignment.teacher_id == teacher.id,
            TeacherClassAssignment.class_section_id == class_section.id,
            TeacherClassAssignment.is_active.is_(True),
        )
    ).scalar_one()
    assert teacher.department_id == department.id
    assert assignment.class_section_id == class_section.id


def test_admin_can_edit_teacher(admin_client, db):
    teacher = _add_teacher(db)
    response = admin_client.post(
        f"/admin/teachers/{teacher.id}/edit",
        data=_teacher_data(
            employee_id="EMP-2000",
            username="updated.teacher",
            email="updated.teacher@example.com",
            password="",
            password_confirmation="",
        ),
    )
    assert response.status_code == 302
    db.session.refresh(teacher)
    assert teacher.employee_id == "EMP-2000"
    assert teacher.user.username == "updated.teacher"
    assert teacher.user.check_password("existing-teacher-password")


def test_admin_can_deactivate_and_reactivate_teacher(admin_client, db):
    teacher = _add_teacher(db)
    response = admin_client.post(
        f"/admin/teachers/{teacher.id}/status", data={"active": "false"}
    )
    assert response.status_code == 302
    db.session.refresh(teacher)
    assert teacher.is_active is False
    assert teacher.user.is_active is False

    response = admin_client.post(
        f"/admin/teachers/{teacher.id}/status", data={"active": "true"}
    )
    assert response.status_code == 302
    db.session.refresh(teacher)
    assert teacher.is_active is True
    assert teacher.user.is_active is True


def test_duplicate_teacher_account_is_rejected(admin_client, db):
    _add_teacher(db)
    response = admin_client.post("/admin/teachers/new", data=_teacher_data(
        employee_id="EMP-1002",
        username="existing.teacher",
        email="new.email@example.com",
    ))
    assert response.status_code == 400
    assert b"username is already in use" in response.data


def test_duplicate_teacher_email_is_rejected(admin_client, db):
    _add_teacher(db)
    response = admin_client.post(
        "/admin/teachers/new",
        data=_teacher_data(
            employee_id="EMP-1002",
            username="new.teacher",
            email="existing.teacher@example.com",
        ),
    )
    assert response.status_code == 400
    assert b"email is already in use" in response.data


def test_teacher_cannot_access_teacher_management(teacher_client):
    for path in ("/admin/teachers", "/admin/teachers/new", "/admin/teachers/1/edit", "/admin/teachers/1/status"):
        response = teacher_client.get(path) if path.endswith(("teachers", "new", "edit")) else teacher_client.post(path)
        assert response.status_code == 403
    assert teacher_client.post("/admin/teachers/new", data=_teacher_data()).status_code == 403
    assert teacher_client.post("/admin/teachers/1/edit", data=_teacher_data()).status_code == 403


def test_student_cannot_access_teacher_management(client, db):
    student = User(username="student", email="student@example.test", role=Role.STUDENT.value)
    student.set_password("student-password-1234")
    db.session.add(student)
    db.session.commit()
    assert client.post(
        "/login", data={"username": "student", "password": "student-password-1234"}
    ).status_code == 302
    assert client.get("/admin/teachers").status_code == 403
    assert client.post("/admin/teachers/new", data=_teacher_data()).status_code == 403
    assert client.post("/admin/teachers/1/status", data={"active": "false"}).status_code == 403


def test_anonymous_is_redirected_from_teacher_management(client):
    for path in ("/admin/teachers", "/admin/teachers/new", "/admin/teachers/1/edit"):
        response = client.get(path)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]
