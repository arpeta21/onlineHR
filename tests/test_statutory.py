import pytest

from payroll_logic import (
    calculate_statutory_deductions,
    build_automatic_ctc_lines,
    calculate_ctc_breakup_lines,
    calculate_net_pay,
)
from statutory import (
    PTNotConfigured,
    annual_tax_new_regime,
    esi_contribution,
    pf_contribution,
    professional_tax,
    resolve_state_code,
    approved_tax_deduction,
    monthly_tds,
)
from types import SimpleNamespace


def test_pf_ceiling_and_eps_split():
    result = pf_contribution(20000)
    assert result["contribution_wage"] == 15000
    assert result["employee_epf"] == 1800
    assert result["employer_eps"] == 1250
    assert result["employer_epf"] == 550


def test_esi_threshold_and_disabled_threshold():
    assert esi_contribution(21001)["applicable"] is False
    assert esi_contribution(25000, disabled=True)["applicable"] is True
    assert esi_contribution(25001, disabled=True)["applicable"] is False


def test_professional_tax_requires_configured_state_and_gender():
    assert professional_tax("MH", 20000, 1, "M") == 200
    assert professional_tax("DL", 100000, 1) == 0
    with pytest.raises(PTNotConfigured):
        professional_tax("TN", 30000, 1)
    with pytest.raises(ValueError):
        professional_tax("MH", 30000, 1)


def test_mh_middle_slab_has_no_february_topup():
    assert professional_tax("MH", 9000, 2, gender="M") == 175
    assert professional_tax("MH", 9000, 1, gender="M") == 175


def test_mh_top_slab_february_topup():
    assert professional_tax("MH", 30000, 2, gender="M") == 300
    assert sum(professional_tax("MH", 30000, month, gender="M") for month in range(1, 13)) == 2500


def test_state_name_resolution():
    assert resolve_state_code("Haryana") == "HR"
    assert resolve_state_code("Maharashtra") == "MH"


def test_new_regime_tax_rebate_and_surcharge_guard():
    assert annual_tax_new_regime(1275000) == 0
    with pytest.raises(NotImplementedError):
        annual_tax_new_regime(6000000)


def test_payroll_helper_combines_statutory_values():
    result = calculate_statutory_deductions(
        basic_wage=15000,
        monthly_gross=20000,
        month=4,
        state_code="KA",
        annual_gross=900000,
        months_remaining=12,
    )
    assert result["pf"]["employee_epf"] == 1800
    assert result["esi"]["applicable"] is True
    assert result["professional_tax"] == 0
    assert result["tds"] >= 0


def test_missing_maharashtra_gender_only_skips_pt():
    result = calculate_statutory_deductions(
        basic_wage=9000,
        monthly_gross=15000,
        month=6,
        state_code="MH",
        gender=None,
        annual_gross=180000,
        months_remaining=12,
    )
    assert result["pf"]["employee_epf"] == 1080
    assert result["esi"]["employee"] == 113
    assert result["tds"] == 0
    assert result["professional_tax"] is None
    assert "gender" in result["pt_error"]


def test_approved_old_regime_investments_are_capped_and_new_regime_ignored():
    declarations = [
        SimpleNamespace(status="draft", approved_amount=150000, section="80C"),
        SimpleNamespace(status="submitted", approved_amount=150000, section="80C"),
        SimpleNamespace(status="rejected", approved_amount=150000, section="80C"),
        SimpleNamespace(status="pending", approved_amount=150000, section="80C"),
        SimpleNamespace(status="approved", approved_amount=100000, section="80C"),
        SimpleNamespace(status="approved", approved_amount=100000, section="80CCC"),
        SimpleNamespace(status="approved", approved_amount=60000, section="80CCD(1B)"),
    ]
    assert approved_tax_deduction(declarations, "old_regime") == 250000
    assert approved_tax_deduction(declarations, "new_regime") == 0
    base_tds = monthly_tds(1500000, 0, 12, tax_regime="old_regime")
    approved_tds = monthly_tds(
        1500000, 0, 12, tax_regime="old_regime",
        other_deductions=approved_tax_deduction(declarations, "old_regime"),
    )
    assert approved_tds < base_tds


def test_monthly_tds_uses_prior_tds_against_projected_annual_liability():
    full_liability = monthly_tds(2400000, 0, 12, tax_regime="new_regime")
    remaining_liability = monthly_tds(2400000, full_liability, 11, tax_regime="new_regime")
    assert remaining_liability <= full_liability


def test_automatic_ctc_breakup_calculates_monthly_amounts():
    proposals = build_automatic_ctc_lines(300000)
    lines = [
        type("Line", (), {
            "id": index + 1,
            "component_name": item["name"],
            "component_type": item["type"],
            "statutory_code": item["code"],
            "calc_basis": item["basis"],
            "calc_value": item["value"],
            "enabled": item["enabled"],
            "is_employer_cost": item["employer"],
            "is_hr_override": False,
        })()
        for index, item in enumerate(proposals)
    ]
    amounts = calculate_ctc_breakup_lines(300000, lines)
    assert amounts[1] == 150000
    assert amounts[2] == 75000
    assert amounts[3] == 75000
    pf_id = next(line.id for line in lines if line.statutory_code == "pf")
    assert amounts[pf_id] == 1800


def test_ctc_breakup_esic_amount_follows_wage_threshold():
    proposal = build_automatic_ctc_lines(20000)
    lines = [
        type("Line", (), {
            "id": index + 1,
            "component_name": item["name"],
            "component_type": item["type"],
            "statutory_code": item["code"],
            "calc_basis": item["basis"],
            "calc_value": item["value"],
            "enabled": item["enabled"],
            "is_employer_cost": item["employer"],
            "is_hr_override": False,
        })()
        for index, item in enumerate(proposal)
    ]
    amounts = calculate_ctc_breakup_lines(20000, lines)
    esic_id = next(line.id for line in lines if line.statutory_code == "esic")
    assert amounts[esic_id] == 150


def test_pf_preview_does_not_use_zero_placeholder_component_amount():
    assert calculate_statutory_deductions(
        basic_wage=150000,
        monthly_gross=300000,
        month=9,
        annual_gross=3600000,
        months_remaining=7,
    )["pf"]["employee_epf"] == 1800


def test_net_pay_matches_requested_deduction_equation():
    net_pay, deductions = calculate_net_pay(
        gross_pay=100000,
        loss_of_pay=5000,
        pf=1800,
        esic=750,
        tds=2500,
        professional_tax=200,
        miscellaneous_deduction=750,
        advance_recovery=1200,
    )
    assert deductions == 12200
    assert net_pay == 87800
