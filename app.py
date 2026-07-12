import os
import re
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, flash, request, send_from_directory, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename

from models import db, User, EmployeeProfile, Child, LeaveType

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}

app = Flask(__name__)
app.config["SECRET_KEY"] = "hrms-dev-secret-key-change-in-production"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "instance", "hrms.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    """Generate the next sequential employee code like EMP1001, EMP1002 ..."""
    last = User.query.filter(User.employee_code.like("EMP%")).order_by(User.id.desc()).first()
    if not last:
        return "EMP1001"
    try:
        num = int(last.employee_code.replace("EMP", ""))
    except ValueError:
        num = 1000
    return f"EMP{num + 1}"


def validate_pan(pan):
    return bool(re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan.upper())) if pan else True


def validate_aadhaar(aadhaar):
    digits = re.sub(r"\D", "", aadhaar) if aadhaar else ""
    return len(digits) == 12 if aadhaar else True


def validate_ifsc(ifsc):
    return bool(re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$", ifsc.upper())) if ifsc else True


def role_required(*roles):
    """Decorator factory to restrict a route to specific roles."""
    from functools import wraps

    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("login"))
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# Import routes at the bottom to avoid circular-import issues while keeping
# everything registered on the same `app` instance.
from routes import register_routes  # noqa: E402

register_routes(app, db, login_user, logout_user, login_required, current_user,
                 allowed_file, parse_date, next_employee_code,
                 validate_pan, validate_aadhaar, validate_ifsc, role_required)


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403,
                            message="You don't have permission to view this page."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404,
                            message="That page doesn't exist."), 404


def create_default_admin():
    """Seed a default admin account on first run so there's always a way in."""
    if not User.query.filter_by(role="admin").first():
        admin = User(employee_code="ADMIN001", role="admin", must_change_password=True)
        admin.set_password("Admin@123")
        db.session.add(admin)
        db.session.commit()

        profile = EmployeeProfile(user_id=admin.id, full_name="System Administrator",
                                   designation="HR Administrator", department="Human Resources")
        db.session.add(profile)
        db.session.commit()


def create_default_leave_types():
    """Seed the three standard leave types on first run, if none exist yet."""
    if LeaveType.query.first():
        return
    defaults = [("Sick Leave", 12), ("Casual Leave", 12), ("Privilege Leave", 18)]
    for name, quota in defaults:
        db.session.add(LeaveType(name=name, annual_quota=quota))
    db.session.commit()


def run_lightweight_migrations():
    """
    SQLite + no migration framework here, so for small additive schema changes
    on an existing database file, just check the column exists and add it if
    not. db.create_all() only creates brand-new tables, so it won't add a
    column to a table that already exists from before this field was added.
    """
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    existing_columns = [c["name"] for c in inspector.get_columns("user")]
    if "is_active" not in existing_columns:
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE user ADD COLUMN is_active BOOLEAN DEFAULT 1"))
            conn.commit()


if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
    with app.app_context():
        db.create_all()
        run_lightweight_migrations()
        create_default_admin()
        create_default_leave_types()
    app.run(debug=True, host="0.0.0.0", port=5000)
