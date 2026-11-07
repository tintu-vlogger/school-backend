"""Payroll's own report sources (loaded by the report engine): pay equity, cost against
budget, CTC breakup, payroll variance trend and the statutory liability summary. The maths
lives in modules/payroll/insights.py."""
from __future__ import annotations

from modules.reports.registry import Field, Param, Source, register

PAY = ("payroll.view",)
MONEY = dict(type="money", sensitive="payroll.view")


def _fy(params: dict, ctx: dict) -> str:
    from modules.payroll import calc
    fy = (params.get("fy") or "").strip()
    return fy if len(fy) == 7 and fy[4] == "-" else calc.fy_of(ctx["today"])


def _settings(db, company_id) -> dict:
    from modules.payroll import service
    return service.get_settings(db, company_id)


def _equity_fetch(db, company_id, ids, params, ctx):
    from modules.payroll import insights
    st = _settings(db, company_id).get("pay_equity") or {}
    people = insights.equity_people(db, company_id, params.get("on") or ctx["today"], st.get("basis", "annual_ctc"), ids)
    return insights.pay_equity(people, threshold=st.get("gap_percent", 5), min_size=int(st.get("min_group_size", 1)))


def _equity() -> Source:
    return Source(
        key="pay_equity", label="Pay equity by gender", category="pay", sensitive=True, fetch=_equity_fetch,
        description="Average and median pay of men and women in the same designation and grade, with gaps above the "
                    "set threshold flagged (equal pay for the same or similar work, Code on Wages section 3).",
        all_perms=PAY, allow_team=False, allow_self=False,
        params=[Param("on", "As on", "date", "@today")],
        fields=[Field("designation", "Designation", "text", default=True), Field("grade", "Grade", "text", default=True),
                Field("people", "People", "number"), Field("men", "Men", "number", default=True),
                Field("women", "Women", "number", default=True), Field("others", "Others / not given", "number"),
                Field("average_men", "Average pay, men", default=True, **MONEY),
                Field("average_women", "Average pay, women", default=True, **MONEY),
                Field("median_men", "Median pay, men", **MONEY), Field("median_women", "Median pay, women", **MONEY),
                Field("gap_percent", "Gap (average) %", "percent", default=True, agg="avg"),
                Field("median_gap_percent", "Gap (median) %", "percent", agg="avg"),
                Field("flag", "Check", "text", default=True), Field("flagged", "Flagged", "bool")])


def _budget_fetch(db, company_id, ids, params, ctx):
    from modules.payroll import insights
    return insights.cost_vs_budget(db, company_id, _fy(params, ctx), params.get("by") or "department", ctx["today"])


def _budget() -> Source:
    return Source(
        key="payroll_cost_budget", label="Payroll cost against budget", category="pay", sensitive=True,
        fetch=_budget_fetch, all_perms=PAY, allow_team=False, allow_self=False,
        description="Employer cost so far this year, projected for the full year, against the budget of each "
                    "department or cost centre.",
        params=[Param("fy", "Financial year (e.g. 2026-27)", "text", None, required=False),
                Param("by", "By (department or cost_centre)", "text", "department", required=False)],
        fields=[Field("name", "Department / cost centre", "text", default=True), Field("dimension", "Type", "text"),
                Field("people", "People paid", "number", default=True),
                Field("budget", "Budget for the year", default=True, **MONEY),
                Field("actual", "Cost so far", default=True, **MONEY), Field("gross", "Gross pay so far", **MONEY),
                Field("months", "Months paid", "number"), Field("projected", "Projected for the year", default=True, **MONEY),
                Field("remaining", "Budget left", **MONEY),
                Field("projected_variance", "Projected over (+) / under (−)", default=True, **MONEY),
                Field("used_percent", "Used %", "percent", default=True, agg="avg"),
                Field("status", "Status", "text", default=True)])


def _ctc_fetch(db, company_id, ids, params, ctx):
    from modules.payroll import insights
    return insights.ctc_rows(db, company_id, params.get("on") or ctx["today"], ids)


def _ctc() -> Source:
    return Source(
        key="ctc_breakup", label="CTC breakup", category="pay", sensitive=True, per_employee=True, fetch=_ctc_fetch,
        all_perms=PAY, allow_team=False, date_field="effective_from",
        description="Each person's annual CTC split into basic, DA, HRA, other allowances, flexible benefits and "
                    "employer contributions.",
        params=[Param("on", "As on", "date", "@today")],
        fields=[Field("employee_id", "Employee id", "number", hidden=True),
                Field("employee_code", "Employee ID", "text", default=True),
                Field("employee_name", "Employee", "text", default=True),
                Field("department", "Department", "text", default=True), Field("designation", "Designation", "text"),
                Field("grade", "Grade", "text"), Field("cost_centre", "Cost centre", "text"),
                Field("effective_from", "Since", "date"),
                Field("annual_ctc", "Annual CTC", default=True, **MONEY),
                Field("monthly_gross", "Monthly gross", default=True, **MONEY),
                Field("basic", "Basic (year)", default=True, **MONEY), Field("da", "DA (year)", **MONEY),
                Field("hra", "HRA (year)", default=True, **MONEY), Field("allowances", "Other allowances (year)", default=True, **MONEY),
                Field("flexible", "Flexible benefits (year)", **MONEY),
                Field("employer_contributions", "Employer contributions (year)", default=True, **MONEY),
                Field("reason", "Last change", "text")])


def _variance_fetch(db, company_id, ids, params, ctx):
    from modules.payroll import insights
    st = _settings(db, company_id)
    return insights.variance_trend(db, company_id, _fy(params, ctx), st.get("variance_percent", 20))


def _variance() -> Source:
    return Source(
        key="payroll_variance", label="Payroll variance trend", category="pay", sensitive=True, fetch=_variance_fetch,
        all_perms=PAY, allow_team=False, allow_self=False, date_field="month",
        description="Month by month: people paid, gross, net and employer cost with the change from the month before; "
                    "big changes are flagged.",
        params=[Param("fy", "Financial year (e.g. 2026-27)", "text", None, required=False)],
        fields=[Field("month", "Month", "date", default=True), Field("employees", "People paid", "number", default=True),
                Field("gross", "Gross", default=True, **MONEY), Field("deductions", "Deductions", **MONEY),
                Field("net", "Net", default=True, **MONEY), Field("employer_cost", "Employer cost", default=True, **MONEY),
                Field("off_cycle_cost", "Off-cycle runs", **MONEY),
                Field("change", "Change in cost", default=True, **MONEY),
                Field("change_percent", "Change %", "percent", default=True, agg="avg"),
                Field("joined", "Joined", "number"), Field("left", "Left", "number"),
                Field("flag", "Check", "text", default=True)])


def _liability_fetch(db, company_id, ids, params, ctx):
    from modules.payroll import insights
    return insights.statutory_liability(db, company_id, _fy(params, ctx), ctx["today"])


def _liability() -> Source:
    return Source(
        key="statutory_liability", label="Statutory liability", category="pay", sensitive=True, fetch=_liability_fetch,
        all_perms=PAY + ("payroll.statutory.manage",), allow_team=False, allow_self=False, date_field="due_date",
        description="PF, ESI, TDS, Professional Tax and LWF owed for each month, what has been paid against the "
                    "challans, what is outstanding and what is late.",
        params=[Param("fy", "Financial year (e.g. 2026-27)", "text", None, required=False)],
        fields=[Field("month", "Month", "date", default=True), Field("kind", "What", "text", default=True),
                Field("state", "State", "text"), Field("entity", "Legal entity", "text"),
                Field("due_date", "Due", "date", default=True), Field("liability", "Owed", default=True, **MONEY),
                Field("paid", "Paid", default=True, **MONEY), Field("outstanding", "Outstanding", default=True, **MONEY),
                Field("status", "Status", "text", default=True), Field("paid_on", "Paid on", "date"),
                Field("reference", "Challan / reference", "text"), Field("days_late", "Days late", "number")])


for _s in (_equity, _budget, _ctc, _variance, _liability):
    register(_s())
