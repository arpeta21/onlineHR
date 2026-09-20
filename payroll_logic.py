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

from models import db, PayComponent, EmployeeCTC, Payslip, PayslipLine, Attendance


def get_active_ctc(user_id, as_of_date=None):
    """The CTC record in effect as of a given date (defaults to today). Fallback to most recent CTC."""
    from datetime import date
    as_of_date = as_of_date or date.today()
    record = EmployeeCTC.query.filter(
        EmployeeCTC.user_id == user_id,
        EmployeeCTC.effective_from <= as_of_date
    ).order_by(EmployeeCTC.effective_from.desc()).first()

    if not record:
        record = EmployeeCTC.query.filter(
            EmployeeCTC.user_id == user_id
        ).order_by(EmployeeCTC.effective_from.desc()).first()

    return record


def compute_payslip_amounts(monthly_ctc):
    """
    Run every active PayComponent against a given monthly CTC and return
    (lines, gross_pay, total_deductions, net_pay) where lines is a list of
    dicts: {name, type, amount}. Does NOT touch the database - pure
    calculation, so it can be used for both real generation and a
    "preview" before committing.
    """
    components = PayComponent.query.filter_by(is_active=True).order_by(
        PayComponent.calc_order, PayComponent.id
    ).all()

    basic_amount = 0.0
    earning_total_so_far = 0.0
    lines = []

    deferred_gross_based = []

    for comp in components:
        if comp.calc_basis == "flat":
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
        if comp.is_basic:
            basic_amount = amount

        lines.append({"name": comp.name, "type": comp.component_type, "amount": amount})
        if comp.component_type == "earning":
            earning_total_so_far += amount

    for comp in deferred_gross_based:
        amount = round(earning_total_so_far * (comp.calc_value / 100), 2)
        lines.append({"name": comp.name, "type": comp.component_type, "amount": amount})
        if comp.component_type == "earning":
            earning_total_so_far += amount

    gross_pay = round(sum(l["amount"] for l in lines if l["type"] == "earning"), 2)
    total_deductions = round(sum(l["amount"] for l in lines if l["type"] == "deduction"), 2)
    net_pay = round(gross_pay - total_deductions, 2)

    return lines, gross_pay, total_deductions, net_pay


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
    from models import Attendance, LeaveApplication, EmployeeCTC

    last_day = calendar.monthrange(year, month)[1]
    total_month_days = last_day
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    ctc_rec = get_active_ctc(user_id, as_of_date=end_date)
    monthly_ctc = ctc_rec.monthly_ctc if ctc_rec else 0.0

    att_recs = Attendance.query.filter(
        Attendance.user_id == user_id,
        Attendance.date >= start_date,
        Attendance.date <= end_date
    ).all()

    present_count = 0.0
    ot_hours = 0.0
    recorded_dates = set()

    for r in att_recs:
        recorded_dates.add(r.date)
        ot_hours += (r.ot_hours or 0.0)
        if r.status in ["Present", "Late"]:
            present_count += 1.0
        elif r.status == "Half Day":
            present_count += 0.5

    approved_leaves = LeaveApplication.query.filter(
        LeaveApplication.user_id == user_id,
        LeaveApplication.status == "approved",
        LeaveApplication.start_date <= end_date,
        LeaveApplication.end_date >= start_date
    ).all()

    approved_leave_days = sum(l.days for l in approved_leaves)

    sunday_count = 0
    for day_num in range(1, total_month_days + 1):
        cur_d = date(year, month, day_num)
        if cur_d.weekday() == 6:
            if cur_d not in recorded_dates or any(r.status != "Absent" for r in att_recs if r.date == cur_d):
                sunday_count += 1

    if not att_recs:
        payable_days = float(total_month_days)
        lop_days = 0.0
        auto_lop_amount = 0.0
    else:
        payable_days = min(float(total_month_days), present_count + approved_leave_days + sunday_count)
        lop_days = max(0.0, float(total_month_days) - payable_days)
        auto_lop_amount = round((monthly_ctc / float(total_month_days)) * lop_days, 2)

    lines, _, _, _ = compute_payslip_amounts(monthly_ctc)
    basic_line = next((l for l in lines if l["name"].lower().startswith("basic")), None)
    basic_amt = basic_line["amount"] if basic_line else (monthly_ctc * 0.5)
    ot_hourly_rate = round((basic_amt / 208.0) * 1.5, 2)
    auto_ot_amount = round(ot_hours * ot_hourly_rate, 2)

    pf_line = next((l for l in lines if "provident" in l["name"].lower() or "pf" in l["name"].lower()), None)
    default_pf = pf_line["amount"] if pf_line else round(basic_amt * 0.12, 2)

    return {
        "monthly_ctc": monthly_ctc,
        "total_month_days": total_month_days,
        "present_days": present_count,
        "approved_leave_days": approved_leave_days,
        "payable_days": payable_days,
        "lop_days": lop_days,
        "auto_lop_amount": auto_lop_amount,
        "ot_hours": ot_hours,
        "auto_ot_amount": auto_ot_amount,
        "default_pf": default_pf
    }


def generate_payslip(user_id, month, year, generated_by_id, overwrite=True,
                     arrears=0.0, loss_of_pay=0.0, ot_hours=None, incentive=0.0,
                     pf_deduction=None, gratuity_provision=0.0):
    """
    Create (or update) payslip with Attendance-based OT hours, Arrears, LOP, Incentives, and PF/Gratuity.
    """
    import calendar
    from datetime import date

    last_day = calendar.monthrange(year, month)[1]
    target_date = date(year, month, last_day)

    ctc_record = get_active_ctc(user_id, as_of_date=target_date)
    if not ctc_record:
        return None, "no_ctc"

    lines, base_gross, base_deductions, base_net = compute_payslip_amounts(ctc_record.monthly_ctc)

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

    ot_hourly_rate = round((basic_amt / 208.0) * 1.5, 2)
    ot_amount = round(ot_hours * ot_hourly_rate, 2)

    final_lines = []
    for l in lines:
        if ("provident" in l["name"].lower() or "pf" in l["name"].lower()) and pf_deduction is not None:
            final_lines.append({"name": l["name"], "type": "deduction", "amount": round(pf_deduction, 2)})
        else:
            final_lines.append({"name": l["name"], "type": l["type"], "amount": l["amount"]})

    if ot_amount > 0:
        final_lines.append({"name": f"Overtime ({ot_hours} hrs)", "type": "earning", "amount": round(ot_amount, 2)})
    if arrears > 0:
        final_lines.append({"name": "Arrears", "type": "earning", "amount": round(arrears, 2)})
    if incentive > 0:
        final_lines.append({"name": "Incentive / Bonus", "type": "earning", "amount": round(incentive, 2)})
    if loss_of_pay > 0:
        final_lines.append({"name": "Loss of Pay (LOP)", "type": "deduction", "amount": round(loss_of_pay, 2)})
    if gratuity_provision > 0:
        final_lines.append({"name": "Gratuity Provision", "type": "deduction", "amount": round(gratuity_provision, 2)})

    gross_pay = round(sum(l["amount"] for l in final_lines if l["type"] == "earning"), 2)
    total_deductions = round(sum(l["amount"] for l in final_lines if l["type"] == "deduction"), 2)
    net_pay = round(gross_pay - total_deductions, 2)

    existing = Payslip.query.filter_by(user_id=user_id, month=month, year=year).first()

    if existing:
        if not overwrite:
            return existing, "already_exists"

        PayslipLine.query.filter_by(payslip_id=existing.id).delete()
        existing.monthly_ctc_used = ctc_record.monthly_ctc
        existing.gross_pay = gross_pay
        existing.total_deductions = total_deductions
        existing.net_pay = net_pay
        existing.arrears = arrears
        existing.loss_of_pay = loss_of_pay
        existing.ot_hours = ot_hours
        existing.ot_amount = ot_amount
        existing.incentive = incentive
        existing.pf_deduction = pf_deduction if pf_deduction is not None else 0.0
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
            pf_deduction=pf_deduction if pf_deduction is not None else 0.0,
            gratuity_provision=gratuity_provision,
            generated_by_id=generated_by_id,
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
        {"name": "Provident Fund (PF)", "component_type": "deduction", "calc_basis": "percent_of_basic", "calc_value": 12.0, "calc_order": 4, "is_basic": False},
        {"name": "Professional Tax (P Tax)", "component_type": "deduction", "calc_basis": "flat", "calc_value": 200.0, "calc_order": 5, "is_basic": False},
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
    pan = p.pan_number if p and p.pan_number else "-"
    bank_acc = p.bank_account_number if p and p.bank_account_number else "-"
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

