"""Statutory API under /api/v1/payroll/statutory, plus the platform statutory library.

GET  /payroll/statutory/summary?month=YYYY-MM     PF, ESI, PT, LWF, TDS for a month (from its pay runs)
GET  /payroll/statutory/files/{kind}?month=        ecr, ecr_arrears, esi, pt, lwf for the month's regular run
GET  /payroll/statutory/calendar?from=&months=     compliance calendar with challan status
POST /payroll/statutory/challans                   record a payment or filing (reference)
GET  /payroll/statutory/registers/bonus?fy=, /registers/gratuity?on=, /registers/wages?month=
Platform (platform admin token):
GET  /platform/statutory/rules, POST /platform/statutory/rules (new version), PUT /platform/statutory/rules/{uuid}
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import audit
from core.db import get_db
from core.errors import BadRequest, NotFound
from core.security import require_permission
from core.timeutil import today_for_company_id
from modules.payroll import calc, compliance, exports, statutory
from modules.payroll.calc import ZERO, d, money
from modules.payroll.models import PayRun, Payslip, StatutoryRule

router = APIRouter(prefix="/payroll/statutory", tags=["Payroll statutory"])
platform_router = APIRouter(prefix="/platform/statutory", tags=["Platform statutory library"])

VIEW = require_permission("payroll.view")
STAT = require_permission("payroll.statutory.manage")


def _file(name: str, media: str, content: bytes) -> Response:
    return Response(content=content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _month(v: str | None, db: Session, cid: int) -> date:
    if not v:
        t = today_for_company_id(db, cid)
        return calc.add_months(t.replace(day=1), -1)
    try:
        y, m = (int(x) for x in v.split("-")[:2])
        return date(y, m, 1)
    except ValueError:
        raise BadRequest("Months look like 2026-11.")


def _regular_run(db: Session, cid: int, month: date) -> PayRun:
    r = (db.query(PayRun).filter(PayRun.company_id == cid, PayRun.kind == "regular", PayRun.period_start == month,
                                 PayRun.status.in_(("approved", "locked", "paid"))).first())
    if not r:
        raise BadRequest(f"There is no approved pay run for {month:%B %Y} yet.")
    return r


@router.get("/summary")
def summary(month: str | None = None, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    m = _month(month, db, cid)
    slips = (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
             .filter(Payslip.company_id == cid, Payslip.period_start == m, PayRun.status.in_(("approved", "locked", "paid")),
                     Payslip.status.in_(("calculated", "held"))).all())
    pf = {"members": 0, "epf_wages": ZERO, "eps_wages": ZERO, "employee": ZERO, "vpf": ZERO, "eps": ZERO, "epf_er": ZERO,
          "edli": ZERO, "admin": ZERO}
    esi = {"covered": 0, "wages": ZERO, "employee": ZERO, "employer": ZERO}
    pt, lwf = {}, {}
    tds = ZERO
    no_uan, no_esi = [], []
    for p in slips:
        st = p.statutory or {}
        f = st.get("pf") or {}
        if d(f.get("employee")) or d(f.get("employer_total")):
            pf["members"] += 1
            for k, src in (("epf_wages", "epf_wage"), ("eps_wages", "eps_wage"), ("employee", "employee"), ("vpf", "vpf"),
                           ("eps", "employer_eps"), ("epf_er", "employer_epf"), ("edli", "edli"), ("admin", "admin")):
                pf[k] += d(f.get(src))
            if not (p.snapshot or {}).get("uan"):
                no_uan.append((p.snapshot or {}).get("name"))
        e = st.get("esi") or {}
        if e.get("covered"):
            esi["covered"] += 1
            esi["wages"] += d(e.get("wage"))
            esi["employee"] += d(e.get("employee"))
            esi["employer"] += d(e.get("employer"))
            if not (p.snapshot or {}).get("esi_number"):
                no_esi.append((p.snapshot or {}).get("name"))
        x = st.get("pt") or {}
        if x.get("state"):
            row = pt.setdefault(x["state"], {"state": x["state"], "employees": 0, "amount": ZERO})
            row["employees"] += 1 if d(x.get("amount")) else 0
            row["amount"] += d(x.get("amount"))
        lw = st.get("lwf") or {}
        if d(lw.get("employee")) or d(lw.get("employer")):
            row = lwf.setdefault(lw.get("state"), {"state": lw.get("state"), "employees": 0, "employee": ZERO, "employer": ZERO})
            row["employees"] += 1
            row["employee"] += d(lw.get("employee"))
            row["employer"] += d(lw.get("employer"))
        tds += d(((p.tax or {}).get("month") or {}).get("tds"))
    book = statutory.RuleBook(db, cid, calc.month_end(m))
    epf_rules = book.need("epf")
    admin_total = calc.pf_admin_charges(pf["admin"], pf["members"], epf_rules) if slips else ZERO

    def m2(x):
        from decimal import Decimal
        return {k: (money(v) if isinstance(v, Decimal) else v) for k, v in x.items()}
    return {"month": m.isoformat(), "runs": len({p.pay_run_id for p in slips}), "employees": len(slips),
            "pf": {**m2(pf), "admin_with_minimum": money(admin_total),
                   "total": money(pf["employee"] + pf["vpf"] + pf["eps"] + pf["epf_er"] + pf["edli"] + admin_total)},
            "esi": {**m2(esi), "total": money(esi["employee"] + esi["employer"])},
            "pt": [m2(v) for v in sorted(pt.values(), key=lambda r: r["state"])],
            "lwf": [m2(v) for v in lwf.values()], "tds": money(tds), "missing_uan": [x for x in no_uan if x],
            "missing_esi_number": [x for x in no_esi if x]}


FILES = {"ecr": exports.ecr, "ecr_arrears": exports.ecr_arrears, "esi": exports.esi_file, "pt": exports.pt_return,
         "lwf": exports.lwf_return}


@router.get("/files/{kind}")
def statutory_file(kind: str, month: str | None = None, format: str | None = None, user: dict = Depends(STAT),
                   db: Session = Depends(get_db)):
    cid = user["company_id"]
    if kind not in FILES:
        raise NotFound("File")
    run = _regular_run(db, cid, _month(month, db, cid))
    name, media, content = FILES[kind](db, cid, run, as_csv=format == "csv") if kind == "esi" else FILES[kind](db, cid, run)
    audit.event(db, "export.downloaded", "pay_runs", run.id, {"file": kind}, entity_uuid=str(run.uuid))
    db.commit()
    return _file(name, media, content)


@router.get("/calendar")
def calendar(start: str | None = None, months: int = 6, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    s = _month(start, db, cid) if start else calc.add_months(today_for_company_id(db, cid).replace(day=1), -2)
    items = compliance.calendar(db, cid, s, max(1, min(months, 15)))
    db.commit()
    return {"items": items, "labels": compliance.LABELS}


class ChallanIn(BaseModel):
    kind: str
    period: str
    legal_entity_id: int | None = 0
    state: str | None = ""
    status: str | None = "paid"
    reference: str | None = None
    paid_on: date | None = None
    amount: float | None = None
    due_date: date | None = None
    notes: str | None = None


@router.post("/challans")
def record_challan(body: ChallanIn, user: dict = Depends(STAT), db: Session = Depends(get_db)):
    c = compliance.record(db, user["company_id"], body.model_dump())
    db.commit()
    return {"uuid": str(c.uuid), "status": c.status, "reference": c.reference}


@router.get("/registers/bonus")
def bonus_register(fy: str | None = None, user: dict = Depends(STAT), db: Session = Depends(get_db)):
    cid = user["company_id"]
    fy = fy or calc.fy_of(today_for_company_id(db, cid))
    name, media, content = exports.bonus_register(db, cid, fy)
    audit.event(db, "export.downloaded", "payslips", None, {"report": "bonus_register", "fy": fy})
    db.commit()
    return _file(name, media, content)


@router.get("/registers/gratuity")
def gratuity_register(on: date | None = None, user: dict = Depends(STAT), db: Session = Depends(get_db)):
    cid = user["company_id"]
    name, media, content = exports.gratuity_provision(db, cid, on or today_for_company_id(db, cid))
    audit.event(db, "export.downloaded", "employee_salaries", None, {"report": "gratuity_provision"})
    db.commit()
    return _file(name, media, content)


@router.get("/registers/wages")
def wage_register(month: str | None = None, user: dict = Depends(STAT), db: Session = Depends(get_db)):
    cid = user["company_id"]
    run = _regular_run(db, cid, _month(month, db, cid))
    name, media, content = exports.wage_register(db, cid, run)
    db.commit()
    return _file(name, media, content)


@router.get("/minimum-wage-check")
def minimum_wage_check(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    """Everyone whose current wages are below the minimum wage for their state, zone and skill."""
    from modules.payroll import adapters
    from modules.payroll import service as svc
    cid = user["company_id"]
    today = today_for_company_id(db, cid)
    book = statutory.RuleBook(db, cid, today)
    settings = svc.get_settings(db, cid)
    company = svc._company(db, cid)
    Employee = svc.employee_model()
    out = []
    for e in db.query(Employee).filter(Employee.company_id == cid, Employee.active.is_(True)).all():
        s = svc.current_salary(db, cid, e.id, today)
        if not s:
            continue
        p = svc.profile_for(db, cid, e)
        mw = statutory.minimum_wage(book, p.min_wage_state or adapters.employee_state(db, e, company, settings),
                                    p.min_wage_zone, p.skill)
        comps = {c["code"]: d(c["monthly"]) for c in s.components or []}
        wages = comps.get("BASIC", ZERO) + comps.get("DA", ZERO)
        if mw and wages < d(mw["monthly"]):
            out.append({"employee_uuid": str(e.uuid), "name": svc.name_of(e), "wages": money(wages),
                        "minimum": money(mw["monthly"]), "source": mw["source"]})
    db.commit()
    return {"items": out}


# ── Platform library (platform admins) ─────────────────────────────

def _platform_user():
    from modules.platform.security import current_platform_user
    return current_platform_user


class RuleIn(BaseModel):
    kind: str
    scope: str | None = None
    effective_from: date
    data: dict
    note: str | None = None
    source: str | None = None
    status: str = "live"


@platform_router.get("/rules")
def platform_rules(kind: str | None = None, pu=Depends(_platform_user()), db: Session = Depends(get_db)):
    return {"items": [statutory.rule_out(r) for r in statutory.library_rows(db, kind)], "kinds": statutory.KIND_LABELS}


@platform_router.post("/rules", status_code=201)
def platform_add_rule(body: RuleIn, pu=Depends(_platform_user()), db: Session = Depends(get_db)):
    """A new version from a date. Earlier periods keep using the earlier version."""
    if db.query(StatutoryRule).filter(StatutoryRule.kind == body.kind, StatutoryRule.scope == body.scope,
                                      StatutoryRule.effective_from == body.effective_from).first():
        raise BadRequest("A version already starts on that date; edit it instead.")
    r = StatutoryRule(kind=body.kind, scope=body.scope, effective_from=body.effective_from, data=body.data, note=body.note,
                      source=body.source, status=body.status if body.status in ("live", "draft") else "draft",
                      created_by_name=getattr(pu, "name", "Platform"))
    db.add(r)
    db.flush()
    audit.event(db, "statutory.rule_added", "statutory_rules", r.id,
                {"kind": body.kind, "scope": body.scope, "from": body.effective_from.isoformat()})
    db.commit()
    return statutory.rule_out(r)


@platform_router.put("/rules/{uuid}")
def platform_update_rule(uuid: UUID, body: RuleIn, pu=Depends(_platform_user()), db: Session = Depends(get_db)):
    r = db.query(StatutoryRule).filter(StatutoryRule.uuid == uuid).first()
    if not r:
        raise NotFound("Rule")
    r.data, r.note, r.source = body.data, body.note, body.source
    r.status = body.status if body.status in ("live", "draft") else r.status
    r.reviewed_by_name = getattr(pu, "name", None)
    audit.event(db, "statutory.rule_changed", "statutory_rules", r.id, {"kind": r.kind, "scope": r.scope})
    db.commit()
    return statutory.rule_out(r)
