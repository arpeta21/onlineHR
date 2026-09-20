import os
import re
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user

from models import db, User, EmployeeProfile, LeaveType, HRLetterTemplate

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}

app = Flask(__name__)

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY",
    "hrms-dev-secret-key-change-in-production"
)

database_url = os.getenv("DATABASE_URL")

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
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB max upload limit

# Security & Cookie Confidentiality Configuration
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 3600  # Auto logout after 1 hour inactivity

@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# ------------------------------------------------------------------------------
# Initialize Extensions
# ------------------------------------------------------------------------------

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


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
    last = (
        User.query.filter(User.employee_code.like("EMP%"))
        .order_by(User.id.desc())
        .first()
    )

    if not last:
        return "EMP1001"

    try:
        number = int(last.employee_code.replace("EMP", ""))
    except ValueError:
        number = 1000

    return f"EMP{number + 1}"


def validate_pan(pan):
    return bool(re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan.upper())) if pan else True


def validate_aadhaar(aadhaar):
    digits = re.sub(r"\D", "", aadhaar) if aadhaar else ""
    return len(digits) == 12 if aadhaar else True


def validate_ifsc(ifsc):
    return bool(re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$", ifsc.upper())) if ifsc else True


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
    if not User.query.filter_by(role="admin").first():
        admin = User(
            employee_code="ADMIN001",
            role="admin",
            must_change_password=True,
        )

        admin.set_password("Admin@123")

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

    inspector = inspect(db.engine)
    tables = inspector.get_table_names()

    if "user" in tables:
        existing_user = [c["name"] for c in inspector.get_columns("user")]
        if "is_active" not in existing_user:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE user ADD COLUMN is_active BOOLEAN DEFAULT 1"))
                conn.commit()
        if "created_by_id" not in existing_user:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE user ADD COLUMN created_by_id INTEGER NULL"))
                conn.commit()

    if "employee_profile" in tables:
        existing_ep = [c["name"] for c in inspector.get_columns("employee_profile")]
        with db.engine.connect() as conn:
            if "is_founding_member" not in existing_ep:
                conn.execute(text("ALTER TABLE employee_profile ADD COLUMN is_founding_member BOOLEAN DEFAULT 0"))
            if "monthly_ctc" not in existing_ep:
                conn.execute(text("ALTER TABLE employee_profile ADD COLUMN monthly_ctc FLOAT DEFAULT 0.0"))
            conn.commit()

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




# ------------------------------------------------------------------------------
# Production Startup
# ------------------------------------------------------------------------------

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)

with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print("db.create_all notice:", e)
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
    app.run(
        debug=True,
        host="0.0.0.0",
        port=5000,
    )