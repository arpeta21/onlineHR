import os
from types import SimpleNamespace

import policy_rag
from app import app
from models import EmployeeProfile, User, db
from policy_rag import build_people_analytics, build_policy_answer, search_policy_documents


RESOURCE_1 = SimpleNamespace(
    id=1,
    title="Annual Leave Policy",
    category="Policy",
    description="Leave eligibility and approvals for employees.",
    filename="annual_leave.txt",
)

RESOURCE_2 = SimpleNamespace(
    id=2,
    title="Attendance Rules",
    category="Policy",
    description="Latecomers and work-from-home rules.",
    filename="attendance.txt",
)


def test_search_policy_documents_returns_highest_match():
    matches = search_policy_documents("annual leave approval", [RESOURCE_1, RESOURCE_2], "annual leave eligibility approved by manager")
    assert matches[0]["resource"].title == "Annual Leave Policy"
    assert "leave" in matches[0]["excerpt"].lower()


def test_policy_answer_is_generated_from_best_match():
    answer = build_policy_answer("leave approval", [RESOURCE_1, RESOURCE_2])
    assert "Annual Leave Policy" in answer
    assert "leave" in answer.lower()


def test_search_matches_synonyms_in_uploaded_document_text(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(policy_rag, "UPLOAD_FOLDER", str(upload_dir))

    file_name = "ev_charger_policy.txt"
    file_path = upload_dir / file_name
    file_path.write_text(
        "The company supports different types of electric vehicle charging stations for employees, including Level 1, Level 2 and DC fast chargers.",
        encoding="utf-8",
    )

    resource = SimpleNamespace(
        id=5,
        title="Employee Handbook",
        category="Policy",
        description="General guidance for workplace facilities and employee benefits.",
        filename=file_name,
    )

    matches = search_policy_documents("types of ev charger", [resource])
    assert matches and matches[0]["resource"].title == "Employee Handbook"
    assert "charging" in matches[0]["excerpt"].lower()


def test_search_matches_casual_leave_in_leave_and_attendance_policy(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(policy_rag, "UPLOAD_FOLDER", str(upload_dir))

    file_name = "leave_and_attendance_policy.txt"
    file_path = upload_dir / file_name
    file_path.write_text(
        "Employees are entitled to 12 casual leave days per year. The leave and attendance policy also covers late reporting, overtime, and shift attendance compliance.",
        encoding="utf-8",
    )

    resource = SimpleNamespace(
        id=6,
        title="Leave and Attendance Policy",
        category="Policy",
        description="Detailed rules for leaves, attendance, and employee work-time management.",
        filename=file_name,
    )

    matches = search_policy_documents("casual leaves as per leave and attendance policy", [resource])
    assert matches and matches[0]["resource"].title == "Leave and Attendance Policy"
    assert "casual" in matches[0]["excerpt"].lower()
    assert "leave" in matches[0]["excerpt"].lower()


def test_build_policy_answer_returns_exact_numeric_leave_entitlement(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(policy_rag, "UPLOAD_FOLDER", str(upload_dir))

    file_name = "leave_and_attendance_policy.txt"
    file_path = upload_dir / file_name
    file_path.write_text(
        "Employees are entitled to 12 casual leave days per year. The leave and attendance policy also covers late reporting, overtime, and shift attendance compliance.",
        encoding="utf-8",
    )

    resource = SimpleNamespace(
        id=7,
        title="Leave and Attendance Policy",
        category="Policy",
        description="Detailed rules for leaves, attendance, and employee work-time management.",
        filename=file_name,
    )

    answer = build_policy_answer("casual leaves", [resource])
    assert "12" in answer
    assert "casual leave" in answer.lower()
    assert "closest related document" not in answer.lower()
    assert "not an exact answer" not in answer.lower()


def test_search_uses_uploaded_file_name_when_text_is_not_extractable():
    resource = SimpleNamespace(
        id=3,
        title="Travel Policy",
        category="Policy",
        description="Travel and expenses guide for employees.",
        filename="travel_policy_2025.pdf",
    )
    matches = search_policy_documents("travel policy", [resource])
    assert matches and matches[0]["resource"].title == "Travel Policy"


def test_build_policy_answer_explains_related_but_not_exact_match():
    resource = SimpleNamespace(
        id=4,
        title="Handbook EV Charger Policy",
        category="Policy",
        description="Rules for using office employee charging points and station access.",
        filename="handbook_ev_charger.pdf",
    )
    answer = build_policy_answer("types of ev charger", [resource])
    assert "closest related document" in answer.lower() or "related policy" in answer.lower()


def test_people_analytics_summary_is_reliable():
    ratings = [
        SimpleNamespace(employee_name="Asha", rating=4.8, self_rating=4.5, department="Engineering", manager_name="Raj"),
        SimpleNamespace(employee_name="Rohit", rating=3.6, self_rating=3.7, department="Engineering", manager_name="Raj"),
        SimpleNamespace(employee_name="Neha", rating=2.9, self_rating=2.8, department="HR", manager_name="Maya"),
    ]
    attendance = [
        SimpleNamespace(status="Present", count=18),
        SimpleNamespace(status="Late", count=6),
        SimpleNamespace(status="Absent", count=3),
    ]
    locations = [
        SimpleNamespace(city="Bangalore", count=12),
        SimpleNamespace(city="Hyderabad", count=4),
    ]

    summary = build_people_analytics(ratings, attendance, locations)
    assert summary["top_rated_employee"] == "Asha"
    assert summary["attendance_top_status"] == "Present"
    assert summary["location_leader"] == "Bangalore"
    assert summary["descriptive_points"]
    assert summary["predictive_points"]
    assert summary["prescriptive_points"]
    assert summary["department_breakdown"][0]["department"] == "Engineering"
    assert summary["rating_trend"][0]["label"] == "Asha"


def test_demo_admin_can_access_admin_dashboard():
    with app.app_context():
        demo_admin = User.query.filter_by(employee_code="DEMOADMIN01").first()
        if demo_admin:
            if demo_admin.profile:
                db.session.delete(demo_admin.profile)
            db.session.delete(demo_admin)
            db.session.commit()

        demo_admin = User(employee_code="DEMOADMIN01", role="demo_admin", must_change_password=False, is_active=True)
        demo_admin.set_password("Demo@123")
        db.session.add(demo_admin)
        db.session.flush()
        db.session.add(EmployeeProfile(user_id=demo_admin.id, full_name="Demo Admin", designation="Practice Admin", department="HR Practice"))
        db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(demo_admin.id)
            session["_fresh"] = True

        response = client.get("/admin", follow_redirects=False)
        assert response.status_code == 200
        assert b"Overview" in response.data or b"Dashboard" in response.data or b"Admin" in response.data

        demo_admin.is_active = False
        db.session.commit()
        assert not demo_admin.is_active


def test_demo_admin_sees_only_its_own_practice_users():
    with app.app_context():
        admin_user = User.query.filter_by(employee_code="ADMIN_SCOPE_FIX").first()
        if admin_user is None:
            admin_user = User(employee_code="ADMIN_SCOPE_FIX", role="admin", must_change_password=False, is_active=True)
            admin_user.set_password("Admin@123")
            db.session.add(admin_user)
            db.session.flush()
            db.session.add(EmployeeProfile(user_id=admin_user.id, full_name="Real Admin", designation="HR Admin", department="HR"))

        demo_admin = User.query.filter_by(employee_code="DEMO_SCOPE_FIX").first()
        if demo_admin:
            if demo_admin.profile:
                db.session.delete(demo_admin.profile)
            db.session.delete(demo_admin)
            db.session.commit()

        demo_admin = User(employee_code="DEMO_SCOPE_FIX", role="demo_admin", must_change_password=False, is_active=True)
        demo_admin.set_password("Demo@123")
        db.session.add(demo_admin)
        db.session.flush()
        db.session.add(EmployeeProfile(user_id=demo_admin.id, full_name="Demo Admin", designation="Practice Admin", department="HR Practice"))

        real_employee = User.query.filter_by(employee_code="EMP_SCOPE_REAL_FIX").first()
        if real_employee is None:
            real_employee = User(employee_code="EMP_SCOPE_REAL_FIX", role="employee", must_change_password=False, is_active=True)
            real_employee.set_password("Demo@123")
            db.session.add(real_employee)
            db.session.flush()
            db.session.add(EmployeeProfile(user_id=real_employee.id, full_name="Real Employee", designation="Ops", department="Operations"))

        practice_employee = User.query.filter_by(employee_code="EMP_SCOPE_PRACTICE_FIX").first()
        if practice_employee is None:
            practice_employee = User(employee_code="EMP_SCOPE_PRACTICE_FIX", role="employee", must_change_password=False, is_active=True, created_by_id=demo_admin.id)
            practice_employee.set_password("Demo@123")
            db.session.add(practice_employee)
            db.session.flush()
            db.session.add(EmployeeProfile(user_id=practice_employee.id, full_name="Practice Employee", designation="Practice", department="Practice"))

        db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(demo_admin.id)
            session["_fresh"] = True

        response = client.get("/admin/employees", follow_redirects=False)
        assert response.status_code == 200
        assert b"Practice Employee" in response.data
        assert b"Real Employee" not in response.data
