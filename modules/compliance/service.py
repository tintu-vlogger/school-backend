"""The compliance dashboard: what is due per legal entity and state, what is done, pending
or late, and the people-related risks (minimum wage, missing IDs, settlements near the
2-day deadline, overtime near the legal limit, pending policy acknowledgements), with
plain-language guidance. Data comes from the other modules' public service functions."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest
from core.timeutil import today_for_company_id
from modules.compliance.guidance import ANNUAL, GUIDANCE, NOTICES
from modules.compliance.models import ComplianceRecord
from modules.reports.registry import Principal

logger = logging.getLogger("hrms")
VIEW = "insights.compliance.view"


def _month_add(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _records(db: Session, company_id: int) -> dict[str, ComplianceRecord]:
    return {r.key: r for r in db.query(ComplianceRecord).filter(ComplianceRecord.company_id == company_id).all()}


def _entities(db: Session, company_id: int) -> list:
    from modules.organization.models import LegalEntity
    return db.query(LegalEntity).filter(LegalEntity.company_id == company_id).order_by(
        LegalEntity.is_primary.desc(), LegalEntity.name).all()


def _states(db: Session, company_id: int) -> dict[int, tuple[str, str]]:
    """state master id -> (code, name)."""
    from core.masters import MasterType, MasterValue
    t = db.query(MasterType).filter(MasterType.company_id == company_id, MasterType.key == "state").first()
    if t is None:
        return {}
    return {v.id: ((v.code or "").upper(), v.label) for v in db.query(MasterValue).filter(MasterValue.type_id == t.id)}


def _status(due: date, today: date, done: bool, na: bool = False) -> str:
    if na:
        return "not_applicable"
    if done:
        return "done"
    return "late" if due < today else "pending"


def _payroll_items(db: Session, company_id: int, today: date) -> list[dict]:
    try:
        from modules.payroll import compliance as payroll_compliance
    except Exception:
        return []
    try:
        with db.begin_nested():
            rows = payroll_compliance.calendar(db, company_id, _month_add(today.replace(day=1), -2), 4)
    except Exception as exc:
        logger.warning("Payroll compliance calendar unavailable: %s", exc)
        return []
    out = []
    for r in rows:
        st = {"paid": "done", "filed": "done", "not_applicable": "not_applicable"}.get(r["status"], r["status"])
        out.append({"key": f"payroll:{r['kind']}:{r['legal_entity_id']}:{r['state']}:{r['period']}", "kind": r["kind"],
                    "label": r["label"], "legal_entity_id": r["legal_entity_id"] or None, "state": r["state"] or None,
                    "period": r["period"], "due_date": r["due_date"], "status": st, "amount": r.get("amount"),
                    "reference": r.get("reference"), "done_on": r.get("paid_on"), "source": "payroll",
                    "record_uuid": r.get("uuid"), "guidance": GUIDANCE.get(r["kind"], ""),
                    "action": "Record it in Payroll → Statutory"})
    return out


def _own_items(db: Session, company_id: int, today: date, recs: dict) -> list[dict]:
    items = []
    # monthly registers for the last two months
    for back in (2, 1):
        m = _month_add(today.replace(day=1), -back)
        key = f"registers:{m:%Y-%m}"
        due = _month_add(m, 1).replace(day=7)
        r = recs.get(key)
        items.append({"key": key, "kind": "registers", "label": f"Statutory registers for {m:%B %Y}",
                      "legal_entity_id": None, "state": None, "period": f"{m:%Y-%m}", "due_date": due.isoformat(),
                      "status": _status(due, today, r is not None and r.status == "done",
                                        r is not None and r.status == "not_applicable"),
                      "done_on": r.done_on.isoformat() if r and r.done_on else None,
                      "reference": r.reference if r else None, "source": "compliance", "markable": True,
                      "guidance": GUIDANCE["registers"]})
    # annual filings: show the one for last year until done, and from 60 days before the due date
    for a in ANNUAL:
        for year in (today.year - 1, today.year):
            due = date(year + 1, a["month"], a["day"])
            if (due - today).days > 60:
                continue
            key = f"{a['kind']}:{year}"
            r = recs.get(key)
            if r is not None and (today - due).days > 90:
                continue
            items.append({"key": key, "kind": a["kind"], "label": f"{a['label']} for {year}",
                          "legal_entity_id": None, "state": None, "period": str(year), "due_date": due.isoformat(),
                          "status": _status(due, today, r is not None and r.status == "done",
                                            r is not None and r.status == "not_applicable"),
                          "done_on": r.done_on.isoformat() if r and r.done_on else None,
                          "reference": r.reference if r else None, "source": "compliance", "markable": True,
                          "guidance": GUIDANCE[a["kind"]]})
    # licence and registration renewals from the legal entities
    states = _states(db, company_id)
    labels = {"pt": "Professional Tax registration", "lwf": "Labour Welfare Fund registration",
              "shops": "Shops and Establishments registration"}
    for le in _entities(db, company_id):
        for reg in le.registrations or []:
            vt = reg.get("valid_to")
            if not vt:
                continue
            try:
                due = date.fromisoformat(str(vt)[:10])
            except ValueError:
                continue
            if (due - today).days > 90:
                continue
            code, name = states.get(reg.get("state_id"), ("", ""))
            key = f"licence:{le.id}:{reg.get('kind')}:{code}:{due.isoformat()}"
            r = recs.get(key)
            items.append({"key": key, "kind": "licence",
                          "label": f"Renew {labels.get(reg.get('kind'), 'registration')}" + (f" · {name}" if name else ""),
                          "legal_entity_id": le.id, "state": code or None, "period": due.isoformat(),
                          "due_date": due.isoformat(), "reference": r.reference if r else reg.get("number"),
                          "status": _status(due, today, r is not None and r.status == "done"),
                          "done_on": r.done_on.isoformat() if r and r.done_on else None, "source": "compliance",
                          "markable": True, "guidance": GUIDANCE["licence"]})
    return items


# ── People risks ───────────────────────────────────────────────────

def _people(db: Session, company_id: int, entity_id: int | None, state: str | None):
    from modules.employees.models import Employee
    from modules.organization.models import Location
    q = db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                  Employee.status.in_(("probation", "confirmed", "notice")))
    if entity_id:
        q = q.filter(Employee.legal_entity_id == entity_id)
    emps = q.all()
    if state:
        states = _states(db, company_id)
        locs = {l.id: states.get(l.state_id, ("", ""))[0] for l in db.query(Location).filter(
            Location.company_id == company_id).all()}
        emps = [e for e in emps if locs.get(e.location_id) == state.upper()]
    return emps


def _person(e, detail: str) -> dict:
    return {"uuid": str(e.uuid), "name": f"{e.first_name} {e.last_name}".strip(), "code": e.employee_code,
            "detail": detail}


def _missing_ids(db: Session, company_id: int, emps: list) -> dict:
    from modules.employees.models import BankAccount
    have_bank = {i for (i,) in db.query(BankAccount.employee_id).filter(
        BankAccount.company_id == company_id, BankAccount.active.is_(True)).all()}
    pf = {}
    try:
        from modules.payroll import service as payroll
        pf = {i: p for i, p in payroll.profiles_for(db, company_id, [e.id for e in emps]).items()}
    except Exception:
        pass
    people = []
    for e in emps:
        missing = []
        if not e.pan:
            missing.append("PAN")
        prof = pf.get(e.id)
        if not e.uan and (prof is None or prof.pf_applicable):
            missing.append("UAN")
        if e.id not in have_bank and (prof is None or prof.payment_mode in (None, "bank")):
            missing.append("bank account")
        if missing:
            people.append(_person(e, "Missing " + ", ".join(missing)))
    return {"key": "missing_ids", "title": "Missing PAN, UAN or bank details", "severity": "medium",
            "count": len(people), "people": people[:100], "guidance": GUIDANCE["missing_ids"],
            "link": "/dashboard/reports?standard=missing_statutory"}


def _min_wage(db: Session, company_id: int, emps: list, today: date) -> dict:
    people = []
    try:
        from modules.payroll.models import PayRun, Payslip
        ids = {e.id: e for e in emps}
        since = _month_add(today.replace(day=1), -2)
        latest: dict[int, Payslip] = {}
        for ps in (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
                   .filter(Payslip.company_id == company_id, Payslip.period_start >= since,
                           PayRun.status != "cancelled", PayRun.kind == "regular")
                   .order_by(Payslip.period_start).all()):
            latest[ps.employee_id] = ps
        for eid, ps in latest.items():
            w = next((x for x in ps.warnings or [] if x.get("code") == "below_min_wage"), None)
            if w and eid in ids:
                people.append(_person(ids[eid], f"{ps.period_start:%b %Y}: {w.get('message')}"))
    except Exception as exc:
        logger.warning("Minimum wage check unavailable: %s", exc)
    return {"key": "min_wage", "title": "Paid below minimum wage", "severity": "high", "count": len(people),
            "people": people, "guidance": GUIDANCE["min_wage"], "link": "/dashboard/payroll"}


def _settlements(db: Session, company_id: int, emps_all: dict, today: date) -> dict:
    people = []
    try:
        from modules.payroll import settlement
        from modules.payroll.models import FinalSettlement
        for s in db.query(FinalSettlement).filter(FinalSettlement.company_id == company_id,
                                                  FinalSettlement.status.notin_(("paid", "cancelled"))).all():
            c = settlement.countdown(s, today)
            if c["state"] in ("overdue", "due_today", "due_soon") or c["days_left"] <= 2:
                e = emps_all.get(s.employee_id)
                if e is None:
                    continue
                when = ("overdue by %d day(s)" % -c["days_left"]) if c["days_left"] < 0 else (
                    "due today" if c["days_left"] == 0 else f"due in {c['days_left']} day(s)")
                people.append(_person(e, f"Last day {s.last_working_day:%d %b}; payment {when}"))
    except Exception as exc:
        logger.warning("Settlement check unavailable: %s", exc)
    return {"key": "settlement", "title": "Final settlements near the 2-day deadline", "severity": "high",
            "count": len(people), "people": people, "guidance": GUIDANCE["settlement"],
            "link": "/dashboard/settlements"}


def _overtime(db: Session, company_id: int, emps: list, today: date) -> dict:
    people = []
    try:
        from modules.attendance import service as attendance
        q_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        with db.begin_nested():
            rep = attendance.overtime_report(db, company_id, [e.id for e in emps], q_start, today)
        for r in rep["rows"]:
            if r["alert"] in ("near", "over"):
                e = next((x for x in emps if x.id == r["employee_id"]), None)
                if e:
                    people.append(_person(e, f"{r['quarter_hours']} of {r['quarter_cap_hours']} hours this quarter"
                                          + (" (over the limit)" if r["alert"] == "over" else "")))
    except Exception as exc:
        logger.warning("Overtime check unavailable: %s", exc)
    return {"key": "overtime", "title": "Overtime near the legal limit", "severity": "medium", "count": len(people),
            "people": people, "guidance": GUIDANCE["overtime"], "link": "/dashboard/attendance/reports"}


def _policy_acks(db: Session, company_id: int, emps: list, today: date) -> dict:
    people = []
    try:
        from modules.documents.models import Policy, PolicyAcknowledgement
        ids = {e.id: e for e in emps}
        rows = (db.query(PolicyAcknowledgement, Policy.title).join(Policy, Policy.id == PolicyAcknowledgement.policy_id)
                .filter(PolicyAcknowledgement.company_id == company_id, PolicyAcknowledgement.acknowledged_at.is_(None),
                        Policy.active.is_(True)).all())
        per = defaultdict(list)
        for a, title in rows:
            if a.employee_id in ids:
                per[a.employee_id].append((title, a.due_on))
        for eid, items in per.items():
            overdue = [t for t, d in items if d and d < today]
            detail = f"{len(items)} pending" + (f", {len(overdue)} overdue" if overdue else "")
            people.append(_person(ids[eid], f"{detail}: " + ", ".join(t for t, _ in items[:3])))
    except Exception as exc:
        logger.warning("Policy acknowledgement check unavailable: %s", exc)
    return {"key": "policy_ack", "title": "Policy acknowledgements pending", "severity": "low", "count": len(people),
            "people": people[:100], "guidance": GUIDANCE["policy_ack"], "link": "/dashboard/documents"}


def _notices(db: Session, company_id: int, today: date, recs: dict) -> list[dict]:
    out = []
    for n in NOTICES:
        key = f"notice:{n['id']}"
        out.append({**n, "key": key, "read": key in recs, "source": "Campusly"})
    try:
        from modules.payroll import statutory
        for r in statutory.library_rows(db):
            eff = r.effective_from
            if eff and abs((eff - today).days) <= 120:
                key = f"notice:rule-{r.id}"
                scope = f" ({r.scope})" if r.scope else ""
                out.append({"id": f"rule-{r.id}", "key": key, "date": eff.isoformat(), "read": key in recs,
                            "title": f"{r.kind.replace('_', ' ').upper() if len(r.kind) <= 4 else r.kind.replace('_', ' ').capitalize()}"
                                     f"{scope} rules {'change' if eff > today else 'changed'} on {eff:%d %b %Y}",
                            "body": r.note or "The payroll rules were updated in the statutory library and apply "
                                              "from this date.",
                            "tags": [r.kind], "source": r.source or "Statutory library"})
    except Exception as exc:
        logger.warning("Statutory library unavailable: %s", exc)
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def dashboard(db: Session, p: Principal, *, entity_id: int | None = None, state: str | None = None) -> dict:
    from modules.employees.models import Employee
    cid = p.company_id
    today = today_for_company_id(db, cid)
    recs = _records(db, cid)
    items = _payroll_items(db, cid, today) + _own_items(db, cid, today, recs)
    if entity_id:
        items = [i for i in items if not i.get("legal_entity_id") or i["legal_entity_id"] == entity_id]
    if state:
        items = [i for i in items if not i.get("state") or i["state"] == state.upper()]
    for i in items:
        d = date.fromisoformat(i["due_date"])
        i["days_left"] = (d - today).days
    items.sort(key=lambda i: ({"late": 0, "pending": 1, "done": 2, "not_applicable": 3}.get(i["status"], 4),
                              i["due_date"]))
    emps = _people(db, cid, entity_id, state)
    all_emps = {e.id: e for e in db.query(Employee).filter(Employee.company_id == cid).all()}
    alerts = [_min_wage(db, cid, emps, today), _settlements(db, cid, all_emps, today),
              _missing_ids(db, cid, emps), _overtime(db, cid, emps, today), _policy_acks(db, cid, emps, today)]
    entities = _entities(db, cid)
    states = _states(db, cid)
    used_states = sorted({i["state"] for i in items if i.get("state")} | set())
    from modules.organization.models import Location
    for l in db.query(Location).filter(Location.company_id == cid).all():
        code = states.get(l.state_id, ("", ""))[0]
        if code and code not in used_states:
            used_states.append(code)
    names = {c: n for c, n in states.values()}
    ent_names = {e.id: e.name for e in entities}
    for i in items:
        i["legal_entity"] = ent_names.get(i.get("legal_entity_id")) if i.get("legal_entity_id") else None
        i["state_name"] = names.get(i["state"]) if i.get("state") else None
    summary = {
        "late": sum(1 for i in items if i["status"] == "late"),
        "due_soon": sum(1 for i in items if i["status"] == "pending" and i["days_left"] <= 7),
        "pending": sum(1 for i in items if i["status"] == "pending"),
        "done": sum(1 for i in items if i["status"] == "done"),
        "risks": sum(a["count"] for a in alerts if a["severity"] in ("high", "medium")),
    }
    return {"today": today.isoformat(), "summary": summary, "items": items, "alerts": alerts,
            "notices": _notices(db, cid, today, recs),
            "filters": {"entities": [{"value": e.id, "label": e.name} for e in entities],
                        "states": [{"value": c, "label": names.get(c, c)} for c in sorted(set(used_states))]},
            "has_payroll": any(i["source"] == "payroll" for i in items)}


def mark(db: Session, p: Principal, key: str, data: dict) -> ComplianceRecord:
    key = (key or "").strip()
    if not key or ":" not in key or key.startswith("payroll:"):
        raise BadRequest("Payments and returns are recorded in Payroll → Statutory.")
    status = data.get("status") or "done"
    if status not in ("done", "not_applicable", "read", "pending"):
        raise BadRequest("Choose done or not needed.")
    r = db.query(ComplianceRecord).filter(ComplianceRecord.company_id == p.company_id,
                                          ComplianceRecord.key == key).first()
    if status == "pending":
        if r is not None:
            db.delete(r)
            audit.event(db, "compliance.reopened", "compliance_records", r.id, {"key": key})
        return r
    if r is None:
        r = ComplianceRecord(company_id=p.company_id, key=key[:120])
        db.add(r)
    r.status = status
    r.done_on = date.fromisoformat(data["done_on"]) if data.get("done_on") else (
        today_for_company_id(db, p.company_id) if status == "done" else None)
    r.reference = (data.get("reference") or "").strip()[:120] or None
    r.notes = (data.get("notes") or "").strip() or None
    r.by_name = p.name or "Company owner"
    db.flush()
    return r

