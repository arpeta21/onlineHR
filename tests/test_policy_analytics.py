import os
import re
from datetime import date
from types import SimpleNamespace

import policy_rag
from app import app, generate_temp_password
from models import EmployeeProfile, User, JobRequisition, Candidate, Recognition, ImportantDate, db
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


def test_payroll_input_sheet_includes_gratuity_field():
    with app.app_context():
        admin_user = User.query.filter_by(employee_code="PAYROLL_SHEET_ADMIN").first()
        if admin_user is None:
            admin_user = User(
                employee_code="PAYROLL_SHEET_ADMIN",
                role="admin",
                must_change_password=False,
                is_active=True,
            )
            admin_user.set_password("Admin@123")
            db.session.add(admin_user)
            db.session.flush()
            db.session.add(EmployeeProfile(
                user_id=admin_user.id,
                full_name="Payroll Sheet Admin",
                designation="HR Admin",
                department="HR",
            ))
            db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(admin_user.id)
            session["_fresh"] = True

        response = client.get("/admin/payroll/generate?month=9&year=2026")
        assert response.status_code == 200
        assert b"Gratuity Provision" in response.data


def test_payroll_input_sheet_includes_kyc_doj_department_and_statutory_columns():
    with app.app_context():
        admin_user = User.query.filter_by(employee_code="PAYROLL_SHEET_ADMIN_2").first()
        if admin_user is None:
            admin_user = User(
                employee_code="PAYROLL_SHEET_ADMIN_2",
                role="admin",
                must_change_password=False,
                is_active=True,
            )
            admin_user.set_password("Admin@123")
            db.session.add(admin_user)
            db.session.flush()
            db.session.add(EmployeeProfile(
                user_id=admin_user.id,
                full_name="Payroll Sheet Admin 2",
                designation="Finance Admin",
                department="Finance",
                date_of_joining=date(2024, 1, 15),
                pan_number="ABCDE1234F",
                aadhaar_number="123456789012",
            ))
            db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(admin_user.id)
            session["_fresh"] = True

        response = client.get("/admin/payroll/generate?month=9&year=2026")
        assert response.status_code == 200
        for token in [
            b"Employee Code",
            b"Date of Joining",
            b"Department",
            b"KYC",
            b"ESIC Deduction",
            b"Tax Deduction",
        ]:
            assert token in response.data


def test_payroll_input_sheet_export_includes_required_columns():
    with app.app_context():
        admin_user = User.query.filter_by(employee_code="PAYROLL_EXPORT_ADMIN").first()
        if admin_user is None:
            admin_user = User(
                employee_code="PAYROLL_EXPORT_ADMIN",
                role="admin",
                must_change_password=False,
                is_active=True,
            )
            admin_user.set_password("Admin@123")
            db.session.add(admin_user)
            db.session.flush()
            db.session.add(EmployeeProfile(
                user_id=admin_user.id,
                full_name="Payroll Export Admin",
                designation="HR Admin",
                department="HR",
                date_of_joining=date(2024, 1, 15),
            ))
            db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(admin_user.id)
            session["_fresh"] = True

        response = client.get("/admin/payroll/generate/export?month=9&year=2026")
        assert response.status_code == 200
        assert response.mimetype == "text/csv"
        for token in [
            b"Employee Code",
            b"KYC PAN",
            b"Department",
            b"Date of Joining",
            b"ESIC Deduction",
            b"Tax Deduction",
        ]:
            assert token in response.data


def test_payroll_components_dashboard_has_deductions_and_tax_regime_fields():
    with app.app_context():
        admin_user = User.query.filter_by(employee_code="PAYROLL_COMPONENT_ADMIN").first()
        if admin_user is None:
            admin_user = User(
                employee_code="PAYROLL_COMPONENT_ADMIN",
                role="admin",
                must_change_password=False,
                is_active=True,
            )
            admin_user.set_password("Admin@123")
            db.session.add(admin_user)
            db.session.flush()
            db.session.add(EmployeeProfile(
                user_id=admin_user.id,
                full_name="Payroll Component Admin",
                designation="Finance Admin",
                department="Finance",
            ))
            db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(admin_user.id)
            session["_fresh"] = True

        dashboard = client.get("/admin/payroll/components")
        assert dashboard.status_code == 200
        assert b"Deductions" in dashboard.data
        assert b"Tax Regime" in dashboard.data

        add_page = client.get("/admin/payroll/components/new")
        assert add_page.status_code == 200
        assert b"Old Regime" in add_page.data
        assert b"New Regime" in add_page.data


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

        app.config["SESSION_COOKIE_SECURE"] = False
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
        else:
            practice_employee.created_by_id = demo_admin.id
            practice_employee.is_active = True

        db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(demo_admin.id)
            session["_fresh"] = True

        response = client.get("/admin/employees", follow_redirects=False)
        assert response.status_code == 200
        assert b"Practice Employee" in response.data
        assert b"Real Employee" not in response.data


def test_temp_passwords_are_strong_and_random():
    first = generate_temp_password()
    second = generate_temp_password()

    assert first != second
    assert len(first) >= 12
    assert any(ch.isupper() for ch in first)
    assert any(ch.islower() for ch in first)
    assert any(ch.isdigit() for ch in first)
    assert any(ch in "!@#$%^&*" for ch in first)


def test_manager_cannot_export_employee_reports():
    with app.app_context():
        manager = User.query.filter_by(employee_code="MGR_EXPORT_FIX").first()
        if manager is None:
            manager = User(employee_code="MGR_EXPORT_FIX", role="manager", must_change_password=False, is_active=True)
            manager.set_password("Manager@123")
            db.session.add(manager)
            db.session.flush()
            db.session.add(EmployeeProfile(user_id=manager.id, full_name="Manager Export Fix", designation="Manager", department="Operations"))
            db.session.commit()

        manager.must_change_password = False
        manager.is_active = True
        db.session.commit()
        previous_secure_cookie = app.config.get("SESSION_COOKIE_SECURE")
        app.config["SESSION_COOKIE_SECURE"] = False
        try:
            client = app.test_client()
            with client.session_transaction() as session:
                session["_user_id"] = str(manager.id)
                session["_fresh"] = True

            response = client.get("/admin/reports/employees/export", follow_redirects=False)
            assert response.status_code == 403
        finally:
            app.config["SESSION_COOKIE_SECURE"] = previous_secure_cookie


def test_post_without_csrf_token_is_rejected():
    client = app.test_client()
    response = client.post("/login", data={"employee_code": "UNKNOWN", "password": "wrong"})
    assert response.status_code == 400


def test_login_failure_locks_account_after_threshold():
    with app.app_context():
        user = User.query.filter_by(employee_code="LOCKOUT_FIX").first()
        if user is None:
            user = User(employee_code="LOCKOUT_FIX", role="employee", must_change_password=False, is_active=True)
            user.set_password("Correct@123")
            db.session.add(user)
            db.session.flush()
            db.session.add(EmployeeProfile(user_id=user.id, full_name="Lockout Fix", designation="Employee", department="Operations"))
        user.failed_login_attempts = 9
        user.locked_until = None
        db.session.commit()

        client = app.test_client()
        login_page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.get_data(as_text=True)).group(1)
        response = client.post(
            "/login",
            data={"employee_code": "LOCKOUT_FIX", "password": "wrong", "csrf_token": token},
            follow_redirects=False,
        )

        db.session.refresh(user)
        assert response.status_code == 200
        assert user.failed_login_attempts == 10
        assert user.locked_until is not None


def test_demo_admin_cannot_mutate_admin_owned_attendance_ctc_or_appraisal():
    with app.app_context():
        admin = User.query.filter_by(employee_code="ADMIN_SCOPE_FIX").first()
        demo_admin = User.query.filter_by(employee_code="DEMO_SCOPE_FIX").first()
        real_employee = User.query.filter_by(employee_code="EMP_SCOPE_REAL_FIX").first()
        real_employee.created_by_id = admin.id
        db.session.commit()

        app.config["SESSION_COOKIE_SECURE"] = False
        client = app.test_client()
        page = client.get("/login")
        token = re.search(r'name=["\']csrf_token["\'] value=["\']([^"\']+)', page.get_data(as_text=True)).group(1)
        login_response = client.post(
            "/login",
            data={"employee_code": "DEMO_SCOPE_FIX", "password": "Demo@123", "csrf_token": token},
            follow_redirects=False,
        )
        assert login_response.status_code == 302

        attendance = client.post(
            "/admin/attendance/mark",
            data={"user_id": real_employee.id, "date": "2026-09-21", "status": "Present", "csrf_token": token},
        )
        ctc = client.post(
            f"/admin/payroll/ctc/{real_employee.id}",
            data={"monthly_ctc": "50000", "effective_from": "2026-09-21", "csrf_token": token},
        )
        appraisal = client.post(
            "/admin/appraisals/initiate",
            data={"user_id": real_employee.id, "period_type": "Q1 (Sep-Oct)", "year": "2026", "csrf_token": token},
        )

        assert attendance.status_code == 403
        assert ctc.status_code == 403
        assert appraisal.status_code == 403


def test_demo_admin_cannot_download_admin_owned_upload():
    with app.app_context():
        admin = User.query.filter_by(employee_code="ADMIN_SCOPE_FIX").first()
        demo_admin = User.query.filter_by(employee_code="DEMO_SCOPE_FIX").first()
        real_employee = User.query.filter_by(employee_code="EMP_SCOPE_REAL_FIX").first()
        real_employee.created_by_id = admin.id
        real_employee.profile.relieving_letter_filename = "private-test.pdf"
        db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(demo_admin.id)
            session["_fresh"] = True

        response = client.get(f"/uploads/{real_employee.id}/private-test.pdf")
        assert response.status_code == 403


def test_demo_admin_cannot_access_reports_or_admin_owned_recruitment_data():
    with app.app_context():
        admin = User.query.filter_by(employee_code="ADMIN_SCOPE_FIX").first()
        demo_admin = User.query.filter_by(employee_code="DEMO_SCOPE_FIX").first()
        real_req = JobRequisition.query.filter_by(title="REAL_SCOPE_REQUISITION").first()
        if real_req is None:
            real_req = JobRequisition(
                title="REAL_SCOPE_REQUISITION",
                department="Operations",
                number_of_positions=1,
                key_skills="Python",
                requested_by_id=admin.id,
                status="Approved",
            )
            db.session.add(real_req)
            db.session.flush()
            db.session.add(Candidate(
                requisition_id=real_req.id,
                full_name="Real Candidate",
                email="real@example.com",
                key_skills="Python",
            ))
        db.session.commit()

        app.config["SESSION_COOKIE_SECURE"] = False
        client = app.test_client()
        login_page = client.get("/login")
        token = re.search(r'name=["\']csrf_token["\'] value=["\']([^"\']+)', login_page.get_data(as_text=True)).group(1)
        client.post(
            "/login",
            data={"employee_code": "DEMO_SCOPE_FIX", "password": "Demo@123", "csrf_token": token},
        )

        assert client.get("/reports").status_code == 403
        recruitment_response = client.get("/recruitment")
        assert recruitment_response.status_code == 200
        assert b"REAL_SCOPE_REQUISITION" not in recruitment_response.data
        assert b"Real Candidate" not in recruitment_response.data


def test_demo_admin_cannot_read_or_write_cross_tenant_attendance():
    with app.app_context():
        demo_admin = User.query.filter_by(employee_code="DEMO_SCOPE_FIX").first()
        real_employee = User.query.filter_by(employee_code="EMP_SCOPE_REAL_FIX").first()
        real_employee.created_by_id = User.query.filter_by(employee_code="ADMIN_SCOPE_FIX").first().id
        db.session.commit()

        from attendance_logic import bulk_mark_attendance, get_user_monthly_attendance
        from flask_login import login_user, logout_user
        with app.test_request_context("/attendance"):
            login_user(demo_admin, fresh=True)
            assert get_user_monthly_attendance(real_employee.id)["records"] == []
            count, message = bulk_mark_attendance(
                [real_employee.id],
                date(2026, 9, 1),
                date(2026, 9, 1),
                "Present",
            )
            logout_user()
        assert count == 0
        assert "authorised scope" in message


def test_inactive_users_are_not_loaded_into_sessions():
    with app.app_context():
        user = User.query.filter_by(employee_code="INACTIVE_LOAD_FIX").first()
        if user is None:
            user = User(employee_code="INACTIVE_LOAD_FIX", role="employee", is_active=False)
            user.set_password("Inactive@123")
            db.session.add(user)
            db.session.commit()
        else:
            user.is_active = False
            db.session.commit()

        from app import load_user
        assert load_user(user.id) is None


def test_late_threshold_marks_1015_and_1105_as_late(monkeypatch):
    from attendance_logic import clock_in_user
    from datetime import datetime

    class FixedDateTime:
        @classmethod
        def now(cls):
            return datetime(2026, 9, 21, 10, 15)

    monkeypatch.setattr("attendance_logic.now_ist", FixedDateTime.now)
    monkeypatch.setattr("attendance_logic.today_ist", lambda: datetime(2026, 9, 21).date())
    with app.app_context():
        user = User.query.filter_by(employee_code="LATE_FIX").first()
        if user is None:
            user = User(employee_code="LATE_FIX", role="employee", is_active=True)
            user.set_password("LateTest@123")
            db.session.add(user)
            db.session.flush()
            db.session.add(EmployeeProfile(user_id=user.id, full_name="Late Fix", designation="Employee", department="Operations"))
            db.session.commit()
        from models import Attendance
        Attendance.query.filter_by(user_id=user.id, date=datetime(2026, 9, 21).date()).delete()
        db.session.commit()
        record, success, _ = clock_in_user(user.id)
        assert success
        assert record.status == "Late"


def test_proxy_forwarded_ips_have_independent_login_limits():
    def attempt(ip):
        client = app.test_client()
        page = client.get("/login", headers={"X-Forwarded-For": ip})
        token = re.search(r'name=["\']csrf_token["\'] value=["\']([^"\']+)', page.get_data(as_text=True)).group(1)
        return client.post(
            "/login",
            headers={"X-Forwarded-For": ip},
            data={"employee_code": "UNKNOWN", "password": "wrong", "csrf_token": token},
        )

    for _ in range(5):
        assert attempt("198.51.100.10").status_code == 200
    assert attempt("198.51.100.10").status_code == 429
    assert attempt("198.51.100.11").status_code == 200
