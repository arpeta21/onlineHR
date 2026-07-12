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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    profile = db.relationship("EmployeeProfile", backref="user", uselist=False,
                               cascade="all, delete-orphan",
                               foreign_keys="EmployeeProfile.user_id")

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
