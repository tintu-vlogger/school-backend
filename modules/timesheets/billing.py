"""Timesheet billing: hourly rates and GST invoices per client per period.

Rates (`ts_billing_rates`), most specific first, in force on the day of the work:
    person on a project  >  project  >  person
Only approved, billable time on billable projects that is not on an invoice yet is billed.

Invoice: one line per project and person (hours x rate). GST from the `billing` setting
(rate, SAC code, the company's GSTIN and state). Place of supply is the client's state
(from its GSTIN when no state is set); the same state as the company gives CGST + SGST
(half the rate each), another state IGST. Status: draft -> sent -> paid (void frees the
hours again). Draft invoices can be changed; sent and paid ones are fixed.
"""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core import audit, context
from core.errors import BadRequest, NotFound
from core.timeutil import iso_utc
from modules.timesheets.models import TimeLog, TsBillingRate, TsClient, TsInvoice, TsProject

ZERO = Decimal("0.00")
STATUSES = {"draft": "Draft", "sent": "Sent", "paid": "Paid", "void": "Void"}
STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh",
    "23": "Madhya Pradesh", "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}


def money(x) -> Decimal:
    return Decimal(str(x or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def settings(db: Session, company_id: int) -> dict:
    """The `billing` time setting; the company's own name and address fill the blanks."""
    from modules.attendance import settings_store
    from modules.attendance.service import today
    from modules.company.models import Company
    cfg = settings_store.get(db, company_id, "billing", today(db, company_id))
    if not cfg.get("supplier_name") or not cfg.get("supplier_address"):
        c = db.query(Company).filter(Company.id == company_id).first()
        if c is not None:
            cfg["supplier_name"] = cfg.get("supplier_name") or c.company_name
            cfg["supplier_address"] = cfg.get("supplier_address") or ", ".join(
                x for x in (c.address, c.city, c.state) if x)
    return cfg


# ── Rates ───────────────────────────────────────────────────────────

def rate_out(r: TsBillingRate, projects: dict, names: dict) -> dict:
    return {"uuid": str(r.uuid), "project_id": r.project_id, "project_name": projects.get(r.project_id),
            "employee_id": r.employee_id, "employee_name": names.get(r.employee_id), "rate": str(r.rate),
            "effective_from": r.effective_from.isoformat(), "note": r.note, "active": r.active,
            "applies_to": "person on project" if r.project_id and r.employee_id else
            ("project" if r.project_id else "person")}


def save_rate(db: Session, company_id: int, data: dict, row: TsBillingRate | None = None) -> TsBillingRate:
    pid, eid = data.get("project_id"), data.get("employee_id")
    if not pid and not eid:
        raise BadRequest("Choose a project, a person or both.")
    if pid and not db.query(TsProject.id).filter(TsProject.company_id == company_id, TsProject.id == pid).first():
        raise BadRequest("That project doesn't exist.")
    if eid:
        from modules.attendance.service import employee
        employee(db, company_id, int(eid))
    rate = money(data.get("rate"))
    if rate <= 0 or rate > Decimal("10000000"):
        raise BadRequest("Enter an hourly rate above zero.")
    eff = data.get("effective_from")
    if not isinstance(eff, date):
        raise BadRequest("Choose the date the rate starts.")
    if row is None:
        row = TsBillingRate(company_id=company_id)
        db.add(row)
    row.project_id, row.employee_id = pid or None, eid or None
    row.rate, row.effective_from = rate, eff
    row.note = (data.get("note") or "").strip() or None
    if data.get("active") is not None:
        row.active = bool(data["active"])
    db.flush()
    return row


class RateBook:
    def __init__(self, db: Session, company_id: int):
        self.rows = (db.query(TsBillingRate).filter(TsBillingRate.company_id == company_id,
                                                    TsBillingRate.active.is_(True))
                     .order_by(TsBillingRate.effective_from.desc(), TsBillingRate.id.desc()).all())

    def rate(self, project_id: int, employee_id: int, day: date) -> Decimal | None:
        for match in (lambda r: r.project_id == project_id and r.employee_id == employee_id,
                      lambda r: r.project_id == project_id and r.employee_id is None,
                      lambda r: r.project_id is None and r.employee_id == employee_id):
            for r in self.rows:
                if match(r) and r.effective_from <= day:
                    return Decimal(r.rate)
        return None


# ── Invoices ────────────────────────────────────────────────────────

def _billable_logs(db: Session, company_id: int, client: TsClient, start: date, end: date,
                   invoice_id: int | None = None) -> list[TimeLog]:
    q = (db.query(TimeLog).join(TsProject, TsProject.id == TimeLog.project_id)
         .filter(TimeLog.company_id == company_id, TsProject.client_id == client.id, TsProject.billable.is_(True),
                 TimeLog.billable.is_(True), TimeLog.status == "approved", TimeLog.date >= start, TimeLog.date <= end))
    if invoice_id:
        q = q.filter((TimeLog.invoice_id.is_(None)) | (TimeLog.invoice_id == invoice_id))
    else:
        q = q.filter(TimeLog.invoice_id.is_(None))
    return q.options(joinedload(TimeLog.project)).order_by(TimeLog.date).all()


def tax_split(subtotal: Decimal, rate: Decimal, supplier_state: str | None, place: str | None) -> dict:
    """CGST + SGST within the same state, IGST across states."""
    intra = not place or not supplier_state or place == supplier_state
    tax = money(subtotal * rate / 100)
    if intra:
        half = money(tax / 2)
        return {"tax_mode": "intra", "cgst": half, "sgst": tax - half, "igst": ZERO}
    return {"tax_mode": "inter", "cgst": ZERO, "sgst": ZERO, "igst": tax}


def build_lines(db: Session, company_id: int, logs: list[TimeLog]) -> tuple[list[dict], list[str]]:
    from modules.attendance.service import names_for
    book = RateBook(db, company_id)
    names = names_for(db, company_id, [l.employee_id for l in logs])
    groups: dict[tuple, dict] = {}
    missing = set()
    for l in logs:
        rate = book.rate(l.project_id, l.employee_id, l.date)
        if rate is None:
            missing.add(f"{names.get(l.employee_id, 'Someone')} on {l.project.name}")
            continue
        key = (l.project_id, l.employee_id, str(rate))
        g = groups.setdefault(key, {"project_id": l.project_id, "project_name": l.project.name,
                                    "project_code": l.project.code, "employee_id": l.employee_id,
                                    "employee_name": names.get(l.employee_id), "hours": ZERO, "rate": rate,
                                    "log_ids": [], "from": l.date, "to": l.date})
        g["hours"] += Decimal(l.hours)
        g["log_ids"].append(l.id)
        g["from"], g["to"] = min(g["from"], l.date), max(g["to"], l.date)
    lines = []
    for g in sorted(groups.values(), key=lambda x: (x["project_name"], x["employee_name"] or "")):
        amount = money(g["hours"] * g["rate"])
        lines.append({**g, "hours": str(money(g["hours"])), "rate": str(money(g["rate"])), "amount": str(amount),
                      "from": g["from"].isoformat(), "to": g["to"].isoformat(),
                      "description": f"{g['project_name']}: {g['employee_name']}"})
    return lines, sorted(missing)


def _client(db: Session, company_id: int, uuid) -> TsClient:
    c = db.query(TsClient).filter(TsClient.company_id == company_id, TsClient.uuid == uuid).first()
    if c is None:
        raise NotFound("Client")
    return c


def preview(db: Session, company_id: int, client: TsClient, start: date, end: date,
            invoice: TsInvoice | None = None) -> dict:
    if end < start or (end - start).days > 370:
        raise BadRequest("Choose a period of up to a year.")
    cfg = settings(db, company_id)
    logs = _billable_logs(db, company_id, client, start, end, invoice.id if invoice else None)
    lines, missing = build_lines(db, company_id, logs)
    subtotal = sum((Decimal(l["amount"]) for l in lines), ZERO)
    rate = money(cfg.get("gst_rate") if cfg.get("gst_rate") is not None else 18)
    supplier_state = f"{int(cfg['supplier_state']):02d}" if str(cfg.get("supplier_state") or "").isdigit() else None
    place = client.state_code or ((client.gstin or "")[:2] if client.gstin else None) or supplier_state
    tax = tax_split(subtotal, rate, supplier_state, place)
    total = subtotal + tax["cgst"] + tax["sgst"] + tax["igst"]
    return {"client": {"uuid": str(client.uuid), "name": client.name, "gstin": client.gstin,
                       "state_code": client.state_code, "address": client.address},
            "period_start": start.isoformat(), "period_end": end.isoformat(), "lines": lines,
            "missing_rates": missing, "hours": str(sum((Decimal(l["hours"]) for l in lines), ZERO)),
            "subtotal": str(subtotal), "gst_rate": str(rate), "place_of_supply": place,
            "place_of_supply_name": STATES.get(place or "", ""), "supplier_state": supplier_state,
            **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in tax.items()}, "total": str(total)}


def _next_number(db: Session, company_id: int, prefix: str, on: date) -> str:
    fy = on.year if on.month >= 4 else on.year - 1
    label = f"{prefix or 'INV'}/{fy % 100:02d}-{(fy + 1) % 100:02d}/"
    n = (db.query(func.count(TsInvoice.id)).filter(TsInvoice.company_id == company_id,
                                                   TsInvoice.number.like(f"{label}%")).scalar()) or 0
    return f"{label}{n + 1:04d}"


def create(db: Session, company_id: int, client: TsClient, start: date, end: date, data: dict) -> TsInvoice:
    pv = preview(db, company_id, client, start, end)
    if not pv["lines"]:
        raise BadRequest("There is no approved billable time with a rate for this client in that period.")
    cfg = settings(db, company_id)
    from modules.attendance.service import today
    issue = data.get("issue_date") or today(db, company_id)
    inv = TsInvoice(company_id=company_id, client_id=client.id, period_start=start, period_end=end, issue_date=issue,
                    status="draft", created_by_name=context.current().actor_name)
    db.add(inv)
    _apply(inv, pv, cfg, data)
    db.flush()
    _attach(db, company_id, inv, pv)
    audit.event(db, "timesheets.invoice_created", "ts_invoices", inv.id,
                {"client": client.name, "total": pv["total"]}, entity_uuid=str(inv.uuid))
    return inv


def _apply(inv: TsInvoice, pv: dict, cfg: dict, data: dict) -> None:
    inv.supplier = {"name": cfg.get("supplier_name") or "", "gstin": (cfg.get("supplier_gstin") or "").upper(),
                    "state": pv["supplier_state"], "state_name": STATES.get(pv["supplier_state"] or "", ""),
                    "address": cfg.get("supplier_address") or "", "bank_details": cfg.get("bank_details") or ""}
    inv.place_of_supply, inv.tax_mode = pv["place_of_supply"], pv["tax_mode"]
    inv.gst_rate, inv.sac_code = Decimal(pv["gst_rate"]), cfg.get("sac_code") or None
    inv.lines = [{k: v for k, v in l.items() if k != "log_ids"} | {"log_count": len(l["log_ids"])} for l in pv["lines"]]
    inv.hours, inv.subtotal = Decimal(pv["hours"]), Decimal(pv["subtotal"])
    inv.cgst, inv.sgst, inv.igst = Decimal(pv["cgst"]), Decimal(pv["sgst"]), Decimal(pv["igst"])
    inv.total = Decimal(pv["total"])
    if data.get("issue_date"):
        inv.issue_date = data["issue_date"]
    terms = int(cfg.get("payment_terms_days") or 0)
    inv.due_date = data.get("due_date") or (inv.issue_date + timedelta(days=terms) if terms else None)
    if "notes" in data:
        inv.notes = (data.get("notes") or "").strip() or None


def _attach(db: Session, company_id: int, inv: TsInvoice, pv: dict) -> None:
    ids = [i for l in pv["lines"] for i in l["log_ids"]]
    db.query(TimeLog).filter(TimeLog.company_id == company_id, TimeLog.invoice_id == inv.id,
                             ~TimeLog.id.in_(ids or [-1])).update({"invoice_id": None}, synchronize_session=False)
    if ids:
        db.query(TimeLog).filter(TimeLog.company_id == company_id, TimeLog.id.in_(ids)).update(
            {"invoice_id": inv.id}, synchronize_session=False)
    db.flush()


def get(db: Session, company_id: int, uuid) -> TsInvoice:
    inv = (db.query(TsInvoice).options(joinedload(TsInvoice.client))
           .filter(TsInvoice.company_id == company_id, TsInvoice.uuid == uuid).first())
    if inv is None:
        raise NotFound("Invoice")
    return inv


def refresh(db: Session, company_id: int, inv: TsInvoice, data: dict) -> TsInvoice:
    """Work a draft out again (new approved time, changed rates) and save edits."""
    if inv.status != "draft":
        raise BadRequest("Only a draft invoice can be changed.")
    pv = preview(db, company_id, inv.client, inv.period_start, inv.period_end, inv)
    _apply(inv, pv, settings(db, company_id), data)
    db.flush()
    _attach(db, company_id, inv, pv)
    return inv


def set_status(db: Session, company_id: int, inv: TsInvoice, status: str, reference: str | None = None) -> TsInvoice:
    allowed = {"draft": ("sent", "void"), "sent": ("paid", "void", "draft"), "paid": ("sent",), "void": ()}
    if status not in STATUSES:
        raise BadRequest("Unknown status.")
    if status not in allowed[inv.status]:
        raise BadRequest(f"A {STATUSES[inv.status].lower()} invoice can't be marked {STATUSES[status].lower()}.")
    now = datetime.now(timezone.utc)
    if status == "sent" and not inv.number:
        cfg = settings(db, company_id)
        inv.number = _next_number(db, company_id, cfg.get("invoice_prefix") or "INV", inv.issue_date)
    if status == "sent":
        inv.sent_at = inv.sent_at or now
        inv.paid_at, inv.paid_reference = None, None
    if status == "paid":
        inv.paid_at, inv.paid_reference = now, (reference or "").strip()[:120] or None
    if status == "void":
        db.query(TimeLog).filter(TimeLog.company_id == company_id, TimeLog.invoice_id == inv.id).update(
            {"invoice_id": None}, synchronize_session=False)
    old = inv.status
    inv.status = status
    db.flush()
    audit.event(db, "timesheets.invoice_status", "ts_invoices", inv.id, {"from": old, "to": status,
                                                                         "number": inv.number},
                entity_uuid=str(inv.uuid), entity_label=inv.number or "Draft invoice")
    return inv


def invoice_out(inv: TsInvoice, full: bool = False) -> dict:
    out = {"uuid": str(inv.uuid), "number": inv.number, "status": inv.status, "status_label": STATUSES[inv.status],
           "client": {"uuid": str(inv.client.uuid), "name": inv.client.name, "gstin": inv.client.gstin} if inv.client else None,
           "period_start": inv.period_start.isoformat(), "period_end": inv.period_end.isoformat(),
           "issue_date": inv.issue_date.isoformat(), "due_date": inv.due_date.isoformat() if inv.due_date else None,
           "hours": str(inv.hours), "subtotal": str(inv.subtotal), "cgst": str(inv.cgst), "sgst": str(inv.sgst),
           "igst": str(inv.igst), "total": str(inv.total), "tax_mode": inv.tax_mode, "gst_rate": str(inv.gst_rate),
           "place_of_supply": inv.place_of_supply, "place_of_supply_name": STATES.get(inv.place_of_supply or "", ""),
           "sent_at": iso_utc(inv.sent_at), "paid_at": iso_utc(inv.paid_at), "paid_reference": inv.paid_reference,
           "created_at": iso_utc(inv.created_at), "created_by_name": inv.created_by_name}
    if full:
        out.update({"lines": inv.lines or [], "supplier": inv.supplier or {}, "sac_code": inv.sac_code,
                    "notes": inv.notes, "client_address": inv.client.address if inv.client else None,
                    "client_state": inv.client.state_code if inv.client else None})
    return out


# ── PDF ─────────────────────────────────────────────────────────────

def _inr(x) -> str:
    v = money(x)
    neg = v < 0
    whole, frac = f"{abs(v):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{'-' if neg else ''}₹{whole}.{frac}"


def _words(n: int) -> str:
    ones = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
            "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

    def two(x):
        return ones[x] if x < 20 else (tens[x // 10] + (" " + ones[x % 10] if x % 10 else ""))

    def three(x):
        h, r = divmod(x, 100)
        return ((ones[h] + " hundred" + (" " if r else "")) if h else "") + (two(r) if r else "")
    if n == 0:
        return "zero"
    parts = []
    for size, name in ((10_000_000, "crore"), (100_000, "lakh"), (1000, "thousand")):
        q, n = divmod(n, size)
        if q:
            parts.append(f"{three(q) if q < 1000 else _words(q)} {name}")
    if n:
        parts.append(three(n))
    return " ".join(parts)


def amount_in_words(x) -> str:
    v = money(x)
    rupees = int(v)
    paise = int((v - rupees) * 100)
    out = f"Rupees {_words(rupees)}"
    if paise:
        out += f" and {_words(paise)} paise"
    return out[0].upper() + out[1:] + " only"


def pdf(inv: TsInvoice) -> bytes:
    from weasyprint import HTML
    e = lambda x: html.escape("" if x is None else str(x))  # noqa: E731
    s, c = inv.supplier or {}, inv.client
    rows = "".join(
        f"<tr><td>{i}</td><td>{e(l.get('project_name'))}<div class='m'>{e(l.get('employee_name'))} · "
        f"{e(l.get('from'))} to {e(l.get('to'))}</div></td><td>{e(inv.sac_code)}</td>"
        f"<td class='r'>{e(l.get('hours'))}</td><td class='r'>{_inr(l.get('rate'))}</td>"
        f"<td class='r'>{_inr(l.get('amount'))}</td></tr>" for i, l in enumerate(inv.lines or [], 1))
    rate = money(inv.gst_rate)
    if inv.tax_mode == "inter":
        taxes = f"<tr><td>IGST @ {rate.normalize()}%</td><td class='r'>{_inr(inv.igst)}</td></tr>"
    else:
        half = (rate / 2).normalize()
        taxes = (f"<tr><td>CGST @ {half}%</td><td class='r'>{_inr(inv.cgst)}</td></tr>"
                 f"<tr><td>SGST @ {half}%</td><td class='r'>{_inr(inv.sgst)}</td></tr>")
    title = "Tax invoice" if s.get("gstin") else "Invoice"
    doc = f"""<html><head><meta charset='utf-8'><style>
      @page {{ size: A4; margin: 16mm; }}
      body {{ font-family: Helvetica, Arial, sans-serif; color: #111; font-size: 9.5pt; font-weight: 300; }}
      h1 {{ font-weight: 300; font-size: 20pt; margin: 0 0 4mm; }}
      .top {{ display: flex; justify-content: space-between; }} .m {{ color: #666; font-size: 8pt; }}
      table {{ width: 100%; border-collapse: collapse; margin-top: 6mm; }}
      th {{ text-align: left; font-weight: 400; color: #555; border-bottom: 0.6pt solid #999; padding: 2mm 1mm; }}
      td {{ padding: 2mm 1mm; border-bottom: 0.3pt solid #ddd; vertical-align: top; }}
      .r {{ text-align: right; }} .tot {{ width: 45%; margin-left: auto; }}
      .tot td {{ border: none; padding: 1mm; }} .grand td {{ border-top: 0.6pt solid #111; font-size: 11pt; }}
      .box {{ margin-top: 6mm; }}
    </style></head><body>
    <h1>{title}</h1>
    <div class='top'><div><b>{e(s.get('name'))}</b><div class='m'>{e(s.get('address'))}</div>
      <div class='m'>GSTIN {e(s.get('gstin') or 'not registered')} · {e(s.get('state_name'))} ({e(s.get('state'))})</div></div>
      <div class='r'><div>Invoice no. {e(inv.number or 'Draft')}</div><div class='m'>Date {inv.issue_date:%d %b %Y}</div>
      <div class='m'>Due {inv.due_date.strftime('%d %b %Y') if inv.due_date else '-'}</div>
      <div class='m'>Period {inv.period_start:%d %b} to {inv.period_end:%d %b %Y}</div></div></div>
    <div class='box'><div class='m'>Bill to</div><div>{e(c.name if c else '')}</div>
      <div class='m'>{e(c.address if c else '')}</div>
      <div class='m'>GSTIN {e((c.gstin if c else None) or 'unregistered')} · Place of supply:
      {e(STATES.get(inv.place_of_supply or '', ''))} ({e(inv.place_of_supply)})</div></div>
    <table><thead><tr><th>#</th><th>Description</th><th>SAC</th><th class='r'>Hours</th><th class='r'>Rate</th>
      <th class='r'>Amount</th></tr></thead><tbody>{rows}</tbody></table>
    <table class='tot'><tr><td>Taxable value</td><td class='r'>{_inr(inv.subtotal)}</td></tr>{taxes}
      <tr class='grand'><td>Total</td><td class='r'>{_inr(inv.total)}</td></tr></table>
    <div class='box m'>{e(amount_in_words(inv.total))}</div>
    {f"<div class='box'><div class='m'>Notes</div>{e(inv.notes)}</div>" if inv.notes else ''}
    {f"<div class='box'><div class='m'>Bank details</div>{e(s.get('bank_details'))}</div>" if s.get('bank_details') else ''}
    <div class='box m'>This is a computer-generated invoice.</div>
    </body></html>"""
    return HTML(string=doc).write_pdf()


def summary(db: Session, company_id: int) -> dict:
    rows = db.query(TsInvoice.status, func.count(TsInvoice.id), func.coalesce(func.sum(TsInvoice.total), 0)).filter(
        TsInvoice.company_id == company_id).group_by(TsInvoice.status).all()
    return {s: {"count": n, "total": str(money(t))} for s, n, t in rows}


def unbilled(db: Session, company_id: int) -> list[dict]:
    """Approved billable hours not invoiced yet, per client."""
    rows = (db.query(TsClient.uuid, TsClient.name, func.sum(TimeLog.hours), func.min(TimeLog.date), func.max(TimeLog.date))
            .join(TsProject, TsProject.client_id == TsClient.id).join(TimeLog, TimeLog.project_id == TsProject.id)
            .filter(TsClient.company_id == company_id, TimeLog.status == "approved", TimeLog.billable.is_(True),
                    TsProject.billable.is_(True), TimeLog.invoice_id.is_(None))
            .group_by(TsClient.uuid, TsClient.name).all())
    return [{"client_uuid": str(u), "client_name": n, "hours": str(money(h)), "from": a.isoformat(), "to": b.isoformat()}
            for u, n, h, a, b in rows]

