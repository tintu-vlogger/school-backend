"""Exit rules: resignation by the employee with an acceptance workflow and withdrawal,
other exit types each with its flow and letters, notice period (buy-out, waiver,
recovery, pay in lieu), retrenchment and lay-off compensation, handover with delegation
of pending approvals, departmental clearances with owners and due dates, the exit
interview, the full and final settlement handed to payroll with the 2-working-day
deadline, relieving and experience letters, disabling and archiving the record on the
last day, alumni access and rehire eligibility.

Functions other modules call:
    start_exit(db, company_id, employee, exit_type, data) -> Exit
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from modules.offboarding import settings as xsettings
from modules.offboarding.models import Exit, ExitClearance, ExitFlow, ExitHandover, ExitInterview
from modules.recruitment import common as c

MANAGE = "talent.exits.manage"
STATUSES = {"requested": "Waiting for acceptance", "in_progress": "Serving notice", "completed": "Exited",
            "withdrawn": "Withdrawn", "rejected": "Not accepted", "cancelled": "Cancelled"}
LIVE = ("requested", "in_progress")


def can_manage(db: Session, user: dict) -> bool:
    return c.has_perm(db, user, MANAGE)


def require_manage(db: Session, user: dict) -> None:
    if not can_manage(db, user):
        raise Forbidden("Only HR can manage exits.")


def _log(x: Exit, text: str, kind: str = "event", **extra) -> None:
    hist = list(x.history or [])
    hist.append(c.event(kind=kind, text=text, **extra))
    x.history = hist[-300:]
    flag_modified(x, "history")


# ── Flows ───────────────────────────────────────────────────────────

def get_flow(db: Session, company_id: int, uuid) -> ExitFlow:
    f = db.query(ExitFlow).filter(ExitFlow.company_id == company_id, ExitFlow.uuid == uuid).first()
    if f is None:
        raise NotFound("Exit flow")
    return f


def _clean_clearances(db: Session, company_id: int, items) -> list[dict]:
    out, keys = [], set()
    for i, cl in enumerate(items or [], start=1):
        label = (cl.get("label") or "").strip()
        if not label:
            continue
        dept = cl.get("department") or "other"
        if dept not in xsettings.CLEARANCE_DEPTS:
            raise BadRequest(f"Unknown department for “{label}”.")
        ot = cl.get("owner_type") or "role"
        if ot not in ("role", "employee", "reporting_manager"):
            raise BadRequest(f"Pick who clears “{label}”.")
        ov = cl.get("owner_value")
        if ot == "employee":
            if not ov or c.employee(db, company_id, int(ov)) is None:
                raise BadRequest(f"Pick the person who clears “{label}”.")
            ov = int(ov)
        if ot == "role" and not ov:
            ov = "hr"
        key = (cl.get("key") or dept or f"c{i}")[:40]
        while key in keys:
            key = f"{key}{i}"
        keys.add(key)
        out.append({"key": key, "label": label[:150], "department": dept, "owner_type": ot, "owner_value": ov,
                    "due_days": int(cl.get("due_days") or 0),
                    "items": [str(x).strip()[:200] for x in cl.get("items") or [] if str(x).strip()]})
    return out


def save_flow(db: Session, company_id: int, data: dict, f: ExitFlow | None = None) -> ExitFlow:
    f = f or ExitFlow(company_id=company_id)
    if "name" in data:
        f.name = (data["name"] or "").strip()[:150]
    if not f.name:
        raise BadRequest("Name the flow.")
    if "exit_type" in data:
        if data["exit_type"] not in xsettings.EXIT_TYPES:
            raise BadRequest("Unknown exit type.")
        f.exit_type = data["exit_type"]
    if not f.exit_type:
        raise BadRequest("Pick the exit type.")
    if "audience" in data:
        f.audience = c.clean_audience(data["audience"])
    if "priority" in data:
        f.priority = int(data["priority"] or 100)
    if "clearances" in data:
        f.clearances = _clean_clearances(db, company_id, data["clearances"])
    if "letters" in data:
        from modules.documents.service import LETTER_TYPES
        f.letters = [x for x in data["letters"] or [] if x in LETTER_TYPES]
    if "raised_by" in data:
        f.raised_by = [x for x in data["raised_by"] or [] if x in ("employee", "manager", "hr")] or ["hr"]
    for k in ("exit_interview", "needs_approval", "active"):
        if k in data:
            setattr(f, k, bool(data[k]))
    db.add(f)
    db.flush()
    return f


def flow_out(db: Session, f: ExitFlow, counts: dict | None = None) -> dict:
    names = c.names_for(db, f.company_id, [cl["owner_value"] for cl in f.clearances or [] if cl.get("owner_type") == "employee"])
    return {"uuid": str(f.uuid), "name": f.name, "exit_type": f.exit_type,
            "exit_type_label": xsettings.EXIT_TYPES[f.exit_type]["label"], "audience": f.audience or {},
            "priority": f.priority,
            "clearances": [{**cl, "owner_name": names.get(cl["owner_value"]) if cl.get("owner_type") == "employee" else None}
                           for cl in f.clearances or []],
            "letters": f.letters or [], "exit_interview": f.exit_interview, "raised_by": f.raised_by or [],
            "needs_approval": f.needs_approval, "active": f.active, "in_use": (counts or {}).get(f.id, 0)}


def pick_flow(db: Session, company_id: int, emp, exit_type: str) -> ExitFlow | None:
    for f in (db.query(ExitFlow).filter(ExitFlow.company_id == company_id, ExitFlow.exit_type == exit_type,
                                        ExitFlow.active.is_(True)).order_by(ExitFlow.priority, ExitFlow.id).all()):
        if c.audience_match(f.audience, emp):
            return f
    return None


# ── Notice and compensation ─────────────────────────────────────────

def monthly_pay(db: Session, company_id: int, emp, on: date, basis: str = "gross") -> Decimal | None:
    """Monthly gross (or basic) from payroll; None when payroll has no salary for them."""
    try:
        from modules.payroll import service as pay
        s = pay.current_salary(db, company_id, emp.id, on)
    except Exception:
        return None
    if s is None:
        return None
    if basis == "basic":
        for ln in (getattr(s, "components", None) or []):
            if (ln.get("code") or "").upper() in ("BASIC", "BASIC_DA"):
                return Decimal(str(ln.get("monthly")))
    gross = getattr(s, "monthly_gross", None)
    if gross:
        return Decimal(str(gross))
    if getattr(s, "annual_ctc", None):
        return (Decimal(str(s.annual_ctc)) / 12).quantize(Decimal("0.01"))
    return None


def required_notice(db: Session, company_id: int, emp, exit_type: str) -> int:
    conf = xsettings.get(db, company_id)
    if not xsettings.EXIT_TYPES[exit_type]["notice"]:
        return 0
    if exit_type == "retrenchment":
        return int(conf["retrenchment"].get("notice_days") or 30)
    if emp.notice_period_days is not None:
        return int(emp.notice_period_days)
    if emp.status == "probation":
        return int(conf["probation_notice_days"])
    return int(conf["default_notice_days"])


def compute_notice(db: Session, x: Exit, emp) -> None:
    conf = xsettings.get(db, x.company_id)
    req = x.notice_required_days
    served = max(0, (x.last_working_day - x.resignation_date).days)
    short = max(0, req - served)
    x.notice_served_days, x.notice_shortfall_days = served, short
    pay = monthly_pay(db, x.company_id, emp, x.resignation_date, conf.get("notice_rate_basis") or "gross")
    daily = (pay / 30) if pay else None
    x.notice_amount = None
    x.notice_pay_in_lieu = None
    if short == 0 and x.notice_action in ("buyout", "recover", "waive", "pay_in_lieu"):
        x.notice_action = "serve"
    if short and x.notice_action == "serve":
        x.notice_action = "recover" if x.exit_type == "resignation" else "pay_in_lieu"
    if daily is not None and short:
        amount = (daily * short).quantize(Decimal("1"))
        if x.notice_action in ("buyout", "recover"):
            x.notice_amount = amount
        elif x.notice_action == "pay_in_lieu":
            x.notice_pay_in_lieu = amount


def compute_compensation(db: Session, x: Exit, emp, inputs: dict | None = None) -> dict:
    """Retrenchment: 15 days' average pay per completed year (more than 6 months counts as a
    year) plus 15 days' wages to the re-skilling fund; government permission above the
    headcount threshold. Lay-off: a share of basic + DA for each laid-off day."""
    conf = xsettings.get(db, x.company_id)
    inp = {**(x.compensation or {}), **(inputs or {})}
    if x.exit_type not in ("retrenchment", "layoff"):
        return {}
    avg = inp.get("average_monthly_pay")
    if avg in (None, ""):
        avg = monthly_pay(db, x.company_id, emp, x.last_working_day, "basic") or monthly_pay(
            db, x.company_id, emp, x.last_working_day)
    avg = Decimal(str(avg or 0))
    out = {"average_monthly_pay": str(avg)}
    if x.exit_type == "retrenchment":
        rc = conf["retrenchment"]
        start = emp.date_of_joining or x.resignation_date
        end = x.last_working_day
        y = end.year - start.year - ((end.month, end.day) < (start.month, start.day))
        months_rem = ((end.year - start.year) * 12 + end.month - start.month - (1 if end.day < start.day else 0)) - y * 12
        years = y + (1 if months_rem > 6 or (months_rem == 6 and end.day >= start.day) else 0)
        day_wage = avg / 26
        comp = (day_wage * int(rc["days_per_year"]) * years).quantize(Decimal("1"))
        reskill = (day_wage * int(rc["reskilling_days"])).quantize(Decimal("1"))
        headcount = rc.get("headcount") or inp.get("headcount")
        if not headcount:
            E = c.Employee()
            headcount = db.query(func.count(E.id)).filter(E.company_id == x.company_id, E.active.is_(True)).scalar()
        notice_given = x.notice_served_days >= int(rc.get("notice_days") or 30)
        out.update({"years_counted": years, "compensation": str(comp), "reskilling_fund": str(reskill),
                    "notice_pay": "0" if notice_given else str((avg).quantize(Decimal("1"))),
                    "headcount": int(headcount), "permission_threshold": int(rc["permission_threshold"]),
                    "government_permission_needed": int(headcount) >= int(rc["permission_threshold"]),
                    "permission_reference": inp.get("permission_reference"),
                    "seniority_note": "Last come, first go applies within the same category unless reasons are recorded."})
    else:
        days = int(inp.get("layoff_days") or 0)
        pct = Decimal(str(conf.get("layoff_compensation_percent", 50)))
        comp = (avg / 30 * days * pct / 100).quantize(Decimal("1"))
        out.update({"layoff_days": days, "percent": str(pct), "compensation": str(comp)})
    x.compensation = out
    flag_modified(x, "compensation")
    return out


def settlement_due(db: Session, company_id: int, emp, lwd: date) -> date:
    n = int(xsettings.get(db, company_id).get("settlement_working_days") or 2)
    return c.add_working_days(lwd, n, c.is_working_day_fn(db, emp))


# ── Starting an exit ────────────────────────────────────────────────

def get(db: Session, company_id: int, uuid) -> Exit:
    x = (db.query(Exit).options(joinedload(Exit.flow), joinedload(Exit.clearances), joinedload(Exit.handovers))
         .filter(Exit.company_id == company_id, Exit.uuid == uuid).first())
    if x is None:
        raise NotFound("Exit")
    return x


def live_exit(db: Session, company_id: int, employee_id: int) -> Exit | None:
    return (db.query(Exit).filter(Exit.company_id == company_id, Exit.employee_id == employee_id,
                                  Exit.status.in_(LIVE)).first())


def _parse_date(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def start_exit(db: Session, company_id: int, emp, exit_type: str, data: dict, *, raised_by: str | None = None) -> Exit:
    if exit_type not in xsettings.EXIT_TYPES:
        raise BadRequest("Unknown exit type.")
    if emp.status in ("exited", "archived"):
        raise BadRequest("This person has already left.")
    if emp.status == "pre_joining":
        raise BadRequest("This person has not joined yet. Cancel their onboarding instead.")
    if live_exit(db, company_id, emp.id) is not None:
        raise BadRequest("An exit is already in progress for this person.")
    t0 = c.today(db, company_id)
    raised_by = raised_by or data.get("raised_by") or "hr"
    flow = pick_flow(db, company_id, emp, exit_type)
    if flow is not None and raised_by not in (flow.raised_by or ["hr"]) and raised_by != "system":
        if not (raised_by == "hr"):
            raise Forbidden(f"A {xsettings.EXIT_TYPES[exit_type]['label'].lower()} can't be raised by the {raised_by}.")
    resigned = _parse_date(data.get("resignation_date")) or t0
    if resigned > t0 + timedelta(days=1) and exit_type == "resignation":
        raise BadRequest("The resignation date can't be in the future.")
    req = required_notice(db, company_id, emp, exit_type)
    proposed = _parse_date(data.get("proposed_last_day")) or _parse_date(data.get("last_working_day"))
    lwd = proposed or (resigned + timedelta(days=req) if req else resigned)
    if exit_type in ("death", "absconding") and not proposed:
        lwd = resigned
    if lwd < resigned:
        raise BadRequest("The last working day can't be before the resignation date.")
    reason_id = data.get("reason_id")
    reason_text = (data.get("reason_text") or "").strip() or None
    if exit_type == "resignation" and not (reason_id or reason_text):
        raise BadRequest("Pick a reason for leaving.")
    x = Exit(company_id=company_id, employee_id=emp.id, flow_id=flow.id if flow else None, exit_type=exit_type,
             reason_id=reason_id or None, reason_text=reason_text, raised_by=raised_by, raised_by_name=c.actor_name(),
             status="requested", resignation_date=resigned, proposed_last_day=proposed, last_working_day=lwd,
             notice_required_days=req, notice_action=data.get("notice_action") or "serve",
             replacement_needed=bool(data.get("replacement_needed")),
             rehire_eligible=xsettings.get(db, company_id).get("rehire_default", True)
             if exit_type in ("resignation", "contract_end", "retirement", "layoff", "retrenchment") else False)
    x.flow = flow
    db.add(x)
    db.flush()
    from core import numbering
    x.number = numbering.next_number(db, company_id, "exit", t0)
    compute_notice(db, x, emp)
    if exit_type in ("retrenchment", "layoff"):
        compute_compensation(db, x, emp, data.get("compensation") or {})
    _log(x, f"{xsettings.EXIT_TYPES[exit_type]['label']} raised by {c.actor_name()}; proposed last day "
            f"{lwd.strftime('%d %b %Y')}", "start")
    needs_approval = (flow.needs_approval if flow else True)
    if exit_type == "resignation" and raised_by == "employee":
        _submit_workflow(db, x, emp, "offboarding.resignation")
    elif raised_by == "manager" and needs_approval:
        _submit_workflow(db, x, emp, "offboarding.exit")
    else:
        accept(db, x, quiet=False)
    audit.event(db, "exit.started", "exits", x.id, {"type": exit_type, "employee_id": emp.id,
                                                     "last_working_day": lwd.isoformat()},
                company_id=company_id, entity_uuid=str(x.uuid), entity_label=f"{emp.first_name} {emp.last_name}")
    return x


def _submit_workflow(db: Session, x: Exit, emp, key: str) -> None:
    from core.workflow import service as wf
    labels = c.org_labels(db, x.company_id, masters=[x.reason_id])
    payload = {"exit_id": x.id, "employee_name": f"{emp.first_name} {emp.last_name}", "exit_type": x.exit_type,
               "reason": labels["master"].get(x.reason_id) or x.reason_text,
               "resignation_date": x.resignation_date.isoformat(), "last_working_day": x.last_working_day.isoformat(),
               "notice_required_days": x.notice_required_days, "notice_shortfall_days": x.notice_shortfall_days,
               "employee_id": emp.id}
    inst = wf.submit(db, x.company_id, key, payload,
                     f"{xsettings.EXIT_TYPES[x.exit_type]['label']}: {emp.first_name} {emp.last_name}",
                     subject_type="exit", subject_id=x.id)
    x.workflow_instance_id = inst.id


def on_decided(db: Session, instance, status: str) -> None:
    x = db.query(Exit).filter(Exit.company_id == instance.company_id,
                              Exit.id == (instance.payload or {}).get("exit_id")).first()
    if x is None or x.status != "requested":
        return
    if status == "approved":
        accept(db, x)
    elif status == "rejected":
        x.status = "rejected"
        _log(x, f"Not accepted: {instance.result_note or ''}", "decision")
    else:
        x.status, x.withdrawn_at = "withdrawn", c.now()
        _log(x, "Withdrawn", "decision")


def accept(db: Session, x: Exit, quiet: bool = False) -> Exit:
    """Accepted: the person goes on notice, clearances, handover and the exit interview are
    set up, and the settlement deadline is known."""
    from modules.employees import service as people
    emp = c.employee(db, x.company_id, x.employee_id)
    x.status, x.accepted_at = "in_progress", c.now()
    if emp.status in ("probation", "confirmed"):
        people.set_status(db, x.company_id, emp, "notice", reason=xsettings.EXIT_TYPES[x.exit_type]["label"],
                          effective=x.resignation_date,
                          extra={"resignation_date": x.resignation_date, "exit_date": x.last_working_day})
    else:
        emp.resignation_date, emp.exit_date = x.resignation_date, x.last_working_day
    x.settlement_due_date = settlement_due(db, x.company_id, emp, x.last_working_day)
    _make_clearances(db, x, emp)
    _make_interview(db, x, emp)
    _log(x, f"Accepted. Last working day {x.last_working_day.strftime('%d %b %Y')}; settlement due by "
            f"{x.settlement_due_date.strftime('%d %b %Y')}", "accept")
    if not quiet:
        c.notify(db, x.company_id, emp.id, f"Your exit is accepted. Your last working day is "
                                           f"{x.last_working_day.strftime('%d %b %Y')}.", "exit")
        if emp.reporting_manager_id:
            c.notify(db, x.company_id, emp.reporting_manager_id,
                     f"{emp.first_name} {emp.last_name}'s last working day is {x.last_working_day.strftime('%d %b %Y')}. "
                     f"Plan the handover.", "exit")
    if x.replacement_needed:
        c.notify_hr(db, x.company_id, f"Replacement needed for {emp.first_name} {emp.last_name}. Raise a hiring request.",
                    "exit")
    db.flush()
    return x


def _owner_for(db: Session, x: Exit, emp, cl: dict) -> tuple[int | None, str | None]:
    if cl.get("owner_type") == "employee":
        return int(cl["owner_value"]), None
    if cl.get("owner_type") == "reporting_manager":
        if emp.reporting_manager_id:
            return emp.reporting_manager_id, None
        return None, "hr"
    role = cl.get("owner_value") or "hr"
    ids = [i for i in c.role_ids(db, x.company_id, (role,)) if i != emp.id]
    return (ids[0] if ids else None), role


def _make_clearances(db: Session, x: Exit, emp) -> None:
    if x.exit_type == "death":
        defs = [cl for cl in (x.flow.clearances if x.flow else xsettings.DEFAULT_CLEARANCES) if cl["department"] in ("assets", "finance", "hr")]
    else:
        defs = x.flow.clearances if x.flow and x.flow.clearances else xsettings.DEFAULT_CLEARANCES
    have = {cl.key for cl in x.clearances}
    for cl in defs:
        if cl["key"] in have:
            continue
        owner, role = _owner_for(db, x, emp, cl)
        items = [{"label": i, "done": False} for i in cl.get("items") or []]
        if cl["department"] == "assets":
            try:
                from modules.assets import service as assets
                items += [{"label": f"Return {a['name']} ({a['asset_tag']})", "done": False,
                           "asset_assignment": a["assignment_uuid"]} for a in assets.outstanding_assets(db, x.company_id, emp.id)]
            except ImportError:
                pass
        row = ExitClearance(company_id=x.company_id, exit_id=x.id, key=cl["key"], label=cl["label"],
                            department=cl["department"], owner_employee_id=owner, owner_role=role,
                            due_days=int(cl.get("due_days") or 0),
                            due_date=x.last_working_day + timedelta(days=int(cl.get("due_days") or 0)),
                            status="pending", items=items)
        db.add(row)
        x.clearances.append(row)
        if owner:
            c.notify(db, x.company_id, owner, f"{cl['label']} for {emp.first_name} {emp.last_name} is due by "
                                              f"{row.due_date.strftime('%d %b %Y')}.", "exit")
    db.flush()


def _make_interview(db: Session, x: Exit, emp) -> None:
    if x.exit_type in ("death", "absconding"):
        return
    if x.flow is not None and not x.flow.exit_interview:
        return
    if db.query(ExitInterview.id).filter(ExitInterview.exit_id == x.id).first():
        return
    conf = xsettings.get(db, x.company_id)
    hr = c.role_ids(db, x.company_id, ("hr",))
    db.add(ExitInterview(company_id=x.company_id, exit_id=x.id, employee_id=emp.id,
                         interviewer_employee_id=next((i for i in hr if i != emp.id), None),
                         scheduled_on=max(c.today(db, x.company_id),
                                          x.last_working_day - timedelta(days=int(conf["exit_interview_days_before"]))),
                         status="pending"))
    db.flush()


def withdraw(db: Session, user: dict, x: Exit, reason: str | None) -> Exit:
    """The employee may withdraw a resignation until it is accepted; HR can withdraw any
    exit before the last day (the person returns to their earlier stage)."""
    from core.workflow.models import WorkflowInstance
    from modules.employees import service as people
    is_hr = can_manage(db, user)
    if x.status == "requested":
        if not is_hr and user.get("employee_id") != x.employee_id:
            raise Forbidden("You can withdraw only your own resignation.")
        if x.workflow_instance_id:
            inst = db.get(WorkflowInstance, x.workflow_instance_id)
            if inst is not None and inst.status == "pending":
                c.cancel_instance(db, x.company_id, inst, reason or "Withdrawn")
    elif x.status == "in_progress":
        if not is_hr:
            raise BadRequest("Your resignation has been accepted. Ask HR if you want to withdraw it.")
        if x.last_working_day < c.today(db, x.company_id):
            raise BadRequest("The last working day has passed.")
        emp = c.employee(db, x.company_id, x.employee_id)
        if emp.status == "notice":
            back = "probation" if emp.probation_end_date and not emp.confirmation_date else "confirmed"
            people.set_status(db, x.company_id, emp, back, reason="Exit withdrawn")
        emp.exit_date = None
        emp.resignation_date = None
        for cl in x.clearances:
            if cl.status == "pending":
                cl.status = "not_applicable"
                cl.remarks = "Exit withdrawn"
        _cancel_settlement(db, x)
    else:
        raise BadRequest("This exit can no longer be withdrawn.")
    x.status, x.withdrawn_at, x.withdraw_reason = "withdrawn", c.now(), (reason or "").strip() or None
    _log(x, "Withdrawn" + (f": {reason}" if reason else ""), "withdraw")
    return x


def update(db: Session, x: Exit, data: dict) -> Exit:
    if x.status not in LIVE:
        raise BadRequest("This exit is closed.")
    emp = c.employee(db, x.company_id, x.employee_id)
    changed = False
    if data.get("last_working_day"):
        lwd = _parse_date(data["last_working_day"])
        if lwd < x.resignation_date:
            raise BadRequest("The last working day can't be before the resignation date.")
        if lwd != x.last_working_day:
            old = x.last_working_day
            x.last_working_day = lwd
            if x.status == "in_progress":
                emp.exit_date = lwd
                x.settlement_due_date = settlement_due(db, x.company_id, emp, lwd)
                for cl in x.clearances:
                    if cl.status == "pending":
                        cl.due_date = lwd + timedelta(days=cl.due_days or 0)
                iv = db.query(ExitInterview).filter(ExitInterview.exit_id == x.id).first()
                if iv is not None and iv.status == "pending":
                    conf = xsettings.get(db, x.company_id)
                    iv.scheduled_on = lwd - timedelta(days=int(conf["exit_interview_days_before"]))
            _log(x, f"Last working day changed from {old.strftime('%d %b %Y')} to {lwd.strftime('%d %b %Y')}; "
                    f"clearance and interview dates recalculated", "update")
            changed = True
    if "notice_action" in data and data["notice_action"]:
        if data["notice_action"] not in xsettings.NOTICE_ACTIONS:
            raise BadRequest("Unknown notice choice.")
        x.notice_action = data["notice_action"]
        changed = True
    if data.get("notice_required_days") is not None:
        x.notice_required_days = max(0, int(data["notice_required_days"]))
        changed = True
    for k in ("reason_id", "reason_text", "replacement_needed", "rehire_eligible", "rehire_note", "handover_notes"):
        if k in data:
            setattr(x, k, data[k])
    if changed:
        compute_notice(db, x, emp)
        _log(x, f"Notice: {xsettings.NOTICE_ACTIONS[x.notice_action]}"
                + (f", ₹{x.notice_amount or x.notice_pay_in_lieu}" if (x.notice_amount or x.notice_pay_in_lieu) else ""),
             "notice")
    if data.get("compensation") is not None and x.exit_type in ("retrenchment", "layoff"):
        compute_compensation(db, x, emp, data["compensation"])
    if x.settlement_status == "handed_over" and changed:
        _push_settlement(db, x, emp)
    return x


# ── Handover ────────────────────────────────────────────────────────

def add_handover(db: Session, x: Exit, data: dict) -> ExitHandover:
    title = (data.get("title") or "").strip()
    if not title:
        raise BadRequest("Describe what is handed over.")
    kind = data.get("kind") or "task"
    if kind not in ("task", "document", "approvals", "knowledge", "other"):
        raise BadRequest("Unknown kind of handover.")
    to = data.get("to_employee_id")
    if to and (c.employee(db, x.company_id, to) is None or int(to) == x.employee_id):
        raise BadRequest("Pick someone else to take this over.")
    h = ExitHandover(company_id=x.company_id, exit_id=x.id, kind=kind, title=title[:250],
                     details=(data.get("details") or "").strip() or None, to_employee_id=to or None, status="pending")
    db.add(h)
    x.handovers.append(h)
    db.flush()
    if to:
        emp = c.employee(db, x.company_id, x.employee_id)
        c.notify(db, x.company_id, to, f"{emp.first_name} {emp.last_name} is handing over: {h.title}", "exit")
    return h


def set_handover(db: Session, h: ExitHandover, status: str) -> ExitHandover:
    if status not in ("pending", "done"):
        raise BadRequest("Mark it done or pending.")
    h.status, h.done_at = status, (c.now() if status == "done" else None)
    return h


def delegate_approvals(db: Session, x: Exit, to_employee_id: int, reassign_tasks: bool = True) -> dict:
    """Pending approvals of the leaver go to someone else now, and new ones until the last
    day; their open tasks can move too."""
    from core.workflow import service as wf
    from core.workflow.models import WorkflowTask
    to = c.employee(db, x.company_id, to_employee_id)
    if to is None or to.id == x.employee_id or not to.active:
        raise BadRequest("Pick an active colleague.")
    t0 = c.today(db, x.company_id)
    moved = 0
    for task in (db.query(WorkflowTask).filter(WorkflowTask.company_id == x.company_id,
                                               WorkflowTask.approver_employee_id == x.employee_id,
                                               WorkflowTask.status == "pending").all()):
        try:
            with db.begin_nested():
                wf.reassign(db, x.company_id, task, to.id, "Handed over before exit", True)
            moved += 1
        except Exception:
            continue
    try:
        with db.begin_nested():
            wf.add_delegation(db, x.company_id, x.employee_id, to.id, t0, max(t0, x.last_working_day) + timedelta(days=30),
                              None, "Leaving the company")
    except Exception:
        pass
    tasks_moved = 0
    if reassign_tasks:
        from modules.tasks.models import Task
        for t in db.query(Task).filter(Task.company_id == x.company_id, Task.assignee_employee_id == x.employee_id,
                                       Task.status.in_(("todo", "in_progress")),
                                       Task.source_module.is_distinct_from("onboarding")).all():
            t.assignee_employee_id = to.id
            tasks_moved += 1
    x.approvals_delegated_to_id, x.approvals_delegated_at = to.id, c.now()
    x.handover_to_employee_id = x.handover_to_employee_id or to.id
    _log(x, f"{moved} pending approval(s) and {tasks_moved} open task(s) handed to {to.first_name} {to.last_name}",
         "handover")
    c.notify(db, x.company_id, to.id, f"You now handle the pending approvals and tasks of a leaving colleague "
                                      f"({moved} approvals, {tasks_moved} tasks).", "exit")
    return {"approvals": moved, "tasks": tasks_moved}


# ── Clearances ──────────────────────────────────────────────────────

def can_clear(db: Session, user: dict, cl: ExitClearance) -> bool:
    if can_manage(db, user):
        return True
    eid = user.get("employee_id")
    return bool(eid and cl.owner_employee_id == eid)


def update_clearance(db: Session, user: dict, cl: ExitClearance, data: dict) -> ExitClearance:
    if not can_clear(db, user, cl):
        raise Forbidden("This clearance belongs to someone else.")
    x = cl.exit
    if x.status not in LIVE and not (x.status == "completed" and data.get("status") in ("cleared", "not_applicable")):
        raise BadRequest("This exit is closed.")
    if "items" in data and data["items"] is not None:
        by = {i["label"]: i for i in cl.items or []}
        for it in data["items"]:
            if it.get("label") in by:
                by[it["label"]]["done"] = bool(it.get("done"))
        cl.items = list(by.values())
        flag_modified(cl, "items")
    if "remarks" in data:
        cl.remarks = (data["remarks"] or "").strip() or None
    if "recovery_amount" in data:
        cl.recovery_amount = c.money(data["recovery_amount"], "Recovery")
    if data.get("owner_employee_id") and can_manage(db, user):
        cl.owner_employee_id = int(data["owner_employee_id"])
    st = data.get("status")
    if st:
        if st not in ("pending", "cleared", "not_applicable"):
            raise BadRequest("A clearance is pending, cleared or not needed.")
        if st == "cleared":
            if cl.department == "assets":
                try:
                    from modules.assets import service as assets
                    left = assets.outstanding_assets(db, x.company_id, x.employee_id)
                except ImportError:
                    left = []
                if left and not cl.recovery_amount:
                    raise BadRequest(f"{len(left)} asset(s) are not returned yet. Take them back in Assets or enter "
                                     f"the amount to recover.")
            open_items = [i["label"] for i in cl.items or [] if not i.get("done") and not i.get("asset_assignment")]
            if open_items and not data.get("force"):
                raise BadRequest(f"Tick the open items first: {', '.join(open_items[:3])}.")
        if st == "not_applicable" and not (cl.remarks or "").strip():
            raise BadRequest("Say why this clearance is not needed.")
        cl.status = st
        cl.cleared_at = c.now() if st != "pending" else None
        cl.cleared_by = c.actor_name() if st != "pending" else None
        _log(x, f"{cl.label}: {'cleared' if st == 'cleared' else 'not needed' if st == 'not_applicable' else 'reopened'}"
                + (f" (recover ₹{cl.recovery_amount})" if cl.recovery_amount else ""), "clearance")
        if st != "pending" and all(k.status != "pending" for k in x.clearances) and x.settlement_status == "not_started":
            emp = c.employee(db, x.company_id, x.employee_id)
            handover_to_payroll(db, x, emp)
    return cl


def remind_clearance(db: Session, cl: ExitClearance) -> None:
    x = cl.exit
    emp = c.employee(db, x.company_id, x.employee_id)
    msg = f"Reminder: {cl.label} for {emp.first_name} {emp.last_name} is due {cl.due_date.strftime('%d %b %Y')}."
    if cl.owner_employee_id:
        c.notify(db, x.company_id, cl.owner_employee_id, msg, "exit")
    else:
        c.notify_hr(db, x.company_id, msg, "exit")
    cl.reminded_on = c.today(db, x.company_id)


# ── Exit interview ──────────────────────────────────────────────────

def submit_interview(db: Session, user: dict, iv: ExitInterview, data: dict) -> ExitInterview:
    eid = user.get("employee_id")
    if not (can_manage(db, user) or eid in (iv.employee_id, iv.interviewer_employee_id)):
        raise Forbidden("Only the leaver, the interviewer or HR can fill in the exit interview.")
    if iv.status == "submitted" and not can_manage(db, user):
        raise BadRequest("The exit interview has already been submitted.")
    ratings = {}
    for k, v in (data.get("ratings") or {}).items():
        if k in xsettings.RATING_AREAS and v not in (None, ""):
            v = int(v)
            if not 1 <= v <= 5:
                raise BadRequest("Ratings go from 1 to 5.")
            ratings[k] = v
    if len(ratings) < 3:
        raise BadRequest("Rate at least three areas.")
    iv.ratings = ratings
    iv.answers = {q["key"]: (data.get("answers") or {}).get(q["key"], "").strip()[:3000]
                  for q in xsettings.INTERVIEW_QUESTIONS if (data.get("answers") or {}).get(q["key"])}
    if data.get("would_rejoin") not in (None, "yes", "no", "maybe"):
        raise BadRequest("Would you rejoin: yes, no or maybe.")
    iv.would_rejoin = data.get("would_rejoin")
    if data.get("would_recommend") not in (None, ""):
        n = int(data["would_recommend"])
        if not 0 <= n <= 10:
            raise BadRequest("Recommend on a scale of 0 to 10.")
        iv.would_recommend = n
    if data.get("primary_reason_id"):
        iv.primary_reason_id = int(data["primary_reason_id"])
    if "checklist" in data:
        iv.checklist = {k: bool(v) for k, v in (data["checklist"] or {}).items()}
    if "interviewer_notes" in data and (can_manage(db, user) or eid == iv.interviewer_employee_id):
        iv.interviewer_notes = (data["interviewer_notes"] or "").strip() or None
    iv.status, iv.submitted_at = "submitted", c.now()
    x = db.get(Exit, iv.exit_id)
    if x is not None:
        _log(x, "Exit interview submitted", "interview")
    return iv


def interview_out(db: Session, iv: ExitInterview | None) -> dict | None:
    if iv is None:
        return None
    names = c.names_for(db, iv.company_id, [iv.interviewer_employee_id])
    return {"uuid": str(iv.uuid), "status": iv.status, "scheduled_on": iv.scheduled_on,
            "interviewer_employee_id": iv.interviewer_employee_id, "interviewer_name": names.get(iv.interviewer_employee_id),
            "primary_reason_id": iv.primary_reason_id, "ratings": iv.ratings or {}, "would_rejoin": iv.would_rejoin,
            "would_recommend": iv.would_recommend, "answers": iv.answers or {}, "checklist": iv.checklist or {},
            "interviewer_notes": iv.interviewer_notes, "submitted_at": iv.submitted_at,
            "questions": xsettings.INTERVIEW_QUESTIONS, "rating_areas": xsettings.RATING_AREAS}


def interview_analytics(db: Session, company_id: int, start: date | None = None, end: date | None = None) -> dict:
    q = db.query(ExitInterview).filter(ExitInterview.company_id == company_id, ExitInterview.status == "submitted")
    if start:
        q = q.filter(ExitInterview.submitted_at >= start)
    if end:
        q = q.filter(ExitInterview.submitted_at < end + timedelta(days=1))
    rows = q.all()
    areas = {k: [] for k in xsettings.RATING_AREAS}
    rejoin = {"yes": 0, "no": 0, "maybe": 0}
    recs = []
    reasons: dict[int, int] = {}
    for r in rows:
        for k, v in (r.ratings or {}).items():
            if k in areas:
                areas[k].append(v)
        if r.would_rejoin in rejoin:
            rejoin[r.would_rejoin] += 1
        if r.would_recommend is not None:
            recs.append(r.would_recommend)
        if r.primary_reason_id:
            reasons[r.primary_reason_id] = reasons.get(r.primary_reason_id, 0) + 1
    xq = db.query(Exit.reason_id, func.count(Exit.id)).filter(Exit.company_id == company_id,
                                                              Exit.status.in_(("in_progress", "completed")),
                                                              Exit.reason_id.isnot(None)).group_by(Exit.reason_id).all()
    for rid, n in xq:
        reasons[rid] = reasons.get(rid, 0) + 0  # reasons from interviews take priority; keep keys
    exit_reasons = dict(xq)
    labels = c.org_labels(db, company_id, masters=list(reasons) + list(exit_reasons))["master"]
    by_type = dict(db.query(Exit.exit_type, func.count(Exit.id)).filter(Exit.company_id == company_id,
                                                                        Exit.status.in_(("in_progress", "completed")))
                   .group_by(Exit.exit_type).all())
    promoters = sum(1 for x in recs if x >= 9)
    detractors = sum(1 for x in recs if x <= 6)
    return {"responses": len(rows),
            "areas": [{"key": k, "label": xsettings.RATING_AREAS[k], "average": round(sum(v) / len(v), 2) if v else None,
                       "count": len(v)} for k, v in areas.items()],
            "would_rejoin": rejoin,
            "enps": round(100 * (promoters - detractors) / len(recs)) if recs else None,
            "reasons": sorted([{"reason": labels.get(k, "Other"), "count": n} for k, n in exit_reasons.items()],
                              key=lambda r: -r["count"]),
            "by_type": [{"type": k, "label": xsettings.EXIT_TYPES[k]["label"], "count": n} for k, n in by_type.items()]}


# ── Settlement handoff ──────────────────────────────────────────────

PAYROLL_KIND = {"resignation": "resignation", "termination": "termination", "retrenchment": "retrenchment",
                "layoff": "layoff", "contract_end": "contract_end", "retirement": "retirement", "death": "death",
                "absconding": "absconding"}


def _settlement_inputs(x: Exit) -> dict:
    inp = {"notice_required_days": x.notice_required_days}
    if x.notice_action == "waive":
        inp["waive_notice"] = True
    elif x.notice_action in ("recover", "buyout"):
        inp["notice_shortfall_days"] = x.notice_shortfall_days
        if x.notice_action == "buyout":
            inp["notice_buyout_paid"] = True
    elif x.notice_action == "pay_in_lieu":
        inp["notice_pay_days"] = x.notice_shortfall_days
    clear_recovery = sum((cl.recovery_amount or Decimal("0")) for cl in x.clearances if cl.department != "assets")
    if clear_recovery:
        inp["extra_deductions"] = [{"label": "Recovery from clearance", "amount": str(clear_recovery)}]
    if x.exit_type in ("retrenchment", "layoff") and x.compensation:
        inp["compensation"] = x.compensation
        if x.exit_type == "layoff" and x.compensation.get("compensation"):
            inp.setdefault("extra_earnings", []).append({"label": "Lay-off compensation",
                                                         "amount": x.compensation["compensation"]})
    return inp


def _payroll_settlement(db: Session, x: Exit):
    try:
        from modules.payroll.models import FinalSettlement
    except ImportError:
        return None
    return (db.query(FinalSettlement).filter(FinalSettlement.company_id == x.company_id,
                                             FinalSettlement.employee_id == x.employee_id,
                                             FinalSettlement.status != "cancelled")
            .order_by(FinalSettlement.id.desc()).first())


def _push_settlement(db: Session, x: Exit, emp) -> str | None:
    """Create or refresh payroll's settlement. Returns its reference or None when payroll
    can't prepare one yet (for example no salary on record)."""
    try:
        from modules.payroll import settlement as ps
    except ImportError:
        return None
    s = _payroll_settlement(db, x)
    data = {"last_working_day": x.last_working_day, "exit_kind": PAYROLL_KIND.get(x.exit_type, "resignation"),
            "resignation_date": x.resignation_date, "inputs": _settlement_inputs(x)}
    try:
        with db.begin_nested():
            if s is None:
                s = ps.create(db, x.company_id, emp, data)
            elif s.status == "draft":
                ps.update(db, x.company_id, s, data)
    except Exception as exc:  # recorded and retried; never blocks the exit
        _log(x, f"Payroll could not prepare the settlement yet: {getattr(exc, 'detail', 'try again later')}", "settlement")
        return None
    return str(s.uuid) if s is not None else None


def handover_to_payroll(db: Session, x: Exit, emp=None) -> Exit:
    if x.status not in ("in_progress", "completed"):
        raise BadRequest("Accept the exit first.")
    emp = emp or c.employee(db, x.company_id, x.employee_id)
    x.settlement_due_date = x.settlement_due_date or settlement_due(db, x.company_id, emp, x.last_working_day)
    ref = _push_settlement(db, x, emp)
    x.settlement_status = "handed_over"
    x.settlement_handed_at = x.settlement_handed_at or c.now()
    x.settlement_ref = ref or x.settlement_ref
    pend = [k.label for k in x.clearances if k.status == "pending"]
    x.settlement_summary = {"notice_action": x.notice_action, "notice_shortfall_days": x.notice_shortfall_days,
                            "notice_amount": str(x.notice_amount) if x.notice_amount else None,
                            "notice_pay_in_lieu": str(x.notice_pay_in_lieu) if x.notice_pay_in_lieu else None,
                            "recoveries": [{"label": k.label, "amount": str(k.recovery_amount)} for k in x.clearances
                                           if k.recovery_amount],
                            "compensation": x.compensation or {}, "pending_clearances": pend}
    flag_modified(x, "settlement_summary")
    _log(x, f"Handed to payroll for full and final settlement, due by {x.settlement_due_date.strftime('%d %b %Y')}"
            + (" (payroll will finish preparing it once the salary is on record)" if not ref else ""), "settlement")
    from modules.recruitment.common import role_ids
    for eid in role_ids(db, x.company_id, ("hr", "admin")):
        c.notify(db, x.company_id, eid, f"Full and final settlement for {emp.first_name} {emp.last_name} is due by "
                                        f"{x.settlement_due_date.strftime('%d %b %Y')} (2 working days after the last day).",
                 "exit")
    return x


def _cancel_settlement(db: Session, x: Exit) -> None:
    s = _payroll_settlement(db, x)
    if s is not None and s.status == "draft":
        try:
            from modules.payroll import settlement as ps
            ps.cancel(db, x.company_id, s)
        except Exception:
            pass
    x.settlement_status = "not_started"


def settlement_state(db: Session, x: Exit) -> dict:
    """Countdown to the legal deadline in working days, and payroll's status when known."""
    t0 = c.today(db, x.company_id)
    s = _payroll_settlement(db, x)
    if s is not None:
        if s.status == "paid" and x.settlement_status != "paid":
            x.settlement_status, x.settlement_paid_on = "paid", s.paid_on
        elif s.status == "approved" and x.settlement_status == "handed_over":
            x.settlement_status = "approved"
    due = x.settlement_due_date
    out = {"status": x.settlement_status, "due_date": due, "handed_at": x.settlement_handed_at,
           "paid_on": x.settlement_paid_on or (s.paid_on if s is not None else None),
           "payroll": {"uuid": str(s.uuid), "number": s.number, "status": s.status,
                       "net": str(s.net) if s.net is not None else None} if s is not None else None}
    if due:
        emp = c.employee(db, x.company_id, x.employee_id)
        wd = c.working_days_between(t0, due, c.is_working_day_fn(db, emp)) if due != t0 else 0
        paid = out["paid_on"]
        if paid:
            state = "paid_on_time" if paid <= due else "paid_late"
        elif t0 > due:
            state = "overdue"
        elif t0 == due:
            state = "due_today"
        elif t0 >= x.last_working_day:
            state = "running"
        else:
            state = "not_started"
        out.update({"working_days_left": wd, "calendar_days_left": (due - t0).days, "state": state})
    return out


def mark_settlement_paid(db: Session, x: Exit, paid_on: date, ref: str | None) -> Exit:
    """For companies that pay outside payroll: record the payment against the exit."""
    if x.settlement_status == "paid":
        raise BadRequest("Already recorded as paid.")
    x.settlement_status, x.settlement_paid_on = "paid", paid_on
    x.settlement_ref = ref or x.settlement_ref
    late = x.settlement_due_date and paid_on > x.settlement_due_date
    _log(x, f"Settlement paid on {paid_on.strftime('%d %b %Y')}" + (" (late)" if late else " (on time)"), "settlement")
    return x


# ── Letters, completion, alumni ─────────────────────────────────────

def generate_letters(db: Session, x: Exit, types: list[str] | None = None) -> list[dict]:
    from modules.documents import service as documents
    emp = c.employee(db, x.company_id, x.employee_id)
    want = types or (x.flow.letters if x.flow and x.flow.letters else xsettings.EXIT_TYPES[x.exit_type]["letters"])
    have = {l_["type"] for l_ in x.letters or []}
    made = []
    for t in want:
        if t in have:
            continue
        template = documents.default_template(db, x.company_id, t)
        if template is None:
            continue
        values = {"job.last_working_day": x.last_working_day.isoformat(),
                  "letter.reason": x.reason_text or xsettings.EXIT_TYPES[x.exit_type]["label"]}
        with db.begin_nested():
            letter = documents.generate_letter(db, x.company_id, template, emp, values)
        made.append({"type": t, "letter_uuid": str(letter.uuid), "title": letter.title, "letter_id": letter.id,
                     "at": c.now().isoformat()})
    if made:
        x.letters = list(x.letters or []) + made
        flag_modified(x, "letters")
        _log(x, "Letters issued: " + ", ".join(m["title"] for m in made), "letters")
    return made


def complete(db: Session, x: Exit, *, force: bool = False, system: bool = False) -> Exit:
    """On or after the last day: login disabled, licence freed (inactive), record archived
    (never deleted), letters issued, alumni access opened."""
    from modules.employees import service as people
    if x.status != "in_progress":
        raise BadRequest("Only an exit in progress can be completed.")
    t0 = c.today(db, x.company_id)
    if x.last_working_day > t0 and not force:
        raise BadRequest("The last working day has not come yet.")
    pend = [k.label for k in x.clearances if k.status == "pending"]
    if pend and not force and not system:
        raise BadRequest(f"Clearances still open: {', '.join(pend[:4])}. Finish them or complete anyway.")
    emp = c.employee(db, x.company_id, x.employee_id)
    conf = xsettings.get(db, x.company_id)
    if emp.status not in ("exited", "archived"):
        if emp.status not in ("notice", "probation", "confirmed"):
            emp.status = "notice"
        people.set_status(db, x.company_id, emp, "exited", reason=xsettings.EXIT_TYPES[x.exit_type]["label"],
                          effective=x.last_working_day, extra={"exit_date": x.last_working_day})
    emp.active = False
    emp.password_reset_token = None
    x.login_disabled_at = c.now()
    if x.settlement_status == "not_started":
        handover_to_payroll(db, x, emp)
    if x.exit_type != "death":
        try:
            generate_letters(db, x)
        except Exception:
            pass
    if conf.get("archive_on_completion", True):
        people.archive(db, x.company_id, emp, f"Exit {x.number or ''} completed".strip())
        x.archived_at = c.now()
    if x.exit_type not in ("death", "absconding") and conf.get("alumni_access_days", 0):
        x.alumni_until = x.last_working_day + timedelta(days=int(conf["alumni_access_days"]))
        x.alumni_token = x.alumni_token or c.token()
        _send_alumni_link(db, x, emp)
    x.status, x.completed_at = "completed", c.now()
    x.pf_exit_updated = False
    _log(x, "Exit completed: sign-in disabled, record archived" + (" (alumni access open)" if x.alumni_token else ""),
         "complete")
    audit.event(db, "exit.completed", "exits", x.id, {"employee_id": emp.id, "login_disabled": True},
                company_id=x.company_id, entity_uuid=str(x.uuid), entity_label=f"{emp.first_name} {emp.last_name}")
    for eid in c.role_ids(db, x.company_id, ("hr",)):
        c.notify(db, x.company_id, eid, f"Update the date of exit for {emp.first_name} {emp.last_name} on the EPFO portal.",
                 "exit")
    return x


def _send_alumni_link(db: Session, x: Exit, emp) -> None:
    from core.email import send_email
    to = emp.personal_email or emp.email
    if not to:
        return
    link = f"{c.frontend_url(db, x.company_id)}/alumni/{x.alumni_token}"
    send_email(to, "Your alumni access", f"Dear {emp.first_name},\n\nThank you for your time with us. You can download your "
               f"payslips, tax certificates and letters here until {x.alumni_until.strftime('%d %b %Y')}:\n{link}\n\n"
               f"We keep only what the law requires and what you need for these downloads; the link stops working after "
               f"that date.\n\nBest wishes,\nHR team")


def alumni_by_token(db: Session, token: str) -> Exit:
    x = db.query(Exit).filter(Exit.alumni_token == token).first()
    if x is None:
        raise NotFound("Alumni link")
    if x.alumni_until and x.alumni_until < c.today(db, x.company_id):
        raise Forbidden("Your alumni access has ended. Please contact HR.")
    return x


def alumni_out(db: Session, x: Exit) -> dict:
    from modules.documents import service as documents
    from modules.documents.models import DocFile
    from modules.employees.models import JobChange
    emp = c.employee(db, x.company_id, x.employee_id)
    conf = xsettings.get(db, x.company_id)
    comp = c.company(db, x.company_id)
    allowed = set(conf.get("alumni_documents") or [])
    docs = []
    if "letters" in allowed:
        for f in (db.query(DocFile).filter(DocFile.company_id == x.company_id, DocFile.employee_id == emp.id,
                                           DocFile.archived_at.is_(None), DocFile.visible_to_employee.is_(True))
                  .order_by(DocFile.created_at.desc()).limit(100)):
            docs.append({"uuid": str(f.uuid), "title": f.title, "kind": "letter", "date": f.created_at,
                         "file": documents.file_link(db, x.company_id, f.stored_file_id, download=True)})
    payslips = []
    if "payslips" in allowed:
        try:
            from modules.payroll.models import Payslip
            for p in (db.query(Payslip).filter(Payslip.company_id == x.company_id, Payslip.employee_id == emp.id,
                                               Payslip.published.is_(True)).order_by(Payslip.period_start.desc()).limit(36)):
                payslips.append({"uuid": str(p.uuid), "period_start": p.period_start, "period_end": p.period_end,
                                 "net": str(getattr(p, "net_pay", None) or getattr(p, "net", "") or "")})
        except Exception:
            pass
    tax = []
    if "tax" in allowed:
        try:
            from modules.payroll.models import PayrollFile
            for f in (db.query(PayrollFile).filter(PayrollFile.company_id == x.company_id, PayrollFile.employee_id == emp.id,
                                                   PayrollFile.kind.like("form16%")).order_by(PayrollFile.fy.desc())):
                tax.append({"uuid": str(f.uuid), "fy": f.fy, "name": f.filename})
        except Exception:
            pass
        def fy_of(d: date) -> str:
            y = d.year if d.month >= 4 else d.year - 1
            return f"{y}-{str(y + 1)[-2:]}"
        # the financial year of the exit and the one before (when they worked in it)
        last = x.last_working_day
        fys = [fy_of(last)]
        prev = date(last.year - (1 if last.month >= 4 else 2), 6, 30)
        if not emp.date_of_joining or emp.date_of_joining <= date(prev.year + 1, 3, 31):
            fys.append(fy_of(prev))
        have = {t["fy"] for t in tax}
        tax += [{"uuid": None, "fy": fy, "name": f"Form 16 {fy}", "generated": True} for fy in fys if fy not in have]
    timeline = [{"date": j.effective_date, "change": j.change_type, "changes": j.changes or {}}
                for j in db.query(JobChange).filter(JobChange.company_id == x.company_id, JobChange.employee_id == emp.id,
                                                    JobChange.status == "applied").order_by(JobChange.effective_date)]
    labels = c.org_labels(db, x.company_id, designations=[emp.designation_id], departments=[emp.department_id])
    logo = comp.logo_url if comp and comp.logo_url and (comp.logo_url.startswith("data:image/") or comp.logo_url.startswith("http")) else None
    return {"company": {"name": comp.company_name if comp else None, "logo_url": logo, "email": comp.email if comp else None},
            "person": {"name": f"{emp.first_name} {emp.last_name}", "employee_code": emp.employee_code,
                       "designation": labels["designation"].get(emp.designation_id),
                       "department": labels["department"].get(emp.department_id),
                       "date_of_joining": emp.date_of_joining, "last_working_day": x.last_working_day,
                       "rehire_eligible": x.rehire_eligible},
            "access_until": x.alumni_until, "documents": docs, "payslips": payslips, "tax": tax,
            "timeline": [{**t, "changes": {k: v for k, v in t["changes"].items() if k in ("status", "designation_id")}}
                         for t in timeline][-20:],
            "hr_contact": comp.email if comp else None}


# ── Output ──────────────────────────────────────────────────────────

def clearance_out(cl: ExitClearance, names: dict, t0: date) -> dict:
    return {"uuid": str(cl.uuid), "key": cl.key, "label": cl.label, "department": cl.department,
            "department_label": xsettings.CLEARANCE_DEPTS.get(cl.department), "owner_employee_id": cl.owner_employee_id,
            "owner_name": names.get(cl.owner_employee_id) or (cl.owner_role or "").upper() or None,
            "due_date": cl.due_date, "status": cl.status, "items": cl.items or [], "remarks": cl.remarks,
            "recovery_amount": cl.recovery_amount, "cleared_at": cl.cleared_at, "cleared_by": cl.cleared_by,
            "delayed": cl.status == "pending" and cl.due_date < t0,
            "days_late": (t0 - cl.due_date).days if cl.status == "pending" and cl.due_date < t0 else 0}


def out(db: Session, x: Exit, *, emp=None, full: bool = False, labels: dict | None = None) -> dict:
    emp = emp or c.employee(db, x.company_id, x.employee_id)
    t0 = c.today(db, x.company_id)
    labels = labels or c.org_labels(db, x.company_id, departments=[emp.department_id], designations=[emp.designation_id],
                                    masters=[x.reason_id])
    ids = [cl.owner_employee_id for cl in x.clearances] + [h.to_employee_id for h in x.handovers] + \
          [x.handover_to_employee_id, x.approvals_delegated_to_id, emp.reporting_manager_id]
    names = c.names_for(db, x.company_id, ids)
    done = sum(1 for cl in x.clearances if cl.status != "pending")
    o = {"uuid": str(x.uuid), "number": x.number, "employee_id": x.employee_id, "employee_uuid": str(emp.uuid),
         "name": f"{emp.first_name} {emp.last_name}".strip(), "employee_code": emp.employee_code,
         "designation": labels["designation"].get(emp.designation_id),
         "department": labels["department"].get(emp.department_id), "manager_name": names.get(emp.reporting_manager_id),
         "date_of_joining": emp.date_of_joining, "exit_type": x.exit_type,
         "exit_type_label": xsettings.EXIT_TYPES[x.exit_type]["label"], "reason_id": x.reason_id,
         "reason": labels["master"].get(x.reason_id) or x.reason_text, "reason_text": x.reason_text,
         "status": x.status, "status_label": STATUSES.get(x.status), "raised_by": x.raised_by,
         "resignation_date": x.resignation_date, "proposed_last_day": x.proposed_last_day,
         "last_working_day": x.last_working_day, "days_to_last_day": (x.last_working_day - t0).days,
         "notice_required_days": x.notice_required_days, "notice_served_days": x.notice_served_days,
         "notice_shortfall_days": x.notice_shortfall_days, "notice_action": x.notice_action,
         "notice_action_label": xsettings.NOTICE_ACTIONS.get(x.notice_action), "notice_amount": x.notice_amount,
         "notice_pay_in_lieu": x.notice_pay_in_lieu, "clearances_done": done, "clearances_total": len(x.clearances),
         "clearances_delayed": sum(1 for cl in x.clearances if cl.status == "pending" and cl.due_date < t0),
         "settlement": settlement_state(db, x), "rehire_eligible": x.rehire_eligible,
         "login_disabled_at": x.login_disabled_at, "archived_at": x.archived_at, "completed_at": x.completed_at,
         "replacement_needed": x.replacement_needed, "created_at": x.created_at}
    if full:
        iv = db.query(ExitInterview).filter(ExitInterview.exit_id == x.id).first()
        o.update({
            "clearances": [clearance_out(cl, names, t0) for cl in x.clearances],
            "handovers": [{"uuid": str(h.uuid), "kind": h.kind, "title": h.title, "details": h.details,
                           "to_employee_id": h.to_employee_id, "to_name": names.get(h.to_employee_id), "status": h.status,
                           "done_at": h.done_at} for h in x.handovers],
            "handover_to_employee_id": x.handover_to_employee_id, "handover_to_name": names.get(x.handover_to_employee_id),
            "approvals_delegated_to_name": names.get(x.approvals_delegated_to_id),
            "approvals_delegated_at": x.approvals_delegated_at, "handover_notes": x.handover_notes,
            "compensation": x.compensation or {}, "letters": x.letters or [], "interview": interview_out(db, iv),
            "history": list(reversed(x.history or [])), "rehire_note": x.rehire_note, "flow_name": x.flow.name if x.flow else None,
            "alumni_until": x.alumni_until, "withdraw_reason": x.withdraw_reason,
            "alumni_link": f"{c.frontend_url(db, x.company_id)}/alumni/{x.alumni_token}" if x.alumni_token else None,
            "outstanding_assets": _outstanding(db, x),
            "leave_encashment": _encashable(db, x, emp),
            "nominees": _nominees(db, x) if x.exit_type == "death" else [],
        })
        if x.workflow_instance_id:
            from core.workflow.models import WorkflowInstance
            inst = db.get(WorkflowInstance, x.workflow_instance_id)
            o["workflow"] = {"uuid": str(inst.uuid), "status": inst.status} if inst else None
    return o


def _outstanding(db: Session, x: Exit) -> list[dict]:
    try:
        from modules.assets import service as assets
        return assets.outstanding_assets(db, x.company_id, x.employee_id)
    except Exception:
        return []


def _encashable(db: Session, x: Exit, emp) -> list[dict]:
    """Leave that would be encashed in the settlement (preview from the leave module)."""
    try:
        from modules.payroll import adapters
        return [{"code": r["code"], "name": r["name"], "days": str(r["days"])}
                for r in adapters.encashable_leave(db, x.company_id, emp, x.last_working_day)]
    except Exception:
        return []


def _nominees(db: Session, x: Exit) -> list[dict]:
    """On the death of an employee, dues (PF, EDLI, gratuity, unpaid salary, leave pay) go
    to the nominees."""
    from modules.employees.models import Nominee
    rows = db.query(Nominee).filter(Nominee.company_id == x.company_id, Nominee.employee_id == x.employee_id).all()
    labels = c.org_labels(db, x.company_id, masters=[n.relation_id for n in rows])["master"]
    return [{"kind": n.kind, "name": n.name, "relation": labels.get(n.relation_id), "share_percent": str(n.share_percent)}
            for n in rows]


def dashboard(db: Session, company_id: int) -> dict:
    t0 = c.today(db, company_id)
    by = dict(db.query(Exit.status, func.count(Exit.id)).filter(Exit.company_id == company_id).group_by(Exit.status).all())
    leaving30 = db.query(func.count(Exit.id)).filter(Exit.company_id == company_id, Exit.status == "in_progress",
                                                     Exit.last_working_day <= t0 + timedelta(days=30)).scalar()
    delayed = (db.query(func.count(ExitClearance.id)).join(Exit, Exit.id == ExitClearance.exit_id)
               .filter(ExitClearance.company_id == company_id, ExitClearance.status == "pending",
                       ExitClearance.due_date < t0, Exit.status.in_(LIVE + ("completed",))).scalar())
    due = db.query(Exit).filter(Exit.company_id == company_id, Exit.settlement_status.in_(("handed_over", "approved", "not_started")),
                                Exit.status.in_(("in_progress", "completed")), Exit.settlement_due_date.isnot(None),
                                Exit.settlement_due_date <= t0 + timedelta(days=3)).count()
    return {"requested": by.get("requested", 0), "in_progress": by.get("in_progress", 0),
            "completed": by.get("completed", 0), "leaving_in_30_days": leaving30, "delayed_clearances": delayed,
            "settlements_due": due}


# ── Background jobs ─────────────────────────────────────────────────

def run_daily(db: Session) -> int:
    """Retirements started by age, exits closed after the last day (sign-in disabled),
    clearance reminders, settlement handoff on the last day and alumni access expiry."""
    from modules.company.models import Company
    n = 0
    for (cid,) in db.query(Company.id).filter(Company.active.is_(True)).all():
        db.info["company_id"] = cid
        n += _retirements(db, cid)
        t0 = c.today(db, cid)
        conf = xsettings.get(db, cid)
        for x in (db.query(Exit).options(joinedload(Exit.clearances), joinedload(Exit.flow))
                  .filter(Exit.company_id == cid, Exit.status == "in_progress").all()):
            emp = c.employee(db, cid, x.employee_id)
            if x.last_working_day <= t0 and x.settlement_status == "not_started":
                handover_to_payroll(db, x, emp)
                n += 1
            if conf.get("auto_complete_after_last_day", True) and x.last_working_day < t0:
                try:
                    with db.begin_nested():
                        complete(db, x, system=True)
                    n += 1
                except Exception:
                    pass
                continue
            for cl in x.clearances:
                if cl.status == "pending" and cl.due_date <= t0 + timedelta(days=1) and cl.reminded_on != t0:
                    remind_clearance(db, cl)
                    n += 1
        for x in db.query(Exit).filter(Exit.company_id == cid, Exit.alumni_token.isnot(None),
                                       Exit.alumni_until < t0).all():
            x.alumni_token = None
            _log(x, "Alumni access ended", "alumni")
            n += 1
    db.info.pop("company_id", None)
    return n


def retirement_date(dob: date, age: int) -> date:
    """Retirement on the last day of the month in which the person reaches the age."""
    import calendar
    y = dob.year + age
    m = dob.month
    return date(y, m, calendar.monthrange(y, m)[1])


def _retirements(db: Session, company_id: int) -> int:
    conf = xsettings.get(db, company_id)
    t0 = c.today(db, company_id)
    age = int(conf["retirement_age"])
    horizon = t0 + timedelta(days=int(conf["retirement_start_days_before"]))
    E = c.Employee()
    latest_dob = date(horizon.year - age, horizon.month, 28)
    n = 0
    for emp in db.query(E).filter(E.company_id == company_id, E.active.is_(True), E.date_of_birth.isnot(None),
                                  E.status.in_(("probation", "confirmed")), E.date_of_birth <= latest_dob + timedelta(days=4)).all():
        rd = retirement_date(emp.date_of_birth, age)
        if rd > horizon or live_exit(db, company_id, emp.id) is not None:
            continue
        if db.query(Exit.id).filter(Exit.company_id == company_id, Exit.employee_id == emp.id,
                                    Exit.exit_type == "retirement", Exit.status == "cancelled").first():
            continue
        with db.begin_nested():
            start_exit(db, company_id, emp, "retirement", {"resignation_date": t0, "last_working_day": max(rd, t0),
                                                           "reason_text": f"Superannuation at {age}"}, raised_by="system")
        c.notify_hr(db, company_id, f"Retirement of {emp.first_name} {emp.last_name} on {rd.strftime('%d %b %Y')} has been "
                                    f"started automatically.", "exit")
        n += 1
    return n
