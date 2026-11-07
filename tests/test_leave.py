"""Leave: accrual (fixed, monthly, days worked), proration, sandwich rules, negative
balance limits, approval and the ledger, partial cancellation, year end, comp off and
expiry, legal floors, working days and tenant isolation."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.conftest import login_as


# ── helpers ─────────────────────────────────────────────────────────

def _db(company_id):
    from core.db import system_session
    db = system_session()
    db.info["company_id"] = company_id
    return db


def _emp_row(db, company, uuid):
    from modules.employees.models import Employee
    return db.query(Employee).filter(Employee.company_id == company.id, Employee.uuid == uuid).first()


def add_emp(company, first="Ravi", doj=date(2024, 1, 1), **extra):
    e = company.add_employee(first, "Kumar", date_of_joining=doj.isoformat(), **extra)
    db = _db(company.id)
    try:
        row = _emp_row(db, company, e["uuid"])
        if hasattr(row, "date_of_joining"):
            row.date_of_joining = doj
        else:
            row.custom = {**(row.custom or {}), "date_of_joining": doj.isoformat()}
        db.commit()
    finally:
        db.close()
    return e


def types(company):
    r = company.get("/leave/types")
    assert r.status_code == 200, r.text
    return {t["code"]: t for t in r.json()}


def set_rules(company, code, rules, effective_from="2000-01-01"):
    t = types(company)[code]
    merged = _deep(t["rules"], rules)
    r = company.put(f"/leave/types/{t['uuid']}", json={"rules": merged, "effective_from": effective_from,
                                                       "change_note": "test"})
    assert r.status_code == 200, r.text
    return r.json()


def _deep(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def adjust(company, emp, code, days, year=2026):
    r = company.post("/leave/balances/adjust", json={"employee_uuid": emp["uuid"], "leave_type_uuid": types(company)[code]["uuid"],
                                                     "days": days, "reason": "test", "year": year})
    assert r.status_code == 201, r.text


def balance(company, emp, code, year=2026):
    r = company.get(f"/leave/me/summary?employee_uuid={emp['uuid']}&year={year}")
    assert r.status_code == 200, r.text
    card = next((c for c in r.json()["cards"] if c["type"]["code"] == code), None)
    return card


def ledger_sum(company_id, emp_uuid, code, year=None, kind=None):
    from sqlalchemy import func
    from modules.employees.models import Employee
    from modules.leave.models import LeaveLedgerEntry, LeaveType
    db = _db(company_id)
    try:
        q = (db.query(func.coalesce(func.sum(LeaveLedgerEntry.days), 0))
             .join(LeaveType, LeaveType.id == LeaveLedgerEntry.leave_type_id)
             .join(Employee, Employee.id == LeaveLedgerEntry.employee_id)
             .filter(Employee.uuid == emp_uuid, LeaveType.code == code, LeaveLedgerEntry.company_id == company_id))
        if year:
            q = q.filter(LeaveLedgerEntry.year == year)
        if kind:
            q = q.filter(LeaveLedgerEntry.kind == kind)
        return Decimal(str(q.scalar()))
    finally:
        db.close()


def run_accrual(company, as_of):
    r = company.post("/leave/runs/accrual", json={"as_of": as_of})
    assert r.status_code == 200, r.text
    return r.json()


def clear_auto_credits(company_id):
    """Start-up accrual (up to today) is not wanted when a test controls dates itself."""
    from modules.leave.models import LeaveLedgerEntry
    db = _db(company_id)
    try:
        db.query(LeaveLedgerEntry).filter(LeaveLedgerEntry.company_id == company_id).delete()
        db.commit()
    finally:
        db.close()


def next_weekday(d: date, weekday: int) -> date:
    return d + timedelta(days=(weekday - d.weekday()) % 7)


# ── defaults ────────────────────────────────────────────────────────

def test_defaults_are_seeded(company_a):
    t = types(company_a)
    assert {"EL", "CL", "SL", "ML", "PTL", "BRV", "MRG", "CO", "LOP", "OH"} <= set(t)
    ml = t["ML"]["rules"]
    assert {o["key"]: o["days"] for o in ml["grant"]["options"]}["birth"] == 182
    el = t["EL"]["rules"]["entitlement"]
    assert el["mode"] == "days_worked" and el["days_worked"]["per_days"] == 20 and el["days_worked"]["min_days_in_year"] == 180
    weeks = company_a.get("/leave/work-weeks").json()
    assert any(w["is_default"] for w in weeks)
    cals = company_a.get("/leave/holiday-calendars").json()
    assert any(c["is_default"] for c in cals) and len(cals) > 1
    flows = company_a.get("/workflow/definitions").json() if company_a.get("/workflow/definitions").status_code == 200 else []
    if isinstance(flows, list) and flows:
        assert any(f.get("request_type") == "leave.request" for f in flows)


def test_legal_floor_for_maternity_and_annual_leave(company_a):
    t = types(company_a)["ML"]
    rules = t["rules"]
    rules["grant"]["options"] = [{"key": "birth", "label": "Birth", "days": 100}]
    r = company_a.put(f"/leave/types/{t['uuid']}", json={"rules": rules})
    assert r.status_code == 400 and "26 weeks" in r.json()["detail"]
    el = types(company_a)["EL"]
    rules = el["rules"]
    rules["entitlement"]["days_worked"]["per_days"] = 30
    r = company_a.put(f"/leave/types/{el['uuid']}", json={"rules": rules})
    assert r.status_code == 400 and "20 days" in r.json()["detail"]


def test_new_rules_create_a_version_and_old_periods_keep_old_rules(company_a):
    clear_auto_credits(company_a.id)
    emp = add_emp(company_a, "Vers", doj=date(2025, 1, 1))
    # CL: 12 a year, monthly at the start of the month. From 1 Jul 2026 it becomes 24 a year.
    set_rules(company_a, "CL", {"entitlement": {"mode": "accrual", "yearly_quota": 12, "frequency": "monthly", "credit_at": "start"}})
    out = set_rules(company_a, "CL", {"entitlement": {"yearly_quota": 24}}, effective_from="2026-07-01")
    assert out["current_version"] >= 2 and out["versions"][0]["effective_from"] == "2026-07-01"
    run_accrual(company_a, "2026-08-15")
    # Jan..Jun at 1 a month, Jul..Aug at 2 a month
    assert ledger_sum(company_a.id, emp["uuid"], "CL", 2026, "accrual") == Decimal("10")


# ── accrual and proration ───────────────────────────────────────────

def test_proration_rules():
    from modules.leave.ledger import _period_credit, proration
    from modules.leave.rules import clean_rules
    rules = clean_rules({"entitlement": {"mode": "fixed", "yearly_quota": 12,
                                         "proration": {"basis": "months", "joining_cutoff_day": 15, "rounding": "half"}}})
    ps, pe = date(2026, 1, 1), date(2026, 12, 31)
    facts = {"date_of_joining": date(2026, 3, 10), "exit_date": None, "on_probation": False}
    assert proration(rules, facts, ps, pe) == Decimal(10) / Decimal(12)
    facts["date_of_joining"] = date(2026, 3, 20)  # after the cut-off: March does not count
    assert _period_credit(rules, facts, ps, pe, ps) == Decimal("9.00")
    facts["exit_date"] = date(2026, 9, 10)  # leaves before the 15th: September does not count
    assert _period_credit(rules, facts, ps, pe, ps) == Decimal("5.50") or _period_credit(rules, facts, ps, pe, ps) == Decimal("5.00")
    rules = clean_rules({"entitlement": {"mode": "fixed", "yearly_quota": 12,
                                         "proration": {"basis": "days", "rounding": "quarter", "round_mode": "down"}}})
    facts = {"date_of_joining": date(2026, 7, 1), "exit_date": None, "on_probation": False}
    assert _period_credit(rules, facts, ps, pe, ps) == Decimal("6.00")


def test_probation_and_experience_levels():
    from modules.leave.ledger import quota_for
    from modules.leave.rules import clean_rules
    rules = clean_rules({"entitlement": {"yearly_quota": 15, "probation": {"credit": "reduced", "reduced_quota": 6},
                                         "experience_levels": [{"after_years": 5, "yearly_quota": 21}]}})
    on = date(2026, 1, 1)
    assert quota_for(rules, {"date_of_joining": date(2025, 10, 1), "on_probation": True}, on) == 6
    assert quota_for(rules, {"date_of_joining": date(2023, 1, 1), "on_probation": False}, on) == 15
    assert quota_for(rules, {"date_of_joining": date(2019, 1, 1), "on_probation": False}, on) == 21


def test_monthly_accrual_is_idempotent(company_a):
    clear_auto_credits(company_a.id)
    emp = add_emp(company_a, "Meena", doj=date(2026, 1, 1))
    set_rules(company_a, "CL", {"entitlement": {"mode": "accrual", "yearly_quota": 12, "frequency": "monthly", "credit_at": "start"}})
    set_rules(company_a, "SL", {"entitlement": {"mode": "accrual", "yearly_quota": 12, "frequency": "quarterly", "credit_at": "end"}})
    run_accrual(company_a, "2026-03-15")
    assert ledger_sum(company_a.id, emp["uuid"], "CL", 2026, "accrual") == Decimal("3")
    again = run_accrual(company_a, "2026-03-15")
    assert ledger_sum(company_a.id, emp["uuid"], "CL", 2026, "accrual") == Decimal("3")
    assert again["by_type"].get("CL", 0) == 0
    # credit at the end of the period: nothing for Q1 on 15 March, two quarters by 1 July
    assert ledger_sum(company_a.id, emp["uuid"], "SL", 2026, "accrual") == 0
    run_accrual(company_a, "2026-07-01")
    assert ledger_sum(company_a.id, emp["uuid"], "SL", 2026, "accrual") == Decimal("6")


def test_days_worked_rule_osh_code(company_a):
    """EL: 1 day per 20 days worked, nothing until 180 days of work in the year."""
    from modules.leave.calendar import WorkCalendar
    clear_auto_credits(company_a.id)
    emp = add_emp(company_a, "Arun", doj=date(2026, 1, 1))
    run_accrual(company_a, "2026-07-01")
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2026, "accrual") == 0  # about 125 days worked by June
    run_accrual(company_a, "2026-11-01")
    db = _db(company_a.id)
    try:
        row = _emp_row(db, company_a, emp["uuid"])
        cal = WorkCalendar(db, company_a.id)
        worked = cal.working_days(row, date(2026, 1, 1), date(2026, 10, 31))
        holidays = sum(1 for x in cal.days(row, date(2026, 1, 1), date(2026, 10, 31)) if x["kind"] == "holiday")
    finally:
        db.close()
    total = worked + holidays
    assert total >= 180
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2026, "accrual") == Decimal(int(total // 20))
    # the same run again adds nothing
    run_accrual(company_a, "2026-11-01")
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2026, "accrual") == Decimal(int(total // 20))


# ── requests ────────────────────────────────────────────────────────

def _apply(session, code, start, end=None, company=None, expect=201, **extra):
    t = types(company or session)[code]
    body = {"leave_type_uuid": t["uuid"], "from_date": start.isoformat(), "to_date": (end or start).isoformat(), **extra}
    r = session.post("/leave/requests", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def _preview(session, code, start, end=None, company=None, **extra):
    t = types(company or session)[code]
    r = session.post("/leave/requests/preview", json={"leave_type_uuid": t["uuid"], "from_date": start.isoformat(),
                                                       "to_date": (end or start).isoformat(), **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_sandwich_counts_weekend_between_leave_days(company_a):
    emp = add_emp(company_a, "Sana")
    adjust(company_a, emp, "EL", 20)
    me = login_as(company_a, emp)
    thu = next_weekday(date(2026, 12, 1), 3)  # Thursday 3 Dec 2026
    mon = thu + timedelta(days=4)
    p = _preview(me, "EL", thu, mon, company=company_a)  # Thu, Fri, (Sat, Sun), Mon
    assert p["days"] == 5 and p["sandwich_days"] == 2
    fri = thu + timedelta(days=1)
    p = _preview(me, "EL", fri, mon, company=company_a)  # only 2 working days: below the 3-day threshold
    assert p["days"] == 2 and p["sandwich_days"] == 0
    set_rules(company_a, "CL", {"restrictions": {"sandwich": {"weekends": False, "holidays": False}, "max_days": 5,
                                                 "max_consecutive": 7}})
    adjust(company_a, emp, "CL", 10)
    p = _preview(me, "CL", thu, mon, company=company_a)
    assert p["days"] == 3


def test_negative_balance_limits_and_lop(company_a):
    emp = add_emp(company_a, "Neha")
    me = login_as(company_a, emp)
    card = balance(company_a, emp, "SL")
    adjust(company_a, emp, "SL", 1 - card["balance"])  # SL balance 1, may go 2 below zero
    mon = next_weekday(date(2026, 11, 16), 0)
    ok = _preview(me, "SL", mon, mon + timedelta(days=2), company=company_a, attachment="data:text/plain;base64,eA==")
    assert ok["ok"], ok
    too_many = _preview(me, "SL", mon, mon + timedelta(days=3), company=company_a, attachment="data:text/plain;base64,eA==")
    assert not too_many["ok"] and any("available" in e for e in too_many["errors"])
    # CL: excess becomes loss of pay
    cl = balance(company_a, emp, "CL")
    adjust(company_a, emp, "CL", 1 - cl["balance"])
    req = _apply(me, "CL", mon + timedelta(days=7), mon + timedelta(days=9), company=company_a)
    assert req["days"] == 3 and req["lop_days"] == 2 and req["paid_days"] == 1


def test_document_and_notice_rules(company_a):
    emp = add_emp(company_a, "Dev")
    me = login_as(company_a, emp)
    adjust(company_a, emp, "SL", 10)
    mon = next_weekday(date(2026, 11, 23), 0)
    p = _preview(me, "SL", mon, mon + timedelta(days=2), company=company_a)
    assert any("document" in e for e in p["errors"])
    adjust(company_a, emp, "EL", 10)
    soon = next_weekday(date(2026, 11, 9), 0)
    p = _preview(me, "EL", soon, soon + timedelta(days=3), company=company_a)
    assert any("in advance" in e for e in p["errors"])


def _decide_as(session, decision="approve", comment=None):
    r = session.get("/workflow/inbox?status=pending")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "nothing waiting"
    task = items[0]
    r = session.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": decision, "comment": comment})
    assert r.status_code == 200, r.text
    return r.json()


def test_approval_updates_ledger_and_rejection_restores(company_a):
    mgr = add_emp(company_a, "Manju", role="manager")
    db = _db(company_a.id)
    try:
        mgr_id = _emp_row(db, company_a, mgr["uuid"]).id
    finally:
        db.close()
    emp = add_emp(company_a, "Ishaan", reporting_manager_id=mgr_id)
    adjust(company_a, emp, "CL", 5)
    before = balance(company_a, emp, "CL")["balance"]
    me = login_as(company_a, emp)
    d = next_weekday(date(2026, 11, 23), 1)
    req = _apply(me, "CL", d, d + timedelta(days=1), company=company_a)
    assert req["status"] == "pending"
    card = balance(company_a, emp, "CL")
    assert card["balance"] == before and card["booked"] == 2 and card["available"] == before - 2
    _decide_as(login_as(company_a, mgr), "approve")
    detail = me.get(f"/leave/requests/{req['uuid']}").json()
    assert detail["status"] == "approved"
    assert balance(company_a, emp, "CL")["balance"] == before - 2
    # a second request, rejected: nothing is taken
    d2 = d + timedelta(days=7)
    req2 = _apply(me, "CL", d2, company=company_a)
    _decide_as(login_as(company_a, mgr), "reject", "Busy week")
    assert me.get(f"/leave/requests/{req2['uuid']}").json()["status"] == "rejected"
    assert balance(company_a, emp, "CL")["balance"] == before - 2
    # cancelling approved leave goes back to the manager; on approval the days come back
    r = me.post(f"/leave/requests/{req['uuid']}/cancel", json={"reason": "Plans changed"})
    assert r.status_code == 200 and r.json()["status"] == "cancel_pending"
    _decide_as(login_as(company_a, mgr), "approve")
    assert me.get(f"/leave/requests/{req['uuid']}").json()["status"] == "cancelled"
    assert balance(company_a, emp, "CL")["balance"] == before


def test_more_than_three_days_needs_hr_too(company_a):
    mgr = add_emp(company_a, "Mohan", role="manager")
    hr = add_emp(company_a, "Hema", role="hr")
    db = _db(company_a.id)
    try:
        mgr_id = _emp_row(db, company_a, mgr["uuid"]).id
    finally:
        db.close()
    emp = add_emp(company_a, "Kiran", reporting_manager_id=mgr_id)
    adjust(company_a, emp, "EL", 15)
    me = login_as(company_a, emp)
    mon = next_weekday(date(2026, 12, 7), 0)
    req = _apply(me, "EL", mon, mon + timedelta(days=4), company=company_a)
    _decide_as(login_as(company_a, mgr))
    assert me.get(f"/leave/requests/{req['uuid']}").json()["status"] == "pending"
    _decide_as(login_as(company_a, hr))
    assert me.get(f"/leave/requests/{req['uuid']}").json()["status"] == "approved"
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2026, "availed") == Decimal("-5")


def test_partial_cancellation_by_hr(company_a):
    emp = add_emp(company_a, "Pooja")
    adjust(company_a, emp, "EL", 15)
    mon = next_weekday(date(2026, 12, 14), 0)
    # HR records leave for the employee: approved at once
    req = _apply(company_a, "EL", mon, mon + timedelta(days=4), employee_uuid=emp["uuid"])
    assert req["status"] == "approved" and req["days"] == 5
    wed = (mon + timedelta(days=2)).isoformat()
    r = company_a.post(f"/leave/requests/{req['uuid']}/cancel", json={"dates": [wed], "reason": "Came to work"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["days"] == 4
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2026, "restored") == Decimal("1")
    # cancelling the rest
    r = company_a.post(f"/leave/requests/{req['uuid']}/cancel", json={"reason": "Not needed"})
    assert r.json()["status"] == "cancelled"
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2026, "restored") == Decimal("5")


def test_extension_and_modify(company_a):
    emp = add_emp(company_a, "Raj")
    adjust(company_a, emp, "EL", 20)
    me = login_as(company_a, emp)
    mon = next_weekday(date(2026, 12, 21), 0)
    req = _apply(me, "EL", mon, mon + timedelta(days=1), company=company_a, reason="Trip")
    r = me.put(f"/leave/requests/{req['uuid']}", json={"to_date": (mon + timedelta(days=2)).isoformat()})
    assert r.status_code == 200 and r.json()["days"] == 3
    r = me.post(f"/leave/requests/{req['uuid']}/extend", json={"to_date": (mon + timedelta(days=3)).isoformat(),
                                                                 "reason": "Train delayed"})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "extension" and r.json()["days"] == 1


def test_comp_off_credit_and_expiry(company_a):
    from modules.leave import ledger
    emp = add_emp(company_a, "Tara")
    me = login_as(company_a, emp)
    sunday = next_weekday(date(2026, 11, 1), 6)
    r = me.post("/leave/comp-offs", json={"worked_date": sunday.isoformat(), "unit": "day", "reason": "Release"})
    assert r.status_code == 201, r.text
    _decide_as(company_a)  # no manager: the company owner approves
    assert ledger_sum(company_a.id, emp["uuid"], "CO", kind="comp_off") == 1
    weekday = next_weekday(date(2026, 11, 2), 2)
    r = me.post("/leave/comp-offs", json={"worked_date": weekday.isoformat(), "unit": "day", "reason": "x"})
    assert r.status_code == 400
    db = _db(company_a.id)
    try:
        ledger.run_expiry(db, company_a.id, sunday + timedelta(days=200))
        db.commit()
    finally:
        db.close()
    assert ledger_sum(company_a.id, emp["uuid"], "CO", kind="expired") == -1


def test_year_end_carry_forward_encash_and_lapse(company_a):
    emp = add_emp(company_a, "Yamini")
    adjust(company_a, emp, "EL", 50, year=2025)  # 50 days left at the end of 2025
    prev = company_a.post("/leave/runs/year-end", json={"year": 2025, "apply": False}).json()
    row = next(r for r in prev["rows"] if r["employee_uuid"] == emp["uuid"] and r["leave_type"] == "EL")
    assert (row["carry"], row["encash"], row["lapse"]) == (30, 15, 5)
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2026, "carry_forward") == 0  # preview writes nothing
    r = company_a.post("/leave/runs/year-end", json={"year": 2025, "apply": True})
    assert r.status_code == 200, r.text
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2026, "carry_forward") == 30
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2025, "encashed") == -15
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2025, "lapsed") == -5
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2025) == 0
    company_a.post("/leave/runs/year-end", json={"year": 2025, "apply": True})  # again: nothing new
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2026, "carry_forward") == 30


def test_opening_balance_import(company_a):
    emp = add_emp(company_a, "Om", employee_code="LV-IMP-1")
    csv = "employee,leave_type,days,year\nLV-IMP-1,EL,7.5,2026\nnobody,EL,1,2026\n"
    r = company_a.post("/leave/balances/import", json={"csv": csv})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1 and r.json()["error_count"] == 1
    assert ledger_sum(company_a.id, emp["uuid"], "EL", 2026, "opening") == Decimal("7.5")


def test_working_days_and_holidays(company_a):
    from modules.leave.calendar import is_working_day, working_days
    cals = company_a.get("/leave/holiday-calendars").json()
    default = next(c for c in cals if c["is_default"])
    r = company_a.post("/leave/holidays", json={"name": "Company day", "date": "2026-12-02", "kind": "festival",
                                                "calendar_uuids": [default["uuid"]]})
    assert r.status_code == 201, r.text
    emp = add_emp(company_a, "Wasim")
    db = _db(company_a.id)
    try:
        row = _emp_row(db, company_a, emp["uuid"])
        assert working_days(db, row, date(2026, 11, 30), date(2026, 12, 6)) == 4  # Mon-Fri less one holiday
        assert not is_working_day(db, row, date(2026, 12, 2))
        assert not is_working_day(db, row, date(2026, 12, 5))
    finally:
        db.close()
    # a 2nd/4th Saturday pattern as the default
    weeks = company_a.get("/leave/work-weeks").json()
    alt = next(w for w in weeks if "Saturday" in w["name"] and "2nd" in w["name"])
    assert company_a.put(f"/leave/work-weeks/{alt['uuid']}", json={"is_default": True}).status_code == 200
    db = _db(company_a.id)
    try:
        row = _emp_row(db, company_a, emp["uuid"])
        assert is_working_day(db, row, date(2026, 12, 5))       # 1st Saturday
        assert not is_working_day(db, row, date(2026, 12, 12))  # 2nd Saturday
    finally:
        db.close()


def test_reports(company_a):
    emp = add_emp(company_a, "Report")
    mon = next_weekday(date(2026, 11, 9), 0)
    _apply(company_a, "LOP", mon, mon + timedelta(days=1), employee_uuid=emp["uuid"])
    lop = company_a.get("/leave/reports/lop?period=2026-11").json()
    row = next(r for r in lop["rows"] if r["employee_uuid"] == emp["uuid"])
    assert row["lop_days"] == 2
    assert company_a.get(f"/leave/reports/absentees?day={mon.isoformat()}").json()["on_leave"] >= 1
    assert company_a.get("/leave/reports/balances?format=csv").status_code == 200
    assert company_a.get("/leave/reports/taken?from=2026-11-01&to=2026-11-30").json()["total"] >= 2


def test_employee_cannot_manage_or_configure(company_a):
    emp = add_emp(company_a, "Plain")
    me = login_as(company_a, emp)
    assert me.post("/leave/balances/adjust", json={"employee_uuid": emp["uuid"], "leave_type_uuid": types(company_a)["EL"]["uuid"],
                                                    "days": 5, "reason": "x"}).status_code == 403
    assert me.post("/leave/runs/accrual", json={}).status_code == 403
    assert me.post("/leave/types", json={"code": "XX", "name": "X"}).status_code == 403
    other = add_emp(company_a, "Other")
    r = me.post("/leave/requests", json={"leave_type_uuid": types(company_a)["CL"]["uuid"], "from_date": "2026-12-01",
                                         "employee_uuid": other["uuid"]})
    assert r.status_code == 403


def test_tenant_isolation(company_a, company_b):
    emp = add_emp(company_a, "Alpha")
    adjust(company_a, emp, "EL", 5)
    mon = next_weekday(date(2026, 12, 7), 0)
    req = _apply(company_a, "CL", mon, employee_uuid=emp["uuid"])
    assert company_b.get(f"/leave/requests/{req['uuid']}").status_code == 404
    assert company_b.post(f"/leave/requests/{req['uuid']}/cancel", json={}).status_code == 404
    b_types = types(company_b)
    assert company_b.post("/leave/balances/adjust", json={"employee_uuid": emp["uuid"], "leave_type_uuid": b_types["EL"]["uuid"],
                                                          "days": 5, "reason": "x"}).status_code == 404
    a_type = types(company_a)["EL"]
    assert company_b.put(f"/leave/types/{a_type['uuid']}", json={"name": "Hacked"}).status_code == 404
    a_cal = company_a.get("/leave/holiday-calendars").json()[0]
    assert company_b.put(f"/leave/holiday-calendars/{a_cal['uuid']}", json={"name": "x"}).status_code == 404
    listed = company_b.get("/leave/requests?scope=all").json()
    assert all(r["uuid"] != req["uuid"] for r in listed["items"])
    assert all(r["employee_uuid"] != emp["uuid"] for r in company_b.get("/leave/reports/balances").json()["rows"])


@pytest.mark.parametrize("table", ["leave_types", "leave_ledger", "leave_requests", "holiday_calendars", "holidays",
                                   "work_week_patterns", "comp_off_requests", "leave_policy_versions"])
def test_rls_hides_other_company_rows(company_a, company_b, table):
    from sqlalchemy import text
    from core.context import RequestContext, use_context
    from core.db import SessionLocal, set_tenant
    db = SessionLocal()
    # a context of its own, so the company set here doesn't stay on the test thread
    with use_context(RequestContext(actor_type="system")):
        try:
            set_tenant(db, company_b.id)
            ids = {r[0] for r in db.execute(text(f"SELECT DISTINCT company_id FROM {table}")).all()}
            assert ids <= {company_b.id}
        finally:
            db.close()
