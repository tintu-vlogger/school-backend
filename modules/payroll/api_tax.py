"""Income tax API (HR) under /api/v1/payroll/tax.

GET  /tax/declarations?fy=&status=&q=        everyone's declarations with totals
GET  /tax/declarations/{uuid}                one declaration with its proofs
PUT  /tax/declarations/{uuid}                HR edits (regime, items, HRA, previous employer)
POST /tax/declarations/{uuid}/verify         accept / reject / partly accept items; complete
POST /tax/declarations/{uuid}/lock-regime
GET  /tax/employees/{employee_uuid}/compare?fy=, /worksheet?fy=
GET/POST /tax/perquisites, PUT /tax/perquisites/{uuid}
GET  /tax/return?fy=&quarter=&format=txt|xlsx     quarterly TDS return data file
GET  /tax/form16/{employee_uuid}?fy=, GET /tax/form16?fy= (zip of all), POST /tax/form16/part-a (upload)
GET  /tax/files/{uuid}                          a proof or Part A file
GET  /tax/items                                 what can be declared
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import audit
from core.db import get_db
from core.errors import BadRequest, Forbidden
from core.security import get_current_user, require_permission
from core.timeutil import today_for_company_id
from modules.payroll import calc, exports, taxsvc
from modules.payroll import service as svc
from modules.payroll.models import Perquisite, TaxDeclaration

router = APIRouter(prefix="/payroll/tax", tags=["Payroll tax"])

VIEW = require_permission("payroll.view")
TAX = require_permission("payroll.tax.manage")


def _file(name: str, media: str, content: bytes) -> Response:
    return Response(content=content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _fy(db: Session, cid: int, fy: str | None) -> str:
    return fy or calc.fy_of(today_for_company_id(db, cid))


@router.get("/items")
def items(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    from modules.payroll import statutory
    sections = statutory.rule(db, user["company_id"], "tax_sections", None, today_for_company_id(db, user["company_id"])) or {}
    return {"items": [{"key": k, "section": s, "label": label, "section_label": (sections.get(s) or {}).get("label", s),
                       "cap": (sections.get(s) or {}).get("cap"), "ref_2025": (sections.get(s) or {}).get("ref_2025")}
                      for s, k, label in taxsvc.ITEMS],
            "windows": taxsvc.windows(svc.get_settings(db, user["company_id"]), today_for_company_id(db, user["company_id"]),
                                      calc.fy_of(today_for_company_id(db, user["company_id"])))}


@router.get("/declarations")
def list_declarations(fy: str | None = None, status: str | None = None, q: str | None = None, page: int = 1,
                      size: int = Query(25, le=200), user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    fy = _fy(db, cid, fy)
    Employee = svc.employee_model()
    emps = db.query(Employee).filter(Employee.company_id == cid, Employee.active.is_(True)).order_by(Employee.first_name).all()
    decls = {x.employee_id: x for x in db.query(TaxDeclaration).filter(TaxDeclaration.company_id == cid, TaxDeclaration.fy == fy).all()}
    rows = []
    for e in emps:
        x = decls.get(e.id)
        row = taxsvc.declaration_out(x, {e.id: svc.name_of(e)}) if x else {
            "uuid": None, "employee_id": e.id, "employee_name": svc.name_of(e), "fy": fy, "regime": "new", "status": "not_started",
            "declared_total": "0.00", "approved_total": "0.00", "rent_total": "0.00", "items": []}
        row["employee_uuid"], row["employee_code"] = str(e.uuid), e.employee_code
        row["pan"] = (e.pan[:2] + "•••••" + e.pan[-3:]) if e.pan and len(e.pan) == 10 else e.pan
        if status and row["status"] not in status.split(","):
            continue
        if q and q.lower() not in (row["employee_name"] or "").lower() and q.lower() not in (e.employee_code or "").lower():
            continue
        rows.append(row)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"items": rows[(page - 1) * size: page * size], "total": len(rows), "page": page, "size": size, "counts": counts,
            "fy": fy}


@router.post("/declarations/start/{employee_uuid}")
def start_declaration(employee_uuid: UUID, fy: str | None = None, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = svc.employee_by_uuid(db, cid, employee_uuid)
    x = taxsvc.declaration(db, cid, e.id, _fy(db, cid, fy))
    db.commit()
    return taxsvc.declaration_out(x, {e.id: svc.name_of(e)})


@router.get("/declarations/{uuid}")
def get_declaration(uuid: UUID, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    x = taxsvc.get_by_uuid(db, cid, uuid)
    e = svc.get_employee(db, cid, x.employee_id)
    return {**taxsvc.declaration_out(x, {e.id: svc.name_of(e)}), "files": taxsvc.proof_files(db, cid, x),
            "employee_uuid": str(e.uuid), "pan": e.pan}


class DeclarationIn(BaseModel):
    regime: str | None = None
    items: list[dict] | None = None
    hra: dict | None = None
    previous_employer: dict | None = None
    other_income: float | None = None
    extra_tds_monthly: float | None = None
    fbp: dict | None = None
    submit: bool = False


@router.put("/declarations/{uuid}")
def update_declaration(uuid: UUID, body: DeclarationIn, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    x = taxsvc.get_by_uuid(db, cid, uuid)
    taxsvc.save(db, cid, x, body.model_dump(exclude_unset=True), by_hr=True)
    db.commit()
    return taxsvc.declaration_out(x)


class VerifyIn(BaseModel):
    items: list[dict] = []
    hra_approved: float | None = None
    hra_comment: str | None = None
    complete: bool = False


@router.post("/declarations/{uuid}/verify")
def verify(uuid: UUID, body: VerifyIn, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    x = taxsvc.get_by_uuid(db, cid, uuid)
    taxsvc.verify(db, cid, x, body.model_dump(exclude_unset=True))
    db.commit()
    return taxsvc.declaration_out(x)


@router.post("/declarations/{uuid}/lock-regime")
def lock_regime(uuid: UUID, locked: bool = True, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    x = taxsvc.get_by_uuid(db, user["company_id"], uuid)
    x.regime_locked = locked
    db.commit()
    return taxsvc.declaration_out(x)


@router.get("/employees/{employee_uuid}/compare")
def compare(employee_uuid: UUID, fy: str | None = None, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = svc.employee_by_uuid(db, cid, employee_uuid)
    out = taxsvc.compare(db, cid, e, _fy(db, cid, fy))
    db.rollback()
    return out


@router.get("/employees/{employee_uuid}/worksheet")
def worksheet(employee_uuid: UUID, fy: str | None = None, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = svc.employee_by_uuid(db, cid, employee_uuid)
    out = taxsvc.worksheet(db, cid, e, _fy(db, cid, fy))
    db.rollback()
    return out


@router.get("/files/{uuid}")
def get_file(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    from modules.payroll.router import perms
    f = taxsvc.get_file(db, user["company_id"], uuid)
    if "payroll.tax.manage" not in perms(db, user) and f.employee_id != user.get("employee_id"):
        raise Forbidden("You can only see your own files.")
    return Response(content=f.content, media_type=f.content_type,
                    headers={"Content-Disposition": f'inline; filename="{f.filename}"'})


# ── Perquisites ─────────────────────────────────────────────────────

class PerqIn(BaseModel):
    employee_uuid: UUID | None = None
    kind: str
    details: dict = {}
    effective_from: date | None = None
    effective_to: date | None = None
    note: str | None = None
    active: bool = True


@router.get("/perquisites")
def list_perqs(user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    rows = db.query(Perquisite).filter(Perquisite.company_id == cid).order_by(Perquisite.effective_from.desc()).all()
    names = {e.id: svc.name_of(e) for e in db.query(svc.employee_model()).filter(
        svc.employee_model().id.in_([r.employee_id for r in rows] or [-1])).all()}
    loans_rows = []
    try:
        from modules.loans.models import Loan
        from modules.loans import service as loans
        today = today_for_company_id(db, cid)
        for (eid,) in db.query(Loan.employee_id).filter(Loan.company_id == cid, Loan.status == "active").distinct().all():
            v = loans.perquisite_value(db, cid, eid, today)
            if v > 0:
                e = svc.get_employee(db, cid, eid)
                loans_rows.append({"employee_name": svc.name_of(e), "monthly_value": str(v)})
    except ImportError:
        pass
    return {"items": [taxsvc.perquisite_out(r, names) for r in rows], "loans": loans_rows}


@router.post("/perquisites", status_code=201)
def add_perq(body: PerqIn, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    if not body.employee_uuid:
        raise BadRequest("Choose the employee.")
    e = svc.employee_by_uuid(db, cid, body.employee_uuid)
    row = taxsvc.save_perquisite(db, cid, e, body.model_dump())
    db.commit()
    return taxsvc.perquisite_out(row, {e.id: svc.name_of(e)})


@router.put("/perquisites/{uuid}")
def update_perq(uuid: UUID, body: PerqIn, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    row = db.query(Perquisite).filter(Perquisite.company_id == cid, Perquisite.uuid == uuid).first()
    if not row:
        raise BadRequest("Perquisite not found.")
    e = svc.get_employee(db, cid, row.employee_id)
    taxsvc.save_perquisite(db, cid, e, body.model_dump(), row)
    db.commit()
    return taxsvc.perquisite_out(row, {e.id: svc.name_of(e)})


# ── Returns and certificates ───────────────────────────────────────

@router.get("/return")
def tds_return(fy: str | None = None, quarter: int = 1, format: str = "txt", user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    name, media, content = exports.tds_return(db, cid, _fy(db, cid, fy), quarter, fmt=format)
    audit.event(db, "export.downloaded", "payslips", None, {"report": "tds_return", "fy": fy, "quarter": quarter})
    db.commit()
    return _file(name, media, content)


@router.get("/form16")
def form16_all(fy: str | None = None, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    fy = _fy(db, cid, fy)
    content = taxsvc.form16_bulk(db, cid, fy)
    db.commit()
    return _file(f"Form16-{fy}.zip", "application/zip", content)


@router.get("/form16/{employee_uuid}")
def form16_one(employee_uuid: UUID, fy: str | None = None, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = svc.employee_by_uuid(db, cid, employee_uuid)
    fy = _fy(db, cid, fy)
    content = taxsvc.form16_pdf(db, cid, e, fy)
    audit.event(db, "export.downloaded", "payslips", None, {"report": "form16", "employee_id": e.id, "fy": fy})
    db.commit()
    return _file(f"Form16-{fy}-{e.employee_code or e.id}.pdf", "application/pdf", content)


@router.post("/form16/part-a")
async def form16_part_a(fy: str = Query(...), files: list[UploadFile] = File(...), user: dict = Depends(TAX),
                        db: Session = Depends(get_db)):
    data = [(f.filename or "part-a.pdf", await f.read()) for f in files]
    res = taxsvc.upload_part_a(db, user["company_id"], fy, data)
    db.commit()
    return res


@router.post("/opening-balances")
async def opening_balances(fy: str = Query(...), file: UploadFile = File(...), user: dict = Depends(TAX),
                           db: Session = Depends(get_db)):
    """CSV or Excel: employee_code, gross, taxable, tds, pf, pt, basic_da, hra (pay earlier in the year)."""
    import csv
    import io
    content = await file.read()
    if (file.filename or "").lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        it = load_workbook(io.BytesIO(content), read_only=True, data_only=True).worksheets[0].iter_rows(values_only=True)
        header = [str(h or "").strip().lower() for h in next(it, [])]
        rows = [dict(zip(header, r)) for r in it if any(v not in (None, "") for v in r)]
    else:
        rows = [{(k or "").strip().lower(): v for k, v in r.items()}
                for r in csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="replace")))]
    res = taxsvc.import_opening(db, user["company_id"], fy, rows)
    db.commit()
    return res
