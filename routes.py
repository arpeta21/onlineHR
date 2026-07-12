import os
from datetime import datetime, date
from flask import render_template, redirect, url_for, flash, request, send_from_directory, abort
from werkzeug.utils import secure_filename

from models import User, EmployeeProfile, Child, LeaveType, LeaveBalance, LeaveApplication
from leave_logic import (ensure_balances_for_user, recalculate_user_balances,
                          recalculate_all_balances_for_type, business_days_count)


def register_routes(app, db, login_user, logout_user, login_required, current_user,
                     allowed_file, parse_date, next_employee_code,
                     validate_pan, validate_aadhaar, validate_ifsc, role_required):

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
        if current_user.role == "admin":
            return redirect(url_for("admin_dashboard"))
        elif current_user.role == "manager":
            return redirect(url_for("manager_dashboard"))
        else:
            return redirect(url_for("employee_dashboard"))

    # -----------------------------------------------------------------
    # ADMIN routes
    # -----------------------------------------------------------------
    @app.route("/admin")
    @role_required("admin")
    def admin_dashboard():
        total_employees = User.query.filter_by(role="employee").count()
        total_managers = User.query.filter_by(role="manager").count()
        completed = EmployeeProfile.query.filter_by(profile_completed=True).count()
        pending = User.query.filter(User.role.in_(["employee", "manager"])).count() - completed
        recent = User.query.filter(User.role.in_(["employee", "manager"])) \
            .order_by(User.id.desc()).limit(5).all()
        pending_leaves = LeaveApplication.query.filter_by(status="pending").count()
        return render_template("admin/dashboard.html", total_employees=total_employees,
                                total_managers=total_managers, completed=completed,
                                pending=max(pending, 0), recent=recent,
                                pending_leaves=pending_leaves)

    @app.route("/admin/employees")
    @role_required("admin")
    def admin_employee_list():
        q = request.args.get("q", "").strip()
        query = User.query.filter(User.role.in_(["employee", "manager"]))
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

            if not full_name:
                flash("Employee name is required.", "danger")
                return render_template("admin/create_employee.html", managers=managers,
                                        form=request.form)

            code = next_employee_code()
            user = User(employee_code=code, role=role, must_change_password=True)
            user.set_password(temp_password)
            db.session.add(user)
            db.session.flush()  # get user.id before commit

            profile = EmployeeProfile(
                user_id=user.id,
                full_name=full_name,
                designation=designation,
                department=department,
                date_of_joining=date_of_joining,
                reporting_manager_id=int(reporting_manager_id) if reporting_manager_id else None,
            )
            db.session.add(profile)
            db.session.commit()

            flash(f"Employee created. Login ID: {code} | Temp password: {temp_password}", "success")
            return redirect(url_for("admin_employee_list"))

        return render_template("admin/create_employee.html", managers=managers, form={})

    @app.route("/admin/employees/<int:user_id>")
    @role_required("admin")
    def admin_view_employee(user_id):
        user = User.query.get_or_404(user_id)
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

            if not new_code or not full_name:
                flash("Employee ID and name are both required.", "danger")
                return render_template("admin/edit_employee.html", user=user, managers=managers,
                                        form=request.form)

            # Employee ID must stay unique across everyone else
            clash = User.query.filter(User.employee_code == new_code, User.id != user.id).first()
            if clash:
                flash(f"Employee ID {new_code} is already used by someone else.", "danger")
                return render_template("admin/edit_employee.html", user=user, managers=managers,
                                        form=request.form)

            # Can't report to yourself
            if reporting_manager_id and int(reporting_manager_id) == user.id:
                flash("Someone can't be their own reporting manager.", "danger")
                return render_template("admin/edit_employee.html", user=user, managers=managers,
                                        form=request.form)

            # Demoting a manager who still has people reporting to them would orphan their team
            if user.role == "manager" and new_role != "manager":
                team_size = EmployeeProfile.query.filter_by(reporting_manager_id=user.id).count()
                if team_size > 0:
                    noun = "person" if team_size == 1 else "people"
                    verb = "reports" if team_size == 1 else "report"
                    flash(f"Can't change {user.profile.full_name}'s account type away from Manager — "
                          f"{team_size} {noun} still {verb} to them. "
                          f"Reassign their team first.", "danger")
                    return render_template("admin/edit_employee.html", user=user, managers=managers,
                                            form=request.form)

            # Simple circular-manager guard: the chosen manager can't report (directly) to this user
            if reporting_manager_id:
                chosen = User.query.get(int(reporting_manager_id))
                if chosen and chosen.profile and chosen.profile.reporting_manager_id == user.id:
                    flash("That would create a reporting loop — they already report to this person.", "danger")
                    return render_template("admin/edit_employee.html", user=user, managers=managers,
                                            form=request.form)

            user.employee_code = new_code
            user.role = new_role
            user.profile.full_name = full_name
            user.profile.designation = designation
            user.profile.department = department
            user.profile.date_of_joining = date_of_joining
            user.profile.reporting_manager_id = int(reporting_manager_id) if reporting_manager_id else None
            db.session.commit()

            flash(f"{full_name}'s details have been updated.", "success")
            return redirect(url_for("admin_view_employee", user_id=user.id))

        return render_template("admin/edit_employee.html", user=user, managers=managers, form=None)

    @app.route("/admin/employees/<int:user_id>/deactivate", methods=["POST"])
    @role_required("admin")
    def admin_deactivate_employee(user_id):
        user = User.query.get_or_404(user_id)
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
        if not user.profile:
            abort(404)
        user.is_active = True
        db.session.commit()
        flash(f"{user.profile.full_name} has been reactivated.", "success")
        return redirect(url_for("admin_employee_list"))

    @app.route("/admin/managers")
    @role_required("admin")
    def admin_manager_list():
        managers = User.query.filter_by(role="manager").all()
        team_counts = {
            m.id: EmployeeProfile.query.filter_by(reporting_manager_id=m.id).count()
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
        return render_template("manager/dashboard.html", team=team,
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
        return render_template("employee/dashboard.html", profile=profile)

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
        is_their_manager = (current_user.role == "manager" and
                             target.profile.reporting_manager_id == current_user.id)

        if not (is_owner or is_admin or is_their_manager):
            abort(403)

        return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=True)
