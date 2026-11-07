"""Salary bank files API (under /api/v1/payroll/bank).

Formats         GET  /payroll/bank/formats                    ready-made and custom layouts
                POST /payroll/bank/formats                    new custom layout
                PUT  /payroll/bank/formats/{uuid}             change one (or switch it off)
                POST /payroll/bank/formats/preview            render a layout with sample rows
Debit accounts  GET/POST /payroll/bank/accounts, PUT /payroll/bank/accounts/{uuid}
                GET  /payroll/bank/entity-accounts             accounts saved on legal entities, to pick from
Pay run files   GET  /payroll/bank/runs/{run}/check           files that would be made and problems
                POST /payroll/bank/runs/{run}/files           make the files (kept for the audit)
                GET  /payroll/bank/runs/{run}/files           files made for the run
History         GET  /payroll/bank/files                      every file made (paginated)
                GET  /payroll/bank/files/{uuid}/download

Permissions: viewing needs payroll.view; layouts and debit accounts need payroll.settings;
making and downloading files needs payroll.bank_files.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest
from core.security import require_permission
from modules.salary_bank import formats, service
from modules.salary_bank.models import BankDebitAccount, BankFileBatch

router = APIRouter(prefix="/payroll/bank", tags=["Salary bank files"])
VIEW = require_permission("payroll.view")
SETTINGS = require_permission("payroll.settings")
FILES = require_permission("payroll.bank_files")


def _run(db: Session, cid: int, uuid):
    from modules.payroll import runs
    return runs.get_run(db, cid, uuid)


# ── Formats ──────────────────────────────────────────────────────────

@router.get("/formats")
def list_formats(include_inactive: bool = False, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    return {"items": service.catalog(db, user["company_id"], include_inactive), "tokens": {
        "row": service.engine.TOKENS_ROW, "total": service.engine.TOKENS_TOTAL}}


class FormatIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    based_on: str | None = Field(None, max_length=80)
    spec: dict
    active: bool | None = None


@router.post("/formats", status_code=201)
def create_format(body: FormatIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    row = service.save_format(db, user["company_id"], body.model_dump())
    db.commit()
    return {"key": f"custom:{row.uuid}", "uuid": str(row.uuid), "name": row.name, "spec": row.spec}


@router.put("/formats/{uuid}")
def update_format(uuid: UUID, body: FormatIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    row = service.format_row(db, cid, uuid)
    if body.active is False:
        used = db.query(BankDebitAccount.id).filter(BankDebitAccount.company_id == cid, BankDebitAccount.active.is_(True),
                                                    BankDebitAccount.format_key == f"custom:{row.uuid}").first()
        if used:
            raise BadRequest("A debit account still uses this format. Choose another format for it first.")
    row = service.save_format(db, cid, body.model_dump(), row)
    db.commit()
    return {"key": f"custom:{row.uuid}", "uuid": str(row.uuid), "name": row.name, "spec": row.spec, "active": row.active}


class PreviewIn(BaseModel):
    spec: dict
    debit: dict | None = None


@router.post("/formats/preview")
def preview_format(body: PreviewIn, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    from modules.payroll import service as psvc
    bank = psvc.get_settings(db, user["company_id"]).get("bank") or {}
    return service.preview(body.spec, body.debit, {"other_bank_mode": bank.get("other_bank_mode") or "auto",
                                                   "rtgs_from": bank.get("rtgs_from") or 200000})


# ── Debit accounts ───────────────────────────────────────────────────

def _format_names(db: Session, cid: int) -> dict:
    return {f["key"]: f["name"] for f in service.catalog(db, cid, include_inactive=True)}


@router.get("/accounts")
def list_accounts(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    rows = (db.query(BankDebitAccount).filter(BankDebitAccount.company_id == cid)
            .order_by(BankDebitAccount.active.desc(), BankDebitAccount.legal_entity_id, BankDebitAccount.label).all())
    ents, names = service.entity_names(db, cid), _format_names(db, cid)
    return {"items": [service.account_out(r, ents, names) for r in rows], "total": len(rows)}


class AccountIn(BaseModel):
    label: str | None = Field(None, max_length=120)
    legal_entity_uuid: UUID | None = None
    account_number: str | None = Field(None, max_length=30)
    ifsc: str | None = Field(None, max_length=11)
    bank_name: str | None = Field(None, max_length=150)
    account_name: str | None = Field(None, max_length=200)
    format_key: str | None = Field(None, max_length=80)
    client_code: str | None = Field(None, max_length=40)
    corporate_id: str | None = Field(None, max_length=40)
    is_default: bool | None = None
    active: bool | None = None
    notes: str | None = Field(None, max_length=1000)


def _account_data(db: Session, cid: int, body: AccountIn) -> dict:
    data = body.model_dump(exclude_unset=True)
    data["legal_entity_id"] = service.entity_id_for(db, cid, body.legal_entity_uuid)
    data.pop("legal_entity_uuid", None)
    return data


@router.post("/accounts", status_code=201)
def create_account(body: AccountIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    row = service.save_account(db, cid, _account_data(db, cid, body))
    db.commit()
    return service.account_out(row, service.entity_names(db, cid), _format_names(db, cid))


@router.get("/accounts/{uuid}")
def get_account(uuid: UUID, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    row = service.account_row(db, cid, uuid)
    return service.account_out(row, service.entity_names(db, cid), _format_names(db, cid), full=True)


@router.put("/accounts/{uuid}")
def update_account(uuid: UUID, body: AccountIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    row = service.account_row(db, cid, uuid)
    data = _account_data(db, cid, body)
    if "legal_entity_uuid" not in body.model_fields_set:
        data["legal_entity_id"] = row.legal_entity_id
    row = service.save_account(db, cid, data, row)
    db.commit()
    return service.account_out(row, service.entity_names(db, cid), _format_names(db, cid))


@router.get("/entity-accounts")
def entity_accounts(user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    """Bank accounts saved on legal entities, offered when adding a debit account."""
    from core.masters import MasterValue
    from modules.organization.models import LegalEntity
    cid = user["company_id"]
    out = []
    rows = db.query(LegalEntity).filter(LegalEntity.company_id == cid, LegalEntity.active.is_(True)).all()
    bank_ids = [a.get("bank_id") for e in rows for a in (e.bank_accounts or []) if a.get("bank_id")]
    labels = dict(db.query(MasterValue.id, MasterValue.label).filter(MasterValue.id.in_(bank_ids or [-1])).all())
    for e in rows:
        for a in e.bank_accounts or []:
            if not a.get("account_number"):
                continue
            ifsc = (a.get("ifsc") or "").upper()
            out.append({"legal_entity_uuid": str(e.uuid), "legal_entity_name": e.name, "account_number": a["account_number"],
                        "account_masked": service.mask(a["account_number"]), "ifsc": ifsc,
                        "bank_name": labels.get(a.get("bank_id")) or formats.bank_label(ifsc),
                        "account_name": a.get("account_name") or e.legal_name or e.name,
                        "purpose": a.get("purpose"), "suggested_format": formats.suggest_for_ifsc(ifsc)})
    return out


@router.get("/suggest")
def suggest(ifsc: str, user: dict = Depends(VIEW)):
    ifsc = ifsc.strip().upper()
    return {"bank": formats.bank_label(ifsc), "format_key": formats.suggest_for_ifsc(ifsc),
            "valid": bool(service.engine.IFSC_RE.match(ifsc))}


# ── Pay run files ────────────────────────────────────────────────────

def _check_run(r) -> None:
    if r.status not in ("approved", "locked", "paid"):
        raise BadRequest("Approve the pay run before making the bank file.")


@router.get("/runs/{uuid}/check")
def check_run(uuid: UUID, format: str | None = None, released_only: bool = False, user: dict = Depends(FILES),
              db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = _run(db, cid, uuid)
    _check_run(r)
    return service.plan_out(service.plan(db, cid, r, format or None, released_only))


class GenerateIn(BaseModel):
    format: str | None = Field(None, max_length=80)
    released_only: bool = False
    leave_out_problems: bool = False


@router.post("/runs/{uuid}/files", status_code=201)
def generate(uuid: UUID, body: GenerateIn, user: dict = Depends(FILES), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = _run(db, cid, uuid)
    _check_run(r)
    rows = service.generate(db, cid, r, body.format or None, body.released_only, body.leave_out_problems)
    db.commit()
    return {"items": [service.batch_out(b, r.title) for b in rows]}


@router.get("/runs/{uuid}/files")
def run_files(uuid: UUID, user: dict = Depends(FILES), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = _run(db, cid, uuid)
    rows = (db.query(BankFileBatch).filter(BankFileBatch.company_id == cid, BankFileBatch.pay_run_id == r.id)
            .order_by(BankFileBatch.created_at.desc(), BankFileBatch.id.desc()).all())
    return {"items": [service.batch_out(b, r.title) for b in rows]}


@router.get("/files")
def all_files(page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200), user: dict = Depends(FILES),
              db: Session = Depends(get_db)):
    from modules.payroll.models import PayRun
    cid = user["company_id"]
    q = db.query(BankFileBatch).filter(BankFileBatch.company_id == cid)
    total = q.count()
    rows = q.order_by(BankFileBatch.created_at.desc(), BankFileBatch.id.desc()).offset((page - 1) * size).limit(size).all()
    titles = dict(db.query(PayRun.id, PayRun.title).filter(PayRun.id.in_([b.pay_run_id for b in rows] or [-1])).all())
    return {"items": [service.batch_out(b, titles.get(b.pay_run_id)) for b in rows], "total": total, "page": page, "size": size}


@router.get("/files/{uuid}/download")
def download(uuid: UUID, user: dict = Depends(FILES), db: Session = Depends(get_db)):
    b = service.batch_row(db, user["company_id"], uuid)
    service.downloaded(db, b)
    db.commit()
    return Response(content=b.content, media_type=b.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{b.file_name}"'})


