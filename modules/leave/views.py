"""Read side of the leave module: who may see what, balance cards, request lists and
details, the team calendar and the admin balances table."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core import context
from core.security import _get_role_permissions
from core.timeutil import iso_utc, today_for_company_id
from modules.leave import ledger, policy
from modules.leave.calendar import WorkCalendar
from modules.leave.facts import applies
from modules.leave.ledger import D, ZERO
from modules.leave.models import CompOffRequest, LeaveLedgerEntry, LeaveRequest, LeaveType
from modules.leave.rules import summary_lines, with_defaults

STATUS_LABEL = {"pending": "Pending", "approved": "Approved", "rejected": "Rejected", "cancelled": "Cancelled",
                "cancel_pending": "Cancellation pending"}


def _employee_model():
    from modules.employees.models import Employee
    return Employee


def f2(x) -> float:
    return float(D(x).quantize(Decimal("0.01")))


# ── Access ──────────────────────────────────────────────────────────

def access(db: Session, user: dict) -> dict:
    perms = set(_get_role_permissions(user["company_id"], user["role"], db))
    return {
        "employee_id": user.get("employee_id"),
        "is_owner": bool(user.get("is_owner")) or not user.get("employee_id"),
        "can_manage": "time.leave.manage" in perms,
        "can_approve": "time.leave.approve" in perms,
        "can_configure": "settings.leave.manage" in perms,
    }


def report_ids(db: Session, company_id: int, manager_id: int, depth: int = 3) -> set[int]:
    Employee = _employee_model()
    out, frontier = set(), {manager_id}
    for _ in range(depth):
        rows = {i for (i,) in db.query(Employee.id).filter(Employee.company_id == company_id,
                                                           Employee.reporting_manager_id.in_(frontier)).all()}
        rows -= out
        if not rows:
            break
        out |= rows
        frontier = rows
    return out


def visible_ids(db: Session, company_id: int, acc: dict, purpose: str = "requests") -> set[int] | None:
    """Employees whose leave this person can see; None means everyone."""
    if acc["can_manage"] or acc["is_owner"]:
        return None
    me = acc["employee_id"]
    ids = {me} | report_ids(db, company_id, me)
    if purpose == "calendar":
        Employee = _employee_model()
        vis = policy.get_settings(db, company_id)["calendar_visibility"]
        emp = db.query(Employee).filter(Employee.id == me).first()
        if vis == "all":
            return None
        if vis == "department" and emp and emp.department_id:
            ids |= {i for (i,) in db.query(Employee.id).filter(Employee.company_id == company_id,
                                                               Employee.department_id == emp.department_id).all()}
        if emp and emp.reporting_manager_id:
            ids.add(emp.reporting_manager_id)
            ids |= {i for (i,) in db.query(Employee.id).filter(Employee.company_id == company_id,
                                                               Employee.reporting_manager_id == emp.reporting_manager_id).all()}
    return ids


# ── Types ───────────────────────────────────────────────────────────

def type_brief(t: LeaveType, rules: dict | None = None) -> dict:
    rules = rules or policy.rules_on(t, date.today())
    return {
        "uuid": str(t.uuid), "id": t.id, "code": t.code, "name": t.name, "color": t.color, "icon": t.icon,
        "kind": t.kind, "description": t.description, "active": t.active,
        "paid": rules["paid"], "mode": rules["entitlement"]["mode"], "unit": rules["unit"],
        "allow_half_day": rules["allow_half_day"], "allow_quarter_day": rules["allow_quarter_day"],
        "allow_hourly": rules["allow_hourly"], "hours_per_day": rules["hours_per_day"],
        "grant_options": rules["grant"]["options"] if rules["entitlement"]["mode"] == "grant" else [],
        "only_optional_holidays": rules["restrictions"]["only_optional_holidays"] or t.kind == "optional_holiday",
        "document_after_days": rules["restrictions"]["document_after_days"],
        "yearly_quota": rules["entitlement"]["yearly_quota"] if rules["entitlement"]["mode"] in ("fixed", "accrual") else None,
        "statutory": rules["statutory"],
    }


def type_full(t: LeaveType) -> dict:
    cur = policy.current_version(t)
    rules = with_defaults(cur.rules if cur else {})
    return {
        **type_brief(t, rules), "is_system": t.is_system, "sort_order": t.sort_order,
        "rules": rules, "summary": summary_lines(rules),
        "current_version": cur.version if cur else None,
        "effective_from": cur.effective_from.isoformat() if cur else None,
        "versions": [{"uuid": str(v.uuid), "version": v.version, "effective_from": v.effective_from.isoformat(),
                      "effective_to": v.effective_to.isoformat() if v.effective_to else None,
                      "change_note": v.change_note, "created_by_name": v.created_by_name,
                      "created_at": iso_utc(v.created_at), "rules": v.rules, "summary": summary_lines(v.rules)}
                     for v in sorted(t.versions, key=lambda v: v.version, reverse=True)],
    }


# ── Balance cards ───────────────────────────────────────────────────

def _card_from_totals(t: LeaveType, rules: dict, year: int, k: dict, booked: Decimal) -> dict:
    g = lambda *names: sum((D(k.get(n)) for n in names), ZERO)  # noqa: E731
    bal = g(*k.keys()) if k else ZERO
    return {
        "year": year, "year_text": policy.year_text(rules, year),
        "opening": f2(g("opening", "carry_forward")), "carried_in": f2(g("carry_forward")),
        "accrued": f2(g("accrual")), "granted": f2(g("grant", "comp_off")),
        "availed": f2(-g("availed", "restored")), "adjusted": f2(g("adjustment")),
        "encashed": f2(-g("encashed")), "lapsed": f2(-g("lapsed", "expired")), "carried_out": f2(-g("carried_out")),
        "balance": f2(bal), "booked": f2(booked), "available": f2(bal - booked),
    }


def my_summary(db: Session, company_id: int, emp, year: int | None = None, include_hidden: bool = False) -> dict:
    today = today_for_company_id(db, company_id)
    cal = WorkCalendar(db, company_id)
    facts = cal.facts(emp)
    types = policy.list_types(db, company_id, include_inactive=False)
    year_of, rules_of = {}, {}
    for t in types:
        r_now = policy.rules_on(t, today)
        y = year if year is not None else policy.year_bounds(r_now, today)[0]
        year_of[t.id] = y
        rules_of[t.id] = policy.rules_on(t, max(policy.year_range(r_now, y)[0], min(today, policy.year_range(r_now, y)[1])))
    totals = ledger.kind_totals(db, company_id, [emp.id], types, year_of)
    pending = (db.query(LeaveRequest).filter(LeaveRequest.company_id == company_id, LeaveRequest.employee_id == emp.id,
                                             LeaveRequest.status == "pending").all())
    booked = defaultdict(lambda: ZERO)
    for r in pending:
        for it in r.breakdown or []:
            if not it.get("cancelled"):
                booked[r.leave_type_id] += D(it["amount"]) - D(it.get("lop"))
    cards = []
    for t in types:
        rules = rules_of[t.id]
        covered = applies(rules["applicability"], facts, today)
        k = totals.get((emp.id, t.id)) or {}
        if not covered and not k:
            continue
        if rules["restrictions"]["hidden_from_employee"] and not include_hidden and not k:
            continue
        card = {"type": type_brief(t, rules), "applicable": covered, "summary": summary_lines(rules),
                **_card_from_totals(t, rules, year_of[t.id], k, booked[t.id])}
        mode = rules["entitlement"]["mode"]
        card["show_balance"] = mode not in ("grant",) and t.kind != "lop"
        if mode in ("accrual", "days_worked") and covered:
            card["to_come"] = f2(ledger.remaining_accrual(db, cal, emp, t, year_of[t.id], today))
        if t.kind == "comp_off" or rules["year_end"]["carry_forward"].get("expire_after_months"):
            soon = [e for e in _entries_for(db, company_id, emp.id, t.id)
                    if e.expires_on and today <= e.expires_on <= today + timedelta(days=30) and e.kind in ledger.EXPIRING_KINDS]
            card["expiring_soon"] = [{"days": f2(e.days), "expires_on": e.expires_on.isoformat()} for e in soon]
        cards.append(card)
    return {
        "employee": {"uuid": str(emp.uuid), "name": f"{emp.first_name} {emp.last_name}".strip(),
                     "date_of_joining": facts["date_of_joining"].isoformat() if facts.get("date_of_joining") else None},
        "today": today.isoformat(), "cards": cards,
        "holiday_calendar": (cal.calendar_for(emp).name if cal.calendar_for(emp) else None),
        "work_week": cal.pattern_name_for(emp),
    }


def _entries_for(db, company_id, emp_id, type_id):
    return (db.query(LeaveLedgerEntry).filter(LeaveLedgerEntry.company_id == company_id,
                                              LeaveLedgerEntry.employee_id == emp_id,
                                              LeaveLedgerEntry.leave_type_id == type_id).all())


# ── Requests ────────────────────────────────────────────────────────

def _emp_brief(e) -> dict | None:
    if e is None:
        return None
    dept = getattr(e, "department", None)
    return {"id": e.id, "uuid": str(e.uuid), "name": f"{e.first_name} {e.last_name}".strip(),
            "employee_code": e.employee_code, "photo": e.profile_image_url if (e.profile_image_url or "").startswith("http") else None,
            "department": dept.department_name if dept else None}


def request_row(req: LeaveRequest, my_tasks: dict[int, str] | None = None, acc: dict | None = None) -> dict:
    t = req.leave_type
    mine = acc is not None and acc.get("employee_id") == req.employee_id
    manage = bool(acc and acc.get("can_manage"))
    return {
        "uuid": str(req.uuid), "reference_no": req.reference_no, "kind": req.kind,
        "employee": _emp_brief(req.employee),
        "leave_type": {"uuid": str(t.uuid), "code": t.code, "name": t.name, "color": t.color, "icon": t.icon},
        "from_date": req.from_date.isoformat(), "to_date": req.to_date.isoformat(), "unit": req.unit, "half": req.half,
        "hours": f2(req.hours) if req.hours is not None else None,
        "days": f2(req.days), "paid_days": f2(req.paid_days), "lop_days": f2(req.lop_days),
        "status": req.status, "status_label": STATUS_LABEL.get(req.status, req.status), "reason": req.reason,
        "on_behalf": req.on_behalf, "created_at": iso_utc(req.created_at),
        "my_task_uuid": (my_tasks or {}).get(req.workflow_instance_id) or (my_tasks or {}).get(req.cancel_instance_id),
        "can_modify": req.status == "pending" and (mine or manage),
        "can_cancel": req.status in ("pending", "approved") and (mine or manage),
        "can_extend": req.status in ("pending", "approved") and req.unit == "day" and (mine or manage),
        "has_attachment": bool(req.attachment),
    }


def my_task_map(db: Session, company_id: int, instance_ids: list[int]) -> dict[int, str]:
    from core.workflow.models import WorkflowTask
    ctx = context.current()
    ids = [i for i in instance_ids if i]
    if not ids:
        return {}
    q = db.query(WorkflowTask.instance_id, WorkflowTask.uuid).filter(
        WorkflowTask.company_id == company_id, WorkflowTask.instance_id.in_(ids), WorkflowTask.status == "pending")
    if ctx.actor_type == "owner":
        q = q.filter(WorkflowTask.approver_kind == "owner")
    elif ctx.employee_id:
        q = q.filter(WorkflowTask.approver_employee_id == ctx.employee_id)
    else:
        return {}
    return {iid: str(u) for iid, u in q.all()}


def list_requests(db: Session, company_id: int, acc: dict, *, scope: str = "mine", status: list[str] | None = None,
                  type_codes: list[str] | None = None, q: str | None = None, start: date | None = None,
                  end: date | None = None, employee_uuid=None, department_id: int | None = None,
                  page: int = 1, size: int = 25) -> dict:
    Employee = _employee_model()
    query = (db.query(LeaveRequest).join(Employee, Employee.id == LeaveRequest.employee_id)
             .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
             .options(joinedload(LeaveRequest.leave_type), joinedload(LeaveRequest.employee).joinedload(Employee.department))
             .filter(LeaveRequest.company_id == company_id))
    if scope == "mine":
        query = query.filter(LeaveRequest.employee_id == (acc["employee_id"] or -1))
    else:
        ids = visible_ids(db, company_id, acc)
        if scope == "team" and acc["employee_id"] and not acc["can_manage"]:
            ids = (ids or set()) - {acc["employee_id"]}
        elif scope == "team" and acc["employee_id"]:
            ids = report_ids(db, company_id, acc["employee_id"])
        if ids is not None:
            query = query.filter(LeaveRequest.employee_id.in_(ids or {-1}))
    if status:
        query = query.filter(LeaveRequest.status.in_(status))
    if type_codes:
        query = query.filter(LeaveType.code.in_(type_codes))
    if employee_uuid:
        query = query.filter(Employee.uuid == employee_uuid)
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    if start:
        query = query.filter(LeaveRequest.to_date >= start)
    if end:
        query = query.filter(LeaveRequest.from_date <= end)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(Employee.first_name.ilike(like), Employee.last_name.ilike(like),
                                 (Employee.first_name + " " + Employee.last_name).ilike(like),
                                 Employee.employee_code.ilike(like), LeaveRequest.reference_no.ilike(like),
                                 LeaveType.name.ilike(like)))
    total = query.order_by(None).count()
    rows = (query.order_by(LeaveRequest.from_date.desc(), LeaveRequest.id.desc())
            .offset((page - 1) * size).limit(size).all())
    tasks = my_task_map(db, company_id, [r.workflow_instance_id for r in rows] + [r.cancel_instance_id for r in rows])
    return {"items": [request_row(r, tasks, acc) for r in rows], "total": total, "page": page, "size": size}


def request_detail(db: Session, company_id: int, req: LeaveRequest, acc: dict) -> dict:
    from core.workflow.models import WorkflowInstance, WorkflowTask
    tasks = my_task_map(db, company_id, [req.workflow_instance_id, req.cancel_instance_id])
    out = request_row(req, tasks, acc)
    out["breakdown"] = req.breakdown
    out["history"] = req.history or []
    out["attachment_name"] = req.attachment_name
    out["grant_option"] = req.grant_option
    out["cancel_dates"] = req.cancel_dates
    out["applied_by_name"] = req.applied_by_name
    out["approvals"] = []
    for iid, label in ((req.workflow_instance_id, "Approval"), (req.cancel_instance_id, "Cancellation")):
        if not iid:
            continue
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == iid).first()
        if inst is None:
            continue
        ts = db.query(WorkflowTask).filter(WorkflowTask.instance_id == iid).order_by(WorkflowTask.id).all()
        out["approvals"].append({
            "label": label, "instance_uuid": str(inst.uuid), "status": inst.status, "reference_no": inst.reference_no,
            "flow": inst.definition.name if inst.definition else None, "created_at": iso_utc(inst.created_at),
            "completed_at": iso_utc(inst.completed_at), "result_note": inst.result_note,
            "tasks": [{"uuid": str(t.uuid), "step_name": t.step_name, "approver_name": t.approver_name, "status": t.status,
                       "comment": t.comment, "decided_at": iso_utc(t.decided_at), "decided_by_name": t.decided_by_name,
                       "created_at": iso_utc(t.created_at), "mine": tasks.get(iid) == str(t.uuid)} for t in ts],
        })
    parent = req.parent
    out["parent"] = {"uuid": str(parent.uuid), "reference_no": parent.reference_no} if parent else None
    children = (db.query(LeaveRequest).filter(LeaveRequest.parent_id == req.id).order_by(LeaveRequest.id).all())
    out["extensions"] = [{"uuid": str(c.uuid), "reference_no": c.reference_no, "from_date": c.from_date.isoformat(),
                          "to_date": c.to_date.isoformat(), "days": f2(c.days), "status": c.status} for c in children]
    return out


# ── Team calendar ───────────────────────────────────────────────────

def team_calendar(db: Session, company_id: int, acc: dict, start: date, end: date, *, department_id: int | None = None,
                  q: str | None = None) -> dict:
    Employee = _employee_model()
    ids = visible_ids(db, company_id, acc, "calendar")
    eq = (db.query(Employee).options(joinedload(Employee.department))
          .filter(Employee.company_id == company_id, Employee.active.is_(True)))
    if ids is not None:
        eq = eq.filter(Employee.id.in_(ids or {-1}))
    if department_id:
        eq = eq.filter(Employee.department_id == department_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        eq = eq.filter(or_(Employee.first_name.ilike(like), Employee.last_name.ilike(like)))
    emps = eq.order_by(Employee.first_name, Employee.last_name).limit(300).all()
    show_type = policy.get_settings(db, company_id)["show_type_on_calendar"] or acc["can_manage"] or acc["can_approve"]
    reqs = (db.query(LeaveRequest).options(joinedload(LeaveRequest.leave_type))
            .filter(LeaveRequest.company_id == company_id, LeaveRequest.employee_id.in_([e.id for e in emps] or [-1]),
                    LeaveRequest.status.in_(("pending", "approved", "cancel_pending")),
                    LeaveRequest.from_date <= end, LeaveRequest.to_date >= start).all())
    cells: dict[int, dict[str, dict]] = defaultdict(dict)
    for r in reqs:
        for it in r.breakdown or []:
            if it.get("cancelled") or D(it.get("amount")) <= 0:
                continue
            d = it["date"]
            if not (start.isoformat() <= d <= end.isoformat()):
                continue
            cells[r.employee_id][d] = {
                "kind": "leave", "code": r.leave_type.code if show_type else "L",
                "name": r.leave_type.name if show_type else "On leave",
                "color": r.leave_type.color if show_type else "#6366f1", "status": r.status,
                "amount": float(it["amount"]), "part": it.get("part"), "request_uuid": str(r.uuid)}
    cal = WorkCalendar(db, company_id)
    people = []
    for e in emps:
        row = {**_emp_brief(e), "cells": cells.get(e.id, {}), "off": {}}
        for x in cal.days(e, start, end):
            if x["kind"] in ("weekend", "holiday", "half"):
                row["off"][x["date"].isoformat()] = {"kind": x["kind"], "holiday": x.get("holiday")}
        people.append(row)
    # company-wide holidays for the header (default calendar)
    default = next((c for c in cal.calendars if c.is_default), None)
    hols = []
    if default:
        for d, hs in sorted(cal.holidays_of(default.id).items()):
            if start <= d <= end:
                hols += [{"date": d.isoformat(), "name": h.name, "kind": h.kind} for h in hs]
    today = today_for_company_id(db, company_id)
    away_today = sum(1 for p in people if today.isoformat() in p["cells"] and p["cells"][today.isoformat()]["status"] != "pending")
    return {"from": start.isoformat(), "to": end.isoformat(), "today": today.isoformat(), "people": people,
            "holidays": hols, "away_today": away_today, "show_type": show_type}


def team_on_leave(db: Session, company_id: int, emp, start: date, end: date) -> list[dict]:
    """Colleagues (same manager or same department) on leave on any of these dates."""
    Employee = _employee_model()
    filters = []
    if emp.reporting_manager_id:
        filters.append(Employee.reporting_manager_id == emp.reporting_manager_id)
        filters.append(Employee.id == emp.reporting_manager_id)
    if emp.department_id:
        filters.append(Employee.department_id == emp.department_id)
    filters.append(Employee.reporting_manager_id == emp.id)
    rows = (db.query(LeaveRequest).join(Employee, Employee.id == LeaveRequest.employee_id)
            .options(joinedload(LeaveRequest.employee), joinedload(LeaveRequest.leave_type))
            .filter(LeaveRequest.company_id == company_id, LeaveRequest.employee_id != emp.id,
                    LeaveRequest.status.in_(("pending", "approved")), LeaveRequest.from_date <= end,
                    LeaveRequest.to_date >= start, or_(*filters)).limit(20).all())
    return [{"name": f"{r.employee.first_name} {r.employee.last_name}".strip(), "from_date": r.from_date.isoformat(),
             "to_date": r.to_date.isoformat(), "status": r.status} for r in rows]


# ── Admin balances ──────────────────────────────────────────────────

def balances_table(db: Session, company_id: int, *, year: int | None, q: str | None, department_id: int | None,
                   page: int, size: int, ids: set[int] | None = None) -> dict:
    Employee = _employee_model()
    today = today_for_company_id(db, company_id)
    types = [t for t in policy.list_types(db, company_id, include_inactive=False) if t.kind != "lop"]
    eq = db.query(Employee).options(joinedload(Employee.department)).filter(Employee.company_id == company_id,
                                                                             Employee.active.is_(True))
    if ids is not None:
        eq = eq.filter(Employee.id.in_(ids or {-1}))
    if q and q.strip():
        like = f"%{q.strip()}%"
        eq = eq.filter(or_(Employee.first_name.ilike(like), Employee.last_name.ilike(like),
                           (Employee.first_name + " " + Employee.last_name).ilike(like), Employee.employee_code.ilike(like)))
    if department_id:
        eq = eq.filter(Employee.department_id == department_id)
    total = eq.count()
    emps = eq.order_by(Employee.first_name, Employee.last_name).offset((page - 1) * size).limit(size).all()
    year_of = {}
    for t in types:
        r = policy.rules_on(t, today)
        year_of[t.id] = year if year is not None else policy.year_bounds(r, today)[0]
    totals = ledger.kind_totals(db, company_id, [e.id for e in emps], types, year_of)
    pend = defaultdict(lambda: ZERO)
    for r in (db.query(LeaveRequest).filter(LeaveRequest.company_id == company_id, LeaveRequest.status == "pending",
                                            LeaveRequest.employee_id.in_([e.id for e in emps] or [-1])).all()):
        pend[(r.employee_id, r.leave_type_id)] += D(r.paid_days)
    items = []
    for e in emps:
        bal = {}
        for t in types:
            k = totals.get((e.id, t.id)) or {}
            s = sum((D(v) for v in k.values()), ZERO)
            bal[t.code] = {"balance": f2(s), "booked": f2(pend[(e.id, t.id)]), "has_entries": bool(k)}
        items.append({**_emp_brief(e), "balances": bal})
    return {"items": items, "total": total, "page": page, "size": size,
            "types": [{"uuid": str(t.uuid), "code": t.code, "name": t.name, "color": t.color,
                       "year": year_of[t.id], "mode": policy.rules_on(t, today)["entitlement"]["mode"]} for t in types]}


def ledger_rows(db: Session, company_id: int, emp_id: int, type_id: int | None, year: int | None) -> list[dict]:
    q = (db.query(LeaveLedgerEntry, LeaveType).join(LeaveType, LeaveType.id == LeaveLedgerEntry.leave_type_id)
         .filter(LeaveLedgerEntry.company_id == company_id, LeaveLedgerEntry.employee_id == emp_id))
    if type_id:
        q = q.filter(LeaveLedgerEntry.leave_type_id == type_id)
    if year:
        q = q.filter(LeaveLedgerEntry.year == year)
    out = []
    for e, t in q.order_by(LeaveLedgerEntry.entry_date.desc(), LeaveLedgerEntry.id.desc()).limit(500).all():
        if (e.meta or {}).get("marker"):
            continue
        out.append({"uuid": str(e.uuid), "date": e.entry_date.isoformat(), "kind": e.kind, "days": f2(e.days),
                    "year": e.year, "note": e.note, "expires_on": e.expires_on.isoformat() if e.expires_on else None,
                    "leave_type": {"code": t.code, "name": t.name, "color": t.color},
                    "created_by_name": e.created_by_name, "created_at": iso_utc(e.created_at)})
    return out


def comp_off_rows(db: Session, company_id: int, acc: dict, scope: str, page: int, size: int) -> dict:
    Employee = _employee_model()
    q = (db.query(CompOffRequest).options(joinedload(CompOffRequest.employee))
         .filter(CompOffRequest.company_id == company_id))
    if scope == "mine":
        q = q.filter(CompOffRequest.employee_id == (acc["employee_id"] or -1))
    else:
        ids = visible_ids(db, company_id, acc)
        if ids is not None:
            q = q.filter(CompOffRequest.employee_id.in_(ids or {-1}))
    total = q.count()
    rows = q.order_by(CompOffRequest.worked_date.desc(), CompOffRequest.id.desc()).offset((page - 1) * size).limit(size).all()
    tasks = my_task_map(db, company_id, [r.workflow_instance_id for r in rows])
    del Employee
    return {"items": [{"uuid": str(r.uuid), "employee": _emp_brief(r.employee), "worked_date": r.worked_date.isoformat(),
                       "day_kind": r.day_kind, "unit": r.unit, "days": f2(r.days), "reason": r.reason, "source": r.source,
                       "status": r.status, "expires_on": r.expires_on.isoformat() if r.expires_on else None,
                       "created_at": iso_utc(r.created_at), "my_task_uuid": tasks.get(r.workflow_instance_id)}
                      for r in rows], "total": total, "page": page, "size": size}
