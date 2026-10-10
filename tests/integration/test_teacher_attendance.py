from __future__ import annotations

from app.models import AttendanceRecord, ClassSection, Department, Role, Student, Teacher, TeacherClassAssignment, User
from tests.conftest import ADMIN_PASSWORD, TEACHER_PASSWORD, app_today


def _class(db, name: str, section: str):
    department = Department(name=f"Department {name}", code=f"D{name}")
    db.session.add(department)
    db.session.flush()
    row = ClassSection(
        name=name,
        year="2",
        section=section,
        department_id=department.id,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _teacher(db, user, employee_id: str, *classes):
    profile = Teacher(user=user, employee_id=employee_id, name=user.username)
    db.session.add(profile)
    db.session.flush()
    db.session.add_all(
        TeacherClassAssignment(teacher_id=profile.id, class_section_id=row.id)
        for row in classes
    )
    db.session.flush()
    return profile


def _student(db, section, roll: str, name: str):
    row = Student(
        student_id=roll,
        name=name,
        email=f"{roll.lower()}@example.test",
        class_section_id=section.id,
        department=section.department.name,
        year=section.year,
        section=section.section,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _login(client, user, password=TEACHER_PASSWORD):
    return client.post("/login", data={"username": user.username, "password": password})


def test_teacher_attendance_routes_are_role_and_profile_protected(
    client, db, admin, teacher
):
    assert client.get("/teacher/attendance").status_code == 302
    _login(client, admin, ADMIN_PASSWORD)
    assert client.get("/teacher/attendance").status_code == 403

    student_user = User(
        username="attendance-student",
        email="attendance-student@example.test",
        role=Role.STUDENT.value,
    )
    student_user.set_password(ADMIN_PASSWORD)
    db.session.add(student_user)
    db.session.commit()
    student_client = client.application.test_client()
    _login(student_client, student_user, ADMIN_PASSWORD)
    assert student_client.get("/teacher/attendance").status_code == 403

    teacher_client = client.application.test_client()
    _login(teacher_client, teacher)
    assert teacher_client.get("/teacher/attendance").status_code == 403


def test_teacher_can_mark_only_assigned_class_students_idempotently(
    teacher_client, teacher, db
):
    own_class = _class(db, "Class A", "A")
    other_class = _class(db, "Class B", "B")
    _teacher(db, teacher, "ATT-A", own_class)
    other_user = User(
        username="teacher-b-attendance",
        email="teacher-b-attendance@example.test",
        role=Role.TEACHER.value,
    )
    other_user.set_password(TEACHER_PASSWORD)
    db.session.add(other_user)
    db.session.flush()
    _teacher(db, other_user, "ATT-B", other_class)
    own_student = _student(db, own_class, "ATTA-001", "Assigned Student")
    other_student = _student(db, other_class, "ATTB-001", "Unassigned Student")
    unrelated_record = AttendanceRecord(
        student_id=other_student.id,
        date=app_today(),
        status="Absent",
        marked_by="fixture",
    )
    db.session.add(unrelated_record)
    db.session.commit()

    page = teacher_client.get(f"/teacher/attendance?class_section_id={own_class.id}")
    assert page.status_code == 200
    assert b"Assigned Student" in page.data
    assert b"Unassigned Student" not in page.data
    assert b"Recent attendance history" in page.data

    post_url = f"/teacher/attendance/{own_class.id}/students/{own_student.id}"
    first = teacher_client.post(
        post_url,
        data={"date": app_today().isoformat(), "status": "Present"},
    )
    assert first.status_code == 302
    duplicate = teacher_client.post(
        post_url,
        data={"date": app_today().isoformat(), "status": "Absent"},
        follow_redirects=True,
    )
    assert duplicate.status_code == 200
    assert b"No duplicate record was created" in duplicate.data
    record = db.session.execute(
        db.select(AttendanceRecord).filter_by(student_id=own_student.id)
    ).scalar_one()
    assert record.status == "Present"
    assert record.marked_by.startswith("Teacher/")
    assert record.marked_by_user_id == teacher.id

    assert teacher_client.post(
        f"/teacher/attendance/{own_class.id}/students/{other_student.id}",
        data={"date": app_today().isoformat(), "status": "Present"},
    ).status_code == 404
    assert teacher_client.post(
        f"/teacher/attendance/{other_class.id}/students/{other_student.id}",
        data={"date": app_today().isoformat(), "status": "Present"},
    ).status_code == 404
    db.session.refresh(unrelated_record)
    assert unrelated_record.status == "Absent"


def test_invalid_status_date_and_inactive_assignment_are_rejected(
    teacher_client, teacher, db
):
    section = _class(db, "Class C", "C")
    profile = _teacher(db, teacher, "ATT-C", section)
    student = _student(db, section, "ATTC-001", "Roster Student")
    db.session.commit()
    endpoint = f"/teacher/attendance/{section.id}/students/{student.id}"

    invalid_status = teacher_client.post(
        endpoint, data={"date": app_today().isoformat(), "status": "Vacationing"}
    )
    assert invalid_status.status_code == 302
    invalid_date = teacher_client.post(
        endpoint, data={"date": "not-a-date", "status": "Present"}
    )
    assert invalid_date.status_code == 400
    future_date = teacher_client.post(
        endpoint,
        data={"date": "2999-01-01", "status": "Present"},
    )
    assert future_date.status_code == 400
    assert db.session.execute(
        db.select(AttendanceRecord).filter_by(student_id=student.id)
    ).first() is None

    assignment = db.session.execute(
        db.select(TeacherClassAssignment).filter_by(
            teacher_id=profile.id, class_section_id=section.id
        )
    ).scalar_one()
    assignment.is_active = False
    db.session.commit()
    assert teacher_client.get(
        f"/teacher/attendance?class_section_id={section.id}"
    ).status_code == 404
    assert teacher_client.post(
        endpoint, data={"date": app_today().isoformat(), "status": "Present"}
    ).status_code == 404


def test_teacher_cannot_access_global_attendance_or_recognition_bypasses(
    teacher_client, teacher, db
):
    section = _class(db, "Class D", "D")
    _teacher(db, teacher, "ATT-D", section)
    db.session.commit()
    assert teacher_client.get("/attendance").headers["Location"].endswith(
        "/teacher/attendance"
    )
    assert teacher_client.get("/").headers["Location"].endswith("/teacher/")
    assert teacher_client.get("/analytics").headers["Location"].endswith(
        "/teacher/attendance"
    )
    assert teacher_client.get("/reports").headers["Location"].endswith(
        "/teacher/attendance"
    )
    assert teacher_client.get("/api/today_attendance").status_code == 403
    assert teacher_client.get("/api/analytics/top_students").status_code == 403
    assert teacher_client.post("/auto_mark_attendance").status_code == 403
    assert teacher_client.post("/start_face_recognition").status_code == 403
    assert teacher_client.get("/get_detected_faces").status_code == 403


def test_recognition_uses_only_assigned_students_and_never_retrains(
    teacher_client, teacher, db, monkeypatch
):
    import app.recognition as recognition
    from app.services import student_service

    own_class = _class(db, "Class E", "E")
    other_class = _class(db, "Class F", "F")
    _teacher(db, teacher, "ATT-E", own_class)
    other_user = User(
        username="teacher-f-attendance",
        email="teacher-f-attendance@example.test",
        role=Role.TEACHER.value,
    )
    other_user.set_password(TEACHER_PASSWORD)
    db.session.add(other_user)
    db.session.flush()
    other_profile = _teacher(db, other_user, "ATT-F", other_class)
    own_profile = db.session.execute(
        db.select(Teacher).where(Teacher.user_id == teacher.id)
    ).scalar_one()
    own_student = _student(db, own_class, "ATTE-001", "Recognized Own")
    other_student = _student(db, other_class, "ATTF-001", "Recognized Other")
    own_student.face_samples_count = 1
    own_student.face_model_label = 4
    other_student.face_samples_count = 1
    other_student.face_model_label = 5
    db.session.commit()

    class FakeRecognizer:
        pass

    class FakePipeline:
        def __init__(self):
            self.recognizer = FakeRecognizer()
            self.recognizer.is_trained = True
            self.recognizer.label_map = {4: own_student.id, 5: other_student.id}
            self.is_running = False
            self.is_recognizing = False
            self.confirm_frames = 3
            self.known = []

        def load_known(self, known):
            self.known = known

        def start(self, *, preview_only=False):
            assert preview_only is False
            self.is_running = True
            self.is_recognizing = True

        def stop(self):
            self.is_running = False
            self.is_recognizing = False

        def status(self):
            return {"running": self.is_running, "recognizing": self.is_recognizing}

        def detected_faces(self):
            return []

        def take_confirmed(self):
            return {own_student.id: 0.94, other_student.id: 0.91}

        def mjpeg_frames(self):
            return iter(())

    fake_pipeline = FakePipeline()
    monkeypatch.setattr(recognition, "get_pipeline", lambda: fake_pipeline)
    retrain_calls = []
    monkeypatch.setattr(
        student_service, "rebuild_face_model", lambda: retrain_calls.append(True)
    )

    started = teacher_client.post(
        "/teacher/attendance/recognition/start",
        data={"class_section_id": str(own_class.id)},
    )
    assert started.status_code == 200
    owner_record = teacher_client.application.extensions["teacher_recognition_owner"]
    with teacher_client.session_transaction() as owner_session:
        owner_token = owner_session.get("teacher_recognition_token")
    assert owner_token == owner_record["session_token"]
    assert teacher.id != other_user.id
    assert own_profile.id != other_profile.id
    assert [row.pk for row in fake_pipeline.known] == [own_student.id]
    assert retrain_calls == []

    other_client = teacher_client.application.test_client()
    login_response = _login(other_client, other_user)
    assert login_response.status_code == 302
    assert other_client.get("/teacher/attendance/recognition/faces").status_code == 403
    assert other_client.get("/teacher/attendance/recognition/feed").status_code == 403
    assert other_client.get("/api/face_recognition_status").status_code == 403
    assert other_client.get("/api/recognition/status").status_code == 403
    assert other_client.post("/teacher/attendance/recognition/mark").status_code == 403
    assert other_client.post("/teacher/attendance/recognition/stop").status_code == 403

    # Requests from another browser session must not alter either side of the
    # owner's server-record/client-session token pair.
    assert teacher_client.application.extensions["teacher_recognition_owner"] == owner_record
    with teacher_client.session_transaction() as owner_session:
        assert owner_session.get("_user_id") == str(teacher.id)
        assert owner_session.get("teacher_recognition_token") == owner_token
    assert teacher_client.get("/teacher/attendance/recognition/faces").status_code == 200

    marked = teacher_client.post("/teacher/attendance/recognition/mark")
    assert marked.status_code == 200
    result = marked.get_json()["marked_students"]
    assert [row["student_id"] for row in result] == [own_student.student_id]
    assert db.session.execute(
        db.select(AttendanceRecord).filter_by(student_id=own_student.id)
    ).scalar_one_or_none() is not None
    assert db.session.execute(
        db.select(AttendanceRecord).filter_by(student_id=other_student.id)
    ).scalar_one_or_none() is None
    assert teacher_client.get(
        "/teacher/attendance/recognition/faces"
    ).status_code == 200
    assert teacher_client.post(
        "/teacher/attendance/recognition/stop"
    ).status_code == 200
    fake_pipeline.recognizer.label_map = {}
    missing_mapping = teacher_client.post(
        "/teacher/attendance/recognition/start",
        data={"class_section_id": str(own_class.id)},
    )
    assert missing_mapping.status_code == 409
    assert b"valid saved model label" in missing_mapping.data
    assert retrain_calls == []
