"""
Appraisal and performance review business logic module.
Supports Q1 (Sep-Oct) & Q2 (Mar-Apr) periods, weighted KRAs (max 5, sum 100%),
metric-driven KPIs (max 6 per KRA, sum 100%), 2-stage approval workflow:
1. KRA Definition & Manager KRA Approval (must be approved before ratings can be given).
2. Self-Rating per KRA/KPI section (locked after submission) & Manager Rating Evaluation.
"""

from datetime import datetime
from flask_login import current_user
from models import db, Appraisal, AppraisalKRA, AppraisalKPI, User, EmployeeProfile
from time_utils import now_ist


def create_appraisal(user_id, evaluator_id, period_type, year):
    """Initiate a new appraisal cycle for an employee."""
    if not current_user.is_authenticated or current_user.role not in {"admin", "demo_admin"}:
        return None, False, "You are not authorised to initiate appraisals."
    if current_user.role == "demo_admin":
        target = User.query.get(user_id)
        if not target or target.created_by_id != current_user.id:
            return None, False, "You are not authorised to initiate this appraisal."
    review_period = f"{period_type} {year}"
    existing = Appraisal.query.filter_by(
        user_id=user_id,
        review_period=review_period
    ).first()

    if existing:
        return existing, False, f"Appraisal for period '{review_period}' already exists for this employee."

    previous_appraisals = Appraisal.query.filter(
        Appraisal.user_id == user_id,
        Appraisal.status != "Archived",
    ).all()
    for previous in previous_appraisals:
        previous.status = "Archived"
        previous.updated_at = now_ist()

    target = User.query.get(user_id)
    appraisal = Appraisal(
        tenant_id=target.tenant_id if target else None,
        user_id=user_id,
        evaluator_id=evaluator_id,
        period_type=period_type,
        year=year,
        review_period=review_period,
        status="Initiated"
    )
    db.session.add(appraisal)
    db.session.commit()
    return appraisal, True, f"Appraisal initiated for {review_period}!"


def submit_kras_for_approval(appraisal_id, kras_input_data):
    """
    Save and submit KRAs & KPIs definitions to Manager for KRA approval.
    Enforces: Max 5 KRAs (sum = 100%), Max 6 KPIs per KRA (sum = 100%).
    Status becomes 'KRA Submitted for Approval'.
    """
    appraisal = Appraisal.query.get(appraisal_id)
    if not appraisal:
        return None, False, "Appraisal record not found."
    if not current_user.is_authenticated or current_user.id != appraisal.user_id:
        return None, False, "You are not authorised to submit these KRAs."

    editable_statuses = ("Initiated", "KRA Draft", "KRA Sent Back for Edit")
    if appraisal.status not in editable_statuses:
        return None, False, (
            f"KRAs cannot be edited while the appraisal status is "
            f"'{appraisal.status}'."
        )

    if len(kras_input_data) > 5:
        return None, False, "Maximum 5 KRAs allowed per appraisal."

    try:
        kra_weights = [float(k.get("weightage_percent") or 0.0) for k in kras_input_data]
    except (TypeError, ValueError):
        return None, False, "KRA weightages must be valid numbers."
    if any(weight <= 0 or weight > 100 for weight in kra_weights):
        return None, False, "Each KRA weightage must be greater than 0% and no more than 100%."
    total_kra_weight = sum(kra_weights)
    if abs(total_kra_weight - 100.0) > 0.1:
        return None, False, f"Total weightage across all KRAs must equal 100% (currently {total_kra_weight:.1f}%)."

    for k_data in kras_input_data:
        kpis_data = k_data.get("kpis", [])
        if not kpis_data:
            return None, False, f"KRA '{k_data.get('title', '').strip()}' must have at least one KPI."
        if len(kpis_data) > 6:
            return None, False, f"Maximum 6 KPIs allowed for KRA '{k_data.get('title', '').strip()}'."
        try:
            kpi_weights = [float(kp.get("weightage_percent") or 0.0) for kp in kpis_data]
        except (TypeError, ValueError):
            return None, False, "KPI weightages must be valid numbers."
        if any(weight <= 0 or weight > 100 for weight in kpi_weights):
            return None, False, "Each KPI weightage must be greater than 0% and no more than 100%."
        total_kpi_weight = sum(kpi_weights)
        if abs(total_kpi_weight - 100.0) > 0.1:
            return None, False, f"Total weightage across KPIs in KRA '{k_data.get('title', '').strip()}' must equal 100% (currently {total_kpi_weight:.1f}%)."

    # Clear old KRAs to replace with updated structure
    AppraisalKRA.query.filter_by(appraisal_id=appraisal.id).delete()

    overall_weighted_score = 0.0

    for k_data in kras_input_data:
        kra_weight = float(k_data.get("weightage_percent") or 0.0)
        kra = AppraisalKRA(
            appraisal_id=appraisal.id,
            title=k_data.get("title", "").strip(),
            description=k_data.get("description", "").strip(),
            weightage_percent=kra_weight
        )
        db.session.add(kra)
        db.session.flush()

        kpis_data = k_data.get("kpis", [])

        kra_score = 0.0
        for kp_data in kpis_data:
            target = float(kp_data.get("target_value") or 100.0)
            actual = float(kp_data.get("actual_value") or 0.0)
            kpi_weight = float(kp_data.get("weightage_percent") or 100.0)

            achievement = round((actual / target) * 100.0, 2) if target > 0 else 0.0
            kpi_score = (achievement * (kpi_weight / 100.0))
            kra_score += kpi_score

            kpi = AppraisalKPI(
                kra_id=kra.id,
                title=kp_data.get("title", "").strip(),
                description=kp_data.get("description", "").strip(),
                weightage_percent=kpi_weight,
                metric_formula_desc=kp_data.get("metric_formula_desc", "").strip(),
                target_value=target,
                actual_value=actual,
                unit=kp_data.get("unit", "%").strip(),
                achievement_percent=achievement
            )
            db.session.add(kpi)

        overall_weighted_score += (kra_score * (kra_weight / 100.0))

    appraisal.weighted_score = round(overall_weighted_score, 2)
    appraisal.status = "KRA Submitted for Approval"
    appraisal.sendback_note = None
    appraisal.updated_at = now_ist()

    db.session.commit()
    return appraisal, True, "KRAs submitted to Manager for approval!"


def decide_kras_approval(appraisal_id, evaluator_id, decision, sendback_note=None):
    """
    Manager approves KRAs or sends back to employee for revision.
    Approval unlocks the Rating Phase ('KRA Approved (Eligible for Rating)').
    """
    appraisal = Appraisal.query.get(appraisal_id)
    if not appraisal:
        return None, False, "Appraisal record not found."
    if not current_user.is_authenticated or current_user.id != evaluator_id:
        return None, False, "You are not authorised to decide these KRAs."
    if current_user.role == "manager":
        if (not appraisal.user.profile
                or appraisal.tenant_id != current_user.tenant_id
                or appraisal.user.tenant_id != current_user.tenant_id
                or appraisal.user.profile.reporting_manager_id != current_user.id):
            return None, False, "You are not authorised to decide these KRAs."
    elif current_user.role == "demo_admin":
        if appraisal.tenant_id != current_user.tenant_id:
            return None, False, "You are not authorised to decide these KRAs."
    elif current_user.role != "admin":
        return None, False, "You are not authorised to decide these KRAs."

    if appraisal.status != "KRA Submitted for Approval":
        return None, False, (
            f"There are no KRAs pending approval for this appraisal "
            f"(current status: '{appraisal.status}')."
        )

    appraisal.evaluator_id = evaluator_id
    appraisal.updated_at = now_ist()

    if decision == "approve":
        appraisal.status = "KRA Approved (Eligible for Rating)"
        appraisal.sendback_note = None
        msg = "KRAs approved! Employee can now submit ratings."
    else:
        appraisal.status = "KRA Sent Back for Edit"
        appraisal.sendback_note = sendback_note or "KRAs sent back for modification."
        msg = "KRAs sent back to employee for revision."

    db.session.commit()
    return appraisal, True, msg


def submit_self_ratings(appraisal_id, self_rating, self_comments, kra_ratings=None, kpi_ratings=None):
    """
    Employee submits self-ratings for each KRA section, KPI, and overall.
    Allowed only after Manager approves KRAs!
    Status becomes 'Self Review Submitted'.
    """
    appraisal = Appraisal.query.get(appraisal_id)
    if not appraisal:
        return None, False, "Appraisal record not found."
    if not current_user.is_authenticated or current_user.id != appraisal.user_id:
        return None, False, "You are not authorised to submit these ratings."

    if appraisal.status not in ["KRA Approved (Eligible for Rating)", "Rating Sent Back for Edit"]:
        return None, False, "Ratings can only be submitted after KRAs are approved by your Manager."

    def valid_rating(value):
        try:
            return float(value) in {1.0, 2.0, 3.0, 4.0, 5.0}
        except (TypeError, ValueError):
            return False

    if not valid_rating(self_rating):
        return None, False, "Self-rating must be between 1 and 5."
    for value in (kra_ratings or {}).values():
        if not valid_rating(value):
            return None, False, "KRA self-ratings must be between 1 and 5."
    for value in (kpi_ratings or {}).values():
        if not valid_rating(value):
            return None, False, "KPI self-ratings must be between 1 and 5."

    appraisal.self_rating = float(self_rating) if self_rating else None
    appraisal.self_comments = self_comments
    appraisal.status = "Self Review Submitted"
    appraisal.sendback_note = None
    appraisal.updated_at = now_ist()

    if kra_ratings:
        for kra in appraisal.kras:
            if str(kra.id) in kra_ratings:
                kra.self_rating = float(kra_ratings[str(kra.id)])

    if kpi_ratings:
        for kra in appraisal.kras:
            for kpi in kra.kpis:
                if str(kpi.id) in kpi_ratings:
                    kpi.self_rating = float(kpi_ratings[str(kpi.id)])

    db.session.commit()
    return appraisal, True, "Self-ratings submitted to Manager for evaluation!"


def submit_manager_ratings(appraisal_id, evaluator_id, rating, evaluator_comments, decision="complete", sendback_note=None, kra_ratings=None, kpi_ratings=None):
    """
    Manager reviews ratings, gives Manager ratings for each section, and finalizes or sends back.
    """
    appraisal = Appraisal.query.get(appraisal_id)
    if not appraisal:
        return None, False, "Appraisal record not found."
    if not current_user.is_authenticated or current_user.id != evaluator_id:
        return None, False, "You are not authorised to submit manager ratings."

    def valid_rating(value):
        try:
            return float(value) in {1.0, 2.0, 3.0, 4.0, 5.0}
        except (TypeError, ValueError):
            return False

    if decision == "complete" and not valid_rating(rating):
        return None, False, "Manager rating must be between 1 and 5."
    for value in (kra_ratings or {}).values():
        if not valid_rating(value):
            return None, False, "KRA ratings must be between 1 and 5."
    for value in (kpi_ratings or {}).values():
        if not valid_rating(value):
            return None, False, "KPI ratings must be between 1 and 5."
    if current_user.role == "manager":
        if (not appraisal.user.profile
                or appraisal.tenant_id != current_user.tenant_id
                or appraisal.user.tenant_id != current_user.tenant_id
                or appraisal.user.profile.reporting_manager_id != current_user.id):
            return None, False, "You are not authorised to submit manager ratings."
    elif current_user.role == "demo_admin":
        if appraisal.tenant_id != current_user.tenant_id:
            return None, False, "You are not authorised to submit manager ratings."
    elif current_user.role != "admin":
        return None, False, "You are not authorised to submit manager ratings."

    if appraisal.status != "Self Review Submitted":
        return None, False, (
            f"Manager ratings can only be submitted after the employee's "
            f"self-review (current status: '{appraisal.status}')."
        )

    appraisal.evaluator_id = evaluator_id
    appraisal.updated_at = now_ist()

    if decision == "complete":
        appraisal.rating = float(rating) if rating else None
        appraisal.evaluator_comments = evaluator_comments
        appraisal.status = "Completed"
        appraisal.sendback_note = None

        if kra_ratings:
            for kra in appraisal.kras:
                if str(kra.id) in kra_ratings:
                    kra.manager_rating = float(kra_ratings[str(kra.id)])

        if kpi_ratings:
            for kra in appraisal.kras:
                for kpi in kra.kpis:
                    if str(kpi.id) in kpi_ratings:
                        kpi.manager_rating = float(kpi_ratings[str(kpi.id)])

        msg = "Appraisal completed and finalized!"
    else:
        appraisal.status = "Rating Sent Back for Edit"
        appraisal.sendback_note = sendback_note or "Self-ratings sent back for modification."
        msg = "Self-ratings sent back to employee for modification."

    db.session.commit()
    return appraisal, True, msg
