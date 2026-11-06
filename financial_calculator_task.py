#!/usr/bin/env python3
"""Multi-agent financial calculator demo built with agent-toolkit modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from budget_guard import BudgetGuard
from handoff_checker import HandoffChecker
from work_claim import WorkLedger


def emi(principal: float, annual_rate_pct: float, tenure_months: int) -> float:
    """Monthly EMI for reducing-balance loans."""
    if principal <= 0 or tenure_months <= 0:
        raise ValueError("principal and tenure_months must be positive")
    monthly_rate = annual_rate_pct / 1200
    if monthly_rate == 0:
        return round(principal / tenure_months, 2)
    factor = (1 + monthly_rate) ** tenure_months
    value = principal * monthly_rate * factor / (factor - 1)
    return round(value, 2)


def sip_future_value(monthly_investment: float, annual_return_pct: float, years: int) -> float:
    """Future value of monthly SIP contributions."""
    if monthly_investment <= 0 or years <= 0:
        raise ValueError("monthly_investment and years must be positive")
    months = years * 12
    monthly_rate = annual_return_pct / 1200
    if monthly_rate == 0:
        return round(monthly_investment * months, 2)
    value = monthly_investment * (((1 + monthly_rate) ** months - 1) / monthly_rate) * (
        1 + monthly_rate
    )
    return round(value, 2)


def roi_pct(initial_value: float, final_value: float) -> float:
    """Return on investment in percentage."""
    if initial_value <= 0:
        raise ValueError("initial_value must be positive")
    return round(((final_value - initial_value) / initial_value) * 100, 2)


def estimated_tax_old_regime(annual_income: float) -> float:
    """Simple old-regime slab estimate (no cess/surcharge/deductions)."""
    if annual_income < 0:
        raise ValueError("annual_income cannot be negative")

    tax = 0.0
    slabs = (
        (250000, 0.0),
        (500000, 0.05),
        (1000000, 0.20),
        (float("inf"), 0.30),
    )
    previous = 0.0
    remaining = annual_income

    for cap, rate in slabs:
        taxable = min(remaining, cap - previous)
        if taxable > 0:
            tax += taxable * rate
            remaining -= taxable
        previous = cap
        if remaining <= 0:
            break
    return round(tax, 2)


def _validate_handoff(checker: HandoffChecker, payload: dict) -> None:
    result = checker.check(payload)
    if result["status"] != "ready":
        raise RuntimeError(f"handoff rejected: {result['status']} | {result['problems']}")


@dataclass
class FinancialSummary:
    monthly_emi: float
    sip_future_value: float
    roi_percent: float
    annual_tax: float


def run() -> FinancialSummary:
    root = Path(".agent-toolkit")
    root.mkdir(exist_ok=True)

    claims = WorkLedger(root / "financial-work-claims.json")
    budgets = BudgetGuard(root / "financial-budget-ledger.json")
    checker = HandoffChecker(root=Path.cwd())

    budgets.open_task(
        "financial-calculator",
        max_tokens=3000,
        max_cost_usd=0.03,
        max_tool_calls=20,
        max_wall_clock_seconds=1800,
        reset=True,
    )

    # Agent-1: Loan module
    claims.take("fin-loan-emi", "agent-1", ttl_seconds=900, note="Building EMI calculator")
    budgets.record("financial-calculator", tokens=300, tool_calls=2, note="loan module")
    monthly_emi = emi(750000, 9.0, 120)
    handoff_1 = {
        "goal": "Build investment calculator from loan module baseline",
        "acceptance_criteria": ["Implement SIP future value", "Implement ROI percent calculation"],
        "owner": "agent-2",
        "constraints": ["Use deterministic formulas", "Handle zero-interest/zero-return edge cases"],
        "budget_left": "2700 tokens, $0.027, 18 tool calls",
        "artifacts": ["financial_calculator_task.py"],
        "notes": f"EMI module ready. Sample EMI = {monthly_emi}",
    }
    _validate_handoff(checker, handoff_1)
    claims.done("fin-loan-emi", "agent-1", note="done")

    # Agent-2: Investment module
    claims.take("fin-investment", "agent-2", ttl_seconds=900, note="Building SIP and ROI")
    budgets.record("financial-calculator", tokens=350, tool_calls=2, note="investment module")
    sip_value = sip_future_value(10000, 12.0, 10)
    roi_value = roi_pct(500000, 820000)
    handoff_2 = {
        "goal": "Integrate tax estimate and final response payload",
        "acceptance_criteria": ["Compute annual tax", "Return all metrics in one summary object"],
        "owner": "agent-3",
        "constraints": ["No external dependencies", "Keep formulas auditable"],
        "budget_left": "2350 tokens, $0.024, 16 tool calls",
        "artifacts": ["financial_calculator_task.py"],
        "notes": f"SIP FV={sip_value}, ROI={roi_value}%",
    }
    _validate_handoff(checker, handoff_2)
    claims.done("fin-investment", "agent-2", note="done")

    # Agent-3: Tax + integration module
    claims.take("fin-tax-integration", "agent-3", ttl_seconds=900, note="Integrating tax estimate")
    budgets.record("financial-calculator", tokens=300, tool_calls=2, note="tax module")
    tax_value = estimated_tax_old_regime(1_800_000)
    claims.done("fin-tax-integration", "agent-3", note="done")

    budget_status = budgets.status("financial-calculator")
    claim_status = claims.list_claims(state="all", limit=10)

    print("\nFinancial Calculator Summary")
    print("-" * 40)
    print(f"EMI (₹): {monthly_emi}")
    print(f"SIP Future Value (₹): {sip_value}")
    print(f"ROI (%): {roi_value}")
    print(f"Estimated Annual Tax (₹): {tax_value}")
    print(f"Budget decision: {budget_status['decision']}")
    print(f"Completed claims: {claim_status['count']}")

    return FinancialSummary(
        monthly_emi=monthly_emi,
        sip_future_value=sip_value,
        roi_percent=roi_value,
        annual_tax=tax_value,
    )


if __name__ == "__main__":
    run()
