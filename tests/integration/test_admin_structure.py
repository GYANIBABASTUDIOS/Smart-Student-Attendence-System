"""Admin department, class-section, and teacher assignment routes."""

from __future__ import annotations

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


def _department_data(**overrides):
    values = {"name": "Science", "code": "SCI", "description": "Science department"}
    values.update(overrides)
    return values


def _class_data(department_id, **overrides):
    values = {
        "name": "Year 1 A",
        "year": "1",
        "section": "A",
        "department_id": str(department_id),
    }
    values.update(overrides)
    return values


def _add_teacher(db):
    user = User(username="structure.teacher", email="structure.teacher@example.com", role="teacher")
    user.set_password("structure-teacher-password")
    teacher = Teacher(user=user, employee_id="STRUCT-1", name="Structure Teacher")
    db.session.add(teacher)
    db.session.commit()
    return teacher


def test_department_crud_status_and_counts(admin_client, db):
    assert admin_client.get("/admin/departments").status_code == 200
    response = admin_client.post("/admin/departments/new", data=_department_data())
    assert response.status_code == 302
    department = db.session.execute(db.select(Department)).scalar_one()
    assert department.code == "SCI"

    response = admin_client.post(
        f"/admin/departments/{department.id}/edit",
        data=_department_data(name="Science & Technology", code="S_T"),
    )
    assert response.status_code == 302
    db.session.refresh(department)
    assert department.name == "Science & Technology"

    for active in ("false", "true"):
        response = admin_client.post(
            f"/admin/departments/{department.id}/status", data={"active": active}
        )
        assert response.status_code == 302
    db.session.refresh(department)
    assert department.is_active is True


def test_duplicate_department_name_and_code_are_rejected(admin_client, db):
    response = admin_client.post("/admin/departments/new", data=_department_data())
    assert response.status_code == 302
    response = admin_client.post(
        "/admin/departments/new", data=_department_data(name="science")
    )
    assert response.status_code == 400
    assert b"name already exists" in response.data
    response = admin_client.post(
        "/admin/departments/new", data=_department_data(name="Physics", code="SCI")
    )
    assert response.status_code == 400
    assert b"code already exists" in response.data


def test_class_crud_duplicate_and_invalid_department(admin_client, db):
    department = Department(name="Science", code="SCI")
    db.session.add(department)
    db.session.commit()
    assert admin_client.get("/admin/classes").status_code == 200

    response = admin_client.post(
        "/admin/classes/new", data=_class_data(department.id)
    )
    assert response.status_code == 302
    class_section = db.session.execute(db.select(ClassSection)).scalar_one()

    response = admin_client.post(
        "/admin/classes/new", data=_class_data(department.id, name="Duplicate")
    )
    assert response.status_code == 400
    assert b"already exist in that department" in response.data

    response = admin_client.post(
        "/admin/classes/new", data=_class_data("9999", year="2", section="B")
    )
    assert response.status_code == 400
    assert b"valid department" in response.data

    response = admin_client.post(
        f"/admin/classes/{class_section.id}/edit",
        data=_class_data(department.id, name="Year One A", year="First"),
    )
    assert response.status_code == 302
    db.session.refresh(class_section)
    assert class_section.name == "Year One A" and class_section.year == "First"

    for active in ("false", "true"):
        response = admin_client.post(
            f"/admin/classes/{class_section.id}/status", data={"active": active}
        )
        assert response.status_code == 302
    db.session.refresh(class_section)
    assert class_section.is_active is True


def test_teacher_assignment_add_remove_and_reassign(admin_client, db):
    department = Department(name="Science", code="SCI")
    db.session.add(department)
    db.session.flush()
    class_section = ClassSection(
        name="Year 1 A", year="1", section="A", department_id=department.id
    )
    db.session.add(class_section)
    teacher = _add_teacher(db)

    response = admin_client.post(
        f"/admin/classes/{class_section.id}/assignments", data={"teacher_id": teacher.id}
    )
    assert response.status_code == 302
    assignment = db.session.execute(
        db.select(TeacherClassAssignment).where(
            TeacherClassAssignment.teacher_id == teacher.id,
            TeacherClassAssignment.class_section_id == class_section.id,
            TeacherClassAssignment.is_active.is_(True),
        )
    ).scalar_one()

    duplicate = admin_client.post(
        f"/admin/classes/{class_section.id}/assignments", data={"teacher_id": teacher.id}
    )
    assert duplicate.status_code == 302
    response = admin_client.post(
        f"/admin/classes/{class_section.id}/assignments/{assignment.id}/remove"
    )
    assert response.status_code == 302
    db.session.refresh(assignment)
    assert assignment.is_active is False

    response = admin_client.post(
        f"/admin/classes/{class_section.id}/assignments", data={"teacher_id": teacher.id}
    )
    assert response.status_code == 302
    assignments = db.session.execute(
        db.select(TeacherClassAssignment).where(
            TeacherClassAssignment.teacher_id == teacher.id,
            TeacherClassAssignment.class_section_id == class_section.id,
        )
    ).scalars().all()
    assert len(assignments) == 1 and assignments[0].is_active


def test_department_and_class_status_changes_preserve_student_attendance(
    admin_client, db, students, attendance_history
):
    department = Department(name="Science", code="SCI")
    db.session.add(department)
    db.session.flush()
    class_section = ClassSection(
        name="Year 1 A", year="1", section="A", department_id=department.id
    )
    db.session.add(class_section)
    db.session.flush()
    teacher = _add_teacher(db)
    assignment = TeacherClassAssignment(
        teacher_id=teacher.id, class_section_id=class_section.id
    )
    db.session.add(assignment)
    students[0].class_section_id = class_section.id
    db.session.commit()
    student_count = db.session.scalar(db.select(db.func.count()).select_from(Student))
    attendance_count = db.session.scalar(
        db.select(db.func.count()).select_from(AttendanceRecord)
    )

    admin_client.post(f"/admin/classes/{class_section.id}/status", data={"active": "false"})
    admin_client.post(f"/admin/departments/{department.id}/status", data={"active": "false"})

    assert db.session.scalar(db.select(db.func.count()).select_from(Student)) == student_count
    assert db.session.scalar(db.select(db.func.count()).select_from(AttendanceRecord)) == attendance_count
    db.session.refresh(assignment)
    assert assignment.is_active is True
    db.session.refresh(students[0])
    assert students[0].class_section_id == class_section.id


def test_teacher_and_student_are_forbidden_from_structure_routes(teacher_client, client, db):
    paths = ("/admin/departments", "/admin/departments/new", "/admin/classes", "/admin/classes/new")
    for path in paths:
        assert teacher_client.get(path).status_code == 403
    assert teacher_client.post("/admin/departments/new", data=_department_data()).status_code == 403
    assert teacher_client.post("/admin/classes/new", data=_class_data(1)).status_code == 403

    student = User(username="structure.student", email="structure.student@example.com", role=Role.STUDENT.value)
    student.set_password("structure-student-password")
    db.session.add(student)
    db.session.commit()
    assert client.post(
        "/login", data={"username": student.username, "password": "structure-student-password"}
    ).status_code == 302
    for path in paths:
        assert client.get(path).status_code == 403
    assert client.post("/admin/departments/new", data=_department_data()).status_code == 403
    assert client.post("/admin/classes/new", data=_class_data(1)).status_code == 403


def test_anonymous_is_redirected_from_structure_routes(client):
    for path in ("/admin/departments", "/admin/departments/new", "/admin/classes", "/admin/classes/new"):
        response = client.get(path)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]
