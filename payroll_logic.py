"""
Payroll calculation engine.

Admin defines reusable PayComponents (Basic, HRA, PF, etc.) with a fixed
calculation basis - never a free-form formula, so there's no arbitrary
expression evaluation risk. Each employee has one active monthly CTC.
Generating a payslip for a given month snapshots every component's computed
amount into PayslipLine rows, so later edits to a component's formula never
retroactively change a payslip that's already been generated.

Calculation order:
  1. Components are processed in ascending calc_order.
  2. flat and percent_of_ctc components can be computed immediately, since
     they don't depend on anything else on the payslip.
  3. percent_of_basic needs the Basic component's amount already computed -
     this means Basic (typically percent_of_ctc, calc_order low) must run
     before any percent_of_basic component (higher calc_order).
  4. percent_of_gross needs the full gross (sum of all earning components)
     already computed - so percent_of_gross components should have the
     highest calc_order of all, after every earning component.
  5. Gross pay = sum of all earning component amounts.
     Net pay = Gross pay - sum of all deduction component amounts.
"""

from models import (
    db, PayComponent, EmployeeCTC, EmployeeCTCLine, Payslip, PayslipLine, Attendance, User,
    EmployeePayrollAdjustment, SalaryAdvance,
    InvestmentDeclaration,
)
from flask_login import current_user
from time_utils import today_ist
from statutory import (
    PTNotConfigured,
    LWFNotConfigured,
    esi_contribution,
    fy_for,
    labour_welfare_fund,
    monthly_tds,
    months_remaining_in_fy,
    pf_contribution,
    pf_wage_ceiling_for,
    professional_tax,
    resolve_state_code,
    approved_tax_deduction,
)
from work_calendar import holiday_for_date

STATUTORY_KEYWORDS = ("provident", "pf", "professional tax", "p tax", "esi", "tds", "income tax")

OT_RATE_MULTIPLIER = 2.0
STANDARD_MONTHLY_WORKING_HOURS = 208.0
GRATUITY_MONTHLY_RATE_OF_BASIC = 15.0 / 26.0 / 12.0
MIN_RECOMMENDED_BASIC_PERCENT_OF_CTC = 50.0


def _is_statutory_component(name):
    low = name.lower()
    return any(keyword in low for keyword in STATUTORY_KEYWORDS)


def calculate_statutory_deductions(
    basic_wage,
    monthly_gross,
    month,
    year=None,
    state_code=None,
    gender=None,
    disabled=False,
    annual_gross=None,
    tds_deducted_so_far=0.0,
    months_remaining=12,
    fy="2026-27",
    restrict_pf_to_ceiling=True,
    tax_regime="new_regime",
    other_deductions=0.0,
):
    """Calculate statutory values without silently guessing missing state data.

    This helper is intentionally separate from ``generate_payslip`` until the
    employee model stores state, gender, disability, FY and TDS history.
    """
    period_date = None
    if year:
        import calendar
        from datetime import date
        period_date = date(year, month, calendar.monthrange(year, month)[1])
    pf = pf_contribution(basic_wage, fy=fy, restrict_to_ceiling=restrict_pf_to_ceiling, as_of_date=period_date)
    esi = esi_contribution(monthly_gross, fy=fy, disabled=disabled)
    tds = None if annual_gross is None else monthly_tds(
        annual_gross, tds_deducted_so_far, months_remaining, fy=fy,
        tax_regime=tax_regime, other_deductions=other_deductions,
    )

    pt = None
    pt_error = None
    if state_code:
        try:
            pt = professional_tax(state_code, monthly_gross, month, gender)
        except (PTNotConfigured, ValueError) as exc:
            pt_error = str(exc)

    lwf_employee = 0.0
    lwf_employer = 0.0
    lwf_error = None
    if state_code:
        try:
            lwf_employee, lwf_employer = labour_welfare_fund(state_code, month, wage=monthly_gross)
        except LWFNotConfigured as exc:
            lwf_error = str(exc)

    return {"pf": pf, "esi": esi, "professional_tax": pt, "tds": tds, "pt_error": pt_error,
            "lwf_employee": lwf_employee, "lwf_employer": lwf_employer, "lwf_error": lwf_error}


def mask_sensitive(value, keep=4):
    if value in (None, ""):
        return "-"
    text = str(value).strip()
    if text.startswith("enc:"):
        return "****"
    if len(text) <= 8:
        return "*" * len(text)
    visible = max(keep, 2)
    return f"{text[:visible]}{'*' * (len(text) - visible)}"


def get_active_ctc(user_id, as_of_date=None):
    """The CTC record in effect as of a given date (defaults to today). Fallback to most recent CTC."""
    from datetime import date
    as_of_date = as_of_date or today_ist()
    record = EmployeeCTC.query.filter(
        EmployeeCTC.user_id == user_id,
        EmployeeCTC.effective_from <= as_of_date
    ).order_by(EmployeeCTC.effective_from.desc()).first()

    if not record:
        record = EmployeeCTC.query.filter(
            EmployeeCTC.user_id == user_id
        ).order_by(EmployeeCTC.effective_from.desc()).first()

    return record


def build_automatic_ctc_lines(monthly_ctc):
    """Return HR-editable proposals for a standard Indian salary structure."""
    basic = round(monthly_ctc * 0.50, 2)
    hra = round(basic * 0.50, 2)
    special = round(max(0.0, monthly_ctc - basic - hra), 2)
    return [
        {"name": "Basic Salary", "type": "earning", "code": "basic", "basis": "percent_ctc", "value": 50.0, "amount": basic, "enabled": True, "employer": False},
        {"name": "House Rent Allowance (HRA)", "type": "earning", "code": "hra", "basis": "percent_basic", "value": 50.0, "amount": hra, "enabled": True, "employer": False},
        {"name": "Special Allowance", "type": "earning", "code": "special_allowance", "basis": "residual", "value": 0.0, "amount": special, "enabled": True, "employer": False},
        {"name": "Employer PF Contribution", "type": "earning", "code": "employer_pf", "basis": "percent_basic", "value": 12.0, "amount": 0.0, "enabled": False, "employer": True},
        {"name": "Gratuity Provision", "type": "earning", "code": "gratuity", "basis": "percent_basic", "value": round(GRATUITY_MONTHLY_RATE_OF_BASIC * 100, 4), "amount": 0.0, "enabled": False, "employer": True},
        {"name": "Provident Fund (PF)", "type": "deduction", "code": "pf", "basis": "statutory", "value": 12.0, "amount": 0.0, "enabled": True, "employer": False},
        {"name": "Voluntary Provident Fund (VPF)", "type": "deduction", "code": "vpf", "basis": "percent_basic", "value": 0.0, "amount": 0.0, "enabled": False, "employer": False},
        {"name": "ESIC", "type": "deduction", "code": "esic", "basis": "statutory", "value": 0.75, "amount": 0.0, "enabled": True, "employer": False},
        {"name": "Professional Tax", "type": "deduction", "code": "professional_tax", "basis": "statutory", "value": 0.0, "amount": 0.0, "enabled": True, "employer": False},
        {"name": "TDS / Income Tax", "type": "deduction", "code": "tds", "basis": "statutory", "value": 0.0, "amount": 0.0, "enabled": True, "employer": False},
        {"name": "Superannuation", "type": "earning", "code": "superannuation", "basis": "percent_basic", "value": 0.0, "amount": 0.0, "enabled": False, "employer": True},
    ]


def calculate_ctc_breakup_lines(monthly_ctc, lines, employee=None, month=None, year=None):
    """Calculate editable CTC lines while reconciling every in-CTC line."""
    month = month or today_ist().month
    year = year or today_ist().year
    fy = fy_for(month, year)
    earnings = [line for line in lines if line.component_type == "earning" and line.enabled]
    deductions = [line for line in lines if line.component_type == "deduction" and line.enabled and not line.is_employer_cost]
    residuals = [line for line in earnings if line.calc_basis == "residual" or line.statutory_code == "special_allowance"]
    ordered = [line for line in earnings if line not in residuals]
    gross_based = [line for line in ordered if line.calc_basis in {"percent_gross", "percent_of_gross"}]
    ordered = [line for line in ordered if line not in gross_based] + gross_based
    basic = 0.0
    gross = 0.0
    ctc_consumed = 0.0
    amounts = {}

    for line in ordered:
        name = line.component_name.lower()
        if line.is_hr_override and line.calc_basis == "flat":
            amount = line.calc_value
        elif line.statutory_code == "basic":
            amount = monthly_ctc * (line.calc_value / 100 or 0.50)
        elif line.statutory_code == "hra":
            amount = basic * (line.calc_value / 100 or 0.50)
        elif line.statutory_code == "employer_pf":
            amount = pf_contribution(basic, fy=fy)["employer_total"]
        elif line.statutory_code == "gratuity":
            amount = basic * (line.calc_value / 100 or GRATUITY_MONTHLY_RATE_OF_BASIC)
        elif line.calc_basis in {"percent_ctc", "percent_ctc"}:
            amount = monthly_ctc * line.calc_value / 100
        elif line.calc_basis in {"percent_basic", "percent_of_basic"}:
            amount = basic * line.calc_value / 100
        elif line.calc_basis in {"percent_gross", "percent_of_gross"}:
            amount = gross * line.calc_value / 100
        else:
            amount = line.calc_value
        amount = round(max(0.0, amount), 2)
        amounts[line.id] = amount
        if line.statutory_code == "basic" or name in {"basic", "basic salary"}:
            basic = amount
        if getattr(line, "is_in_ctc", True):
            ctc_consumed += amount
        if not line.is_employer_cost:
            gross += amount

    for line in residuals:
        amount = line.calc_value if line.is_hr_override and line.calc_basis == "flat" else max(0.0, monthly_ctc - ctc_consumed)
        amount = round(max(0.0, amount), 2)
        amounts[line.id] = amount
        if getattr(line, "is_in_ctc", True):
            ctc_consumed += amount
        if not line.is_employer_cost:
            gross += amount

    state_code = resolve_state_code(employee.profile.current_state) if employee and employee.profile else None
    gender = {"male": "M", "female": "F"}.get((employee.profile.gender or "").lower()) if employee and employee.profile else None
    for line in deductions:
        if line.is_hr_override and line.calc_basis == "flat":
            amount = line.calc_value
        elif line.statutory_code == "pf":
            amount = pf_contribution(basic, fy=fy)["employee_epf"]
        elif line.statutory_code == "esic":
            esi = esi_contribution(gross, fy=fy)
            amount = esi["employee"] if esi["applicable"] else 0.0
        elif line.statutory_code == "professional_tax" and state_code:
            try:
                amount = professional_tax(state_code, gross, month, gender) or 0.0
            except (PTNotConfigured, ValueError):
                amount = 0.0
        elif line.statutory_code in {"tds", "vpf"} or line.calc_basis == "statutory":
            amount = 0.0
        elif line.calc_basis in {"percent_basic", "percent_of_basic"}:
            amount = basic * line.calc_value / 100
        elif line.calc_basis in {"percent_gross", "percent_of_gross"}:
            amount = gross * line.calc_value / 100
        elif line.calc_basis in {"percent_ctc", "percent_of_ctc"}:
            amount = monthly_ctc * line.calc_value / 100
        else:
            amount = line.calc_value
        amounts[line.id] = round(max(0.0, amount), 2)
    return amounts


def calculate_net_pay(gross_pay, loss_of_pay=0.0, pf=0.0, esic=0.0,
                      tds=0.0, professional_tax=0.0, lwf=0.0,
                      miscellaneous_deduction=0.0, advance_recovery=0.0):
    """Apply the payroll contract: gross less each employee deduction once."""
    total_deductions = sum(
        max(0.0, float(value or 0.0))
        for value in (
            loss_of_pay, pf, esic, tds, professional_tax, lwf,
            miscellaneous_deduction, advance_recovery,
        )
    )
    return round(float(gross_pay or 0.0) - total_deductions, 2), round(total_deductions, 2)


def compute_payslip_amounts(monthly_ctc, employee_ctc=None):
    """
    Run every active PayComponent against a given monthly CTC and return
    (lines, gross_pay, total_deductions, net_pay) where lines is a list of
    dicts: {name, type, amount}. Does NOT touch the database - pure
    calculation, so it can be used for both real generation and a
    "preview" before committing.
    """
    if employee_ctc and employee_ctc.breakup_status == "approved" and employee_ctc.lines:
        components = [line for line in employee_ctc.lines if line.enabled]
    else:
        components = PayComponent.query.filter_by(is_active=True).order_by(
            PayComponent.calc_order, PayComponent.id
        ).all()
        component_priority = {
            "basic": 0,
            "hra": 1,
            "special_allowance": 2,
        }
        components.sort(key=lambda component: (
            component_priority.get(component.statutory_code, 10),
            component.calc_order,
            component.id,
        ))
        components = [
            component for component in components
            if not (
                component.statutory_code is None
                and _is_statutory_component(component.name)
            )
        ]

    basic_amount = 0.0
    earning_total_so_far = 0.0
    lines = []

    deferred_gross_based = []

    earnings = [comp for comp in components if comp.component_type == "earning"]
    deductions = [comp for comp in components if comp.component_type == "deduction"]

    for comp in earnings:
        component_name = comp.component_name.strip().lower() if isinstance(comp, EmployeeCTCLine) else comp.name.strip().lower()
        is_override = isinstance(comp, EmployeeCTCLine) and comp.is_hr_override
        if isinstance(comp, EmployeeCTCLine):
            amount = comp.monthly_amount
        elif comp.calc_basis == "residual":
            amount = max(0.0, monthly_ctc - earning_total_so_far)
        elif component_name in {"basic", "basic salary"} and not is_override:
            amount = monthly_ctc * 0.50
        elif component_name in {"hra", "house rent allowance", "house rent allowance (hra)"} and not is_override:
            amount = basic_amount * 0.50
        elif component_name in {"special allowance", "special allowance (residual)"} and not is_override:
            amount = max(0.0, monthly_ctc - earning_total_so_far)
        elif comp.calc_basis == "flat":
            amount = comp.calc_value
        elif comp.calc_basis in ["percent_of_ctc", "percent_ctc"]:
            amount = monthly_ctc * (comp.calc_value / 100)
        elif comp.calc_basis in ["percent_of_basic", "percent_basic"]:
            amount = basic_amount * (comp.calc_value / 100)
        elif comp.calc_basis in ["percent_of_gross", "percent_gross"]:
            deferred_gross_based.append(comp)
            continue
        else:
            amount = 0.0

        amount = round(amount, 2)
        if getattr(comp, "is_basic", False) or component_name in {"basic", "basic salary"}:
            basic_amount = amount

        line_type = "employer_cost" if getattr(comp, "is_employer_cost", False) else comp.component_type
        lines.append({
            "name": comp.component_name if isinstance(comp, EmployeeCTCLine) else comp.name,
            "type": line_type,
            "amount": amount,
            "statutory_code": getattr(comp, "statutory_code", None),
        })
        if line_type == "earning":
            earning_total_so_far += amount

    for comp in deferred_gross_based:
        amount = round(earning_total_so_far * (comp.calc_value / 100), 2)
        line_type = "employer_cost" if getattr(comp, "is_employer_cost", False) else comp.component_type
        lines.append({
            "name": comp.component_name if isinstance(comp, EmployeeCTCLine) else comp.name,
            "type": line_type,
            "amount": amount,
            "statutory_code": getattr(comp, "statutory_code", None),
        })
        if line_type == "earning":
            earning_total_so_far += amount

    for comp in deductions:
        if isinstance(comp, EmployeeCTCLine):
            amount = comp.monthly_amount
        elif comp.calc_basis in ["percent_of_gross", "percent_gross"]:
            amount = earning_total_so_far * (comp.calc_value / 100)
        elif comp.calc_basis in ["percent_of_basic", "percent_basic"]:
            amount = basic_amount * (comp.calc_value / 100)
        elif comp.calc_basis in ["percent_of_ctc", "percent_ctc"]:
            amount = monthly_ctc * (comp.calc_value / 100)
        elif comp.calc_basis == "flat":
            amount = comp.calc_value
        else:
            amount = 0.0
        line_type = "employer_cost" if getattr(comp, "is_employer_cost", False) else "deduction"
        lines.append({
            "name": comp.component_name if isinstance(comp, EmployeeCTCLine) else comp.name,
            "type": line_type,
            "amount": round(amount, 2),
            "statutory_code": getattr(comp, "statutory_code", None),
        })

    gross_pay = round(sum(l["amount"] for l in lines if l["type"] == "earning"), 2)
    total_deductions = round(sum(l["amount"] for l in lines if l["type"] == "deduction"), 2)
    net_pay = round(gross_pay - total_deductions, 2)

    return lines, gross_pay, total_deductions, net_pay


def _compute_attendance_days_and_lop(user_id, month, year, monthly_ctc):
    """Return payable attendance days and the corresponding automatic LOP."""
    import calendar
    from datetime import date
    from models import Attendance, LeaveApplication, SpecialApproval

    last_day = calendar.monthrange(year, month)[1]
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)
    employee = User.query.get(user_id)
    state_code = employee.profile.current_state if employee and employee.profile else None
    att_recs = Attendance.query.filter(
        Attendance.user_id == user_id,
        Attendance.date >= start_date,
        Attendance.date <= end_date,
    ).all()
    present_count = 0.0
    recorded_dates = set()
    for record in att_recs:
        recorded_dates.add(record.date)
        if record.status in {"Present", "Late"}:
            present_count += 1.0
        elif record.status == "Half Day":
            present_count += 0.5

    approved_leave_days = sum(item.days for item in LeaveApplication.query.filter(
        LeaveApplication.user_id == user_id,
        LeaveApplication.status == "approved",
        LeaveApplication.start_date <= end_date,
        LeaveApplication.end_date >= start_date,
    ).all())
    approved_special_days = sum(item.days for item in SpecialApproval.query.filter(
        SpecialApproval.user_id == user_id,
        SpecialApproval.status == "approved",
        SpecialApproval.start_date <= end_date,
        SpecialApproval.end_date >= start_date,
    ).all())
    payable_off_days = sum(
        1 for day_number in range(1, last_day + 1)
        if (date(year, month, day_number).weekday() == 6 or
            holiday_for_date(date(year, month, day_number), state_code))
        and (
            date(year, month, day_number) not in recorded_dates
            or any(record.status != "Absent" for record in att_recs
                   if record.date == date(year, month, day_number))
        )
    )
    if not att_recs:
        payable_days = float(last_day)
        lop_days = 0.0
        lop_amount = 0.0
    else:
        payable_days = min(
            float(last_day),
            present_count + approved_leave_days + approved_special_days + payable_off_days,
        )
        lop_days = max(0.0, float(last_day) - payable_days)
        lop_amount = round(monthly_ctc / float(last_day) * lop_days, 2) if monthly_ctc else 0.0
    return last_day, present_count, approved_leave_days, payable_days, lop_days, lop_amount


def get_employee_monthly_payroll_inputs(user_id, month, year):
    """
    Calculate attendance-linked defaults for payroll run:
    - total_month_days
    - present_days
    - approved_leave_days
    - payable_days
    - lop_days
    - auto_lop_amount
    - total_ot_hours
    - auto_ot_amount
    - default_pf
    """
    import calendar
    from datetime import date
    from models import Attendance, EmployeeCTC

    last_day = calendar.monthrange(year, month)[1]
    total_month_days = last_day
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    ctc_rec = get_active_ctc(user_id, as_of_date=end_date)
    monthly_ctc = ctc_rec.monthly_ctc if ctc_rec else 0.0
    employee = User.query.get(user_id)
    state_code = employee.profile.current_state if employee and employee.profile else None

    att_recs = Attendance.query.filter(
        Attendance.user_id == user_id,
        Attendance.date >= start_date,
        Attendance.date <= end_date
    ).all()

    ot_hours = 0.0

    for r in att_recs:
        ot_hours += (r.ot_hours or 0.0)
    (total_month_days, present_count, approved_leave_days,
     payable_days, lop_days, auto_lop_amount) = _compute_attendance_days_and_lop(
        user_id, month, year, monthly_ctc
    )

    lines, calculated_gross, _, _ = compute_payslip_amounts(monthly_ctc, ctc_rec)
    breakup_deductions = round(sum(
        line["amount"] for line in lines
        if line["type"] == "deduction" and not (
            _is_statutory_component(line["name"])
            or line.get("statutory_code") in {"pf", "vpf", "esic", "professional_tax", "tds"}
        )
    ), 2)
    basic_line = next((l for l in lines if l["name"].lower().startswith("basic")), None)
    basic_amt = basic_line["amount"] if basic_line else (monthly_ctc * 0.5)
    ot_hourly_rate = round((basic_amt / STANDARD_MONTHLY_WORKING_HOURS) * OT_RATE_MULTIPLIER, 2)
    auto_ot_amount = round(ot_hours * ot_hourly_rate, 2)

    default_pf = 0.0

    default_esi = 0.0
    default_tax = 0.0
    default_tds = 0.0
    approved_investment_total = 0.0
    advance_repayment = 0.0
    active_advance = SalaryAdvance.query.filter_by(user_id=user_id, status="active").order_by(SalaryAdvance.id).first()
    if active_advance and active_advance.start_year * 12 + active_advance.start_month <= year * 12 + month:
        advance_repayment = min(active_advance.monthly_repayment, active_advance.outstanding_amount)
    if employee and employee.profile:
        try:
            fy = fy_for(month, year)
            tax_regime = employee.profile.tax_regime or "new_regime"
            declarations = InvestmentDeclaration.query.filter_by(
                user_id=user_id, financial_year=fy, tax_regime=tax_regime,
                status="approved",
            ).all()
            approved_investment_total = approved_tax_deduction(declarations, tax_regime)
            fy_start_key = (year if month >= 4 else year - 1) * 12 + 4
            current_key = year * 12 + month
            prior_payslips = [
                payslip for payslip in Payslip.query.filter(Payslip.user_id == user_id).all()
                if fy_start_key <= payslip.year * 12 + payslip.month < current_key
            ]
            prior_gross = sum(payslip.gross_pay or 0.0 for payslip in prior_payslips)
            prior_tds = sum(payslip.tds_deduction or 0.0 for payslip in prior_payslips)
            projected_annual_gross = prior_gross + calculated_gross * months_remaining_in_fy(month)
            state_code = resolve_state_code(employee.profile.current_state if employee.profile else None)
            gender = {"male": "M", "female": "F"}.get((employee.profile.gender or "").strip().lower()) if employee.profile else None
            statutory = calculate_statutory_deductions(
                basic_wage=basic_amt,
                monthly_gross=calculated_gross,
                month=month,
                year=year,
                state_code=state_code,
                gender=gender,
                annual_gross=projected_annual_gross,
                tds_deducted_so_far=prior_tds,
                months_remaining=months_remaining_in_fy(month),
                fy=fy,
                tax_regime=tax_regime,
                other_deductions=approved_investment_total,
            )
            default_esi = statutory["esi"]["employee"] if statutory["esi"]["applicable"] else 0.0
            default_pf = statutory["pf"]["employee_epf"]
            default_tax = statutory["professional_tax"] or 0.0
            default_tds = statutory["tds"] or 0.0
        except (PTNotConfigured, NotImplementedError, ValueError):
            default_esi = 0.0
            default_tax = 0.0
            default_pf = round(min(basic_amt, pf_wage_ceiling_for(date(year, month, last_day))) * 0.12, 2)

    return {
        "monthly_ctc": monthly_ctc,
        "calculated_gross": calculated_gross,
        "calculated_basic": basic_amt,
        "breakup_deductions": breakup_deductions,
        "total_month_days": total_month_days,
        "present_days": present_count,
        "approved_leave_days": approved_leave_days,
        "payable_days": payable_days,
        "lop_days": lop_days,
        "auto_lop_amount": auto_lop_amount,
        "ot_hours": ot_hours,
        "auto_ot_amount": auto_ot_amount,
        "default_pf": default_pf,
        "default_esi": default_esi,
        "default_tax": default_tax,
        "default_tds": default_tds,
        "approved_investment_total": approved_investment_total,
        "advance_repayment": round(advance_repayment, 2),
        "tax_regime": employee.profile.tax_regime if employee and employee.profile else "new_regime",
    }


def generate_payslip(user_id, month, year, generated_by_id, overwrite=True,
                     arrears=0.0, loss_of_pay=None, ot_hours=None, incentive=0.0,
                     pf_deduction=None, esi_deduction=None, professional_tax_deduction=None,
                     gratuity_provision=0.0, additional_deduction=0.0,
                     advance_repayment=0.0, use_statutory=True):
    """
    Create (or update) payslip with Attendance-based OT hours, Arrears, LOP, Incentives, and PF/Gratuity.
    """
    import calendar
    from datetime import date

    last_day = calendar.monthrange(year, month)[1]
    target_date = date(year, month, last_day)

    employee = User.query.get(user_id)
    if not employee:
        return None, "employee_not_found"
    if current_user.is_authenticated and current_user.role != "admin":
        if current_user.role != "demo_admin" or employee.tenant_id != current_user.tenant_id:
            return None, "unauthorized"

    ctc_record = get_active_ctc(user_id, as_of_date=target_date)
    if not ctc_record:
        return None, "no_ctc"

    lines, base_gross, base_deductions, base_net = compute_payslip_amounts(
        ctc_record.monthly_ctc, ctc_record
    )

    if loss_of_pay is None:
        _, _, _, _, _, loss_of_pay = _compute_attendance_days_and_lop(
            user_id, month, year, ctc_record.monthly_ctc
        )

    if ot_hours is None:
        start_m = date(year, month, 1)
        end_m = date(year, month, last_day)
        att_recs = Attendance.query.filter(
            Attendance.user_id == user_id,
            Attendance.date >= start_m,
            Attendance.date <= end_m
        ).all()
        ot_hours = sum(r.ot_hours or 0.0 for r in att_recs)

    basic_line = next((l for l in lines if l["name"].lower().startswith("basic")), None)
    basic_amt = basic_line["amount"] if basic_line else (ctc_record.monthly_ctc * 0.5)

    ot_hourly_rate = round((basic_amt / STANDARD_MONTHLY_WORKING_HOURS) * OT_RATE_MULTIPLIER, 2)
    ot_amount = round(ot_hours * ot_hourly_rate, 2)

    profile = None
    if employee:
        profile = employee.profile

    tax_regime = (profile.tax_regime if profile and profile.tax_regime in {"old_regime", "new_regime"}
                  else "new_regime")
    fy = fy_for(month, year)
    declarations = InvestmentDeclaration.query.filter_by(
        user_id=user_id, financial_year=fy, tax_regime=tax_regime,
        status="approved",
    ).all()
    approved_investment_total = approved_tax_deduction(declarations, tax_regime)

    final_lines = [dict(line) for line in lines if line["type"] == "earning"]
    if ot_amount > 0:
        final_lines.append({"name": f"Overtime ({ot_hours} hrs)", "type": "earning", "amount": round(ot_amount, 2)})
    if arrears > 0:
        final_lines.append({"name": "Arrears", "type": "earning", "amount": round(arrears, 2)})
    if incentive > 0:
        final_lines.append({"name": "Incentive / Bonus", "type": "earning", "amount": round(incentive, 2)})

    gross_pay = round(sum(line["amount"] for line in final_lines), 2)

    for line in lines:
        if line["type"] == "deduction" and not (
            use_statutory and (
                _is_statutory_component(line["name"])
                or line.get("statutory_code") in {"pf", "vpf", "esic", "professional_tax", "tds"}
            )
        ):
            final_lines.append(dict(line))

    esi_amt = pt_amt = tds_amt = lwf_amt = 0.0
    statutory_note = None
    pf_amt = pf_deduction
    if use_statutory:
        state_code = resolve_state_code(profile.current_state if profile else None)
        gender = {"male": "M", "female": "F"}.get(
            (profile.gender or "").strip().lower()
        ) if profile else None
        fy_start_key = (year if month >= 4 else year - 1) * 12 + 4
        current_key = year * 12 + month
        prior_tds = sum(
            payslip.tds_deduction or 0.0
            for payslip in Payslip.query.filter(Payslip.user_id == user_id).all()
            if fy_start_key <= payslip.year * 12 + payslip.month < current_key
        )
        prior_gross = sum(
            payslip.gross_pay or 0.0
            for payslip in Payslip.query.filter(Payslip.user_id == user_id).all()
            if fy_start_key <= payslip.year * 12 + payslip.month < current_key
        )
        try:
            statutory = calculate_statutory_deductions(
                basic_wage=basic_amt,
                monthly_gross=gross_pay,
                month=month,
                year=year,
                state_code=state_code,
                gender=gender,
                annual_gross=prior_gross + gross_pay * months_remaining_in_fy(month),
                tds_deducted_so_far=prior_tds,
                months_remaining=months_remaining_in_fy(month),
                fy=fy,
                tax_regime=tax_regime,
                other_deductions=approved_investment_total,
            )
            if pf_amt is None:
                pf_amt = statutory["pf"]["employee_epf"]
            if esi_deduction is None:
                esi_amt = statutory["esi"]["employee"] if statutory["esi"]["applicable"] else 0.0
            else:
                esi_amt = float(esi_deduction)
            if professional_tax_deduction is None:
                pt_amt = statutory["professional_tax"] or 0.0
            else:
                pt_amt = float(professional_tax_deduction)
            tds_amt = statutory["tds"] or 0.0
            lwf_amt = statutory.get("lwf_employee") or 0.0
            if statutory.get("pt_error"):
                statutory_note = f"Professional Tax skipped: {statutory['pt_error']}"
            elif state_code is None:
                statutory_note = "Professional Tax skipped: employee state not on file."
            if statutory.get("lwf_error"):
                lwf_note = f"LWF skipped: {statutory['lwf_error']}"
                statutory_note = f"{statutory_note}; {lwf_note}" if statutory_note else lwf_note
        except (PTNotConfigured, NotImplementedError, ValueError) as exc:
            statutory_note = f"Statutory calculation incomplete: {exc}"[:300]
            if pf_amt is None:
                pf_amt = round(min(basic_amt, pf_wage_ceiling_for(target_date)) * 0.12, 2)
            if esi_deduction is not None:
                esi_amt = float(esi_deduction)
            if professional_tax_deduction is not None:
                pt_amt = float(professional_tax_deduction)
    else:
        if esi_deduction is not None:
            esi_amt = float(esi_deduction)
        if professional_tax_deduction is not None:
            pt_amt = float(professional_tax_deduction)

    if pf_amt is None:
        pf_amt = 0.0
    if loss_of_pay > 0:
        final_lines.append({"name": "Loss of Pay (LOP)", "type": "deduction", "amount": round(loss_of_pay, 2)})
    if pf_amt > 0:
        final_lines.append({"name": "Provident Fund (EPF)", "type": "deduction", "amount": round(pf_amt, 2)})
    if esi_amt > 0:
        final_lines.append({"name": "ESI (Employee)", "type": "deduction", "amount": round(esi_amt, 2)})
    if pt_amt > 0:
        final_lines.append({"name": "Professional Tax", "type": "deduction", "amount": round(pt_amt, 2)})
    if tds_amt > 0:
        final_lines.append({"name": "TDS (Income Tax)", "type": "deduction", "amount": round(tds_amt, 2)})
    if lwf_amt > 0:
        final_lines.append({"name": "Labour Welfare Fund (LWF)", "type": "deduction", "amount": round(lwf_amt, 2)})
    if gratuity_provision > 0:
        final_lines.append({"name": "Gratuity Provision", "type": "employer_cost", "amount": round(gratuity_provision, 2)})
    if additional_deduction > 0:
        final_lines.append({"name": "Miscellaneous Deduction", "type": "deduction", "amount": round(additional_deduction, 2)})
    if advance_repayment > 0:
        active_advance = SalaryAdvance.query.filter_by(user_id=user_id, status="active").order_by(SalaryAdvance.id).first()
        if active_advance:
            advance_repayment = min(float(advance_repayment), active_advance.outstanding_amount)
        final_lines.append({"name": "Salary Advance Recovery", "type": "deduction", "amount": round(advance_repayment, 2)})

    unique_lines = []
    seen_lines = set()
    for line in final_lines:
        line_key = (
            line["name"].strip().lower(),
            line["type"],
        )
        if line_key not in seen_lines:
            seen_lines.add(line_key)
            unique_lines.append(line)
    final_lines = unique_lines

    miscellaneous_deduction = round(sum(
        line["amount"] for line in final_lines
        if line["type"] == "deduction"
        and line["name"] not in {"Miscellaneous Deduction", "Salary Advance Recovery"}
        and not (
            _is_statutory_component(line["name"])
            or line.get("statutory_code") in {"pf", "vpf", "esic", "professional_tax", "tds"}
        )
    ), 2)
    net_pay, total_deductions = calculate_net_pay(
        gross_pay, loss_of_pay, pf_amt, esi_amt, tds_amt, pt_amt, lwf_amt,
        miscellaneous_deduction, advance_repayment,
    )

    existing = Payslip.query.filter_by(user_id=user_id, month=month, year=year).first()

    if existing:
        if not overwrite:
            return existing, "already_exists"

        PayslipLine.query.filter_by(payslip_id=existing.id).delete(synchronize_session=False)
        db.session.commit()
        db.session.expire(existing, ["lines"])
        existing.monthly_ctc_used = ctc_record.monthly_ctc
        existing.gross_pay = gross_pay
        existing.total_deductions = total_deductions
        existing.net_pay = net_pay
        existing.arrears = arrears
        existing.loss_of_pay = loss_of_pay
        existing.ot_hours = ot_hours
        existing.ot_amount = ot_amount
        existing.incentive = incentive
        existing.pf_deduction = round(pf_amt, 2)
        existing.esi_deduction = round(esi_amt, 2)
        existing.professional_tax = round(pt_amt, 2)
        existing.tds_deduction = round(tds_amt, 2)
        existing.lwf_deduction = round(lwf_amt, 2)
        existing.tax_regime = tax_regime
        existing.statutory_note = statutory_note
        existing.tenant_id = employee.tenant_id
        existing.gratuity_provision = gratuity_provision
        existing.generated_by_id = generated_by_id
        payslip = existing
        status = "updated"
    else:
        payslip = Payslip(
            user_id=user_id, month=month, year=year,
            monthly_ctc_used=ctc_record.monthly_ctc,
            gross_pay=gross_pay, total_deductions=total_deductions, net_pay=net_pay,
            arrears=arrears, loss_of_pay=loss_of_pay,
            ot_hours=ot_hours, ot_amount=ot_amount, incentive=incentive,
            pf_deduction=round(pf_amt, 2),
            esi_deduction=round(esi_amt, 2),
            professional_tax=round(pt_amt, 2),
            tds_deduction=round(tds_amt, 2),
            lwf_deduction=round(lwf_amt, 2),
            tax_regime=tax_regime,
            statutory_note=statutory_note,
            gratuity_provision=gratuity_provision,
            generated_by_id=generated_by_id,
            tenant_id=employee.tenant_id,
        )
        db.session.add(payslip)
        db.session.flush()
        status = "created"

    for fl in final_lines:
        db.session.add(PayslipLine(
            payslip_id=payslip.id,
            component_name=fl["name"],
            component_type=fl["type"],
            amount=fl["amount"]
        ))

    db.session.commit()

    if additional_deduction > 0:
        adjustment = EmployeePayrollAdjustment.query.filter_by(
            user_id=user_id, month=month, year=year, category="miscellaneous_deduction"
        ).first()
        if adjustment:
            adjustment.amount = round(additional_deduction, 2)
        else:
            db.session.add(EmployeePayrollAdjustment(
                user_id=user_id, tenant_id=employee.tenant_id, month=month, year=year,
                category="miscellaneous_deduction", description="Payroll input deduction",
                amount=round(additional_deduction, 2), created_by_id=generated_by_id,
            ))
        db.session.commit()
    if advance_repayment > 0 and status == "created":
        advance = SalaryAdvance.query.filter_by(user_id=user_id, status="active").order_by(SalaryAdvance.id).first()
        if advance:
            advance.outstanding_amount = round(max(0.0, advance.outstanding_amount - advance_repayment), 2)
            if advance.outstanding_amount == 0:
                advance.status = "settled"
            db.session.commit()
    return payslip, status



def validate_components_for_generation():
    """
    Sanity checks before letting admin generate payslips at all - returns a
    list of human-readable problems, empty list if everything looks fine.
    """
    problems = []
    active = PayComponent.query.filter_by(is_active=True).all()

    if not active:
        problems.append("No active pay components exist yet. Add at least a Basic component.")
        return problems

    basics = [c for c in active if c.is_basic]
    if len(basics) == 0:
        problems.append('No component is marked as "Basic". Percent-of-Basic components need one.')
    elif len(basics) > 1:
        problems.append('More than one component is marked as "Basic" - only one is allowed.')

    needs_basic = [c for c in active if c.calc_basis in ["percent_of_basic", "percent_basic"]]
    if needs_basic and not basics:
        names = ", ".join(c.name for c in needs_basic)
        problems.append(f'{names} use "% of Basic" but no Basic component is marked.')

    return problems


def seed_default_pay_components():
    """Seed standard Saarthi EV pay components if none exist."""
    if PayComponent.query.first():
        return

    defaults = [
        {"name": "Basic Salary", "component_type": "earning", "calc_basis": "percent_of_ctc", "calc_value": 50.0, "calc_order": 1, "is_basic": True},
        {"name": "House Rent Allowance (HRA)", "component_type": "earning", "calc_basis": "percent_of_basic", "calc_value": 50.0, "calc_order": 2, "is_basic": False},
        {"name": "Special Allowance", "component_type": "earning", "calc_basis": "percent_of_ctc", "calc_value": 25.0, "calc_order": 3, "is_basic": False},
    ]

    for d in defaults:
        comp = PayComponent(**d)
        db.session.add(comp)
    db.session.commit()



def num_to_words(n):
    """Converts a number to Indian currency words format."""
    units = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
             "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def convert_less_than_thousand(num):
        if num == 0:
            return ""
        elif num < 20:
            return units[num] + " "
        elif num < 100:
            return tens[num // 10] + " " + convert_less_than_thousand(num % 10)
        else:
            return units[num // 100] + " Hundred " + convert_less_than_thousand(num % 100)

    n = int(round(n))
    if n == 0:
        return "Zero Rupees Only"

    res = ""
    if n >= 10000000:
        res += convert_less_than_thousand(n // 10000000) + "Crore "
        n %= 10000000
    if n >= 100000:
        res += convert_less_than_thousand(n // 100000) + "Lakh "
        n %= 100000
    if n >= 1000:
        res += convert_less_than_thousand(n // 1000) + "Thousand "
        n %= 1000
    if n > 0:
        res += convert_less_than_thousand(n)

    return res.strip() + " Rupees Only"


def generate_payslip_pdf(payslip):
    """
    Generate a PDF document for a given payslip using ReportLab and return BytesIO buffer.
    """
    from io import BytesIO
    import calendar
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    story = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'CompanyTitle',
        parent=styles['Heading1'],
        fontSize=20,
        leading=24,
        textColor=colors.HexColor('#1e293b'),
        alignment=1
    )
    subtitle_style = ParagraphStyle(
        'PayslipSubTitle',
        parent=styles['Normal'],
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#475569'),
        alignment=1
    )
    cell_bold = ParagraphStyle(
        'CellBold',
        parent=styles['Normal'],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#0f172a'),
        fontName='Helvetica-Bold'
    )
    cell_normal = ParagraphStyle(
        'CellNormal',
        parent=styles['Normal'],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#334155')
    )

    story.append(Paragraph("<b>HRMS ENTERPRISE SOLUTIONS</b>", title_style))
    month_name = calendar.month_name[payslip.month]
    story.append(Paragraph(f"<b>PAYSLIP FOR {month_name.upper()} {payslip.year}</b>", subtitle_style))
    story.append(Spacer(1, 15))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#2563eb'), spaceAfter=15))

    u = payslip.user
    p = u.profile if u else None

    emp_code = u.employee_code if u else "-"
    emp_name = p.full_name if p else "-"
    designation = p.designation if p and p.designation else "-"
    department = p.department if p and p.department else "-"
    doj = p.date_of_joining.strftime('%d-%b-%Y') if p and p.date_of_joining else "-"
    pan = mask_sensitive(p.pan_number) if p and p.pan_number else "-"
    bank_acc = mask_sensitive(p.bank_account_number) if p and p.bank_account_number else "-"
    ifsc = p.bank_ifsc_code if p and p.bank_ifsc_code else "-"

    emp_data = [
        [
            Paragraph("Employee ID:", cell_bold), Paragraph(emp_code, cell_normal),
            Paragraph("Employee Name:", cell_bold), Paragraph(emp_name, cell_normal)
        ],
        [
            Paragraph("Designation:", cell_bold), Paragraph(designation, cell_normal),
            Paragraph("Department:", cell_bold), Paragraph(department, cell_normal)
        ],
        [
            Paragraph("Date of Joining:", cell_bold), Paragraph(doj, cell_normal),
            Paragraph("PAN Number:", cell_bold), Paragraph(pan, cell_normal)
        ],
        [
            Paragraph("Bank Account:", cell_bold), Paragraph(bank_acc, cell_normal),
            Paragraph("IFSC Code:", cell_bold), Paragraph(ifsc, cell_normal)
        ]
    ]

    emp_table = Table(emp_data, colWidths=[110, 150, 110, 150])
    emp_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('PADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))

    story.append(emp_table)
    story.append(Spacer(1, 20))

    earnings = [line for line in payslip.lines if line.component_type == 'earning']
    deductions = [line for line in payslip.lines if line.component_type == 'deduction']

    max_rows = max(len(earnings), len(deductions))

    pay_table_data = [
        [
            Paragraph("<b>EARNINGS</b>", cell_bold), Paragraph("<b>AMOUNT (Rs.)</b>", cell_bold),
            Paragraph("<b>DEDUCTIONS</b>", cell_bold), Paragraph("<b>AMOUNT (Rs.)</b>", cell_bold)
        ]
    ]

    for i in range(max_rows):
        e_name = earnings[i].component_name if i < len(earnings) else ""
        e_amt = f"Rs. {earnings[i].amount:,.2f}" if i < len(earnings) else ""

        d_name = deductions[i].component_name if i < len(deductions) else ""
        d_amt = f"Rs. {deductions[i].amount:,.2f}" if i < len(deductions) else ""

        pay_table_data.append([
            Paragraph(e_name, cell_normal), Paragraph(e_amt, cell_normal),
            Paragraph(d_name, cell_normal), Paragraph(d_amt, cell_normal)
        ])

    pay_table_data.append([
        Paragraph("<b>Total Gross Earnings</b>", cell_bold),
        Paragraph(f"<b>Rs. {payslip.gross_pay:,.2f}</b>", cell_bold),
        Paragraph("<b>Total Deductions</b>", cell_bold),
        Paragraph(f"<b>Rs. {payslip.total_deductions:,.2f}</b>", cell_bold)
    ])

    pay_table = Table(pay_table_data, colWidths=[170, 90, 170, 90])
    pay_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (1, 0), colors.HexColor('#e0f2fe')),
        ('BACKGROUND', (2, 0), (3, 0), colors.HexColor('#fee2e2')),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f1f5f9')),
        ('PADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))

    story.append(pay_table)
    story.append(Spacer(1, 15))

    net_words = num_to_words(payslip.net_pay)
    net_data = [
        [
            Paragraph("<b>NET PAYABLE AMOUNT:</b>", cell_bold),
            Paragraph(f"<font size=14 color='#166534'><b>Rs. {payslip.net_pay:,.2f}</b></font>", cell_bold)
        ],
        [
            Paragraph("<b>Amount in Words:</b>", cell_bold),
            Paragraph(f"<i>{net_words}</i>", cell_normal)
        ]
    ]

    net_table = Table(net_data, colWidths=[150, 370])
    net_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f0fdf4')),
        ('PADDING', (0, 0), (-1, -1), 8),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#86efac')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))

    story.append(net_table)
    story.append(Spacer(1, 30))

    footer_data = [
        [
            Paragraph("<i>This is a computer-generated document and does not require a physical signature.</i>", cell_normal),
            Paragraph("<b>Authorized Signatory</b><br/>HR & Payroll Department", ParagraphStyle('RightText', parent=cell_normal, alignment=2))
        ]
    ]
    footer_table = Table(footer_data, colWidths=[320, 200])
    footer_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
    ]))

    story.append(footer_table)

    doc.build(story)
    buffer.seek(0)
    return buffer

