"""
Special Approval business logic module.
Handles On-Duty (OD), Work From Home (WFH), Short Leave, and Special Clearance requests.
Automatically approves requests submitted by Founding Members.
"""

from datetime import datetime
from models import db, SpecialApproval, User, EmployeeProfile


def apply_special_approval(user_id, approval_type, start_date, end_date, days, reason):
    """
    Submit a special approval request.
    If the applicant is a Founding Member, auto-approve immediately!
    """
    user = User.query.get(user_id)
    if not user:
        return None, False, "User not found."

    is_founding = user.profile.is_founding_member if user.profile else False

    status = "approved" if is_founding else "pending"
    is_self_approved = is_founding

    approval = SpecialApproval(
        user_id=user_id,
        approver_id=user_id if is_founding else (user.profile.reporting_manager_id if user.profile else None),
        approval_type=approval_type,
        start_date=start_date,
        end_date=end_date,
        days=float(days) if days else 1.0,
        reason=reason,
        status=status,
        is_self_approved=is_self_approved,
        decided_at=datetime.utcnow() if is_founding else None,
        decision_note="Auto self-approved (Founding Member)" if is_founding else None
    )

    db.session.add(approval)
    db.session.commit()

    if is_founding:
        msg = f"Special approval for {approval_type} self-approved automatically (Founding Member status)."
    else:
        msg = f"Special approval request for {approval_type} submitted to your manager."

    return approval, True, msg


def decide_special_approval(approval_id, approver_id, decision, decision_note=None):
    """Approve or reject a pending special approval request."""
    approval = SpecialApproval.query.get(approval_id)
    if not approval:
        return None, False, "Request not found."

    if approval.status != "pending":
        return approval, False, "This request has already been decided."

    approval.status = decision
    approval.approver_id = approver_id
    approval.decided_at = datetime.utcnow()
    approval.decision_note = decision_note

    db.session.commit()
    return approval, True, f"Special approval request {decision} successfully."
