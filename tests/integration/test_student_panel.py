from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from io import BytesIO

from app.extensions import db
from app.models import AttendanceRecord, LeaveRequest, Role, Student, User
from app.services import student_service
from tests.conftest import ADMIN_PASSWORD, TEACHER_PASSWORD, app_today


def _student_account(db, username: str, identifier: str) -> tuple[User, Student]:
    user = User(
        username=username,
        email=f"{username}@example.test",
        role=Role.STUDENT.value,
    )
    user.set_password("student-password-123")
    student = Student(
        user=user,
        student_id=identifier,
        name=f"Name {username}",
        email=f"{username}.student@example.test",
        phone="1234567890",
        department="Engineering",
        year="2",
        section="A",
    )
    db.session.add_all([user, student])
    db.session.flush()
    return user, student


def _login(client, user: User, password: str = "student-password-123"):
    return client.post("/login", data={"username": user.username, "password": password})


def test_student_panel_requires_student_role_and_active_linked_profile(client, db, admin, teacher):
    for path in (
        "/student/", "/student/attendance", "/student/profile", "/student/leaves",
        "/student/profile/face-registration",
    ):
        assert client.get(path).status_code == 302

    assert _login(client, admin, ADMIN_PASSWORD).status_code == 302
    assert client.get("/student/").status_code == 403
    assert client.get("/student/profile/face-registration").status_code == 403

    teacher_client = client.application.test_client()
    assert _login(teacher_client, teacher, TEACHER_PASSWORD).status_code == 302
    assert teacher_client.get("/student/").status_code == 403
    assert teacher_client.get("/student/profile/face-registration").status_code == 403

    profileless = User(
        username="student-without-profile",
        email="student-without-profile@example.test",
        role=Role.STUDENT.value,
    )
    profileless.set_password("student-password-123")
    db.session.add(profileless)
    db.session.commit()
    profileless_client = client.application.test_client()
    assert _login(profileless_client, profileless).status_code == 302
    assert profileless_client.get("/student/").status_code == 403
    assert profileless_client.get("/student/profile/face-registration").status_code == 403

    inactive_user, inactive_student = _student_account(db, "inactive-student", "STU-INACTIVE")
    inactive_student.is_active = False
    db.session.commit()
    inactive_client = client.application.test_client()
    assert _login(inactive_client, inactive_user).status_code == 302
    assert inactive_client.get("/student/").status_code == 403
    assert inactive_client.get("/student/profile/face-registration").status_code == 403


def test_student_dashboard_attendance_and_leaves_are_scoped_to_own_profile(
    app, client, db
):
    own_user, own = _student_account(db, "student-own", "STU-OWN-001")
    _other_user, other = _student_account(db, "student-other", "STU-OTHER-001")
    today = app_today()
    db.session.add_all(
        [
            AttendanceRecord(student_id=own.id, date=today, status="Present", marked_by="teacher"),
            AttendanceRecord(student_id=other.id, date=today, status="Absent", marked_by="teacher"),
            LeaveRequest(
                student_id=own.id,
                leave_type="Sick",
                start_date=today,
                end_date=today + timedelta(days=1),
                reason="A long enough own leave reason.",
            ),
            LeaveRequest(
                student_id=other.id,
                leave_type="Personal",
                start_date=today,
                end_date=today,
                reason="Another student's private leave request.",
            ),
        ]
    )
    db.session.commit()
    assert _login(client, own_user).status_code == 302

    dashboard = client.get("/student/")
    assert dashboard.status_code == 200
    dashboard_text = dashboard.get_data(as_text=True)
    assert "STUDENT PANEL" in dashboard_text
    assert "Name student-own" in dashboard_text
    assert "STU-OWN-001" in dashboard_text
    assert "Name student-other" not in dashboard_text
    assert "STU-OTHER-001" not in dashboard_text
    assert "Another student's private leave request." not in dashboard_text

    attendance = client.get("/student/attendance")
    assert attendance.status_code == 200
    assert "Present" in attendance.get_data(as_text=True)
    assert "Absent" not in attendance.get_data(as_text=True)

    leaves = client.get("/student/leaves")
    leaves_text = leaves.get_data(as_text=True)
    assert leaves.status_code == 200
    assert "A long enough own leave reason." in leaves_text
    assert "Another student's private leave request." not in leaves_text


def test_student_cannot_forge_another_student_id_or_access_legacy_global_pages(
    client, db
):
    own_user, own = _student_account(db, "student-forge-own", "STU-FORGE-OWN")
    _other_user, other = _student_account(db, "student-forge-other", "STU-FORGE-OTHER")
    db.session.commit()
    assert _login(client, own_user).status_code == 302

    response = client.post(
        "/student/leaves",
        data={
            "student_id": str(other.id),
            "leave_type": "Sick",
            "start_date": app_today().isoformat(),
            "end_date": (app_today() + timedelta(days=1)).isoformat(),
            "reason": "This is a valid leave reason.",
            "status": "Approved",
            "reviewed_by": "forged-reviewer",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    row = db.session.execute(db.select(LeaveRequest)).scalar_one()
    assert row.student_id == own.id
    assert row.status == "Pending"
    assert row.reviewed_by is None
    assert client.get(f"/api/student/{own.id}").status_code == 200
    assert client.get(f"/api/student/{other.id}").status_code == 404

    for path in ("/leave", "/attendance", "/api/attendance_summary", "/api/leave/1"):
        assert client.get(path).status_code == 403
    assert client.get("/students").status_code == 403
    assert client.post(
        "/apply_leave",
        data={
            "student_id": str(other.id),
            "leave_type": "Sick",
            "start_date": app_today().isoformat(),
            "end_date": app_today().isoformat(),
            "reason": "Forged legacy leave request.",
        },
    ).status_code == 403
    assert client.post(
        "/review_leave",
        data={"leave_id": row.id, "status": "Approved", "review_notes": "self-approved"},
    ).status_code == 403


def test_student_leave_validation_and_profile_edit_are_limited_to_safe_fields(client, db):
    user, student = _student_account(db, "student-edit", "STU-EDIT-001")
    db.session.commit()
    assert _login(client, user).status_code == 302

    invalid = client.post(
        "/student/leaves",
        data={
            "leave_type": "Unlisted",
            "start_date": "not-a-date",
            "end_date": app_today().isoformat(),
            "reason": "short",
        },
    )
    assert invalid.status_code == 400
    assert db.session.execute(db.select(LeaveRequest)).scalar_one_or_none() is None

    response = client.post(
        "/student/profile",
        data={
            "phone": "+15555550199",
            "student_id": "FORGED-IDENTIFIER",
            "name": "Changed Name",
            "user_id": "9999",
            "role": "admin",
            "class_section_id": "9999",
        },
        follow_redirects=True,
    )
    db.session.refresh(student)
    assert response.status_code == 200
    assert student.phone == "+15555550199"
    assert student.student_id == "STU-EDIT-001"
    assert student.name == "Name student-edit"
    assert student.user_id == user.id
    assert student.class_section_id is None

    invalid_phone = client.post("/student/profile", data={"phone": "invalid!"})
    assert invalid_phone.status_code == 400


def test_admin_and_teacher_keep_existing_leave_panel_access(app, admin, teacher):
    admin_client = app.test_client()
    teacher_client = app.test_client()
    assert _login(admin_client, admin, ADMIN_PASSWORD).status_code == 302
    assert _login(teacher_client, teacher, TEACHER_PASSWORD).status_code == 302
    assert admin_client.get("/leave").status_code == 200
    assert teacher_client.get("/leave").status_code == 200


def test_student_face_registration_is_owned_consented_and_teacher_compatible(
    client, db, tmp_path, monkeypatch
):
    own_user, own = _student_account(db, "face-self", "STU-FACE-SELF")
    _other_user, other = _student_account(db, "face-other", "STU-FACE-OTHER")
    db.session.commit()
    assert _login(client, own_user).status_code == 302

    # Consent is enforced server-side before the enrollment service is called.
    unconsented = client.post(
        "/student/profile/face-registration",
        data={"image": (BytesIO(b"image bytes"), "capture.jpg")},
    )
    assert unconsented.status_code == 400
    assert db.session.get(Student, own.id).face_samples_count == 0

    upload_path = tmp_path / "temporary-upload.jpg"

    def store_upload(payload, folder, prefix, allowed_extensions, max_pixels):
        upload_path.write_bytes(payload)
        return str(upload_path)

    def add_sample(image_path, student_pk, *, face_root, detector, crop_size, crop_margin):
        sample_dir = Path(face_root) / "samples" / str(student_pk)
        sample_dir.mkdir(parents=True, exist_ok=True)
        sample_path = sample_dir / "001.png"
        sample_path.write_bytes(b"normalized face sample")
        return sample_path

    def rebuild_model():
        db.session.get(Student, own.id).face_samples_count = 1
        db.session.get(Student, own.id).face_model_label = 42
        db.session.commit()
        Path(client.application.config["FACE_MODEL_PATH"]).write_bytes(b"fake LBPH model")
        label_path = Path(client.application.config["FACE_LABEL_MAP_PATH"])
        label_path.parent.mkdir(parents=True, exist_ok=True)
        label_path.write_text(json.dumps({"42": own.id}), encoding="utf-8")
        return 1

    monkeypatch.setattr(student_service, "store_image", store_upload)
    monkeypatch.setattr(student_service, "add_sample_from_image", add_sample)
    monkeypatch.setattr(student_service, "rebuild_face_model", rebuild_model)
    monkeypatch.setattr(
        student_service, "get_recognizer", lambda: SimpleNamespace(label_map={42: own.id})
    )

    response = client.post(
        "/student/profile/face-registration",
        data={
            "consent": "yes",
            # Forged IDs are ignored; the authenticated profile determines ownership.
            "student_id": str(other.id),
            "image": (BytesIO(b"self capture bytes"), "self.jpg"),
        },
    )
    assert response.status_code == 302
    db.session.refresh(own)
    assert own.face_samples_count == 1
    assert own.face_model_label == 42
    assert student_service.count_samples(client.application.config["FACE_DATA_FOLDER"], own.id) == 1
    assert student_service.count_samples(client.application.config["FACE_DATA_FOLDER"], other.id) == 0
    assert student_service.get_recognizer().label_map.get(own.face_model_label) == own.id
    assert not upload_path.exists()

    sample = Path(client.application.config["FACE_DATA_FOLDER"]) / "samples" / str(own.id) / "001.png"
    original_sample = sample.read_bytes()
    duplicate = client.post(
        "/student/profile/face-registration",
        data={"consent": "yes", "image": (BytesIO(b"second capture"), "again.jpg")},
    )
    assert duplicate.status_code == 400
    assert sample.read_bytes() == original_sample
