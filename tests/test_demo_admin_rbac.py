"""
test_demo_admin_rbac.py
=======================
Authorization tests for the demo_admin role in Sanvit HRMS.

Coverage:
  - demo_admin PERMITTED access (read-only admin views, own-tenant data)
  - demo_admin DENIED access (destruction, payroll write, sensitive PII docs,
    leave decisions, HR letter issuance, CTC write)
  - Employee and Manager denied admin-only routes (vertical privilege)
  - Direct URL / POST access to restricted endpoints (no session injection tricks)

Fixture pattern mirrors test_tenant_isolation.py:
  - Uses app.test_client() + _session_as() to set the authenticated user.
  - CSRF is disabled for the duration of POST tests.
  - All DB rows created by a test are deleted in the `finally` block so the
    test database is left clean regardless of assertion failures.
"""

import os
from io import BytesIO
from datetime import date, datetime
import re
from secrets import token_hex

from app import app
from statutory import approved_tax_deduction, monthly_tds
from models import (
    EmployeeProfile,
    EmployeeCTC,
    HRLetterTemplate,
    InvestmentDeclaration,
    LeaveApplication,
    LeaveType,
    Tenant,
    User,
    db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_as(client, user_id):
    """Inject a valid Flask-Login session for *user_id* without going through /login."""
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def _build_demo_env(suffix):
    """
    Create a minimal but complete demo environment:
      - demo_tenant  — tenant of type "demo"
      - demo_admin   — role=demo_admin, owns demo_tenant
      - employee     — role=employee, created_by=demo_admin, in demo_tenant
      - leave_type   — a generic leave type so leave applications can be made
      - leave_app    — a pending leave application by the employee

    Returns a dict of all created objects (committed).
    """
    demo_tenant = Tenant(company_name=f"Demo Corp {suffix}", tenant_type="demo")
    db.session.add(demo_tenant)
    db.session.flush()

    demo_admin = User(
        employee_code=f"DA{suffix}",
        role="demo_admin",
        tenant_id=demo_tenant.id,
        must_change_password=False,
    )
    demo_admin.set_password("Demo@123")
    db.session.add(demo_admin)
    db.session.flush()
    db.session.add(EmployeeProfile(
        user_id=demo_admin.id,
        tenant_id=demo_tenant.id,
        full_name="Demo Admin User",
    ))

    employee = User(
        employee_code=f"EMP{suffix}",
        role="employee",
        tenant_id=demo_tenant.id,
        created_by_id=demo_admin.id,
        must_change_password=False,
    )
    employee.set_password("Emp@1234")
    db.session.add(employee)
    db.session.flush()
    db.session.add(EmployeeProfile(
        user_id=employee.id,
        tenant_id=demo_tenant.id,
        full_name="Demo Employee",
        pan_number="TESTPAN1234",
        aadhaar_number="123456789012",
    ))

    # Leave type (unique name per run to avoid clashes with other test data)
    leave_type = LeaveType(name=f"CasualLeave{suffix}", annual_quota=12, is_active=True)
    db.session.add(leave_type)
    db.session.flush()

    leave_app = LeaveApplication(
        tenant_id=demo_tenant.id,
        user_id=employee.id,
        leave_type_id=leave_type.id,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 1),
        days=1.0,
        reason="Test leave",
        status="pending",
    )
    db.session.add(leave_app)

    db.session.commit()
    return {
        "demo_tenant": demo_tenant,
        "demo_admin": demo_admin,
        "employee": employee,
        "leave_type": leave_type,
        "leave_app": leave_app,
    }


def _teardown_demo_env(env):
    """Delete all rows created by _build_demo_env in safe dependency order."""
    # Leave application first (references user + leave_type)
    db.session.delete(env["leave_app"])
    # CTC rows if any
    for ctc in EmployeeCTC.query.filter_by(user_id=env["employee"].id).all():
        db.session.delete(ctc)
    # Leave type
    db.session.delete(env["leave_type"])
    # Profiles then users
    if env["employee"].profile:
        db.session.delete(env["employee"].profile)
    db.session.delete(env["employee"])
    if env["demo_admin"].profile:
        db.session.delete(env["demo_admin"].profile)
    db.session.delete(env["demo_admin"])
    db.session.delete(env["demo_tenant"])
    db.session.commit()


# ---------------------------------------------------------------------------
# 1. demo_admin PERMITTED access
# ---------------------------------------------------------------------------

def test_demo_admin_can_view_admin_dashboard():
    """demo_admin reaches the admin dashboard (200 or redirect to it)."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        try:
            resp = client.get("/admin", follow_redirects=True)
            assert resp.status_code == 200
        finally:
            _teardown_demo_env(env)


def test_demo_admin_can_view_employee_list():
    """demo_admin can view the employee list page."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        try:
            resp = client.get("/admin/employees", follow_redirects=True)
            assert resp.status_code == 200
            page = resp.get_data(as_text=True)
            assert 'id="select-all-people"' in page
            assert page.count('class="person-select"') == 2
            assert page.count('id="person-') == 2
            assert 'name="selected_user_ids"' in page
            assert 'form="people-selection-form"' in page
            assert 'getElementById("select-all-people")' in page
            assert 'querySelectorAll(".person-select")' in page
            assert 'action="/admin/employees/bulk-action"' in page
            assert 'name="action" value="delete"' in page
            assert 'id="delete-selected-people"' not in page

            csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
            selected_resp = client.post(
                "/admin/employees",
                data={
                    "csrf_token": csrf_token,
                    "selected_user_ids": [env["demo_admin"].id, env["employee"].id],
                },
            )
            selected_page = selected_resp.get_data(as_text=True)
            assert selected_resp.status_code == 200
            assert 'id="person-{}" name="selected_user_ids" value="{}" form="people-selection-form" class="person-select" checked'.format(
                env["demo_admin"].id, env["demo_admin"].id
            ) in selected_page
            assert 'id="person-{}" name="selected_user_ids" value="{}" form="people-selection-form" class="person-select" checked'.format(
                env["employee"].id, env["employee"].id
            ) in selected_page
            assert "2 selected" in selected_page
        finally:
            _teardown_demo_env(env)


def test_real_admin_sees_bulk_delete_control():
    """Only real admins should see the permanent bulk-delete button."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        env["demo_admin"].role = "admin"
        db.session.commit()
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        try:
            resp = client.get("/admin/employees", follow_redirects=True)
            assert resp.status_code == 200
            page = resp.get_data(as_text=True)
            assert 'id="delete-selected-people"' in page
            assert 'action="/admin/employees/bulk-action"' in page
            assert 'confirm(\'Are you sure you want to delete the selected records?\')' in page
        finally:
            _teardown_demo_env(env)


def test_real_admin_payroll_subpage_links_to_dashboard():
    """Payroll sub-pages link to the existing Payroll dashboard route."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        env["demo_admin"].role = "admin"
        db.session.commit()
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        try:
            resp = client.get("/admin/payroll/advances", follow_redirects=True)
            assert resp.status_code == 200
            assert b'href="/admin/payroll"' in resp.data
            assert "Back to Payroll".encode() in resp.data
        finally:
            _teardown_demo_env(env)


def test_employee_can_save_draft_submit_and_only_access_own_declarations():
    """Employees can draft/submit their own declarations but cannot access another tenant's."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        env["demo_admin"].role = "admin"
        env["employee"].profile.tax_regime = "old_regime"
        db.session.commit()
        foreign_tenant = Tenant(company_name=f"Investment Tenant {suffix}", tenant_type="demo")
        db.session.add(foreign_tenant)
        db.session.flush()
        foreign_user = User(
            employee_code=f"IX{suffix}",
            role="employee",
            tenant_id=foreign_tenant.id,
            must_change_password=False,
        )
        foreign_user.set_password("Foreign@1234")
        db.session.add(foreign_user)
        db.session.flush()
        db.session.add(EmployeeProfile(
            user_id=foreign_user.id,
            tenant_id=foreign_tenant.id,
            full_name="Foreign Investment Employee",
        ))
        foreign_declaration = InvestmentDeclaration(
            user_id=foreign_user.id,
            tenant_id=foreign_tenant.id,
            financial_year="2026-27",
            tax_regime="old_regime",
            section="80C",
            declared_amount=50000,
            status="submitted",
        )
        db.session.add(foreign_declaration)
        db.session.commit()

        foreign_declaration_id = foreign_declaration.id
        client = app.test_client()
        _session_as(client, env["employee"].id)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        uploaded_filename = None
        try:
            fy = f"{date.today().year}-{(date.today().year + 1) % 100:02d}"
            page = client.get("/employee/payroll/investments")
            assert page.status_code == 200
            assert b"Save Draft" in page.data
            assert b"Submit Declaration" in page.data
            assert b'href="/employee/payroll/investments"' in page.data

            draft_response = client.post("/employee/payroll/investments", data={
                "action": "save_draft",
                "financial_year": fy,
                "section": "80C",
                "declared_amount": "120000",
            })
            assert draft_response.status_code == 302
            declaration = InvestmentDeclaration.query.filter_by(
                user_id=env["employee"].id,
                financial_year=fy,
                section="80C",
            ).one()
            assert declaration.status == "draft"
            assert declaration.document_filename is None
            declaration_id = declaration.id

            assert client.get(
                f"/employee/payroll/investments?edit={foreign_declaration_id}"
            ).status_code == 404
            assert client.get(
                f"/payroll/investments/{foreign_declaration_id}/document"
            ).status_code == 403
            assert client.post("/employee/payroll/investments", data={
                "action": "save_draft",
                "declaration_id": str(foreign_declaration_id),
                "financial_year": fy,
                "section": "80C",
                "declared_amount": "1",
            }).status_code == 404
            assert InvestmentDeclaration.query.get(foreign_declaration_id).declared_amount == 50000

            submit_response = client.post(
                "/employee/payroll/investments",
                data={
                    "action": "submit",
                    "declaration_id": str(declaration_id),
                    "financial_year": fy,
                    "section": "80C",
                    "declared_amount": "120000",
                    "document": (BytesIO(b"%PDF-1.4\nproof"), "proof.pdf"),
                },
                content_type="multipart/form-data",
            )
            assert submit_response.status_code == 302
            db.session.refresh(declaration)
            assert declaration.status == "submitted"
            assert declaration.tax_regime == "old_regime"
            assert declaration.document_filename
            uploaded_filename = declaration.document_filename

            assert approved_tax_deduction([declaration], "old_regime") == 0
            submitted_page = client.get("/employee/payroll/investments")
            assert b"Submitted" in submitted_page.data
            assert b"Waiting for HR review" in submitted_page.data

            declaration.status = "approved"
            declaration.approved_amount = 80000
            declaration.decision_note = "Proof verified"
            rejected_declaration = InvestmentDeclaration(
                user_id=env["employee"].id,
                tenant_id=env["demo_tenant"].id,
                financial_year=fy,
                tax_regime="old_regime",
                section="80D",
                declared_amount=20000,
                status="rejected",
                decision_note="Please upload itemized proof.",
            )
            db.session.add(rejected_declaration)
            db.session.commit()
            status_page = client.get("/employee/payroll/investments")
            assert b"Approved" in status_page.data
            assert b"Proof verified" in status_page.data
            assert b"Rejected" in status_page.data
            assert b"Please upload itemized proof." in status_page.data
            assert b"Update and resubmit" in status_page.data
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            for declaration in InvestmentDeclaration.query.filter(
                db.or_(
                    InvestmentDeclaration.user_id == env["employee"].id,
                    InvestmentDeclaration.id == foreign_declaration_id,
                )
            ).all():
                db.session.delete(declaration)
            db.session.commit()
            if uploaded_filename:
                upload_path = os.path.join(app.config["UPLOAD_FOLDER"], uploaded_filename)
                if os.path.isfile(upload_path):
                    os.remove(upload_path)
            db.session.delete(foreign_user.profile)
            db.session.delete(foreign_user)
            db.session.delete(foreign_tenant)
            db.session.commit()
            _teardown_demo_env(env)


def test_admin_reviews_declarations_and_approved_old_regime_amount_reduces_tds():
    """Only admin-approved eligible amounts reduce the existing old-regime TDS calculation."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        env["demo_admin"].role = "admin"
        env["employee"].profile.tax_regime = "old_regime"
        declaration = InvestmentDeclaration(
            user_id=env["employee"].id,
            tenant_id=env["demo_tenant"].id,
            financial_year="2026-27",
            tax_regime="old_regime",
            section="80C",
            declared_amount=120000,
            status="submitted",
            document_filename="proof.pdf",
        )
        rejected_declaration = InvestmentDeclaration(
            user_id=env["employee"].id,
            tenant_id=env["demo_tenant"].id,
            financial_year="2026-27",
            tax_regime="old_regime",
            section="80D",
            declared_amount=20000,
            status="submitted",
        )
        db.session.add_all([declaration, rejected_declaration])
        db.session.commit()
        declaration_id = declaration.id
        rejected_declaration_id = rejected_declaration.id
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        admin_client = app.test_client()
        _session_as(admin_client, env["demo_admin"].id)
        try:
            admin_page = admin_client.get("/admin/payroll/investments")
            assert admin_page.status_code == 200
            assert env["employee"].employee_code.encode() in admin_page.data
            assert b"Submitted" in admin_page.data
            assert b"View proof" in admin_page.data

            empty_rejection = admin_client.post(
                f"/admin/payroll/investments/{rejected_declaration_id}/decision",
                data={"decision": "rejected"},
                follow_redirects=True,
            )
            assert b"Add a rejection remark" in empty_rejection.data
            assert InvestmentDeclaration.query.get(rejected_declaration_id).status == "submitted"

            approved_response = admin_client.post(
                f"/admin/payroll/investments/{declaration_id}/decision",
                data={
                    "decision": "approved",
                    "approved_amount": "80000",
                    "decision_note": "Proof verified",
                },
            )
            assert approved_response.status_code == 302
            db.session.refresh(declaration)
            assert declaration.status == "approved"
            assert declaration.approved_amount == 80000
            assert declaration.decision_note == "Proof verified"

            rejected_response = admin_client.post(
                f"/admin/payroll/investments/{rejected_declaration_id}/decision",
                data={
                    "decision": "rejected",
                    "decision_note": "Proof does not support the declared amount.",
                },
            )
            assert rejected_response.status_code == 302
            db.session.refresh(rejected_declaration)
            assert rejected_declaration.status == "rejected"
            assert approved_tax_deduction([rejected_declaration], "old_regime") == 0

            approved_deduction = approved_tax_deduction([declaration], "old_regime")
            assert approved_deduction == 80000
            base_tds = monthly_tds(1500000, 0, 12, fy="2026-27", tax_regime="old_regime")
            approved_tds = monthly_tds(
                1500000, 0, 12, fy="2026-27", tax_regime="old_regime",
                other_deductions=approved_deduction,
            )
            assert approved_tds < base_tds
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            for row in InvestmentDeclaration.query.filter_by(user_id=env["employee"].id).all():
                db.session.delete(row)
            db.session.commit()
            _teardown_demo_env(env)


def test_real_admin_can_bulk_delete_selected_employees():
    """Selected employee IDs are passed through to the guarded bulk delete route."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        bulk_user_ids = []
        foreign_tenant = None
        foreign_user_id = None
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            env["demo_admin"].role = "admin"
            for index in range(2):
                user = User(
                    employee_code=f"BD{suffix}{index}",
                    role="employee",
                    tenant_id=env["demo_tenant"].id,
                    created_by_id=bulk_user_ids[0] if bulk_user_ids else env["demo_admin"].id,
                    must_change_password=False,
                )
                user.set_password("Bulk@1234")
                db.session.add(user)
                db.session.flush()
                if index == 0:
                    user.created_by_id = user.id
                bulk_user_ids.append(user.id)
                db.session.add(EmployeeProfile(
                    user_id=user.id,
                    tenant_id=env["demo_tenant"].id,
                    full_name=f"Bulk Delete Employee {index}",
                ))
            foreign_tenant = Tenant(
                company_name=f"Foreign Demo Corp {suffix}",
                tenant_type="demo",
            )
            db.session.add(foreign_tenant)
            db.session.flush()
            foreign_user = User(
                employee_code=f"FD{suffix}",
                role="employee",
                tenant_id=foreign_tenant.id,
                must_change_password=False,
            )
            foreign_user.set_password("Foreign@1234")
            db.session.add(foreign_user)
            db.session.flush()
            foreign_user_id = foreign_user.id
            db.session.add(EmployeeProfile(
                user_id=foreign_user.id,
                tenant_id=foreign_tenant.id,
                full_name="Foreign Tenant Employee",
            ))
            db.session.commit()

            client = app.test_client()
            _session_as(client, env["demo_admin"].id)
            empty_resp = client.post(
                "/admin/employees/bulk-action",
                data={"action": "delete"},
                follow_redirects=True,
            )
            assert b"Please select at least one record." in empty_resp.data

            cross_tenant_resp = client.post(
                "/admin/employees/bulk-action",
                data={
                    "action": "delete",
                    "selected_user_ids": [str(bulk_user_ids[0]), str(foreign_user_id)],
                },
                follow_redirects=False,
            )
            assert cross_tenant_resp.status_code == 302
            assert User.query.get(bulk_user_ids[0]) is None
            assert User.query.get(bulk_user_ids[1]) is not None
            assert User.query.get(foreign_user_id) is None

            resp = client.post(
                "/admin/employees/bulk-action",
                data={
                    "action": "delete",
                    "selected_user_ids": [str(bulk_user_ids[1])],
                },
                follow_redirects=False,
            )
            assert resp.status_code == 302
            assert all(User.query.get(user_id) is None for user_id in bulk_user_ids)
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            db.session.rollback()
            for user_id in bulk_user_ids:
                remaining_user = User.query.get(user_id)
                if remaining_user:
                    if remaining_user.profile:
                        db.session.delete(remaining_user.profile)
                    db.session.delete(remaining_user)
            if foreign_user_id:
                foreign_user = User.query.get(foreign_user_id)
                if foreign_user:
                    if foreign_user.profile:
                        db.session.delete(foreign_user.profile)
                    db.session.delete(foreign_user)
            if foreign_tenant and Tenant.query.get(foreign_tenant.id):
                db.session.delete(foreign_tenant)
            db.session.commit()
            _teardown_demo_env(env)


def test_demo_admin_can_view_own_tenant_employee_profile():
    """demo_admin can view the profile of an employee they created."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        emp_id = env["employee"].id
        try:
            resp = client.get(f"/admin/employees/{emp_id}", follow_redirects=True)
            assert resp.status_code == 200
        finally:
            _teardown_demo_env(env)


def test_demo_admin_can_view_leave_requests():
    """demo_admin can view the leave requests list (read-only)."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        try:
            resp = client.get("/admin/leaves", follow_redirects=True)
            assert resp.status_code == 200
        finally:
            _teardown_demo_env(env)


def test_demo_admin_can_view_admin_attendance():
    """demo_admin can access the admin attendance view."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        try:
            resp = client.get("/admin/attendance", follow_redirects=True)
            assert resp.status_code == 200
        finally:
            _teardown_demo_env(env)


def test_demo_admin_can_view_payroll_generate_page():
    """demo_admin may view the payroll generation page (GET), but not submit it."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        try:
            resp = client.get("/admin/payroll/generate", follow_redirects=True)
            assert resp.status_code == 200
        finally:
            _teardown_demo_env(env)


def test_demo_admin_can_view_payslip_list():
    """demo_admin can view the payslip list page."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        try:
            resp = client.get("/admin/payroll/payslips", follow_redirects=True)
            assert resp.status_code == 200
        finally:
            _teardown_demo_env(env)


def test_demo_admin_can_download_own_tenant_employee_profile_picture():
    """demo_admin can download the profile picture of an employee they own (non-sensitive doc)."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        # Give the employee a fake profile picture filename (no actual file—
        # we just confirm the route does NOT return 403; a 404 is acceptable
        # because the file doesn't exist on disk in the test environment).
        env["employee"].profile.profile_picture_filename = f"profile_{suffix}.png"
        db.session.commit()
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        emp_id = env["employee"].id
        try:
            resp = client.get(f"/employee-documents/{emp_id}/profile-picture")
            # 404 = file not on disk (expected). 403 = wrongly blocked. 200 = file served.
            assert resp.status_code != 403, (
                "demo_admin should be able to access profile pictures of their own employees"
            )
        finally:
            _teardown_demo_env(env)


# ---------------------------------------------------------------------------
# 2. demo_admin DENIED access — all 8 hardened endpoints
# ---------------------------------------------------------------------------

def test_demo_admin_cannot_delete_employee():
    """GAP 1 — POST /admin/employees/<id>/delete must return 403 for demo_admin."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        emp_id = env["employee"].id
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(f"/admin/employees/{emp_id}/delete", follow_redirects=False)
            assert resp.status_code == 403, (
                f"Expected 403 but got {resp.status_code}. "
                "demo_admin must not permanently delete employees."
            )
            # Confirm the employee still exists in the database
            still_exists = User.query.get(emp_id)
            assert still_exists is not None, "Employee was deleted despite 403"
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _teardown_demo_env(env)


def test_demo_admin_cannot_bulk_delete_employees():
    """GAP 2 — POST /admin/employees/bulk-action must return 403 for demo_admin."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        emp_id = env["employee"].id
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                "/admin/employees/bulk-action",
                data={"action": "delete", "selected_user_ids": str(emp_id)},
                follow_redirects=False,
            )
            assert resp.status_code == 403, (
                f"Expected 403 but got {resp.status_code}. "
                "demo_admin must not bulk-delete employees."
            )
            still_exists = User.query.get(emp_id)
            assert still_exists is not None, "Employee was deleted despite 403"
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _teardown_demo_env(env)


def test_demo_admin_cannot_process_payroll():
    """GAP 3 — POST /admin/payroll/generate with process_payroll must return 403."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                "/admin/payroll/generate",
                data={"month": "10", "year": "2026", "process_payroll": "1"},
                follow_redirects=False,
            )
            assert resp.status_code == 403, (
                f"Expected 403 but got {resp.status_code}. "
                "demo_admin must not trigger payroll generation."
            )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _teardown_demo_env(env)


def test_demo_admin_cannot_save_employee_ctc():
    """GAP 4 — POST /admin/payroll/ctc/<id> must return 403 for demo_admin."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        emp_id = env["employee"].id
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                f"/admin/payroll/ctc/{emp_id}",
                data={"monthly_ctc": "50000", "effective_from": "2026-01-01"},
                follow_redirects=False,
            )
            assert resp.status_code == 403, (
                f"Expected 403 but got {resp.status_code}. "
                "demo_admin must not write employee CTC records."
            )
            # Confirm no CTC row was written
            ctc = EmployeeCTC.query.filter_by(user_id=emp_id).first()
            assert ctc is None, "CTC record was written despite 403"
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _teardown_demo_env(env)


def test_demo_admin_cannot_download_pan_document():
    """GAP 5 — GET /employee-documents/<id>/pan must return 403 for demo_admin."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        # Assign a fake filename so the route sees a non-null value and
        # proceeds to the permission check (rather than 404 on missing filename).
        env["employee"].profile.pan_document_filename = f"pan_{suffix}.png"
        db.session.commit()
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        emp_id = env["employee"].id
        try:
            resp = client.get(f"/employee-documents/{emp_id}/pan")
            assert resp.status_code == 403, (
                f"Expected 403 but got {resp.status_code}. "
                "demo_admin must not download employee PAN documents."
            )
        finally:
            _teardown_demo_env(env)


def test_demo_admin_cannot_download_aadhaar_document():
    """GAP 5 — GET /employee-documents/<id>/aadhaar must return 403 for demo_admin."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        env["employee"].profile.aadhaar_document_filename = f"aadhaar_{suffix}.png"
        db.session.commit()
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        emp_id = env["employee"].id
        try:
            resp = client.get(f"/employee-documents/{emp_id}/aadhaar")
            assert resp.status_code == 403, (
                f"Expected 403 but got {resp.status_code}. "
                "demo_admin must not download employee Aadhaar documents."
            )
        finally:
            _teardown_demo_env(env)


def test_demo_admin_cannot_download_cancelled_cheque():
    """GAP 5 — GET /employee-documents/<id>/cancelled-cheque must return 403 for demo_admin."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        env["employee"].profile.cancelled_cheque_filename = f"cheque_{suffix}.png"
        db.session.commit()
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        emp_id = env["employee"].id
        try:
            resp = client.get(f"/employee-documents/{emp_id}/cancelled-cheque")
            assert resp.status_code == 403, (
                f"Expected 403 but got {resp.status_code}. "
                "demo_admin must not download employee cancelled-cheque documents."
            )
        finally:
            _teardown_demo_env(env)


def test_demo_admin_cannot_decide_leave():
    """GAP 7 — POST /admin/leaves/<id>/decide must return 403 for demo_admin."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        app_id = env["leave_app"].id
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                f"/admin/leaves/{app_id}/decide",
                data={"decision": "approved", "decision_note": ""},
                follow_redirects=False,
            )
            assert resp.status_code == 403, (
                f"Expected 403 but got {resp.status_code}. "
                "demo_admin must not approve or reject leave requests."
            )
            # Confirm leave is still pending
            refreshed = LeaveApplication.query.get(app_id)
            assert refreshed.status == "pending", (
                "Leave status was changed despite the 403 response"
            )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _teardown_demo_env(env)


def test_demo_admin_cannot_issue_hr_letter():
    """GAP 8 — POST /admin/hr-resources/letters/issue must return 403 for demo_admin."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        # Create a letter template to reference in the POST body.
        tmpl = HRLetterTemplate(
            letter_type=f"offer_{suffix}",
            title="Offer Letter",
            body="Dear {{ employee_name }}, congratulations.",
            hr_only=False,
        )
        db.session.add(tmpl)
        db.session.commit()
        tmpl_id = tmpl.id

        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        emp_id = env["employee"].id
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                "/admin/hr-resources/letters/issue",
                data={
                    "template_id": str(tmpl_id),
                    "employee_id": str(emp_id),
                    "subject": "Test Offer",
                    "details": "Test details",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 403, (
                f"Expected 403 but got {resp.status_code}. "
                "demo_admin must not issue official HR letters."
            )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            db.session.delete(tmpl)
            db.session.commit()
            _teardown_demo_env(env)


def test_demo_admin_edit_employee_does_not_overwrite_pan():
    """
    GAP 6 — POST /admin/employees/<id>/edit must not update PAN or Aadhaar
    fields when submitted by demo_admin.
    """
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        emp = env["employee"]
        emp.profile.pan_number = "ORIGINAL_PAN"
        emp.profile.aadhaar_number = "ORIGINAL_AADHAAR"
        db.session.commit()
        emp_id = emp.id

        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                f"/admin/employees/{emp_id}/edit",
                data={
                    "employee_code": emp.employee_code,
                    "full_name": emp.profile.full_name,
                    "designation": "Engineer",
                    "department": "Tech",
                    "date_of_joining": "2025-01-01",
                    "role": "employee",
                    "pan_number": "ATTACKER_PAN",       # should be ignored
                    "aadhaar_number": "999999999999",   # should be ignored
                },
                follow_redirects=False,
            )
            # Either a redirect (success) or 200 (re-rendered form). Both are
            # acceptable outcomes — what matters is that the PAN was not changed.
            assert resp.status_code in (200, 302, 303), (
                f"Unexpected status {resp.status_code}"
            )
            db.session.expire_all()
            refreshed_profile = EmployeeProfile.query.filter_by(user_id=emp_id).first()
            assert refreshed_profile.pan_number == "ORIGINAL_PAN", (
                "demo_admin managed to overwrite PAN number — fix not applied"
            )
            assert refreshed_profile.aadhaar_number == "ORIGINAL_AADHAAR", (
                "demo_admin managed to overwrite Aadhaar number — fix not applied"
            )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _teardown_demo_env(env)


# ---------------------------------------------------------------------------
# 3. demo_admin DENIED access — platform-level routes (always blocked)
# ---------------------------------------------------------------------------

def test_demo_admin_cannot_access_reports_export():
    """demo_admin cannot access the employee data export endpoint."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        try:
            resp = client.get("/admin/reports/employees/export")
            assert resp.status_code == 403
        finally:
            _teardown_demo_env(env)


def test_demo_admin_cannot_access_leave_type_management():
    """demo_admin cannot manage global leave types."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            assert client.get("/admin/leave-types").status_code == 403
            assert client.post(
                "/admin/leave-types/new",
                data={"name": "Hacked Leave", "annual_quota": "99"},
            ).status_code == 403
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _teardown_demo_env(env)


def test_demo_admin_cannot_access_holidays_management():
    """demo_admin cannot add or delete holidays."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            assert client.get("/admin/holidays").status_code == 403
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _teardown_demo_env(env)


def test_demo_admin_cannot_access_demo_leads():
    """demo_admin cannot view the sales demo leads pipeline."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        try:
            resp = client.get("/admin/demo-leads")
            assert resp.status_code == 403
        finally:
            _teardown_demo_env(env)


def test_demo_admin_cannot_access_payroll_components():
    """demo_admin cannot view or edit global salary components."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        try:
            assert client.get("/admin/payroll").status_code == 403
        finally:
            _teardown_demo_env(env)


def test_demo_admin_cannot_update_hr_letter_template():
    """demo_admin cannot edit HR letter templates."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        tmpl = HRLetterTemplate(
            letter_type=f"exp_{suffix}",
            title="Experience Letter",
            body="Dear {{ employee_name }}.",
            hr_only=False,
        )
        db.session.add(tmpl)
        db.session.commit()
        tmpl_id = tmpl.id

        client = app.test_client()
        _session_as(client, env["demo_admin"].id)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                f"/admin/hr-resources/letter-templates/{tmpl_id}",
                data={"title": "Hacked Title", "body": "Hacked body"},
            )
            assert resp.status_code == 403
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            db.session.delete(tmpl)
            db.session.commit()
            _teardown_demo_env(env)


# ---------------------------------------------------------------------------
# 4. Employee denied admin-only routes (vertical privilege escalation)
# ---------------------------------------------------------------------------

def test_employee_cannot_access_admin_dashboard():
    """A plain employee is denied the admin dashboard."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["employee"].id)
        try:
            resp = client.get("/admin", follow_redirects=False)
            assert resp.status_code in (302, 403), (
                f"Expected redirect or 403, got {resp.status_code}"
            )
        finally:
            _teardown_demo_env(env)


def test_employee_cannot_delete_employee():
    """A plain employee cannot POST to the delete-employee endpoint."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["employee"].id)
        emp_id = env["employee"].id
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                f"/admin/employees/{emp_id}/delete",
                follow_redirects=False,
            )
            assert resp.status_code == 403
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _teardown_demo_env(env)


def test_employee_cannot_process_payroll():
    """A plain employee cannot trigger payroll generation."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["employee"].id)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                "/admin/payroll/generate",
                data={"month": "10", "year": "2026", "process_payroll": "1"},
                follow_redirects=False,
            )
            assert resp.status_code == 403
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _teardown_demo_env(env)


def test_employee_cannot_decide_leave():
    """A plain employee cannot approve or reject a leave request."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        client = app.test_client()
        _session_as(client, env["employee"].id)
        app_id = env["leave_app"].id
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                f"/admin/leaves/{app_id}/decide",
                data={"decision": "approved"},
                follow_redirects=False,
            )
            assert resp.status_code == 403
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _teardown_demo_env(env)


def test_employee_cannot_download_another_employees_pan():
    """A plain employee cannot download another employee's PAN document."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        # Set up a second employee to be the "victim"
        victim = User(
            employee_code=f"VIC{suffix}",
            role="employee",
            tenant_id=env["demo_tenant"].id,
            created_by_id=env["demo_admin"].id,
            must_change_password=False,
        )
        victim.set_password("Victim@123")
        db.session.add(victim)
        db.session.flush()
        db.session.add(EmployeeProfile(
            user_id=victim.id,
            tenant_id=env["demo_tenant"].id,
            full_name="Victim Employee",
            pan_document_filename=f"pan_victim_{suffix}.png",
        ))
        db.session.commit()
        victim_id = victim.id

        client = app.test_client()
        _session_as(client, env["employee"].id)
        try:
            resp = client.get(f"/employee-documents/{victim_id}/pan")
            assert resp.status_code == 403
        finally:
            db.session.delete(victim.profile)
            db.session.delete(victim)
            db.session.commit()
            _teardown_demo_env(env)


# ---------------------------------------------------------------------------
# 5. Manager denied admin-only routes (vertical privilege escalation)
# ---------------------------------------------------------------------------

def test_manager_cannot_access_admin_employee_delete():
    """A manager cannot POST to the delete-employee endpoint."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        # Create a manager in the same tenant
        manager = User(
            employee_code=f"MGR{suffix}",
            role="manager",
            tenant_id=env["demo_tenant"].id,
            created_by_id=env["demo_admin"].id,
            must_change_password=False,
        )
        manager.set_password("Mgr@1234")
        db.session.add(manager)
        db.session.flush()
        db.session.add(EmployeeProfile(
            user_id=manager.id,
            tenant_id=env["demo_tenant"].id,
            full_name="Test Manager",
        ))
        db.session.commit()
        manager_id = manager.id
        emp_id = env["employee"].id

        client = app.test_client()
        _session_as(client, manager_id)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                f"/admin/employees/{emp_id}/delete",
                follow_redirects=False,
            )
            assert resp.status_code == 403
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            db.session.delete(manager.profile)
            db.session.delete(manager)
            db.session.commit()
            _teardown_demo_env(env)


def test_manager_cannot_process_payroll():
    """A manager cannot trigger payroll generation."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        manager = User(
            employee_code=f"MGR{suffix}",
            role="manager",
            tenant_id=env["demo_tenant"].id,
            created_by_id=env["demo_admin"].id,
            must_change_password=False,
        )
        manager.set_password("Mgr@1234")
        db.session.add(manager)
        db.session.flush()
        db.session.add(EmployeeProfile(
            user_id=manager.id,
            tenant_id=env["demo_tenant"].id,
            full_name="Test Manager",
        ))
        db.session.commit()
        manager_id = manager.id

        client = app.test_client()
        _session_as(client, manager_id)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                "/admin/payroll/generate",
                data={"month": "10", "year": "2026", "process_payroll": "1"},
                follow_redirects=False,
            )
            assert resp.status_code == 403
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            db.session.delete(manager.profile)
            db.session.delete(manager)
            db.session.commit()
            _teardown_demo_env(env)


def test_manager_cannot_save_employee_ctc():
    """A manager cannot write CTC records."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        manager = User(
            employee_code=f"MGR{suffix}",
            role="manager",
            tenant_id=env["demo_tenant"].id,
            created_by_id=env["demo_admin"].id,
            must_change_password=False,
        )
        manager.set_password("Mgr@1234")
        db.session.add(manager)
        db.session.flush()
        db.session.add(EmployeeProfile(
            user_id=manager.id,
            tenant_id=env["demo_tenant"].id,
            full_name="Test Manager",
        ))
        db.session.commit()
        manager_id = manager.id
        emp_id = env["employee"].id

        client = app.test_client()
        _session_as(client, manager_id)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                f"/admin/payroll/ctc/{emp_id}",
                data={"monthly_ctc": "99999", "effective_from": "2026-01-01"},
                follow_redirects=False,
            )
            assert resp.status_code == 403
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            db.session.delete(manager.profile)
            db.session.delete(manager)
            db.session.commit()
            _teardown_demo_env(env)


def test_manager_cannot_download_employee_pan():
    """A manager cannot download a team member's PAN document."""
    suffix = token_hex(6).upper()
    with app.app_context():
        env = _build_demo_env(suffix)
        manager = User(
            employee_code=f"MGR{suffix}",
            role="manager",
            tenant_id=env["demo_tenant"].id,
            created_by_id=env["demo_admin"].id,
            must_change_password=False,
        )
        manager.set_password("Mgr@1234")
        db.session.add(manager)
        db.session.flush()
        db.session.add(EmployeeProfile(
            user_id=manager.id,
            tenant_id=env["demo_tenant"].id,
            full_name="Test Manager",
        ))
        # Make the employee report to this manager
        env["employee"].profile.reporting_manager_id = manager.id
        env["employee"].profile.pan_document_filename = f"pan_{suffix}.png"
        db.session.commit()
        manager_id = manager.id
        emp_id = env["employee"].id

        client = app.test_client()
        _session_as(client, manager_id)
        try:
            resp = client.get(f"/employee-documents/{emp_id}/pan")
            assert resp.status_code == 403, (
                f"Expected 403, got {resp.status_code}. "
                "Managers must not download employee PAN documents."
            )
        finally:
            db.session.delete(manager.profile)
            db.session.delete(manager)
            db.session.commit()
            _teardown_demo_env(env)


# ---------------------------------------------------------------------------
# 6. Unauthenticated access (direct URL) to restricted endpoints
# ---------------------------------------------------------------------------

def test_unauthenticated_cannot_access_admin_dashboard():
    """Unauthenticated requests to admin routes are redirected to login."""
    with app.app_context():
        client = app.test_client()
        resp = client.get("/admin", follow_redirects=False)
        assert resp.status_code in (302, 401), (
            f"Expected redirect to login, got {resp.status_code}"
        )


def test_unauthenticated_cannot_delete_employee():
    """Unauthenticated POST to delete-employee is rejected."""
    with app.app_context():
        client = app.test_client()
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post("/admin/employees/999999/delete", follow_redirects=False)
            assert resp.status_code in (302, 401, 403)
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf


def test_unauthenticated_cannot_process_payroll():
    """Unauthenticated POST to generate payroll is rejected."""
    with app.app_context():
        client = app.test_client()
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = client.post(
                "/admin/payroll/generate",
                data={"month": "10", "year": "2026", "process_payroll": "1"},
                follow_redirects=False,
            )
            assert resp.status_code in (302, 401, 403)
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
