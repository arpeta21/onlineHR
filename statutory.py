"""Indian statutory payroll calculations for the configured financial year.

Rates are deliberately data-driven and must be reviewed by a payroll
professional before production use.
"""
import math

from datetime import date

DEFAULT_FY = "2026-27"
RATES = {
    "2026-27": {
        "pf": {"employee_rate": 0.12, "eps_rate": 0.0833, "wage_ceiling": 25000,
            "edli_rate": 0.005, "edli_wage_cap": 25000, "admin_rate": 0.005},
        "esi": {"wage_threshold": 21000, "wage_threshold_disabled": 25000,
                 "employee_rate": 0.0075, "employer_rate": 0.0325},
        "tds_new_regime": {"standard_deduction": 75000, "rebate_taxable_limit": 1200000,
                           "rebate_max": 60000, "cess": 0.04,
                           "surcharge_free_limit": 5000000,
                           "slabs": [(400000, 0.00), (800000, 0.05), (1200000, 0.10),
                                     (1600000, 0.15), (2000000, 0.20), (2400000, 0.25),
                                     (None, 0.30)]},
                        "tds_old_regime": {"standard_deduction": 50000, "rebate_taxable_limit": 500000,
                                   "rebate_max": 12500, "cess": 0.04,
                                   "slabs": [(250000, 0.00), (500000, 0.05), (1000000, 0.20),
                                         (None, 0.30)]},
    }
}
PT_NOT_LEVIED = {"DL", "HR", "UP", "RJ"}
STATE_CODE_BY_NAME = {
    "maharashtra": "MH", "karnataka": "KA", "gujarat": "GJ",
    "telangana": "TG", "andhra pradesh": "AP", "west bengal": "WB",
    "delhi": "DL", "nct of delhi": "DL", "new delhi": "DL",
    "haryana": "HR", "uttar pradesh": "UP", "rajasthan": "RJ",
}
PT_STATES = {
    "MH": {"by_gender": True, "feb_top": 300, "slabs": {
        "M": [(7500, 0), (10000, 175), (None, 200)],
        "F": [(25000, 0), (None, 200)]}},
    "KA": {"feb_top": 300, "slabs": [(24999, 0), (None, 200)]},
    "GJ": {"feb_top": None, "slabs": [(5999, 0), (8999, 80), (11999, 150), (None, 200)]},
    "TG": {"feb_top": None, "slabs": [(15000, 0), (20000, 150), (None, 200)]},
    "AP": {"feb_top": None, "slabs": [(15000, 0), (20000, 150), (None, 200)]},
    "WB": {"feb_top": None, "slabs": [(10000, 0), (15000, 110), (25000, 130), (40000, 150), (None, 200)]},
}

# Every state/UT an employee's `current_state` might realistically contain,
# used only to proactively flag ones PT_STATES/PT_NOT_LEVIED doesn't cover
# yet - never used for the actual PT calculation itself. Keeping this list
# separate from STATE_CODE_BY_NAME means adding a new state's PT slabs
# later (in PT_STATES) automatically clears its compliance warning below,
# with nothing else to update.
ALL_INDIAN_STATES_AND_UTS = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya",
    "mizoram", "nagaland", "odisha", "punjab", "rajasthan", "sikkim",
    "tamil nadu", "telangana", "tripura", "uttar pradesh", "uttarakhand",
    "west bengal", "andaman and nicobar islands", "chandigarh",
    "dadra and nagar haveli and daman and diu", "delhi", "jammu and kashmir",
    "ladakh", "lakshadweep", "puducherry",
}

OLD_REGIME_SECTION_CAPS = {
    "80C": 150000.0,
    "80CCC": 150000.0,
    "80CCD(1)": 150000.0,
    "80CCD(1B)": 50000.0,
    "80D": 25000.0,
}

GRATUITY_MIN_YEARS_OF_SERVICE = 5
GRATUITY_RATE_DAYS_PER_YEAR = 15
GRATUITY_MONTH_DIVISOR = 26
GRATUITY_EXEMPTION_LIMIT = 2000000.0


def completed_years_of_service(date_of_joining, exit_date):
    """Return completed service years, rounding a part-year over six months up."""
    if not date_of_joining or exit_date <= date_of_joining:
        return 0
    years = exit_date.year - date_of_joining.year
    anniversary = date_of_joining.replace(year=exit_date.year)
    if exit_date < anniversary:
        years -= 1
    last_anniversary = date_of_joining.replace(year=date_of_joining.year + years)
    months_since = (exit_date.year - last_anniversary.year) * 12 + exit_date.month - last_anniversary.month
    if exit_date.day < last_anniversary.day:
        months_since -= 1
    if months_since > 6:
        years += 1
    return max(0, years)


def calculate_gratuity(date_of_joining, exit_date, last_drawn_basic_da,
                       is_death_or_disablement=False):
    """Calculate gratuity eligibility, amount, exemption, and taxable amount."""
    years = completed_years_of_service(date_of_joining, exit_date)
    is_eligible = is_death_or_disablement or years >= GRATUITY_MIN_YEARS_OF_SERVICE
    reason = None if is_eligible else (
        f"Only {years} completed year(s) of service "
        f"({GRATUITY_MIN_YEARS_OF_SERVICE} required, unless exit is due to "
        "death or disablement - mark that manually if applicable)."
    )
    gratuity_amount = round(
        GRATUITY_RATE_DAYS_PER_YEAR * max(0.0, last_drawn_basic_da or 0.0) * years
        / GRATUITY_MONTH_DIVISOR, 2
    ) if is_eligible else 0.0
    tax_exempt_amount = round(min(gratuity_amount, GRATUITY_EXEMPTION_LIMIT), 2)
    return {
        "completed_years": years,
        "is_eligible": is_eligible,
        "ineligibility_reason": reason,
        "gratuity_amount": gratuity_amount,
        "tax_exempt_amount": tax_exempt_amount,
        "taxable_amount": round(max(0.0, gratuity_amount - tax_exempt_amount), 2),
    }


class PTNotConfigured(ValueError):
    pass


class LWFNotConfigured(ValueError):
    pass


LWF_STATES = {
    "MH": {"employee": 25.0, "employer": 75.0, "due_months": [6, 12]},
    "GJ": {"employee": 6.0, "employer": 12.0, "due_months": [6, 12], "wage_ceiling": 15000},
    "TG": {"employee": 2.0, "employer": 5.0, "due_months": [12]},
    "AP": {"employee": 30.0, "employer": 70.0, "due_months": [12]},
    "KA": {"employee": 50.0, "employer": 100.0, "due_months": [12]},
    "WB": {"employee": 3.0, "employer": 30.0, "due_months": [6, 12]},
}
LWF_NOT_LEVIED = {"UP", "RJ", "BR", "JH", "AS"}


def labour_welfare_fund(state_code, month, wage=None):
    code = (state_code or "").upper()
    if code in LWF_NOT_LEVIED:
        return 0.0, 0.0
    state = LWF_STATES.get(code)
    if not state:
        raise LWFNotConfigured(f"Labour Welfare Fund rates for '{state_code}' are not configured.")
    if month not in state["due_months"]:
        return 0.0, 0.0
    if wage is not None and state.get("wage_ceiling") and wage > state["wage_ceiling"]:
        return 0.0, 0.0
    return state["employee"], state["employer"]


def round_half_up(value, ndigits=0):
    factor = 10 ** ndigits
    return math.floor(value * factor + 0.5) / factor


def _rates(fy):
    if fy not in RATES:
        raise ValueError(f"No statutory rates configured for FY {fy}.")
    return RATES[fy]


PF_WAGE_CEILING_CHANGE_DATE = date(2026, 9, 17)
PF_WAGE_CEILING_BEFORE = 15000
PF_WAGE_CEILING_ON_OR_AFTER = 25000


def pf_wage_ceiling_for(as_of_date=None):
    as_of_date = as_of_date or date.today()
    return PF_WAGE_CEILING_ON_OR_AFTER if as_of_date >= PF_WAGE_CEILING_CHANGE_DATE else PF_WAGE_CEILING_BEFORE


def pf_contribution(pf_wage, fy=DEFAULT_FY, restrict_to_ceiling=True, as_of_date=None):
    cfg = _rates(fy)["pf"]
    if pf_wage < 0:
        raise ValueError("pf_wage cannot be negative")
    ceiling = pf_wage_ceiling_for(as_of_date)
    contribution_wage = min(pf_wage, ceiling) if restrict_to_ceiling else pf_wage
    employee_epf = round_half_up(contribution_wage * cfg["employee_rate"])
    employer_eps = round_half_up(min(pf_wage, ceiling) * cfg["eps_rate"])
    employer_total = employee_epf
    return {"contribution_wage": contribution_wage, "employee_epf": employee_epf,
            "employer_eps": employer_eps, "employer_epf": employer_total - employer_eps,
            "employer_total": employer_total,
            "employer_edli": round_half_up(min(pf_wage, ceiling) * cfg["edli_rate"]),
            "employer_admin": round_half_up(min(pf_wage, ceiling) * cfg["admin_rate"])}


def esi_contribution(gross_wages, fy=DEFAULT_FY, disabled=False):
    cfg = _rates(fy)["esi"]
    limit = cfg["wage_threshold_disabled"] if disabled else cfg["wage_threshold"]
    if gross_wages > limit:
        return {"applicable": False, "employee": 0, "employer": 0}
    return {"applicable": True,
            "employee": math.ceil(gross_wages * cfg["employee_rate"]),
            "employer": math.ceil(gross_wages * cfg["employer_rate"])}


def professional_tax(state_code, monthly_gross, month, gender=None):
    code = (state_code or "").upper()
    if code in PT_NOT_LEVIED:
        return 0
    state = PT_STATES.get(code)
    if not state:
        raise PTNotConfigured(f"Professional Tax slabs for '{state_code}' are not configured.")
    slabs = state["slabs"]
    if state.get("by_gender"):
        if gender not in {"M", "F"}:
            raise ValueError("Maharashtra PT needs gender 'M' or 'F'.")
        slabs = slabs[gender]
    slab_index, amount = next(
        (index, slab_amount)
        for index, (upper, slab_amount) in enumerate(slabs)
        if upper is None or monthly_gross <= upper
    )
    is_top_slab = slabs[slab_index][0] is None
    if amount and month == 2 and is_top_slab and state.get("feb_top"):
        amount = state["feb_top"]
    return amount


def resolve_state_code(value):
    """Map a free-text state name or two-letter code to a PT state code."""
    if not value:
        return None
    text = str(value).strip()
    upper = text.upper()
    if upper in PT_NOT_LEVIED or upper in PT_STATES:
        return upper
    return STATE_CODE_BY_NAME.get(text.lower())


def fy_for(month, year):
    start = year if month >= 4 else year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def months_remaining_in_fy(month):
    return 12 - ((month - 4) % 12)


def approved_tax_deduction(declarations, tax_regime):
    """Return approved deductions eligible for the selected Indian regime."""
    if tax_regime != "old_regime":
        return 0.0
    total = 0.0
    for declaration in declarations:
        if declaration.status != "approved":
            continue
        amount = max(0.0, declaration.approved_amount or 0.0)
        cap = OLD_REGIME_SECTION_CAPS.get(declaration.section.upper())
        total += min(amount, cap) if cap is not None else amount
    return round(total, 2)


def _slab_tax(taxable, slabs):
    tax = 0.0
    lower = 0.0
    for upper, rate in slabs:
        top = taxable if upper is None else min(taxable, upper)
        if top > lower:
            tax += (top - lower) * rate
        if upper is None or taxable <= upper:
            break
        lower = upper
    return tax


SURCHARGE_BANDS_NEW_REGIME = [(5000000, 0.0), (10000000, 0.10), (20000000, 0.15), (None, 0.25)]
SURCHARGE_BANDS_OLD_REGIME = [(5000000, 0.0), (10000000, 0.10), (20000000, 0.15), (50000000, 0.25), (None, 0.37)]


def apply_surcharge_with_marginal_relief(taxable, base_tax, slabs, tax_regime):
    bands = SURCHARGE_BANDS_OLD_REGIME if tax_regime == "old_regime" else SURCHARGE_BANDS_NEW_REGIME
    band_index = next((index for index, (upper, _) in enumerate(bands) if upper is None or taxable <= upper), 0)
    rate = bands[band_index][1]
    if rate == 0.0:
        return base_tax
    band_lower = bands[band_index - 1][0]
    rate_below = bands[band_index - 1][1]
    naive_total = base_tax * (1 + rate)
    tax_at_threshold = _slab_tax(band_lower, slabs)
    relief_cap = tax_at_threshold * (1 + rate_below) + (taxable - band_lower)
    return min(naive_total, relief_cap)


def annual_tax_new_regime(annual_gross_salary, fy=DEFAULT_FY, other_deductions=0.0):
    cfg = _rates(fy)["tds_new_regime"]
    taxable = max(0.0, annual_gross_salary - cfg["standard_deduction"] - other_deductions)
    tax = _slab_tax(taxable, cfg["slabs"])
    if taxable <= cfg["rebate_taxable_limit"]:
        tax -= min(tax, cfg["rebate_max"])
    else:
        tax = min(tax, taxable - cfg["rebate_taxable_limit"])
    tax = apply_surcharge_with_marginal_relief(taxable, tax, cfg["slabs"], "new_regime")
    return float(round_half_up(tax * (1 + cfg["cess"]) / 10) * 10)


def annual_tax_old_regime(annual_gross_salary, fy=DEFAULT_FY, other_deductions=0.0):
    cfg = _rates(fy)["tds_old_regime"]
    taxable = max(0.0, annual_gross_salary - cfg["standard_deduction"] - other_deductions)
    tax = _slab_tax(taxable, cfg["slabs"])
    if taxable <= cfg["rebate_taxable_limit"]:
        tax = max(0.0, tax - cfg["rebate_max"])
    tax = apply_surcharge_with_marginal_relief(taxable, tax, cfg["slabs"], "old_regime")
    return float(round_half_up(tax * (1 + cfg["cess"]) / 10) * 10)


def monthly_tds(projected_annual_gross, tds_deducted_so_far, months_remaining,
                fy=DEFAULT_FY, other_deductions=0.0, tax_regime="new_regime"):
    if months_remaining < 1:
        raise ValueError("months_remaining must be at least 1")
    calculator = annual_tax_old_regime if tax_regime == "old_regime" else annual_tax_new_regime
    annual = calculator(projected_annual_gross, fy, other_deductions)
    return round_half_up(max(0.0, annual - tds_deducted_so_far) / months_remaining)


# --------------------------------------------------------------------------
# Proactive compliance checks
# --------------------------------------------------------------------------
# Every function above already fails safely when data is missing (a bad PT
# state raises PTNotConfigured; an unconfigured FY raises ValueError) - but
# those only surface one employee, one payslip at a time, often caught by
# a broad except and quietly turned into a `statutory_note` on a single
# payslip. The two functions below are for a payroll admin to call BEFORE
# running a payroll cycle, so a stale rate table or a missing state's PT
# slabs are caught as one clear warning up front, not discovered payslip
# by payslip after the fact.

def list_configured_financial_years():
    """Every FY this file currently has PF/ESI/TDS rates for, sorted."""
    return sorted(RATES.keys())


def current_indian_fy(as_of_date=None):
    """The Indian FY (e.g. '2026-27') for a given date, defaulting to today."""
    from datetime import date as _date
    as_of_date = as_of_date or _date.today()
    return fy_for(as_of_date.month, as_of_date.year)


def check_rates_currency(as_of_date=None):
    """
    Warn if today's real-world Indian FY has no entry in RATES yet.
    PF wage ceilings, ESI thresholds and TDS slabs are all revised from
    time to time (most recently effective changes have landed with each
    Union Budget) - once the calendar rolls into an FY this file doesn't
    have rates for, PF/ESI/TDS all silently fall back to rough estimates
    or zero (see the broad except blocks in payroll_logic.py) rather than
    failing loudly. Calling this once per payroll cycle catches that
    before it happens.
    """
    fy = current_indian_fy(as_of_date)
    configured = fy in RATES
    return {
        "fy": fy,
        "configured": configured,
        "warning": None if configured else (
            f"No PF/ESI/TDS rates configured for FY {fy}. Statutory "
            "deductions will silently fall back to rough estimates or "
            "zero until statutory.RATES is updated for this FY - do this "
            "before running payroll."
        ),
    }


def list_unconfigured_pt_states():
    """
    States/UTs with no Professional Tax configuration at all (not in
    PT_STATES and not in PT_NOT_LEVIED). Any employee whose `current_state`
    resolves to one of these will hit PTNotConfigured the moment payroll
    tries to calculate their PT - this lets HR see the gap in advance,
    before it's discovered one employee at a time.
    """
    configured_names = {
        name for name, code in STATE_CODE_BY_NAME.items()
        if code in PT_STATES or code in PT_NOT_LEVIED
    }
    return sorted(ALL_INDIAN_STATES_AND_UTS - configured_names)


def list_unconfigured_lwf_states():
    """Return known LWF states that lack configured rates."""
    known_lwf_states = {
        "AP", "CH", "CG", "DL", "GA", "GJ", "HR", "KA", "KL", "MP",
        "MH", "OD", "PB", "TN", "TG", "WB",
    }
    return sorted(known_lwf_states - (set(LWF_STATES) | LWF_NOT_LEVIED))


def run_statutory_compliance_checks(employee_states=None, as_of_date=None):
    """
    One entry point an admin route/dashboard can call before generating a
    payroll cycle. Returns a list of human-readable warning strings, empty
    if nothing looks wrong - same contract as
    payroll_logic.validate_components_for_generation(), so both can be
    surfaced together.

    `employee_states` (optional): the actual `current_state` values on
    file for employees about to be paid this cycle (e.g.
    [e.profile.current_state for e in employees]). When given, only states
    that are BOTH unconfigured AND actually in use are flagged, instead of
    every unconfigured state in the country - keeps the warning list
    relevant to the employees who exist.
    """
    problems = []

    rates_check = check_rates_currency(as_of_date)
    if rates_check["warning"]:
        problems.append(rates_check["warning"])

    unconfigured = list_unconfigured_pt_states()
    if employee_states is not None:
        in_use_unresolved = sorted({
            (state or "").strip()
            for state in employee_states
            if state and resolve_state_code(state) is None
        })
        if in_use_unresolved:
            problems.append(
                "Professional Tax is not configured for these employee "
                "state(s) on file, so PT will be skipped for them: "
                + ", ".join(in_use_unresolved)
            )
    elif unconfigured:
        problems.append(
            f"Professional Tax is not configured for {len(unconfigured)} "
            "state(s)/UT(s) (e.g. " + ", ".join(unconfigured[:5]) +
            ("..." if len(unconfigured) > 5 else "") +
            "). Employees based there will have PT silently skipped."
        )

    unconfigured_lwf = list_unconfigured_lwf_states()
    if unconfigured_lwf:
        problems.append(
            f"Labour Welfare Fund is not configured for {len(unconfigured_lwf)} "
            "state(s)/UT(s) known to have an LWF Act (e.g. "
            + ", ".join(unconfigured_lwf[:5])
            + ("..." if len(unconfigured_lwf) > 5 else "")
            + "). Employees based there will have LWF skipped until rates are reviewed."
        )

    return problems
