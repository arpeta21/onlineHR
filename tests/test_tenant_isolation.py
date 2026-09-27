from datetime import date, datetime
from secrets import token_hex

from app import app
from attendance_logic import decide_attendance_regularization
from flask_login import login_user
from models import (
    Candidate,
    CandidateInterview,
    EmployeeProfile,
    Appraisal,
    AttendanceRegularization,
    JobRequisition,
    ResourceDocument,
    SpecialApproval,
    Tenant,
    User,
    db,
)


def _session_as(client, user_id):
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def test_demo_admin_cannot_access_cross_tenant_resource_candidate_or_offer():
    suffix = token_hex(6).upper()
    with app.app_context():
        tenant_a = Tenant(company_name=f"Audit A {suffix}", tenant_type="demo")
        tenant_b = Tenant(company_name=f"Audit B {suffix}", tenant_type="demo")
        db.session.add_all([tenant_a, tenant_b])
        db.session.flush()

        demo_admin = User(employee_code=f"DA{suffix}", role="demo_admin", tenant_id=tenant_a.id, must_change_password=False)
        demo_admin.set_password("Demo@123")
        db.session.add(demo_admin)
        db.session.flush()
        db.session.add(EmployeeProfile(
            user_id=demo_admin.id, tenant_id=tenant_a.id, full_name="Audit Demo Admin"
        ))

        manager_a = User(employee_code=f"MA{suffix}", role="manager", tenant_id=tenant_a.id, must_change_password=False)
        manager_a.set_password("Manager@123")
        db.session.add(manager_a)
        db.session.flush()
        db.session.add(EmployeeProfile(
            user_id=manager_a.id, tenant_id=tenant_a.id, full_name="Tenant A Manager"
        ))

        manager_b = User(employee_code=f"MB{suffix}", role="manager", tenant_id=tenant_b.id, must_change_password=False)
        manager_b.set_password("Manager@123")
        db.session.add(manager_b)
        db.session.flush()
        db.session.add(EmployeeProfile(
            user_id=manager_b.id, tenant_id=tenant_b.id, full_name="Tenant B Manager"
        ))

        employee_b = User(employee_code=f"EB{suffix}", role="employee", tenant_id=tenant_b.id, must_change_password=False)
        employee_b.set_password("Employee@123")
        db.session.add(employee_b)
        db.session.flush()
        db.session.add(EmployeeProfile(
            user_id=employee_b.id, tenant_id=tenant_b.id, full_name="Tenant B Employee",
            reporting_manager_id=manager_b.id,
        ))

        requisition_b = JobRequisition(
            tenant_id=tenant_b.id, title="Tenant B Role", department="Engineering",
            number_of_positions=1, key_skills="Python", requested_by_id=manager_b.id,
            status="Approved", is_manager_approved=True, is_admin_approved=True,
        )
        db.session.add(requisition_b)
        db.session.flush()
        candidate_b = Candidate(
            tenant_id=tenant_b.id, requisition_id=requisition_b.id,
            full_name="Tenant B Candidate", email="candidate-b@example.test",
            status="Offered", offered_ctc=10.0,
        )
        resource_b = ResourceDocument(
            tenant_id=tenant_b.id, title="Tenant B Policy", category="Policy",
            description="Private tenant policy", filename="tenant_b_policy.txt",
            uploaded_by_id=manager_b.id, is_published=True,
        )
        db.session.add_all([candidate_b, resource_b])
        appraisal_b = Appraisal(tenant_id=tenant_b.id, user_id=employee_b.id, evaluator_id=manager_b.id,
                                period_type="Q1 (Sep-Oct)", year=2026, review_period="Q1 (Sep-Oct) 2026")
        regularization_b = AttendanceRegularization(
            tenant_id=tenant_b.id, user_id=employee_b.id, approver_id=manager_b.id,
            date=date.today(), reason="Cross-tenant test",
        )
        special_b = SpecialApproval(
            tenant_id=tenant_b.id, user_id=employee_b.id, approver_id=manager_b.id,
            approval_type="WFH", start_date=date.today(), end_date=date.today(), reason="Cross-tenant test",
        )
        interview_b = CandidateInterview(
            tenant_id=tenant_b.id, candidate=candidate_b, interviewer_id=manager_b.id,
            scheduled_time=datetime.utcnow(), round_name="Test Round",
        )
        db.session.add_all([appraisal_b, regularization_b, special_b, interview_b])
        db.session.commit()
        candidate_id = candidate_b.id
        resource_id = resource_b.id
        demo_admin_id = demo_admin.id
        appraisal_id = appraisal_b.id
        regularization_id = regularization_b.id
        special_id = special_b.id
        interview_id = interview_b.id

        client = app.test_client()
        _session_as(client, demo_admin_id)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            assert client.get(f"/hr-resources/resources/{resource_id}/download").status_code == 403
            assert client.get(f"/recruitment/candidates/{candidate_id}/edit").status_code == 403
            assert client.get(f"/recruitment/candidates/{candidate_id}/offer").status_code == 403
            assert client.get(f"/appraisals/{appraisal_id}").status_code == 403
            assert client.get("/admin/reports/employees/export").status_code == 403

            manager_client = app.test_client()
            login_response = manager_client.post("/login", data={
                "employee_code": manager_a.employee_code,
                "password": "Manager@123",
            }, follow_redirects=False)
            assert login_response.status_code == 302
            assert manager_client.get(f"/recruitment/candidates/{candidate_id}/edit").status_code == 403
            assert manager_client.get(f"/recruitment/candidates/{candidate_id}/offer").status_code == 403
            feedback_response = manager_client.post(f"/recruitment/interviews/{interview_id}/feedback", data={
                "manager_rating": "4", "manager_feedback": "test", "decision": "Reject",
            })
            assert feedback_response.status_code == 403
            with app.test_request_context("/regularizations/decide"):
                login_user(manager_a)
                _, regularization_success, _ = decide_attendance_regularization(
                    regularization_id, manager_a.id, "approved"
                )
            assert regularization_success is False
            assert manager_client.post(f"/special-approvals/{special_id}/decide", data={"decision": "approve"}).status_code == 403
            assert manager_client.post(f"/appraisals/{appraisal_id}", data={"action": "manager_review", "rating": "4"}).status_code == 403
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            db.session.delete(resource_b)
            db.session.delete(interview_b)
            db.session.delete(special_b)
            db.session.delete(regularization_b)
            db.session.delete(appraisal_b)
            db.session.delete(candidate_b)
            db.session.delete(requisition_b)
            db.session.delete(employee_b.profile)
            db.session.delete(employee_b)
            db.session.delete(manager_b.profile)
            db.session.delete(manager_b)
            db.session.delete(manager_a.profile)
            db.session.delete(manager_a)
            db.session.delete(demo_admin.profile)
            db.session.delete(demo_admin)
            db.session.delete(tenant_a)
            db.session.delete(tenant_b)
            db.session.commit()
