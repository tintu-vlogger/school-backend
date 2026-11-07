"""Fees API.

Staff:   /fees/...            (permissions fees.view / collect / manage / refund / settings)
Portal:  /fees/portal/...     (parents and students; each endpoint checks the learner is theirs)
Public:  /public/fees/webhook/<provider>/<slug>, /public/fees/return/payu/<slug> (signed by the provider)
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from core import audit, context
from core.crud import crud_router, get_or_404
from core.db import bypass_rls, get_db, set_tenant
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import _get_role_permissions, get_current_user, require_permission, require_portal
from modules.fees import gateway, pdf, reports, service
from modules.fees import settings as fee_settings
from modules.fees.models import (FeeAdjustment, FeeAllocation, FeeCategory, FeeConcession, FeeConcessionType, FeeHead,
                                 FeeInvoice, FeeInvoiceLine, FeeLateRule, FeeLedgerCategory, FeeLedgerEntry, FeeOptIn,
                                 FeePayment, FeeRefund, FeeStructure, FeeStructureItem, FeeWalletEntry)
from modules.fees.service import D0, q

router = APIRouter(prefix="/fees", tags=["Fees"])
portal_router = APIRouter(prefix="/fees/portal", tags=["Fees (portal)"])
public_router = APIRouter(prefix="/public/fees", tags=["Fees (public)"])

VIEW, COLLECT, MANAGE, REFUND, SETTINGS = "fees.view", "fees.collect", "fees.manage", "fees.refund", "fees.settings"


def require_any(*perms: str):
    def checker(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
        have = _get_role_permissions(user["company_id"], user["role"], db)
        if not any(p in have for p in perms):
            raise Forbidden(f"Permission denied: {perms[0]}")
        return user
    return checker


def _student(db: Session, company_id: int, uuid: UUID):
    from modules.students.models import Student
    s = db.query(Student).filter(Student.company_id == company_id, Student.uuid == uuid).first()
    if s is None:
        raise NotFound("Student")
    return s


def _student_by_id(db: Session, company_id: int, sid: int):
    from modules.students.models import Student
    s = db.query(Student).filter(Student.company_id == company_id, Student.id == sid).first()
    if s is None:
        raise NotFound("Student")
    return s


def _brief(db: Session, cid: int, s) -> dict:
    from modules.students.service import current_enrollment, student_brief
    return student_brief(s, current_enrollment(db, cid, s.id))


def _label(sec) -> str | None:
    from modules.academics.service import section_label
    return section_label(sec) if sec else None


# ── Output shapes ────────────────────────────────────────────────────

def line_out(ln: FeeInvoiceLine) -> dict:
    return {"id": ln.id, "head_id": ln.head_id, "head": ln.head.name if ln.head else None,
            "head_code": ln.head.code if ln.head else None, "description": ln.description, "amount": q(ln.amount),
            "concession": q(ln.concession), "concession_note": ln.concession_note, "tax_rate": ln.tax_rate,
            "tax": q(ln.tax), "net": q(ln.net), "credited": q(ln.credited), "paid": q(ln.paid), "due": q(ln.due),
            "refunded": q(ln.refunded), "kind": ln.kind}


def invoice_out(inv: FeeInvoice, lines: bool = False, late_fee: Decimal | None = None) -> dict:
    out = {"id": inv.id, "uuid": str(inv.uuid), "number": inv.number, "title": inv.title, "issue_date": inv.issue_date,
           "due_date": inv.due_date, "gross": q(inv.gross), "concession": q(inv.concession), "tax": q(inv.tax),
           "total": q(inv.total), "credited": q(inv.credited), "paid": q(inv.paid), "balance": q(inv.balance),
           "status": inv.status, "source": inv.source, "student_id": inv.student_id,
           "student_uuid": str(inv.student.uuid) if inv.student else None,
           "student_name": inv.student.full_name if inv.student else None,
           "admission_no": inv.student.admission_no if inv.student else None,
           "section_label": _label(inv.section), "session_id": inv.session_id, "notes": inv.notes,
           "cancel_reason": inv.cancel_reason, "cancelled_at": inv.cancelled_at, "cancelled_by_name": inv.cancelled_by_name,
           "created_by_name": inv.created_by_name, "created_at": inv.created_at}
    if lines:
        out["lines"] = [line_out(ln) for ln in sorted(inv.lines, key=lambda x: x.sort_order)]
    if late_fee is not None:
        out["late_fee"] = q(late_fee)
        out["payable"] = q(inv.balance) + q(late_fee)
    return out


def payment_out(p: FeePayment, full: bool = False) -> dict:
    out = {"id": p.id, "uuid": str(p.uuid), "number": p.number, "paid_on": p.paid_on, "amount": q(p.amount),
           "wallet_used": q(p.wallet_used), "allocated": q(p.allocated), "late_fee": q(p.late_fee),
           "advance": q(p.advance), "mode": p.mode, "mode_label": service.MODES.get(p.mode, p.mode.title()),
           "source": p.source, "status": p.status, "notes": p.notes, "collected_by_name": p.collected_by_name,
           "cancel_reason": p.cancel_reason, "cancelled_at": p.cancelled_at, "cancelled_by_name": p.cancelled_by_name,
           "student_id": p.student_id, "student_uuid": str(p.student.uuid) if p.student else None,
           "student_name": p.student.full_name if p.student else None,
           "admission_no": p.student.admission_no if p.student else None, "created_at": p.created_at,
           "modes": [{"mode": m.mode, "label": service.MODES.get(m.mode, m.mode), "amount": q(m.amount),
                      "reference": m.reference, "bank": m.bank, "instrument_date": m.instrument_date, "status": m.status}
                     for m in p.modes]}
    if full:
        out["allocations"] = [{"invoice_uuid": str(a.invoice.uuid), "invoice_number": a.invoice.number,
                               "invoice_title": a.invoice.title, "head": a.line.head.name if a.line.head else None,
                               "description": a.line.description, "amount": q(a.amount), "reversed": a.reversed}
                              for a in sorted(p.allocations, key=lambda a: (a.invoice.due_date, a.id))]
    return out


def concession_out(c: FeeConcession) -> dict:
    return {"id": c.id, "uuid": str(c.uuid), "student_id": c.student_id,
            "student_uuid": str(c.student.uuid) if c.student else None,
            "student_name": c.student.full_name if c.student else None,
            "admission_no": c.student.admission_no if c.student else None, "session_id": c.session_id,
            "type_id": c.type_id, "name": c.name, "reason": c.reason, "reason_label": service.REASONS.get(c.reason, c.reason),
            "kind": c.kind, "value": q(c.value), "head_id": c.head_id, "head": c.head.name if c.head else None,
            "valid_from": c.valid_from, "valid_to": c.valid_to, "status": c.status, "auto": c.auto, "notes": c.notes,
            "requested_by_name": c.requested_by_name, "decided_at": c.decided_at, "created_at": c.created_at}


def refund_out(r: FeeRefund) -> dict:
    return {"id": r.id, "uuid": str(r.uuid), "number": r.number, "student_id": r.student_id,
            "student_uuid": str(r.student.uuid) if r.student else None,
            "student_name": r.student.full_name if r.student else None, "source": r.source, "amount": q(r.amount),
            "reason": r.reason, "status": r.status, "mode": r.mode, "reference": r.reference, "paid_on": r.paid_on,
            "requested_by_name": r.requested_by_name, "paid_by_name": r.paid_by_name, "created_at": r.created_at}


def structure_out(s: FeeStructure, full: bool = True) -> dict:
    out = {"id": s.id, "uuid": str(s.uuid), "name": s.name, "session_id": s.session_id,
           "session_name": s.session.name if s.session else None, "program_id": s.program_id,
           "program_name": s.program.name if s.program else None, "category_id": s.category_id,
           "category_name": s.category.name if s.category else None, "plan": s.plan, "notes": s.notes,
           "active": s.active, "instalment_count": len(s.instalments), "total": service.structure_total(s),
           "total_with_optional": service.structure_total(s, True), "created_at": s.created_at}
    if full:
        out["instalments"] = [{"id": i.id, "uuid": str(i.uuid), "seq": i.seq, "name": i.name, "due_date": i.due_date}
                              for i in s.instalments]
        out["items"] = [{"id": it.id, "head_id": it.head_id, "head": it.head.name if it.head else None,
                         "head_code": it.head.code if it.head else None, "amount": q(it.amount), "billing": it.billing,
                         "instalment_no": it.instalment_no, "optional": it.optional} for it in s.items]
    return out


# ── Meta, summary, settings ──────────────────────────────────────────

@router.get("/meta")
def meta(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cfg = fee_settings.get(db, user["company_id"])
    return {"currency": fee_settings.currency(db, user["company_id"]), "modes": service.MODES,
            "head_kinds": service.HEAD_KINDS, "reasons": service.REASONS,
            "plans": {"monthly": "Monthly", "quarterly": "Quarterly", "term": "Term-wise", "half_yearly": "Half-yearly",
                      "yearly": "Yearly", "custom": "Custom"},
            "allow_advance": cfg["allow_advance"], "bounce_charge": cfg["bounce_charge"],
            "concession_approval": cfg["concession_approval"]}


@router.get("/summary")
def summary_(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return service.summary(db, user["company_id"])


@router.get("/overview")
def overview(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return service.summary(db, user["company_id"], full=True)


@router.get("/settings")
def get_settings_(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return {**fee_settings.get(db, user["company_id"]), "currency": fee_settings.currency(db, user["company_id"])}


@router.put("/settings")
def put_settings(data: dict, db: Session = Depends(get_db), user: dict = Depends(require_permission(SETTINGS))):
    out = fee_settings.save(db, user["company_id"], data)
    audit.event(db, "fees.settings_saved", "companies", user["company_id"], {"keys": sorted(data.keys())})
    db.commit()
    return out


@router.get("/gateway")
def get_gateway(db: Session = Depends(get_db), user: dict = Depends(require_permission(SETTINGS))):
    return gateway.out(db, user["company_id"])


@router.put("/gateway")
def put_gateway(data: dict, db: Session = Depends(get_db), user: dict = Depends(require_permission(SETTINGS))):
    out = gateway.save(db, user["company_id"], data)
    db.commit()
    return out


# ── Simple set-up lists ──────────────────────────────────────────────

class HeadIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    code: str = Field(..., min_length=1, max_length=30)
    kind: str = Field("recurring", pattern="^(recurring|one_time|optional|deposit)$")
    tax_rate: Decimal | None = Field(None, ge=0, le=100)
    tax_code: str | None = Field(None, max_length=20)
    ledger_account: str | None = Field(None, max_length=120)
    description: str | None = None
    sort_order: int = 1


class HeadPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    code: str | None = Field(None, min_length=1, max_length=30)
    kind: str | None = Field(None, pattern="^(recurring|one_time|optional|deposit)$")
    tax_rate: Decimal | None = Field(None, ge=0, le=100)
    tax_code: str | None = None
    ledger_account: str | None = None
    description: str | None = None
    sort_order: int | None = None
    active: bool | None = None


def _head_validate(db, cid, data, row):
    if "code" in data and data["code"]:
        data["code"] = data["code"].strip().upper()
        if row is not None and row.is_system and data["code"] != row.code:
            raise BadRequest("The code of a built-in head cannot change.")


router.include_router(crud_router(
    model=FeeHead, prefix="/heads", tag="Fees", create_schema=HeadIn, update_schema=HeadPatch,
    view_perm=VIEW, manage_perm=MANAGE, search_fields=("name", "code"), order_by=("sort_order", "name"),
    label="Fee head", validate=_head_validate, filters=("kind",)))


class CategoryIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    code: str | None = Field(None, max_length=30)
    description: str | None = None


class CategoryPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    code: str | None = None
    description: str | None = None
    active: bool | None = None


router.include_router(crud_router(
    model=FeeCategory, prefix="/categories", tag="Fees", create_schema=CategoryIn, update_schema=CategoryPatch,
    view_perm=VIEW, manage_perm=MANAGE, search_fields=("name", "code"), label="Fee category"))


class ConcessionTypeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    reason: str = Field("other", max_length=30)
    kind: str = Field("percent", pattern="^(percent|fixed)$")
    value: Decimal = Field(..., gt=0)
    head_id: int | None = None
    description: str | None = None


class ConcessionTypePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    reason: str | None = None
    kind: str | None = Field(None, pattern="^(percent|fixed)$")
    value: Decimal | None = Field(None, gt=0)
    head_id: int | None = None
    description: str | None = None
    active: bool | None = None


def _ctype_validate(db, cid, data, row):
    kind = data.get("kind") or (row.kind if row else "percent")
    if kind == "percent" and data.get("value") is not None and data["value"] > 100:
        raise BadRequest("A percentage cannot be more than 100.")
    if data.get("head_id"):
        service._head(db, cid, data["head_id"])


router.include_router(crud_router(
    model=FeeConcessionType, prefix="/concession-types", tag="Fees", create_schema=ConcessionTypeIn,
    update_schema=ConcessionTypePatch, view_perm=VIEW, manage_perm=MANAGE, label="Concession type",
    validate=_ctype_validate))


class LateRuleIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    kind: str = Field("per_day", pattern="^(fixed|per_day)$")
    amount: Decimal = Field(..., gt=0)
    grace_days: int = Field(0, ge=0, le=365)
    max_amount: Decimal | None = Field(None, ge=0)
    structure_id: int | None = None


class LateRulePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    kind: str | None = Field(None, pattern="^(fixed|per_day)$")
    amount: Decimal | None = Field(None, gt=0)
    grace_days: int | None = Field(None, ge=0, le=365)
    max_amount: Decimal | None = Field(None, ge=0)
    structure_id: int | None = None
    active: bool | None = None


def _late_validate(db, cid, data, row):
    if data.get("structure_id") and not db.query(FeeStructure.id).filter(
            FeeStructure.company_id == cid, FeeStructure.id == data["structure_id"]).first():
        raise BadRequest("The fee structure was not found.")


router.include_router(crud_router(
    model=FeeLateRule, prefix="/late-rules", tag="Fees", create_schema=LateRuleIn, update_schema=LateRulePatch,
    view_perm=VIEW, manage_perm=SETTINGS, label="Late fee rule", validate=_late_validate))


class LedgerCategoryIn(BaseModel):
    kind: str = Field(..., pattern="^(income|expense)$")
    name: str = Field(..., min_length=1, max_length=120)


class LedgerCategoryPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    active: bool | None = None


router.include_router(crud_router(
    model=FeeLedgerCategory, prefix="/ledger-categories", tag="Fees", create_schema=LedgerCategoryIn,
    update_schema=LedgerCategoryPatch, view_perm=VIEW, manage_perm=MANAGE, label="Category",
    order_by=("kind", "name"), filters=("kind",)))


# ── Structures ───────────────────────────────────────────────────────

class InstalmentIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    due_date: date


class ItemIn(BaseModel):
    head_id: int
    amount: Decimal = Field(..., ge=0)
    billing: str | None = Field(None, pattern="^(per_instalment|once)$")
    instalment_no: int | None = None
    optional: bool = False


class StructureIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    session_id: int
    program_id: int | None = None
    category_id: int | None = None
    plan: str = Field("quarterly", pattern="^(monthly|quarterly|term|half_yearly|yearly|custom)$")
    notes: str | None = None
    active: bool | None = True
    instalments: list[InstalmentIn] = Field(..., max_length=24)
    items: list[ItemIn] = Field(..., max_length=60)


def _structure_q(db: Session, cid: int):
    return (db.query(FeeStructure).options(selectinload(FeeStructure.items).joinedload(FeeStructureItem.head),
                                           selectinload(FeeStructure.instalments), joinedload(FeeStructure.session),
                                           joinedload(FeeStructure.program), joinedload(FeeStructure.category))
            .filter(FeeStructure.company_id == cid))


@router.get("/structures")
def list_structures(session_id: int | None = None, program_id: int | None = None, q_: str | None = Query(None, alias="q"),
                    active: bool | None = None, params: PageParams = Depends(), db: Session = Depends(get_db),
                    user: dict = Depends(require_permission(VIEW))):
    qy = _structure_q(db, user["company_id"])
    if session_id:
        qy = qy.filter(FeeStructure.session_id == session_id)
    if program_id:
        qy = qy.filter(FeeStructure.program_id == program_id)
    if active is not None:
        qy = qy.filter(FeeStructure.active.is_(active))
    if q_:
        qy = qy.filter(FeeStructure.name.ilike(f"%{q_.strip()}%"))
    return paginate(qy.order_by(FeeStructure.session_id.desc(), FeeStructure.name), params, structure_out)


@router.get("/structures/plan")
def plan_suggestion(plan: str = "quarterly", session_id: int | None = None, day: int = 10,
                    db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """Suggested instalments (names and due dates) for a plan in a session."""
    from modules.academics.models import AcademicSession, Term
    from modules.academics.service import current_session
    cid = user["company_id"]
    sess = db.query(AcademicSession).filter(AcademicSession.company_id == cid, AcademicSession.id == session_id).first() \
        if session_id else current_session(db, cid)
    if sess is None:
        raise BadRequest("Add an academic session first.")
    if plan == "term":
        terms = db.query(Term).filter(Term.company_id == cid, Term.session_id == sess.id).order_by(Term.sort_order).all()
        if terms:
            return [{"name": t.name, "due_date": t.start_date + timedelta(days=9)} for t in terms]
    return [{"name": n, "due_date": d} for n, d in service.plan_dates(plan if plan != "custom" else "yearly",
                                                                      sess.start_date, day=day)]


@router.get("/structures/{uuid}")
def get_structure(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    s = _structure_q(db, user["company_id"]).filter(FeeStructure.uuid == uuid).first()
    if s is None:
        raise NotFound("Fee structure")
    out = structure_out(s)
    out["invoices"] = db.query(func.count(FeeInvoice.id)).filter(FeeInvoice.structure_id == s.id,
                                                                 FeeInvoice.status != "cancelled").scalar()
    return out


@router.post("/structures", status_code=201)
def create_structure(payload: StructureIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    s = service.save_structure(db, user["company_id"], payload.model_dump())
    db.commit()
    return get_structure(s.uuid, db, user)


@router.put("/structures/{uuid}")
def update_structure(uuid: UUID, payload: StructureIn, db: Session = Depends(get_db),
                     user: dict = Depends(require_permission(MANAGE))):
    s = get_or_404(db, FeeStructure, user["company_id"], uuid, "Fee structure")
    service.save_structure(db, user["company_id"], payload.model_dump(), s)
    db.commit()
    return get_structure(uuid, db, user)


@router.delete("/structures/{uuid}")
def archive_structure(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    s = get_or_404(db, FeeStructure, user["company_id"], uuid, "Fee structure")
    s.active = False
    db.commit()
    return {"detail": "Switched off"}


class CopyIn(BaseModel):
    program_ids: list[int] = Field(..., min_length=1, max_length=50)
    session_id: int | None = None


@router.post("/structures/{uuid}/copy")
def copy_structure(uuid: UUID, payload: CopyIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Copy a structure to other classes (and optionally another session, shifting due dates)."""
    from modules.academics.models import AcademicSession, Program
    cid = user["company_id"]
    src = _structure_q(db, cid).filter(FeeStructure.uuid == uuid).first()
    if src is None:
        raise NotFound("Fee structure")
    shift = timedelta(0)
    sess_id = payload.session_id or src.session_id
    if sess_id != src.session_id:
        dst = db.query(AcademicSession).filter(AcademicSession.company_id == cid, AcademicSession.id == sess_id).first()
        if dst is None:
            raise BadRequest("The session was not found.")
        shift = dst.start_date - src.session.start_date
    made = 0
    for pid in payload.program_ids:
        prog = db.query(Program).filter(Program.company_id == cid, Program.id == pid).first()
        if prog is None:
            continue
        service.save_structure(db, cid, {
            "name": f"{prog.name} fees", "session_id": sess_id, "program_id": pid, "category_id": src.category_id,
            "plan": src.plan, "notes": src.notes, "active": True,
            "instalments": [{"name": i.name, "due_date": i.due_date + shift} for i in src.instalments],
            "items": [{"head_id": it.head_id, "amount": it.amount, "billing": it.billing,
                       "instalment_no": it.instalment_no, "optional": it.optional} for it in src.items]})
        made += 1
    db.commit()
    return {"created": made}


# ── Learners: search, account, opt-ins ───────────────────────────────

@router.get("/students/search")
def search_students(q_: str = Query("", alias="q"), db: Session = Depends(get_db),
                    user: dict = Depends(require_any(COLLECT, VIEW))):
    """Find a learner by name, admission number, or a learner's or parent's phone."""
    from modules.students.models import Guardian, Student, StudentGuardian
    from modules.students.service import search_students as ss, student_brief
    cid = user["company_id"]
    text = q_.strip()
    if not text:
        return []
    rows = ss(db, cid, text, status=None).limit(15).all()
    seen = {s.id for s, _ in rows}
    like = f"%{text}%"
    extra = (db.query(Student).join(StudentGuardian, StudentGuardian.student_id == Student.id)
             .join(Guardian, Guardian.id == StudentGuardian.guardian_id)
             .filter(Student.company_id == cid, Student.archived_at.is_(None),
                     or_(Guardian.phone.ilike(like), Guardian.first_name.ilike(like), Guardian.last_name.ilike(like)))
             .limit(15).all())
    out = [student_brief(s, e) for s, e in rows]
    from modules.students.service import current_enrollment
    for s in extra:
        if s.id not in seen:
            seen.add(s.id)
            out.append(student_brief(s, current_enrollment(db, cid, s.id)))
    for o in out[:20]:
        d = service.student_dues(db, cid, o["id"])
        o["due"], o["overdue"] = d["total"], d["overdue"]
    return out[:20]


@router.get("/students/{uuid}/account")
def student_account(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_any(COLLECT, VIEW))):
    """Everything the fee counter needs for one learner."""
    cid = user["company_id"]
    s = _student(db, cid, uuid)
    return _account(db, cid, s, staff=True)


def _account(db: Session, cid: int, s, staff: bool) -> dict:
    on = service.today(db, cid)
    prev = service.preview_dues(db, cid, s.id, on)
    open_rows = [invoice_out(r["invoice"], lines=True, late_fee=r["late_fee"]) for r in prev["rows"]]
    paid = (db.query(FeeInvoice).options(selectinload(FeeInvoice.lines).joinedload(FeeInvoiceLine.head))
            .filter(FeeInvoice.company_id == cid, FeeInvoice.student_id == s.id, FeeInvoice.status == "paid")
            .order_by(FeeInvoice.due_date.desc()).limit(40).all())
    pays = (db.query(FeePayment).options(selectinload(FeePayment.modes), joinedload(FeePayment.student))
            .filter(FeePayment.company_id == cid, FeePayment.student_id == s.id)
            .order_by(FeePayment.paid_on.desc(), FeePayment.id.desc()).limit(50).all())
    if not staff:
        pays = [p for p in pays if p.status == "paid"]
    out = {"student": _brief(db, cid, s), "currency": fee_settings.currency(db, cid), "today": on,
           "open": open_rows, "paid": [invoice_out(i, lines=True) for i in paid],
           "payments": [payment_out(p) for p in pays], "total_due": prev["total"], "late_fee": prev["late_fee"],
           "overdue": sum((r["balance"] for r in open_rows if r["due_date"] < on), D0), "wallet": prev["wallet"]}
    if staff:
        concs = db.query(FeeConcession).options(joinedload(FeeConcession.student), joinedload(FeeConcession.head)).filter(
            FeeConcession.company_id == cid, FeeConcession.student_id == s.id).order_by(FeeConcession.id.desc()).all()
        out["concessions"] = [concession_out(c) for c in concs]
        out["deposit_refundable"] = service.deposit_refundable(db, cid, s.id)
        out["refunds"] = [refund_out(r) for r in db.query(FeeRefund).options(joinedload(FeeRefund.student)).filter(
            FeeRefund.company_id == cid, FeeRefund.student_id == s.id).order_by(FeeRefund.id.desc()).all()]
        out["opt_ins"] = [h for (h,) in db.query(FeeOptIn.head_id).filter(FeeOptIn.company_id == cid,
                                                                          FeeOptIn.student_id == s.id).all()]
    return out


class OptInsIn(BaseModel):
    head_ids: list[int] = Field(default_factory=list, max_length=30)
    session_id: int | None = None


@router.put("/students/{uuid}/opt-ins")
def set_opt_ins(uuid: UUID, payload: OptInsIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Optional heads (transport, hostel, uniform …) the learner takes this session."""
    from modules.academics.service import current_session
    cid = user["company_id"]
    s = _student(db, cid, uuid)
    sess_id = payload.session_id or (current_session(db, cid).id if current_session(db, cid) else None)
    if not sess_id:
        raise BadRequest("Add an academic session first.")
    for hid in payload.head_ids:
        service._head(db, cid, hid)
    existing = db.query(FeeOptIn).filter(FeeOptIn.company_id == cid, FeeOptIn.student_id == s.id,
                                         FeeOptIn.session_id == sess_id).all()
    want = set(payload.head_ids)
    for o in existing:
        if o.head_id not in want:
            db.delete(o)
    have = {o.head_id for o in existing}
    for hid in want - have:
        db.add(FeeOptIn(company_id=cid, student_id=s.id, session_id=sess_id, head_id=hid))
    db.commit()
    return {"head_ids": sorted(want)}


# ── Invoices ─────────────────────────────────────────────────────────

def _invoice_q(db: Session, cid: int):
    from modules.academics.models import Section
    return (db.query(FeeInvoice).options(joinedload(FeeInvoice.student),
                                         joinedload(FeeInvoice.section).joinedload(Section.program))
            .filter(FeeInvoice.company_id == cid))


@router.get("/invoices")
def list_invoices(q_: str | None = Query(None, alias="q"), status: str | None = None, session_id: int | None = None,
                  program_id: int | None = None, section_id: int | None = None, student_id: int | None = None,
                  overdue: bool = False, source: str | None = None, params: PageParams = Depends(),
                  db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    from modules.students.models import Student
    cid = user["company_id"]
    qy = _invoice_q(db, cid)
    if status:
        qy = qy.filter(FeeInvoice.status.in_(status.split(",")))
    for col, v in ((FeeInvoice.session_id, session_id), (FeeInvoice.program_id, program_id),
                   (FeeInvoice.section_id, section_id), (FeeInvoice.student_id, student_id), (FeeInvoice.source, source)):
        if v:
            qy = qy.filter(col == v)
    if overdue:
        qy = qy.filter(FeeInvoice.status.in_(service.LIVE), FeeInvoice.due_date < service.today(db, cid))
    if q_:
        like = f"%{q_.strip()}%"
        qy = qy.join(Student, Student.id == FeeInvoice.student_id).filter(or_(
            FeeInvoice.number.ilike(like), FeeInvoice.title.ilike(like), Student.first_name.ilike(like),
            Student.last_name.ilike(like), Student.admission_no.ilike(like),
            func.concat(Student.first_name, " ", Student.last_name).ilike(like)))
    return paginate(qy.order_by(FeeInvoice.due_date.desc(), FeeInvoice.id.desc()), params, invoice_out)


@router.get("/invoices/{uuid}")
def get_invoice(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = user["company_id"]
    inv = service.get_invoice(db, cid, uuid)
    out = invoice_out(inv, lines=True, late_fee=service.late_fee_due(db, inv, service.today(db, cid)))
    out["adjustments"] = [{"uuid": str(a.uuid), "number": a.number, "kind": a.kind, "amount": q(a.amount),
                           "reason": a.reason, "head": a.head.name if a.head else None,
                           "created_by_name": a.created_by_name, "created_at": a.created_at}
                          for a in db.query(FeeAdjustment).options(joinedload(FeeAdjustment.head))
                          .filter(FeeAdjustment.company_id == cid, FeeAdjustment.invoice_id == inv.id)
                          .order_by(FeeAdjustment.id).all()]
    allocs = (db.query(FeeAllocation).options(joinedload(FeeAllocation.payment))
              .filter(FeeAllocation.company_id == cid, FeeAllocation.invoice_id == inv.id).all())
    pays: dict = {}
    for a in allocs:
        p = pays.setdefault(a.payment_id, {"uuid": str(a.payment.uuid), "number": a.payment.number,
                                           "paid_on": a.payment.paid_on, "status": a.payment.status, "amount": D0})
        p["amount"] += q(a.amount)
    out["payments"] = list(pays.values())
    return out


class GenerateIn(BaseModel):
    session_id: int | None = None
    program_id: int | None = None
    section_id: int | None = None
    student_ids: list[int] | None = Field(None, max_length=5000)
    instalment_seqs: list[int] | None = None


@router.post("/invoices/generate")
def generate(payload: GenerateIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Make invoices from the fee structures for a whole session, a class, a section or chosen learners."""
    res = service.generate_bulk(db, user["company_id"], **payload.model_dump())
    db.commit()
    return res


class ManualLineIn(BaseModel):
    head_id: int
    amount: Decimal = Field(..., gt=0)
    description: str | None = Field(None, max_length=200)


class ManualInvoiceIn(BaseModel):
    student_uuid: UUID
    title: str = Field(..., min_length=1, max_length=200)
    due_date: date
    lines: list[ManualLineIn] = Field(..., min_length=1, max_length=30)
    notes: str | None = None


@router.post("/invoices", status_code=201)
def create_invoice(payload: ManualInvoiceIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    s = _student(db, cid, payload.student_uuid)
    inv = service.add_manual_invoice(db, cid, s, payload.title, payload.due_date,
                                     [ln.model_dump() for ln in payload.lines], payload.notes)
    db.commit()
    return get_invoice(inv.uuid, db, user)


class ReasonIn(BaseModel):
    reason: str = Field(..., min_length=2, max_length=500)


@router.post("/invoices/{uuid}/cancel")
def cancel_invoice(uuid: UUID, payload: ReasonIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    inv = service.get_invoice(db, user["company_id"], uuid)
    service.cancel_invoice(db, inv, payload.reason)
    db.commit()
    return get_invoice(uuid, db, user)


class AdjustIn(BaseModel):
    kind: str = Field(..., pattern="^(credit|debit)$")
    amount: Decimal = Field(..., gt=0)
    reason: str = Field(..., min_length=2, max_length=500)
    head_id: int | None = None


@router.post("/invoices/{uuid}/adjust")
def adjust_invoice(uuid: UUID, payload: AdjustIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    inv = service.get_invoice(db, user["company_id"], uuid)
    service.adjust_invoice(db, inv, payload.kind, payload.amount, payload.reason, payload.head_id)
    db.commit()
    return get_invoice(uuid, db, user)


@router.get("/invoices/{uuid}/pdf")
def invoice_pdf(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    inv = service.get_invoice(db, user["company_id"], uuid)
    return _pdf(pdf.invoice_html(db, inv), f"invoice-{(inv.number or 'draft').replace('/', '-')}.pdf")


def _pdf(doc: str, name: str) -> Response:
    return Response(content=pdf.to_pdf(doc), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})


# ── Collection and receipts ──────────────────────────────────────────

class ModeIn(BaseModel):
    mode: str = Field("cash", pattern="^(cash|cheque|dd|upi|card|bank_transfer|online)$")
    amount: Decimal = Field(..., ge=0)
    reference: str | None = Field(None, max_length=120)
    bank: str | None = Field(None, max_length=120)
    instrument_date: date | None = None


class CollectIn(BaseModel):
    student_uuid: UUID
    modes: list[ModeIn] = Field(default_factory=list, max_length=6)
    invoice_uuids: list[UUID] | None = None
    use_wallet: Decimal = Field(Decimal("0"), ge=0)
    waive_late_fee: bool = False
    paid_on: date | None = None
    notes: str | None = Field(None, max_length=500)


@router.post("/collect", status_code=201)
def collect(payload: CollectIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(COLLECT))):
    cid = user["company_id"]
    s = _student(db, cid, payload.student_uuid)
    if payload.waive_late_fee and MANAGE not in _get_role_permissions(cid, user["role"], db):
        raise Forbidden("Only people who manage fees can waive a late fee.")
    if payload.paid_on and payload.paid_on > service.today(db, cid):
        raise BadRequest("The payment date cannot be in the future.")
    ids = None
    if payload.invoice_uuids is not None:
        ids = [i for (i,) in db.query(FeeInvoice.id).filter(FeeInvoice.company_id == cid, FeeInvoice.student_id == s.id,
                                                            FeeInvoice.uuid.in_(payload.invoice_uuids or [])).all()]
    pay = service.collect(db, cid, s, modes=[m.model_dump() for m in payload.modes], paid_on=payload.paid_on,
                          invoice_ids=ids, use_wallet=payload.use_wallet, waive_late_fee=payload.waive_late_fee,
                          notes=payload.notes)
    db.commit()
    return payment_out(service.get_payment(db, cid, pay.uuid), full=True)


@router.get("/payments")
def list_payments(q_: str | None = Query(None, alias="q"), status: str | None = None, mode: str | None = None,
                  source: str | None = None, date_from: date | None = Query(None, alias="from"),
                  date_to: date | None = Query(None, alias="to"), student_id: int | None = None,
                  params: PageParams = Depends(), db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(VIEW))):
    from modules.students.models import Student
    cid = user["company_id"]
    qy = db.query(FeePayment).options(selectinload(FeePayment.modes), joinedload(FeePayment.student)).filter(
        FeePayment.company_id == cid)
    if status:
        qy = qy.filter(FeePayment.status.in_(status.split(",")))
    if mode:
        qy = qy.filter(FeePayment.mode == mode)
    if source:
        qy = qy.filter(FeePayment.source == source)
    if date_from:
        qy = qy.filter(FeePayment.paid_on >= date_from)
    if date_to:
        qy = qy.filter(FeePayment.paid_on <= date_to)
    if student_id:
        qy = qy.filter(FeePayment.student_id == student_id)
    if q_:
        like = f"%{q_.strip()}%"
        qy = qy.join(Student, Student.id == FeePayment.student_id).filter(or_(
            FeePayment.number.ilike(like), Student.first_name.ilike(like), Student.last_name.ilike(like),
            Student.admission_no.ilike(like), func.concat(Student.first_name, " ", Student.last_name).ilike(like)))
    total = q(qy.with_entities(func.coalesce(func.sum(FeePayment.amount), 0)).filter(FeePayment.status == "paid")
              .order_by(None).scalar())
    out = paginate(qy.order_by(FeePayment.paid_on.desc(), FeePayment.id.desc()), params, payment_out)
    out["sum"] = total
    return out


@router.get("/payments/{uuid}")
def get_payment(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_any(VIEW, COLLECT))):
    return payment_out(service.get_payment(db, user["company_id"], uuid), full=True)


@router.get("/payments/{uuid}/pdf")
def payment_pdf(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_any(VIEW, COLLECT))):
    cid = user["company_id"]
    p = service.get_payment(db, cid, uuid)
    return _pdf(pdf.receipt_html(db, p, fee_settings.get(db, cid)["receipt_note"]),
                f"receipt-{(p.number or '').replace('/', '-')}.pdf")


@router.get("/payments/{uuid}/print", response_class=HTMLResponse)
def payment_print(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_any(VIEW, COLLECT))):
    cid = user["company_id"]
    p = service.get_payment(db, cid, uuid)
    return HTMLResponse(pdf.receipt_html(db, p, fee_settings.get(db, cid)["receipt_note"]))


@router.post("/payments/{uuid}/cancel")
def cancel_payment(uuid: UUID, payload: ReasonIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(REFUND))):
    cid = user["company_id"]
    p = service.get_payment(db, cid, uuid)
    service.cancel_payment(db, p, payload.reason)
    db.commit()
    return payment_out(service.get_payment(db, cid, uuid), full=True)


class BounceIn(BaseModel):
    reason: str = Field("Cheque returned by the bank", min_length=2, max_length=500)
    charge: Decimal | None = Field(None, ge=0)


@router.post("/payments/{uuid}/bounce")
def bounce_payment(uuid: UUID, payload: BounceIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(REFUND))):
    cid = user["company_id"]
    p = service.get_payment(db, cid, uuid)
    if not any(m.mode in ("cheque", "dd") for m in p.modes):
        raise BadRequest("Only cheque or demand draft receipts can bounce.")
    charge = payload.charge if payload.charge is not None else fee_settings.get(db, cid)["bounce_charge"]
    service.cancel_payment(db, p, payload.reason, bounced=True, charge=charge)
    db.commit()
    return payment_out(service.get_payment(db, cid, uuid), full=True)


# ── Concessions ──────────────────────────────────────────────────────

@router.get("/concessions")
def list_concessions(q_: str | None = Query(None, alias="q"), status: str | None = None, reason: str | None = None,
                     session_id: int | None = None, params: PageParams = Depends(), db: Session = Depends(get_db),
                     user: dict = Depends(require_permission(VIEW))):
    from modules.students.models import Student
    cid = user["company_id"]
    qy = (db.query(FeeConcession).options(joinedload(FeeConcession.student), joinedload(FeeConcession.head))
          .join(Student, Student.id == FeeConcession.student_id).filter(FeeConcession.company_id == cid))
    if status:
        qy = qy.filter(FeeConcession.status.in_(status.split(",")))
    if reason:
        qy = qy.filter(FeeConcession.reason == reason)
    if session_id:
        qy = qy.filter(or_(FeeConcession.session_id == session_id, FeeConcession.session_id.is_(None)))
    if q_:
        like = f"%{q_.strip()}%"
        qy = qy.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like), Student.admission_no.ilike(like),
                           FeeConcession.name.ilike(like)))
    return paginate(qy.order_by(FeeConcession.id.desc()), params, concession_out)


class ConcessionIn(BaseModel):
    student_uuid: UUID
    type_id: int | None = None
    name: str | None = Field(None, max_length=120)
    reason: str | None = Field(None, max_length=30)
    kind: str | None = Field(None, pattern="^(percent|fixed)$")
    value: Decimal | None = Field(None, gt=0)
    head_id: int | None = None
    session_id: int | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    notes: str | None = None


@router.post("/concessions", status_code=201)
def create_concession(payload: ConcessionIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    s = _student(db, cid, payload.student_uuid)
    data = payload.model_dump(exclude_unset=True)
    data.pop("student_uuid")
    data["student_id"] = s.id
    c = service.create_concession(db, cid, data)
    db.commit()
    db.refresh(c)
    return concession_out(c)


@router.post("/concessions/{uuid}/cancel")
def cancel_concession(uuid: UUID, payload: ReasonIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    c = get_or_404(db, FeeConcession, user["company_id"], uuid, "Concession")
    service.cancel_concession(db, c, payload.reason)
    db.commit()
    return concession_out(c)


@router.post("/concessions/apply-sibling-rule")
def apply_sibling_rule(db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Re-check the sibling rule for every enrolled learner and update open invoices."""
    from modules.academics.service import current_session
    from modules.students.models import Enrollment, Student
    cid = user["company_id"]
    sess = current_session(db, cid)
    if sess is None:
        raise BadRequest("Add an academic session first.")
    students = (db.query(Student).join(Enrollment, Enrollment.student_id == Student.id)
                .filter(Enrollment.company_id == cid, Enrollment.session_id == sess.id, Student.status == "active").all())
    for s in students:
        service.ensure_sibling_concession(db, cid, s, sess.id)
        service.reprice_open_invoices(db, cid, s.id, sess.id)
    n = db.query(func.count(FeeConcession.id)).filter(FeeConcession.company_id == cid, FeeConcession.auto.is_(True),
                                                      FeeConcession.status == "approved").scalar()
    db.commit()
    return {"learners": len(students), "sibling_concessions": n}


# ── Refunds ──────────────────────────────────────────────────────────

@router.get("/refunds")
def list_refunds(status: str | None = None, student_id: int | None = None, params: PageParams = Depends(),
                 db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    qy = db.query(FeeRefund).options(joinedload(FeeRefund.student)).filter(FeeRefund.company_id == user["company_id"])
    if status:
        qy = qy.filter(FeeRefund.status.in_(status.split(",")))
    if student_id:
        qy = qy.filter(FeeRefund.student_id == student_id)
    return paginate(qy.order_by(FeeRefund.id.desc()), params, refund_out)


class RefundIn(BaseModel):
    student_uuid: UUID
    amount: Decimal = Field(..., gt=0)
    reason: str = Field(..., min_length=2, max_length=500)
    source: str = Field("wallet", pattern="^(wallet|deposit|payment)$")
    payment_uuid: UUID | None = None


@router.post("/refunds", status_code=201)
def create_refund(payload: RefundIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(REFUND))):
    cid = user["company_id"]
    s = _student(db, cid, payload.student_uuid)
    pid = None
    if payload.payment_uuid:
        pid = get_or_404(db, FeePayment, cid, payload.payment_uuid, "Receipt").id
    r = service.request_refund(db, cid, s, payload.amount, payload.reason, payload.source, pid)
    db.commit()
    db.refresh(r)
    return refund_out(r)


@router.post("/students/{uuid}/deposit-refund", status_code=201)
def deposit_refund(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(REFUND))):
    """Refund the whole refundable deposit (for a learner who is leaving)."""
    cid = user["company_id"]
    s = _student(db, cid, uuid)
    amount = service.deposit_refundable(db, cid, s.id) - service._pending_refunds(db, cid, s.id, "deposit")
    if amount <= 0:
        raise BadRequest("There is no deposit to refund.")
    r = service.request_refund(db, cid, s, amount, "Deposit refund on leaving", "deposit")
    db.commit()
    db.refresh(r)
    return refund_out(r)


class RefundPayIn(BaseModel):
    mode: str = Field("bank_transfer", pattern="^(cash|cheque|dd|upi|card|bank_transfer|online)$")
    reference: str | None = Field(None, max_length=120)
    paid_on: date | None = None


@router.post("/refunds/{uuid}/pay")
def pay_refund(uuid: UUID, payload: RefundPayIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(REFUND))):
    r = get_or_404(db, FeeRefund, user["company_id"], uuid, "Refund")
    service.pay_refund(db, r, payload.mode, payload.reference, payload.paid_on)
    db.commit()
    db.refresh(r)
    return refund_out(r)


# ── Dues and reminders ───────────────────────────────────────────────

@router.get("/dues")
def dues(q_: str | None = Query(None, alias="q"), session_id: int | None = None, program_id: int | None = None,
         section_id: int | None = None, overdue_only: bool = False, bucket: str | None = None, format: str | None = None,
         params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    rows = service.dues_by_student(db, user["company_id"], session_id=session_id, program_id=program_id,
                                   section_id=section_id, q_text=q_, overdue_only=overdue_only)
    if bucket:
        rows = [r for r in rows if r["bucket"] == bucket]
    if format == "csv":
        audit.event(db, "export.downloaded", "fee_invoices", None, {"report": "dues"})
        db.commit()
        name, text = reports.csv_for("dues", {"rows": rows})
        return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})
    ageing = {"0_30": D0, "31_60": D0, "61_90": D0, "90_plus": D0}
    for r in rows:
        if r["bucket"] in ageing:
            ageing[r["bucket"]] += r["overdue"]
    start = params.offset
    return {"items": rows[start:start + params.size], "total": len(rows), "page": params.page, "size": params.size,
            "totals": {"due": sum((r["due"] for r in rows), D0), "overdue": sum((r["overdue"] for r in rows), D0),
                       "defaulters": sum(1 for r in rows if r["overdue"] > 0)}, "ageing": ageing}


class RemindIn(BaseModel):
    student_ids: list[int] | None = Field(None, max_length=5000)
    overdue_only: bool = True
    program_id: int | None = None
    section_id: int | None = None


@router.post("/dues/remind")
def remind(payload: RemindIn, db: Session = Depends(get_db), user: dict = Depends(require_any(COLLECT, MANAGE))):
    """Remind families of dues: chosen learners, or everyone (overdue) in a class or section."""
    cid = user["company_id"]
    ids = payload.student_ids
    if not ids:
        ids = [r["student_id"] for r in service.dues_by_student(db, cid, program_id=payload.program_id,
                                                                section_id=payload.section_id,
                                                                overdue_only=payload.overdue_only)]
    res = service.send_reminders(db, cid, ids)
    db.commit()
    return res


# ── Reports ──────────────────────────────────────────────────────────

@router.get("/reports/{kind}")
def report(kind: str, date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
           session_id: int | None = None, program_id: int | None = None, section_id: int | None = None,
           group: str = "section", format: str | None = None, db: Session = Depends(get_db),
           user: dict = Depends(require_permission(VIEW))):
    from modules.academics.service import current_session
    cid = user["company_id"]
    on = service.today(db, cid)
    date_to = date_to or on
    date_from = date_from or date_to
    if (date_to - date_from).days > 800:
        raise BadRequest("Pick a period of up to two years.")
    if session_id is None and kind in ("outstanding", "concessions", "register", "instalments"):
        cur = current_session(db, cid)
        session_id = cur.id if cur else None
    if kind == "daily":
        data = reports.daily_collection(db, cid, date_from, date_to)
    elif kind == "heads":
        data = reports.head_wise(db, cid, date_from, date_to)
    elif kind == "outstanding":
        data = reports.outstanding(db, cid, session_id, group)
    elif kind == "concessions":
        data = reports.concession_report(db, cid, session_id)
    elif kind == "register":
        data = reports.fee_register(db, cid, session_id, program_id, section_id)
    elif kind == "instalments":
        data = reports.instalment_dues(db, cid, session_id)
    else:
        raise NotFound("Report")
    if format == "csv":
        audit.event(db, "export.downloaded", "fee_payments", None, {"report": kind})
        db.commit()
        name, text = reports.csv_for(kind, data)
        return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return {**data, "from": date_from, "to": date_to, "session_id": session_id}


# ── Cash book (other income and expenses) ────────────────────────────

def ledger_out(db: Session, e: FeeLedgerEntry) -> dict:
    from modules.documents.storage import with_links
    return {"id": e.id, "uuid": str(e.uuid), "number": e.number, "kind": e.kind, "category_id": e.category_id,
            "category": e.category.name if e.category else None, "entry_date": e.entry_date, "amount": q(e.amount),
            "mode": e.mode, "reference": e.reference, "party": e.party, "description": e.description,
            "attachments": with_links(db, e.company_id, e.attachments or []), "status": e.status,
            "cancel_reason": e.cancel_reason, "created_by_name": e.created_by_name, "created_at": e.created_at}


@router.get("/ledger")
def list_ledger(kind: str | None = None, category_id: int | None = None, q_: str | None = Query(None, alias="q"),
                date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
                params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = user["company_id"]
    on = service.today(db, cid)
    date_to = date_to or on
    date_from = date_from or date_to.replace(day=1)
    qy = db.query(FeeLedgerEntry).options(joinedload(FeeLedgerEntry.category)).filter(
        FeeLedgerEntry.company_id == cid, FeeLedgerEntry.entry_date >= date_from, FeeLedgerEntry.entry_date <= date_to)
    if kind:
        qy = qy.filter(FeeLedgerEntry.kind == kind)
    if category_id:
        qy = qy.filter(FeeLedgerEntry.category_id == category_id)
    if q_:
        like = f"%{q_.strip()}%"
        qy = qy.filter(or_(FeeLedgerEntry.party.ilike(like), FeeLedgerEntry.description.ilike(like),
                           FeeLedgerEntry.number.ilike(like), FeeLedgerEntry.reference.ilike(like)))
    live = qy.filter(FeeLedgerEntry.status == "active")
    income = q(live.filter(FeeLedgerEntry.kind == "income").with_entities(func.coalesce(func.sum(FeeLedgerEntry.amount), 0)).order_by(None).scalar())
    expense = q(live.filter(FeeLedgerEntry.kind == "expense").with_entities(func.coalesce(func.sum(FeeLedgerEntry.amount), 0)).order_by(None).scalar())
    fees_in = service._collected(db, cid, date_from, date_to)
    refunds = q(db.query(func.coalesce(func.sum(FeeRefund.amount), 0)).filter(
        FeeRefund.company_id == cid, FeeRefund.status == "paid", FeeRefund.paid_on >= date_from,
        FeeRefund.paid_on <= date_to).scalar())
    out = paginate(qy.order_by(FeeLedgerEntry.entry_date.desc(), FeeLedgerEntry.id.desc()), params,
                   lambda e: ledger_out(db, e))
    out["totals"] = {"fee_collection": fees_in, "other_income": income, "expenses": expense, "refunds": refunds,
                     "net": fees_in + income - expense - refunds}
    out["by_category"] = [{"kind": k, "category": n or "Uncategorised", "amount": q(a)} for k, n, a in (
        db.query(FeeLedgerEntry.kind, FeeLedgerCategory.name, func.sum(FeeLedgerEntry.amount))
        .outerjoin(FeeLedgerCategory, FeeLedgerCategory.id == FeeLedgerEntry.category_id)
        .filter(FeeLedgerEntry.company_id == cid, FeeLedgerEntry.status == "active",
                FeeLedgerEntry.entry_date >= date_from, FeeLedgerEntry.entry_date <= date_to)
        .group_by(FeeLedgerEntry.kind, FeeLedgerCategory.name).all())]
    out["from"], out["to"] = date_from, date_to
    return out


class LedgerIn(BaseModel):
    kind: str = Field(..., pattern="^(income|expense)$")
    category_id: int | None = None
    entry_date: date
    amount: Decimal = Field(..., gt=0)
    mode: str = Field("cash", pattern="^(cash|cheque|dd|upi|card|bank_transfer|online)$")
    reference: str | None = Field(None, max_length=120)
    party: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=2000)
    attachments: list[str] = Field(default_factory=list, max_length=10)


def _ledger_save(db: Session, cid: int, data: dict, e: FeeLedgerEntry | None = None) -> FeeLedgerEntry:
    from modules.documents.storage import attachments_from
    if data.get("category_id"):
        cat = db.query(FeeLedgerCategory).filter(FeeLedgerCategory.company_id == cid,
                                                 FeeLedgerCategory.id == data["category_id"]).first()
        if cat is None or cat.kind != data["kind"]:
            raise BadRequest("Pick a category of the same kind.")
    if data["entry_date"] > service.today(db, cid):
        raise BadRequest("The date cannot be in the future.")
    data["attachments"] = attachments_from(db, cid, data.get("attachments"))
    if e is None:
        e = FeeLedgerEntry(company_id=cid, created_by_name=context.current().actor_name)
        e.number = service._next(db, cid, "fee_voucher", "VCH", FeeLedgerEntry)
        db.add(e)
    elif e.status != "active":
        raise BadRequest("This entry is cancelled.")
    for k, v in data.items():
        setattr(e, k, q(v) if k == "amount" else v)
    db.flush()
    return e


@router.post("/ledger", status_code=201)
def create_ledger(payload: LedgerIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    e = _ledger_save(db, user["company_id"], payload.model_dump())
    db.commit()
    db.refresh(e)
    return ledger_out(db, e)


@router.put("/ledger/{uuid}")
def update_ledger(uuid: UUID, payload: LedgerIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    e = get_or_404(db, FeeLedgerEntry, user["company_id"], uuid, "Entry")
    _ledger_save(db, user["company_id"], payload.model_dump(), e)
    db.commit()
    db.refresh(e)
    return ledger_out(db, e)


@router.post("/ledger/{uuid}/cancel")
def cancel_ledger(uuid: UUID, payload: ReasonIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    e = get_or_404(db, FeeLedgerEntry, user["company_id"], uuid, "Entry")
    if e.status != "active":
        raise BadRequest("This entry is already cancelled.")
    e.status, e.cancel_reason = "cancelled", payload.reason
    db.commit()
    db.refresh(e)
    return ledger_out(db, e)


# ── Portal (parents and students) ────────────────────────────────────

PORTAL = require_portal("parent", "student")


def _portal_student(db: Session, user: dict, student_id: int):
    from modules.students.service import require_student_access
    require_student_access(db, user, student_id)
    return _student_by_id(db, user["company_id"], student_id)


def _online_info(db: Session, cid: int) -> dict:
    cfg = fee_settings.get(db, cid)
    provider, _ = gateway.effective(db, cid)
    enabled = bool(cfg.get("online_payments", True)) and provider != "off"
    return {"online_enabled": enabled, "provider": provider if enabled else None, "test_mode": provider == "test",
            "allow_partial_online": cfg.get("allow_partial_online", True)}


@portal_router.get("/summary")
def portal_summary(student_id: int, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    cid = user["company_id"]
    s = _portal_student(db, user, student_id)
    d = service.student_dues(db, cid, s.id)
    late = service.preview_dues(db, cid, s.id)["late_fee"]
    d["late_fee"] = late
    d["total"] = d["total"] + late
    return {**d, "student_id": s.id, "currency": fee_settings.currency(db, cid),
            "wallet": service.wallet_balance(db, cid, s.id), **_online_info(db, cid)}


@portal_router.get("/account")
def portal_account(student_id: int, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    cid = user["company_id"]
    s = _portal_student(db, user, student_id)
    return {**_account(db, cid, s, staff=False), **_online_info(db, cid)}


def _portal_payment(db: Session, user: dict, uuid: UUID) -> FeePayment:
    p = service.get_payment(db, user["company_id"], uuid)
    from modules.students.service import visible_student_ids
    ids = visible_student_ids(db, user) or set()
    if p.student_id not in ids:
        raise NotFound("Receipt")
    return p


@portal_router.get("/receipts/{uuid}/pdf")
def portal_receipt(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    p = _portal_payment(db, user, uuid)
    return _pdf(pdf.receipt_html(db, p, fee_settings.get(db, user["company_id"])["receipt_note"]),
                f"receipt-{(p.number or '').replace('/', '-')}.pdf")


@portal_router.get("/invoices/{uuid}/pdf")
def portal_invoice(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    from modules.students.service import visible_student_ids
    inv = service.get_invoice(db, user["company_id"], uuid)
    if inv.student_id not in (visible_student_ids(db, user) or set()):
        raise NotFound("Invoice")
    return _pdf(pdf.invoice_html(db, inv), f"invoice-{(inv.number or '').replace('/', '-')}.pdf")


class OrderIn(BaseModel):
    student_id: int
    invoice_uuids: list[str] = Field(default_factory=list, max_length=24)
    return_url: str | None = Field(None, max_length=500)


@portal_router.post("/orders", status_code=201)
def portal_order(payload: OrderIn, request: Request, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    cid = user["company_id"]
    s = _portal_student(db, user, payload.student_id)
    res = gateway.create_order(db, cid, s, payload.invoice_uuids, portal_account_id=user.get("portal_id"),
                               return_url=payload.return_url, api_base=str(request.base_url))
    db.commit()
    return res


def _portal_order(db: Session, user: dict, uuid: UUID):
    from modules.students.service import visible_student_ids
    o = gateway.get_order(db, user["company_id"], uuid)
    if o.student_id not in (visible_student_ids(db, user) or set()):
        raise NotFound("Payment")
    return o


def order_out(db: Session, o) -> dict:
    receipt = None
    if o.payment_id:
        p = db.query(FeePayment).filter(FeePayment.id == o.payment_id).first()
        receipt = {"uuid": str(p.uuid), "number": p.number} if p else None
    return {"order": str(o.uuid), "status": o.status, "amount": q(o.amount), "currency": o.currency,
            "provider": o.provider, "test_mode": o.test_mode, "receipt": receipt, "error": o.error}


@portal_router.get("/orders/{uuid}")
def portal_order_status(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    return order_out(db, _portal_order(db, user, uuid))


@portal_router.post("/orders/{uuid}/verify")
def portal_verify(uuid: UUID, data: dict, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    o = _portal_order(db, user, uuid)
    gateway.verify(db, o, data or {})
    db.commit()
    return order_out(db, o)


class TestPayIn(BaseModel):
    succeed: bool = True


@portal_router.post("/orders/{uuid}/test-pay")
def portal_test_pay(uuid: UUID, payload: TestPayIn, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    o = _portal_order(db, user, uuid)
    gateway.test_pay(db, o, payload.succeed)
    db.commit()
    return order_out(db, o)


# ── Public: provider callbacks ───────────────────────────────────────

def _company_by_slug(db: Session, slug: str) -> int:
    from modules.company.models import Company
    with bypass_rls(db):
        cid = db.query(Company.id).filter(Company.slug == slug, Company.active.is_(True)).scalar()
    if cid is None:
        raise NotFound("Company")
    set_tenant(db, cid)
    return cid


@public_router.post("/webhook/{provider}/{slug}")
async def provider_webhook(provider: str, slug: str, request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    cid = _company_by_slug(db, slug)
    ctx = context.current()
    ctx.actor_type, ctx.actor_name = "system", f"{provider.title()} webhook"
    res = gateway.webhook(db, cid, provider, body, {k.lower(): v for k, v in request.headers.items()})
    db.commit()
    return res


@public_router.post("/return/payu/{slug}")
async def payu_return(slug: str, request: Request, db: Session = Depends(get_db)):
    """PayU posts the result here; we check the hash, record the payment and send the parent back."""
    from urllib.parse import parse_qs
    body = (await request.body()).decode()
    form = {k: v[0] for k, v in parse_qs(body).items()}
    cid = _company_by_slug(db, slug)
    ctx = context.current()
    ctx.actor_type, ctx.actor_name = "system", "PayU"
    gateway.payu_return(db, cid, form)
    db.commit()
    back = form.get("udf2") or ""
    if not back.startswith(("http://", "https://")):
        from core.workflow.email_actions import company_url
        back = f"{company_url(db, cid)}/dashboard/my/fees"
    return RedirectResponse(back, status_code=303)


extra_routers = [portal_router, public_router]
