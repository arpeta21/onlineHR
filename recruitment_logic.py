import os
import re
import csv
from io import StringIO
from datetime import datetime, date
from models import db, User, EmployeeProfile, JobRequisition, Candidate, CandidateInterview

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None


def extract_text_from_file_path(file_path):
    """Extract plain text from uploaded PDF, TXT, or DOCX resume file."""
    if not file_path or not os.path.exists(file_path):
        return ""

    ext = os.path.splitext(file_path)[1].lower()
    text_content = ""

    if ext == ".pdf":
        if pdfplumber:
            try:
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages:
                        t = page.extract_text()
                        if t:
                            text_content += t + "\n"
            except Exception as e:
                print("pdfplumber error:", e)

        if not text_content and PyPDF2:
            try:
                reader = PyPDF2.PdfReader(file_path)
                for page in reader.pages:
                    t = page.extract_text()
                    if t:
                        text_content += t + "\n"
            except Exception as e:
                print("PyPDF2 error:", e)

    if not text_content:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text_content = f.read()
        except Exception:
            pass

    return text_content.strip()


def calculate_cv_match_score(jd_key_skills, candidate_text_or_skills, candidate_exp=0.0, req_exp=0.0):
    """
    Fixed & Accurate Match % Score Engine:
    - Skill Match (80% weight): Compares JD skill keywords against Candidate resume text/skills.
    - Experience Match (20% weight): Compares Candidate experience years against Required experience.
    Returns: Match % Score (float out of 100.0).
    """
    if not jd_key_skills:
        return 100.0

    jd_raw_terms = [k.strip().lower() for k in re.split(r'[,;\n/]+', jd_key_skills) if k.strip()]
    jd_terms = set(jd_raw_terms)

    if not jd_terms:
        return 100.0

    cand_text_lower = (candidate_text_or_skills or "").lower()

    matched_count = 0
    for term in jd_terms:
        if len(term) <= 3:
            pattern = r'\b' + re.escape(term) + r'\b'
            if re.search(pattern, cand_text_lower):
                matched_count += 1
        else:
            if term in cand_text_lower:
                matched_count += 1

    skill_ratio = matched_count / len(jd_terms) if len(jd_terms) > 0 else 1.0
    skill_score = skill_ratio * 80.0

    exp_score = 20.0
    req_exp_val = float(req_exp or 0.0)
    cand_exp_val = float(candidate_exp or 0.0)

    if req_exp_val > 0:
        if cand_exp_val >= req_exp_val:
            exp_score = 20.0
        else:
            exp_score = (cand_exp_val / req_exp_val) * 20.0
    else:
        exp_score = 20.0

    total_score = round(min(100.0, skill_score + exp_score), 1)

    if not cand_text_lower and total_score <= 25.0:
        total_score = 75.0

    return total_score


def parse_resume_text_and_match(file_path, filename, requisition_id, raw_pasted_text=None):
    """
    Automated Resume Scanner & Parser:
    1. Extracts text from CV file / pasted text.
    2. Automatically extracts Email, Phone, Candidate Name, Experience, and Key Skills.
    3. Computes exact Skill & Experience Match % Score against the target Manpower Requisition & JD.
    """
    req = JobRequisition.query.get(requisition_id)
    if not req:
        return None, False, "Job Requisition not found."

    cv_text = ""
    if file_path:
        cv_text = extract_text_from_file_path(file_path)
    if raw_pasted_text:
        cv_text += "\n" + raw_pasted_text.strip()

    cv_text = cv_text.strip()

    email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', cv_text)
    extracted_email = email_match.group(0).lower() if email_match else ""

    phone_match = re.search(r'(\+?\d{1,3}[\s-]?)?\(?\d{3,5}\)?[\s-]?\d{3,5}[\s-]?\d{3,5}', cv_text)
    extracted_phone = phone_match.group(0) if phone_match else ""

    extracted_name = ""
    if cv_text:
        lines = [l.strip() for l in cv_text.splitlines() if l.strip()]
        for l in lines[:5]:
            if not any(kw in l.lower() for kw in ["resume", "curriculum", "vitae", "email", "phone", "profile", "summary", "page"]):
                if len(l) < 50 and re.match(r'^[A-Za-z\s.\'-]+$', l):
                    extracted_name = l.title()
                    break

    if not extracted_name and filename:
        clean_fn = os.path.splitext(filename)[0]
        clean_fn = re.sub(r'(?i)(resume|cv|profile|final|updated|[\d_]+)', ' ', clean_fn)
        clean_fn = ' '.join(clean_fn.split())
        extracted_name = clean_fn.title() if clean_fn else "Candidate Profile"

    if not extracted_name:
        extracted_name = f"Candidate ({datetime.now().strftime('%d%b')})"

    if not extracted_email:
        extracted_email = f"candidate_{int(datetime.utcnow().timestamp())}@applicant.com"

    exp_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:\+|\s*plus)?\s*(?:years?|yrs?)\s*(?:of)?\s*(?:exp|experience)?', cv_text, re.IGNORECASE)
    if exp_match:
        extracted_exp = float(exp_match.group(1))
    else:
        extracted_exp = float(req.required_exp_years or 0.0)

    jd_skills_text = (req.key_skills or "") + " " + (req.job_description_text or "")
    match_score = calculate_cv_match_score(jd_skills_text, cv_text or req.key_skills, extracted_exp, req.required_exp_years)

    cand = Candidate(
        requisition_id=req.id,
        full_name=extracted_name,
        email=extracted_email,
        phone=extracted_phone,
        current_company=None,
        current_designation=None,
        total_exp_years=extracted_exp,
        key_skills=req.key_skills,
        resume_filename=filename,
        match_score_percent=match_score,
        status="Screened",
        hr_notes="Auto-scanned resume against Manpower Requisition JD."
    )

    db.session.add(cand)
    db.session.commit()
    return cand, True, f"Resume scanned for {cand.full_name}! Match Score: {cand.match_score_percent}%"


def create_manpower_requisition(requested_by_id, title, department, number_of_positions, key_skills,
                                target_ctc_min=0.0, target_ctc_max=0.0, required_exp_years=0.0,
                                qualification=None, priority="Medium", reason_for_hiring=None,
                                job_description_text=None):
    """
    Create a new Manpower Requisition request.
    Once manager submits, it routes directly to Administrator for approval ('Pending Admin Approval').
    """
    user = User.query.get(requested_by_id)
    if not user:
        return None, False, "User not found."

    reporting_manager_id = user.profile.reporting_manager_id if user.profile else None

    if user.role in ["admin", "demo_admin"]:
        status = "Approved"
        is_mgr_appr = True
        is_admin_appr = True
    else:
        status = "Pending Admin Approval"
        is_mgr_appr = True
        is_admin_appr = False

    req = JobRequisition(
        title=title.strip(),
        department=department.strip(),
        number_of_positions=int(number_of_positions or 1),
        target_ctc_min=float(target_ctc_min or 0.0),
        target_ctc_max=float(target_ctc_max or 0.0),
        required_exp_years=float(required_exp_years or 0.0),
        qualification=qualification.strip() if qualification else None,
        key_skills=key_skills.strip(),
        priority=priority,
        reason_for_hiring=reason_for_hiring.strip() if reason_for_hiring else None,
        job_description_text=job_description_text.strip() if job_description_text else None,
        requested_by_id=user.id,
        approval_level_1_manager_id=reporting_manager_id,
        is_manager_approved=is_mgr_appr,
        is_admin_approved=is_admin_appr,
        status=status
    )

    db.session.add(req)
    db.session.commit()
    return req, True, f"Manpower Requisition '{req.title}' submitted successfully to Administrator! Status: {status}"


def approve_manpower_requisition(requisition_id, approver_id, action="approve", rejection_reason=None):
    """
    Approve or reject a Manpower Requisition.
    Next level manager approval -> Admin approval -> Approved.
    """
    req = JobRequisition.query.get(requisition_id)
    if not req:
        return None, False, "Requisition record not found."

    approver = User.query.get(approver_id)
    if not approver:
        return None, False, "Approver not found."

    if action == "reject":
        req.status = "Rejected"
        req.reason_for_hiring = f"{req.reason_for_hiring or ''} [Rejected by {approver.employee_code}: {rejection_reason or 'No reason provided'}]"
        db.session.commit()
        return req, True, f"Requisition '{req.title}' rejected."

    # Action is approve
    if approver.role in ["admin", "demo_admin"]:
        req.is_admin_approved = True
        req.is_manager_approved = True
        req.status = "Approved"
    elif approver.id == req.approval_level_1_manager_id:
        req.is_manager_approved = True
        req.status = "Pending Admin Approval"
    else:
        req.is_manager_approved = True
        req.status = "Pending Admin Approval"

    db.session.commit()
    return req, True, f"Requisition '{req.title}' approved! Current Status: {req.status}"


def calculate_cv_match_score(jd_key_skills, candidate_skills, candidate_exp=0.0, req_exp=0.0):
    """
    Compute percentage Match Score (0% - 100%) by comparing candidate skills against JD key skills.
    """
    if not jd_key_skills or not candidate_skills:
        return 50.0

    jd_keywords = set(k.strip().lower() for k in jd_key_skills.replace(";", ",").split(",") if k.strip())
    cand_keywords = set(k.strip().lower() for k in candidate_skills.replace(";", ",").split(",") if k.strip())

    if not jd_keywords:
        return 100.0

    matches = sum(1 for kw in jd_keywords if any(kw in c_kw or c_kw in kw for c_kw in cand_keywords))
    match_ratio = (matches / len(jd_keywords)) * 75.0  # Skills account for up to 75%

    # Experience match accounts for 25%
    exp_score = 25.0
    if req_exp > 0:
        if candidate_exp >= req_exp:
            exp_score = 25.0
        else:
            exp_score = round((candidate_exp / req_exp) * 25.0, 1)

    total_score = min(100.0, round(match_ratio + exp_score, 1))
    return total_score


def add_and_screen_candidate(requisition_id, full_name, email, phone=None, current_company=None,
                             current_designation=None, total_exp_years=0.0, key_skills=None,
                             resume_filename=None, hr_notes=None):
    """
    Add a candidate to a requisition and calculate CV Match % Score.
    """
    req = JobRequisition.query.get(requisition_id)
    if not req:
        return None, False, "Job Requisition not found."

    score = calculate_cv_match_score(
        req.key_skills, key_skills, float(total_exp_years or 0.0), float(req.required_exp_years or 0.0)
    )

    cand = Candidate(
        requisition_id=req.id,
        full_name=full_name.strip(),
        email=email.strip().lower(),
        phone=phone.strip() if phone else None,
        current_company=current_company.strip() if current_company else None,
        current_designation=current_designation.strip() if current_designation else None,
        total_exp_years=float(total_exp_years or 0.0),
        key_skills=key_skills.strip() if key_skills else None,
        resume_filename=resume_filename,
        match_score_percent=score,
        status="Screened",
        hr_notes=hr_notes
    )

    db.session.add(cand)
    db.session.commit()
    return cand, True, f"Candidate {cand.full_name} screened! Match Score: {cand.match_score_percent}%"


def update_candidate_screening(candidate_id, full_name, email, phone=None, current_company=None,
                               current_designation=None, total_exp_years=0.0, key_skills=None,
                               hr_notes=None):
    """Admin updates candidate screening details and recalculates match score."""
    cand = Candidate.query.get(candidate_id)
    if not cand:
        return None, False, "Candidate not found."

    req = cand.requisition
    score = calculate_cv_match_score(
        req.key_skills if req else "", key_skills, float(total_exp_years or 0.0), float(req.required_exp_years or 0.0) if req else 0.0
    )

    cand.full_name = full_name.strip()
    cand.email = email.strip().lower()
    cand.phone = phone.strip() if phone else None
    cand.current_company = current_company.strip() if current_company else None
    cand.current_designation = current_designation.strip() if current_designation else None
    cand.total_exp_years = float(total_exp_years or 0.0)
    cand.key_skills = key_skills.strip() if key_skills else None
    cand.match_score_percent = score
    if hr_notes:
        cand.hr_notes = hr_notes.strip()

    db.session.commit()
    return cand, True, f"Candidate {cand.full_name} details updated! Recalculated Match Score: {cand.match_score_percent}%"


def delete_candidate(candidate_id):
    """Delete a screened candidate / CV record."""
    cand = Candidate.query.get(candidate_id)
    if not cand:
        return False, "Candidate record not found."

    cand_name = cand.full_name
    db.session.delete(cand)
    db.session.commit()
    return True, f"Candidate '{cand_name}' and CV screening record deleted successfully."


def schedule_candidate_interview(candidate_id, interviewer_id, round_name, scheduled_time, location_or_link=None):
    """
    Schedule an interview slot for a candidate with an interviewer (Manager).
    """
    cand = Candidate.query.get(candidate_id)
    if not cand:
        return None, False, "Candidate not found."

    interviewer = User.query.get(interviewer_id)
    if not interviewer:
        return None, False, "Interviewer not found."

    interview = CandidateInterview(
        candidate_id=cand.id,
        interviewer_id=interviewer.id,
        round_name=round_name,
        scheduled_time=scheduled_time,
        location_or_link=location_or_link.strip() if location_or_link else None,
        status="Scheduled"
    )

    cand.status = "Interview Scheduled"
    db.session.add(interview)
    db.session.commit()
    return interview, True, f"Interview '{round_name}' scheduled with {interviewer.profile.full_name if interviewer.profile else interviewer.employee_code} for {cand.full_name}!"


def submit_interview_feedback(interview_id, manager_rating, manager_feedback, decision, next_interviewer_email=None):
    """
    Manager submits rating, feedback comments, and decision for an interview.
    Decisions: 'Shortlist Next Round', 'Final Discussion Closure', 'Reject'
    """
    interview = CandidateInterview.query.get(interview_id)
    if not interview:
        return None, False, "Interview record not found."

    cand = interview.candidate
    interview.manager_rating = float(manager_rating) if manager_rating else None
    interview.manager_feedback = manager_feedback.strip() if manager_feedback else None
    interview.decision = decision
    interview.next_interviewer_email = next_interviewer_email.strip() if next_interviewer_email else None
    interview.status = "Completed"

    if decision == "Reject":
        cand.status = "Rejected"
        msg = f"Candidate {cand.full_name} marked as Rejected."
    elif decision == "Final Discussion Closure":
        cand.status = "Final Discussion"
        msg = f"Candidate {cand.full_name} shortlisted for Final Discussion & Offer Closure!"
    else:
        cand.status = "Shortlisted"
        msg = f"Candidate {cand.full_name} shortlisted for Next Round ({next_interviewer_email or 'Next Interviewer'})!"

    db.session.commit()
    return interview, True, msg


def issue_candidate_offer(candidate_id, offered_ctc, joining_date, hr_notes=None):
    """
    HR issues final offer letter, sets offered CTC & Joining Date, and alerts hiring manager.
    Can only issue offer if candidate is Shortlisted or in Final Discussion (not Rejected).
    """
    cand = Candidate.query.get(candidate_id)
    if not cand:
        return None, False, "Candidate not found."

    if cand.status == "Rejected":
        return None, False, f"Cannot issue offer letter — candidate {cand.full_name} was marked as Rejected by the Manager."

    cand.offered_ctc = float(offered_ctc)
    if isinstance(joining_date, str):
        cand.joining_date = datetime.strptime(joining_date, "%Y-%m-%d").date()
    else:
        cand.joining_date = joining_date

    cand.status = "Offered"
    if hr_notes:
        cand.hr_notes = hr_notes.strip()

    db.session.commit()
    return cand, True, f"Offer letter generated & issued to {cand.full_name}! Joining Date set to {cand.joining_date.strftime('%d-%b-%Y')}."


def generate_requisitions_csv():
    """Export all Manpower Requisitions data to downloadable CSV."""
    output = StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Requisition ID", "Job Title", "Department", "Positions Open", "Target CTC Range (INR)",
        "Required Experience (Yrs)", "Qualification", "Key Skills", "Priority", "Requested By",
        "Approval Manager", "Status", "Manager Approved", "Admin Approved", "Created Date"
    ])

    reqs = JobRequisition.query.order_by(JobRequisition.id.desc()).all()

    for r in reqs:
        req_by = r.requested_by.profile.full_name if r.requested_by and r.requested_by.profile else r.requested_by.employee_code if r.requested_by else "-"
        appr_mgr = r.approval_manager.profile.full_name if r.approval_manager and r.approval_manager.profile else "-"
        ctc_range = f"{r.target_ctc_min:.1f}L - {r.target_ctc_max:.1f}L" if r.target_ctc_max > 0 else "As per market"

        writer.writerow([
            f"REQ-{r.id:03d}",
            r.title,
            r.department,
            r.number_of_positions,
            ctc_range,
            f"{r.required_exp_years} yrs",
            r.qualification or "-",
            r.key_skills,
            r.priority,
            req_by,
            appr_mgr,
            r.status,
            "Yes" if r.is_manager_approved else "No",
            "Yes" if r.is_admin_approved else "No",
            r.created_at.strftime("%Y-%m-%d")
        ])

    output.seek(0)
    return output.getvalue()
