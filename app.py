import os
import re
import base64
import hashlib
import secrets
import shutil
import string
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template, redirect, url_for, abort, request
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from flask_migrate import Migrate
from cryptography.fernet import Fernet
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import text

from models import db, User, Tenant, EmployeeProfile, LeaveType, HRLetterTemplate

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
APP_ENV = os.getenv("APP_ENV", "development").lower()
APP_DEBUG = os.getenv("APP_DEBUG", "0").strip().lower() in {"1", "true", "yes"}
UPLOAD_FOLDER = os.getenv("UPLOAD_DIR") or os.path.join(BASE_DIR, "instance", "uploads")
PUBLIC_UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp", "txt", "doc", "docx", "xls", "xlsx"}

# ------------------------------------------------------------------------------
# Lockout configuration — configurable via environment variables so thresholds
# can be tightened in production without a code change.
# MAX_LOGIN_ATTEMPTS : number of consecutive failures before the account locks.
# LOCKOUT_MINUTES    : how long (in minutes) the account stays locked.
# LOGIN_RATE_LIMIT   : flask-limiter rate string applied to the login POST.
# CHANGE_PW_RATE_LIMIT: flask-limiter rate string applied to the change-password POST.
# ------------------------------------------------------------------------------
def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

MAX_LOGIN_ATTEMPTS  = _env_int("MAX_LOGIN_ATTEMPTS", 10)
LOCKOUT_MINUTES     = _env_int("LOCKOUT_MINUTES", 15)
LOGIN_RATE_LIMIT    = os.getenv("LOGIN_RATE_LIMIT", "5 per minute")
CHANGE_PW_RATE_LIMIT = os.getenv("CHANGE_PW_RATE_LIMIT", "10 per minute")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
csrf = CSRFProtect(app)

# ------------------------------------------------------------------------------
# Rate-limit storage
# In production RATELIMIT_STORAGE_URI must point to Redis so that limits are
# shared across all Gunicorn workers and survive process restarts.  Falling
# back to in-memory storage in production would let each worker maintain its
# own independent counter, effectively multiplying the allowed attempts by the
# number of workers and losing state on every restart.
# ------------------------------------------------------------------------------
_ratelimit_storage_uri = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
if APP_ENV == "production" and _ratelimit_storage_uri == "memory://":
    raise RuntimeError(
        "RATELIMIT_STORAGE_URI must be set to a Redis URL in production. "
        "In-memory rate limiting is unsafe with multiple Gunicorn workers."
    )
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=_ratelimit_storage_uri,
    storage_options={"socket_connect_timeout": 5},
    on_breach=None,          # use default 429 response
    swallow_errors=False,    # raise, never silently fall back to memory
)

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

secret_key = os.getenv("SECRET_KEY")
if APP_ENV == "production" and not secret_key:
    raise RuntimeError("SECRET_KEY must be configured in production.")
app.config["SECRET_KEY"] = secret_key or secrets.token_hex(32)

database_url = os.getenv("DATABASE_URL")

if APP_ENV == "production" and not database_url:
    raise RuntimeError("DATABASE_URL must be configured in production.")
if (
    APP_ENV == "production"
    and database_url
    and database_url.lower().startswith("sqlite")
    and os.getenv("ALLOW_SQLITE_PRODUCTION", "0").strip().lower() not in {"1", "true", "yes"}
):
    raise RuntimeError("Production DATABASE_URL must use the configured production database.")

if database_url:
    # Render/PostgreSQL
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    # Local SQLite
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        "sqlite:///" + os.path.join(BASE_DIR, "instance", "hrms.db")
    )

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["DATA_ENCRYPTION_KEY"] = os.getenv("DATA_ENCRYPTION_KEY")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB max upload limit
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=1)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = APP_ENV == "production"
app.config["REMEMBER_COOKIE_SECURE"] = APP_ENV == "production"
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"

if APP_ENV == "production":
    try:
        Fernet(app.config["DATA_ENCRYPTION_KEY"].encode("utf-8"))
    except (AttributeError, ValueError, TypeError):
        raise RuntimeError("DATA_ENCRYPTION_KEY is invalid in production.")

@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response


@app.get("/health")
def health_check():
    """Minimal liveness/readiness check without exposing configuration."""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok"}), 200
    except Exception:
        app.logger.exception("Health check database query failed")
        return jsonify({"status": "unavailable"}), 503


if APP_ENV == "production":
    @app.errorhandler(500)
    def production_server_error(error):
        incident_id = secrets.token_hex(8)
        app.logger.exception("Unhandled server error %s", incident_id)
        db.session.rollback()
        return render_template(
            "error.html",
            code=500,
            message=f"Something went wrong. Reference: {incident_id}",
        ), 500

    @app.errorhandler(413)
    def production_request_too_large(error):
        return render_template(
            "error.html",
            code=413,
            message="The uploaded request is too large.",
        ), 413

# ------------------------------------------------------------------------------
# Initialize Extensions
# ------------------------------------------------------------------------------

db.init_app(app)
migrate = Migrate(app, db)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "info"


@app.before_request
def force_password_change():
    if not current_user.is_authenticated:
        return None
    allowed_endpoints = {"login", "logout", "change_password", "static"}
    if current_user.must_change_password and request.endpoint not in allowed_endpoints:
        return redirect(url_for("change_password"))
    return None


@app.before_request
def enforce_tenant_status():
    if not current_user.is_authenticated or current_user.role == "admin":
        return None
    if current_user.tenant is None and APP_ENV != "production":
        return None
    from tenant_security import ensure_tenant_active
    ensure_tenant_active()
    return None


@login_manager.user_loader
def load_user(user_id):
    user = db.session.get(User, int(user_id))
    return user if user and user.is_active else None


# ------------------------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------------------------

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def next_employee_code():
    highest_number = 1000
    for row in User.query.filter(User.employee_code.like("EMP%")):
        match = re.fullmatch(r"EMP(\d+)", row.employee_code or "", re.IGNORECASE)
        if match:
            highest_number = max(highest_number, int(match.group(1)))
    return f"EMP{highest_number + 1}"


def validate_pan(pan):
    return bool(re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan.upper())) if pan else True


def validate_aadhaar(aadhaar):
    digits = re.sub(r"\D", "", aadhaar) if aadhaar else ""
    return len(digits) == 12 if aadhaar else True


def validate_ifsc(ifsc):
    return bool(re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$", ifsc.upper())) if ifsc else True


def generate_temp_password(length=12):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(ch.isupper() for ch in password)
            and any(ch.islower() for ch in password)
            and any(ch.isdigit() for ch in password)
            and any(ch in "!@#$%^&*" for ch in password)
        ):
            return password


def get_fernet():
    key = app.config["DATA_ENCRYPTION_KEY"]
    if key:
        return Fernet(key.encode("utf-8"))
    digest = hashlib.sha256(app.config["SECRET_KEY"].encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest[:32]))


def get_legacy_fernet():
    digest = hashlib.sha256(app.config["SECRET_KEY"].encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest[:32]))


def encrypt_field(value):
    if value in (None, ""):
        return value
    text = str(value).strip()
    if text.startswith("enc:"):
        return text
    return "enc:" + get_fernet().encrypt(text.encode("utf-8")).decode("utf-8")


def decrypt_field(value):
    if value in (None, ""):
        return value
    text = str(value).strip()
    if not text.startswith("enc:"):
        return text
    try:
        return get_fernet().decrypt(text.replace("enc:", "", 1).encode("utf-8")).decode("utf-8")
    except Exception:
        try:
            return get_legacy_fernet().decrypt(text.replace("enc:", "", 1).encode("utf-8")).decode("utf-8")
        except Exception:
            return "***"


def mask_sensitive(value, keep=4):
    raw = decrypt_field(value) if value is not None else ""
    if not raw:
        return "—"
    if len(raw) <= 8:
        return "*" * len(raw)
    visible = max(keep, 2)
    return f"{raw[:visible]}{'*' * (len(raw) - visible)}"


app.jinja_env.globals["mask_sensitive"] = mask_sensitive
app.jinja_env.globals["decrypt_field"] = decrypt_field


def role_required(*roles):
    from functools import wraps

    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("login"))

            effective_roles = set(roles)
            if "admin" in effective_roles:
                effective_roles.add("demo_admin")

            if current_user.role not in effective_roles:
                abort(403)

            return f(*args, **kwargs)

        return wrapped

    return decorator


# ------------------------------------------------------------------------------
# Register Routes
# ------------------------------------------------------------------------------

from routes import register_routes

register_routes(
    app,
    db,
    login_user,
    logout_user,
    login_required,
    current_user,
    allowed_file,
    parse_date,
    next_employee_code,
    validate_pan,
    validate_aadhaar,
    validate_ifsc,
    role_required,
    generate_temp_password,
    encrypt_field,
    limiter,
    max_login_attempts=MAX_LOGIN_ATTEMPTS,
    lockout_minutes=LOCKOUT_MINUTES,
    login_rate_limit=LOGIN_RATE_LIMIT,
    change_pw_rate_limit=CHANGE_PW_RATE_LIMIT,
)


# ------------------------------------------------------------------------------
# Error Pages
# ------------------------------------------------------------------------------

@app.errorhandler(403)
def forbidden(e):
    return (
        render_template(
            "error.html",
            code=403,
            message="You don't have permission to view this page.",
        ),
        403,
    )


@app.errorhandler(404)
def not_found(e):
    return (
        render_template(
            "error.html",
            code=404,
            message="That page doesn't exist.",
        ),
        404,
    )


@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    return (
        render_template(
            "error.html",
            code=500,
            message="An unexpected system error occurred. Data transaction rolled back safely.",
        ),
        500,
    )


# ------------------------------------------------------------------------------
# Initial Data
# ------------------------------------------------------------------------------

def create_default_admin():
    tenant = Tenant.query.filter_by(tenant_type="internal").first()
    if tenant is None:
        tenant = Tenant(company_name="Sanvit HR Internal", tenant_type="internal")
        db.session.add(tenant)
        db.session.flush()

    existing_admin = User.query.filter_by(role="admin").first()
    if existing_admin and existing_admin.tenant_id is not None:
        existing_admin.tenant_id = None
        db.session.commit()
    if existing_admin and existing_admin.check_password("Admin@123"):
        temp_password = generate_temp_password()
        existing_admin.set_password(temp_password)
        existing_admin.must_change_password = True
        db.session.commit()
        return

    if not existing_admin:
        temp_password = os.getenv("ADMIN_INITIAL_PASSWORD") or generate_temp_password()
        if len(temp_password) < 10:
            raise RuntimeError("ADMIN_INITIAL_PASSWORD must be at least 10 characters.")
        admin = User(
            employee_code="ADMIN001",
            role="admin",
            must_change_password=True,
            tenant_id=None,
        )

        admin.set_password(temp_password)

        db.session.add(admin)
        db.session.commit()

        profile = EmployeeProfile(
            user_id=admin.id,
            full_name="System Administrator",
            designation="HR Administrator",
            department="Human Resources",
        )

        db.session.add(profile)
        db.session.commit()

    for user in User.query.filter(User.role != "admin", User.tenant_id.is_(None)).all():
        if user.role == "demo_admin":
            company_name = user.profile.full_name if user.profile else user.employee_code
            user.tenant = Tenant(company_name=f"{company_name} Practice", tenant_type="demo", subscription_status="trial")
            db.session.flush()
        else:
            user.tenant = tenant
    for demo_admin in User.query.filter_by(role="demo_admin").all():
        if demo_admin.tenant_id:
            User.query.filter(
                User.created_by_id == demo_admin.id,
                User.tenant_id.is_(None),
            ).update({"tenant_id": demo_admin.tenant_id})
    db.session.commit()


@app.cli.command("reset-admin")
def reset_admin_command():
    """Reset the real admin password using ADMIN_INITIAL_PASSWORD or a generated value."""
    with app.app_context():
        admin = User.query.filter_by(role="admin").first()
        if not admin:
            raise RuntimeError("No admin account exists.")
        password = os.getenv("ADMIN_INITIAL_PASSWORD")
        if not password:
            raise RuntimeError("ADMIN_INITIAL_PASSWORD must be supplied for reset-admin.")
        if len(password) < 10:
            raise RuntimeError("ADMIN_INITIAL_PASSWORD must be at least 10 characters.")
        admin.set_password(password)
        admin.must_change_password = True
        admin.failed_login_attempts = 0
        admin.locked_until = None
        db.session.commit()
        app.logger.info("Admin password reset completed for employee code %s", admin.employee_code)


def create_default_leave_types():
    if LeaveType.query.first():
        return

    defaults = [
        ("Sick Leave", 12),
        ("Casual Leave", 12),
        ("Privilege Leave", 18),
    ]

    for name, quota in defaults:
        db.session.add(LeaveType(name=name, annual_quota=quota))

    db.session.commit()


def create_default_letter_templates():
    defaults = [
        ("Appointment Letter", "Appointment Letter", False),
        ("Offer Letter", "Offer Letter", False),
        ("Appraisal Letter", "Appraisal Letter", True),
        ("Transfer Letter", "Transfer Letter", False),
        ("Promotion Cum Appraisal Letter", "Promotion Cum Appraisal Letter", False),
        ("Relieving Letter", "Relieving Letter", True),
        ("Work Experience Letter", "Work Experience Letter", True),
        ("Confirmation Letter", "Confirmation Letter", True),
    ]
    body = (
        "Dear {{ employee_name }},\n\n"
        "This letter is issued to you by {{ company_name }}. "
        "Your designation is {{ designation }} and your department is {{ department }}.\n\n"
        "{{ details }}\n\n"
        "We appreciate your contribution and wish you continued success.\n\n"
        "For {{ company_name }}\nHuman Resources Department"
    )
    for letter_type, title, hr_only in defaults:
        if not HRLetterTemplate.query.filter_by(letter_type=letter_type).first():
            db.session.add(HRLetterTemplate(
                letter_type=letter_type, title=title, body=body, hr_only=hr_only
            ))
    db.session.commit()


def run_lightweight_migrations():
    from sqlalchemy import inspect, text
    from models import (
        Appraisal,
        Attendance,
        AttendanceSession,
        AttendanceRegularization,
        Candidate,
        CandidateInterview,
        EmployeeCTC,
        EmployeeLetter,
        AppraisalMeeting,
        HRLetterTemplate,
        HRMemory,
        Holiday,
        ImportantDate,
        JobRequisition,
        LeaveBalance,
        LeaveApplication,
        LeaveType,
        PayComponent,
        PayrollSetting,
        Payslip,
        PayslipLine,
        Recognition,
        ResourceDocument,
        SpecialApproval,
    )

    inspector = inspect(db.engine)
    tables = inspector.get_table_names()

    if "payslip_line" in tables:
        for payslip in Payslip.query.all():
            seen_lines = set()
            for line in sorted(payslip.lines, key=lambda item: item.id):
                line_key = (
                    line.component_name.strip().lower(),
                    line.component_type,
                )
                if line_key in seen_lines:
                    db.session.delete(line)
                else:
                    seen_lines.add(line_key)
        db.session.commit()
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_payslip_line_component "
            "ON payslip_line (payslip_id, component_name, component_type)"
        ))
        db.session.commit()

    tenant_columns = {
        "audit_log": "tenant_id INTEGER",
        "employee_profile": "tenant_id INTEGER",
        "leave_application": "tenant_id INTEGER",
        "payslip": "tenant_id INTEGER",
        "attendance": "tenant_id INTEGER",
        "attendance_session": "tenant_id INTEGER",
        "special_approval": "tenant_id INTEGER",
        "attendance_regularization": "tenant_id INTEGER",
        "appraisal": "tenant_id INTEGER",
        "resource_document": "tenant_id INTEGER",
        "job_requisition": "tenant_id INTEGER",
        "candidate": "tenant_id INTEGER",
        "candidate_interview": "tenant_id INTEGER",
        "recognition": "tenant_id INTEGER",
        "important_date": "tenant_id INTEGER",
        "holiday": "tenant_id INTEGER",
        "leave_type": "tenant_id INTEGER",
        "leave_balance": "tenant_id INTEGER",
        "pay_component": "tenant_id INTEGER",
        "employee_ctc": "tenant_id INTEGER",
        "payroll_setting": "tenant_id INTEGER",
        "employee_letter": "tenant_id INTEGER",
        "hr_letter_template": "tenant_id INTEGER",
        "appraisal_meeting": "tenant_id INTEGER",
        "hr_memory": "tenant_id INTEGER",
    }
    for table, column_definition in tenant_columns.items():
        if table in tables:
            existing_columns = {column["name"] for column in inspector.get_columns(table)}
            if "tenant_id" not in existing_columns:
                with db.engine.connect() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_definition}"))
                    conn.commit()

    if "user" in tables:
        existing_user = [c["name"] for c in inspector.get_columns("user")]
        if "tenant_id" not in existing_user:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE "user" ADD COLUMN tenant_id INTEGER NULL'))
                conn.commit()

    if "user" in tables:
        existing_user = [c["name"] for c in inspector.get_columns("user")]
        if "is_active" not in existing_user:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE "user" ADD COLUMN is_active BOOLEAN DEFAULT 1'))
                conn.commit()
        if "created_by_id" not in existing_user:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE "user" ADD COLUMN created_by_id INTEGER NULL'))
                conn.commit()
        if "deactivated_at" not in existing_user:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE "user" ADD COLUMN deactivated_at DATETIME NULL'))
                conn.commit()
        with db.engine.connect() as conn:
            if "failed_login_attempts" not in existing_user:
                conn.execute(text('ALTER TABLE "user" ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0'))
            if "locked_until" not in existing_user:
                conn.execute(text('ALTER TABLE "user" ADD COLUMN locked_until DATETIME NULL'))
            if "last_login_at" not in existing_user:
                conn.execute(text('ALTER TABLE "user" ADD COLUMN last_login_at DATETIME NULL'))
            if "password_changed_at" not in existing_user:
                conn.execute(text('ALTER TABLE "user" ADD COLUMN password_changed_at DATETIME NULL'))
            if "password_reset_at" not in existing_user:
                conn.execute(text('ALTER TABLE "user" ADD COLUMN password_reset_at DATETIME NULL'))
            conn.commit()

    if "employee_profile" in tables:
        existing_ep = [c["name"] for c in inspector.get_columns("employee_profile")]
        with db.engine.connect() as conn:
            if "is_founding_member" not in existing_ep:
                conn.execute(text("ALTER TABLE employee_profile ADD COLUMN is_founding_member BOOLEAN DEFAULT 0"))
            if "monthly_ctc" not in existing_ep:
                conn.execute(text("ALTER TABLE employee_profile ADD COLUMN monthly_ctc FLOAT DEFAULT 0.0"))
            for col in ("pan_document_filename", "aadhaar_document_filename", "cancelled_cheque_filename", "profile_picture_filename"):
                if col not in existing_ep:
                    conn.execute(text(f"ALTER TABLE employee_profile ADD COLUMN {col} VARCHAR(255)"))
            if "tax_regime" not in existing_ep:
                conn.execute(text("ALTER TABLE employee_profile ADD COLUMN tax_regime VARCHAR(20) DEFAULT 'new_regime'"))
            conn.commit()

        for profile in EmployeeProfile.query.all():
            changed = False
            for field in ("pan_number", "aadhaar_number", "bank_account_number"):
                value = getattr(profile, field)
                if value and not str(value).startswith("enc:"):
                    setattr(profile, field, encrypt_field(value))
                    changed = True
            if changed:
                db.session.add(profile)
        db.session.commit()

    if "attendance" in tables:
        existing_att = [c["name"] for c in inspector.get_columns("attendance")]
        with db.engine.connect() as conn:
            if "ot_hours" not in existing_att:
                conn.execute(text("ALTER TABLE attendance ADD COLUMN ot_hours FLOAT DEFAULT 0.0"))
            if "is_off_day" not in existing_att:
                conn.execute(text("ALTER TABLE attendance ADD COLUMN is_off_day BOOLEAN DEFAULT 0"))
            conn.commit()

    if "payslip" in tables:
        existing_ps = [c["name"] for c in inspector.get_columns("payslip")]
        with db.engine.connect() as conn:
            for col, col_type in [
                ("arrears", "FLOAT DEFAULT 0.0"),
                ("loss_of_pay", "FLOAT DEFAULT 0.0"),
                ("ot_hours", "FLOAT DEFAULT 0.0"),
                ("ot_amount", "FLOAT DEFAULT 0.0"),
                ("incentive", "FLOAT DEFAULT 0.0"),
                ("pf_deduction", "FLOAT DEFAULT 0.0"),
                ("gratuity_provision", "FLOAT DEFAULT 0.0"),
            ]:
                if col not in existing_ps:
                    conn.execute(text(f"ALTER TABLE payslip ADD COLUMN {col} {col_type}"))
            for col, col_type in [
                ("esi_deduction", "FLOAT DEFAULT 0.0"),
                ("professional_tax", "FLOAT DEFAULT 0.0"),
                ("tds_deduction", "FLOAT DEFAULT 0.0"),
                ("statutory_note", "VARCHAR(300)"),
                ("tax_regime", "VARCHAR(20) DEFAULT 'new_regime'"),
            ]:
                if col not in existing_ps:
                    conn.execute(text(f"ALTER TABLE payslip ADD COLUMN {col} {col_type}"))
            conn.commit()

    if "pay_component" in tables:
        existing_pc = [c["name"] for c in inspector.get_columns("pay_component")]
        with db.engine.connect() as conn:
            for col, definition in [
                ("statutory_code", "VARCHAR(30)"),
                ("basis_component_id", "INTEGER"),
                ("is_employer_cost", "BOOLEAN DEFAULT 0"),
                ("is_in_ctc", "BOOLEAN DEFAULT 1"),
            ]:
                if col not in existing_pc:
                    conn.execute(text(f"ALTER TABLE pay_component ADD COLUMN {col} {definition}"))
            conn.commit()

    if "employee_ctc" in tables:
        existing_ctc = [c["name"] for c in inspector.get_columns("employee_ctc")]
        with db.engine.connect() as conn:
            for col, definition in [
                ("breakup_mode", "VARCHAR(20) DEFAULT 'automatic'"),
                ("breakup_status", "VARCHAR(20) DEFAULT 'draft'"),
            ]:
                if col not in existing_ctc:
                    conn.execute(text(f"ALTER TABLE employee_ctc ADD COLUMN {col} {definition}"))
            conn.commit()

    if "pay_component" in tables:
        existing_pc = [c["name"] for c in inspector.get_columns("pay_component")]
        with db.engine.connect() as conn:
            if "tax_regime" not in existing_pc:
                conn.execute(text("ALTER TABLE pay_component ADD COLUMN tax_regime VARCHAR(30) DEFAULT 'general'"))
            conn.commit()

    if "appraisal" in tables:
        existing_appr = [c["name"] for c in inspector.get_columns("appraisal")]
        with db.engine.connect() as conn:
            if "period_type" not in existing_appr:
                conn.execute(text("ALTER TABLE appraisal ADD COLUMN period_type VARCHAR(50) DEFAULT 'Q1 (Sep-Oct)'"))
            if "year" not in existing_appr:
                conn.execute(text("ALTER TABLE appraisal ADD COLUMN year INTEGER DEFAULT 2026"))
            if "weighted_score" not in existing_appr:
                conn.execute(text("ALTER TABLE appraisal ADD COLUMN weighted_score FLOAT DEFAULT 0.0"))
            if "potential_rating" not in existing_appr:
                conn.execute(text("ALTER TABLE appraisal ADD COLUMN potential_rating FLOAT"))
            if "sendback_note" not in existing_appr:
                conn.execute(text("ALTER TABLE appraisal ADD COLUMN sendback_note TEXT"))
            conn.commit()

    # Backfill tenant ownership from the authoritative user or parent record.
    for profile in EmployeeProfile.query.filter(EmployeeProfile.tenant_id.is_(None)).all():
        if profile.user and profile.user.tenant_id:
            profile.tenant_id = profile.user.tenant_id
    for model in (LeaveApplication, Payslip, Attendance, SpecialApproval, AttendanceRegularization, Appraisal):
        for record in model.query.filter(model.tenant_id.is_(None)).all():
            user = getattr(record, "user", None)
            if user and user.tenant_id:
                record.tenant_id = user.tenant_id
    for session in AttendanceSession.query.filter(AttendanceSession.tenant_id.is_(None)).all():
        if session.attendance and session.attendance.tenant_id:
            session.tenant_id = session.attendance.tenant_id
    for resource in ResourceDocument.query.filter(ResourceDocument.tenant_id.is_(None)).all():
        if resource.uploaded_by and resource.uploaded_by.tenant_id:
            resource.tenant_id = resource.uploaded_by.tenant_id
    for requisition in JobRequisition.query.filter(JobRequisition.tenant_id.is_(None)).all():
        if requisition.requested_by and requisition.requested_by.tenant_id:
            requisition.tenant_id = requisition.requested_by.tenant_id
    for candidate in Candidate.query.filter(Candidate.tenant_id.is_(None)).all():
        if candidate.requisition and candidate.requisition.tenant_id:
            candidate.tenant_id = candidate.requisition.tenant_id
    for interview in CandidateInterview.query.filter(CandidateInterview.tenant_id.is_(None)).all():
        if interview.candidate and interview.candidate.tenant_id:
            interview.tenant_id = interview.candidate.tenant_id
    for recognition in Recognition.query.filter(Recognition.tenant_id.is_(None)).all():
        if recognition.sender and recognition.sender.tenant_id:
            recognition.tenant_id = recognition.sender.tenant_id
    for important_date in ImportantDate.query.filter(ImportantDate.tenant_id.is_(None)).all():
        if important_date.created_by and important_date.created_by.tenant_id:
            important_date.tenant_id = important_date.created_by.tenant_id
    for leave_balance in LeaveBalance.query.filter(LeaveBalance.tenant_id.is_(None)).all():
        if leave_balance.user and leave_balance.user.tenant_id:
            leave_balance.tenant_id = leave_balance.user.tenant_id
    for employee_ctc in EmployeeCTC.query.filter(EmployeeCTC.tenant_id.is_(None)).all():
        if employee_ctc.user and employee_ctc.user.tenant_id:
            employee_ctc.tenant_id = employee_ctc.user.tenant_id
    for employee_letter in EmployeeLetter.query.filter(EmployeeLetter.tenant_id.is_(None)).all():
        if employee_letter.employee and employee_letter.employee.tenant_id:
            employee_letter.tenant_id = employee_letter.employee.tenant_id
    for meeting in AppraisalMeeting.query.filter(AppraisalMeeting.tenant_id.is_(None)).all():
        if meeting.created_by and meeting.created_by.tenant_id:
            meeting.tenant_id = meeting.created_by.tenant_id
    for memory in HRMemory.query.filter(HRMemory.tenant_id.is_(None)).all():
        if memory.created_by and memory.created_by.tenant_id:
            memory.tenant_id = memory.created_by.tenant_id
    db.session.commit()




# ------------------------------------------------------------------------------
# Production Startup
# ------------------------------------------------------------------------------

if APP_ENV == "production":
    if not app.config["DATA_ENCRYPTION_KEY"]:
        raise RuntimeError("DATA_ENCRYPTION_KEY must be configured in production.")
    if app.config["SECRET_KEY"] == "hrms-dev-secret-key-change-in-production":
        raise RuntimeError("SECRET_KEY must be configured in production.")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)

if os.path.isdir(PUBLIC_UPLOAD_FOLDER):
    for filename in os.listdir(PUBLIC_UPLOAD_FOLDER):
        source = os.path.join(PUBLIC_UPLOAD_FOLDER, filename)
        target = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.isfile(source) and not os.path.exists(target):
            shutil.move(source, target)

with app.app_context():
    try:
        db.create_all()
    except Exception as exc:
        app.logger.exception("Database initialization failed")
        if APP_ENV == "production":
            raise RuntimeError("Database initialization failed.") from exc
    run_lightweight_migrations()
    create_default_admin()
    create_default_leave_types()
    create_default_letter_templates()
    from payroll_logic import seed_default_pay_components
    seed_default_pay_components()



# ------------------------------------------------------------------------------
# Local Development
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    debug_mode = APP_ENV == "development" and APP_DEBUG
    app.run(
        debug=debug_mode,
        host="127.0.0.1" if debug_mode else "0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
    )