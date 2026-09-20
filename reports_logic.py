"""
HRMS Data Export & CSV / Excel Reports Engine.
Generates downloadable report files for Employees, Attendance, Payroll, and Appraisals.
"""

import csv
from io import StringIO, BytesIO
from datetime import date
from models import User, EmployeeProfile, Attendance, Payslip, Appraisal, EmployeeCTC


def generate_employee_report_csv():
    """Export all employee profiles to CSV."""
    output = StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Employee Code", "Full Name", "Role", "Designation", "Department",
        "Date of Joining", "Reporting Manager", "Founding Member", "Monthly CTC (INR)",
        "Email", "Phone", "PAN Number", "Aadhaar Number", "Bank Account", "IFSC Code"
    ])

    users = User.query.filter(User.role.in_(["employee", "manager", "demo_admin"])).order_by(User.employee_code).all()

    for u in users:
        p = u.profile
        mgr_name = p.reporting_manager.profile.full_name if p and p.reporting_manager and p.reporting_manager.profile else "-"
        writer.writerow([
            u.employee_code,
            p.full_name if p else "-",
            u.role.capitalize(),
            p.designation if p else "-",
            p.department if p else "-",
            p.date_of_joining.strftime("%Y-%m-%d") if p and p.date_of_joining else "-",
            mgr_name,
            "Yes" if p and p.is_founding_member else "No",
            f"{p.monthly_ctc:.2f}" if p else "0.00",
            p.personal_email if p else "-",
            p.phone_number if p else "-",
            p.pan_number if p else "-",
            p.aadhaar_number if p else "-",
            p.bank_account_number if p else "-",
            p.bank_ifsc_code if p else "-"
        ])

    output.seek(0)
    return output.getvalue()


def generate_attendance_report_csv(month=None, year=None):
    """Export monthly attendance records to CSV."""
    today = date.today()
    month = month or today.month
    year = year or today.year

    output = StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Employee Code", "Full Name", "Date", "Clock In", "Clock Out",
        "Duration", "Status", "OT Hours", "Off Day", "Notes"
    ])

    import calendar
    last_day = calendar.monthrange(year, month)[1]
    start_d = date(year, month, 1)
    end_d = date(year, month, last_day)

    records = Attendance.query.filter(
        Attendance.date >= start_d,
        Attendance.date <= end_d
    ).order_by(Attendance.date.asc()).all()

    for r in records:
        u = r.user
        p = u.profile if u else None
        writer.writerow([
            u.employee_code if u else "-",
            p.full_name if p else "-",
            r.date.strftime("%Y-%m-%d"),
            r.clock_in.strftime("%I:%M %p") if r.clock_in else "-",
            r.clock_out.strftime("%I:%M %p") if r.clock_out else "-",
            r.work_duration,
            r.status,
            r.ot_hours or 0.0,
            "Yes" if r.is_off_day else "No",
            r.notes or "-"
        ])

    output.seek(0)
    return output.getvalue()


def generate_payroll_report_csv(month=None, year=None):
    """Export monthly payroll & payslip details to CSV."""
    today = date.today()
    month = month or today.month
    year = year or today.year

    output = StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Employee Code", "Full Name", "Month", "Year", "Monthly CTC Used",
        "Gross Pay", "Arrears", "Incentive / Bonus", "OT Amount",
        "Total Deductions", "Loss of Pay (LOP)", "PF Deduction", "Net Pay"
    ])

    payslips = Payslip.query.filter_by(month=month, year=year).all()

    for ps in payslips:
        u = ps.user
        p = u.profile if u else None
        writer.writerow([
            u.employee_code if u else "-",
            p.full_name if p else "-",
            ps.month_label(),
            ps.year,
            f"{ps.monthly_ctc_used:.2f}",
            f"{ps.gross_pay:.2f}",
            f"{ps.arrears:.2f}",
            f"{ps.incentive:.2f}",
            f"{ps.ot_amount:.2f}",
            f"{ps.total_deductions:.2f}",
            f"{ps.loss_of_pay:.2f}",
            f"{ps.pf_deduction:.2f}",
            f"{ps.net_pay:.2f}"
        ])

    output.seek(0)
    return output.getvalue()


def generate_appraisal_report_csv(period_type=None, year=None):
    """Export performance appraisals summary to CSV."""
    output = StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Employee Code", "Full Name", "Review Period", "Period Type", "Year",
        "Evaluator / Manager", "Status", "Self Rating (Out of 5)",
        "Manager Rating (Out of 5)", "Weighted Score %", "Self Comments", "Manager Comments"
    ])

    query = Appraisal.query
    if period_type:
        query = query.filter_by(period_type=period_type)
    if year:
        query = query.filter_by(year=int(year))

    appraisals = query.order_by(Appraisal.id.desc()).all()

    for a in appraisals:
        u = a.user
        p = u.profile if u else None
        ev_p = a.evaluator.profile if a.evaluator else None
        evaluator_name = ev_p.full_name if ev_p else "-"

        writer.writerow([
            u.employee_code if u else "-",
            p.full_name if p else "-",
            a.review_period,
            a.period_type,
            a.year,
            evaluator_name,
            a.status,
            a.self_rating if a.self_rating else "-",
            a.rating if a.rating else "-",
            f"{a.weighted_score:.2f}%" if a.weighted_score is not None else "-",
            a.self_comments or "-",
            a.evaluator_comments or "-"
        ])

    output.seek(0)
    return output.getvalue()
