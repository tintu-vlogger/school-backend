"""Fee receipt and invoice as printable HTML / PDF (WeasyPrint through documents.pdf)."""
from __future__ import annotations

import html
from collections import OrderedDict
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.fees.models import FeeInvoice, FeePayment
from modules.fees.service import MODES, q

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n: int) -> str:
    return _ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))


def _three(n: int) -> str:
    h, r = divmod(n, 100)
    return ((_ONES[h] + " Hundred" + (" " if r else "")) if h else "") + (_two(r) if r else "")


def words(amount: Decimal, currency: str = "INR") -> str:
    """Amount in words: Indian grouping (lakh, crore) for INR, thousands/millions otherwise."""
    amount = q(amount)
    whole = int(amount)
    paise = int((amount - whole) * 100)
    if whole == 0:
        text = "Zero"
    elif currency == "INR":
        parts = []
        crore, rest = divmod(whole, 10_000_000)
        lakh, rest = divmod(rest, 100_000)
        thousand, rest = divmod(rest, 1000)
        if crore:
            parts.append(_three(crore) + " Crore") if crore < 1000 else parts.append(f"{crore} Crore")
        if lakh:
            parts.append(_two(lakh) + " Lakh")
        if thousand:
            parts.append(_two(thousand) + " Thousand")
        if rest:
            parts.append(_three(rest))
        text = " ".join(parts)
    else:
        parts = []
        for size, name in ((1_000_000_000, "Billion"), (1_000_000, "Million"), (1000, "Thousand")):
            n, whole = divmod(whole, size)
            if n:
                parts.append(_three(n) + " " + name)
        if whole:
            parts.append(_three(whole))
        text = " ".join(parts)
    unit, sub = ("Rupees", "Paise") if currency == "INR" else (currency, "cents")
    return f"{unit} {text}" + (f" and {_two(paise)} {sub}" if paise else "") + " only"


def _company(db: Session, company_id: int):
    from modules.company.models import Company
    return db.query(Company).filter(Company.id == company_id).first()


def _money(v, cur: str) -> str:
    return f"{cur} {q(v):,.2f}"


CSS = """
body { font-family: Helvetica, Arial, sans-serif; color: #1c1b19; font-size: 10.5pt; }
.hd { display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 1px solid #ccc; padding-bottom: 10px; }
.hd h1 { font-size: 16pt; font-weight: 400; margin: 0; }
.muted { color: #6b6a66; font-size: 9pt; }
.title { text-align: center; font-size: 12pt; letter-spacing: 0.12em; margin: 14px 0 10px; text-transform: uppercase; }
table { width: 100%; border-collapse: collapse; }
td, th { padding: 5px 6px; text-align: left; vertical-align: top; }
th { font-weight: 400; color: #6b6a66; border-bottom: 1px solid #ccc; font-size: 9pt; }
.lines td { border-bottom: 1px solid #eee; }
.r { text-align: right; }
.tot td { border-top: 1px solid #999; font-size: 11pt; }
.meta td { padding: 2px 6px; }
.stamp { margin-top: 28px; display: flex; justify-content: space-between; font-size: 9pt; color: #6b6a66; }
.void { color: #b42318; border: 1px solid #b42318; display: inline-block; padding: 2px 10px; letter-spacing: .1em; }
"""


def _header(company, label: str) -> str:
    addr = ", ".join(x for x in [company.address, company.city, company.state, company.pincode] if x) if company else ""
    gst = f"GSTIN {company.gstin}" if company and company.gstin else ""
    logo = f'<img src="{html.escape(company.logo_url)}" style="height:46px">' if company and company.logo_url and \
        company.logo_url.startswith(("http", "data:")) else ""
    return (f'<div class="hd"><div><h1>{html.escape(company.company_name if company else "")}</h1>'
            f'<div class="muted">{html.escape(addr)}</div><div class="muted">{html.escape(company.phone or "") if company else ""}'
            f'{" · " + html.escape(company.email) if company and company.email else ""}</div>'
            f'<div class="muted">{gst}</div></div>{logo}</div><div class="title">{label}</div>')


def receipt_html(db: Session, pay: FeePayment, note: str = "") -> str:
    from modules.fees.settings import currency
    from modules.students.service import current_enrollment
    from modules.academics.service import section_label
    company = _company(db, pay.company_id)
    cur = currency(db, pay.company_id)
    st = pay.student
    enr = current_enrollment(db, pay.company_id, st.id)
    groups: "OrderedDict[str, list]" = OrderedDict()
    for a in sorted(pay.allocations, key=lambda a: (a.invoice.due_date, a.invoice_id, a.line.sort_order)):
        groups.setdefault(a.invoice.title, []).append(a)
    rows = ""
    for title, allocs in groups.items():
        for i, a in enumerate(allocs):
            rows += (f'<tr><td>{html.escape(title) if i == 0 else ""}</td><td>{html.escape(a.line.description)}</td>'
                     f'<td class="r">{_money(a.amount, cur)}</td></tr>')
    if q(pay.advance) > 0:
        rows += f'<tr><td></td><td>Advance (kept for future dues)</td><td class="r">{_money(pay.advance, cur)}</td></tr>'
    modes = "".join(f"<tr><td>{MODES.get(m.mode, m.mode)}{' (bounced)' if m.status == 'bounced' else ''}</td>"
                    f"<td>{html.escape(' · '.join(x for x in [m.reference or '', m.bank or '', m.instrument_date.strftime('%d %b %Y') if m.instrument_date else ''] if x))}</td>"
                    f'<td class="r">{_money(m.amount, cur)}</td></tr>' for m in pay.modes)
    if q(pay.wallet_used) > 0:
        modes += f'<tr><td>Advance used</td><td></td><td class="r">{_money(pay.wallet_used, cur)}</td></tr>'
    void = f'<p><span class="void">{pay.status.upper()}</span> {html.escape(pay.cancel_reason or "")}</p>' if pay.status != "paid" else ""
    total = q(pay.amount) + q(pay.wallet_used)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Receipt {html.escape(pay.number or '')}</title>
<style>@page {{ size: A5 landscape; margin: 12mm; }} {CSS}</style></head><body>
{_header(company, "Fee receipt")}
{void}
<table class="meta"><tr><td><span class="muted">Receipt no.</span><br>{html.escape(pay.number or '')}</td>
<td><span class="muted">Date</span><br>{pay.paid_on:%d %b %Y}</td>
<td><span class="muted">Student</span><br>{html.escape(st.full_name)}</td>
<td><span class="muted">Admission no.</span><br>{html.escape(st.admission_no)}</td>
<td><span class="muted">Class</span><br>{html.escape(section_label(enr.section) if enr and enr.section else (enr.program.name if enr and enr.program else ''))}</td></tr></table>
<br><table class="lines"><tr><th>For</th><th>Fee</th><th class="r">Amount</th></tr>{rows}
<tr class="tot"><td></td><td>Total received</td><td class="r">{_money(total, cur)}</td></tr></table>
<p class="muted">{html.escape(words(total, cur))}</p>
<table class="lines"><tr><th>Paid by</th><th>Details</th><th class="r">Amount</th></tr>{modes}</table>
<div class="stamp"><span>Received by {html.escape(pay.collected_by_name or '')}</span><span>{html.escape(note)}</span></div>
</body></html>"""


def invoice_html(db: Session, inv: FeeInvoice) -> str:
    from modules.fees.settings import currency
    from modules.academics.service import section_label
    company = _company(db, inv.company_id)
    cur = currency(db, inv.company_id)
    rows = "".join(
        f'<tr><td>{html.escape(ln.description)}{"<br><span class=muted>" + html.escape(ln.concession_note) + "</span>" if ln.concession_note else ""}</td>'
        f'<td class="r">{_money(ln.amount, cur)}</td><td class="r">{_money(ln.concession, cur) if q(ln.concession) else "—"}</td>'
        f'<td class="r">{_money(ln.tax, cur) if q(ln.tax) else "—"}</td><td class="r">{_money(ln.net, cur)}</td></tr>'
        for ln in inv.lines)
    void = f'<p><span class="void">CANCELLED</span> {html.escape(inv.cancel_reason or "")}</p>' if inv.status == "cancelled" else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Invoice {html.escape(inv.number or '')}</title>
<style>@page {{ size: A4; margin: 16mm; }} {CSS}</style></head><body>
{_header(company, "Fee invoice")}
{void}
<table class="meta"><tr><td><span class="muted">Invoice no.</span><br>{html.escape(inv.number or '')}</td>
<td><span class="muted">Issued</span><br>{inv.issue_date:%d %b %Y}</td><td><span class="muted">Due</span><br>{inv.due_date:%d %b %Y}</td>
<td><span class="muted">Student</span><br>{html.escape(inv.student.full_name)} ({html.escape(inv.student.admission_no)})</td>
<td><span class="muted">Class</span><br>{html.escape(section_label(inv.section) if inv.section else '')}</td></tr></table>
<p>{html.escape(inv.title)}</p>
<table class="lines"><tr><th>Fee</th><th class="r">Amount</th><th class="r">Concession</th><th class="r">Tax</th><th class="r">Net</th></tr>{rows}
<tr class="tot"><td>Total</td><td class="r">{_money(inv.gross, cur)}</td><td class="r">{_money(inv.concession, cur)}</td>
<td class="r">{_money(inv.tax, cur)}</td><td class="r">{_money(inv.total, cur)}</td></tr></table>
<table style="width:45%;margin-left:55%;margin-top:10px">
<tr><td>Credit notes</td><td class="r">{_money(inv.credited, cur)}</td></tr>
<tr><td>Paid</td><td class="r">{_money(inv.paid, cur)}</td></tr>
<tr class="tot"><td>Balance due</td><td class="r">{_money(max(q(inv.balance), q(0)), cur)}</td></tr></table>
</body></html>"""


def to_pdf(doc_html: str) -> bytes:
    from modules.documents.pdf import render_pdf
    return render_pdf(doc_html)
