import os
import re
from datetime import datetime, date, timedelta
from types import SimpleNamespace
from flask import render_template, redirect, url_for, flash, request, send_from_directory, send_file, abort, Response
from werkzeug.utils import secure_filename
from payroll_logic import (
    generate_payslip,
    validate_components_for_generation,
    get_active_ctc,
    generate_payslip_pdf,
    num_to_words,
    get_employee_monthly_payroll_inputs,
)

from models import (
    User,
    EmployeeProfile,
    Child,
    LeaveType,
    LeaveBalance,
    LeaveApplication,
    PayComponent,
    EmployeeCTC,
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
    CandidateInterview,
    Recognition,
    ImportantDate,
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
from reports_logic import (generate_employee_report_csv, generate_attendance_report_csv, generate_payroll_report_csv, generate_appraisal_report_csv)
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






def register_routes(app, db, login_user, logout_user, login_required, current_user,
                     allowed_file, parse_date, next_employee_code,
                     validate_pan, validate_aadhaar, validate_ifsc, role_required):

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
            return query.filter(User.created_by_id == current_user.id)
        return query

    def safe_float(value):
        if value is None or str(value).strip() == "":
            return 0.0
        try:
            return float(str(value).strip().replace(",", ""))
        except (TypeError, ValueError):
            return 0.0

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
                temp_password = str(row_value(row, "temp_password", "password", "default_password") or "Welcome@123").strip() or "Welcome@123"
                monthly_ctc = safe_float(row_value(row, "monthly_ctc", "ctc", "salary"))
                is_founding_member = parse_bulk_bool(row_value(row, "is_founding_member", "founding_member", "founding"))
                reporting_manager_value = row_value(row, "reporting_manager", "manager", "reporting_manager_name", "reporting_manager_employee_code", "manager_code")
                reporting_manager_id = resolve_manager_reference(reporting_manager_value)

                user = User(employee_code=code, role=role, must_change_password=True)
                user.set_password(temp_password)
                db.session.add(user)
                db.session.flush()

                profile = EmployeeProfile(
                    user_id=user.id,
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
                        effective_from=date_of_joining or date.today(),
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
            "Asha Verma,Senior Software Engineer,Engineering,2026-01-15,employee,Rajesh Kumar,65000,No,Welcome@123\n"
            "Nikhil Shah,Team Lead,Engineering,2026-02-01,manager,,75000,Yes,Welcome@123\n"
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
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            employee_code = request.form.get("employee_code", "").strip().upper()
            password = request.form.get("password", "")

            user = User.query.filter_by(employee_code=employee_code).first()
            if user and not user.is_active:
                flash("This account has been deactivated. Contact your administrator.", "danger")
            elif user and user.check_password(password):
                login_user(user)
                if user.must_change_password:
                    return redirect(url_for("change_password"))
                return redirect(url_for("dashboard"))
            else:
                flash("Invalid employee ID or password.", "danger")
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("You've been logged out.", "info")
        return redirect(url_for("login"))

    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    def change_password():
        if request.method == "POST":
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if len(new_password) < 6:
                flash("Password must be at least 6 characters.", "danger")
            elif new_password != confirm_password:
                flash("Passwords do not match.", "danger")
            else:
                current_user.set_password(new_password)
                current_user.must_change_password = False
                db.session.commit()
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
        today = date.today()
        people = User.query.join(
            EmployeeProfile, EmployeeProfile.user_id == User.id
        ).filter(
            User.role.in_(["admin", "demo_admin", "manager", "employee"]),
            User.is_active == True,
        ).all()

        birthdays = [
            person for person in people
            if person.profile and person.profile.date_of_birth
        ]
        birthdays.sort(key=lambda person: (
            (person.profile.date_of_birth.month - today.month) % 12,
            person.profile.date_of_birth.day,
        ))

        important_dates = ImportantDate.query.order_by(ImportantDate.date.asc()).all()
        upcoming_dates = sorted(
            important_dates,
            key=lambda item: ((item.date - today).days % 365, item.date),
        )[:5]
        recognitions = Recognition.query.order_by(Recognition.created_at.desc()).limit(6).all()
        return {
            "upcoming_birthdays": birthdays[:6],
            "upcoming_dates": upcoming_dates,
            "recent_recognitions": recognitions,
        }

    # -----------------------------------------------------------------
    # ADMIN routes
    # -----------------------------------------------------------------
    @app.route("/admin")
    @role_required("admin")
    def admin_dashboard():
        employee_query = scope_to_practice_data(User.query.filter_by(role="employee"))
        manager_query = scope_to_practice_data(User.query.filter_by(role="manager"))
        completed = scope_to_practice_data(
            EmployeeProfile.query.join(User, User.id == EmployeeProfile.user_id).filter(
                User.role.in_(["employee", "manager", "demo_admin"]),
                EmployeeProfile.profile_completed == True,
            )
        ).count()
        pending = scope_to_practice_data(
            User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
        ).count() - completed
        recent = scope_to_practice_data(
            User.query.filter(User.role.in_(["employee", "manager", "demo_admin"]))
        ).order_by(User.id.desc()).limit(5).all()
        total_employees = employee_query.count()
        total_managers = manager_query.count()
        pending_leaves = LeaveApplication.query.filter_by(status="pending").count()
        return render_template("admin/dashboard.html", **overview_context(),
                    total_employees=total_employees,
                                total_managers=total_managers, completed=completed,
                                pending=max(pending, 0), recent=recent,
                                pending_leaves=pending_leaves)

    @app.route("/admin/employees")
    @role_required("admin")
    def admin_employee_list():
        q = request.args.get("q", "").strip()
        query = scope_to_practice_data(User.query.filter(User.role.in_(["employee", "manager", "demo_admin"])))
        if q:
            query = query.join(EmployeeProfile, EmployeeProfile.user_id == User.id).filter(
                db.or_(EmployeeProfile.full_name.ilike(f"%{q}%"),
                       User.employee_code.ilike(f"%{q}%"))
            )
        people = query.order_by(User.id.desc()).all()
        return render_template("admin/employee_list.html", people=people, q=q)

    @app.route("/admin/employees/new", methods=["GET", "POST"])
    @role_required("admin")
    def admin_create_employee():
        managers = User.query.filter(User.role == "manager", User.is_active == True) \
            .join(EmployeeProfile, EmployeeProfile.user_id == User.id).all()

        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            designation = request.form.get("designation", "").strip()
            department = request.form.get("department", "").strip()
            date_of_joining = parse_date(request.form.get("date_of_joining"))
            reporting_manager_id = request.form.get("reporting_manager_id") or None
            role = request.form.get("role", "employee")
            temp_password = request.form.get("temp_password", "").strip() or "Welcome@123"
            is_founding_member = request.form.get("is_founding_member") == "on"
            monthly_ctc = float(request.form.get("monthly_ctc") or 0.0)

            if role == "demo_admin" and not full_name:
                full_name = "Demo Admin"

            if not full_name:
                flash("Employee name is required.", "danger")
                return render_template("admin/create_employee.html", managers=managers,
                                        form=request.form)

            code = next_employee_code()
            user = User(employee_code=code, role=role, must_change_password=True, created_by_id=current_user.id)
            user.set_password(temp_password)
            db.session.add(user)
            db.session.flush()

            profile = EmployeeProfile(
                user_id=user.id,
                full_name=full_name,
                designation=designation,
                department=department,
                date_of_joining=date_of_joining,
                reporting_manager_id=int(reporting_manager_id) if reporting_manager_id else None,
                is_founding_member=is_founding_member,
                monthly_ctc=monthly_ctc,
            )
            db.session.add(profile)

            if monthly_ctc > 0:
                ctc = EmployeeCTC(
                    user_id=user.id,
                    monthly_ctc=monthly_ctc,
                    effective_from=date_of_joining or date.today()
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
        return render_template("profile_view.html", person=user, profile=user.profile,
                                viewer="admin")

    @app.route("/admin/employees/<int:user_id>/reset-password", methods=["POST"])
    @role_required("admin")
    def admin_reset_password(user_id):
        user = User.query.get_or_404(user_id)
        new_temp = "Welcome@123"
        user.set_password(new_temp)
        user.must_change_password = True
        db.session.commit()
        flash(f"Password for {user.employee_code} reset to: {new_temp}", "success")
        return redirect(url_for("admin_employee_list"))

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
            new_role = request.form.get("role", user.role)
            is_founding_member = request.form.get("is_founding_member") == "on"
            monthly_ctc = float(request.form.get("monthly_ctc") or 0.0)

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

            if monthly_ctc > 0:
                ctc = EmployeeCTC.query.filter_by(user_id=user.id).first()
                if ctc:
                    ctc.monthly_ctc = monthly_ctc
                    ctc.effective_from = date_of_joining or date.today()
                else:
                    ctc = EmployeeCTC(
                        user_id=user.id,
                        monthly_ctc=monthly_ctc,
                        effective_from=date_of_joining or date.today()
                    )
                    db.session.add(ctc)

            db.session.commit()

            flash(f"{full_name}'s details have been updated.", "success")
            return redirect(url_for("admin_view_employee", user_id=user.id))

        return render_template("admin/edit_employee.html", user=user, managers=managers, form=None)

        return render_template("admin/edit_employee.html", user=user, managers=managers, form=None)

    @app.route("/admin/employees/<int:user_id>/deactivate", methods=["POST"])
    @role_required("admin")
    def admin_deactivate_employee(user_id):
        user = User.query.get_or_404(user_id)
        if current_user.role == "demo_admin" and user.created_by_id != current_user.id:
            abort(403)
        if not user.profile:
            abort(404)

        if user.role == "manager":
            team_size = EmployeeProfile.query.filter_by(reporting_manager_id=user.id).count()
            if team_size > 0:
                noun = "person" if team_size == 1 else "people"
                verb = "reports" if team_size == 1 else "report"
                flash(f"Can't deactivate {user.profile.full_name} — {team_size} "
                      f"{noun} still {verb} to them. "
                      f"Reassign their team first.", "danger")
                return redirect(url_for("admin_employee_list"))

        user.is_active = False
        db.session.commit()
        flash(f"{user.profile.full_name} has been deactivated and can no longer log in.", "success")
        return redirect(url_for("admin_employee_list"))

    @app.route("/admin/employees/<int:user_id>/reactivate", methods=["POST"])
    @role_required("admin")
    def admin_reactivate_employee(user_id):
        user = User.query.get_or_404(user_id)
        if current_user.role == "demo_admin" and user.created_by_id != current_user.id:
            abort(403)
        if not user.profile:
            abort(404)
        user.is_active = True
        db.session.commit()
        flash(f"{user.profile.full_name} has been reactivated.", "success")
        return redirect(url_for("admin_employee_list"))

    @app.route("/admin/employees/<int:user_id>/delete", methods=["POST"])
    @role_required("admin")
    def admin_delete_employee(user_id):
        user = User.query.get_or_404(user_id)
        if current_user.role == "demo_admin" and user.created_by_id != current_user.id:
            abort(403)
        if user.id == current_user.id:
            flash("You cannot delete your own admin account.", "danger")
            return redirect(url_for("admin_employee_list"))

        name = user.profile.full_name if user.profile else user.employee_code
        
        try:
            from models import EmployeeCTC, Attendance, LeaveApplication, LeaveBalance, Payslip, Appraisal
            
            # Clear reporting manager references for direct reports
            EmployeeProfile.query.filter_by(reporting_manager_id=user.id).update({"reporting_manager_id": None})

            EmployeeCTC.query.filter_by(user_id=user.id).delete()
            Attendance.query.filter_by(user_id=user.id).delete()
            LeaveApplication.query.filter_by(user_id=user.id).delete()
            LeaveBalance.query.filter_by(user_id=user.id).delete()
            Payslip.query.filter_by(user_id=user.id).delete()
            Appraisal.query.filter_by(user_id=user.id).delete()
            
            if user.profile:
                db.session.delete(user.profile)

            db.session.delete(user)
            db.session.commit()
            flash(f"Employee '{name}' ({user.employee_code}) has been permanently deleted from the system.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Could not delete employee: {str(e)}", "danger")

        return redirect(url_for("admin_employee_list"))

    @app.route("/admin/managers")
    @role_required("admin")
    def admin_manager_list():
        managers = scope_to_practice_data(User.query.filter_by(role="manager")).all()
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
        profiles = EmployeeProfile.query.filter_by(reporting_manager_id=current_user.id).all()
        team = profiles
        completed = sum(1 for p in profiles if p.profile_completed)
        team_ids = [p.user_id for p in profiles]
        pending_leaves = LeaveApplication.query.filter(
            LeaveApplication.user_id.in_(team_ids or [-1]),
            LeaveApplication.status == "pending"
        ).count()
        return render_template("manager/dashboard.html", **overview_context(), team=team,
                                completed=completed, pending=len(profiles) - completed,
                                pending_leaves=pending_leaves)

    @app.route("/manager/team")
    @role_required("manager")
    def manager_team_list():
        profiles = EmployeeProfile.query.filter_by(reporting_manager_id=current_user.id).all()
        return render_template("manager/team_list.html", profiles=profiles)

    @app.route("/manager/team/<int:user_id>")
    @role_required("manager")
    def manager_view_employee(user_id):
        user = User.query.get_or_404(user_id)
        if not user.profile or user.profile.reporting_manager_id != current_user.id:
            abort(403)
        return render_template("profile_view.html", person=user, profile=user.profile,
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
        return render_template("community.html", people=people, **context)

    @app.route("/admin/important-dates", methods=["POST"])
    @role_required("admin")
    def admin_add_important_date():
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

    @app.route("/admin/important-dates/<int:date_id>/delete", methods=["POST"])
    @role_required("admin")
    def admin_delete_important_date(date_id):
        important_date = ImportantDate.query.get_or_404(date_id)
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
            profile.pan_number = pan
            profile.aadhaar_number = aadhaar

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
            profile.bank_account_number = request.form.get("bank_account_number", "").strip()
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
                    filename = secure_filename(
                        f"{current_user.employee_code}_relieving_{file.filename}")
                    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                    file.save(filepath)
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
            profile.updated_at = datetime.utcnow()
            db.session.commit()

            flash("Your profile has been saved.", "success")
            return redirect(url_for("employee_view_own_profile"))

        return render_template("employee/edit_profile.html", profile=profile, form=None)

    # -----------------------------------------------------------------
    # LEAVE MANAGEMENT — Admin: manage leave types
    # -----------------------------------------------------------------
    @app.route("/admin/leave-types")
    @role_required("admin")
    def admin_leave_types():
        leave_types = LeaveType.query.order_by(LeaveType.id).all()
        return render_template("admin/leave_types.html", leave_types=leave_types)

    @app.route("/admin/leave-types/new", methods=["POST"])
    @role_required("admin")
    def admin_create_leave_type():
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

        recalculate_all_balances_for_type(lt, date.today().year)
        flash(f'"{name}" added with an annual quota of {annual_quota} days.', "success")
        return redirect(url_for("admin_leave_types"))

    @app.route("/admin/leave-types/<int:type_id>/edit", methods=["POST"])
    @role_required("admin")
    def admin_edit_leave_type(type_id):
        lt = LeaveType.query.get_or_404(type_id)
        annual_quota = request.form.get("annual_quota", "").strip()

        if not annual_quota.isdigit():
            flash("Annual quota must be a whole number.", "danger")
            return redirect(url_for("admin_leave_types"))

        lt.annual_quota = int(annual_quota)
        db.session.commit()

        recalculate_all_balances_for_type(lt, date.today().year)
        flash(f'"{lt.name}" updated to {annual_quota} days/year. Balances recalculated for everyone.', "success")
        return redirect(url_for("admin_leave_types"))

    @app.route("/admin/leave-types/<int:type_id>/toggle", methods=["POST"])
    @role_required("admin")
    def admin_toggle_leave_type(type_id):
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
        query = LeaveApplication.query
        if status_filter != "all":
            query = query.filter_by(status=status_filter)
        applications = query.order_by(LeaveApplication.applied_at.desc()).all()
        return render_template("admin/leave_requests.html", applications=applications,
                                status_filter=status_filter)

    @app.route("/admin/leaves/<int:app_id>/decide", methods=["POST"])
    @role_required("admin")
    def admin_decide_leave(app_id):
        application = LeaveApplication.query.get_or_404(app_id)
        decision = request.form.get("decision")
        note = request.form.get("decision_note", "").strip()

        if application.status != "pending":
            flash("This request has already been decided.", "warning")
            return redirect(url_for("admin_leave_requests"))

        if decision not in ("approved", "rejected"):
            abort(400)

        application.status = decision
        application.decided_at = datetime.utcnow()
        application.decided_by_id = current_user.id
        application.decision_note = note
        db.session.commit()

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
            reporting_manager_id=current_user.id).all()]
        query = LeaveApplication.query.filter(LeaveApplication.user_id.in_(team_ids or [-1]))
        if status_filter != "all":
            query = query.filter_by(status=status_filter)
        applications = query.order_by(LeaveApplication.applied_at.desc()).all()
        return render_template("manager/leave_requests.html", applications=applications,
                                status_filter=status_filter)

    @app.route("/manager/leaves/<int:app_id>/decide", methods=["POST"])
    @role_required("manager")
    def manager_decide_leave(app_id):
        application = LeaveApplication.query.get_or_404(app_id)

        if not application.user.profile or application.user.profile.reporting_manager_id != current_user.id:
            abort(403)
        if application.status != "pending":
            flash("This request has already been decided.", "warning")
            return redirect(url_for("manager_leave_requests"))

        decision = request.form.get("decision")
        note = request.form.get("decision_note", "").strip()
        if decision not in ("approved", "rejected"):
            abort(400)

        application.status = decision
        application.decided_at = datetime.utcnow()
        application.decided_by_id = current_user.id
        application.decision_note = note
        db.session.commit()

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

        today = date.today()
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

        today = date.today()
        if current_user.profile.date_of_joining > today:
            flash("You can't apply for leave before your date of joining.", "danger")
            return redirect(url_for("my_leaves"))

        leave_types = LeaveType.query.filter_by(is_active=True).all()

        if request.method == "POST":
            leave_type_id = request.form.get("leave_type_id")
            start_date = parse_date(request.form.get("start_date"))
            end_date = parse_date(request.form.get("end_date"))
            reason = request.form.get("reason", "").strip()

            leave_type = LeaveType.query.get(leave_type_id) if leave_type_id else None

            if not leave_type or not start_date or not end_date:
                flash("Please fill in the leave type and both dates.", "danger")
                return render_template("employee/apply_leave.html", leave_types=leave_types, form=request.form)

            if end_date < start_date:
                flash("End date can't be before the start date.", "danger")
                return render_template("employee/apply_leave.html", leave_types=leave_types, form=request.form)

            if start_date < current_user.profile.date_of_joining:
                flash("You can't apply for leave before your date of joining.", "danger")
                return render_template("employee/apply_leave.html", leave_types=leave_types, form=request.form)

            days = business_days_count(start_date, end_date)
            balance = ensure_balances_for_user(current_user, start_date.year)
            matching = next((b for b in balance if b.leave_type_id == leave_type.id), None)

            if matching and days > matching.remaining:
                flash(f"You only have {matching.remaining:g} days of {leave_type.name} left "
                      f"for {start_date.year} — this request needs {days:g}.", "danger")
                return render_template("employee/apply_leave.html", leave_types=leave_types, form=request.form)

            application = LeaveApplication(
                user_id=current_user.id, leave_type_id=leave_type.id,
                start_date=start_date, end_date=end_date, days=days,
                reason=reason, status="pending"
            )
            db.session.add(application)
            db.session.commit()

            flash("Leave request submitted for approval.", "success")
            return redirect(url_for("my_leaves"))

        return render_template("employee/apply_leave.html", leave_types=leave_types, form=None)

    @app.route("/leaves/<int:app_id>/cancel", methods=["POST"])
    @role_required("employee", "manager")
    def cancel_leave(app_id):
        application = LeaveApplication.query.get_or_404(app_id)
        if application.user_id != current_user.id:
            abort(403)
        if application.status != "pending":
            flash("Only pending requests can be cancelled.", "warning")
            return redirect(url_for("my_leaves"))

        application.status = "cancelled"
        application.decided_at = datetime.utcnow()
        db.session.commit()
        flash("Leave request cancelled.", "success")
        return redirect(url_for("my_leaves"))
     # -----------------------------------------------------------------
    # PAYROLL DASHBOARD
    # -----------------------------------------------------------------

    @app.route("/admin/payroll")
    @role_required("admin")
    def payroll_dashboard():

        total_components = PayComponent.query.count()
        total_ctc = EmployeeCTC.query.count()
        total_payslips = Payslip.query.count()

        return render_template(
            "payroll/dashboard.html",
            total_components=total_components,
            total_ctc=total_ctc,
            total_payslips=total_payslips,
        )
    # -----------------------------------------------------------------
    # PAY COMPONENTS
    # -----------------------------------------------------------------

    @app.route("/admin/payroll/components")
    @role_required("admin")
    def pay_components():

        components = PayComponent.query.order_by(
            PayComponent.calc_order,
            PayComponent.id
        ).all()

        return render_template(
            "payroll/pay_components.html",
            components=components
        )
        # -----------------------------------------------------------------
    # EMPLOYEE CTC
    # -----------------------------------------------------------------

    @app.route("/admin/payroll/ctc")
    @role_required("admin")
    def employee_ctc():

        employees = User.query.filter(
            User.role.in_(["employee", "manager", "demo_admin"])
        ).order_by(User.employee_code).all()

        ctc_records = {
            c.user_id: c
            for c in EmployeeCTC.query.all()
        }

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
         # -----------------------------------------------------------------
    # GENERATE PAYROLL
    # -----------------------------------------------------------------

    @app.route("/admin/payroll/generate", methods=["GET", "POST"])
    @role_required("admin")
    def generate_payroll():
        today = date.today()
        month = int(request.args.get("month") or request.form.get("month") or today.month)
        year = int(request.args.get("year") or request.form.get("year") or today.year)

        problems = validate_components_for_generation()
        if problems:
            for problem in problems:
                flash(problem, "danger")

        employees = User.query.filter(User.role.in_(["employee", "manager", "demo_admin"])).order_by(User.employee_code).all()

        if request.method == "POST" and "process_payroll" in request.form:
            created_count = 0
            updated_count = 0
            no_ctc_count = 0

            for emp in employees:
                arrears = float(request.form.get(f"arrears_{emp.id}") or 0.0)
                loss_of_pay = float(request.form.get(f"loss_of_pay_{emp.id}") or 0.0)
                ot_hours_val = request.form.get(f"ot_hours_{emp.id}")
                ot_hours = float(ot_hours_val) if ot_hours_val is not None and ot_hours_val != "" else None
                incentive = float(request.form.get(f"incentive_{emp.id}") or 0.0)
                pf_ded = float(request.form.get(f"pf_deduction_{emp.id}") or 0.0)
                gratuity = float(request.form.get(f"gratuity_provision_{emp.id}") or 0.0)

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
                    pf_deduction=pf_ded,
                    gratuity_provision=gratuity
                )

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


        return render_template("payroll/generate_payroll.html")

    # -----------------------------------------------------------------
    # ALL PAYSLIPS
    # -----------------------------------------------------------------

    @app.route("/admin/payroll/payslips")
    @role_required("admin")
    def admin_payslip_list():

        payslips = (
            Payslip.query
            .order_by(
                Payslip.year.desc(),
                Payslip.month.desc(),
                Payslip.id.desc()
            )
            .all()
        )

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

        if request.method == "POST":

            name = request.form.get("name", "").strip()

            component_type = request.form.get("component_type")

            calc_basis = request.form.get("calc_basis")

            calc_value = float(request.form.get("calc_value") or 0)

            calc_order = int(request.form.get("calc_order") or 1)

            is_basic = request.form.get("is_basic") == "on"

            is_active = request.form.get("is_active") == "on"

            component = PayComponent(
                name=name,
                component_type=component_type,
                calc_basis=calc_basis,
                calc_value=calc_value,
                calc_order=calc_order,
                is_basic=is_basic,
                is_active=is_active,
            )

            db.session.add(component)
            db.session.commit()

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

        component = PayComponent.query.get_or_404(component_id)

        if request.method == "POST":

            component.name = request.form.get("name")
            component.component_type = request.form.get("component_type")
            component.calc_basis = request.form.get("calc_basis")
            component.calc_value = float(request.form.get("calc_value") or 0)
            component.calc_order = int(request.form.get("calc_order") or 1)
            component.is_basic = request.form.get("is_basic") == "on"
            component.is_active = request.form.get("is_active") == "on"

            db.session.commit()

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
        is_admin = current_user.role in ["admin", "demo_admin"]
        is_their_manager = (
            current_user.role == "manager"
            and target.profile.reporting_manager_id == current_user.id
        )

        return send_from_directory(
            app.config["UPLOAD_FOLDER"],
            filename,
            as_attachment=True,
        )

    # -----------------------------------------------------------------
    # PAYSLIP VIEWING & DOWNLOAD ROUTES
    # -----------------------------------------------------------------
    @app.route("/payroll/payslips/<int:payslip_id>")
    @login_required
    def view_payslip(payslip_id):
        payslip = Payslip.query.get_or_404(payslip_id)
        is_owner = current_user.id == payslip.user_id
        is_admin = current_user.role == "admin"
        is_their_manager = (
            current_user.role == "manager"
            and payslip.user.profile
            and payslip.user.profile.reporting_manager_id == current_user.id
        )
        if not (is_owner or is_admin or is_their_manager):
            abort(403)

        net_words = num_to_words(payslip.net_pay)
        return render_template("payroll/payslip_view.html", payslip=payslip, net_words=net_words)

    @app.route("/payroll/payslips/<int:payslip_id>/pdf")
    @login_required
    def download_payslip_pdf(payslip_id):
        payslip = Payslip.query.get_or_404(payslip_id)
        is_owner = current_user.id == payslip.user_id
        is_admin = current_user.role in ["admin", "demo_admin"]
        is_their_manager = (
            current_user.role == "manager"
            and payslip.user.profile
            and payslip.user.profile.reporting_manager_id == current_user.id
        )
        if not (is_owner or is_admin or is_their_manager):
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
        today = date.today()
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
        team_profiles = EmployeeProfile.query.filter_by(reporting_manager_id=current_user.id).all()
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
        requests = AttendanceRegularization.query.order_by(AttendanceRegularization.applied_at.desc()).all()
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
        team_profiles = EmployeeProfile.query.filter_by(reporting_manager_id=current_user.id).all()
        team_user_ids = [p.user_id for p in team_profiles]

        today = date.today()
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
        users = User.query.filter(User.role.in_(["employee", "manager", "demo_admin"])).order_by(User.employee_code).all()
        selected_date_str = request.args.get("date", date.today().strftime("%Y-%m-%d"))
        selected_date = parse_date(selected_date_str) or date.today()

        records = Attendance.query.filter_by(date=selected_date).all()
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
        att_date = parse_date(request.form.get("date")) or date.today()
        status = request.form.get("status", "Present")
        notes = request.form.get("notes", "").strip()

        rec = Attendance.query.filter_by(user_id=user_id, date=att_date).first()
        if rec:
            rec.status = status
            rec.notes = notes
        else:
            rec = Attendance(user_id=user_id, date=att_date, status=status, notes=notes)
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
            letters = EmployeeLetter.query.order_by(EmployeeLetter.requested_at.desc()).all()
        templates = HRLetterTemplate.query.order_by(HRLetterTemplate.title.asc()).all()
        resources = ResourceDocument.query.filter_by(is_published=True).order_by(ResourceDocument.category, ResourceDocument.created_at.desc()).all()
        policy_matches = []
        policy_answer = None
        resources_to_display = resources

        if tab == "resources" and search_query:
            policy_matches = search_policy_documents(search_query, resources)
            resources_to_display = [match["resource"] for match in policy_matches]
            policy_answer = build_policy_answer(search_query, resources)

        meetings = AppraisalMeeting.query.order_by(AppraisalMeeting.scheduled_for.asc()).all()
        memories = HRMemory.query.order_by(HRMemory.event_date.desc(), HRMemory.created_at.desc()).all()
        employees = User.query.filter(User.role.in_(["employee", "manager", "demo_admin"])).join(
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
        template = HRLetterTemplate.query.get_or_404(request.form.get("template_id", type=int))
        employee = User.query.get_or_404(request.form.get("employee_id", type=int))
        details = request.form.get("details", "").strip()
        letter_id = request.form.get("letter_id", type=int)
        letter = EmployeeLetter.query.get(letter_id) if letter_id else None
        if not letter:
            letter = EmployeeLetter(
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
        letter.issued_at = datetime.utcnow()
        db.session.commit()
        flash("Letter issued and made visible to the employee.", "success")
        return redirect(url_for("hr_resources", tab="letters"))

    @app.route("/admin/hr-resources/letter-templates/<int:template_id>", methods=["POST"])
    @role_required("admin")
    def update_hr_letter_template(template_id):
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
        if letter.employee_id != current_user.id and current_user.role != "admin":
            abort(403)
        if letter.status != "Issued" and current_user.role != "admin":
            abort(403)
        return render_template("hr_letter_view.html", letter=letter)

    @app.route("/hr-resources/letters/<int:letter_id>/download")
    @login_required
    def download_hr_letter(letter_id):
        letter = EmployeeLetter.query.get_or_404(letter_id)
        if letter.employee_id != current_user.id and current_user.role != "admin":
            abort(403)
        if letter.status != "Issued" and current_user.role != "admin":
            abort(403)
        return Response(
            letter.content,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={letter.subject.replace(' ', '_')}.txt"},
        )

    @app.route("/admin/hr-resources/resources/upload", methods=["POST"])
    @role_required("admin")
    def upload_hr_resource():
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Choose a handbook, policy, or form file first.", "danger")
            return redirect(url_for("hr_resources", tab="resources"))
        extension = os.path.splitext(file.filename)[1].lower()
        if extension not in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg"}:
            flash("Only PDF, Word, Excel, and image files are supported.", "danger")
            return redirect(url_for("hr_resources", tab="resources"))
        filename = secure_filename(f"resource_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}{extension}")
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        db.session.add(ResourceDocument(
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
        if not resource.is_published and current_user.role != "admin":
            abort(404)
        return send_from_directory(app.config["UPLOAD_FOLDER"], resource.filename, as_attachment=True)

    @app.route("/admin/appraisals/meetings", methods=["POST"])
    @role_required("admin")
    def add_appraisal_meeting():
        scheduled_for = request.form.get("scheduled_for", "")
        try:
            meeting_time = datetime.strptime(scheduled_for, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("Enter a valid meeting date and time.", "danger")
            return redirect(url_for("hr_resources", tab="appraisals"))
        db.session.add(AppraisalMeeting(
            title=request.form.get("title", "Appraisal meeting").strip(),
            scheduled_for=meeting_time,
            location=request.form.get("location", "").strip(),
            notes=request.form.get("notes", "").strip(),
            employee_id=request.form.get("employee_id", type=int) or None,
            created_by_id=current_user.id,
        ))
        db.session.commit()
        flash("Appraisal meeting added to the shared calendar.", "success")
        return redirect(url_for("hr_resources", tab="appraisals"))

    @app.route("/admin/hr-resources/memories/upload", methods=["POST"])
    @role_required("admin")
    def upload_hr_memory():
        file = request.files.get("image")
        filename = None
        if file and file.filename:
            extension = os.path.splitext(file.filename)[1].lower()
            if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
                flash("Memories support PNG, JPG, and WEBP images.", "danger")
                return redirect(url_for("hr_resources", tab="memories"))
            filename = secure_filename(f"memory_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}{extension}")
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        db.session.add(HRMemory(
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
        meetings = AppraisalMeeting.query.order_by(AppraisalMeeting.scheduled_for.asc()).all()
        return render_template("appraisals/index.html", appraisals=appraisals, meetings=meetings)

    @app.route("/admin/appraisals")
    @role_required("admin")
    def admin_appraisals():
        appraisals = Appraisal.query.order_by(Appraisal.id.desc()).all()
        employees = User.query.filter(User.role.in_(["employee", "manager", "demo_admin"])).order_by(User.employee_code).all()
        return render_template("appraisals/admin_list.html", appraisals=appraisals, employees=employees)

    @app.route("/manager/appraisals")
    @role_required("manager")
    def manager_appraisals():
        team_profiles = EmployeeProfile.query.filter_by(reporting_manager_id=current_user.id).all()
        team_user_ids = [p.user_id for p in team_profiles]

        appraisals = Appraisal.query.filter(
            Appraisal.user_id.in_(team_user_ids or [-1])
        ).order_by(Appraisal.id.desc()).all()

        return render_template("appraisals/manager_list.html", appraisals=appraisals)

    @app.route("/manager/appraisals/9-box")
    @role_required("manager")
    def manager_appraisal_matrix():
        team_profiles = EmployeeProfile.query.filter_by(reporting_manager_id=current_user.id).all()
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

    @app.route("/admin/appraisals/initiate", methods=["POST"])
    @role_required("admin")
    def admin_initiate_appraisal():
        period_type = request.form.get("period_type", "Q1 (Sep-Oct)")
        year = int(request.form.get("year") or 2026)
        user_id_val = request.form.get("user_id")

        if user_id_val == "all":
            employees = User.query.filter(User.role.in_(["employee", "manager", "demo_admin"])).all()
            count = 0
            for emp in employees:
                evaluator_id = emp.profile.reporting_manager_id if emp.profile else None
                _, success, _ = create_appraisal(emp.id, evaluator_id, period_type, year)
                if success:
                    count += 1
            flash(f"Appraisal cycle '{period_type} {year}' initiated for {count} employee(s)!", "success")
        else:
            emp = User.query.get_or_404(int(user_id_val))
            evaluator_id = emp.profile.reporting_manager_id if emp.profile else None
            _, success, msg = create_appraisal(emp.id, evaluator_id, period_type, year)
            flash(msg, "success" if success else "warning")

        return redirect(url_for("admin_appraisals"))

    @app.route("/appraisals/<int:appraisal_id>", methods=["GET", "POST"])
    @login_required
    def view_edit_appraisal(appraisal_id):
        appraisal = Appraisal.query.get_or_404(appraisal_id)

        is_owner = current_user.id == appraisal.user_id
        is_admin = current_user.role in ["admin", "demo_admin"]
        is_their_manager = (
            current_user.role == "manager"
            and appraisal.user.profile
            and appraisal.user.profile.reporting_manager_id == current_user.id
        )

        if not (is_owner or is_admin or is_their_manager):
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
            elif action == "decide_kras" and (is_their_manager or is_admin):
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
            elif action == "manager_review" and (is_their_manager or is_admin):
                decision = request.form.get("decision", "complete")  # complete or send_back
                rating = request.form.get("rating")
                potential_rating = request.form.get("potential_rating")
                evaluator_comments = request.form.get("evaluator_comments", "").strip()
                note = request.form.get("sendback_note", "").strip()

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
                    appraisal.potential_rating = max(1.0, min(5.0, float(potential_rating)))
                    db.session.commit()
                flash(msg, "success" if success else "danger")
                return redirect(url_for("view_edit_appraisal", appraisal_id=appraisal.id))

        return render_template(
            "appraisals/detail.html",
            appraisal=appraisal,
            is_owner=is_owner,
            is_evaluator=(is_their_manager or is_admin)
        )

    # -----------------------------------------------------------------
    # ANALYTICS DASHBOARD
    # -----------------------------------------------------------------
    @app.route("/admin/analytics")
    @app.route("/analytics")
    @role_required("admin", "manager")
    def admin_analytics_dashboard():
        month = request.args.get("month", type=int) or date.today().month
        year = request.args.get("year", type=int) or date.today().year

        rating_rows = []
        for appraisal in Appraisal.query.filter_by(year=year).all():
            if appraisal.rating is None and appraisal.self_rating is None:
                continue
            employee_name = appraisal.user.profile.full_name if appraisal.user and appraisal.user.profile else appraisal.user.employee_code if appraisal.user else "Unknown"
            dept_name = (appraisal.user.profile.department if appraisal.user and appraisal.user.profile else "Unassigned") or "Unassigned"
            mgr_name = (appraisal.user.profile.reporting_manager.profile.full_name if appraisal.user and appraisal.user.profile and appraisal.user.profile.reporting_manager and appraisal.user.profile.reporting_manager.profile else "Unassigned")
            rating_rows.append(SimpleNamespace(
                employee_name=employee_name,
                rating=float(appraisal.rating or 0.0),
                self_rating=float(appraisal.self_rating or 0.0),
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
        month = request.args.get("month", type=int) or date.today().month
        year = request.args.get("year", type=int) or date.today().year
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
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-disposition": f"attachment; filename=HR_Analytics_{year}_{month}.csv"},
        )

    # -----------------------------------------------------------------
    # EXPORT REPORTS ROUTES (Employees, Attendance, Payroll, Appraisals)
    # -----------------------------------------------------------------
    @app.route("/admin/reports")
    @app.route("/reports")
    @role_required("admin", "manager")
    def admin_reports_dashboard():
        today = date.today()
        return render_template("reports/index.html", current_month=today.month, current_year=today.year)

    @app.route("/admin/reports/employees/export")
    @role_required("admin", "manager")
    def export_employee_report():
        csv_data = generate_employee_report_csv()
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-disposition": "attachment; filename=Employees_Master_Report.csv"}
        )

    @app.route("/admin/reports/attendance/export")
    @role_required("admin", "manager")
    def export_attendance_report():
        month = request.args.get("month", type=int)
        year = request.args.get("year", type=int)
        csv_data = generate_attendance_report_csv(month, year)
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-disposition": f"attachment; filename=Attendance_Report_{month or 'All'}_{year or 'All'}.csv"}
        )

    @app.route("/admin/reports/payroll/export")
    @role_required("admin")
    def export_payroll_report():
        month = request.args.get("month", type=int)
        year = request.args.get("year", type=int)
        csv_data = generate_payroll_report_csv(month, year)
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-disposition": f"attachment; filename=Payroll_Report_{month or 'All'}_{year or 'All'}.csv"}
        )

    @app.route("/admin/reports/appraisals/export")
    @role_required("admin", "manager")
    def export_appraisal_report():
        period_type = request.args.get("period_type")
        year = request.args.get("year")
        csv_data = generate_appraisal_report_csv(period_type, year)
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-disposition": "attachment; filename=Performance_Appraisals_Report.csv"}
        )


    # -----------------------------------------------------------------
    # BULK ATTENDANCE ROUTES (Admin, Manager & Employee Full-Month Approval)
    # -----------------------------------------------------------------
    @app.route("/admin/attendance/bulk", methods=["GET", "POST"])
    @role_required("admin")
    def admin_bulk_attendance():
        employees = User.query.filter(User.role.in_(["employee", "manager", "demo_admin"])).order_by(User.employee_code).all()

        if request.method == "POST":
            user_ids = [int(uid) for uid in request.form.getlist("user_ids")]
            start_date = parse_date(request.form.get("start_date"))
            end_date = parse_date(request.form.get("end_date")) or start_date
            status = request.form.get("status", "Present")
            notes = request.form.get("notes", "").strip()

            if not user_ids or not start_date:
                flash("Select at least one employee and a start date.", "danger")
                return render_template("attendance/bulk_mark.html", employees=employees, role_mode="admin")

            count, msg = bulk_mark_attendance(user_ids, start_date, end_date, status, notes)
            flash(msg, "success")
            return redirect(url_for("admin_attendance", date=start_date.strftime("%Y-%m-%d")))

        return render_template("attendance/bulk_mark.html", employees=employees, role_mode="admin")

    @app.route("/manager/attendance/bulk", methods=["GET", "POST"])
    @role_required("manager")
    def manager_bulk_attendance():
        team_profiles = EmployeeProfile.query.filter_by(reporting_manager_id=current_user.id).all()
        team_user_ids = [p.user_id for p in team_profiles]
        team_members = User.query.filter(User.id.in_(team_user_ids or [-1])).order_by(User.employee_code).all()

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
        team_profiles = EmployeeProfile.query.filter_by(reporting_manager_id=current_user.id).all()
        team_user_ids = [p.user_id for p in team_profiles]

        requests = SpecialApproval.query.filter(
            SpecialApproval.user_id.in_(team_user_ids or [-1])
        ).order_by(SpecialApproval.applied_at.desc()).all()

        return render_template("special_approvals/manager_list.html", requests=requests)

    @app.route("/special-approvals/<int:approval_id>/decide", methods=["POST"])
    @login_required
    def decide_special_approval_route(approval_id):
        appr = SpecialApproval.query.get_or_404(approval_id)
        is_admin = current_user.role in ["admin", "demo_admin"]
        is_manager = (
            current_user.role == "manager"
            and appr.user.profile
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
        requests = SpecialApproval.query.order_by(SpecialApproval.applied_at.desc()).all()
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
        leads = DemoLead.query.order_by(DemoLead.id.desc()).all()
        return render_template("admin/demo_leads.html", leads=leads)

    @app.route("/admin/demo-leads/<int:lead_id>/status", methods=["POST"])
    @role_required("admin")
    def update_demo_lead_status(lead_id):
        lead = DemoLead.query.get_or_404(lead_id)
        new_status = request.form.get("status", "Contacted")
        lead.status = new_status
        db.session.commit()
        flash(f"Demo lead status for '{lead.full_name}' updated to '{lead.status}'.", "success")
        return redirect(url_for("admin_demo_leads"))

    @app.route("/recruitment")
    @role_required("admin", "manager")
    def recruitment_dashboard():
        requisitions = JobRequisition.query.order_by(JobRequisition.id.desc()).all()
        candidates = Candidate.query.order_by(Candidate.id.desc()).all()
        interviews = CandidateInterview.query.order_by(CandidateInterview.scheduled_time.asc()).all()

        return render_template(
            "recruitment/index.html",
            requisitions=requisitions,
            candidates=candidates,
            interviews=interviews
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
        action = request.form.get("action", "approve")
        reason = request.form.get("rejection_reason")
        _, success, msg = approve_manpower_requisition(req_id, current_user.id, action, reason)
        flash(msg, "success" if success else "danger")
        return redirect(url_for("recruitment_dashboard"))

    @app.route("/recruitment/requisitions/export")
    @role_required("admin", "manager")
    def export_requisitions_csv_route():
        csv_data = generate_requisitions_csv()
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-disposition": "attachment; filename=Manpower_Requisitions_Report.csv"}
        )

    @app.route("/recruitment/screener", methods=["GET", "POST"])
    @role_required("admin")
    def cv_screener():
        requisitions = JobRequisition.query.filter(JobRequisition.status == "Approved").all()

        if request.method == "POST":
            requisition_id = request.form.get("requisition_id")
            raw_cv_text = request.form.get("raw_cv_text")
            files = request.files.getlist("resumes") or request.files.getlist("resume")

            if not requisition_id:
                flash("Please select an approved Job Requisition to scan against.", "danger")
                return redirect(url_for("cv_screener"))

            scanned_count = 0
            for file in files:
                if file and file.filename and allowed_file(file.filename):
                    filename = secure_filename(f"cv_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{file.filename}")
                    file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                    file.save(file_path)

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

        candidates = Candidate.query.order_by(Candidate.match_score_percent.desc()).all()
        return render_template("recruitment/screener.html", requisitions=requisitions, candidates=candidates)

    @app.route("/recruitment/candidates/<int:candidate_id>/edit", methods=["GET", "POST"])
    @role_required("admin")
    def edit_candidate(candidate_id):
        cand = Candidate.query.get_or_404(candidate_id)

        if request.method == "POST":
            full_name = request.form.get("full_name")
            email = request.form.get("email")
            phone = request.form.get("phone")
            company = request.form.get("current_company")
            designation = request.form.get("current_designation")
            exp = request.form.get("total_exp_years", 0)
            skills = request.form.get("key_skills")
            notes = request.form.get("hr_notes")

            if not full_name or not email or not skills:
                flash("Full name, email, and key skills are required.", "danger")
                return render_template("recruitment/edit_candidate.html", candidate=cand)

            _, success, msg = update_candidate_screening(
                cand.id, full_name, email, phone, company, designation, exp, skills, notes
            )
            flash(msg, "success" if success else "danger")
            return redirect(url_for("cv_screener"))

        return render_template("recruitment/edit_candidate.html", candidate=cand)

    @app.route("/recruitment/candidates/<int:candidate_id>/delete", methods=["POST"])
    @role_required("admin")
    def delete_candidate_route(candidate_id):
        success, msg = delete_candidate(candidate_id)
        flash(msg, "success" if success else "danger")
        return redirect(url_for("cv_screener"))

    @app.route("/recruitment/interviews", methods=["GET", "POST"])
    @role_required("admin", "manager")
    def interviews_calendar():
        managers = User.query.filter(User.role.in_(["manager", "admin"])).all()
        candidates = Candidate.query.filter(Candidate.status.in_(["Screened", "Shortlisted", "Interview Scheduled"])).all()

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
            interviews = CandidateInterview.query.filter_by(interviewer_id=current_user.id).order_by(CandidateInterview.scheduled_time.asc()).all()
        else:
            interviews = CandidateInterview.query.order_by(CandidateInterview.scheduled_time.asc()).all()

        return render_template("recruitment/interviews.html", interviews=interviews, managers=managers, candidates=candidates)

    @app.route("/recruitment/interviews/<int:interview_id>/feedback", methods=["POST"])
    @role_required("admin", "manager")
    def submit_interview_feedback_route(interview_id):
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
        cand = Candidate.query.get_or_404(candidate_id)

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
        cand = Candidate.query.get_or_404(candidate_id)
        if cand.status != "Offered" and not cand.offered_ctc:
            flash("Offer letter has not been issued for this candidate yet.", "warning")
            return redirect(url_for("recruitment_dashboard"))

        return render_template("recruitment/offer_letter_view.html", candidate=cand)