from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(db.Model, UserMixin):
    """
    Login account. Every person who can log in (admin / manager / employee)
    has exactly one row here. role decides what they can see.
    """
    id = db.Column(db.Integer, primary_key=True)
    employee_code = db.Column(db.String(20), unique=True, nullable=False)  # e.g. EMP1001
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="employee")  # admin / manager / employee
    must_change_password = db.Column(db.Boolean, default=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    profile = db.relationship("EmployeeProfile", backref="user", uselist=False,
                               cascade="all, delete-orphan",
                               foreign_keys="EmployeeProfile.user_id")
    created_by = db.relationship("User", remote_side="User.id", foreign_keys=[created_by_id], backref="created_users")

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)


class EmployeeProfile(db.Model):
    """
    Everything about a person. Split conceptually into:
      - Admin-fed fields (locked to the employee): name, code, DOJ, manager, designation, dept
      - Self-service fields the employee fills in once they log in.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)

    # ---- Set by Admin at creation time (read-only to employee) ----
    full_name = db.Column(db.String(150), nullable=False)
    designation = db.Column(db.String(100))
    department = db.Column(db.String(100))
    date_of_joining = db.Column(db.Date)
    reporting_manager_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    is_founding_member = db.Column(db.Boolean, default=False)
    monthly_ctc = db.Column(db.Float, default=0.0)

    # ---- Personal details (filled by employee) ----
    date_of_birth = db.Column(db.Date)
    gender = db.Column(db.String(20))
    personal_email = db.Column(db.String(120))
    phone_number = db.Column(db.String(20))
    blood_group = db.Column(db.String(10))

    # ---- Identity documents ----
    pan_number = db.Column(db.String(20))
    aadhaar_number = db.Column(db.String(20))

    # ---- Address ----
    current_address_line1 = db.Column(db.String(200))
    current_address_line2 = db.Column(db.String(200))
    current_city = db.Column(db.String(100))
    current_state = db.Column(db.String(100))
    current_pincode = db.Column(db.String(10))

    permanent_address_line1 = db.Column(db.String(200))
    permanent_address_line2 = db.Column(db.String(200))
    permanent_city = db.Column(db.String(100))
    permanent_state = db.Column(db.String(100))
    permanent_pincode = db.Column(db.String(10))
    same_as_current = db.Column(db.Boolean, default=False)

    # ---- Bank details ----
    bank_account_number = db.Column(db.String(40))
    bank_ifsc_code = db.Column(db.String(20))
    bank_name = db.Column(db.String(100))
    bank_branch = db.Column(db.String(100))

    # ---- Previous employment ----
    previous_company_name = db.Column(db.String(150))
    previous_designation = db.Column(db.String(100))
    previous_employment_from = db.Column(db.Date)
    previous_employment_to = db.Column(db.Date)
    relieving_letter_filename = db.Column(db.String(255))

    # ---- Emergency / family ----
    emergency_contact_name = db.Column(db.String(150))
    emergency_contact_relation = db.Column(db.String(50))
    emergency_contact_phone = db.Column(db.String(20))

    spouse_name = db.Column(db.String(150))
    spouse_dob = db.Column(db.Date)

    profile_completed = db.Column(db.Boolean, default=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    reporting_manager = db.relationship("User", foreign_keys=[reporting_manager_id])
    children = db.relationship("Child", backref="profile", cascade="all, delete-orphan")

    def manager_profile(self):
        if self.reporting_manager and self.reporting_manager.profile:
            return self.reporting_manager.profile
        return None


class Child(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("employee_profile.id"), nullable=False)
    name = db.Column(db.String(150))
    date_of_birth = db.Column(db.Date)


class Recognition(db.Model):
    """A public thank-you or good wish shared with a colleague."""
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    category = db.Column(db.String(30), nullable=False, default="Appreciation")
    message = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sender = db.relationship("User", foreign_keys=[sender_id])
    recipient = db.relationship("User", foreign_keys=[recipient_id])


class ImportantDate(db.Model):
    """An organization-wide date maintained by an administrator."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(500))
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    created_by = db.relationship("User", foreign_keys=[created_by_id])


class LeaveType(db.Model):
    """
    Admin-managed leave categories, e.g. Sick Leave (12/yr), Casual Leave (12/yr),
    Privilege Leave (18/yr). annual_quota is the full-year entitlement; actual
    balances per employee per year live in LeaveBalance and are prorated from this.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    annual_quota = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LeaveBalance(db.Model):
    """
    One row per (employee, leave type, calendar year). allotted is the prorated
    (or full, for non-joining years) quota for that year. used is recalculated
    from approved LeaveApplications. Recomputed lazily whenever it's missing or
    the admin changes a quota / an employee's DOJ.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    leave_type_id = db.Column(db.Integer, db.ForeignKey("leave_type.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    allotted = db.Column(db.Float, nullable=False, default=0)
    used = db.Column(db.Float, nullable=False, default=0)

    user = db.relationship("User", foreign_keys=[user_id])
    leave_type = db.relationship("LeaveType")

    __table_args__ = (db.UniqueConstraint("user_id", "leave_type_id", "year",
                                           name="uq_balance_user_type_year"),)

    @property
    def remaining(self):
        return round(self.allotted - self.used, 2)


class LeaveApplication(db.Model):
    """
    A single leave request. Goes to the employee's reporting manager for
    approval; admin can also act on any pending request.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    leave_type_id = db.Column(db.Integer, db.ForeignKey("leave_type.id"), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    days = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(500))
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending/approved/rejected/cancelled
    applied_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_at = db.Column(db.DateTime)
    decided_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    decision_note = db.Column(db.String(300))

    user = db.relationship("User", foreign_keys=[user_id])
    leave_type = db.relationship("LeaveType")
    decided_by = db.relationship("User", foreign_keys=[decided_by_id])
    
# ==========================================================
# PAYROLL MODULE
# ==========================================================

class PayComponent(db.Model):
    """
    Salary components defined by HR.
    Example:
        Basic (40% of CTC)
        HRA (20% of Basic)
        PF (12% of Basic)
        Professional Tax (Flat)
    """
    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(100), unique=True, nullable=False)

    # earning / deduction
    component_type = db.Column(db.String(20), nullable=False)

    # flat / percent_of_ctc / percent_of_basic / percent_of_gross
    calc_basis = db.Column(db.String(30), nullable=False)

    calc_value = db.Column(db.Float, nullable=False)

    calc_order = db.Column(db.Integer, default=0)

    is_basic = db.Column(db.Boolean, default=False)

    is_active = db.Column(db.Boolean, default=True)


class EmployeeCTC(db.Model):
    """
    Active salary assigned to an employee.
    """
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    monthly_ctc = db.Column(db.Float, nullable=False)

    effective_from = db.Column(db.Date, nullable=False)

    user = db.relationship("User")


class Payslip(db.Model):
    """
    One payslip per employee per month.
    """
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    month = db.Column(db.Integer, nullable=False)

    year = db.Column(db.Integer, nullable=False)

    monthly_ctc_used = db.Column(db.Float, nullable=False)

    gross_pay = db.Column(db.Float, nullable=False)

    total_deductions = db.Column(db.Float, nullable=False)

    net_pay = db.Column(db.Float, nullable=False)

    # ---- Post-generation & Variable Adjustments ----
    arrears = db.Column(db.Float, default=0.0)
    loss_of_pay = db.Column(db.Float, default=0.0)
    ot_hours = db.Column(db.Float, default=0.0)
    ot_amount = db.Column(db.Float, default=0.0)
    incentive = db.Column(db.Float, default=0.0)
    pf_deduction = db.Column(db.Float, default=0.0)
    gratuity_provision = db.Column(db.Float, default=0.0)

    generated_at = db.Column(db.DateTime, default=datetime.utcnow)

    generated_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    user = db.relationship("User", foreign_keys=[user_id])

    generated_by = db.relationship("User", foreign_keys=[generated_by_id])

    lines = db.relationship(
        "PayslipLine",
        backref="payslip",
        cascade="all, delete-orphan"
    )

    __table_args__ = (
        db.UniqueConstraint(
            "user_id",
            "month",
            "year",
            name="uq_payslip_month"
        ),
    )

    def month_label(self):
        import calendar
        return f"{calendar.month_name[self.month]} {self.year}"


class PayslipLine(db.Model):
    """
    Every salary component stored inside a payslip.
    """
    id = db.Column(db.Integer, primary_key=True)

    payslip_id = db.Column(
        db.Integer,
        db.ForeignKey("payslip.id"),
        nullable=False
    )

    component_name = db.Column(db.String(100), nullable=False)

    component_type = db.Column(db.String(20), nullable=False)

    amount = db.Column(db.Float, nullable=False)


# ==========================================================
# ATTENDANCE MODULE
# ==========================================================

class Attendance(db.Model):
    """
    Daily attendance records for employees.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    clock_in = db.Column(db.DateTime, nullable=True)
    clock_out = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="Present")  # Present, Late, Half Day, Absent, On Leave
    notes = db.Column(db.String(300), nullable=True)
    ot_hours = db.Column(db.Float, default=0.0)
    is_off_day = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        db.UniqueConstraint("user_id", "date", name="uq_user_attendance_date"),
    )

    @property
    def total_seconds_worked(self):
        total = 0
        if self.sessions:
            for s in self.sessions:
                if s.clock_in and s.clock_out:
                    total += (s.clock_out - s.clock_in).total_seconds()
                elif s.clock_in and not s.clock_out:
                    total += (datetime.now() - s.clock_in).total_seconds()
        elif self.clock_in and self.clock_out:
            total = (self.clock_out - self.clock_in).total_seconds()
        return total

    @property
    def work_duration(self):
        secs = self.total_seconds_worked
        if secs > 0:
            hours, remainder = divmod(int(secs), 3600)
            minutes, _ = divmod(remainder, 60)
            return f"{hours}h {minutes}m"
        return "N/A"

    @property
    def total_hours(self):
        return round(self.total_seconds_worked / 3600.0, 2)


class AttendanceSession(db.Model):
    """
    Individual clock-in/out session within a single day for outdoor/break tracking.
    """
    id = db.Column(db.Integer, primary_key=True)
    attendance_id = db.Column(db.Integer, db.ForeignKey("attendance.id"), nullable=False)
    clock_in = db.Column(db.DateTime, nullable=False)
    clock_out = db.Column(db.DateTime, nullable=True)
    reason = db.Column(db.String(200), nullable=True)

    attendance = db.relationship("Attendance", backref=db.backref("sessions", cascade="all, delete-orphan"))


class SpecialApproval(db.Model):
    """
    Special Approvals (On-Duty, Work From Home, Short Leave, Special Clearance).
    Founding members get instant self-approval.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    approver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    approval_type = db.Column(db.String(50), nullable=False)  # WFH, On-Duty, Short Leave, Special Clearance
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    days = db.Column(db.Float, default=1.0)
    reason = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending, approved, rejected
    is_self_approved = db.Column(db.Boolean, default=False)
    applied_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_at = db.Column(db.DateTime)
    decision_note = db.Column(db.String(300))

    user = db.relationship("User", foreign_keys=[user_id])
    approver = db.relationship("User", foreign_keys=[approver_id])


class PayrollSetting(db.Model):
    """
    Key-value settings for payroll (OT formula parameters, standard workday hours, PF rate, etc.)
    """
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(200), nullable=False)


class AttendanceRegularization(db.Model):
    """
    Backdated attendance regularization / adjustment request for past dates.
    Founding members get instant self-approval.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    approver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    date = db.Column(db.Date, nullable=False)
    requested_clock_in = db.Column(db.DateTime, nullable=True)
    requested_clock_out = db.Column(db.DateTime, nullable=True)
    requested_status = db.Column(db.String(20), default="Present")  # Present, Late, Half Day
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending, approved, rejected
    is_self_approved = db.Column(db.Boolean, default=False)
    applied_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_at = db.Column(db.DateTime)
    decision_note = db.Column(db.String(300))

    user = db.relationship("User", foreign_keys=[user_id])
    approver = db.relationship("User", foreign_keys=[approver_id])




# ==========================================================
# APPRAISAL MODULE
# ==========================================================

# ==========================================================
# APPRAISAL MODULE (KRAs & KPIs Metric-Driven)
# ==========================================================

class Appraisal(db.Model):
    """
    Performance appraisal record for Q1 (Sep-Oct) or Q2 (Mar-Apr) for a specified year.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    evaluator_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    
    period_type = db.Column(db.String(50), nullable=False)  # "Q1 (Sep-Oct)" or "Q2 (Mar-Apr)"
    year = db.Column(db.Integer, nullable=False)  # e.g. 2026
    review_period = db.Column(db.String(80), nullable=False)  # "Q1 (Sep-Oct) 2026"
    
    rating = db.Column(db.Float, nullable=True)  # Manager rating (1.0 to 5.0)
    potential_rating = db.Column(db.Float, nullable=True)  # Manager potential assessment (1.0 to 5.0)
    self_rating = db.Column(db.Float, nullable=True)  # Employee self rating (1.0 to 5.0)
    weighted_score = db.Column(db.Float, nullable=True)  # Auto-calculated weighted score %
    
    self_comments = db.Column(db.Text, nullable=True)
    evaluator_comments = db.Column(db.Text, nullable=True)
    sendback_note = db.Column(db.Text, nullable=True)
    
    status = db.Column(db.String(30), nullable=False, default="Initiated")  
    # States: Initiated, Draft, Self Review Submitted, Sent Back for Edit, Completed
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id])
    evaluator = db.relationship("User", foreign_keys=[evaluator_id])
    
    kras = db.relationship("AppraisalKRA", backref="appraisal", cascade="all, delete-orphan", order_by="AppraisalKRA.id")


class AppraisalKRA(db.Model):
    """
    Key Result Area (Max 5 per Appraisal). Weightage sum across KRAs = 100%.
    """
    id = db.Column(db.Integer, primary_key=True)
    appraisal_id = db.Column(db.Integer, db.ForeignKey("appraisal.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    weightage_percent = db.Column(db.Float, nullable=False, default=20.0)  # Max 100%, total KRAs sum = 100%
    
    self_rating = db.Column(db.Float, nullable=True)  # 1.0 to 5.0
    manager_rating = db.Column(db.Float, nullable=True)  # 1.0 to 5.0

    kpis = db.relationship("AppraisalKPI", backref="kra", cascade="all, delete-orphan", order_by="AppraisalKPI.id")


class AppraisalKPI(db.Model):
    """
    Key Performance Indicator (Max 6 per KRA). Metric Driven (Actual / Target * 100).
    Weightage sum across KPIs within a KRA = 100%.
    """
    id = db.Column(db.Integer, primary_key=True)
    kra_id = db.Column(db.Integer, db.ForeignKey("appraisal_kra.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    weightage_percent = db.Column(db.Float, nullable=False, default=100.0)  # Weightage within KRA (sum = 100%)
    
    metric_formula_desc = db.Column(db.String(200), nullable=True)  # e.g. "Total Training Scheduled / Total Training Planned * 100"
    target_value = db.Column(db.Float, nullable=False, default=100.0)
    actual_value = db.Column(db.Float, nullable=False, default=0.0)
    unit = db.Column(db.String(30), default="%")
    
    achievement_percent = db.Column(db.Float, default=0.0)  # (actual / target) * 100
    
    self_rating = db.Column(db.Float, nullable=True)  # 1.0 to 5.0
    manager_rating = db.Column(db.Float, nullable=True)  # 1.0 to 5.0


class HRLetterTemplate(db.Model):
    """Editable master format used when HR issues an employee letter."""
    id = db.Column(db.Integer, primary_key=True)
    letter_type = db.Column(db.String(50), unique=True, nullable=False)
    title = db.Column(db.String(150), nullable=False)
    body = db.Column(db.Text, nullable=False)
    hr_only = db.Column(db.Boolean, default=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EmployeeLetter(db.Model):
    """A requested or issued letter belonging to one employee."""
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey("hr_letter_template.id"), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Requested")
    requested_at = db.Column(db.DateTime, default=datetime.utcnow)
    issued_at = db.Column(db.DateTime)
    issued_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    employee = db.relationship("User", foreign_keys=[employee_id])
    template = db.relationship("HRLetterTemplate")
    issued_by = db.relationship("User", foreign_keys=[issued_by_id])


class ResourceDocument(db.Model):
    """Published handbook, policy, or form available to all signed-in users."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(40), nullable=False, default="Policy")
    description = db.Column(db.String(500))
    filename = db.Column(db.String(255), nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_published = db.Column(db.Boolean, default=True)

    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id])


class AppraisalMeeting(db.Model):
    """Shared appraisal discussion calendar entry."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    scheduled_for = db.Column(db.DateTime, nullable=False)
    location = db.Column(db.String(200))
    notes = db.Column(db.String(500))
    employee_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    employee = db.relationship("User", foreign_keys=[employee_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])


class HRMemory(db.Model):
    """Photo and story from an HR-organized event."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    event_date = db.Column(db.Date)
    description = db.Column(db.Text)
    image_filename = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    created_by = db.relationship("User", foreign_keys=[created_by_id])


# ==========================================================
# RECRUITMENT & APPLICANT TRACKING SYSTEM (ATS) MODULE
# ==========================================================

class JobRequisition(db.Model):
    """
    Manpower Requisition Request & Job Description posted by Managers/Admin.
    Supports multi-level manager hierarchy approval -> Admin final approval.
    """
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    department = db.Column(db.String(100), nullable=False)
    number_of_positions = db.Column(db.Integer, nullable=False, default=1)
    
    target_ctc_min = db.Column(db.Float, nullable=True, default=0.0)
    target_ctc_max = db.Column(db.Float, nullable=True, default=0.0)
    required_exp_years = db.Column(db.Float, nullable=True, default=0.0)
    qualification = db.Column(db.String(200), nullable=True)
    key_skills = db.Column(db.Text, nullable=False)  # Comma-separated key skills for screening
    
    priority = db.Column(db.String(20), default="Medium")  # Low, Medium, High, Urgent
    reason_for_hiring = db.Column(db.Text, nullable=True)
    job_description_text = db.Column(db.Text, nullable=True)
    
    requested_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    approval_level_1_manager_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    
    is_manager_approved = db.Column(db.Boolean, default=False)
    is_admin_approved = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(50), default="Pending Manager Approval") 
    # States: Pending Manager Approval, Pending Admin Approval, Approved, Rejected, Closed
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    requested_by = db.relationship("User", foreign_keys=[requested_by_id])
    approval_manager = db.relationship("User", foreign_keys=[approval_level_1_manager_id])
    
    candidates = db.relationship("Candidate", backref="requisition", cascade="all, delete-orphan", order_by="Candidate.id.desc()")


class Candidate(db.Model):
    """
    Candidate application profile for a Job Requisition.
    Includes automated Skill Match % Score and recruitment pipeline status.
    """
    id = db.Column(db.Integer, primary_key=True)
    requisition_id = db.Column(db.Integer, db.ForeignKey("job_requisition.id"), nullable=False)
    
    full_name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    current_company = db.Column(db.String(150), nullable=True)
    current_designation = db.Column(db.String(150), nullable=True)
    total_exp_years = db.Column(db.Float, default=0.0)
    key_skills = db.Column(db.Text, nullable=True)
    
    resume_filename = db.Column(db.String(250), nullable=True)
    match_score_percent = db.Column(db.Float, default=0.0)  # Computed Match % Score against JD
    
    status = db.Column(db.String(50), default="Screened")
    # States: Screened, Interview Scheduled, Shortlisted, Final Discussion, Offered, Joined, Rejected
    
    offered_ctc = db.Column(db.Float, nullable=True)
    joining_date = db.Column(db.Date, nullable=True)
    hr_notes = db.Column(db.Text, nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    interviews = db.relationship("CandidateInterview", backref="candidate", cascade="all, delete-orphan", order_by="CandidateInterview.id")


class CandidateInterview(db.Model):
    """
    Interview schedule slot linked to an interviewer (Manager) with evaluation feedback.
    """
    id = db.Column(db.Integer, primary_key=True)
    candidate_id = db.Column(db.Integer, db.ForeignKey("candidate.id"), nullable=False)
    interviewer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    
    round_name = db.Column(db.String(100), nullable=False, default="Technical Round 1")
    scheduled_time = db.Column(db.DateTime, nullable=False)
    location_or_link = db.Column(db.String(250), nullable=True)
    
    status = db.Column(db.String(30), default="Scheduled")  # Scheduled, Completed, Cancelled
    manager_rating = db.Column(db.Float, nullable=True)  # 1.0 to 5.0
    manager_feedback = db.Column(db.Text, nullable=True)
    next_interviewer_email = db.Column(db.String(150), nullable=True)
    
    decision = db.Column(db.String(50), nullable=True)
    # Decision: Shortlist Next Round, Final Discussion Closure, Reject
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    interviewer = db.relationship("User", foreign_keys=[interviewer_id])


class DemoLead(db.Model):
    """
    Sales demo lead submitted via the product brochure modal.
    """
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    work_email = db.Column(db.String(150), nullable=False)
    company_name = db.Column(db.String(150), nullable=False)
    team_size = db.Column(db.String(50), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), default="New Lead")  # New Lead, Contacted, Scheduled, Closed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)



