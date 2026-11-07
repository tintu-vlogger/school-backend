"""Leave balances and the runs that move them.

Every change of a balance is a row in leave_ledger; a balance is the sum of its rows for
an employee, a leave type and a leave year (types that never reset, such as comp off,
sum across years). Scheduled work is idempotent: each credit has a key such as
"acc:<type>:<employee>:<period start>", so running the same accrual twice adds nothing.

run_accruals(as_of)      credit everything due up to a date (fixed, periodic and days-worked)
year_end(year, apply)    carry forward, encash and lapse; preview without writing
run_expiry(as_of)        expire carried leave and comp off that passed their date (oldest first)
adjust(...)              manual change with a reason
import_opening(csv)      opening balances from a file
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from core import audit, context
from core.errors import BadRequest
from modules.leave import policy
from modules.leave.calendar import WorkCalendar
from modules.leave.facts import applies, tenure_days
from modules.leave.models import LeaveLedgerEntry, LeaveRequest, LeaveRun, LeaveType
from modules.leave.rules import FREQUENCIES, ROUNDINGS

ZERO = Decimal("0")
CREDIT_KINDS = ("opening", "accrual", "carry_forward", "grant", "comp_off")
EXPIRING_KINDS = ("carry_forward", "comp_off")


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x or 0))


def q2(x: Decimal) -> Decimal:
    return D(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def round_to(x: Decimal, rounding: str, mode: str) -> Decimal:
    step = ROUNDINGS.get(rounding)
    if not step:
        return q2(x)
    r = {"nearest": ROUND_HALF_UP, "down": ROUND_FLOOR, "up": ROUND_CEILING}[mode]
    return q2((D(x) / step).quantize(Decimal("1"), rounding=r) * step)


def _employee_model():
    from modules.employees.models import Employee
    return Employee


def _actor() -> str:
    return context.current().actor_name or "System"


def add_entry(db: Session, company_id: int, employee_id: int, type_id: int, year: int, day: date, kind: str,
              days, *, request_id=None, version_id=None, idem: str | None = None, note: str | None = None,
              expires_on: date | None = None, meta: dict | None = None, known_keys: set | None = None):
    days = q2(days)
    if days == 0 and kind not in ("opening",):
        return None
    if idem:
        if known_keys is not None:
            if idem in known_keys:
                return None
        elif db.query(LeaveLedgerEntry.id).filter(LeaveLedgerEntry.company_id == company_id,
                                                  LeaveLedgerEntry.idem_key == idem).first():
            return None
    row = LeaveLedgerEntry(company_id=company_id, employee_id=employee_id, leave_type_id=type_id, year=year,
                           entry_date=day, kind=kind, days=days, request_id=request_id, policy_version_id=version_id,
                           idem_key=idem, note=(note or None) and note[:300], expires_on=expires_on, meta=meta or {},
                           created_by_name=_actor())
    db.add(row)
    if known_keys is not None and idem:
        known_keys.add(idem)
    return row


# ── Balances ────────────────────────────────────────────────────────

def _resets(t: LeaveType, on: date) -> bool:
    return bool(policy.rules_on(t, on)["year_end"]["reset"])


def kind_totals(db: Session, company_id: int, employee_ids: list[int] | None, types: list[LeaveType],
                year_of: dict[int, int]) -> dict[tuple[int, int], dict[str, Decimal]]:
    """{(employee_id, type_id): {kind: total}} for each type's year (all years for types that don't reset)."""
    out: dict[tuple[int, int], dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    if not types:
        return out
    q = (db.query(LeaveLedgerEntry.employee_id, LeaveLedgerEntry.leave_type_id, LeaveLedgerEntry.year,
                  LeaveLedgerEntry.kind, func.sum(LeaveLedgerEntry.days))
         .filter(LeaveLedgerEntry.company_id == company_id,
                 LeaveLedgerEntry.leave_type_id.in_([t.id for t in types]))
         .group_by(LeaveLedgerEntry.employee_id, LeaveLedgerEntry.leave_type_id, LeaveLedgerEntry.year,
                   LeaveLedgerEntry.kind))
    if employee_ids is not None:
        q = q.filter(LeaveLedgerEntry.employee_id.in_(employee_ids))
    resets = {t.id: bool(policy.rules_on(t, date(year_of[t.id], 12, 31))["year_end"]["reset"]) for t in types}
    for emp_id, type_id, year, kind, total in q.all():
        if resets[type_id] and year != year_of[type_id]:
            continue
        out[(emp_id, type_id)][kind] += D(total)
    return out


def balance(db: Session, company_id: int, employee_id: int, t: LeaveType, year: int) -> Decimal:
    q = db.query(func.coalesce(func.sum(LeaveLedgerEntry.days), 0)).filter(
        LeaveLedgerEntry.company_id == company_id, LeaveLedgerEntry.employee_id == employee_id,
        LeaveLedgerEntry.leave_type_id == t.id)
    if _resets(t, policy.year_range(policy.rules_on(t, date(year, 12, 31)), year)[0]):
        q = q.filter(LeaveLedgerEntry.year == year)
    return D(q.scalar())


def booked(db: Session, company_id: int, employee_id: int, t: LeaveType, year: int | None,
           exclude_request_id: int | None = None) -> Decimal:
    """Days of pending requests (not yet in the ledger) in a leave year."""
    q = db.query(LeaveRequest).filter(LeaveRequest.company_id == company_id, LeaveRequest.employee_id == employee_id,
                                      LeaveRequest.leave_type_id == t.id, LeaveRequest.status == "pending")
    if exclude_request_id:
        q = q.filter(LeaveRequest.id != exclude_request_id)
    total = ZERO
    for r in q.all():
        for it in r.breakdown or []:
            if it.get("cancelled"):
                continue
            if year is not None:
                rules = policy.rules_on(t, date.fromisoformat(it["date"]))
                if rules["year_end"]["reset"] and policy.year_bounds(rules, date.fromisoformat(it["date"]))[0] != year:
                    continue
            total += D(it.get("amount")) - D(it.get("lop"))
    return total


def expiry_plan(entries: list[LeaveLedgerEntry], as_of: date) -> list[tuple[LeaveLedgerEntry, Decimal]]:
    """Credits that expired before `as_of` with what is left of each, using leave taken on
    the oldest-expiring credits first. Returns [(credit, remaining)] for unprocessed ones."""
    credits = sorted([e for e in entries if e.kind in EXPIRING_KINDS and e.expires_on is not None and D(e.days) > 0],
                     key=lambda e: (e.expires_on, e.id or 0))
    used_pool = -sum((D(e.days) for e in entries if e.kind in ("availed", "restored", "encashed")), ZERO)
    done = {(e.meta or {}).get("credit_id") for e in entries if e.kind == "expired"}
    out = []
    for c in credits:
        take = min(D(c.days), max(used_pool, ZERO))
        used_pool -= take
        remaining = D(c.days) - take
        if c.expires_on < as_of and remaining > 0 and c.id not in done:
            out.append((c, remaining))
    return out


def pending_expiry(db: Session, company_id: int, employee_id: int, t: LeaveType, year: int, as_of: date) -> Decimal:
    entries = _entries(db, company_id, employee_id, t, year)
    return sum((r for _, r in expiry_plan(entries, as_of)), ZERO)


def _entries(db: Session, company_id: int, employee_id: int, t: LeaveType, year: int) -> list[LeaveLedgerEntry]:
    q = db.query(LeaveLedgerEntry).filter(LeaveLedgerEntry.company_id == company_id,
                                          LeaveLedgerEntry.employee_id == employee_id,
                                          LeaveLedgerEntry.leave_type_id == t.id)
    if _resets(t, date(year, 12, 31)):
        q = q.filter(LeaveLedgerEntry.year == year)
    return q.order_by(LeaveLedgerEntry.id).all()


# ── Accrual ─────────────────────────────────────────────────────────

def periods(rules: dict, year: int) -> list[tuple[date, date]]:
    start, end = policy.year_range(rules, year)
    n = FREQUENCIES[rules["entitlement"]["frequency"]]
    months = 12 // n
    out, cur = [], start
    for _ in range(n):
        m = cur.month - 1 + months
        nxt = date(cur.year + m // 12, m % 12 + 1, 1)
        out.append((cur, min(nxt - timedelta(days=1), end)))
        cur = nxt
    return out


def quota_for(rules: dict, facts: dict, on: date) -> Decimal:
    e = rules["entitlement"]
    quota = D(e["yearly_quota"])
    years = tenure_days(facts, on) / 365.25
    for lv in e.get("experience_levels") or []:
        if years >= float(lv["after_years"]):
            quota = D(lv["yearly_quota"])
    pr = e["probation"]
    if facts.get("on_probation") and pr["credit"] == "none":
        return ZERO
    if facts.get("on_probation") and pr["credit"] == "reduced":
        quota = D(pr["reduced_quota"])
    return quota


def _month_starts(a: date, b: date):
    cur = date(a.year, a.month, 1)
    while cur <= b:
        yield cur
        cur = date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)


def proration(rules: dict, facts: dict, ps: date, pe: date) -> Decimal:
    """Share (0..1) of a period the employee should be credited for."""
    p = rules["entitlement"]["proration"]
    doj, exit_ = facts.get("date_of_joining"), facts.get("exit_date")
    if doj and doj > pe:
        return ZERO
    if exit_ and exit_ < ps:
        return ZERO
    start = max(ps, doj) if (doj and p["on_joining"]) else ps
    end = min(pe, exit_) if (exit_ and p["on_exit"]) else pe
    if start == ps and end == pe:
        return Decimal("1")
    if p["basis"] == "days":
        return D((end - start).days + 1) / D((pe - ps).days + 1)
    cutoff = int(p["joining_cutoff_day"] or 0)
    months = list(_month_starts(ps, pe))
    counted = ZERO
    for m in months:
        m_end = date(m.year + (m.month // 12), m.month % 12 + 1, 1) - timedelta(days=1)
        if m_end < start or m > end:
            continue
        if start > m:  # joining month
            if cutoff:
                counted += 1 if start.day <= cutoff else 0
            else:
                counted += D((m_end - start).days + 1) / D((m_end - m).days + 1)
        elif end < m_end:  # exit month
            if cutoff:
                counted += 1 if end.day >= cutoff else 0
            else:
                counted += D((end - m).days + 1) / D((m_end - m).days + 1)
        else:
            counted += 1
    return counted / D(len(months))


def days_worked(db: Session, cal: WorkCalendar, emp, rules: dict, start: date, end: date) -> Decimal:
    """Days worked between two dates for days-worked leave. Uses the attendance module when
    it provides `days_worked(db, employee, start, end)`, else the work calendar less unpaid
    leave (and paid leave or holidays when the policy says they don't count)."""
    if end < start:
        return ZERO
    try:
        from modules.attendance import service as attendance  # type: ignore
        fn = getattr(attendance, "days_worked", None)
        if fn is not None:
            return D(fn(db, emp, start, end))
    except ImportError:
        pass
    dw = rules["entitlement"]["days_worked"]
    total = ZERO
    for x in cal.days(emp, start, end):
        if x["kind"] in ("working", "half"):
            total += x["value"]
        elif x["kind"] == "holiday" and dw["count_holidays"]:
            total += 1
    leave = (db.query(LeaveRequest).filter(LeaveRequest.company_id == emp.company_id, LeaveRequest.employee_id == emp.id,
                                           LeaveRequest.status == "approved", LeaveRequest.from_date <= end,
                                           LeaveRequest.to_date >= start).all())
    for r in leave:
        unpaid_type = r.leave_type.kind == "lop" or policy.rules_on(r.leave_type, r.from_date)["paid"] != "paid"
        for it in r.breakdown or []:
            d = date.fromisoformat(it["date"])
            if it.get("cancelled") or d < start or d > end or it.get("kind") not in ("working", "half"):
                continue
            if unpaid_type:
                total -= D(it["amount"])
            else:
                total -= D(it.get("lop"))
                if not dw["count_paid_leave"]:
                    total -= D(it["amount"]) - D(it.get("lop"))
    return max(total, ZERO)


def _active_employees(db: Session, company_id: int, employee_ids=None):
    Employee = _employee_model()
    q = db.query(Employee).filter(Employee.company_id == company_id)
    if employee_ids:
        q = q.filter(Employee.id.in_(employee_ids))
    else:
        q = q.filter(Employee.active.is_(True))
    return q.order_by(Employee.id).all()


def run_accruals(db: Session, company_id: int, as_of: date, employee_ids: list[int] | None = None,
                 record: bool = True) -> dict:
    """Credit every accrual due on or before `as_of`. Safe to run again (idempotent)."""
    cal = WorkCalendar(db, company_id)
    types = [t for t in policy.list_types(db, company_id, include_inactive=False)]
    emps = _active_employees(db, company_id, employee_ids)
    known = {k for (k,) in db.query(LeaveLedgerEntry.idem_key).filter(
        LeaveLedgerEntry.company_id == company_id, LeaveLedgerEntry.idem_key.like("acc:%")).all()}
    created, by_type = 0, defaultdict(lambda: ZERO)
    for t in types:
        cur_rules = policy.rules_on(t, as_of)
        label, ystart, _ = policy.year_bounds(cur_rules, as_of)
        # the previous year only for credits that fall due at its very end (run early in the new year)
        years = (label - 1, label) if (as_of - ystart).days < 45 else (label,)
        for year in years:
            yr_rules = policy.rules_on(t, policy.year_range(cur_rules, year)[0])
            mode = yr_rules["entitlement"]["mode"]
            if mode not in ("fixed", "accrual", "days_worked"):
                continue
            if mode == "fixed":
                yr_rules = {**yr_rules, "entitlement": {**yr_rules["entitlement"], "frequency": "yearly"}}
            for ps, pe in periods(yr_rules, year):
                rules = policy.rules_on(t, ps)
                if rules["entitlement"]["mode"] != mode:
                    continue
                if mode == "fixed":
                    rules = {**rules, "entitlement": {**rules["entitlement"], "frequency": "yearly", "credit_at": "start"}}
                credit_on = pe if (mode == "days_worked" or rules["entitlement"]["credit_at"] == "end") else ps
                if credit_on > as_of:
                    continue
                version = policy.version_on(t, ps)
                for emp in emps:
                    key = f"acc:{t.id}:{emp.id}:{ps.isoformat()}"
                    if key in known:
                        continue
                    f = cal.facts(emp)
                    if f.get("date_of_joining") and f["date_of_joining"] > pe:
                        continue
                    if f.get("exit_date") and f["exit_date"] < ps:
                        continue
                    check_on = max(ps, f["date_of_joining"]) if f.get("date_of_joining") else ps
                    if not applies(rules["applicability"], f, check_on):
                        continue
                    if mode == "days_worked":
                        amount = _days_worked_credit(db, cal, emp, t, rules, year, ps, pe, f)
                    else:
                        amount = _period_credit(rules, f, ps, pe, credit_on)
                    cap = rules["entitlement"].get("max_balance")
                    if cap is not None and amount > 0:
                        room = D(cap) - balance(db, company_id, emp.id, t, year)
                        amount = max(ZERO, min(amount, room))
                    if amount <= 0:
                        continue
                    add_entry(db, company_id, emp.id, t.id, year, credit_on, "accrual", amount,
                              version_id=version.id if version else None, idem=key, known_keys=known,
                              note=_accrual_note(rules, ps, pe), meta={"period": [ps.isoformat(), pe.isoformat()]})
                    db.flush()
                    created += 1
                    by_type[t.code] += amount
    result = {"entries": created, "by_type": {k: float(v) for k, v in by_type.items()}, "as_of": as_of.isoformat()}
    if record:
        db.add(LeaveRun(company_id=company_id, kind="accrual", label=f"Accrual up to {as_of:%d %b %Y}", as_of=as_of,
                        entries=created, result=result, ran_by_name=_actor()))
        audit.event(db, "leave.accrual_run", "leave_runs", None, result)
    db.flush()
    return result


def _accrual_note(rules: dict, ps: date, pe: date) -> str:
    mode = rules["entitlement"]["mode"]
    if mode == "days_worked":
        return f"Earned for days worked up to {pe:%d %b %Y}"
    return f"Credit for {ps:%d %b %Y} to {pe:%d %b %Y}"


def _period_credit(rules: dict, facts: dict, ps: date, pe: date, credit_on: date) -> Decimal:
    e = rules["entitlement"]
    pr = e["probation"]
    if pr.get("waiting_days") and tenure_days(facts, credit_on) < int(pr["waiting_days"]):
        return ZERO
    quota = quota_for(rules, facts, max(ps, facts.get("date_of_joining") or ps))
    per = quota / D(FREQUENCIES[e["frequency"]])
    share = proration(rules, facts, ps, pe)
    if share >= 1:
        return q2(per)
    return round_to(per * share, e["proration"]["rounding"], e["proration"]["round_mode"])


def _days_worked_credit(db: Session, cal: WorkCalendar, emp, t: LeaveType, rules: dict, year: int,
                        ps: date, pe: date, facts: dict) -> Decimal:
    dw = rules["entitlement"]["days_worked"]
    ystart, _ = policy.year_range(rules, year)
    start = max(ystart, facts.get("date_of_joining") or ystart)
    end = min(pe, facts["exit_date"]) if facts.get("exit_date") else pe
    worked = days_worked(db, cal, emp, rules, start, end)
    if worked < D(dw["min_days_in_year"] or 0):
        return ZERO
    earned = (worked / D(dw["per_days"])).to_integral_value(rounding=ROUND_FLOOR) * D(dw["credit"])
    already = D(db.query(func.coalesce(func.sum(LeaveLedgerEntry.days), 0)).filter(
        LeaveLedgerEntry.company_id == emp.company_id, LeaveLedgerEntry.employee_id == emp.id,
        LeaveLedgerEntry.leave_type_id == t.id, LeaveLedgerEntry.year == year,
        LeaveLedgerEntry.kind == "accrual").scalar())
    return max(ZERO, earned - already)


def remaining_accrual(db: Session, cal: WorkCalendar, emp, t: LeaveType, year: int, today: date) -> Decimal:
    """Estimate of what will still be credited this leave year (for 'up to year-end accrual')."""
    rules = policy.rules_on(t, today)
    e = rules["entitlement"]
    f = cal.facts(emp)
    total = ZERO
    if e["mode"] == "accrual":
        for ps, pe in periods(rules, year):
            credit_on = pe if e["credit_at"] == "end" else ps
            if credit_on > today:
                total += _period_credit(rules, f, ps, pe, credit_on)
    elif e["mode"] == "days_worked":
        _, yend = policy.year_range(rules, year)
        future = cal.working_days(emp, today + timedelta(days=1), yend)
        total = (future / D(e["days_worked"]["per_days"])).to_integral_value(rounding=ROUND_FLOOR) * D(e["days_worked"]["credit"])
    return total


# ── Year end ────────────────────────────────────────────────────────

def year_end(db: Session, company_id: int, year: int, *, apply: bool = False, type_ids: list[int] | None = None,
             employee_ids: list[int] | None = None) -> dict:
    """Close a leave year: carry forward, encash and lapse what is left. With apply=False
    nothing is written (preview)."""
    types = [t for t in policy.list_types(db, company_id) if (not type_ids or t.id in type_ids)]
    Employee = _employee_model()
    names = {e.id: (f"{e.first_name} {e.last_name}".strip(), e.uuid, e.employee_code)
             for e in db.query(Employee).filter(Employee.company_id == company_id).all()}
    rows, created = [], 0
    totals = defaultdict(lambda: ZERO)
    for t in types:
        rules = policy.rules_on(t, policy.year_range(policy.rules_on(t, date(year, 6, 30)), year)[1])
        y = rules["year_end"]
        if not y["reset"] or t.kind in ("lop",):
            continue
        ystart, yend = policy.year_range(rules, year)
        nstart, _ = policy.year_range(rules, year + 1)
        if apply:
            run_expiry(db, company_id, yend + timedelta(days=1), record=False, type_ids=[t.id])
        q = (db.query(LeaveLedgerEntry.employee_id, func.sum(LeaveLedgerEntry.days))
             .filter(LeaveLedgerEntry.company_id == company_id, LeaveLedgerEntry.leave_type_id == t.id,
                     LeaveLedgerEntry.year == year).group_by(LeaveLedgerEntry.employee_id))
        if employee_ids:
            q = q.filter(LeaveLedgerEntry.employee_id.in_(employee_ids))
        done_keys = {k for (k,) in db.query(LeaveLedgerEntry.idem_key).filter(
            LeaveLedgerEntry.company_id == company_id, LeaveLedgerEntry.idem_key.like(f"ye:{t.id}:%:{year}:%")).all()}
        version = policy.version_on(t, yend)
        for emp_id, closing in q.all():
            closing = D(closing)
            if f"ye:{t.id}:{emp_id}:{year}:done" in done_keys:
                continue
            plan = plan_year_end(rules, closing)
            nm = names.get(emp_id, ("", None, None))
            rows.append({"employee_id": emp_id, "employee_name": nm[0], "employee_uuid": str(nm[1]) if nm[1] else None,
                         "employee_code": nm[2], "leave_type": t.code, "leave_type_name": t.name, "color": t.color,
                         "closing": float(closing), **{k: float(v) for k, v in plan.items()}})
            for k, v in plan.items():
                totals[f"{t.code}.{k}"] += v
            if not apply:
                continue
            base = f"ye:{t.id}:{emp_id}:{year}"
            vid = version.id if version else None
            if plan["carry"]:
                add_entry(db, company_id, emp_id, t.id, year, yend, "carried_out", -plan["carry"], idem=f"{base}:co",
                          version_id=vid, note=f"Carried to {policy.year_text(rules, year + 1)}")
                exp = None
                if y["carry_forward"]["expire_after_months"] and plan["carry"] > 0:
                    m = nstart.month - 1 + int(y["carry_forward"]["expire_after_months"])
                    exp = date(nstart.year + m // 12, m % 12 + 1, 1) - timedelta(days=1)
                add_entry(db, company_id, emp_id, t.id, year + 1, nstart, "carry_forward", plan["carry"],
                          idem=f"{base}:cf", version_id=vid, expires_on=exp,
                          note=f"Carried from {policy.year_text(rules, year)}")
            if plan["encash"]:
                add_entry(db, company_id, emp_id, t.id, year, yend, "encashed", -plan["encash"], idem=f"{base}:en",
                          version_id=vid, note="Encashed at year end", meta={"payroll_status": "pending", "reason": "year_end"})
            if plan["lapse"]:
                add_entry(db, company_id, emp_id, t.id, year, yend, "lapsed", -plan["lapse"], idem=f"{base}:la",
                          version_id=vid, note="Lapsed at year end")
            add_entry(db, company_id, emp_id, t.id, year, yend, "opening", 0, idem=f"{base}:done",
                      note="Year closed", meta={"marker": True})
            created += 1
    result = {"year": year, "employees": created if apply else len(rows),
              "totals": {k: float(v) for k, v in totals.items()}}
    if apply:
        db.add(LeaveRun(company_id=company_id, kind="year_end", label=f"Year end {year}", year=year, entries=created,
                        result=result, ran_by_name=_actor()))
        audit.event(db, "leave.year_end_run", "leave_runs", None, result)
        db.flush()
    return {**result, "rows": rows}


def plan_year_end(rules: dict, closing: Decimal) -> dict:
    y = rules["year_end"]
    carry = encash = lapse = ZERO
    if closing < 0:
        carry = closing  # a deficit moves to the next year
        return {"carry": q2(carry), "encash": ZERO, "lapse": ZERO}
    cf = y["carry_forward"]
    if cf["enabled"]:
        carry = min(closing * D(cf["percent"]) / 100, D(cf["max_days"]))
        if cf.get("max_accumulation") is not None:
            carry = min(carry, D(cf["max_accumulation"]))
        carry = q2(carry)
    rest = closing - carry
    en = y["encashment"]
    if en["enabled"] and rest > 0:
        encash = min(rest * D(en["percent"]) / 100, D(en["max_days"]),
                     max(ZERO, closing - carry - D(en["min_balance_after"] or 0)))
        encash = q2(max(encash, ZERO))
    lapse = q2(closing - carry - encash)
    return {"carry": carry, "encash": encash, "lapse": lapse}


def encash_on_exit(db: Session, company_id: int, emp, exit_date: date) -> list[dict]:
    """Encash the balance of every type that allows it on exit (for the final settlement)."""
    out = []
    for t in policy.list_types(db, company_id):
        rules = policy.rules_on(t, exit_date)
        if not rules["year_end"]["encashment"]["on_exit"]:
            continue
        year, _, _ = policy.year_bounds(rules, exit_date)
        bal = balance(db, company_id, emp.id, t, year)
        if bal <= 0:
            continue
        add_entry(db, company_id, emp.id, t.id, year, exit_date, "encashed", -bal, idem=f"exit:{t.id}:{emp.id}",
                  note="Encashed on exit", meta={"payroll_status": "pending", "reason": "exit"})
        out.append({"leave_type": t.code, "days": float(bal)})
    db.flush()
    return out


# ── Expiry ──────────────────────────────────────────────────────────

def run_expiry(db: Session, company_id: int, as_of: date, *, record: bool = True, type_ids=None) -> dict:
    """Expire carried leave and comp off whose date has passed (what is left after leave
    taken, oldest-expiring first)."""
    q = (db.query(LeaveLedgerEntry.employee_id, LeaveLedgerEntry.leave_type_id, LeaveLedgerEntry.year)
         .filter(LeaveLedgerEntry.company_id == company_id, LeaveLedgerEntry.kind.in_(EXPIRING_KINDS),
                 LeaveLedgerEntry.expires_on < as_of).distinct())
    if type_ids:
        q = q.filter(LeaveLedgerEntry.leave_type_id.in_(type_ids))
    types = {t.id: t for t in policy.list_types(db, company_id)}
    created = 0
    seen = set()
    for emp_id, type_id, year in q.all():
        t = types.get(type_id)
        if t is None:
            continue
        key = (emp_id, type_id, year if _resets(t, date(year, 12, 31)) else None)
        if key in seen:
            continue
        seen.add(key)
        for credit, remaining in expiry_plan(_entries(db, company_id, emp_id, t, year), as_of):
            add_entry(db, company_id, emp_id, type_id, credit.year, credit.expires_on, "expired", -remaining,
                      idem=f"exp:{credit.id}", meta={"credit_id": credit.id},
                      note=("Comp off expired" if credit.kind == "comp_off" else "Carried leave expired"))
            created += 1
    db.flush()
    result = {"entries": created, "as_of": as_of.isoformat()}
    if record:
        db.add(LeaveRun(company_id=company_id, kind="expiry", label=f"Expiry up to {as_of:%d %b %Y}", as_of=as_of,
                        entries=created, result=result, ran_by_name=_actor()))
        db.flush()
    return result


# ── Manual changes ──────────────────────────────────────────────────

def adjust(db: Session, company_id: int, emp, t: LeaveType, days, reason: str, *, year: int | None = None,
           on: date | None = None, kind: str = "adjustment") -> LeaveLedgerEntry:
    from core.timeutil import today_for_company_id
    reason = (reason or "").strip()
    if not reason:
        raise BadRequest("Say why the balance is being changed.")
    days = D(days)
    if days == 0:
        raise BadRequest("Enter the number of days to add or take away.")
    if abs(days) > 1000:
        raise BadRequest("That is too many days.")
    on = on or today_for_company_id(db, company_id)
    rules = policy.rules_on(t, on)
    year = year or policy.year_bounds(rules, on)[0]
    row = add_entry(db, company_id, emp.id, t.id, year, on, kind, days, note=reason,
                    version_id=(policy.version_on(t, on).id if policy.version_on(t, on) else None))
    db.flush()
    audit.event(db, "leave.balance_adjusted", "leave_ledger", row.id,
                {"employee_id": emp.id, "leave_type": t.code, "days": float(days), "year": year, "reason": reason},
                entity_uuid=str(row.uuid), entity_label=f"{t.name} for {emp.first_name} {emp.last_name}")
    return row


def import_opening(db: Session, company_id: int, text: str, *, dry_run: bool = False) -> dict:
    """CSV with columns employee (code or email), leave_type (code), days, year (optional),
    note (optional). Each row adds an opening balance; a later file for the same person,
    type and year replaces the earlier opening."""
    from core.timeutil import today_for_company_id
    Employee = _employee_model()
    reader = csv.DictReader(io.StringIO(text.strip().lstrip("﻿")))
    if not reader.fieldnames:
        raise BadRequest("The file is empty.")
    cols = {c.strip().lower(): c for c in reader.fieldnames}
    need = ("employee", "leave_type", "days")
    missing = [c for c in need if c not in cols]
    if missing:
        raise BadRequest("The file needs the columns employee, leave_type and days (year and note are optional).")
    emps = db.query(Employee).filter(Employee.company_id == company_id).all()
    by_key = {}
    for e in emps:
        by_key[e.email.lower()] = e
        if e.employee_code:
            by_key[e.employee_code.lower()] = e
    types = {t.code.upper(): t for t in policy.list_types(db, company_id)}
    today = today_for_company_id(db, company_id)
    ok, errors = 0, []
    for i, raw in enumerate(reader, start=2):
        row = {k: (raw.get(cols[k]) or "").strip() for k in cols}
        emp = by_key.get(row.get("employee", "").lower())
        t = types.get(row.get("leave_type", "").upper())
        if emp is None:
            errors.append({"row": i, "detail": f"No employee with ID or email “{row.get('employee')}”."})
            continue
        if t is None:
            errors.append({"row": i, "detail": f"No leave type with code “{row.get('leave_type')}”."})
            continue
        try:
            days = D(row.get("days"))
        except Exception:
            errors.append({"row": i, "detail": "Days must be a number."})
            continue
        rules = policy.rules_on(t, today)
        try:
            year = int(row["year"]) if row.get("year") else policy.year_bounds(rules, today)[0]
        except ValueError:
            errors.append({"row": i, "detail": "Year must be a number such as 2026."})
            continue
        if dry_run:
            ok += 1
            continue
        old = (db.query(LeaveLedgerEntry).filter(LeaveLedgerEntry.company_id == company_id,
                                                 LeaveLedgerEntry.idem_key == f"open:{t.id}:{emp.id}:{year}").first())
        start, _ = policy.year_range(rules, year)
        if old is not None:
            delta = days - D(old.days)
            if delta != 0:
                add_entry(db, company_id, emp.id, t.id, year, start, "opening", delta,
                          note=row.get("note") or "Opening balance corrected by import")
        else:
            add_entry(db, company_id, emp.id, t.id, year, start, "opening", days, idem=f"open:{t.id}:{emp.id}:{year}",
                      note=row.get("note") or "Opening balance (import)")
        ok += 1
    result = {"imported": ok, "errors": errors[:200], "error_count": len(errors), "dry_run": dry_run}
    if not dry_run:
        db.add(LeaveRun(company_id=company_id, kind="import", label="Opening balances import", entries=ok,
                        result={k: v for k, v in result.items() if k != "errors"}, ran_by_name=_actor()))
        audit.event(db, "leave.opening_imported", "leave_runs", None, {"rows": ok, "errors": len(errors)})
    db.flush()
    return result
