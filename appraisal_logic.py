"""
Appraisal and performance review business logic module.
Supports Q1 (Sep-Oct) & Q2 (Mar-Apr) periods, weighted KRAs (max 5, sum 100%),
metric-driven KPIs (max 6 per KRA, sum 100%), 2-stage approval workflow:
1. KRA Definition & Manager KRA Approval (must be approved before ratings can be given).
2. Self-Rating per KRA/KPI section (locked after submission) & Manager Rating Evaluation.
"""

from datetime import datetime
from models import db, Appraisal, AppraisalKRA, AppraisalKPI, User, EmployeeProfile


def create_appraisal(user_id, evaluator_id, period_type, year):
    """Initiate a new appraisal cycle for an employee."""
    review_period = f"{period_type} {year}"
    existing = Appraisal.query.filter_by(
        user_id=user_id,
        review_period=review_period
    ).first()

    if existing:
        return existing, False, f"Appraisal for period '{review_period}' already exists for this employee."

    appraisal = Appraisal(
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

    if len(kras_input_data) > 5:
        return None, False, "Maximum 5 KRAs allowed per appraisal."

    total_kra_weight = sum(float(k.get("weightage_percent") or 0.0) for k in kras_input_data)
    if abs(total_kra_weight - 100.0) > 0.1:
        return None, False, f"Total weightage across all KRAs must equal 100% (currently {total_kra_weight:.1f}%)."

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
        if len(kpis_data) > 6:
            return None, False, f"Maximum 6 KPIs allowed for KRA '{kra.title}'."

        total_kpi_weight = sum(float(kp.get("weightage_percent") or 0.0) for kp in kpis_data)
        if kpis_data and abs(total_kpi_weight - 100.0) > 0.1:
            return None, False, f"Total weightage across KPIs in KRA '{kra.title}' must equal 100% (currently {total_kpi_weight:.1f}%)."

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
    appraisal.updated_at = datetime.utcnow()

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

    appraisal.evaluator_id = evaluator_id
    appraisal.updated_at = datetime.utcnow()

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

    if appraisal.status not in ["KRA Approved (Eligible for Rating)", "Rating Sent Back for Edit"]:
        return None, False, "Ratings can only be submitted after KRAs are approved by your Manager."

    appraisal.self_rating = float(self_rating) if self_rating else None
    appraisal.self_comments = self_comments
    appraisal.status = "Self Review Submitted"
    appraisal.sendback_note = None
    appraisal.updated_at = datetime.utcnow()

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

    appraisal.evaluator_id = evaluator_id
    appraisal.updated_at = datetime.utcnow()

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
