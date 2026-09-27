import os
import re
import secrets
import hmac
import math
from functools import wraps
from datetime import datetime, date, timedelta
from types import SimpleNamespace
from sqlalchemy.exc import IntegrityError
from flask import render_template, redirect, url_for, flash, request, send_from_directory, send_file, abort, Response, jsonify
from payroll_logic import (
    generate_payslip,
    validate_components_for_generation,
    get_active_ctc,
    generate_payslip_pdf,
    num_to_words,
    get_employee_monthly_payroll_inputs,
    build_automatic_ctc_lines,
    calculate_ctc_breakup_lines,
)

from models import (
    User,
    Tenant,
    AuditLog,
    EmployeeProfile,
    Child,
    LeaveType,
    LeaveBalance,
    LeaveApplication,
    PayComponent,
    EmployeeCTC,
    EmployeeCTCLine,
    EmployeePayrollAdjustment,
    SalaryAdvance,
    GratuityRecord,
    InvestmentDeclaration,
    Payslip,
    PayslipLine,
    Attendance,
    AttendanceSession,
    Appraisal,
    AppraisalKRA,
    AppraisalKPI,
    SpecialApproval,
    PayrollSetting,
    AttendanceRegularization,
    JobRequisition,
    Candidate,
    CandidateRecruitmentMeta,
    CandidateInterview,
    Recognition,
    ImportantDate,
    Holiday,
    HRLetterTemplate,
    EmployeeLetter,
    ResourceDocument,
    AppraisalMeeting,
    HRMemory,
    DemoLead,
)
from leave_logic import (ensure_balances_for_user, recalculate_user_balances,
                          recalculate_all_balances_for_type, business_days_count)
from attendance_logic import (clock_in_user, clock_out_user, get_today_attendance,
                              get_user_monthly_attendance, bulk_mark_attendance,
                              get_user_full_month_calendar, apply_attendance_regularization,
                              decide_attendance_regularization)
from appraisal_logic import (create_appraisal, submit_kras_for_approval, decide_kras_approval, submit_self_ratings, submit_manager_ratings)
from special_approval_logic import (apply_special_approval, decide_special_approval)
from reports_logic import (
    generate_employee_report_csv,
    generate_attendance_report_csv,
    generate_payroll_report_csv,
    generate_payroll_input_sheet_csv,
    generate_appraisal_report_csv,
    csv_to_styled_xlsx,
)
from recruitment_logic import (
    create_manpower_requisition,
    approve_manpower_requisition,
    add_and_screen_candidate,
    update_candidate_screening,
    delete_candidate,
    parse_resume_text_and_match,
    schedule_candidate_interview,
    submit_interview_feedback,
    issue_candidate_offer,
    generate_requisitions_csv,
)
from policy_rag import search_policy_documents, build_policy_answer, build_people_analytics
from statutory import run_statutory_compliance_checks, calculate_gratuity
from time_utils import now_ist, today_ist


class UploadRejected(ValueError):
    pass






def register_routes(app, db, login_user, logout_user, login_required, current_user,
                     allowed_file, parse_date, next_employee_code,
                     validate_pan, validate_aadhaar, validate_ifsc,
                     role_required, generate_temp_password, encrypt_field, limiter,
                     max_login_attempts=10, lockout_minutes=15,
                     login_rate_limit="5 per minute",
                     change_pw_rate_limit="10 per minute"):

    def password_policy_error(password):
        if len(password) < 12:
            return "Temporary password must be at least 12 characters."
        if not any(char.isupper() for char in password):
            return "Temporary password must contain an uppercase letter."
        if not any(char.islower() for char in password):
            return "Temporary password must contain a lowercase letter."
        if not any(char.isdigit() for char in password):
            return "Temporary password must contain a number."
        if not any(char in "!@#$%^&*" for char in password):
            return "Temporary password must contain a special character."
        return None

    def normalize_bulk_key(value):
        if value is None:
            return ""
        return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())

    def parse_bulk_date(value):
        if value is None or str(value).strip() == "":
            return None
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return parse_date(text)

    def parse_bulk_bool(value):
        if value is None or str(value).strip() == "":
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "founding", "foundingmember"}

    def scope_to_practice_data(query):
        if current_user.is_authenticated and current_user.role == "demo_admin":
            if current_user.tenant_id:
                return query.filter(User.tenant_id == current_user.tenant_id)
            return query.filter(User.created_by_id == current_user.id)
        if current_user.is_authenticated and current_user.role != "admin" and current_user.tenant_id:
            return query.filter(User.tenant_id == current_user.tenant_id)
        return query

    def scoped_user_or_403(user_id):
        user = User.query.get_or_404(user_id)
        if current_user.role == "demo_admin" and user.created_by_id != current_user.id:
            abort(403)
        return user

    def candidate_for_actor_or_403(candidate_id):
        candidate = Candidate.query.get_or_404(candidate_id)
        requisition = candidate.requisition
        if current_user.role == "admin":
            return candidate
        if not requisition or candidate.tenant_id != current_user.tenant_id or requisition.tenant_id != current_user.tenant_id:
            abort(403)
        if current_user.role == "demo_admin" and requisition.requested_by_id == current_user.id:
            return candidate
        if current_user.role == "manager" and requisition.requested_by_id == current_user.id:
            return candidate
        abort(403)

    manager_hidden_profile_fields = {
        "pan_number", "aadhaar_number", "pan_document_filename",
        "aadhaar_document_filename", "bank_account_number", "bank_ifsc_code",
        "bank_name", "bank_branch", "cancelled_cheque_filename",
    }

    def profile_for_manager_view(profile):
        if profile is None:
            return None
        names = {column.name for column in EmployeeProfile.__table__.columns}
        names.update(relation.key for relation in EmployeeProfile.__mapper__.relationships)
        values = {
            name: None if name in manager_hidden_profile_fields else getattr(profile, name)
            for name in names
        }
        values["manager_profile"] = profile.manager_profile
        return SimpleNamespace(**values)

    def real_admin_only():
        if current_user.role != "admin":
            abort(403)

    def audit_event(event_type, target_user_id=None, details=None, user_id=None):
        db.session.add(AuditLog(
            event_type=event_type,
            user_id=user_id if user_id is not None else (current_user.id if current_user.is_authenticated else None),
            tenant_id=current_user.tenant_id if current_user.is_authenticated else None,
            target_user_id=target_user_id,
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
            details=details,
        ))
        db.session.commit()

    def safe_float(value):
        if value is None or str(value).strip() == "":
            return 0.0
        try:
            return float(str(value).strip().replace(",", ""))
        except (TypeError, ValueError):
            return 0.0

    def save_private_upload(file_storage, prefix="upload", allowed_extensions=None):
        extension = os.path.splitext(file_storage.filename or "")[1].lower()
        allowed = allowed_extensions or {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".doc", ".docx", ".xls", ".xlsx"}
        if extension not in allowed or not has_valid_file_signature(file_storage, extension):
            raise UploadRejected("The uploaded file type or content is not allowed.")
        filename = f"{prefix}_{secrets.token_urlsafe(24)}{extension}"
        file_storage.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        return filename

    def has_valid_file_signature(file_storage, extension):
        header = file_storage.stream.read(16)
        file_storage.stream.seek(0)
        if extension == ".pdf":
            return header.startswith(b"%PDF-")
        if extension in {".jpg", ".jpeg"}:
            return header.startswith(b"\xff\xd8\xff")
        if extension == ".png":
            return header.startswith(b"\x89PNG\r\n\x1a\n")
        if extension == ".webp":
            return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
        if extension == ".docx" or extension == ".xlsx":
            return header.startswith(b"PK")
        if extension in {".xls", ".doc"}:
            return header.startswith(b"\xd0\xcf\x11\xe0")
        if extension == ".txt":
            sample = file_storage.read(4096)
            file_storage.stream.seek(0)
            try:
                sample.decode("utf-8")
            except UnicodeDecodeError:
                return False
            return True
        return False

    def resolve_manager_reference(value):
        if value is None or str(value).strip() == "":
            return None
        text = str(value).strip()
        if text.isdigit():
            manager = User.query.get(int(text))
            if manager:
                return manager.id

        manager = User.query.join(EmployeeProfile, EmployeeProfile.user_id == User.id).filter(
            db.or_(
                User.employee_code.ilike(text),
                EmployeeProfile.full_name.ilike(f"%{text}%"),
            )
        ).filter(User.role == "manager").first()
        return manager.id if manager else None

    def read_bulk_employee_rows(file_storage):
        filename = file_storage.filename or ""
        ext = os.path.splitext(filename)[1].lower()
        if ext not in {".csv", ".xlsx", ".xls"}:
            raise ValueError("Only CSV, XLSX, and XLS files are allowed.")

        rows = []
        file_storage.stream.seek(0)

        if ext == ".csv":
            import io
            data = file_storage.read().decode("utf-8-sig", errors="ignore")
            rows = list(__import__("csv").DictReader(io.StringIO(data)))
        else:
            try:
                import pandas as pd
            except Exception as exc:  # pragma: no cover
                raise ValueError("Excel support requires pandas/openpyxl to be installed.") from exc
            file_storage.stream.seek(0)
            df = pd.read_excel(file_storage.stream)
            rows = df.where(pd.notna(df), None).to_dict(orient="records")

        return rows

    def row_value(row, *keys):
        norm = {normalize_bulk_key(k): v for k, v in row.items()}
        for key in keys:
            key_norm = normalize_bulk_key(key)
            if key_norm in norm and norm[key_norm] not in (None, ""):
                return norm[key_norm]
        return ""

    @app.route("/admin/employees/bulk-upload", methods=["GET", "POST"])
    @role_required("admin")
    def admin_bulk_import_employees():
        managers = User.query.filter(User.role == "manager", User.is_active == True) \
            .join(EmployeeProfile, EmployeeProfile.user_id == User.id).all()

        if request.method == "POST":
            file = request.files.get("file")
            if not file or not file.filename:
                flash("Choose a CSV or Excel file to import employees.", "danger")
                return render_template("admin/create_employee.html", managers=managers, form={})

            try:
                rows = read_bulk_employee_rows(file)
            except ValueError as exc:
                flash(str(exc), "danger")
                return render_template("admin/create_employee.html", managers=managers, form={})
            except Exception:
                flash("The file could not be read. Please check the format and try again.", "danger")
                return render_template("admin/create_employee.html", managers=managers, form={})

            created = 0
            skipped = 0
            for index, row in enumerate(rows, start=2):
                if row is None:
                    continue

                full_name = str(row_value(row, "full_name", "employee_name", "name", "fullName") or "").strip()
                if not full_name:
                    skipped += 1
                    continue

                code_value = row_value(row, "employee_code", "employeeid", "emp_code", "employee_id", "code")
                code = str(code_value).strip().upper() if code_value else next_employee_code()
                while User.query.filter_by(employee_code=code).first():
                    code = next_employee_code()

                designation = str(row_value(row, "designation", "job_title", "title") or "").strip()
                department = str(row_value(row, "department", "team", "division") or "").strip()
                date_of_joining = parse_bulk_date(row_value(row, "date_of_joining", "joining_date", "doj"))
                role = str(row_value(row, "role", "account_type") or "employee").strip().lower()
                role = "manager" if role in {"manager", "lead", "supervisor"} else "employee"
                temp_password = generate_temp_password()
                monthly_ctc = safe_float(row_value(row, "monthly_ctc", "ctc", "salary"))
                is_founding_member = parse_bulk_bool(row_value(row, "is_founding_member", "founding_member", "founding"))
                reporting_manager_value = row_value(row, "reporting_manager", "manager", "reporting_manager_name", "reporting_manager_employee_code", "manager_code")
                reporting_manager_id = resolve_manager_reference(reporting_manager_value)
                tenant_id = current_user.tenant_id
                if tenant_id is None and current_user.role == "admin":
                    internal_tenant = Tenant.query.filter_by(tenant_type="internal", is_active=True).first()
                    tenant_id = internal_tenant.id if internal_tenant else None
                if tenant_id is None:
                    abort(403, description="The creating user has no active tenant.")
                tenant = Tenant.query.get(tenant_id)
                if not tenant or not tenant.is_active:
                    abort(403, description="The target tenant is inactive.")
                if reporting_manager_id:
                    manager = User.query.filter_by(
                        id=reporting_manager_id,
                        role="manager",
                        tenant_id=tenant_id,
                        is_active=True,
                    ).first()
                    if not manager:
                        abort(403, description="The reporting manager must belong to the same active tenant.")

                user = User(
                    employee_code=code,
                    role=role,
                    must_change_password=True,
                    created_by_id=current_user.id,
                    tenant_id=tenant_id,
                )
                user.set_password(temp_password)
                db.session.add(user)
                db.session.flush()

                profile = EmployeeProfile(
                    user_id=user.id,
                    tenant_id=user.tenant_id,
                    full_name=full_name,
                    designation=designation,
                    department=department,
                    date_of_joining=date_of_joining,
                    reporting_manager_id=reporting_manager_id,
                    is_founding_member=is_founding_member,
                    monthly_ctc=monthly_ctc,
                )
                db.session.add(profile)

                if monthly_ctc > 0:
                    ctc = EmployeeCTC(
                        user_id=user.id,
                        monthly_ctc=monthly_ctc,
                        effective_from=date_of_joining or today_ist(),
                    )
                    db.session.add(ctc)

                created += 1

            db.session.commit()
            if created:
                flash(f"Imported {created} employee records successfully. {skipped} rows were skipped because they were blank.", "success")
            else:
                flash("No employee records were imported. Please check the file columns and values.", "warning")
            return redirect(url_for("admin_employee_list"))

        return render_template("admin/create_employee.html", managers=managers, form={})

    @app.route("/admin/employees/bulk-import-template")
    @role_required("admin")
    def admin_bulk_employee_template():
        template = (
            "full_name,designation,department,date_of_joining,role,reporting_manager,monthly_ctc,"
            "is_founding_member,temp_password\n"
            "Asha Verma,Senior Software Engineer,Engineering,2026-01-15,employee,Rajesh Kumar,65000,No," + generate_temp_password() + "\n"
            "Nikhil Shah,Team Lead,Engineering,2026-02-01,manager,,75000,Yes," + generate_temp_password() + "\n"
        )
        return Response(
            template,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=employee_bulk_upload_template.csv"},
        )

    # -----------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------
    @app.route("/", methods=["GET"])
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit(login_rate_limit, methods=["POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            employee_code = request.form.get("employee_code", "").strip().upper()
            password = request.form.get("password", "")

            user = User.query.filter_by(employee_code=employee_code).first()
            if user and user.locked_until and user.locked_until > now_ist():
                flash("Invalid employee ID or password.", "danger")
            elif user and not user.is_active:
                flash("Invalid employee ID or password.", "danger")
            elif user and user.check_password(password):
                user.failed_login_attempts = 0
                user.locked_until = None
                user.last_login_at = now_ist()
                db.session.commit()
                audit_event("login_success", target_user_id=user.id)
                login_user(user, remember=False, fresh=True)
                if user.must_change_password:
                    return redirect(url_for("change_password"))
                return redirect(url_for("dashboard"))
            else:
                if user:
                    user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
                    if user.failed_login_attempts >= max_login_attempts:
                        user.locked_until = now_ist() + timedelta(minutes=lockout_minutes)
                db.session.commit()
                audit_event("login_failure", target_user_id=user.id if user else None)
                flash("Invalid employee ID or password.", "danger")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        flash("You've been logged out.", "info")
        return redirect(url_for("login"))

    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    @limiter.limit(change_pw_rate_limit, methods=["POST"])
    def change_password():
        if request.method == "POST":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not current_user.check_password(current_password):
                flash("Current password is incorrect.", "danger")
            elif len(new_password) < 12:
                flash("Password must be at least 12 characters.", "danger")
            elif not any(char.isupper() for char in new_password):
                flash("Password must contain an uppercase letter.", "danger")
            elif not any(char.islower() for char in new_password):
                flash("Password must contain a lowercase letter.", "danger")
            elif not any(char.isdigit() for char in new_password):
                flash("Password must contain a number.", "danger")
            elif not any(char in "!@#$%^&*" for char in new_password):
                flash("Password must contain a special character.", "danger")
            elif current_user.check_password(new_password):
                flash("New password must be different.", "danger")
            elif new_password != confirm_password:
                flash("Passwords do not match.", "danger")
            else:
                current_user.set_password(new_password)
                current_user.must_change_password = False
                current_user.password_changed_at = now_ist()
                db.session.commit()
                audit_event("password_changed", target_user_id=current_user.id)
                flash("Password updated successfully.", "success")
                return redirect(url_for("dashboard"))
        return render_template("change_password.html")

    # -----------------------------------------------------------------
    # Dashboard router — sends each role to the right landing page
    # -----------------------------------------------------------------
    @app.route("/dashboard")
    @login_required
    def dashboard():
        if current_user.role in ["admin", "demo_admin"]:
            return redirect(url_for("admin_dashboard"))
        elif current_user.role == "manager":
            return redirect(url_for("manager_dashboard"))
        else:
            return redirect(url_for("employee_dashboard"))

    def overview_context():
        """Build the shared people-and-culture content shown on every overview."""
        today = today_ist()
        month_start = datetime(today.year, today.month, 1).date()
        workforce_roles = ["employee", "manager"]
        active_headcount = scope_to_practice_data(User.query.filter(
            User.role.in_(workforce_roles), User.is_active == True
        )).count()
        current_attendance_query = Attendance.query.join(User, User.id == Attendance.user_id).filter(
            Attendance.date == today,
            User.role.in_(workforce_roles),
            User.is_active == True,
        )
        if current_user.role != "admin" and current_user.tenant_id:
            current_attendance_query = current_attendance_query.filter(Attendance.tenant_id == current_user.tenant_id)
        current_attendance = current_attendance_query.all()
        attendance_marked = len(current_attendance)
        attendance_present = sum(
            0.5 if record.status == "Half Day" else 1.0
            for record in current_attendance
            if record.status in {"Present", "Late", "Half Day"}
        )
        attendance_rate = round((attendance_present / active_headcount) * 100, 1) if active_headcount else 0.0
        open_positions_query = JobRequisition.query.filter(
            JobRequisition.status.notin_(["Rejected", "Closed"])
        )
        if current_user.role != "admin" and current_user.tenant_id:
            open_positions_query = open_positions_query.filter(JobRequisition.tenant_id == current_user.tenant_id)
        open_positions = sum(item.number_of_positions or 0 for item in open_positions_query.all())
        new_joiners_query = scope_to_practice_data(User.query.filter(
            User.role.in_(workforce_roles),
            User.is_active == True,
        )).join(EmployeeProfile, EmployeeProfile.user_id == User.id).filter(
            EmployeeProfile.date_of_joining >= month_start,
            EmployeeProfile.date_of_joining <= today,
        )
        new_joiners = new_joiners_query.count()
        exits = scope_to_practice_data(User.query.filter(
            User.role.in_(workforce_roles),
            User.deactivated_at >= datetime.combine(month_start, datetime.min.time()),
            User.deactivated_at <= now_ist(),
        )).count()
        workforce_ids = [person.id for person in scope_to_practice_data(
            User.query.filter(User.role.in_(workforce_roles), User.is_active == True)
        ).all()]
        appraisal_base = Appraisal.query.join(User, User.id == Appraisal.user_id).filter(
            Appraisal.year == today.year,
            Appraisal.status != "Archived",
            Appraisal.user_id.in_(workforce_ids or [-1]),
        )
        if current_user.role != "admin" and current_user.tenant_id:
            appraisal_base = appraisal_base.filter(Appraisal.tenant_id == current_user.tenant_id)
        appraisal_total = appraisal_base.count()
        appraisal_completed = appraisal_base.filter(Appraisal.status == "Completed").count()
        appraisal_kra_approved = appraisal_base.filter(
            Appraisal.status.in_(["KRA Approved (Eligible for Rating)", "Rating Submitted", "Completed"])
        ).count()
        workforce_trend = []
        for month_offset in range(5, -1, -1):
            month_index = today.month - month_offset
            year_value = today.year
            while month_index <= 0:
                month_index += 12
                year_value -= 1
            period_start = datetime(year_value, month_index, 1)
            next_month = month_index + 1
            next_year = year_value
            if next_month == 13:
                next_month = 1
                next_year += 1
            period_end = datetime(next_year, next_month, 1)
            population_query = scope_to_practice_data(User.query.filter(
                User.role.in_(["employee", "manager"]),
            ))
            opening_count = population_query.join(EmployeeProfile, EmployeeProfile.user_id == User.id).filter(
                EmployeeProfile.date_of_joining < period_start.date(),
                db.or_(User.deactivated_at.is_(None), User.deactivated_at >= period_start),
            ).count()
            joiner_count = population_query.join(EmployeeProfile, EmployeeProfile.user_id == User.id).filter(
                EmployeeProfile.date_of_joining >= period_start.date(),
                EmployeeProfile.date_of_joining < period_end.date(),
            ).count()
            exit_count = population_query.filter(
                User.deactivated_at >= period_start,
                User.deactivated_at < period_end,
            ).count()
            closing_count = population_query.join(EmployeeProfile, EmployeeProfile.user_id == User.id).filter(
                EmployeeProfile.date_of_joining < period_end.date(),
                User.is_active == True,
                db.or_(User.deactivated_at.is_(None), User.deactivated_at >= period_end),
            ).count()
            if period_start.date() == month_start:
                closing_count = active_headcount
            workforce_trend.append({
                "label": period_start.strftime("%b"),
                "opening": opening_count,
                "joiners": joiner_count,
                "exits": exit_count,
                "closing": closing_count,
            })
        now = now_ist()
        window_end = today + timedelta(days=31)
        people_query = scope_to_practice_data(User.query).join(
            EmployeeProfile, EmployeeProfile.user_id == User.id
        ).filter(
            User.role.in_(["admin", "demo_admin", "manager", "employee"]),
            User.is_active == True,
        )
        if current_user.role == "manager":
            people_query = people_query.filter(
                db.or_(User.id == current_user.id, EmployeeProfile.reporting_manager_id == current_user.id)
            )
        elif current_user.role == "employee":
            people_query = people_query.filter(User.id == current_user.id)
        people = people_query.all()

        birthdays = []
        for person in people:
            if not person.profile or not person.profile.date_of_birth:
                continue
            birthday = person.profile.date_of_birth.replace(year=today.year)
            if birthday < today:
                birthday = birthday.replace(year=today.year + 1)
            if birthday <= window_end:
                birthdays.append((birthday, person))
        birthdays.sort(key=lambda item: item[0])

        important_dates = ImportantDate.query.join(User, User.id == ImportantDate.created_by_id)
        if current_user.role != "admin" and current_user.tenant_id:
            important_dates = important_dates.filter(User.tenant_id == current_user.tenant_id)
        important_dates = important_dates.order_by(ImportantDate.date.asc()).all()
        upcoming_dates = [item for item in important_dates if 0 <= (item.date - today).days <= 31]
        upcoming_dates.sort(key=lambda item: item.date)
        recognitions = Recognition.query.join(User, User.id == Recognition.sender_id)
        if current_user.role != "admin" and current_user.tenant_id:
            recognitions = recognitions.filter(User.tenant_id == current_user.tenant_id)
        recognitions = recognitions.filter(Recognition.created_at >= now - timedelta(hours=24)).order_by(Recognition.created_at.desc()).limit(6).all()

        active_people_query = scope_to_practice_data(User.query.filter(
            User.role.in_(workforce_roles), User.is_active == True
        ))
        inactive_people_query = scope_to_practice_data(User.query.filter(
            User.role.in_(workforce_roles), User.is_active == False
        ))
        active_count = active_people_query.count()
        inactive_count = inactive_people_query.count()
        month_start = datetime(today.year, today.month, 1)
        inactive_this_period = inactive_people_query.filter(User.deactivated_at >= month_start).count()
        period_population = active_count + inactive_this_period
        attrition_rate = round((inactive_this_period / period_population) * 100, 1) if period_population else 0.0
        recent_added = active_people_query.join(EmployeeProfile, EmployeeProfile.user_id == User.id).filter(
            EmployeeProfile.date_of_joining >= month_start,
            EmployeeProfile.date_of_joining <= today,
        ).order_by(EmployeeProfile.date_of_joining.desc()).limit(5).all()
        updated_profiles = [person.profile for person in people if person.profile and person.profile.updated_at]
        latest_employee_update = max((profile.updated_at for profile in updated_profiles), default=None)
        return {
            "upcoming_birthdays": [person for _, person in birthdays[:6]],
            "upcoming_dates": upcoming_dates,
            "recent_recognitions": recognitions,
            "recent_added": recent_added,
            "latest_employee_update": latest_employee_update,
            "attrition_rate": attrition_rate,
            "attrition_left": inactive_count,
            "attrition_period_left": inactive_this_period,
            "attrition_active": active_count,
            "active_headcount": active_headcount,
            "attendance_rate": attendance_rate,
            "attendance_marked": attendance_marked,
            "open_positions": open_positions,
            "new_joiners": new_joiners,
            "exits": exits,
            "appraisal_total": appraisal_total,
            "appraisal_completed": appraisal_completed,
            "appraisal_kra_approved": appraisal_kra_approved,
            "workforce_trend": workforce_trend,
        }

    # -----------------------------------------------------------------
    # ADMIN routes
    # -----------------------------------------------------------------
    @app.route("/admin")
    @role_required("admin")
    def admin_dashboard():
        employee_query = scope_to_practice_data(User.query.filter(
            User.role == "employee",
            User.is_active == True,
        ))
        manager_query = scope_to_practice_data(User.query.filter(
            User.role == "manager",
            User.is_active == True,
        ))
        active_headcount = scope_to_practice_data(User.query.filter(
            User.role.in_(["employee", "manager"]), User.is_active == True
        )).count()
        completed = scope_to_practice_data(
            EmployeeProfile.query.join(User, User.id == EmployeeProfile.user_id).filter(
                User.role.in_(["employee", "manager"]),
                User.is_active == True,
                EmployeeProfile.profile_completed == True,
            )
        ).count()
        pending = max(active_headcount - completed, 0)
        recent = scope_to_practice_data(
            User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
        ).order_by(User.id.desc()).limit(5).all()
        total_employees = employee_query.count()
        total_managers = manager_query.count()
        pending_leave_query = LeaveApplication.query.filter_by(status="pending")
        if current_user.role != "admin" and current_user.tenant_id:
            pending_leave_query = pending_leave_query.filter(
                LeaveApplication.tenant_id == current_user.tenant_id
            )
        pending_leaves = pending_leave_query.count()
        workforce = scope_to_practice_data(
            User.query.filter(
                User.role.in_(["employee", "manager"]),
                User.is_active == True,
            )
        ).join(EmployeeProfile, EmployeeProfile.user_id == User.id).all()

        def grouped_counts(values):
            counts = {}
            for value in values:
                label = (value or "Not specified").strip() or "Not specified"
                counts[label] = counts.get(label, 0) + 1
            return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:8]

        today = today_ist()
        today_attendance_query = Attendance.query.join(User, User.id == Attendance.user_id).filter(
            Attendance.date == today,
            User.role.in_(["employee", "manager"]),
            User.is_active == True,
        )
        today_leave_query = LeaveApplication.query.filter(
            LeaveApplication.status == "approved",
            LeaveApplication.start_date <= today,
            LeaveApplication.end_date >= today,
        )
        today_leave_marked_query = LeaveApplication.query.filter(
            LeaveApplication.start_date <= today,
            LeaveApplication.end_date >= today,
        )
        if current_user.role != "admin" and current_user.tenant_id:
            today_attendance_query = today_attendance_query.filter(Attendance.tenant_id == current_user.tenant_id)
            today_leave_query = today_leave_query.filter(LeaveApplication.tenant_id == current_user.tenant_id)
            today_leave_marked_query = today_leave_marked_query.filter(LeaveApplication.tenant_id == current_user.tenant_id)
        today_attendance = today_attendance_query.all()
        attendance_status_counts = grouped_counts([record.status for record in today_attendance])
        today_leave_count = today_leave_query.count()
        today_leave_marked_count = today_leave_marked_query.count()
        return render_template("admin/dashboard.html", **overview_context(),
                    total_employees=total_employees,
                                total_managers=total_managers, completed=completed,
                                pending=max(pending, 0), recent=recent,
                                pending_leaves=pending_leaves,
                                department_counts=grouped_counts([person.profile.department for person in workforce]),
                                designation_counts=grouped_counts([person.profile.designation for person in workforce]),
                                gender_counts=grouped_counts([person.profile.gender for person in workforce]),
                                today_attendance_count=len(today_attendance),
                                attendance_status_counts=attendance_status_counts,
                                today_leave_count=today_leave_count,
                                today_leave_marked_count=today_leave_marked_count,
                                overview_today=today)

    @app.route("/admin/employees", methods=["GET", "POST"])
    @role_required("admin")
    def admin_employee_list():
        q = request.args.get("q", "").strip()
        show_inactive = request.args.get("show_inactive") == "1"
        query = User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
        if not show_inactive:
            query = query.filter(User.is_active == True)
        query = scope_to_practice_data(query)
        if q:
            query = query.join(EmployeeProfile, EmployeeProfile.user_id == User.id).filter(
                db.or_(EmployeeProfile.full_name.ilike(f"%{q}%"),
                       User.employee_code.ilike(f"%{q}%"))
            )
        people = query.order_by(User.id.desc()).all()
        selected_ids = set()
        if request.method == "POST":
            allowed_ids = {person.id for person in people}
            for raw_id in request.form.getlist("selected_user_ids"):
                try:
                    user_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if user_id in allowed_ids:
                    selected_ids.add(user_id)
            flash(f"{len(selected_ids)} selected.", "success")
        return render_template(
            "admin/employee_list.html",
            people=people,
            q=q,
            show_inactive=show_inactive,
            selected_ids=selected_ids,
        )

    @app.route("/admin/employees/new", methods=["GET", "POST"])
    @role_required("admin")
    def admin_create_employee():
        managers = scope_to_practice_data(User.query.filter(User.role == "manager", User.is_active == True)) \
            .join(EmployeeProfile, EmployeeProfile.user_id == User.id).all()

        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            designation = request.form.get("designation", "").strip()
            department = request.form.get("department", "").strip()
            date_of_joining = parse_date(request.form.get("date_of_joining"))
            reporting_manager_id = request.form.get("reporting_manager_id") or None
            role = request.form.get("role", "employee").strip().lower()
            allowed_roles = {"employee", "manager"}
            if current_user.role == "admin":
                allowed_roles.add("demo_admin")
            if role not in allowed_roles:
                abort(403)
            temp_password = request.form.get("temp_password", "").strip() or generate_temp_password()
            password_error = password_policy_error(temp_password)
            if password_error:
                flash(password_error, "danger")
                return render_template("admin/create_employee.html", managers=managers, form=request.form)
            is_founding_member = request.form.get("is_founding_member") == "on"
            monthly_ctc = float(request.form.get("monthly_ctc") or 0.0)
            tax_regime = request.form.get("tax_regime", "new_regime")
            if tax_regime not in {"old_regime", "new_regime"}:
                tax_regime = "new_regime"
            date_of_birth = parse_date(request.form.get("date_of_birth"))
            gender = request.form.get("gender", "").strip()
            personal_email = request.form.get("personal_email", "").strip()
            phone_number = request.form.get("phone_number", "").strip()
            blood_group = request.form.get("blood_group", "").strip()
            pan_number = request.form.get("pan_number", "").strip().upper()
            aadhaar_number = request.form.get("aadhaar_number", "").strip()
            tax_regime = request.form.get("tax_regime", "new_regime")
            if tax_regime not in {"old_regime", "new_regime"}:
                tax_regime = "new_regime"

            if role == "demo_admin" and not full_name:
                full_name = "Demo Admin"

            if not full_name:
                flash("Employee name is required.", "danger")
                return render_template("admin/create_employee.html", managers=managers,
                                        form=request.form)

            code = next_employee_code()
            tenant_id = current_user.tenant_id
            if role == "demo_admin" and current_user.role == "admin":
                demo_tenant = Tenant(
                    company_name=f"{full_name} Practice",
                    tenant_type="demo",
                    subscription_status="trial",
                )
                db.session.add(demo_tenant)
                db.session.flush()
                tenant_id = demo_tenant.id
            if tenant_id is None and role != "admin":
                internal_tenant = Tenant.query.filter_by(tenant_type="internal").first()
                tenant_id = internal_tenant.id if internal_tenant else None
            if reporting_manager_id:
                manager = User.query.filter_by(
                    id=int(reporting_manager_id),
                    role="manager",
                    tenant_id=tenant_id,
                    is_active=True,
                ).first()
                if manager is None:
                    abort(403)

            user = User(
                employee_code=code,
                role=role,
                must_change_password=True,
                created_by_id=current_user.id,
                tenant_id=tenant_id,
            )
            user.set_password(temp_password)
            db.session.add(user)
            db.session.flush()

            profile = EmployeeProfile(
                user_id=user.id,
                tenant_id=user.tenant_id,
                full_name=full_name,
                designation=designation,
                department=department,
                date_of_joining=date_of_joining,
                reporting_manager_id=int(reporting_manager_id) if reporting_manager_id else None,
                is_founding_member=is_founding_member,
                monthly_ctc=monthly_ctc,
            )
            db.session.add(profile)
            picture = request.files.get("profile_picture")
            if picture and picture.filename:
                try:
                    profile.profile_picture_filename = save_private_upload(
                        picture, prefix=f"profile_{user.id}",
                        allowed_extensions={".png", ".jpg", ".jpeg", ".webp"},
                    )
                except UploadRejected as exc:
                    db.session.rollback()
                    flash(str(exc), "danger")
                    return render_template("admin/create_employee.html", managers=managers, form=request.form)

            if monthly_ctc > 0:
                ctc = EmployeeCTC(
                    user_id=user.id,
                    monthly_ctc=monthly_ctc,
                    effective_from=date_of_joining or today_ist()
                )
                db.session.add(ctc)

            db.session.commit()

            flash(f"Employee created. Login ID: {code} | Temp password: {temp_password}", "success")
            return redirect(url_for("admin_employee_list"))

        return render_template("admin/create_employee.html", managers=managers, form={})

    @app.route("/admin/employees/<int:user_id>")
    @role_required("admin")
    def admin_view_employee(user_id):
        user = User.query.get_or_404(user_id)
        if current_user.role == "demo_admin" and user.created_by_id != current_user.id:
            abort(403)
        if not user.profile:
            abort(404)
        audit_event("profile_view", target_user_id=user.id)
        return render_template("profile_view.html", person=user, profile=user.profile,
                                viewer="admin")

    @app.route("/admin/employees/<int:user_id>/reset-password", methods=["POST"])
    @role_required("admin")
    def admin_reset_password(user_id):
        user = scoped_user_or_403(user_id)
        if not user.is_active:
            flash("Deactivated accounts cannot receive password resets. Reactivate the account first.", "danger")
            return redirect(url_for("admin_employee_list"))
        if user.role == "admin" and current_user.role != "admin":
            abort(403)
        new_temp = request.form.get("temp_password", "").strip() or generate_temp_password()
        password_error = password_policy_error(new_temp)
        if password_error:
            flash(password_error, "danger")
            return redirect(url_for("admin_employee_list"))
        user.set_password(new_temp)
        user.must_change_password = True
        user.password_reset_at = now_ist()
        db.session.commit()
        audit_event("password_reset", target_user_id=user.id)
        return render_template(
            "admin/reset_password_result.html",
            employee_code=user.employee_code,
            temporary_password=new_temp,
        )

    @app.route("/admin/employees/<int:user_id>/edit", methods=["GET", "POST"])
    @role_required("admin")
    def admin_edit_employee(user_id):
        user = User.query.get_or_404(user_id)
        if current_user.role == "demo_admin" and user.created_by_id != current_user.id:
            abort(403)
        if not user.profile:
            abort(404)

        managers = User.query.filter(
            User.role == "manager", User.id != user.id,
            db.or_(User.is_active == True, User.id == user.profile.reporting_manager_id)
        ).join(EmployeeProfile, EmployeeProfile.user_id == User.id).all()

        if request.method == "POST":
            new_code = request.form.get("employee_code", "").strip().upper()
            full_name = request.form.get("full_name", "").strip()
            designation = request.form.get("designation", "").strip()
            department = request.form.get("department", "").strip()
            date_of_joining = parse_date(request.form.get("date_of_joining"))
            reporting_manager_id = request.form.get("reporting_manager_id") or None
            new_role = request.form.get("role", user.role).strip().lower()
            allowed_roles = {"employee", "manager"}
            if current_user.role == "admin":
                allowed_roles.add("demo_admin")
            if new_role not in allowed_roles:
                abort(403)
            if reporting_manager_id:
                manager = User.query.filter_by(
                    id=int(reporting_manager_id),
                    role="manager",
                    tenant_id=user.tenant_id,
                    is_active=True,
                ).first()
                if manager is None:
                    abort(403)
            is_founding_member = request.form.get("is_founding_member") == "on"
            monthly_ctc = float(request.form.get("monthly_ctc") or 0.0)
            tax_regime = request.form.get("tax_regime", "new_regime")
            if tax_regime not in {"old_regime", "new_regime"}:
                tax_regime = "new_regime"
            date_of_birth = parse_date(request.form.get("date_of_birth"))
            gender = request.form.get("gender", "").strip()
            personal_email = request.form.get("personal_email", "").strip()
            phone_number = request.form.get("phone_number", "").strip()
            blood_group = request.form.get("blood_group", "").strip()
            pan_number = request.form.get("pan_number", "").strip().upper()
            aadhaar_number = request.form.get("aadhaar_number", "").strip()

            # demo_admin must not overwrite sensitive identity / financial fields.
            if current_user.role == "demo_admin":
                pan_number = ""
                aadhaar_number = ""

            if new_role == "demo_admin" and not full_name:
                full_name = "Demo Admin"

            if not new_code or not full_name:
                flash("Employee ID and name are both required.", "danger")
                return render_template("admin/edit_employee.html", user=user, managers=managers,
                                        form=request.form)

            clash = User.query.filter(User.employee_code == new_code, User.id != user.id).first()
            if clash:
                flash(f"Employee ID {new_code} is already used by someone else.", "danger")
                return render_template("admin/edit_employee.html", user=user, managers=managers,
                                        form=request.form)

            if reporting_manager_id and int(reporting_manager_id) == user.id:
                flash("Someone can't be their own reporting manager.", "danger")
                return render_template("admin/edit_employee.html", user=user, managers=managers,
                                        form=request.form)

            user.employee_code = new_code
            user.role = new_role
            user.profile.full_name = full_name
            user.profile.designation = designation
            user.profile.department = department
            user.profile.date_of_joining = date_of_joining
            user.profile.reporting_manager_id = int(reporting_manager_id) if reporting_manager_id else None
            user.profile.is_founding_member = is_founding_member
            user.profile.monthly_ctc = monthly_ctc
            user.profile.tax_regime = tax_regime
            user.profile.date_of_birth = date_of_birth
            user.profile.gender = gender
            user.profile.personal_email = personal_email
            user.profile.phone_number = phone_number
            user.profile.blood_group = blood_group
            user.profile.pan_number = encrypt_field(pan_number) if pan_number else user.profile.pan_number
            user.profile.aadhaar_number = encrypt_field(aadhaar_number) if aadhaar_number else user.profile.aadhaar_number
            for field_name, profile_field, prefix in (
                ("pan_document", "pan_document_filename", "pan"),
                ("aadhaar_document", "aadhaar_document_filename", "aadhaar"),
                ("cancelled_cheque", "cancelled_cheque_filename", "cheque"),
            ):
                # demo_admin must not replace sensitive identity / financial documents.
                if current_user.role == "demo_admin":
                    continue
                document = request.files.get(field_name)
                if document and document.filename:
                    try:
                        setattr(user.profile, profile_field, save_private_upload(
                            document, prefix=f"{prefix}_{user.id}",
                            allowed_extensions={".pdf", ".png", ".jpg", ".jpeg", ".webp"},
                        ))
                    except UploadRejected as exc:
                        flash(f"{field_name.replace('_', ' ').title()}: {exc}", "danger")
                        return render_template("admin/edit_employee.html", user=user, managers=managers, form=request.form)
            picture = request.files.get("profile_picture")
            if picture and picture.filename:
                try:
                    user.profile.profile_picture_filename = save_private_upload(
                        picture, prefix=f"profile_{user.id}",
                        allowed_extensions={".png", ".jpg", ".jpeg", ".webp"},
                    )
                except UploadRejected as exc:
                    flash(str(exc), "danger")
                    return render_template("admin/edit_employee.html", user=user, managers=managers, form=request.form)
            user.profile.current_address_line1 = request.form.get("current_address_line1", "").strip()
            user.profile.current_address_line2 = request.form.get("current_address_line2", "").strip()
            user.profile.current_city = request.form.get("current_city", "").strip()
            user.profile.current_state = request.form.get("current_state", "").strip()
            user.profile.current_pincode = request.form.get("current_pincode", "").strip()
            bank_account_number = request.form.get("bank_account_number", "").strip()
            user.profile.bank_account_number = encrypt_field(bank_account_number) if bank_account_number else user.profile.bank_account_number
            user.profile.bank_ifsc_code = request.form.get("bank_ifsc_code", "").strip().upper()
            user.profile.bank_name = request.form.get("bank_name", "").strip()
            user.profile.bank_branch = request.form.get("bank_branch", "").strip()

            if monthly_ctc > 0:
                ctc = EmployeeCTC.query.filter_by(user_id=user.id).first()
                if ctc:
                    ctc.monthly_ctc = monthly_ctc
                    ctc.effective_from = date_of_joining or today_ist()
                else:
                    ctc = EmployeeCTC(
                        user_id=user.id,
                        monthly_ctc=monthly_ctc,
                        effective_from=date_of_joining or today_ist()
                    )
                    db.session.add(ctc)

            db.session.commit()

            flash(f"{full_name}'s details have been updated.", "success")
            return redirect(url_for("admin_view_employee", user_id=user.id))

        return render_template("admin/edit_employee.html", user=user, managers=managers, form=None)

    @app.route("/admin/employees/<int:user_id>/deactivate", methods=["POST"])
    @role_required("admin")
    def admin_deactivate_employee(user_id):
        user = User.query.get_or_404(user_id)
        if current_user.role != "admin" and user.tenant_id != current_user.tenant_id:
            abort(403)
        if user.role == "admin":
            abort(403)
        if not user.profile:
            abort(404)

        if user.role == "manager":
            team_size = EmployeeProfile.query.filter_by(
                reporting_manager_id=user.id,
                tenant_id=user.tenant_id,
            ).count()
            if team_size > 0:
                noun = "person" if team_size == 1 else "people"
                verb = "reports" if team_size == 1 else "report"
                flash(f"Can't deactivate {user.profile.full_name} — {team_size} "
                      f"{noun} still {verb} to them. "
                      f"Reassign their team first.", "danger")
                return redirect(url_for("admin_employee_list"))

        user.is_active = False
        user.deactivated_at = now_ist()
        if user.role == "demo_admin":
            User.query.filter(
                User.created_by_id == user.id,
                User.tenant_id == user.tenant_id,
            ).update({"is_active": False, "deactivated_at": now_ist()})
        audit_event("account_deactivated", target_user_id=user.id)

        last_payslip = Payslip.query.filter_by(user_id=user.id).order_by(
            Payslip.year.desc(), Payslip.month.desc()
        ).first()
        last_drawn_basic_da = None
        if last_payslip:
            basic_line = next(
                (line for line in last_payslip.lines
                 if line.component_name.lower().startswith("basic")),
                None,
            )
            if basic_line:
                last_drawn_basic_da = basic_line.amount
        if last_drawn_basic_da is None:
            active_ctc = get_active_ctc(user.id, as_of_date=today_ist())
            last_drawn_basic_da = active_ctc.monthly_ctc * 0.5 if active_ctc else 0.0

        gratuity_result = calculate_gratuity(
            user.profile.date_of_joining,
            today_ist(),
            last_drawn_basic_da,
        )
        db.session.add(GratuityRecord(
            tenant_id=user.tenant_id,
            user_id=user.id,
            date_of_joining=user.profile.date_of_joining,
            exit_date=today_ist(),
            completed_years=gratuity_result["completed_years"],
            last_drawn_basic_da=last_drawn_basic_da,
            is_eligible=gratuity_result["is_eligible"],
            ineligibility_reason=gratuity_result["ineligibility_reason"],
            gratuity_amount=gratuity_result["gratuity_amount"],
            tax_exempt_amount=gratuity_result["tax_exempt_amount"],
            taxable_amount=gratuity_result["taxable_amount"],
            status="pending_review",
        ))
        db.session.commit()
        flash(f"{user.profile.full_name} has been deactivated and can no longer log in.", "success")
        return redirect(url_for("admin_employee_list"))

    @app.route("/admin/employees/<int:user_id>/reactivate", methods=["POST"])
    @role_required("admin")
    def admin_reactivate_employee(user_id):
        user = User.query.get_or_404(user_id)
        if current_user.role != "admin" and user.tenant_id != current_user.tenant_id:
            abort(403)
        if user.role == "admin":
            abort(403)
        if not user.profile:
            abort(404)
        user.is_active = True
        user.deactivated_at = None
        db.session.commit()
        flash(f"{user.profile.full_name} has been reactivated.", "success")
        return redirect(url_for("admin_employee_list"))

    @app.route("/admin/employees/<int:user_id>/delete", methods=["POST"])
    @role_required("admin")
    def admin_delete_employee(user_id):
        # Permanent deletion of production records is restricted to real admins.
        real_admin_only()
        user = User.query.get_or_404(user_id)
        if current_user.role != "admin" and user.tenant_id != current_user.tenant_id:
            abort(403)
        if user.id == current_user.id or user.role == "admin":
            flash("Platform administrator accounts cannot be deleted.", "danger")
            return redirect(url_for("admin_employee_list"))

        reports = EmployeeProfile.query.filter_by(reporting_manager_id=user.id).count()
        if reports:
            flash("This manager cannot be deleted while employees still report to them.", "danger")
            return redirect(url_for("admin_employee_list", show_inactive=1))

        target_id = user.id
        upload_files = []
        if user.profile and user.profile.relieving_letter_filename:
            upload_files.append(user.profile.relieving_letter_filename)
        for profile_document in (
            user.profile.pan_document_filename if user.profile else None,
            user.profile.aadhaar_document_filename if user.profile else None,
            user.profile.cancelled_cheque_filename if user.profile else None,
            user.profile.profile_picture_filename if user.profile else None,
        ):
            if profile_document:
                upload_files.append(profile_document)
        upload_files.extend(
            declaration.document_filename
            for declaration in InvestmentDeclaration.query.filter_by(user_id=target_id).all()
            if declaration.document_filename
        )

        # Preserve shared audit/history records while removing references to the user.
        AuditLog.query.filter(AuditLog.user_id == target_id).update({"user_id": None}, synchronize_session=False)
        AuditLog.query.filter(AuditLog.target_user_id == target_id).update({"target_user_id": None}, synchronize_session=False)
        User.query.filter(User.created_by_id == target_id).update({"created_by_id": None}, synchronize_session=False)
        db.session.expire_all()
        EmployeeProfile.query.filter(EmployeeProfile.reporting_manager_id == target_id).update(
            {"reporting_manager_id": None}, synchronize_session=False
        )
        Appraisal.query.filter(Appraisal.evaluator_id == target_id).update({"evaluator_id": None}, synchronize_session=False)
        LeaveApplication.query.filter(LeaveApplication.decided_by_id == target_id).update({"decided_by_id": None}, synchronize_session=False)
        SpecialApproval.query.filter(SpecialApproval.approver_id == target_id).update({"approver_id": None}, synchronize_session=False)
        AttendanceRegularization.query.filter(AttendanceRegularization.approver_id == target_id).update({"approver_id": None}, synchronize_session=False)
        AppraisalMeeting.query.filter(AppraisalMeeting.employee_id == target_id).update({"employee_id": None}, synchronize_session=False)
        AppraisalMeeting.query.filter(AppraisalMeeting.created_by_id == target_id).update({"created_by_id": current_user.id}, synchronize_session=False)
        ResourceDocument.query.filter(ResourceDocument.uploaded_by_id == target_id).update({"uploaded_by_id": current_user.id}, synchronize_session=False)
        HRMemory.query.filter(HRMemory.created_by_id == target_id).update({"created_by_id": current_user.id}, synchronize_session=False)
        ImportantDate.query.filter(ImportantDate.created_by_id == target_id).update({"created_by_id": current_user.id}, synchronize_session=False)
        JobRequisition.query.filter(JobRequisition.requested_by_id == target_id).update({"requested_by_id": current_user.id}, synchronize_session=False)
        JobRequisition.query.filter(JobRequisition.approval_level_1_manager_id == target_id).update({"approval_level_1_manager_id": None}, synchronize_session=False)
        CandidateInterview.query.filter(CandidateInterview.interviewer_id == target_id).update({"interviewer_id": current_user.id}, synchronize_session=False)

        # These records are employee-owned or contain a non-null employee FK.
        Recognition.query.filter(db.or_(Recognition.sender_id == target_id, Recognition.recipient_id == target_id)).delete(synchronize_session=False)
        for model in (Child, EmployeeCTCLine, PayslipLine, AttendanceSession, AppraisalKPI, AppraisalKRA,
                      LeaveApplication, LeaveBalance, EmployeePayrollAdjustment, SalaryAdvance,
                      InvestmentDeclaration, Payslip, Attendance, SpecialApproval,
                      AttendanceRegularization, Appraisal, EmployeeCTC, EmployeeLetter):
            if model is Child:
                query = model.query.join(EmployeeProfile, Child.profile_id == EmployeeProfile.id).filter(EmployeeProfile.user_id == target_id)
            elif model is EmployeeCTCLine:
                query = model.query.join(EmployeeCTC, EmployeeCTCLine.employee_ctc_id == EmployeeCTC.id).filter(EmployeeCTC.user_id == target_id)
            elif model is PayslipLine:
                query = model.query.join(Payslip, PayslipLine.payslip_id == Payslip.id).filter(Payslip.user_id == target_id)
            elif model is AttendanceSession:
                query = model.query.join(Attendance, AttendanceSession.attendance_id == Attendance.id).filter(Attendance.user_id == target_id)
            elif model in (AppraisalKPI, AppraisalKRA):
                if model is AppraisalKPI:
                    query = model.query.join(AppraisalKRA, AppraisalKPI.kra_id == AppraisalKRA.id).join(
                        Appraisal, AppraisalKRA.appraisal_id == Appraisal.id
                    ).filter(Appraisal.user_id == target_id)
                else:
                    query = model.query.join(Appraisal, AppraisalKRA.appraisal_id == Appraisal.id).filter(Appraisal.user_id == target_id)
            elif hasattr(model, "user_id"):
                query = model.query.filter(model.user_id == target_id)
            else:
                query = model.query.filter(model.employee_id == target_id)
            for record in query.all():
                db.session.delete(record)

        if user.profile:
            db.session.delete(user.profile)
        db.session.delete(user)
        db.session.commit()

        for filename in upload_files:
            path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            if os.path.isfile(path):
                os.remove(path)
        flash("Employee and employee-owned records were permanently deleted.", "success")

        return redirect(url_for("admin_employee_list"))

    @app.route("/admin/employees/bulk-action", methods=["POST"])
    @role_required("admin")
    def admin_bulk_employee_action():
        # Bulk deletion of production records is restricted to real admins.
        real_admin_only()
        action = request.form.get("action")
        selected_ids = request.form.getlist("selected_user_ids")
        if action != "delete":
            flash("Select a valid bulk action.", "danger")
            return redirect(url_for("admin_employee_list"))
        if not selected_ids:
            flash("Please select at least one record.", "warning")
            return redirect(url_for("admin_employee_list"))

        try:
            selected_ids = list(dict.fromkeys(int(raw_id) for raw_id in selected_ids))
        except (TypeError, ValueError):
            flash("The selected records are invalid.", "danger")
            return redirect(url_for("admin_employee_list"))

        selected_users = User.query.filter(User.id.in_(selected_ids)).all()
        if {user.id for user in selected_users} != set(selected_ids):
            flash("One or more selected records could not be found.", "danger")
            return redirect(url_for("admin_employee_list"))

        deleted = 0
        blocked = 0
        for user_id in selected_ids:
            try:
                result = admin_delete_employee(user_id)
            except (ValueError, TypeError):
                blocked += 1
                continue
            if isinstance(result, Response):
                # The guarded single-record route flashes its own reason for
                # protected users or managers with direct reports.
                if User.query.get(user_id) is None:
                    deleted += 1
                else:
                    blocked += 1
        flash(f"Bulk delete completed: {deleted} employee(s) deleted, {blocked} skipped.", "success" if deleted else "warning")
        return redirect(url_for("admin_employee_list"))

    @app.route("/admin/managers")
    @role_required("admin")
    def admin_manager_list():
        managers = scope_to_practice_data(User.query.filter(
            User.role == "manager",
            User.is_active == True,
        )).all()
        team_counts = {
            m.id: scope_to_practice_data(EmployeeProfile.query.filter_by(reporting_manager_id=m.id).join(User, User.id == EmployeeProfile.user_id)).count()
            for m in managers
        }
        return render_template("admin/manager_list.html", managers=managers, team_counts=team_counts)

    # -----------------------------------------------------------------
    # MANAGER routes
    # -----------------------------------------------------------------
    @app.route("/manager")
    @role_required("manager")
    def manager_dashboard():
        profiles = EmployeeProfile.query.filter_by(
            reporting_manager_id=current_user.id,
            tenant_id=current_user.tenant_id,
        ).all()
        team = profiles
        completed = sum(1 for p in profiles if p.profile_completed)
        team_ids = [p.user_id for p in profiles]
        pending_leaves = LeaveApplication.query.filter(
            LeaveApplication.user_id.in_(team_ids or [-1]),
            LeaveApplication.tenant_id == current_user.tenant_id,
            LeaveApplication.status == "pending"
        ).count()
        return render_template("manager/dashboard.html", **overview_context(), team=team,
                                completed=completed, pending=len(profiles) - completed,
                                pending_leaves=pending_leaves)

    @app.route("/manager/team")
    @role_required("manager")
    def manager_team_list():
        profiles = EmployeeProfile.query.filter_by(
            reporting_manager_id=current_user.id,
            tenant_id=current_user.tenant_id,
        ).all()
        return render_template(
            "manager/team_list.html",
            profiles=[profile_for_manager_view(profile) for profile in profiles],
        )

    @app.route("/manager/team/<int:user_id>")
    @role_required("manager")
    def manager_view_employee(user_id):
        user = User.query.get_or_404(user_id)
        if (
            not user.profile
            or user.tenant_id != current_user.tenant_id
            or user.profile.reporting_manager_id != current_user.id
        ):
            abort(403)
        return render_template("profile_view.html", person=user, profile=profile_for_manager_view(user.profile),
                                viewer="manager")

    # -----------------------------------------------------------------
    # EMPLOYEE routes (also used by managers for THEIR OWN profile)
    # -----------------------------------------------------------------
    @app.route("/employee")
    @role_required("employee", "manager")
    def employee_dashboard():
        profile = current_user.profile
        return render_template("employee/dashboard.html", profile=profile,
                                **overview_context())

    # -----------------------------------------------------------------
    # PEOPLE & CULTURE
    # -----------------------------------------------------------------
    @app.route("/community", methods=["GET", "POST"])
    @login_required
    def community():
        if request.method == "POST":
            recipient_id = request.form.get("recipient_id", type=int)
            category = request.form.get("category", "Appreciation").strip()
            message = request.form.get("message", "").strip()
            recipient = User.query.filter(
                User.id == recipient_id,
                User.is_active == True,
                User.role.in_(["admin", "demo_admin", "manager", "employee"]),
            ).first()
            if not recipient or not message:
                flash("Choose a colleague and write a message before sharing.", "danger")
            else:
                db.session.add(Recognition(
                    sender_id=current_user.id,
                    recipient_id=recipient.id,
                    category=category or "Appreciation",
                    message=message,
                ))
                db.session.commit()
                flash("Your message is now visible to the team.", "success")
                return redirect(url_for("community"))

        people = scope_to_practice_data(User.query.join(
            EmployeeProfile, EmployeeProfile.user_id == User.id
        ).filter(
            User.is_active == True,
            User.role.in_(["admin", "demo_admin", "manager", "employee"]),
        )).order_by(EmployeeProfile.full_name.asc()).all()
        context = overview_context()
        if current_user.role == "demo_admin":
            practice_ids = [person.id for person in people]
            context["upcoming_birthdays"] = [
                person for person in people
                if person.profile and person.profile.date_of_birth
            ][:6]
            context["recent_recognitions"] = Recognition.query.filter(
                db.or_(
                    Recognition.sender_id.in_(practice_ids or [-1]),
                    Recognition.recipient_id.in_(practice_ids or [-1]),
                )
            ).order_by(Recognition.created_at.desc()).limit(6).all()
            context["upcoming_dates"] = ImportantDate.query.filter_by(
                created_by_id=current_user.id
            ).order_by(ImportantDate.date.asc()).all()
        return render_template("community.html", people=people, **context)

    @app.route("/admin/important-dates", methods=["POST"])
    @role_required("admin")
    def admin_add_important_date():
        if current_user.role == "demo_admin":
            return abort(403)
        title = request.form.get("title", "").strip()
        event_date = parse_date(request.form.get("date"))
        description = request.form.get("description", "").strip()
        if not title or not event_date:
            flash("A title and valid date are required.", "danger")
        else:
            db.session.add(ImportantDate(
                title=title, date=event_date, description=description,
                created_by_id=current_user.id,
            ))
            db.session.commit()
            flash("Important date added.", "success")
        return redirect(url_for("community"))

    @app.route("/admin/holidays", methods=["GET", "POST"])
    @role_required("admin")
    def admin_holidays():
        real_admin_only()
        if request.method == "POST":
            holiday_date = parse_date(request.form.get("date"))
            name = request.form.get("name", "").strip()
            state_code = request.form.get("state_code", "").strip().upper() or None
            if not holiday_date or not name:
                flash("Holiday date and name are required.", "danger")
            elif Holiday.query.filter_by(date=holiday_date, tenant_id=current_user.tenant_id).first():
                flash("A holiday already exists on that date.", "danger")
            else:
                db.session.add(Holiday(
                    tenant_id=current_user.tenant_id,
                    date=holiday_date,
                    name=name,
                    state_code=state_code,
                ))
                db.session.commit()
                flash("Holiday added.", "success")
            return redirect(url_for("admin_holidays"))

        holidays = Holiday.query.filter(
            db.or_(Holiday.tenant_id.is_(None), Holiday.tenant_id == current_user.tenant_id)
        ).order_by(Holiday.date.asc()).all()
        return render_template("admin/holidays.html", holidays=holidays)

    @app.route("/admin/holidays/<int:holiday_id>/delete", methods=["POST"])
    @role_required("admin")
    def admin_delete_holiday(holiday_id):
        real_admin_only()
        holiday = Holiday.query.get_or_404(holiday_id)
        if holiday.tenant_id is not None and holiday.tenant_id != current_user.tenant_id:
            abort(403)
        db.session.delete(holiday)
        db.session.commit()
        flash("Holiday removed.", "success")
        return redirect(url_for("admin_holidays"))

    @app.route("/admin/important-dates/<int:date_id>/delete", methods=["POST"])
    @role_required("admin")
    def admin_delete_important_date(date_id):
        important_date = ImportantDate.query.get_or_404(date_id)
        if current_user.role == "demo_admin" and important_date.created_by_id != current_user.id:
            abort(403)
        db.session.delete(important_date)
        db.session.commit()
        flash("Important date removed.", "success")
        return redirect(url_for("community"))

    @app.route("/employee/profile")
    @role_required("employee", "manager")
    def employee_view_own_profile():
        profile = current_user.profile
        return render_template("profile_view.html", person=current_user, profile=profile,
                                viewer="self")

    @app.route("/employee/profile/edit", methods=["GET", "POST"])
    @role_required("employee", "manager")
    def employee_edit_profile():
        profile = current_user.profile

        if request.method == "POST":
            errors = []

            pan = request.form.get("pan_number", "").strip().upper()
            aadhaar = request.form.get("aadhaar_number", "").strip()
            ifsc = request.form.get("bank_ifsc_code", "").strip().upper()

            if pan and not validate_pan(pan):
                errors.append("PAN number format looks invalid. Expected format: ABCDE1234F.")
            if aadhaar and not validate_aadhaar(aadhaar):
                errors.append("Aadhaar number must be exactly 12 digits.")
            if ifsc and not validate_ifsc(ifsc):
                errors.append("IFSC code format looks invalid. Expected format: ABCD0123456.")

            if errors:
                for e in errors:
                    flash(e, "danger")
                return render_template("employee/edit_profile.html", profile=profile,
                                        form=request.form)

            # ---- Personal ----
            profile.date_of_birth = parse_date(request.form.get("date_of_birth"))
            profile.gender = request.form.get("gender", "").strip()
            profile.personal_email = request.form.get("personal_email", "").strip()
            profile.phone_number = request.form.get("phone_number", "").strip()
            profile.blood_group = request.form.get("blood_group", "").strip()

            # ---- Identity ----
            profile.pan_number = encrypt_field(pan) if pan else ""
            profile.aadhaar_number = encrypt_field(aadhaar) if aadhaar else ""
            uploaded_documents = []

            for field_name, profile_field, prefix in (
                ("pan_document", "pan_document_filename", "pan"),
                ("aadhaar_document", "aadhaar_document_filename", "aadhaar"),
                ("cancelled_cheque", "cancelled_cheque_filename", "cheque"),
            ):
                document = request.files.get(field_name)
                if document and document.filename:
                    try:
                        setattr(profile, profile_field, save_private_upload(
                            document, prefix=f"{prefix}_{current_user.id}",
                            allowed_extensions={".pdf", ".png", ".jpg", ".jpeg", ".webp"},
                        ))
                        uploaded_documents.append(field_name.replace("_", " ").title())
                    except UploadRejected as exc:
                        flash(f"{field_name.replace('_', ' ').title()}: {exc}", "danger")
                        return render_template("employee/edit_profile.html", profile=profile, form=request.form)
            picture = request.files.get("profile_picture")
            if picture and picture.filename:
                try:
                    profile.profile_picture_filename = save_private_upload(
                        picture, prefix=f"profile_{current_user.id}",
                        allowed_extensions={".png", ".jpg", ".jpeg", ".webp"},
                    )
                except UploadRejected as exc:
                    flash(str(exc), "danger")
                    return render_template("employee/edit_profile.html", profile=profile, form=request.form)

            # ---- Current address ----
            profile.current_address_line1 = request.form.get("current_address_line1", "").strip()
            profile.current_address_line2 = request.form.get("current_address_line2", "").strip()
            profile.current_city = request.form.get("current_city", "").strip()
            profile.current_state = request.form.get("current_state", "").strip()
            profile.current_pincode = request.form.get("current_pincode", "").strip()

            # ---- Permanent address ----
            same_as_current = request.form.get("same_as_current") == "on"
            profile.same_as_current = same_as_current
            if same_as_current:
                profile.permanent_address_line1 = profile.current_address_line1
                profile.permanent_address_line2 = profile.current_address_line2
                profile.permanent_city = profile.current_city
                profile.permanent_state = profile.current_state
                profile.permanent_pincode = profile.current_pincode
            else:
                profile.permanent_address_line1 = request.form.get("permanent_address_line1", "").strip()
                profile.permanent_address_line2 = request.form.get("permanent_address_line2", "").strip()
                profile.permanent_city = request.form.get("permanent_city", "").strip()
                profile.permanent_state = request.form.get("permanent_state", "").strip()
                profile.permanent_pincode = request.form.get("permanent_pincode", "").strip()

            # ---- Bank ----
            profile.bank_account_number = encrypt_field(request.form.get("bank_account_number", "").strip()) if request.form.get("bank_account_number", "").strip() else ""
            profile.bank_ifsc_code = ifsc
            profile.bank_name = request.form.get("bank_name", "").strip()
            profile.bank_branch = request.form.get("bank_branch", "").strip()

            # ---- Previous employment ----
            profile.previous_company_name = request.form.get("previous_company_name", "").strip()
            profile.previous_designation = request.form.get("previous_designation", "").strip()
            profile.previous_employment_from = parse_date(request.form.get("previous_employment_from"))
            profile.previous_employment_to = parse_date(request.form.get("previous_employment_to"))

            # ---- Relieving letter upload ----
            file = request.files.get("relieving_letter")
            if file and file.filename:
                if allowed_file(file.filename):
                    extension = os.path.splitext(file.filename)[1].lower()
                    if not has_valid_file_signature(file, extension):
                        flash("The uploaded file type does not match its extension.", "danger")
                        return render_template("employee/edit_profile.html", profile=profile, form=request.form)
                    filename = save_private_upload(file, "relieving")
                    profile.relieving_letter_filename = filename
                else:
                    flash("Relieving letter must be a PDF, PNG or JPG file.", "warning")

            # ---- Emergency contact ----
            profile.emergency_contact_name = request.form.get("emergency_contact_name", "").strip()
            profile.emergency_contact_relation = request.form.get("emergency_contact_relation", "").strip()
            profile.emergency_contact_phone = request.form.get("emergency_contact_phone", "").strip()

            # ---- Spouse ----
            profile.spouse_name = request.form.get("spouse_name", "").strip()
            profile.spouse_dob = parse_date(request.form.get("spouse_dob"))

            # ---- Children (dynamic rows: child_name_1, child_dob_1, ...) ----
            Child.query.filter_by(profile_id=profile.id).delete()
            child_names = request.form.getlist("child_name")
            child_dobs = request.form.getlist("child_dob")
            for name, dob in zip(child_names, child_dobs):
                if name.strip():
                    db.session.add(Child(profile_id=profile.id, name=name.strip(),
                                          date_of_birth=parse_date(dob)))

            profile.profile_completed = True
            profile.updated_at = now_ist()
            db.session.commit()

            message = "Your profile has been saved."
            if uploaded_documents:
                message += " Uploaded: " + ", ".join(uploaded_documents) + "."
            flash(message, "success")
            return redirect(url_for("employee_view_own_profile"))

        return render_template("employee/edit_profile.html", profile=profile, form=None)

    @app.route("/employee/payroll/investments", methods=["GET", "POST"])
    @role_required("employee", "manager")
    def employee_investment_declarations():
        sections = ["80C", "80CCC", "80CCD(1)", "80CCD(1B)", "80D", "80E", "80G", "HRA", "24(b)", "Other"]
        tax_regime = (
            current_user.profile.tax_regime
            if current_user.profile and current_user.profile.tax_regime in {"old_regime", "new_regime"}
            else "new_regime"
        )

        def owned_declaration(declaration_id):
            return InvestmentDeclaration.query.filter_by(
                id=declaration_id,
                user_id=current_user.id,
            ).filter(
                db.or_(
                    InvestmentDeclaration.tenant_id == current_user.tenant_id,
                    InvestmentDeclaration.tenant_id.is_(None),
                )
            ).first_or_404()

        def render_declarations(editing=None, form_values=None):
            declarations = InvestmentDeclaration.query.filter_by(
                user_id=current_user.id
            ).filter(
                db.or_(
                    InvestmentDeclaration.tenant_id == current_user.tenant_id,
                    InvestmentDeclaration.tenant_id.is_(None),
                )
            ).order_by(
                InvestmentDeclaration.financial_year.desc(),
                InvestmentDeclaration.id.desc(),
            ).all()
            current_year = today_ist().year
            return render_template(
                "employee/investment_declarations.html",
                declarations=declarations,
                sections=sections,
                current_year=current_year,
                tax_regime=tax_regime,
                editing=editing,
                form_values=form_values,
            )

        if request.method == "POST":
            action = request.form.get("action")
            if action not in {"save_draft", "submit"}:
                abort(400)

            raw_declaration_id = request.form.get("declaration_id", "").strip()
            declaration = None
            if raw_declaration_id:
                try:
                    declaration = owned_declaration(int(raw_declaration_id))
                except ValueError:
                    flash("The selected declaration is invalid.", "danger")
                    return redirect(url_for("employee_investment_declarations"))
                if declaration.status not in {"draft", "rejected"}:
                    abort(403)

            financial_year = request.form.get("financial_year", "").strip()
            submitted_regime = request.form.get("tax_regime", tax_regime)
            section = request.form.get("section", "")
            amount_text = request.form.get("declared_amount", "").strip()
            try:
                declared_amount = float(amount_text) if amount_text else 0.0
            except ValueError:
                declared_amount = -1.0
            document = request.files.get("document")
            has_document = bool(document and document.filename)

            if not re.fullmatch(r"\d{4}-\d{2}", financial_year):
                flash("Enter a valid financial year, for example 2026-27.", "danger")
                return render_declarations(declaration, request.form)
            elif submitted_regime != tax_regime or section not in sections:
                flash("Select a valid tax regime and declaration section.", "danger")
                return render_declarations(declaration, request.form)
            elif declared_amount < 0 or (action == "submit" and declared_amount <= 0):
                flash("Enter a valid declaration amount greater than zero to submit.", "danger")
                return render_declarations(declaration, request.form)
            elif action == "submit" and not has_document and not (declaration and declaration.document_filename):
                flash("Upload supporting proof for the declaration.", "danger")
                return render_declarations(declaration, request.form)

            new_filename = None
            if has_document:
                try:
                    new_filename = save_private_upload(document, prefix=f"investment_{current_user.id}")
                except UploadRejected as exc:
                    flash(str(exc), "danger")
                    return render_declarations(declaration, request.form)

            if declaration is None:
                declaration = InvestmentDeclaration(
                    user_id=current_user.id,
                    tenant_id=current_user.tenant_id,
                    financial_year=financial_year,
                    tax_regime=tax_regime,
                    section=section,
                    declared_amount=declared_amount,
                )
                db.session.add(declaration)
            else:
                declaration.financial_year = financial_year
                declaration.tax_regime = tax_regime
                declaration.section = section
                declaration.declared_amount = declared_amount

            old_filename = declaration.document_filename
            if new_filename:
                declaration.document_filename = new_filename
            declaration.status = "submitted" if action == "submit" else "draft"
            if action == "submit":
                declaration.approved_amount = 0.0
                declaration.submitted_at = now_ist()
                declaration.decided_at = None
                declaration.decided_by_id = None
                declaration.decision_note = None
            db.session.commit()

            if new_filename and old_filename and old_filename != new_filename:
                old_path = os.path.join(app.config["UPLOAD_FOLDER"], old_filename)
                if os.path.isfile(old_path):
                    os.remove(old_path)

            flash(
                "Investment declaration submitted for HR approval."
                if action == "submit" else "Investment declaration saved as a draft.",
                "success",
            )
            return redirect(url_for("employee_investment_declarations"))

        edit_id = request.args.get("edit", type=int)
        editing = owned_declaration(edit_id) if edit_id else None
        if editing and editing.status not in {"draft", "rejected"}:
            flash("Only draft or rejected declarations can be edited.", "warning")
            return redirect(url_for("employee_investment_declarations"))
        return render_declarations(editing)

    @app.route("/admin/payroll/investments")
    @role_required("admin")
    def admin_investment_declarations():
        real_admin_only()
        declarations = InvestmentDeclaration.query.order_by(
            InvestmentDeclaration.status, InvestmentDeclaration.submitted_at.desc()
        ).all()
        return render_template("admin/investment_declarations.html", declarations=declarations)

    @app.route("/admin/payroll/investments/<int:declaration_id>/decision", methods=["POST"])
    @role_required("admin")
    def decide_investment_declaration(declaration_id):
        real_admin_only()
        declaration = InvestmentDeclaration.query.get_or_404(declaration_id)
        if declaration.status not in {"submitted", "pending"}:
            flash("Only submitted declarations can be reviewed.", "warning")
            return redirect(url_for("admin_investment_declarations"))
        decision = request.form.get("decision")
        if decision not in {"approved", "rejected"}:
            abort(400)
        decision_note = request.form.get("decision_note", "").strip()
        if decision == "rejected" and not decision_note:
            flash("Add a rejection remark before rejecting the declaration.", "danger")
            return redirect(url_for("admin_investment_declarations"))
        try:
            approved_amount = float(request.form.get("approved_amount", "0")) if decision == "approved" else 0.0
        except (TypeError, ValueError):
            approved_amount = -1.0
        if approved_amount < 0 or approved_amount > declaration.declared_amount:
            flash("Approved amount must be between zero and the declared amount.", "danger")
            return redirect(url_for("admin_investment_declarations"))
        declaration.status = decision
        declaration.approved_amount = approved_amount
        declaration.decided_by_id = current_user.id
        declaration.decided_at = datetime.utcnow()
        declaration.decision_note = decision_note
        db.session.commit()
        flash(f"Investment declaration {decision}.", "success")
        return redirect(url_for("admin_investment_declarations"))

    @app.route("/payroll/investments/<int:declaration_id>/document")
    @login_required
    def download_investment_document(declaration_id):
        declaration = InvestmentDeclaration.query.get_or_404(declaration_id)
        is_owner = declaration.user_id == current_user.id
        is_admin = current_user.role == "admin"
        same_tenant = declaration.tenant_id in {None, current_user.tenant_id}
        if not (is_admin or (is_owner and same_tenant)):
            abort(403)
        if not declaration.document_filename:
            abort(404)
        return send_from_directory(
            app.config["UPLOAD_FOLDER"], declaration.document_filename, as_attachment=True
        )

    # -----------------------------------------------------------------
    # LEAVE MANAGEMENT — Admin: manage leave types
    # -----------------------------------------------------------------
    @app.route("/admin/leave-types")
    @role_required("admin")
    def admin_leave_types():
        real_admin_only()
        leave_types = LeaveType.query.order_by(LeaveType.id).all()
        return render_template("admin/leave_types.html", leave_types=leave_types)

    @app.route("/admin/leave-types/new", methods=["POST"])
    @role_required("admin")
    def admin_create_leave_type():
        real_admin_only()
        name = request.form.get("name", "").strip()
        annual_quota = request.form.get("annual_quota", "").strip()

        if not name or not annual_quota.isdigit():
            flash("Enter a leave name and a whole-number annual quota.", "danger")
            return redirect(url_for("admin_leave_types"))

        if LeaveType.query.filter_by(name=name).first():
            flash(f'A leave type called "{name}" already exists.', "danger")
            return redirect(url_for("admin_leave_types"))

        lt = LeaveType(name=name, annual_quota=int(annual_quota))
        db.session.add(lt)
        db.session.commit()

        recalculate_all_balances_for_type(lt, today_ist().year)
        flash(f'"{name}" added with an annual quota of {annual_quota} days.', "success")
        return redirect(url_for("admin_leave_types"))

    @app.route("/admin/leave-types/<int:type_id>/edit", methods=["POST"])
    @role_required("admin")
    def admin_edit_leave_type(type_id):
        real_admin_only()
        lt = LeaveType.query.get_or_404(type_id)
        annual_quota = request.form.get("annual_quota", "").strip()

        if not annual_quota.isdigit():
            flash("Annual quota must be a whole number.", "danger")
            return redirect(url_for("admin_leave_types"))

        lt.annual_quota = int(annual_quota)
        db.session.commit()

        recalculate_all_balances_for_type(lt, today_ist().year)
        flash(f'"{lt.name}" updated to {annual_quota} days/year. Balances recalculated for everyone.', "success")
        return redirect(url_for("admin_leave_types"))

    @app.route("/admin/leave-types/<int:type_id>/toggle", methods=["POST"])
    @role_required("admin")
    def admin_toggle_leave_type(type_id):
        real_admin_only()
        lt = LeaveType.query.get_or_404(type_id)
        lt.is_active = not lt.is_active
        db.session.commit()
        flash(f'"{lt.name}" is now {"active" if lt.is_active else "inactive"}.', "success")
        return redirect(url_for("admin_leave_types"))

    # -----------------------------------------------------------------
    # LEAVE MANAGEMENT — Admin: see + act on pending requests across everyone
    # -----------------------------------------------------------------
    @app.route("/admin/leaves")
    @role_required("admin")
    def admin_leave_requests():
        status_filter = request.args.get("status", "pending")
        scoped_ids = [user.id for user in scope_to_practice_data(
            User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
        ).all()]
        query = LeaveApplication.query.filter(LeaveApplication.user_id.in_(scoped_ids or [-1]))
        if status_filter != "all":
            query = query.filter_by(status=status_filter)
        applications = query.order_by(LeaveApplication.applied_at.desc()).all()
        return render_template("admin/leave_requests.html", applications=applications,
                                status_filter=status_filter)

    @app.route("/admin/leaves/<int:app_id>/decide", methods=["POST"])
    @role_required("admin")
    def admin_decide_leave(app_id):
        # Leave approval authority belongs to real admins only.
        real_admin_only()
        application = LeaveApplication.query.get_or_404(app_id)
        if current_user.role != "admin" and application.tenant_id != current_user.tenant_id:
            abort(403)
        decision = request.form.get("decision")
        note = request.form.get("decision_note", "").strip()

        if application.status != "pending":
            flash("This request has already been decided.", "warning")
            return redirect(url_for("admin_leave_requests"))

        if decision not in ("approved", "rejected"):
            abort(400)
        if decision == "approved":
            balances = ensure_balances_for_user(application.user, application.start_date.year)
            balance = next((item for item in balances if item.leave_type_id == application.leave_type_id), None)
            if balance and application.days > balance.remaining:
                flash("This request exceeds the employee's remaining leave balance.", "danger")
                return redirect(url_for("admin_leave_requests"))

        application.status = decision
        application.decided_at = now_ist()
        application.decided_by_id = current_user.id
        application.decision_note = note
        db.session.commit()
        audit_event("leave_approved" if decision == "approved" else "leave_rejected", target_user_id=application.user_id)

        recalculate_user_balances(application.user, application.start_date.year)
        flash(f"Leave request for {application.user.profile.full_name} {decision}.", "success")
        return redirect(url_for("admin_leave_requests"))

    # -----------------------------------------------------------------
    # LEAVE MANAGEMENT — Manager: see + act on their team's requests
    # -----------------------------------------------------------------
    @app.route("/manager/leaves")
    @role_required("manager")
    def manager_leave_requests():
        status_filter = request.args.get("status", "pending")
        team_ids = [p.user_id for p in EmployeeProfile.query.filter_by(
            reporting_manager_id=current_user.id,
            tenant_id=current_user.tenant_id,
        ).all()]
        query = LeaveApplication.query.filter(
            LeaveApplication.user_id.in_(team_ids or [-1]),
            LeaveApplication.tenant_id == current_user.tenant_id,
        )
        if status_filter != "all":
            query = query.filter_by(status=status_filter)
        applications = query.order_by(LeaveApplication.applied_at.desc()).all()
        return render_template("manager/leave_requests.html", applications=applications,
                                status_filter=status_filter)

    @app.route("/manager/leaves/<int:app_id>/decide", methods=["POST"])
    @role_required("manager")
    def manager_decide_leave(app_id):
        application = LeaveApplication.query.get_or_404(app_id)

        if (
            not application.user.profile
            or application.user.tenant_id != current_user.tenant_id
            or application.user.profile.reporting_manager_id != current_user.id
        ):
            abort(403)
        if application.status != "pending":
            flash("This request has already been decided.", "warning")
            return redirect(url_for("manager_leave_requests"))

        decision = request.form.get("decision")
        note = request.form.get("decision_note", "").strip()
        if decision not in ("approved", "rejected"):
            abort(400)
        if decision == "approved":
            balances = ensure_balances_for_user(application.user, application.start_date.year)
            balance = next((item for item in balances if item.leave_type_id == application.leave_type_id), None)
            if balance and application.days > balance.remaining:
                flash("This request exceeds the employee's remaining leave balance.", "danger")
                return redirect(url_for("manager_leave_requests"))

        application.status = decision
        application.decided_at = now_ist()
        application.decided_by_id = current_user.id
        application.decision_note = note
        db.session.commit()
        audit_event("leave_approved" if decision == "approved" else "leave_rejected", target_user_id=application.user_id)

        recalculate_user_balances(application.user, application.start_date.year)
        flash(f"Leave request for {application.user.profile.full_name} {decision}.", "success")
        return redirect(url_for("manager_leave_requests"))

    # -----------------------------------------------------------------
    # LEAVE MANAGEMENT — Employee/Manager self-service: balances + apply
    # -----------------------------------------------------------------
    @app.route("/leaves")
    @role_required("employee", "manager")
    def my_leaves():
        if not current_user.profile or not current_user.profile.date_of_joining:
            flash("Your administrator hasn't set your date of joining yet, so leave balances "
                  "aren't available until that's in place.", "warning")
            return render_template("employee/leaves.html", balances=[], applications=[],
                                    not_yet_joined=True)

        today = today_ist()
        if current_user.profile.date_of_joining > today:
            return render_template("employee/leaves.html", balances=[], applications=[],
                                    not_yet_joined=True,
                                    joining_date=current_user.profile.date_of_joining)

        balances = ensure_balances_for_user(current_user, today.year)
        applications = LeaveApplication.query.filter_by(user_id=current_user.id) \
            .order_by(LeaveApplication.applied_at.desc()).all()

        return render_template("employee/leaves.html", balances=balances,
                                applications=applications, not_yet_joined=False)

    @app.route("/leaves/apply", methods=["GET", "POST"])
    @role_required("employee", "manager")
    def apply_leave():
        if not current_user.profile or not current_user.profile.date_of_joining:
            abort(403)

        today = today_ist()
        if current_user.profile.date_of_joining > today:
            flash("You can't apply for leave before your date of joining.", "danger")
            return redirect(url_for("my_leaves"))

        leave_types = LeaveType.query.filter(
            LeaveType.is_active == True,
            db.or_(LeaveType.tenant_id.is_(None), LeaveType.tenant_id == current_user.tenant_id),
        ).all()
        employee_state = (current_user.profile.current_state or "").strip().upper()
        holiday_query = Holiday.query.filter(
            db.or_(Holiday.tenant_id.is_(None), Holiday.tenant_id == current_user.tenant_id),
            db.or_(Holiday.state_code.is_(None), db.func.upper(Holiday.state_code) == employee_state),
        ).order_by(Holiday.date.asc())
        holiday_calendar = [
            {"date": holiday.date.isoformat(), "name": holiday.name, "state": holiday.state_code or "All"}
            for holiday in holiday_query.all()
        ]

        if request.method == "POST":
            leave_type_id = request.form.get("leave_type_id")
            start_date = parse_date(request.form.get("start_date"))
            end_date = parse_date(request.form.get("end_date"))
            reason = request.form.get("reason", "").strip()

            leave_type = LeaveType.query.filter(
                LeaveType.id == leave_type_id,
                LeaveType.is_active == True,
                db.or_(LeaveType.tenant_id.is_(None), LeaveType.tenant_id == current_user.tenant_id),
            ).first() if leave_type_id else None

            if not leave_type or not start_date or not end_date:
                flash("Please fill in the leave type and both dates.", "danger")
                return render_template("employee/apply_leave.html", leave_types=leave_types, form=request.form, holiday_calendar=holiday_calendar, employee_state=employee_state)

            if end_date < start_date:
                flash("End date can't be before the start date.", "danger")
                return render_template("employee/apply_leave.html", leave_types=leave_types, form=request.form, holiday_calendar=holiday_calendar, employee_state=employee_state)

            if start_date < current_user.profile.date_of_joining:
                flash("You can't apply for leave before your date of joining.", "danger")
                return render_template("employee/apply_leave.html", leave_types=leave_types, form=request.form, holiday_calendar=holiday_calendar, employee_state=employee_state)

            days = business_days_count(
                start_date,
                end_date,
                current_user.profile.current_state if current_user.profile else None,
            )
            balance = ensure_balances_for_user(current_user, start_date.year)
            matching = next((b for b in balance if b.leave_type_id == leave_type.id), None)

            if matching and days > matching.remaining:
                flash(f"You only have {matching.remaining:g} days of {leave_type.name} left "
                      f"for {start_date.year} — this request needs {days:g}.", "danger")
                return render_template("employee/apply_leave.html", leave_types=leave_types, form=request.form, holiday_calendar=holiday_calendar, employee_state=employee_state)

            application = LeaveApplication(
                tenant_id=current_user.tenant_id,
                user_id=current_user.id, leave_type_id=leave_type.id,
                start_date=start_date, end_date=end_date, days=days,
                reason=reason, status="pending"
            )
            db.session.add(application)
            db.session.commit()

            flash("Leave request submitted for approval.", "success")
            return redirect(url_for("my_leaves"))

        return render_template("employee/apply_leave.html", leave_types=leave_types, form=None, holiday_calendar=holiday_calendar, employee_state=employee_state)

    @app.route("/leaves/<int:app_id>/cancel", methods=["POST"])
    @role_required("employee", "manager")
    def cancel_leave(app_id):
        application = LeaveApplication.query.get_or_404(app_id)
        if application.user_id != current_user.id or (
            current_user.tenant_id and application.tenant_id != current_user.tenant_id
        ):
            abort(403)
        if application.status != "pending":
            flash("Only pending requests can be cancelled.", "warning")
            return redirect(url_for("my_leaves"))

        application.status = "cancelled"
        application.decided_at = now_ist()
        db.session.commit()
        audit_event("leave_cancelled", target_user_id=current_user.id)
        flash("Leave request cancelled.", "success")
        return redirect(url_for("my_leaves"))
     # -----------------------------------------------------------------
    # PAYROLL DASHBOARD
    # -----------------------------------------------------------------

    @app.route("/admin/payroll")
    @role_required("admin")
    def payroll_dashboard():
        real_admin_only()

        total_components = PayComponent.query.count()
        total_ctc = EmployeeCTC.query.count()
        total_payslips = Payslip.query.count()
        processed_payslips = Payslip.query.order_by(Payslip.year.desc(), Payslip.month.desc()).all()
        latest_period = (processed_payslips[0].year, processed_payslips[0].month) if processed_payslips else None
        period_payslips = [
            payslip for payslip in processed_payslips
            if latest_period and (payslip.year, payslip.month) == latest_period
        ]

        def payroll_total(attribute, rows=period_payslips):
            return round(sum(float(getattr(row, attribute, 0.0) or 0.0) for row in rows), 2)

        monthly_costs = {}
        for payslip in processed_payslips:
            key = (payslip.year, payslip.month)
            monthly_costs[key] = monthly_costs.get(key, 0.0) + float(payslip.monthly_ctc_used or 0.0)
        monthly_disbursed = [
            {"label": f"{month:02d}/{year}", "amount": round(amount, 2)}
            for (year, month), amount in sorted(monthly_costs.items(), reverse=True)[:12]
        ][::-1]
        max_monthly_disbursed = max((item["amount"] for item in monthly_disbursed), default=1.0)

        department_totals = {}
        for payslip in period_payslips:
            department = (
                payslip.user.profile.department
                if payslip.user and payslip.user.profile and payslip.user.profile.department
                else "Unassigned"
            )
            department_totals[department] = department_totals.get(department, 0.0) + float(payslip.monthly_ctc_used or 0.0)
        department_costs = sorted(
            ((label, round(amount, 2)) for label, amount in department_totals.items()),
            key=lambda item: (-item[1], item[0]),
        )[:8]
        max_department_cost = max((amount for _, amount in department_costs), default=1.0)

        return render_template(
            "payroll/dashboard.html",
            total_components=total_components,
            total_ctc=total_ctc,
            total_payslips=total_payslips,
            latest_period=latest_period,
            latest_ctc_disbursed=payroll_total("monthly_ctc_used"),
            latest_gross_pay=payroll_total("gross_pay"),
            latest_net_pay=payroll_total("net_pay"),
            latest_deductions=payroll_total("total_deductions"),
            latest_arrears=payroll_total("arrears"),
            latest_ot_amount=payroll_total("ot_amount"),
            monthly_disbursed=monthly_disbursed,
            max_monthly_disbursed=max_monthly_disbursed,
            department_costs=department_costs,
            max_department_cost=max_department_cost,
            recent_payslips=period_payslips[:8],
            compliance_warnings=run_statutory_compliance_checks(),
        )

    @app.route("/admin/payroll/advances", methods=["GET", "POST"])
    @role_required("admin")
    def payroll_advances():
        real_admin_only()
        employees = scope_to_practice_data(
            User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
        ).order_by(User.employee_code).all()
        if request.method == "POST":
            employee_id = int(request.form.get("user_id"))
            scoped_user_or_403(employee_id)
            amount = float(request.form.get("advance_amount") or 0)
            repayment = float(request.form.get("monthly_repayment") or 0)
            if amount <= 0 or repayment <= 0:
                flash("Advance amount and monthly repayment must be greater than zero.", "danger")
                return redirect(url_for("payroll_advances"))
            db.session.add(SalaryAdvance(
                user_id=employee_id,
                tenant_id=User.query.get(employee_id).tenant_id,
                advance_amount=amount,
                outstanding_amount=amount,
                monthly_repayment=min(repayment, amount),
                start_month=int(request.form.get("start_month")),
                start_year=int(request.form.get("start_year")),
                description=request.form.get("description", "").strip(),
                created_by_id=current_user.id,
            ))
            db.session.commit()
            flash("Salary advance added to the employee ledger.", "success")
            return redirect(url_for("payroll_advances"))

        advances = SalaryAdvance.query.order_by(SalaryAdvance.status, SalaryAdvance.id.desc()).all()
        return render_template("payroll/advances.html", employees=employees, advances=advances)
    # -----------------------------------------------------------------
    # PAY COMPONENTS
    # -----------------------------------------------------------------

    @app.route("/admin/payroll/components", methods=["GET", "POST"])
    @role_required("admin")
    def pay_components():
        real_admin_only()

        if request.method == "POST":
            preview_ctc = safe_float(request.form.get("preview_ctc"))
            if preview_ctc <= 0:
                flash("Enter a valid reference CTC for the global breakup.", "danger")
                return redirect(url_for("pay_components"))
            for code in ["basic", "hra", "special_allowance", "pf", "vpf", "esic", "professional_tax", "tds", "superannuation"]:
                component = PayComponent.query.filter_by(statutory_code=code).first()
                if not component:
                    continue
                component.calc_basis = request.form.get(f"global_basis_{code}", component.calc_basis)
                component.calc_value = safe_float(request.form.get(f"global_value_{code}"))
                component.is_active = request.form.get(f"global_enabled_{code}") == "on"

            for ctc in EmployeeCTC.query.all():
                for line in ctc.lines:
                    if line.is_hr_override:
                        continue
                    component = PayComponent.query.filter_by(statutory_code=line.statutory_code).first()
                    if component:
                        line.pay_component_id = component.id
                        line.calc_basis = component.calc_basis
                        line.calc_value = component.calc_value
                        line.enabled = component.is_active
                calculated = calculate_ctc_breakup_lines(ctc.monthly_ctc, ctc.lines, ctc.user)
                for line in ctc.lines:
                    if not line.is_hr_override:
                        line.monthly_amount = calculated.get(line.id, 0.0)
            db.session.commit()
            flash("Global breakup defaults saved and non-overridden employee breakups updated.", "success")
            return redirect(url_for("pay_components"))

        components = PayComponent.query.order_by(
            PayComponent.calc_order,
            PayComponent.id
        ).all()

        name_codes = {
            "basic salary": "basic", "basic": "basic",
            "house rent allowance (hra)": "hra", "hra": "hra",
            "special allowance": "special_allowance",
            "provident fund (pf)": "pf", "pf": "pf",
            "vpf": "vpf", "voluntary provident fund (vpf)": "vpf",
            "esic": "esic", "esi": "esic",
            "professional tax": "professional_tax",
            "tds / income tax": "tds", "tds": "tds",
            "superannuation": "superannuation",
        }
        changed_codes = False
        for component in components:
            code = name_codes.get(component.name.strip().lower())
            if code and not component.statutory_code:
                component.statutory_code = code
                changed_codes = True
        if changed_codes:
            db.session.commit()

        standard_components = {
            "basic": ("Basic Salary", "earning", "percent_ctc", 50.0, True),
            "hra": ("House Rent Allowance (HRA)", "earning", "percent_basic", 50.0, True),
            "special_allowance": ("Special Allowance", "earning", "residual", 0.0, True),
            "pf": ("Provident Fund (PF)", "deduction", "statutory", 12.0, True),
            "vpf": ("Voluntary Provident Fund (VPF)", "deduction", "percent_basic", 0.0, False),
            "esic": ("ESIC", "deduction", "statutory", 0.75, True),
            "professional_tax": ("Professional Tax", "deduction", "statutory", 0.0, True),
            "tds": ("TDS / Income Tax", "deduction", "statutory", 0.0, True),
            "superannuation": ("Superannuation", "earning", "percent_basic", 0.0, False),
        }
        existing_codes = {component.statutory_code for component in components}
        added_standard = False
        for code, (name, component_type, basis, value, active) in standard_components.items():
            if code not in existing_codes:
                db.session.add(PayComponent(
                    name=name, component_type=component_type, calc_basis=basis,
                    calc_value=value, calc_order=20, statutory_code=code,
                    is_active=active, is_basic=(code == "basic"),
                    is_employer_cost=(code == "superannuation"),
                ))
                added_standard = True
        if added_standard:
            db.session.commit()
            components = PayComponent.query.order_by(PayComponent.calc_order, PayComponent.id).all()

        earning_components = [c for c in components if c.component_type == "earning"]
        deduction_components = [c for c in components if c.component_type == "deduction"]
        global_breakup = build_automatic_ctc_lines(300000)
        global_components = {c.statutory_code: c for c in components if c.statutory_code}
        for item in global_breakup:
            component = global_components.get(item["code"])
            if component:
                item["basis"] = component.calc_basis
                item["value"] = component.calc_value
                item["enabled"] = component.is_active
        preview_lines = [SimpleNamespace(
            id=index + 1,
            component_name=item["name"],
            component_type=item["type"],
            statutory_code=item["code"],
            calc_basis=item["basis"],
            calc_value=item["value"],
            enabled=item["enabled"],
            is_employer_cost=item["employer"],
            is_hr_override=False,
        ) for index, item in enumerate(global_breakup)]
        preview_amounts = calculate_ctc_breakup_lines(300000, preview_lines)
        for item, line in zip(global_breakup, preview_lines):
            item["amount"] = preview_amounts.get(line.id, 0.0)

        return render_template(
            "payroll/pay_components.html",
            components=components,
            earning_components=earning_components,
            deduction_components=deduction_components,
            global_breakup=global_breakup,
            global_components=global_components,
        )
        # -----------------------------------------------------------------
    # EMPLOYEE CTC
    # -----------------------------------------------------------------

    @app.route("/admin/payroll/ctc")
    @role_required("admin")
    def employee_ctc():

        employees = scope_to_practice_data(
            User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
        ).order_by(User.employee_code).all()

        ctc_records = {}
        for employee in employees:
            ctc = get_active_ctc(employee.id)
            if ctc:
                ctc_records[employee.id] = ctc

        return render_template(
            "payroll/employee_ctc.html",
            employees=employees,
            ctc_records=ctc_records
        )
        # -----------------------------------------------------------------
    # SAVE EMPLOYEE CTC
    # -----------------------------------------------------------------

    @app.route("/admin/payroll/ctc/<int:user_id>", methods=["POST"])
    @role_required("admin")
    def save_employee_ctc(user_id):
        # Writing employee CTC records is restricted to real admins.
        real_admin_only()

        scoped_user_or_403(user_id)

        monthly_ctc = float(request.form.get("monthly_ctc"))

        effective_from = datetime.strptime(
            request.form.get("effective_from"),
            "%Y-%m-%d"
        ).date()

        ctc = EmployeeCTC.query.filter_by(user_id=user_id).first()

        if ctc:

            ctc.monthly_ctc = monthly_ctc
            ctc.effective_from = effective_from

        else:

            ctc = EmployeeCTC(
                user_id=user_id,
                monthly_ctc=monthly_ctc,
                effective_from=effective_from
            )

            db.session.add(ctc)

        db.session.commit()

        flash("Employee CTC saved successfully.", "success")

        return redirect(url_for("employee_ctc"))

    @app.route("/admin/payroll/ctc/<int:user_id>/breakup", methods=["GET", "POST"])
    @role_required("admin")
    def edit_employee_ctc_breakup(user_id):
        real_admin_only()
        employee = scoped_user_or_403(user_id)
        ctc = get_active_ctc(user_id)
        if not ctc:
            flash("Save the employee's monthly CTC before editing its breakup.", "danger")
            return redirect(url_for("employee_ctc"))

        if not ctc.lines:
            for proposal in build_automatic_ctc_lines(ctc.monthly_ctc):
                global_component = PayComponent.query.filter_by(
                    statutory_code=proposal["code"]
                ).first()
                ctc.lines.append(EmployeeCTCLine(
                    component_name=proposal["name"], component_type=proposal["type"],
                    pay_component_id=global_component.id if global_component else None,
                    statutory_code=proposal["code"], calc_basis=proposal["basis"],
                    calc_value=proposal["value"], monthly_amount=proposal["amount"],
                    enabled=proposal["enabled"], is_employer_cost=proposal["employer"],
                    is_in_ctc=True,
                ))
            db.session.commit()

        if request.method == "POST":
            ctc.breakup_mode = request.form.get("breakup_mode", "automatic")
            ctc.breakup_status = "approved" if request.form.get("approve_breakup") == "on" else "draft"
            for line in ctc.lines:
                line.enabled = request.form.get(f"enabled_{line.id}") == "on"
                line.calc_basis = request.form.get(f"basis_{line.id}", line.calc_basis)
                line.is_hr_override = request.form.get(f"override_{line.id}") == "on"
                line.calc_value = safe_float(request.form.get(f"value_{line.id}"))
                if line.is_hr_override:
                    line.calc_basis = "flat"
                    line.monthly_amount = safe_float(request.form.get(f"amount_{line.id}"))
                    line.calc_value = line.monthly_amount
            calculated = calculate_ctc_breakup_lines(ctc.monthly_ctc, ctc.lines, employee)
            for line in ctc.lines:
                if not line.is_hr_override:
                    line.monthly_amount = calculated.get(line.id, 0.0)
            db.session.commit()
            flash("Employee CTC breakup saved." if ctc.breakup_status == "draft" else "Employee CTC breakup approved for payroll.", "success")
            return redirect(url_for("edit_employee_ctc_breakup", user_id=user_id))

        calculated = calculate_ctc_breakup_lines(ctc.monthly_ctc, ctc.lines, employee)
        for line in ctc.lines:
            if not line.is_hr_override:
                line.monthly_amount = calculated.get(line.id, 0.0)
        db.session.commit()
        return render_template(
            "payroll/employee_ctc_breakup.html",
            employee=employee, ctc=ctc,
        )
         # -----------------------------------------------------------------
    # GENERATE PAYROLL
    # -----------------------------------------------------------------

    @app.route("/admin/payroll/generate", methods=["GET", "POST"])
    @role_required("admin")
    def generate_payroll():
        # Processing (writing) payroll is restricted to real admins.
        if request.method == "POST" and "process_payroll" in request.form:
            real_admin_only()
        today = today_ist()
        month = int(request.args.get("month") or request.form.get("month") or today.month)
        year = int(request.args.get("year") or request.form.get("year") or today.year)
        import calendar
        payroll_period_end = date(year, month, calendar.monthrange(year, month)[1])

        problems = validate_components_for_generation()
        if problems:
            for problem in problems:
                flash(problem, "danger")

        employees = scope_to_practice_data(
            User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
        ).join(EmployeeProfile, EmployeeProfile.user_id == User.id).filter(
            EmployeeProfile.date_of_joining <= payroll_period_end
        ).order_by(User.employee_code).all()
        employee_states = [
            employee.profile.current_state
            for employee in employees
            if employee.profile and employee.profile.current_state
        ]
        for warning in run_statutory_compliance_checks(employee_states=employee_states):
            flash(warning, "danger")

        if request.method == "POST" and "process_payroll" in request.form:
            created_count = 0
            updated_count = 0
            no_ctc_count = 0
            failed_count = 0

            for emp in employees:
                arrears = float(request.form.get(f"arrears_{emp.id}") or 0.0)
                loss_of_pay_value = request.form.get(f"loss_of_pay_{emp.id}")
                loss_of_pay = (
                    float(loss_of_pay_value)
                    if loss_of_pay_value is not None and loss_of_pay_value != ""
                    else None
                )
                ot_hours_val = request.form.get(f"ot_hours_{emp.id}")
                ot_hours = float(ot_hours_val) if ot_hours_val is not None and ot_hours_val != "" else None
                incentive = float(request.form.get(f"incentive_{emp.id}") or 0.0)
                gratuity = float(request.form.get(f"gratuity_provision_{emp.id}") or 0.0)
                additional_deduction = float(request.form.get(f"additional_deduction_{emp.id}") or 0.0)
                advance_repayment = float(request.form.get(f"advance_repayment_{emp.id}") or 0.0)

                try:
                    payslip, status = generate_payslip(
                        emp.id,
                        month,
                        year,
                        current_user.id,
                        overwrite=True,
                        arrears=arrears,
                        loss_of_pay=loss_of_pay,
                        ot_hours=ot_hours,
                        incentive=incentive,
                        pf_deduction=None,
                        esi_deduction=None,
                        professional_tax_deduction=None,
                        gratuity_provision=gratuity,
                        additional_deduction=additional_deduction,
                        advance_repayment=advance_repayment,
                    )
                except IntegrityError:
                    db.session.rollback()
                    failed_count += 1
                    continue

                if status == "created":
                    created_count += 1
                elif status == "updated":
                    updated_count += 1
                elif status == "no_ctc":
                    no_ctc_count += 1

            import calendar
            month_name = calendar.month_name[month]
            msg = f"Payroll processed for {month_name} {year}: {created_count} new payslip(s) generated, {updated_count} updated."
            if no_ctc_count > 0:
                msg += f" ({no_ctc_count} employee(s) skipped — no CTC assigned)"
            if failed_count > 0:
                msg += f" ({failed_count} employee(s) could not be processed)"

            flash(msg, "success" if (created_count > 0 or updated_count > 0) else "warning")
            return redirect(url_for("admin_payslip_list"))

        emp_payroll_inputs = []
        for emp in employees:
            stats = get_employee_monthly_payroll_inputs(emp.id, month, year)
            emp_payroll_inputs.append({
                "employee": emp,
                "stats": stats
            })

        import calendar
        month_name = calendar.month_name[month]
        return render_template(
            "payroll/generate_payroll.html",
            month=month,
            year=year,
            month_name=month_name,
            emp_payroll_inputs=emp_payroll_inputs
        )

    # -----------------------------------------------------------------
    # ALL PAYSLIPS
    # -----------------------------------------------------------------

    @app.route("/admin/payroll/payslips")
    @role_required("admin")
    def admin_payslip_list():
        query = Payslip.query
        if current_user.role == "demo_admin":
            query = query.filter(Payslip.tenant_id == current_user.tenant_id)
        payslips = query.order_by(
            Payslip.year.desc(), Payslip.month.desc(), Payslip.id.desc()
        ).all()

        return render_template(
            "payroll/payslip_list.html",
            payslips=payslips
        )

        # -----------------------------------------------------------------
    # ADD PAY COMPONENT
    # -----------------------------------------------------------------

    @app.route("/admin/payroll/components/new", methods=["GET", "POST"])
    @role_required("admin")
    def add_pay_component():
        real_admin_only()

        if request.method == "POST":

            name = request.form.get("name", "").strip()

            duplicate = PayComponent.query.filter(
                db.func.lower(PayComponent.name) == name.lower()
            ).first()
            if duplicate:
                flash("A salary component with this name already exists. Use a unique name.", "danger")
                return render_template("payroll/add_component.html")

            component_type = request.form.get("component_type")

            calc_basis = request.form.get("calc_basis")
            statutory_code = request.form.get("statutory_code") or None

            calc_value = float(request.form.get("calc_value") or 0)

            calc_order = int(request.form.get("calc_order") or 1)

            tax_regime = request.form.get("tax_regime") or "general"

            is_basic = request.form.get("is_basic") == "on"

            is_active = request.form.get("is_active") == "on"

            component = PayComponent(
                name=name,
                component_type=component_type,
                calc_basis=calc_basis,
                calc_value=calc_value,
                calc_order=calc_order,
                statutory_code=statutory_code,
                tax_regime=tax_regime,
                is_basic=is_basic,
                is_active=is_active,
            )

            db.session.add(component)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("A salary component with this name already exists. Use a unique name.", "danger")
                return render_template("payroll/add_component.html")

            flash("Salary Component added successfully.", "success")

            return redirect(url_for("pay_components"))

        return render_template("payroll/add_component.html")
        # -----------------------------------------------------------------
    # EDIT PAY COMPONENT
    # -----------------------------------------------------------------

    @app.route("/admin/payroll/components/<int:component_id>/edit",
               methods=["GET", "POST"])
    @role_required("admin")
    def edit_pay_component(component_id):
        real_admin_only()

        component = PayComponent.query.get_or_404(component_id)

        if request.method == "POST":

            component.name = (request.form.get("name") or "").strip()
            duplicate = PayComponent.query.filter(
                db.func.lower(PayComponent.name) == component.name.strip().lower(),
                PayComponent.id != component.id,
            ).first()
            if duplicate:
                flash("A salary component with this name already exists. Use a unique name.", "danger")
                return render_template("payroll/edit_component.html", component=component)
            component.component_type = request.form.get("component_type")
            component.calc_basis = request.form.get("calc_basis")
            component.statutory_code = request.form.get("statutory_code") or None
            component.calc_value = float(request.form.get("calc_value") or 0)
            component.calc_order = int(request.form.get("calc_order") or 1)
            component.tax_regime = request.form.get("tax_regime") or "general"
            component.is_basic = request.form.get("is_basic") == "on"
            component.is_active = request.form.get("is_active") == "on"

            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("A salary component with this name already exists. Use a unique name.", "danger")
                return render_template("payroll/edit_component.html", component=component)

            flash("Salary Component updated successfully.", "success")

            return redirect(url_for("pay_components"))

        return render_template(
            "payroll/edit_component.html",
            component=component
        )
    # -----------------------------------------------------------------
    # Shared: file download (relieving letter) — guarded by ownership/role
    # -----------------------------------------------------------------

    @app.route("/uploads/<int:user_id>/<path:filename>")
    @login_required
    def download_upload(user_id, filename):
        target = User.query.get_or_404(user_id)

        if not target.profile or target.profile.relieving_letter_filename != filename:
            abort(404)

        is_owner = current_user.id == user_id
        is_admin = current_user.role == "admin"
        is_demo_owner = (
            current_user.role == "demo_admin"
            and target.tenant_id == current_user.tenant_id
            and target.created_by_id == current_user.id
        )
        is_their_manager = (
            current_user.role == "manager"
            and target.tenant_id == current_user.tenant_id
            and target.profile.reporting_manager_id == current_user.id
        )

        if not (is_owner or is_admin or is_demo_owner or is_their_manager):
            abort(403)

        return send_from_directory(
            app.config["UPLOAD_FOLDER"],
            filename,
            as_attachment=True,
        )

    @app.route("/employee-documents/<int:user_id>/<document_type>")
    @login_required
    def download_employee_document(user_id, document_type):
        target = User.query.get_or_404(user_id)
        if not target.profile:
            abort(404)
        filename = {
            "profile-picture": target.profile.profile_picture_filename,
            "pan": target.profile.pan_document_filename,
            "aadhaar": target.profile.aadhaar_document_filename,
            "cancelled-cheque": target.profile.cancelled_cheque_filename,
        }.get(document_type)
        if not filename:
            abort(404)
        is_their_manager = (
            current_user.role == "manager"
            and target.tenant_id == current_user.tenant_id
            and target.profile.reporting_manager_id == current_user.id
        )
        if is_their_manager and document_type in {"pan", "aadhaar", "cancelled-cheque"}:
            abort(403)
        is_demo_owner = (
            current_user.role == "demo_admin"
            and target.tenant_id == current_user.tenant_id
            and target.created_by_id == current_user.id
        )
        # demo_admin must not access sensitive identity/financial documents,
        # matching the same restriction already applied to managers.
        if is_demo_owner and document_type in {"pan", "aadhaar", "cancelled-cheque"}:
            abort(403)
        allowed = current_user.id == user_id or current_user.role == "admin" or is_demo_owner or is_their_manager
        if not allowed:
            abort(403)
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=True)

    # -----------------------------------------------------------------
    # PAYSLIP VIEWING & DOWNLOAD ROUTES
    # -----------------------------------------------------------------
    @app.route("/payroll/payslips/<int:payslip_id>")
    @login_required
    def view_payslip(payslip_id):
        payslip = Payslip.query.get_or_404(payslip_id)
        is_owner = current_user.id == payslip.user_id
        is_admin = current_user.role == "admin"
        is_demo_owner = (
            current_user.role == "demo_admin"
            and payslip.user
            and payslip.tenant_id == current_user.tenant_id
        )
        if not (is_owner or is_admin or is_demo_owner):
            abort(403)

        net_words = num_to_words(payslip.net_pay)
        return render_template("payroll/payslip_view.html", payslip=payslip, net_words=net_words)

    @app.route("/payroll/payslips/<int:payslip_id>/pdf")
    @login_required
    def download_payslip_pdf(payslip_id):
        payslip = Payslip.query.get_or_404(payslip_id)
        is_owner = current_user.id == payslip.user_id
        is_admin = current_user.role == "admin"
        is_demo_owner = (
            current_user.role == "demo_admin"
            and payslip.user
            and payslip.tenant_id == current_user.tenant_id
        )
        if not (is_owner or is_admin or is_demo_owner):
            abort(403)

        pdf_buffer = generate_payslip_pdf(payslip)
        emp_code = payslip.user.employee_code if payslip.user else f"EMP{payslip.user_id}"
        month_name = payslip.month_label().replace(" ", "_")
        filename = f"Payslip_{emp_code}_{month_name}.pdf"

        return send_file(
            pdf_buffer,
            as_attachment=True,
            download_name=filename,
            mimetype="application/pdf"
        )

    @app.route("/employee/payslips")
    @role_required("employee", "manager")
    def employee_payslips():
        payslips = Payslip.query.filter_by(user_id=current_user.id)\
            .order_by(Payslip.year.desc(), Payslip.month.desc()).all()
        return render_template("payroll/employee_payslips.html", payslips=payslips)

    # -----------------------------------------------------------------
    # ATTENDANCE ROUTES
    # -----------------------------------------------------------------
    @app.route("/attendance", methods=["GET"])
    @role_required("employee", "manager", "admin")
    def attendance_index():
        today_att = get_today_attendance(current_user.id)
        today = today_ist()
        month = request.args.get("month", type=int) or today.month
        year = request.args.get("year", type=int) or today.year

        calendar_data = get_user_full_month_calendar(current_user.id, month, year)
        reg_requests = AttendanceRegularization.query.filter_by(user_id=current_user.id).order_by(AttendanceRegularization.applied_at.desc()).all()

        return render_template(
            "attendance/index.html",
            today_att=today_att,
            calendar_data=calendar_data,
            reg_requests=reg_requests,
            selected_month=month,
            selected_year=year
        )

    @app.route("/attendance/regularize", methods=["POST"])
    @login_required
    def attendance_regularize():
        date_str = request.form.get("date")
        date_obj = parse_date(date_str)
        clock_in_str = request.form.get("clock_in_time")
        clock_out_str = request.form.get("clock_out_time")
        status = request.form.get("status", "Present")
        reason = request.form.get("reason", "").strip()

        if not date_obj or not reason:
            flash("Select a past date and provide a valid reason.", "danger")
            return redirect(url_for("attendance_index"))

        cin_dt = datetime.combine(date_obj, datetime.strptime(clock_in_str, "%H:%M").time()) if clock_in_str else None
        cout_dt = datetime.combine(date_obj, datetime.strptime(clock_out_str, "%H:%M").time()) if clock_out_str else None

        _, success, msg = apply_attendance_regularization(
            current_user.id, date_obj, cin_dt, cout_dt, status, reason
        )
        flash(msg, "success" if success else "danger")
        return redirect(url_for("attendance_index", month=date_obj.month, year=date_obj.year))

    @app.route("/manager/regularizations")
    @role_required("manager")
    def manager_regularization_list():
        team_profiles = EmployeeProfile.query.filter_by(
            reporting_manager_id=current_user.id,
            tenant_id=current_user.tenant_id,
        ).join(User, User.id == EmployeeProfile.user_id).filter(User.is_active == True).all()
        team_user_ids = [p.user_id for p in team_profiles]

        requests = AttendanceRegularization.query.filter(
            AttendanceRegularization.user_id.in_(team_user_ids or [-1])
        ).order_by(AttendanceRegularization.applied_at.desc()).all()

        return render_template("attendance/manager_regularizations.html", requests=requests)

    @app.route("/regularizations/<int:reg_id>/decide", methods=["POST"])
    @login_required
    def decide_regularization_route(reg_id):
        reg = AttendanceRegularization.query.get_or_404(reg_id)
        is_admin = current_user.role in ["admin", "demo_admin"]
        is_manager = (
            current_user.role == "manager"
            and reg.user.profile
            and reg.user.tenant_id == current_user.tenant_id
            and reg.user.profile.reporting_manager_id == current_user.id
        )

        if not (is_admin or is_manager):
            abort(403)

        decision = request.form.get("decision")
        note = request.form.get("decision_note", "").strip()

        _, success, msg = decide_attendance_regularization(reg.id, current_user.id, decision, note)
        flash(msg, "success" if success else "danger")

        if is_admin:
            return redirect(url_for("admin_regularizations"))
        return redirect(url_for("manager_regularization_list"))

    @app.route("/admin/regularizations")
    @role_required("admin")
    def admin_regularizations():
        scoped_ids = [user.id for user in scope_to_practice_data(
            User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
        ).all()]
        requests = AttendanceRegularization.query.filter(
            AttendanceRegularization.user_id.in_(scoped_ids or [-1])
        ).order_by(AttendanceRegularization.applied_at.desc()).all()
        return render_template("attendance/admin_regularizations.html", requests=requests)


    @app.route("/attendance/clock-in", methods=["POST"])
    @login_required
    def attendance_clock_in():
        notes = request.form.get("notes", "").strip()
        rec, success, msg = clock_in_user(current_user.id, notes)
        flash(msg, "success" if success else "warning")
        return redirect(url_for("attendance_index"))

    @app.route("/attendance/clock-out", methods=["POST"])
    @login_required
    def attendance_clock_out():
        notes = request.form.get("notes", "").strip()
        rec, success, msg = clock_out_user(current_user.id, notes)
        flash(msg, "success" if success else "warning")
        return redirect(url_for("attendance_index"))

    @app.route("/manager/attendance")
    @role_required("manager")
    def manager_attendance():
        team_profiles = EmployeeProfile.query.filter_by(
            reporting_manager_id=current_user.id,
            tenant_id=current_user.tenant_id,
        ).all()
        team_user_ids = [p.user_id for p in team_profiles]

        today = today_ist()
        today_records = Attendance.query.filter(
            Attendance.user_id.in_(team_user_ids or [-1]),
            Attendance.date == today
        ).all()
        today_dict = {r.user_id: r for r in today_records}

        return render_template(
            "attendance/manager_team.html",
            team_profiles=team_profiles,
            today_dict=today_dict,
            today=today
        )

    @app.route("/admin/attendance")
    @role_required("admin")
    def admin_attendance():
        selected_date_str = request.args.get("date", today_ist().strftime("%Y-%m-%d"))
        selected_date = parse_date(selected_date_str) or today_ist()
        users = scope_to_practice_data(
            User.query.filter(
                User.role.in_(["employee", "manager", "demo_admin"]),
                User.is_active == True,
            )
        ).join(EmployeeProfile, EmployeeProfile.user_id == User.id).filter(
            db.or_(
                EmployeeProfile.date_of_joining <= selected_date,
                db.and_(EmployeeProfile.date_of_joining.is_(None), selected_date >= today_ist()),
            )
        ).order_by(User.employee_code).all()

        records = Attendance.query.filter(
            Attendance.date == selected_date,
            Attendance.user_id.in_([user.id for user in users] or [-1]),
        ).all()
        records_dict = {r.user_id: r for r in records}

        return render_template(
            "attendance/admin_manage.html",
            users=users,
            records_dict=records_dict,
            selected_date=selected_date
        )

    @app.route("/admin/attendance/mark", methods=["POST"])
    @role_required("admin")
    def admin_mark_attendance():
        user_id = int(request.form.get("user_id"))
        target = User.query.get_or_404(user_id)
        if not target.is_active:
            abort(403)
        if current_user.role == "demo_admin":
            if target.tenant_id != current_user.tenant_id:
                abort(403)
        elif current_user.role == "manager":
            if not target.profile or target.profile.reporting_manager_id != current_user.id:
                abort(403)
        elif current_user.role != "admin":
            abort(403)
        att_date = parse_date(request.form.get("date")) or today_ist()
        status = request.form.get("status", "Present")
        notes = request.form.get("notes", "").strip()

        rec = Attendance.query.filter_by(user_id=user_id, date=att_date).first()
        if rec:
            rec.status = status
            rec.notes = notes
        else:
            rec = Attendance(
                user_id=user_id,
                tenant_id=target.tenant_id,
                date=att_date,
                status=status,
                notes=notes,
            )
            db.session.add(rec)

        db.session.commit()
        flash("Attendance updated successfully.", "success")
        return redirect(url_for("admin_attendance", date=att_date.strftime("%Y-%m-%d")))

    # -----------------------------------------------------------------
    # HR RESOURCES, LETTERS & PEOPLE OPERATIONS
    # -----------------------------------------------------------------
    def populate_letter(template, employee, details):
        profile = employee.profile
        replacements = {
            "{{ employee_name }}": profile.full_name if profile else employee.employee_code,
            "{{ employee_code }}": employee.employee_code,
            "{{ designation }}": (profile.designation if profile else None) or "Employee",
            "{{ department }}": (profile.department if profile else None) or "-",
            "{{ company_name }}": "Sanvit HR Enterprise",
            "{{ details }}": details or "Please refer to the terms and details shared by Human Resources.",
        }
        content = template.body
        for placeholder, value in replacements.items():
            content = content.replace(placeholder, str(value))
        return content

    @app.route("/hr-resources")
    @login_required
    def hr_resources():
        tab = request.args.get("tab")
        search_query = request.args.get("q", "").strip()

        if search_query and not tab:
            tab = "resources"
        elif tab is None:
            tab = "letters"

        letters = EmployeeLetter.query.filter_by(employee_id=current_user.id).order_by(EmployeeLetter.requested_at.desc()).all()
        if current_user.role in ["admin", "demo_admin"]:
            scoped_ids = [user.id for user in scope_to_practice_data(
                User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
            ).all()]
            letters = EmployeeLetter.query.filter(
                EmployeeLetter.employee_id.in_(scoped_ids or [-1])
            ).order_by(EmployeeLetter.requested_at.desc()).all()
        templates = HRLetterTemplate.query.order_by(HRLetterTemplate.title.asc()).all()
        resource_query = ResourceDocument.query.filter_by(is_published=True)
        if current_user.role != "admin" and current_user.tenant_id:
            resource_query = resource_query.join(User, User.id == ResourceDocument.uploaded_by_id).filter(
                User.tenant_id == current_user.tenant_id
            )
        resources = resource_query.order_by(ResourceDocument.category, ResourceDocument.created_at.desc()).all()
        policy_matches = []
        policy_answer = None
        resources_to_display = resources

        if tab == "resources" and search_query:
            policy_matches = search_policy_documents(search_query, resources)
            resources_to_display = [match["resource"] for match in policy_matches]
            policy_answer = build_policy_answer(search_query, resources)

        meetings = AppraisalMeeting.query
        memories = HRMemory.query
        if current_user.role != "admin" and current_user.tenant_id:
            meetings = meetings.join(User, User.id == AppraisalMeeting.created_by_id).filter(User.tenant_id == current_user.tenant_id)
            memories = memories.join(User, User.id == HRMemory.created_by_id).filter(User.tenant_id == current_user.tenant_id)
        meetings = meetings.order_by(AppraisalMeeting.scheduled_for.asc()).all()
        memories = memories.order_by(HRMemory.event_date.desc(), HRMemory.created_at.desc()).all()
        employees = scope_to_practice_data(
            User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
        ).join(
            EmployeeProfile, EmployeeProfile.user_id == User.id
        ).order_by(EmployeeProfile.full_name.asc()).all()
        return render_template(
            "hr_resources.html", tab=tab, letters=letters, templates=templates,
            resources=resources_to_display, meetings=meetings, memories=memories,
            employees=employees, search_query=search_query, policy_answer=policy_answer,
            policy_matches=policy_matches,
        )

    @app.route("/hr-resources/letters/request", methods=["POST"])
    @login_required
    def request_hr_letter():
        template = HRLetterTemplate.query.get_or_404(request.form.get("template_id", type=int))
        if template.hr_only:
            abort(403)
        subject = request.form.get("subject", "").strip() or template.title
        details = request.form.get("details", "").strip()
        db.session.add(EmployeeLetter(
            tenant_id=current_user.tenant_id,
            employee_id=current_user.id,
            template_id=template.id,
            subject=subject,
            content=populate_letter(template, current_user, details),
        ))
        db.session.commit()
        flash("Your letter request has been sent to HR.", "success")
        return redirect(url_for("hr_resources", tab="letters"))

    @app.route("/admin/hr-resources/letters/issue", methods=["POST"])
    @role_required("admin")
    def issue_hr_letter():
        # Issuing official HR letters is restricted to real admins.
        real_admin_only()
        template = HRLetterTemplate.query.get_or_404(request.form.get("template_id", type=int))
        employee = scoped_user_or_403(request.form.get("employee_id", type=int))
        details = request.form.get("details", "").strip()
        letter_id = request.form.get("letter_id", type=int)
        letter = EmployeeLetter.query.get(letter_id) if letter_id else None
        if letter and letter.employee and current_user.role != "admin" and letter.employee.tenant_id != current_user.tenant_id:
            abort(403)
        if not letter:
            letter = EmployeeLetter(
                tenant_id=current_user.tenant_id,
                employee_id=employee.id, template_id=template.id,
                subject=request.form.get("subject", "").strip() or template.title,
                content="",
            )
            db.session.add(letter)
        letter.template_id = template.id
        letter.employee_id = employee.id
        letter.subject = request.form.get("subject", "").strip() or template.title
        letter.content = populate_letter(template, employee, details)
        letter.status = "Issued"
        letter.issued_by_id = current_user.id
        letter.issued_at = now_ist()
        db.session.commit()
        flash("Letter issued and made visible to the employee.", "success")
        return redirect(url_for("hr_resources", tab="letters"))

    @app.route("/admin/hr-resources/letter-templates/<int:template_id>", methods=["POST"])
    @role_required("admin")
    def update_hr_letter_template(template_id):
        real_admin_only()
        template = HRLetterTemplate.query.get_or_404(template_id)
        template.title = request.form.get("title", "").strip() or template.title
        template.body = request.form.get("body", "").strip() or template.body
        db.session.commit()
        flash("Letter format updated for future issues.", "success")
        return redirect(url_for("hr_resources", tab="letters"))

    @app.route("/hr-resources/letters/<int:letter_id>")
    @login_required
    def view_hr_letter(letter_id):
        letter = EmployeeLetter.query.get_or_404(letter_id)
        employee = letter.employee
        platform_admin = current_user.role == "admin"
        same_tenant = employee and employee.tenant_id == current_user.tenant_id
        tenant_admin = current_user.role == "demo_admin" and same_tenant
        if not platform_admin and not tenant_admin and letter.employee_id != current_user.id:
            abort(403)
        if letter.status != "Issued" and not platform_admin:
            abort(403)
        return render_template("hr_letter_view.html", letter=letter)

    @app.route("/hr-resources/letters/<int:letter_id>/download")
    @login_required
    def download_hr_letter(letter_id):
        letter = EmployeeLetter.query.get_or_404(letter_id)
        employee = letter.employee
        platform_admin = current_user.role == "admin"
        same_tenant = employee and employee.tenant_id == current_user.tenant_id
        tenant_admin = current_user.role == "demo_admin" and same_tenant
        if not platform_admin and not tenant_admin and letter.employee_id != current_user.id:
            abort(403)
        if letter.status != "Issued" and not platform_admin:
            abort(403)
        return Response(
            letter.content,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={letter.subject.replace(' ', '_')}.txt"},
        )

    @app.route("/admin/hr-resources/resources/upload", methods=["POST"])
    @role_required("admin")
    def upload_hr_resource():
        real_admin_only()
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Choose a handbook, policy, or form file first.", "danger")
            return redirect(url_for("hr_resources", tab="resources"))
        extension = os.path.splitext(file.filename)[1].lower()
        if extension not in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg"}:
            flash("Only PDF, Word, Excel, and image files are supported.", "danger")
            return redirect(url_for("hr_resources", tab="resources"))
        if not has_valid_file_signature(file, extension):
            flash("The uploaded file type does not match its extension.", "danger")
            return redirect(url_for("hr_resources", tab="resources"))
        filename = save_private_upload(file, "resource")
        db.session.add(ResourceDocument(
            tenant_id=current_user.tenant_id,
            title=request.form.get("title", "").strip() or file.filename,
            category=request.form.get("category", "Policy"),
            description=request.form.get("description", "").strip(),
            filename=filename, uploaded_by_id=current_user.id,
        ))
        db.session.commit()
        flash("Resource published for all employees.", "success")
        return redirect(url_for("hr_resources", tab="resources"))

    @app.route("/hr-resources/resources/<int:resource_id>/download")
    @login_required
    def download_hr_resource(resource_id):
        resource = ResourceDocument.query.get_or_404(resource_id)
        if current_user.role != "admin" and resource.tenant_id not in {None, current_user.tenant_id}:
            abort(403)
        if current_user.role == "demo_admin" and resource.uploaded_by_id != current_user.id:
            abort(403)
        if not resource.is_published and current_user.role != "admin":
            abort(404)
        return send_private_file(resource.filename, download_name=resource.filename)

    @app.route("/admin/appraisals/meetings", methods=["POST"])
    @role_required("admin")
    def add_appraisal_meeting():
        scheduled_for = request.form.get("scheduled_for", "")
        try:
            meeting_time = datetime.strptime(scheduled_for, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("Enter a valid meeting date and time.", "danger")
            return redirect(url_for("hr_resources", tab="appraisals"))
        employee_id = request.form.get("employee_id", type=int)
        employee = User.query.get(employee_id) if employee_id else None
        if employee and current_user.role != "admin" and employee.tenant_id != current_user.tenant_id:
            abort(403)
        db.session.add(AppraisalMeeting(
            title=request.form.get("title", "Appraisal meeting").strip(),
            scheduled_for=meeting_time,
            location=request.form.get("location", "").strip(),
            notes=request.form.get("notes", "").strip(),
            employee_id=employee.id if employee else None,
            created_by_id=current_user.id,
            tenant_id=current_user.tenant_id,
        ))
        db.session.commit()
        flash("Appraisal meeting added to the shared calendar.", "success")
        return redirect(url_for("hr_resources", tab="appraisals"))

    @app.route("/admin/hr-resources/memories/upload", methods=["POST"])
    @role_required("admin")
    def upload_hr_memory():
        real_admin_only()
        file = request.files.get("image")
        filename = None
        if file and file.filename:
            extension = os.path.splitext(file.filename)[1].lower()
            if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
                flash("Memories support PNG, JPG, and WEBP images.", "danger")
                return redirect(url_for("hr_resources", tab="memories"))
            if not has_valid_file_signature(file, extension):
                flash("The uploaded file type does not match its extension.", "danger")
                return redirect(url_for("hr_resources", tab="memories"))
            filename = save_private_upload(file, "memory")
        db.session.add(HRMemory(
            tenant_id=current_user.tenant_id,
            title=request.form.get("title", "").strip() or "HR Event",
            event_date=parse_date(request.form.get("event_date")),
            description=request.form.get("description", "").strip(),
            image_filename=filename, created_by_id=current_user.id,
        ))
        db.session.commit()
        flash("HR memory published for the team.", "success")
        return redirect(url_for("hr_resources", tab="memories"))

    # -----------------------------------------------------------------
    # APPRAISAL ROUTES
    # -----------------------------------------------------------------
    @app.route("/appraisals")
    @role_required("employee", "manager", "admin")
    def appraisals_index():
        appraisals = Appraisal.query.filter_by(user_id=current_user.id)\
            .order_by(Appraisal.id.desc()).all()
        meetings = AppraisalMeeting.query.filter(
            db.or_(AppraisalMeeting.tenant_id.is_(None), AppraisalMeeting.tenant_id == current_user.tenant_id)
        ).order_by(AppraisalMeeting.scheduled_for.asc()).all()
        return render_template("appraisals/index.html", appraisals=appraisals, meetings=meetings)

    @app.route("/admin/appraisals")
    @role_required("admin")
    def admin_appraisals():
        employees = scope_to_practice_data(
            User.query.filter(
                User.role.in_(["employee", "manager"]),
                User.is_active == True,
            )
        ).order_by(User.employee_code).all()
        appraisals = Appraisal.query.filter(
            Appraisal.user_id.in_([employee.id for employee in employees] or [-1])
        ).order_by(Appraisal.id.desc()).all()
        statuses = [
            "Initiated", "KRA Submitted for Approval", "KRA Approved (Eligible for Rating)",
            "Self Review Submitted", "Completed", "Rating Sent Back for Edit",
        ]
        status_counts = [sum(1 for item in appraisals if item.status == status) for status in statuses]
        self_averages = []
        manager_averages = []
        for status in statuses:
            rows = [item for item in appraisals if item.status == status]
            self_values = [item.self_rating for item in rows if item.self_rating is not None]
            manager_values = [item.rating for item in rows if item.rating is not None]
            self_averages.append(round(sum(self_values) / len(self_values), 2) if self_values else 0)
            manager_averages.append(round(sum(manager_values) / len(manager_values), 2) if manager_values else 0)
        return render_template(
            "appraisals/admin_list.html",
            appraisals=appraisals,
            employees=employees,
            view=request.args.get("view", "overview"),
            chart_statuses=statuses,
            status_counts=status_counts,
            self_averages=self_averages,
            manager_averages=manager_averages,
        )

    @app.route("/admin/appraisals/<int:appraisal_id>/delete", methods=["POST"])
    @role_required("admin")
    def admin_delete_appraisal(appraisal_id):
        appraisal = Appraisal.query.get_or_404(appraisal_id)
        if current_user.role == "demo_admin":
            if not appraisal.user or appraisal.user.created_by_id != current_user.id:
                abort(403)
        elif current_user.role != "admin":
            abort(403)
        db.session.delete(appraisal)
        db.session.commit()
        flash("Appraisal and its KRA/KPI records were deleted.", "success")
        return redirect(url_for("admin_appraisals", view="all"))

    @app.route("/manager/appraisals")
    @role_required("manager")
    def manager_appraisals():
        team_profiles = EmployeeProfile.query.filter_by(
            reporting_manager_id=current_user.id,
            tenant_id=current_user.tenant_id,
        ).all()
        team_user_ids = [p.user_id for p in team_profiles]

        appraisals = Appraisal.query.filter(
            Appraisal.user_id.in_(team_user_ids or [-1])
        ).order_by(Appraisal.id.desc()).all()

        return render_template("appraisals/manager_list.html", appraisals=appraisals)

    @app.route("/manager/appraisals/9-box")
    @role_required("manager")
    def manager_appraisal_matrix():
        team_profiles = EmployeeProfile.query.filter_by(
            reporting_manager_id=current_user.id,
            tenant_id=current_user.tenant_id,
        ).all()
        team_user_ids = [p.user_id for p in team_profiles]
        appraisals = Appraisal.query.filter(
            Appraisal.user_id.in_(team_user_ids or [-1]),
            Appraisal.status == "Completed",
        ).order_by(Appraisal.updated_at.desc()).all()

        def band(value):
            if value is None:
                return None
            if value >= 4:
                return "high"
            if value >= 3:
                return "medium"
            return "low"

        labels = {
            ("high", "high"): "Future Leader",
            ("high", "medium"): "Growth Player",
            ("high", "low"): "Potential Gem",
            ("medium", "high"): "High Performer",
            ("medium", "medium"): "Core Player",
            ("medium", "low"): "Steady Contributor",
            ("low", "high"): "Trusted Specialist",
            ("low", "medium"): "Effective Contributor",
            ("low", "low"): "Needs Support",
        }
        cells = {}
        unassessed = []
        for appraisal in appraisals:
            performance = band(appraisal.rating)
            potential = band(appraisal.potential_rating)
            if not performance or not potential:
                unassessed.append(appraisal)
                continue
            key = (potential, performance)
            cells.setdefault(key, []).append(appraisal)

        return render_template(
            "appraisals/matrix.html", cells=cells, labels=labels,
            unassessed=unassessed, assessed_count=sum(len(items) for items in cells.values()),
        )

    @app.route("/admin/appraisals/9-box")
    @role_required("admin")
    def admin_appraisal_matrix():
        employees = scope_to_practice_data(
            User.query.filter(User.role.in_(["employee", "manager"]), User.is_active == True)
        ).all()
        employee_ids = [employee.id for employee in employees]
        appraisals = Appraisal.query.filter(
            Appraisal.user_id.in_(employee_ids or [-1]),
            Appraisal.status == "Completed",
        ).order_by(Appraisal.updated_at.desc()).all()

        def band(value):
            if value is None:
                return None
            if value >= 4:
                return "high"
            if value >= 3:
                return "medium"
            return "low"

        labels = {
            ("high", "high"): "Future Leader", ("high", "medium"): "Growth Player", ("high", "low"): "Potential Gem",
            ("medium", "high"): "High Performer", ("medium", "medium"): "Core Player", ("medium", "low"): "Steady Contributor",
            ("low", "high"): "Trusted Specialist", ("low", "medium"): "Effective Contributor", ("low", "low"): "Needs Support",
        }
        cells = {}
        unassessed = []
        for appraisal in appraisals:
            key = (band(appraisal.potential_rating), band(appraisal.rating))
            if None in key:
                unassessed.append(appraisal)
            else:
                cells.setdefault(key, []).append(appraisal)
        return render_template(
            "appraisals/matrix.html", cells=cells, labels=labels,
            unassessed=unassessed, assessed_count=sum(len(items) for items in cells.values()),
            matrix_title="Organization 9-box Matrix", matrix_scope="All completed appraisals",
            back_endpoint="admin_appraisals",
        )

    @app.route("/admin/appraisals/initiate", methods=["POST"])
    @role_required("admin")
    def admin_initiate_appraisal():
        period_type = request.form.get("period_type", "Q1 (Sep-Oct)")
        year = int(request.form.get("year") or 2026)
        user_id_val = request.form.get("user_id")

        if user_id_val == "all":
            employees = scope_to_practice_data(
                User.query.filter(
                    User.role.in_(["employee", "manager"]),
                    User.is_active == True,
                )
            ).all()
            count = 0
            for emp in employees:
                evaluator_id = emp.profile.reporting_manager_id if emp.profile else None
                _, success, _ = create_appraisal(emp.id, evaluator_id, period_type, year)
                if success:
                    count += 1
            flash(f"Appraisal cycle '{period_type} {year}' initiated for {count} employee(s)!", "success")
        else:
            emp = scoped_user_or_403(int(user_id_val))
            if not emp.is_active or emp.role not in {"employee", "manager"}:
                flash("Appraisals can only be initiated for active employees and managers.", "warning")
                return redirect(url_for("admin_appraisals"))
            evaluator_id = emp.profile.reporting_manager_id if emp.profile else None
            _, success, msg = create_appraisal(emp.id, evaluator_id, period_type, year)
            flash(msg, "success" if success else "warning")

        return redirect(url_for("admin_appraisals"))

    @app.route("/appraisals/<int:appraisal_id>", methods=["GET", "POST"])
    @login_required
    def view_edit_appraisal(appraisal_id):
        appraisal = Appraisal.query.get_or_404(appraisal_id)

        is_owner = current_user.id == appraisal.user_id
        is_admin = current_user.role == "admin"
        is_demo_owner = (
            current_user.role == "demo_admin"
            and appraisal.user
            and appraisal.tenant_id == current_user.tenant_id
            and appraisal.user.tenant_id == current_user.tenant_id
            and appraisal.user.created_by_id == current_user.id
        )
        is_their_manager = (
            current_user.role == "manager"
            and appraisal.tenant_id == current_user.tenant_id
            and appraisal.user.tenant_id == current_user.tenant_id
            and appraisal.user.profile
            and appraisal.user.profile.reporting_manager_id == current_user.id
        )

        if not (is_owner or is_admin or is_demo_owner or is_their_manager):
            abort(403)

        if request.method == "POST":
            action = request.form.get("action")

            # STAGE 1: Employee submits defined KRAs to Manager for approval
            if action == "submit_kras" and is_owner:
                if appraisal.status not in ["Initiated", "KRA Draft", "KRA Sent Back for Edit"]:
                    flash("KRAs cannot be modified once submitted to Manager or approved.", "danger")
                    return redirect(url_for("view_edit_appraisal", appraisal_id=appraisal.id))

                kras_data = []
                for k_idx in range(5):
                    kra_title = request.form.get(f"kra_title_{k_idx}")
                    if not kra_title:
                        continue
                    kra_weight = float(request.form.get(f"kra_weight_{k_idx}") or 0.0)

                    kpis = []
                    for kp_idx in range(6):
                        kpi_title = request.form.get(f"kpi_title_{k_idx}_{kp_idx}")
                        if not kpi_title:
                            continue
                        formula = request.form.get(f"kpi_formula_{k_idx}_{kp_idx}", "").strip()
                        target = float(request.form.get(f"kpi_target_{k_idx}_{kp_idx}") or 100.0)
                        actual = float(request.form.get(f"kpi_actual_{k_idx}_{kp_idx}") or 0.0)
                        unit = request.form.get(f"kpi_unit_{k_idx}_{kp_idx}", "%").strip()
                        kpi_weight = float(request.form.get(f"kpi_weight_{k_idx}_{kp_idx}") or 100.0)

                        kpis.append({
                            "title": kpi_title,
                            "metric_formula_desc": formula,
                            "target_value": target,
                            "actual_value": actual,
                            "unit": unit,
                            "weightage_percent": kpi_weight
                        })

                    kras_data.append({
                        "title": kra_title,
                        "weightage_percent": kra_weight,
                        "kpis": kpis
                    })

                _, success, msg = submit_kras_for_approval(appraisal.id, kras_data)
                flash(msg, "success" if success else "danger")
                return redirect(url_for("view_edit_appraisal", appraisal_id=appraisal.id))

            # STAGE 1 MANAGER ACTION: Approve or Send Back KRAs for Edit
            elif action == "decide_kras" and (is_their_manager or is_admin or is_demo_owner):
                decision = request.form.get("decision", "approve")  # approve or send_back
                note = request.form.get("sendback_note", "").strip()
                _, success, msg = decide_kras_approval(appraisal.id, current_user.id, decision, note)
                flash(msg, "success" if success else "danger")
                return redirect(url_for("view_edit_appraisal", appraisal_id=appraisal.id))

            # STAGE 2: Employee Submits Self-Ratings for each KRA/KPI
            elif action == "submit_ratings" and is_owner:
                if appraisal.status not in ["KRA Approved (Eligible for Rating)", "Rating Sent Back for Edit"]:
                    flash("Self-ratings can only be submitted after your Manager approves your KRAs.", "danger")
                    return redirect(url_for("view_edit_appraisal", appraisal_id=appraisal.id))

                self_rating = request.form.get("self_rating")
                self_comments = request.form.get("self_comments", "").strip()

                kra_ratings = {}
                kpi_ratings = {}
                for kra in appraisal.kras:
                    r_val = request.form.get(f"kra_self_rating_{kra.id}")
                    if r_val:
                        kra_ratings[str(kra.id)] = float(r_val)
                    for kpi in kra.kpis:
                        kp_val = request.form.get(f"kpi_self_rating_{kpi.id}")
                        if kp_val:
                            kpi_ratings[str(kpi.id)] = float(kp_val)

                _, success, msg = submit_self_ratings(
                    appraisal.id, self_rating, self_comments, kra_ratings=kra_ratings, kpi_ratings=kpi_ratings
                )
                flash(msg, "success" if success else "danger")
                return redirect(url_for("view_edit_appraisal", appraisal_id=appraisal.id))

            # STAGE 2 MANAGER ACTION: Manager Ratings Evaluation & Final Approval / Send Back
            elif action == "manager_review" and (is_their_manager or is_admin or is_demo_owner):
                decision = request.form.get("decision", "complete")  # complete or send_back
                rating = request.form.get("rating")
                potential_rating = request.form.get("potential_rating")
                evaluator_comments = request.form.get("evaluator_comments", "").strip()
                note = request.form.get("sendback_note", "").strip()

                try:
                    if potential_rating is not None and float(potential_rating) not in {1.0, 2.0, 3.0, 4.0, 5.0}:
                        raise ValueError
                except (TypeError, ValueError):
                    flash("Potential rating must be between 1 and 5.", "danger")
                    return redirect(url_for("view_edit_appraisal", appraisal_id=appraisal.id))

                kra_ratings = {}
                kpi_ratings = {}
                for kra in appraisal.kras:
                    r_val = request.form.get(f"kra_manager_rating_{kra.id}")
                    if r_val:
                        kra_ratings[str(kra.id)] = float(r_val)
                    for kpi in kra.kpis:
                        kp_val = request.form.get(f"kpi_manager_rating_{kpi.id}")
                        if kp_val:
                            kpi_ratings[str(kpi.id)] = float(kp_val)

                _, success, msg = submit_manager_ratings(
                    appraisal.id, current_user.id, rating, evaluator_comments,
                    decision=decision, sendback_note=note, kra_ratings=kra_ratings, kpi_ratings=kpi_ratings
                )
                if success and potential_rating:
                    appraisal.potential_rating = float(potential_rating)
                    db.session.commit()
                flash(msg, "success" if success else "danger")
                return redirect(url_for("view_edit_appraisal", appraisal_id=appraisal.id))

        return render_template(
            "appraisals/detail.html",
            appraisal=appraisal,
            is_owner=is_owner,
            is_evaluator=(is_their_manager or is_admin or is_demo_owner)
        )

    # -----------------------------------------------------------------
    # ANALYTICS DASHBOARD
    # -----------------------------------------------------------------
    @app.route("/admin/analytics")
    @app.route("/analytics")
    @role_required("admin", "manager")
    def admin_analytics_dashboard():
        month = request.args.get("month", type=int) or today_ist().month
        year = request.args.get("year", type=int) or today_ist().year
        practice_ids = None
        if current_user.role == "demo_admin":
            practice_ids = [user.id for user in scope_to_practice_data(
                User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
            ).all()] or [-1]
        elif current_user.role == "manager":
            practice_ids = [profile.user_id for profile in EmployeeProfile.query.filter_by(
                reporting_manager_id=current_user.id,
                tenant_id=current_user.tenant_id,
            ).all()] or [-1]

        rating_rows = []
        appraisal_query = Appraisal.query.filter_by(year=year)
        if practice_ids is not None:
            appraisal_query = appraisal_query.filter(Appraisal.user_id.in_(practice_ids))
        for appraisal in appraisal_query.all():
            if appraisal.rating is None and appraisal.self_rating is None:
                continue
            employee_name = appraisal.user.profile.full_name if appraisal.user and appraisal.user.profile else appraisal.user.employee_code if appraisal.user else "Unknown"
            dept_name = (appraisal.user.profile.department if appraisal.user and appraisal.user.profile else "Unassigned") or "Unassigned"
            mgr_name = (appraisal.user.profile.reporting_manager.profile.full_name if appraisal.user and appraisal.user.profile and appraisal.user.profile.reporting_manager and appraisal.user.profile.reporting_manager.profile else "Unassigned")
            rating_rows.append(SimpleNamespace(
                employee_name=employee_name,
                rating=float(appraisal.rating or 0.0),
                self_rating=float(appraisal.self_rating or 0.0),
                potential_rating=float(appraisal.potential_rating or 0.0),
                department=dept_name,
                manager_name=mgr_name,
            ))

        start = date(year, month, 1)
        if month == 12:
            end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(year, month + 1, 1) - timedelta(days=1)

        attendance_rows = []
        status_query = db.session.query(Attendance.status, db.func.count(Attendance.id).label("count")).filter(
            Attendance.date >= start,
            Attendance.date <= end,
        ).group_by(Attendance.status).all()
        if practice_ids is not None:
            status_query = db.session.query(
                Attendance.status, db.func.count(Attendance.id).label("count")
            ).filter(
                Attendance.date >= start,
                Attendance.date <= end,
                Attendance.user_id.in_(practice_ids),
                Attendance.tenant_id == current_user.tenant_id,
            ).group_by(Attendance.status).all()

        for status, count in status_query:
            attendance_rows.append(SimpleNamespace(status=status or "Unknown", count=int(count or 0)))

        location_rows = []
        city_query = db.session.query(
            EmployeeProfile.current_city.label("city"),
            db.func.count(EmployeeProfile.id).label("count"),
        ).filter(
            EmployeeProfile.current_city.isnot(None),
            EmployeeProfile.current_city != "",
        ).group_by(EmployeeProfile.current_city).order_by(db.desc("count")).limit(10).all()
        if practice_ids is not None:
            city_query = db.session.query(
                EmployeeProfile.current_city.label("city"),
                db.func.count(EmployeeProfile.id).label("count"),
            ).filter(
                EmployeeProfile.user_id.in_(practice_ids),
                EmployeeProfile.tenant_id == current_user.tenant_id,
                EmployeeProfile.current_city.isnot(None),
                EmployeeProfile.current_city != "",
            ).group_by(EmployeeProfile.current_city).order_by(db.desc("count")).limit(10).all()

        for city, count in city_query:
            location_rows.append(SimpleNamespace(city=city, count=int(count or 0)))

        summary = build_people_analytics(rating_rows, attendance_rows, location_rows)
        return render_template(
            "admin/analytics.html",
            summary=summary,
            rating_rows=sorted(rating_rows, key=lambda row: row.rating, reverse=True),
            attendance_rows=sorted(attendance_rows, key=lambda row: row.count, reverse=True),
            location_rows=sorted(location_rows, key=lambda row: row.count, reverse=True),
            selected_month=month,
            selected_year=year,
        )

    @app.route("/admin/analytics/export")
    @role_required("admin", "manager")
    def export_analytics_dashboard():
        real_admin_only()
        month = request.args.get("month", type=int) or today_ist().month
        year = request.args.get("year", type=int) or today_ist().year
        rating_rows = []
        for appraisal in Appraisal.query.filter_by(year=year).all():
            if appraisal.rating is None and appraisal.self_rating is None:
                continue
            employee_name = appraisal.user.profile.full_name if appraisal.user and appraisal.user.profile else appraisal.user.employee_code if appraisal.user else "Unknown"
            rating_rows.append({
                "Employee": employee_name,
                "Department": (appraisal.user.profile.department if appraisal.user and appraisal.user.profile else "Unassigned") or "Unassigned",
                "Manager": (appraisal.user.profile.reporting_manager.profile.full_name if appraisal.user and appraisal.user.profile and appraisal.user.profile.reporting_manager and appraisal.user.profile.reporting_manager.profile else "Unassigned"),
                "Manager Rating": float(appraisal.rating or 0.0),
                "Self Rating": float(appraisal.self_rating or 0.0),
            })

        output = []
        output.append(["Employee", "Department", "Manager", "Manager Rating", "Self Rating"])
        for item in rating_rows:
            output.append([
                item["Employee"],
                item["Department"],
                item["Manager"],
                item["Manager Rating"],
                item["Self Rating"],
            ])

        import csv
        from io import StringIO
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerows(output)
        csv_data = buffer.getvalue()
        return send_file(
            csv_to_styled_xlsx(csv_data, "Analytics"),
            as_attachment=True,
            download_name=f"HR_Analytics_{year}_{month}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    # -----------------------------------------------------------------
    # EXPORT REPORTS ROUTES (Employees, Attendance, Payroll, Appraisals)
    # -----------------------------------------------------------------
    @app.route("/admin/reports")
    @app.route("/reports")
    @role_required("admin")
    def admin_reports_dashboard():
        real_admin_only()
        today = today_ist()
        return render_template("reports/index.html", current_month=today.month, current_year=today.year)

    @app.route("/admin/reports/employees/export")
    @role_required("admin")
    def export_employee_report():
        real_admin_only()
        audit_event("export_employee_report")
        csv_data = generate_employee_report_csv(current_user.tenant_id)
        return send_file(
            csv_to_styled_xlsx(csv_data, "Employees"),
            as_attachment=True,
            download_name="Employees_Master_Report.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/admin/reports/attendance/export")
    @role_required("admin")
    def export_attendance_report():
        real_admin_only()
        audit_event("export_attendance_report")
        month = request.args.get("month", type=int)
        year = request.args.get("year", type=int)
        csv_data = generate_attendance_report_csv(month, year, current_user.tenant_id)
        return send_file(
            csv_to_styled_xlsx(csv_data, "Attendance"),
            as_attachment=True,
            download_name=f"Attendance_Report_{month or 'All'}_{year or 'All'}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/admin/reports/payroll/export")
    @role_required("admin")
    def export_payroll_report():
        real_admin_only()
        audit_event("export_payroll_report")
        month = request.args.get("month", type=int)
        year = request.args.get("year", type=int)
        csv_data = generate_payroll_report_csv(month, year, current_user.tenant_id)
        return send_file(
            csv_to_styled_xlsx(csv_data, "Payroll"),
            as_attachment=True,
            download_name=f"Payroll_Report_{month or 'All'}_{year or 'All'}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/admin/payroll/generate/export")
    @role_required("admin")
    def export_payroll_input_sheet():
        real_admin_only()
        audit_event("export_payroll_input_sheet")
        month = request.args.get("month", type=int) or today_ist().month
        year = request.args.get("year", type=int) or today_ist().year
        csv_data = generate_payroll_input_sheet_csv(month, year, current_user.tenant_id)
        return send_file(
            csv_to_styled_xlsx(csv_data, "Payroll Input"),
            as_attachment=True,
            download_name=f"Payroll_Input_Sheet_{month}_{year}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/admin/reports/appraisals/export")
    @role_required("admin")
    def export_appraisal_report():
        real_admin_only()
        audit_event("export_appraisal_report")
        period_type = request.args.get("period_type")
        year = request.args.get("year")
        csv_data = generate_appraisal_report_csv(period_type, year, current_user.tenant_id)
        return send_file(
            csv_to_styled_xlsx(csv_data, "Appraisals"),
            as_attachment=True,
            download_name="Performance_Appraisals_Report.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


    # -----------------------------------------------------------------
    # BULK ATTENDANCE ROUTES (Admin, Manager & Employee Full-Month Approval)
    # -----------------------------------------------------------------
    @app.route("/admin/attendance/bulk", methods=["GET", "POST"])
    @role_required("admin")
    def admin_bulk_attendance():
        selected_start = parse_date(request.args.get("start_date")) or today_ist()
        employees = scope_to_practice_data(
            User.query.filter(
                User.role.in_(["employee", "manager", "demo_admin"]),
                User.is_active == True,
            )
        ).join(EmployeeProfile, EmployeeProfile.user_id == User.id).filter(
            db.or_(
                EmployeeProfile.date_of_joining <= selected_start,
                db.and_(EmployeeProfile.date_of_joining.is_(None), selected_start >= today_ist()),
            )
        ).order_by(User.employee_code).all()

        if request.method == "POST":
            user_ids = [int(uid) for uid in request.form.getlist("user_ids")]
            start_date = parse_date(request.form.get("start_date"))
            end_date = parse_date(request.form.get("end_date")) or start_date
            status = request.form.get("status", "Present")
            notes = request.form.get("notes", "").strip()

            if not user_ids or not start_date:
                flash("Select at least one employee and a start date.", "danger")
                return render_template("attendance/bulk_mark.html", employees=employees, role_mode="admin")

            allowed_ids = {employee.id for employee in employees}
            if not set(user_ids).issubset(allowed_ids):
                abort(403)

            count, msg = bulk_mark_attendance(user_ids, start_date, end_date, status, notes)
            flash(msg, "success")
            return redirect(url_for("admin_attendance", date=start_date.strftime("%Y-%m-%d")))

        return render_template("attendance/bulk_mark.html", employees=employees, role_mode="admin")

    @app.route("/manager/attendance/bulk", methods=["GET", "POST"])
    @role_required("manager")
    def manager_bulk_attendance():
        team_profiles = EmployeeProfile.query.filter_by(
            reporting_manager_id=current_user.id,
            tenant_id=current_user.tenant_id,
        ).all()
        team_user_ids = [p.user_id for p in team_profiles]
        team_members = User.query.filter(
            User.id.in_(team_user_ids or [-1]), User.is_active == True
        ).order_by(User.employee_code).all()

        if request.method == "POST":
            user_ids = [int(uid) for uid in request.form.getlist("user_ids")]
            start_date = parse_date(request.form.get("start_date"))
            end_date = parse_date(request.form.get("end_date")) or start_date
            status = request.form.get("status", "Present")
            notes = request.form.get("notes", "").strip()

            if not user_ids or not start_date:
                flash("Select at least one team member and a start date.", "danger")
                return render_template("attendance/bulk_mark.html", employees=team_members, role_mode="manager")

            count, msg = bulk_mark_attendance(user_ids, start_date, end_date, status, notes)
            flash(msg, "success")
            return redirect(url_for("manager_attendance"))

        return render_template("attendance/bulk_mark.html", employees=team_members, role_mode="manager")

    @app.route("/attendance/bulk", methods=["GET", "POST"])
    @login_required
    def employee_bulk_attendance():
        if request.method == "POST":
            start_date = parse_date(request.form.get("start_date"))
            end_date = parse_date(request.form.get("end_date")) or start_date
            status = request.form.get("status", "Present")
            notes = request.form.get("notes", "").strip()

            if not start_date:
                flash("Select a start date.", "danger")
                return render_template("attendance/bulk_mark.html", employees=[current_user], role_mode="employee")

            count, msg = bulk_mark_attendance([current_user.id], start_date, end_date, status, notes)
            flash(msg, "success")
            return redirect(url_for("attendance_index"))

        return render_template("attendance/bulk_mark.html", employees=[current_user], role_mode="employee")


    # -----------------------------------------------------------------
    # SPECIAL APPROVAL ROUTES (Founding Member Auto-Approval)
    # -----------------------------------------------------------------
    @app.route("/special-approvals", methods=["GET", "POST"])
    @role_required("employee", "manager", "admin")
    def special_approvals_index():
        if request.method == "POST":
            approval_type = request.form.get("approval_type")
            start_date = parse_date(request.form.get("start_date"))
            end_date = parse_date(request.form.get("end_date")) or start_date
            days = float(request.form.get("days") or 1.0)
            reason = request.form.get("reason", "").strip()

            if not approval_type or not start_date:
                flash("Select an approval type and start date.", "danger")
                return redirect(url_for("special_approvals_index"))

            appr, success, msg = apply_special_approval(
                current_user.id, approval_type, start_date, end_date, days, reason
            )
            flash(msg, "success" if success else "danger")
            return redirect(url_for("special_approvals_index"))

        requests = SpecialApproval.query.filter_by(user_id=current_user.id).order_by(SpecialApproval.applied_at.desc()).all()
        return render_template("special_approvals/index.html", requests=requests)

    @app.route("/manager/special-approvals")
    @role_required("manager")
    def manager_special_approvals():
        team_profiles = EmployeeProfile.query.filter_by(
            reporting_manager_id=current_user.id,
            tenant_id=current_user.tenant_id,
        ).all()
        team_user_ids = [p.user_id for p in team_profiles]

        requests = SpecialApproval.query.filter(
            SpecialApproval.user_id.in_(team_user_ids or [-1])
        ).order_by(SpecialApproval.applied_at.desc()).all()

        return render_template("special_approvals/manager_list.html", requests=requests)

    @app.route("/special-approvals/<int:approval_id>/decide", methods=["POST"])
    @login_required
    def decide_special_approval_route(approval_id):
        appr = SpecialApproval.query.get_or_404(approval_id)
        is_admin = current_user.role in ["admin", "demo_admin"] and (
            current_user.role == "admin" or appr.user.created_by_id == current_user.id
        )
        is_manager = (
            current_user.role == "manager"
            and appr.user.profile
            and appr.user.tenant_id == current_user.tenant_id
            and appr.user.profile.reporting_manager_id == current_user.id
        )

        if not (is_admin or is_manager):
            abort(403)

        decision = request.form.get("decision")
        note = request.form.get("decision_note", "").strip()

        _, success, msg = decide_special_approval(appr.id, current_user.id, decision, note)
        flash(msg, "success" if success else "danger")

        if is_admin:
            return redirect(url_for("admin_special_approvals"))
        return redirect(url_for("manager_special_approvals"))

    @app.route("/admin/special-approvals")
    @role_required("admin")
    def admin_special_approvals():
        scoped_ids = [user.id for user in scope_to_practice_data(
            User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
        ).all()]
        requests = SpecialApproval.query.filter(
            SpecialApproval.user_id.in_(scoped_ids or [-1])
        ).order_by(SpecialApproval.applied_at.desc()).all()
        return render_template("special_approvals/admin_list.html", requests=requests)

    @app.route("/brochure")
    def brochure_presentation():
        return render_template("brochure.html")

    @app.route("/brochure/demo-request", methods=["POST"])
    def submit_demo_request():
        full_name = request.form.get("full_name")
        work_email = request.form.get("work_email")
        company_name = request.form.get("company_name")
        team_size = request.form.get("team_size")
        notes = request.form.get("notes")

        if not full_name or not work_email or not company_name:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
                return {"success": False, "message": "Full name, work email, and company name are required."}, 400
            flash("Full name, work email, and company name are required.", "danger")
            return redirect(url_for("brochure_presentation"))

        lead = DemoLead(
            full_name=full_name.strip(),
            work_email=work_email.strip().lower(),
            company_name=company_name.strip(),
            team_size=team_size.strip() if team_size else None,
            notes=notes.strip() if notes else None,
            status="New Lead"
        )
        db.session.add(lead)
        db.session.commit()

        # Send email notification directly to arpeta26@gmail.com
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        recipient = "arpeta26@gmail.com"
        subject = f"🔥 New Sanvit HRMS Demo Request from {lead.company_name} ({lead.full_name})"
        body_text = f"""New Live Demo Request Received!

Prospect Name: {lead.full_name}
Work Email:    {lead.work_email}
Company Name:  {lead.company_name}
Team Size:     {lead.team_size or 'Not specified'}
Notes:         {lead.notes or 'None provided.'}

Lead logged in database. Please reach out to {lead.work_email} to schedule the demo.
"""
        mail_user = os.getenv("MAIL_USERNAME")
        mail_pass = os.getenv("MAIL_PASSWORD")
        if mail_user and mail_pass:
            try:
                msg = MIMEMultipart()
                msg["From"] = mail_user
                msg["To"] = recipient
                msg["Subject"] = subject
                msg.attach(MIMEText(body_text, "plain"))

                server = smtplib.SMTP(os.getenv("MAIL_SERVER", "smtp.gmail.com"), int(os.getenv("MAIL_PORT", "587")))
                server.starttls()
                server.login(mail_user, mail_pass)
                server.sendmail(mail_user, recipient, msg.as_string())
                server.quit()
            except Exception as ex:
                print("SMTP send notice:", ex)

        success_msg = f"Thank you {lead.full_name}! Your live demo request has been sent to our sales team at arpeta26@gmail.com. We will contact you at {lead.work_email} shortly."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return {"success": True, "message": success_msg}

        flash(success_msg, "success")
        return redirect(url_for("brochure_presentation"))

    @app.route("/admin/demo-leads")
    @role_required("admin")
    def admin_demo_leads():
        real_admin_only()
        leads = DemoLead.query.order_by(DemoLead.id.desc()).all()
        return render_template("admin/demo_leads.html", leads=leads)

    @app.route("/admin/demo-leads/<int:lead_id>/status", methods=["POST"])
    @role_required("admin")
    def update_demo_lead_status(lead_id):
        real_admin_only()
        lead = DemoLead.query.get_or_404(lead_id)
        new_status = request.form.get("status", "Contacted")
        lead.status = new_status
        db.session.commit()
        flash(f"Demo lead status for '{lead.full_name}' updated to '{lead.status}'.", "success")
        return redirect(url_for("admin_demo_leads"))

    @app.route("/recruitment/pipeline")
    @role_required("admin", "manager")
    def recruitment_dashboard():
        requisition_query = JobRequisition.query
        if current_user.role == "demo_admin":
            if current_user.tenant_id:
                requisition_query = requisition_query.filter(JobRequisition.tenant_id == current_user.tenant_id)
            else:
                requisition_query = requisition_query.filter(JobRequisition.requested_by_id == current_user.id)
        elif current_user.role == "manager":
            requisition_query = requisition_query.filter(
                JobRequisition.tenant_id == current_user.tenant_id,
                JobRequisition.requested_by_id == current_user.id,
            )
        requisitions = requisition_query.order_by(JobRequisition.id.desc()).all()
        candidates = Candidate.query.filter(
            Candidate.requisition_id.in_([req.id for req in requisitions] or [-1])
        ).order_by(Candidate.id.desc()).all()
        interviews = CandidateInterview.query.filter(
            CandidateInterview.candidate_id.in_([candidate.id for candidate in candidates] or [-1])
        ).order_by(CandidateInterview.scheduled_time.asc()).all()

        return render_template(
            "recruitment/index.html",
            requisitions=requisitions,
            candidates=candidates,
            interviews=interviews
        )

    @app.route("/recruitment")
    @role_required("admin", "manager")
    def recruitment_home():
        return recruitment_metrics()

    @app.route("/recruitment/metrics")
    @role_required("admin", "manager")
    def recruitment_metrics():
        requisition_query = JobRequisition.query
        if current_user.role == "demo_admin":
            requisition_query = requisition_query.filter(JobRequisition.tenant_id == current_user.tenant_id)
        elif current_user.role == "manager":
            requisition_query = requisition_query.filter(
                JobRequisition.tenant_id == current_user.tenant_id,
                JobRequisition.requested_by_id == current_user.id,
            )
        requisitions = requisition_query.order_by(JobRequisition.created_at.desc()).all()
        requisition_ids = [item.id for item in requisitions]
        candidates = Candidate.query.filter(
            Candidate.requisition_id.in_(requisition_ids or [-1])
        ).all()

        def average(values):
            return round(sum(values) / len(values), 1) if values else 0.0

        time_to_fill = []
        time_to_offer = []
        offer_to_join = []
        hired = []
        offered = []
        source_counts = {"internal": 0, "external": 0}
        source_costs = {"internal": 0.0, "external": 0.0}
        for candidate in candidates:
            meta = candidate.recruitment_meta
            source = meta.hiring_source if meta and meta.hiring_source in source_counts else "external"
            cost = float(meta.recruitment_cost or 0.0) if meta else 0.0
            source_costs[source] += cost
            if candidate.offered_ctc or (meta and meta.offered_at):
                offered.append(candidate)
            if candidate.joining_date:
                hired.append(candidate)
                time_to_fill.append(max(0, (candidate.joining_date - candidate.requisition.created_at.date()).days))
                source_counts[source] += 1
            if meta and meta.offered_at:
                time_to_offer.append(max(0, (meta.offered_at.date() - candidate.created_at.date()).days))
                if candidate.joining_date:
                    offer_to_join.append(max(0, (candidate.joining_date - meta.offered_at.date()).days))

        planned_budget = sum(
            float(item.target_ctc_max or item.target_ctc_min or 0.0) * int(item.number_of_positions or 0)
            for item in requisitions
        )
        approved_budget = sum(
            float(item.target_ctc_max or item.target_ctc_min or 0.0) * int(item.number_of_positions or 0)
            for item in requisitions if item.status == "Approved"
        )
        positions_planned = sum(int(item.number_of_positions or 0) for item in requisitions)
        positions_approved = sum(int(item.number_of_positions or 0) for item in requisitions if item.status == "Approved")
        metrics = {
            "time_to_fill": average(time_to_fill),
            "time_to_offer": average(time_to_offer),
            "offer_to_join": average(offer_to_join),
            "offer_to_join_ratio": round((len(hired) / len(offered)) * 100, 1) if offered else 0.0,
            "cost_per_hire": round(sum(source_costs.values()) / len(hired), 2) if hired else 0.0,
            "planned_budget": planned_budget,
            "approved_budget": approved_budget,
            "positions_planned": positions_planned,
            "positions_approved": positions_approved,
            "internal_hires": source_counts["internal"],
            "external_hires": source_counts["external"],
            "internal_cost": source_costs["internal"],
            "external_cost": source_costs["external"],
        }
        budget_rows = [
            {
                "requisition": item,
                "planned_budget": float(item.target_ctc_max or item.target_ctc_min or 0.0) * int(item.number_of_positions or 0),
            }
            for item in requisitions
        ]
        return render_template(
            "recruitment/metrics.html",
            metrics=metrics,
            budget_rows=budget_rows,
            requisitions=requisitions,
            candidates=candidates,
        )

    @app.route("/recruitment/requisitions/new", methods=["GET", "POST"])
    @role_required("admin", "manager")
    def create_requisition():
        if request.method == "POST":
            title = request.form.get("title")
            department = request.form.get("department")
            number_of_positions = request.form.get("number_of_positions", 1)
            target_ctc_min = request.form.get("target_ctc_min", 0)
            target_ctc_max = request.form.get("target_ctc_max", 0)
            required_exp_years = request.form.get("required_exp_years", 0)
            qualification = request.form.get("qualification")
            key_skills = request.form.get("key_skills")
            priority = request.form.get("priority", "Medium")
            reason = request.form.get("reason_for_hiring")
            jd_text = request.form.get("job_description_text")

            if not title or not department or not key_skills:
                flash("Job title, department, and key skills are required.", "danger")
                return render_template("recruitment/requisition_form.html")

            req, success, msg = create_manpower_requisition(
                current_user.id, title, department, number_of_positions, key_skills,
                target_ctc_min, target_ctc_max, required_exp_years, qualification,
                priority, reason, jd_text
            )
            flash(msg, "success" if success else "danger")
            return redirect(url_for("recruitment_dashboard"))

        return render_template("recruitment/requisition_form.html")

    @app.route("/recruitment/requisitions/<int:req_id>/approve", methods=["POST"])
    @role_required("admin", "manager")
    def approve_requisition_route(req_id):
        requisition = JobRequisition.query.get_or_404(req_id)
        if current_user.role == "demo_admin" and requisition.requested_by_id != current_user.id:
            abort(403)
        action = request.form.get("action", "approve")
        reason = request.form.get("rejection_reason")
        _, success, msg = approve_manpower_requisition(req_id, current_user.id, action, reason)
        flash(msg, "success" if success else "danger")
        return redirect(url_for("recruitment_dashboard"))

    @app.route("/recruitment/requisitions/export")
    @role_required("admin")
    def export_requisitions_csv_route():
        real_admin_only()
        csv_data = generate_requisitions_csv(current_user.tenant_id)
        return send_file(
            csv_to_styled_xlsx(csv_data, "Requisitions"),
            as_attachment=True,
            download_name="Manpower_Requisitions_Report.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    career_portal_max_resume_bytes = 10 * 1024 * 1024

    def career_portal_api_error(message, status_code):
        return jsonify({"success": False, "error": message}), status_code

    def career_portal_rate_limit_response(_request_limit):
        response = jsonify({"success": False, "error": "Rate limit exceeded."})
        response.status_code = 429
        return response

    def career_portal_json_error_boundary(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            try:
                return view(*args, **kwargs)
            except Exception:
                db.session.rollback()
                return career_portal_api_error("Application could not be processed.", 500)
        return wrapped

    @app.route("/api/integrations/career-portal/applications", methods=["POST"])
    @app.extensions["csrf"].exempt
    @career_portal_json_error_boundary
    @limiter.limit("30 per minute", on_breach=career_portal_rate_limit_response)
    def career_portal_application():
        expected_token = os.getenv("CAREER_PORTAL_API_TOKEN")
        configured_tenant_id = os.getenv("CAREER_PORTAL_TENANT_ID", "")
        if not expected_token or not configured_tenant_id:
            return career_portal_api_error("Career portal integration is not configured.", 503)

        authorization = request.headers.get("Authorization", "")
        scheme, separator, supplied_token = authorization.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not separator
            or not supplied_token
            or not hmac.compare_digest(supplied_token.encode("utf-8"), expected_token.encode("utf-8"))
        ):
            return career_portal_api_error("A valid bearer token is required.", 401)

        try:
            tenant_id = int(configured_tenant_id)
        except (TypeError, ValueError):
            return career_portal_api_error("Career portal integration is not configured.", 503)
        tenant = Tenant.query.filter_by(id=tenant_id, is_active=True).first()
        if tenant is None:
            return career_portal_api_error("Career portal integration is not configured.", 503)

        if request.content_length and request.content_length > career_portal_max_resume_bytes + 65536:
            return career_portal_api_error("Resume exceeds the 10 MB file-size limit.", 413)

        requisition_id_value = request.form.get("requisition_id", "").strip()
        if not requisition_id_value.isdigit():
            return career_portal_api_error("A valid requisition_id is required.", 400)
        requisition = JobRequisition.query.get(int(requisition_id_value))
        if requisition is None:
            return career_portal_api_error("Requisition not found.", 404)
        if requisition.tenant_id != tenant.id:
            return career_portal_api_error("Requisition is not available to this integration.", 403)
        if requisition.status != "Approved":
            return career_portal_api_error("Applications are accepted only for approved requisitions.", 400)

        source = request.form.get("source", "external_job_portal").strip()
        if source != "external_job_portal":
            return career_portal_api_error("source must be external_job_portal.", 400)

        applicant_name = request.form.get("name", "").strip()
        applicant_email = request.form.get("email", "").strip()
        applicant_phone = request.form.get("phone", "").strip()
        location = request.form.get("location", "").strip()
        cover_note = request.form.get("cover_note", "").strip()
        if len(applicant_name) > 150 or len(applicant_email) > 150 or len(applicant_phone) > 30:
            return career_portal_api_error("Candidate field exceeds the supported length.", 400)
        if applicant_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", applicant_email):
            return career_portal_api_error("email must be a valid email address.", 400)
        if len(location) > 200 or len(cover_note) > 4000:
            return career_portal_api_error("location or cover_note exceeds the supported length.", 400)

        experience = None
        experience_value = request.form.get("experience", "").strip()
        if experience_value:
            try:
                experience = float(experience_value)
            except ValueError:
                return career_portal_api_error("experience must be a non-negative number.", 400)
            if not math.isfinite(experience) or experience < 0 or experience > 80:
                return career_portal_api_error("experience must be between 0 and 80 years.", 400)

        resume = request.files.get("resume")
        if not resume or not resume.filename:
            return career_portal_api_error("resume is required.", 400)
        allowed_resume_extensions = {".pdf", ".doc", ".docx"}
        original_filename = os.path.basename(resume.filename)
        extension = os.path.splitext(original_filename)[1].lower()
        if extension not in allowed_resume_extensions:
            return career_portal_api_error("resume must be a PDF, DOC, or DOCX file.", 400)
        try:
            resume.stream.seek(0, os.SEEK_END)
            resume_size = resume.stream.tell()
            resume.stream.seek(0)
        except (AttributeError, OSError):
            return career_portal_api_error("resume could not be read.", 400)
        if resume_size == 0:
            return career_portal_api_error("resume must not be empty.", 400)
        if resume_size > career_portal_max_resume_bytes:
            return career_portal_api_error("Resume exceeds the 10 MB file-size limit.", 413)

        try:
            stored_filename = save_private_upload(
                resume,
                prefix="career_portal",
                allowed_extensions=allowed_resume_extensions,
            )
        except UploadRejected:
            return career_portal_api_error("resume file content does not match its file type.", 400)

        stored_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_filename)
        candidate_id = None
        try:
            candidate, success, _message = parse_resume_text_and_match(
                stored_path,
                original_filename,
                requisition.id,
            )
            if not success or candidate is None:
                if os.path.isfile(stored_path):
                    os.remove(stored_path)
                return career_portal_api_error("Resume could not be processed.", 400)

            candidate_id = candidate.id
            candidate.resume_filename = stored_filename
            if applicant_name:
                candidate.full_name = applicant_name
            if applicant_email:
                candidate.email = applicant_email.lower()
            if applicant_phone:
                candidate.phone = applicant_phone
            if experience is not None:
                candidate.total_exp_years = experience
            applicant_details = []
            if location:
                applicant_details.append(f"Applicant location: {location}")
            if cover_note:
                applicant_details.append(f"Applicant cover note: {cover_note}")
            if applicant_details:
                candidate.hr_notes = "\n".join(filter(None, [candidate.hr_notes, *applicant_details]))
            db.session.add(CandidateRecruitmentMeta(
                candidate_id=candidate.id,
                tenant_id=tenant.id,
                hiring_source=source,
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
            if candidate_id is not None:
                try:
                    orphan = Candidate.query.get(candidate_id)
                    if orphan is not None:
                        db.session.delete(orphan)
                        db.session.commit()
                except Exception:
                    db.session.rollback()
            if os.path.isfile(stored_path):
                os.remove(stored_path)
            return career_portal_api_error("Application could not be processed.", 500)

        return jsonify({
            "success": True,
            "candidate_id": candidate.id,
            "requisition_id": requisition.id,
            "status": "created",
        }), 201

    @app.route("/recruitment/screener", methods=["GET", "POST"])
    @role_required("admin")
    def cv_screener():
        requisitions = JobRequisition.query.filter(JobRequisition.status == "Approved").all()
        if current_user.role == "demo_admin":
            requisitions = [
                req for req in requisitions
                if (current_user.tenant_id and req.tenant_id == current_user.tenant_id)
                or (not current_user.tenant_id and req.requested_by_id == current_user.id)
            ]

        if request.method == "POST":
            requisition_id = request.form.get("requisition_id")
            raw_cv_text = request.form.get("raw_cv_text")
            files = request.files.getlist("resumes") or request.files.getlist("resume")

            if not requisition_id:
                flash("Please select an approved Job Requisition to scan against.", "danger")
                return redirect(url_for("cv_screener"))

            requisition = JobRequisition.query.get_or_404(int(requisition_id))
            if current_user.role == "demo_admin" and requisition.requested_by_id != current_user.id:
                abort(403)

            scanned_count = 0
            for file in files:
                if file and file.filename and allowed_file(file.filename):
                    extension = os.path.splitext(file.filename)[1].lower()
                    if not has_valid_file_signature(file, extension):
                        continue
                    filename = save_private_upload(file, "cv")
                    file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)

                    cand, success, _ = parse_resume_text_and_match(file_path, filename, requisition_id)
                    if success:
                        scanned_count += 1

            if raw_cv_text and raw_cv_text.strip():
                cand, success, _ = parse_resume_text_and_match(None, None, requisition_id, raw_pasted_text=raw_cv_text)
                if success:
                    scanned_count += 1

            if scanned_count > 0:
                flash(f"Successfully scanned and matched {scanned_count} resume(s) against Job Requisition JD!", "success")
            else:
                flash("Please upload at least one valid resume file (PDF, TXT) or paste resume text.", "warning")

            return redirect(url_for("cv_screener"))

        candidates = Candidate.query.filter(
            Candidate.requisition_id.in_([req.id for req in requisitions] or [-1])
        ).order_by(Candidate.match_score_percent.desc()).all()
        return render_template("recruitment/screener.html", requisitions=requisitions, candidates=candidates)

    @app.route("/recruitment/candidates/<int:candidate_id>/resume")
    @login_required
    def download_candidate_resume(candidate_id):
        candidate = candidate_for_actor_or_403(candidate_id)
        if not candidate.resume_filename:
            abort(404)
        if current_user.role not in ["admin", "demo_admin", "manager"]:
            abort(403)
        return send_from_directory(app.config["UPLOAD_FOLDER"], candidate.resume_filename, as_attachment=True)

    @app.route("/recruitment/candidates/<int:candidate_id>/edit", methods=["GET", "POST"])
    @role_required("admin")
    def edit_candidate(candidate_id):
        cand = candidate_for_actor_or_403(candidate_id)

        if request.method == "POST":
            full_name = request.form.get("full_name")
            email = request.form.get("email")
            phone = request.form.get("phone")
            company = request.form.get("current_company")
            designation = request.form.get("current_designation")
            exp = request.form.get("total_exp_years", 0)
            skills = request.form.get("key_skills")
            notes = request.form.get("hr_notes")
            hiring_source = request.form.get("hiring_source", "external")
            recruitment_cost = safe_float(request.form.get("recruitment_cost"))

            if not full_name or not email or not skills:
                flash("Full name, email, and key skills are required.", "danger")
                return render_template("recruitment/edit_candidate.html", candidate=cand)

            _, success, msg = update_candidate_screening(
                cand.id, full_name, email, phone, company, designation, exp, skills, notes
            )
            meta = cand.recruitment_meta
            if not meta:
                meta = CandidateRecruitmentMeta(
                    candidate_id=cand.id,
                    tenant_id=cand.tenant_id,
                )
                db.session.add(meta)
            meta.hiring_source = hiring_source if hiring_source in {"internal", "external"} else "external"
            meta.recruitment_cost = max(0.0, recruitment_cost)
            db.session.commit()
            flash(msg, "success" if success else "danger")
            return redirect(url_for("cv_screener"))

        return render_template("recruitment/edit_candidate.html", candidate=cand)

    @app.route("/recruitment/candidates/<int:candidate_id>/delete", methods=["POST"])
    @role_required("admin")
    def delete_candidate_route(candidate_id):
        candidate_for_actor_or_403(candidate_id)
        success, msg = delete_candidate(candidate_id)
        flash(msg, "success" if success else "danger")
        return redirect(url_for("cv_screener"))

    @app.route("/recruitment/interviews", methods=["GET", "POST"])
    @role_required("admin", "manager")
    def interviews_calendar():
        managers = User.query.filter(User.role.in_(["manager", "admin"])).all()
        candidate_query = Candidate.query.filter(
            Candidate.status.in_(["Screened", "Shortlisted", "Interview Scheduled"])
        )
        if current_user.role == "manager":
            managers = User.query.filter_by(id=current_user.id, tenant_id=current_user.tenant_id).all()
            candidate_query = candidate_query.filter(Candidate.tenant_id == current_user.tenant_id)
        elif current_user.role == "demo_admin":
            managers = User.query.filter(User.role == "manager", User.tenant_id == current_user.tenant_id).all()
            candidate_query = candidate_query.filter(Candidate.tenant_id == current_user.tenant_id)
        candidates = candidate_query.all()

        if request.method == "POST":
            candidate_id = request.form.get("candidate_id")
            interviewer_id = request.form.get("interviewer_id")
            round_name = request.form.get("round_name", "Technical Round 1")
            sched_str = request.form.get("scheduled_time")
            location_or_link = request.form.get("location_or_link")

            if not candidate_id or not interviewer_id or not sched_str:
                flash("Candidate, interviewer, and date/time are required.", "danger")
                return redirect(url_for("interviews_calendar"))

            try:
                scheduled_time = datetime.strptime(sched_str, "%Y-%m-%dT%H:%M")
            except ValueError:
                flash("Invalid interview date and time format.", "danger")
                return redirect(url_for("interviews_calendar"))

            _, success, msg = schedule_candidate_interview(
                candidate_id, interviewer_id, round_name, scheduled_time, location_or_link
            )
            flash(msg, "success" if success else "danger")
            return redirect(url_for("interviews_calendar"))

        if current_user.role == "manager":
            interviews = CandidateInterview.query.filter_by(
                interviewer_id=current_user.id,
                tenant_id=current_user.tenant_id,
            ).order_by(CandidateInterview.scheduled_time.asc()).all()
        elif current_user.role == "demo_admin":
            interviews = CandidateInterview.query.filter(
                CandidateInterview.tenant_id == current_user.tenant_id
            ).order_by(CandidateInterview.scheduled_time.asc()).all()
        else:
            interviews = CandidateInterview.query.order_by(CandidateInterview.scheduled_time.asc()).all()

        return render_template("recruitment/interviews.html", interviews=interviews, managers=managers, candidates=candidates)

    @app.route("/recruitment/interviews/<int:interview_id>/feedback", methods=["POST"])
    @role_required("admin", "manager")
    def submit_interview_feedback_route(interview_id):
        interview = CandidateInterview.query.get_or_404(interview_id)
        candidate_for_actor_or_403(interview.candidate_id)
        if current_user.role == "manager" and interview.interviewer_id != current_user.id:
            abort(403)
        rating = request.form.get("manager_rating")
        feedback = request.form.get("manager_feedback")
        decision = request.form.get("decision", "Shortlist Next Round")
        next_email = request.form.get("next_interviewer_email")

        _, success, msg = submit_interview_feedback(interview_id, rating, feedback, decision, next_email)
        flash(msg, "success" if success else "danger")
        return redirect(url_for("interviews_calendar"))

    @app.route("/recruitment/candidates/<int:candidate_id>/offer", methods=["GET", "POST"])
    @role_required("admin", "manager")
    def candidate_offer_route(candidate_id):
        cand = candidate_for_actor_or_403(candidate_id)

        if cand.status == "Rejected":
            flash(f"Cannot issue offer letter — candidate {cand.full_name} was marked as Rejected by the Manager.", "danger")
            return redirect(url_for("recruitment_dashboard"))

        if request.method == "POST":
            offered_ctc = request.form.get("offered_ctc")
            joining_date = request.form.get("joining_date")
            hr_notes = request.form.get("hr_notes")

            if not offered_ctc or not joining_date:
                flash("Offered CTC and Joining Date are required.", "danger")
                return render_template("recruitment/offer.html", candidate=cand)

            _, success, msg = issue_candidate_offer(cand.id, offered_ctc, joining_date, hr_notes)
            flash(msg, "success" if success else "danger")
            if success:
                return redirect(url_for("view_offer_letter", candidate_id=cand.id))
            return redirect(url_for("recruitment_dashboard"))

        return render_template("recruitment/offer.html", candidate=cand)

    @app.route("/recruitment/candidates/<int:candidate_id>/offer-letter")
    @role_required("admin", "manager")
    def view_offer_letter(candidate_id):
        cand = candidate_for_actor_or_403(candidate_id)
        if cand.status != "Offered" and not cand.offered_ctc:
            flash("Offer letter has not been issued for this candidate yet.", "warning")
            return redirect(url_for("recruitment_dashboard"))

        return render_template("recruitment/offer_letter_view.html", candidate=cand)