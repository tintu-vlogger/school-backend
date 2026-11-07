"""PDFs: payslips (English with Hindi labels, password-protected), Form 16 Part B and the
full and final settlement statement. HTML rendered by WeasyPrint (proper Devanagari
shaping through the system fonts), then encrypted with pypdf.
"""
from __future__ import annotations

import html
import io
import secrets
from collections import defaultdict
from datetime import date

from modules.payroll import calc
from modules.payroll.calc import ZERO, d

FONT = "'Helvetica Neue', Helvetica, Arial, 'Noto Sans', 'DejaVu Sans', sans-serif"
HINDI = "'Kohinoor Devanagari', 'Noto Sans Devanagari', 'Devanagari Sangam MN', 'Lohit Devanagari', 'Mangal', sans-serif"
HI = {"payslip": "वेतन पर्ची", "earnings": "आय", "deductions": "कटौतियाँ", "gross": "सकल वेतन", "total_deductions": "कुल कटौती",
      "net": "शुद्ध वेतन", "employer": "नियोक्ता का योगदान", "days": "भुगतान दिवस", "lop": "वेतन रहित दिवस",
      "tax": "आयकर सारांश", "leave": "अवकाश शेष", "ytd": "वर्ष अब तक"}


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def inr(x) -> str:
    v = d(x)
    neg = v < 0
    v = abs(v).quantize(d("0.01"))
    whole, frac = str(v).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'−' if neg else ''}₹{whole}" + (f".{frac}" if frac != "00" else "")


def render(html_text: str) -> bytes:
    from weasyprint import HTML
    return HTML(string=html_text).write_pdf()


def encrypt(pdf: bytes, password: str | None) -> bytes:
    if not password:
        return pdf
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(pdf))
    writer = PdfWriter()
    for p in reader.pages:
        writer.add_page(p)
    writer.encrypt(user_password=password, owner_password=secrets.token_hex(16), algorithm="AES-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def payslip_password(snapshot: dict, scheme: str) -> str | None:
    """pan: the PAN in capitals; pan_dob: first five PAN letters + date of birth DDMM;
    none: no password. Without a PAN: employee ID + year of birth."""
    if scheme == "none":
        return None
    pan = (snapshot.get("pan") or "").upper().strip()
    dob = snapshot.get("date_of_birth")
    if pan and scheme == "pan":
        return pan
    if pan and scheme == "pan_dob" and dob:
        return pan[:5] + date.fromisoformat(dob).strftime("%d%m")
    code = (snapshot.get("employee_code") or "").upper()
    return code + (dob[:4] if dob else "")


def password_hint(scheme: str) -> str:
    return {"pan": "Your PAN in capital letters", "pan_dob": "First five letters of your PAN and your birth day and month (DDMM)",
            "none": "No password"}.get(scheme, "")


BASE_CSS = f"""
@page {{ size: A4; margin: 14mm 14mm 16mm; @bottom-center {{ content: counter(page) " / " counter(pages); font-size: 7.5pt; color: #9aa0ad; }} }}
* {{ box-sizing: border-box; }}
body {{ font-family: {FONT}; font-size: 9pt; color: #1f2430; margin: 0; font-weight: 300; }}
.hi {{ font-family: {HINDI}; color: #8a8f9c; font-size: 7.5pt; font-weight: 400; }}
h1 {{ font-size: 15pt; font-weight: 300; margin: 0; letter-spacing: -0.2pt; }}
h2 {{ font-size: 10pt; font-weight: 400; margin: 0 0 6pt; color: #4b5060; }}
.muted {{ color: #6b7080; }}
.top {{ display: flex; justify-content: space-between; align-items: flex-start; padding-bottom: 10pt; border-bottom: 1.5pt solid var(--accent); }}
.logo {{ max-height: 34pt; max-width: 140pt; margin-bottom: 4pt; }}
.title {{ text-align: right; }}
.title .period {{ font-size: 11pt; color: var(--accent); margin-top: 2pt; }}
.grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 6pt 12pt; margin: 12pt 0; padding: 10pt 12pt; background: #f6f7fb; border-radius: 6pt; }}
.grid .k {{ font-size: 7pt; text-transform: uppercase; letter-spacing: 0.4pt; color: #8a8f9c; }}
.grid .v {{ font-size: 9pt; color: #1f2430; }}
.cols {{ display: flex; gap: 12pt; }}
.cols > div {{ flex: 1; }}
table {{ width: 100%; border-collapse: collapse; }}
th {{ text-align: left; font-weight: 400; font-size: 7.5pt; text-transform: uppercase; letter-spacing: 0.4pt; color: #8a8f9c; padding: 5pt 6pt; border-bottom: 0.6pt solid #d9dce4; }}
td {{ padding: 4.5pt 6pt; border-bottom: 0.4pt solid #eceef3; vertical-align: top; }}
td.r, th.r {{ text-align: right; white-space: nowrap; }}
tr.total td {{ border-top: 0.8pt solid #c9ccd4; border-bottom: none; font-weight: 400; color: #1f2430; }}
.net {{ margin-top: 12pt; padding: 12pt 14pt; border: 1pt solid var(--accent); border-radius: 8pt; display: flex; justify-content: space-between; align-items: center; }}
.net .amt {{ font-size: 18pt; font-weight: 300; color: var(--accent); }}
.small {{ font-size: 7.5pt; }}
.boxes {{ display: flex; gap: 12pt; margin-top: 12pt; }}
.box {{ flex: 1; border: 0.6pt solid #e2e4ea; border-radius: 6pt; padding: 8pt 10pt; }}
.foot {{ margin-top: 14pt; font-size: 7.5pt; color: #9aa0ad; text-align: center; }}
"""


def payslip_html(p, company: dict, settings: dict, *, ytd: dict | None = None, leave: list[dict] | None = None) -> str:
    ps = settings.get("payslip") or {}
    accent = ps.get("accent") or "#4f46e5"
    hindi = ps.get("hindi", True)
    s = p.snapshot or {}
    lines = p.lines or []
    earnings = [ln for ln in lines if ln["kind"] in ("earning", "reimbursement")]
    deductions = [ln for ln in lines if ln["kind"] == "deduction"]
    employer = [ln for ln in lines if ln["kind"] == "employer" and ln.get("show", True)]
    show_ytd = ps.get("show_ytd", True) and ytd is not None

    def hi(key_or_text, raw=False):
        if not hindi:
            return ""
        t = key_or_text if raw else HI.get(key_or_text, "")
        return f'<div class="hi">{e(t)}</div>' if t else ""

    def rows(items):
        out = []
        for ln in items:
            y = f'<td class="r muted">{inr(ytd.get(ln["code"], ZERO) + d(ln["amount"]))}</td>' if show_ytd else ""
            note = f'<div class="small muted">{e(ln.get("note"))}</div>' if ln.get("note") else ""
            out.append(f'<tr><td>{e(ln["name"])}{hi(ln.get("name_hi"), raw=True) if ln.get("name_hi") else ""}{note}</td>'
                       f'<td class="r">{inr(ln["amount"])}</td>{y}</tr>')
        return "".join(out)

    yh = f'<th class="r">YTD{hi("ytd")}</th>' if show_ytd else ""
    span = 3 if show_ytd else 2
    bank = s.get("bank") or {}
    pan = s.get("pan") or ""
    pan_masked = (pan[:2] + "•••••" + pan[-3:]) if len(pan) == 10 else (pan or "Not given")
    period = p.period_start.strftime("%B %Y")
    logo = f'<img class="logo" src="{e(company.get("logo_url"))}">' if company.get("logo_url") else ""
    tax = p.tax or {}
    ws = tax.get("worksheet") or {}
    tax_box = ""
    if ps.get("show_tax", True) and ws:
        tax_box = (f'<div class="box"><h2>Tax summary{hi("tax")}</h2><table>'
                   f'<tr><td>Regime</td><td class="r">{"New" if ws.get("regime") == "new" else "Old"}</td></tr>'
                   f'<tr><td>Projected taxable income</td><td class="r">{inr(ws.get("taxable_income"))}</td></tr>'
                   f'<tr><td>Tax for the year</td><td class="r">{inr(ws.get("annual_tax"))}</td></tr>'
                   f'<tr><td>Deducted before this month</td><td class="r">{inr(tax.get("deducted_so_far"))}</td></tr>'
                   f'<tr><td>This month</td><td class="r">{inr(tax.get("tds"))}</td></tr></table></div>')
    leave_box = ""
    if ps.get("show_leave", True) and leave:
        leave_box = ('<div class="box"><h2>Leave balance' + hi("leave") + '</h2><table>' +
                     "".join(f'<tr><td>{e(x["name"])}</td><td class="r">{e(x["balance"])}</td></tr>' for x in leave[:6]) +
                     "</table></div>")
    emp_box = ""
    if employer:
        emp_box = ('<div class="box"><h2>Employer contributions' + hi("employer") + '</h2><table>' +
                   "".join(f'<tr><td>{e(x["name"])}</td><td class="r">{inr(x["amount"])}</td></tr>' for x in employer) +
                   "</table></div>")
    arrears = ""
    if p.arrears:
        arrears = '<p class="small muted" style="margin-top:6pt">Arrears: ' + ", ".join(
            f'{e(a.get("name") or a["code"])} {e(a["period"])} {inr(a["amount"])}' for a in p.arrears) + "</p>"
    hold = f'<p class="small" style="color:#b45309;margin-top:8pt">Salary on hold: {e(p.hold_reason)}</p>' if p.status == "held" else ""
    grid = [("Employee", s.get("name")), ("Employee ID", s.get("employee_code")), ("Designation", s.get("designation")),
            ("Department", s.get("department")), ("Date of joining", s.get("date_of_joining")), ("PAN", pan_masked),
            ("UAN", s.get("uan") or "—"), ("Bank", f'{bank.get("name") or ""} ••{bank.get("last4") or ""}' if bank else "—"),
            ("Days in month", p.days_in_period), ("Paid days", p.payable_days), ("Loss of pay", p.lop_days),
            ("ESI number", s.get("esi_number") or "—")]
    grid_html = "".join(f'<div><div class="k">{e(k)}</div><div class="v">{e(v if v not in (None, "") else "—")}</div></div>'
                        for k, v in grid)
    return f"""<html><head><meta charset="utf-8"><style>:root{{--accent:{accent}}} {BASE_CSS}</style></head><body>
<div class="top"><div>{logo}<h1>{e(company.get("name"))}</h1><div class="muted small">{e(company.get("address"))}</div></div>
<div class="title"><div class="muted">Payslip</div>{hi("payslip")}<div class="period">{e(period)}</div></div></div>
<div class="grid">{grid_html}</div>
<div class="cols"><div><table><tr><th>Earnings{hi("earnings")}</th><th class="r">Amount</th>{yh}</tr>{rows(earnings)}
<tr class="total"><td>Gross pay{hi("gross")}</td><td class="r">{inr(p.gross)}</td>{'<td></td>' if show_ytd else ''}</tr></table></div>
<div><table><tr><th>Deductions{hi("deductions")}</th><th class="r">Amount</th>{yh}</tr>{rows(deductions) or f'<tr><td colspan="{span}" class="muted">None</td></tr>'}
<tr class="total"><td>Total deductions{hi("total_deductions")}</td><td class="r">{inr(p.deductions)}</td>{'<td></td>' if show_ytd else ''}</tr></table></div></div>
<div class="net"><div><div class="muted">Net pay</div>{hi("net")}<div class="small muted">{e(calc.amount_in_words(p.net))}</div></div><div class="amt">{inr(p.net)}</div></div>
{arrears}{hold}
<div class="boxes">{emp_box}{tax_box}{leave_box}</div>
<div class="foot">{e(ps.get("footer") or "")}</div>
</body></html>"""


def payslip_pdf(p, company: dict, settings: dict, *, ytd: dict | None = None, leave: list[dict] | None = None,
                with_password: bool = True) -> bytes:
    pdf = render(payslip_html(p, company, settings, ytd=ytd, leave=leave))
    scheme = (settings.get("payslip") or {}).get("password", "pan")
    return encrypt(pdf, payslip_password(p.snapshot or {}, scheme)) if with_password else pdf


def ytd_by_code(slips) -> dict:
    out = defaultdict(lambda: ZERO)
    for p in slips:
        for ln in p.lines or []:
            out[ln["code"]] += d(ln["amount"])
    return out


# ── Form 16 Part B ─────────────────────────────────────────────────

def form16_html(emp: dict, entity: dict, fy: str, ws: dict, months: list[dict], forms: dict) -> str:
    title = forms.get("certificate", "Form 16")
    rows = "".join(f'<tr><td>{e(m["month"])}</td><td class="r">{inr(m["gross"])}</td><td class="r">{inr(m["tds"])}</td></tr>'
                   for m in months)
    ex = "".join(f'<tr><td>{e(x["label"])}</td><td class="r">{inr(x["amount"])}</td></tr>' for x in ws.get("exemptions") or [])
    de = "".join(f'<tr><td>{e(x["label"])}</td><td class="r">{inr(x["amount"])}</td></tr>' for x in ws.get("deductions") or [])
    t = ws.get("tax") or {}
    a, b = calc.fy_bounds(fy)
    return f"""<html><head><meta charset="utf-8"><style>:root{{--accent:#1f2430}} {BASE_CSS} td {{ font-size: 8.5pt; }}</style></head><body>
<div class="top"><div><h1>{e(title)} — Part B</h1><div class="muted small">Certificate of tax deducted at source on salary, {e(a.strftime('%d %b %Y'))} to {e(b.strftime('%d %b %Y'))}</div></div>
<div class="title"><div class="muted">Financial year {e(fy)}</div></div></div>
<div class="grid"><div><div class="k">Employer</div><div class="v">{e(entity.get("legal_name"))}</div></div>
<div><div class="k">TAN</div><div class="v">{e(entity.get("tan") or "—")}</div></div>
<div><div class="k">Employer PAN</div><div class="v">{e(entity.get("pan") or "—")}</div></div>
<div><div class="k">Regime</div><div class="v">{"New (default)" if ws.get("regime") == "new" else "Old"}</div></div>
<div><div class="k">Employee</div><div class="v">{e(emp.get("name"))}</div></div>
<div><div class="k">Employee PAN</div><div class="v">{e(emp.get("pan") or "Not given")}</div></div>
<div><div class="k">Employee ID</div><div class="v">{e(emp.get("employee_code"))}</div></div>
<div><div class="k">Designation</div><div class="v">{e(emp.get("designation") or "—")}</div></div></div>
<h2>Details of salary paid and tax deducted</h2>
<table><tr><td>1. Gross salary (incl. perquisites)</td><td class="r">{inr(d(ws.get("salary")) + d(ws.get("perquisites")))}</td></tr>
<tr><td>&nbsp;&nbsp;Salary from previous employer</td><td class="r">{inr(ws.get("previous_employer_income"))}</td></tr>
<tr><td>&nbsp;&nbsp;Other income declared</td><td class="r">{inr(ws.get("other_income"))}</td></tr></table>
<h2 style="margin-top:10pt">2. Exemptions and standard deduction</h2><table>{ex or '<tr><td class="muted">None</td><td></td></tr>'}</table>
<h2 style="margin-top:10pt">3. Deductions</h2><table>{de or '<tr><td class="muted">None</td><td></td></tr>'}</table>
<h2 style="margin-top:10pt">4. Tax</h2><table>
<tr><td>Total taxable income</td><td class="r">{inr(ws.get("taxable_income"))}</td></tr>
<tr><td>Tax on total income</td><td class="r">{inr(t.get("tax_on_income"))}</td></tr>
<tr><td>Rebate</td><td class="r">{inr(d(t.get("rebate")) + d(t.get("rebate_marginal_relief")))}</td></tr>
<tr><td>Surcharge</td><td class="r">{inr(t.get("surcharge"))}</td></tr>
<tr><td>Health and education cess</td><td class="r">{inr(t.get("cess"))}</td></tr>
<tr class="total"><td>Tax payable</td><td class="r">{inr(ws.get("annual_tax"))}</td></tr>
<tr><td>Tax deducted</td><td class="r">{inr(sum((d(m["tds"]) for m in months), ZERO))}</td></tr></table>
<h2 style="margin-top:10pt">Month by month</h2><table><tr><th>Month</th><th class="r">Gross</th><th class="r">TDS</th></tr>{rows}</table>
<p class="small muted" style="margin-top:14pt">Verification: I, {e((entity.get("signatory") or {}).get("name") or "the authorised signatory")}, certify that the information above is true and correct and is based on the books of account and other records available.</p>
</body></html>"""


def merge(parts: list[bytes]) -> bytes:
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for part in parts:
        for page in PdfReader(io.BytesIO(part)).pages:
            w.add_page(page)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


# ── Settlement statement ────────────────────────────────────────────

def settlement_html(s, emp_name: str, emp_code: str | None, company: dict, kind_label: str) -> str:
    earn = [x for x in s.lines or [] if x["kind"] == "earning"]
    ded = [x for x in s.lines or [] if x["kind"] == "deduction"]

    def rows(items):
        return "".join(f'<tr><td>{e(x["label"])}</td><td class="r">{inr(x["amount"])}</td></tr>' for x in items) or \
            '<tr><td class="muted">None</td><td></td></tr>'
    w = s.workings or {}
    g = w.get("gratuity") or {}
    return f"""<html><head><meta charset="utf-8"><style>:root{{--accent:#4f46e5}} {BASE_CSS}</style></head><body>
<div class="top"><div><h1>{e(company.get("name"))}</h1><div class="muted small">{e(company.get("address"))}</div></div>
<div class="title"><div class="muted">Full and final settlement</div><div class="period">{e(s.number or "")}</div></div></div>
<div class="grid"><div><div class="k">Employee</div><div class="v">{e(emp_name)}</div></div>
<div><div class="k">Employee ID</div><div class="v">{e(emp_code or "—")}</div></div>
<div><div class="k">Exit</div><div class="v">{e(kind_label)}</div></div>
<div><div class="k">Last working day</div><div class="v">{e(s.last_working_day.strftime("%d %b %Y"))}</div></div>
<div><div class="k">Payable by</div><div class="v">{e(s.due_date.strftime("%d %b %Y"))}</div></div>
<div><div class="k">Service</div><div class="v">{e(f"{(g.get('service') or {}).get('years', '—')} years {(g.get('service') or {}).get('months', '')} months" if g else "—")}</div></div>
<div><div class="k">Paid on</div><div class="v">{e(s.paid_on.strftime("%d %b %Y") if s.paid_on else "—")}</div></div>
<div><div class="k">Reference</div><div class="v">{e(s.payment_ref or "—")}</div></div></div>
<div class="cols"><div><table><tr><th>Payable to you</th><th class="r">Amount</th></tr>{rows(earn)}
<tr class="total"><td>Total</td><td class="r">{inr(s.total_earnings)}</td></tr></table></div>
<div><table><tr><th>Recovered</th><th class="r">Amount</th></tr>{rows(ded)}
<tr class="total"><td>Total</td><td class="r">{inr(s.total_deductions)}</td></tr></table></div></div>
<div class="net"><div><div class="muted">Net settlement</div><div class="small muted">{e(calc.amount_in_words(max(d(s.net), ZERO)))}</div></div><div class="amt">{inr(s.net)}</div></div>
<p class="small muted" style="margin-top:14pt">This statement settles all dues between you and the company as of your last working day, as required by the Code on Wages, 2019. Please write to HR within 30 days if anything looks wrong.</p>
<div style="display:flex;justify-content:space-between;margin-top:40pt" class="small muted"><div>For {e(company.get("name"))}<br><br>Authorised signatory</div><div>Received<br><br>{e(emp_name)}</div></div>
</body></html>"""


# ── Full and final statement, laid out like a payslip ───────────────

def fnf_statement_html(s, info: dict, company: dict, kind_label: str, accent: str = "#4f46e5") -> str:
    """`info`: the employee snapshot (name, code, designation, department, joining date, PAN,
    UAN, bank) as a payslip keeps it."""
    lines = s.lines or []
    w = s.workings or {}
    salary_rows, earn_rows, ded_rows = [], [], []
    for x in lines:
        if x["code"] == "SALARY":
            det = x.get("detail") or {}
            for ern in det.get("earnings") or []:
                salary_rows.append((ern["name"], ern["amount"], ""))
        elif x["code"] == "RESKILL":
            continue
        elif x["kind"] == "earning":
            note = f'Taxable {inr(x["taxable"])}' if d(x.get("taxable")) < d(x["amount"]) else ""
            earn_rows.append((x["label"], x["amount"], note))
        else:
            ded_rows.append((x["label"], x["amount"], ""))

    def rows(items):
        return "".join(f'<tr><td>{e(a)}{f"<div class=small muted>{e(n)}</div>" if n else ""}</td><td class="r">{inr(b)}</td></tr>'
                       for a, b, n in items) or '<tr><td class="muted" colspan="2">None</td></tr>'
    g = w.get("gratuity") or {}
    service = (g.get("service") or {})
    pan = info.get("pan") or ""
    pan_masked = (pan[:2] + "•••••" + pan[-3:]) if len(pan) == 10 else (pan or "Not given")
    bank = info.get("bank") or {}
    grid = [("Employee", info.get("name")), ("Employee ID", info.get("employee_code")), ("Designation", info.get("designation")),
            ("Department", info.get("department")), ("Date of joining", info.get("date_of_joining")),
            ("Last working day", s.last_working_day.strftime("%d %b %Y")), ("Exit", kind_label),
            ("Service", f"{service.get('years')} years {service.get('months')} months" if service else None),
            ("PAN", pan_masked), ("UAN", info.get("uan")),
            ("Bank", f'{bank.get("name") or ""} ••{bank.get("last4") or ""}' if bank else None),
            ("Pay by", s.due_date.strftime("%d %b %Y"))]
    grid_html = "".join(f'<div><div class="k">{e(k)}</div><div class="v">{e(v if v not in (None, "") else "—")}</div></div>'
                        for k, v in grid)
    employer = [x for x in w.get("employer") or [] if x.get("show", True)]
    emp_box = ""
    if employer:
        emp_box = ('<div class="box"><h2>Employer contributions (final month)</h2><table>' +
                   "".join(f'<tr><td>{e(x["name"])}</td><td class="r">{inr(x["amount"])}</td></tr>' for x in employer) +
                   "</table></div>")
    tax = w.get("tax") or {}
    ws = tax.get("worksheet") or {}
    tax_box = ""
    if ws:
        tax_box = (f'<div class="box"><h2>Income tax</h2><table>'
                   f'<tr><td>Regime</td><td class="r">{"New" if ws.get("regime") == "new" else "Old"}</td></tr>'
                   f'<tr><td>Taxable income for the year</td><td class="r">{inr(ws.get("taxable_income"))}</td></tr>'
                   f'<tr><td>Tax for the year</td><td class="r">{inr(ws.get("annual_tax"))}</td></tr>'
                   f'<tr><td>Deducted earlier</td><td class="r">{inr(tax.get("deducted_so_far"))}</td></tr>'
                   f'<tr><td>Deducted now</td><td class="r">{inr(s.tax)}</td></tr></table></div>')
    notice = s.notice or {}
    work_box = ('<div class="box"><h2>Workings</h2><table>' +
                (f'<tr><td>Gratuity</td><td class="r">{e(g.get("years_counted"))} years × 15/26 × {inr(g.get("wage"))}</td></tr>' if g.get("eligible") else
                 '<tr><td>Gratuity</td><td class="r">Not yet eligible</td></tr>' if g else "") +
                f'<tr><td>Notice</td><td class="r">{e(notice.get("required_days", "—"))} days required, {e(notice.get("served_days", 0))} served</td></tr>'
                f'<tr><td>Wages (Code on Wages)</td><td class="r">{inr(w.get("wages"))} a month</td></tr></table></div>')
    reskill = next((x for x in lines if x["code"] == "RESKILL"), None)
    reskill_note = (f'<p class="small muted" style="margin-top:6pt">Also paid by the employer into the worker re-skilling fund for you: '
                    f'{inr(reskill["amount"])}.</p>') if reskill else ""
    logo = f'<img class="logo" src="{e(company.get("logo_url"))}">' if company.get("logo_url") else ""
    sal_total = sum((d(a[1]) for a in salary_rows), ZERO)
    return f"""<html><head><meta charset="utf-8"><style>:root{{--accent:{accent}}} {BASE_CSS}</style></head><body>
<div class="top"><div>{logo}<h1>{e(company.get("name"))}</h1><div class="muted small">{e(company.get("address"))}</div></div>
<div class="title"><div class="muted">Full and final settlement</div><div class="period">{e(s.number or s.last_working_day.strftime("%B %Y"))}</div></div></div>
<div class="grid">{grid_html}</div>
<div class="cols"><div><table><tr><th>Earnings</th><th class="r">Amount</th></tr>{rows(salary_rows) if salary_rows else ""}
{f'<tr><td class="muted small" colspan="2">Salary for the final days: {inr(sal_total)}</td></tr>' if salary_rows else ""}
{rows(earn_rows) if earn_rows or not salary_rows else ""}
<tr class="total"><td>Total earnings</td><td class="r">{inr(s.total_earnings)}</td></tr></table></div>
<div><table><tr><th>Deductions and recoveries</th><th class="r">Amount</th></tr>{rows(ded_rows)}
<tr class="total"><td>Total deductions</td><td class="r">{inr(s.total_deductions)}</td></tr></table></div></div>
<div class="net"><div><div class="muted">Net settlement</div><div class="small muted">{e(calc.amount_in_words(max(d(s.net), ZERO)))}</div></div><div class="amt">{inr(s.net)}</div></div>
{reskill_note}
<div class="boxes">{emp_box}{tax_box}{work_box}</div>
<p class="small muted" style="margin-top:12pt">{f"Paid on {s.paid_on:%d %b %Y}, reference {e(s.payment_ref or '—')}. " if s.paid_on else ""}This statement settles all dues between you and the company as of your last working day, paid within two working days as the Code on Wages, 2019 requires. Please write to HR within 30 days if anything looks wrong.</p>
<div style="display:flex;justify-content:space-between;margin-top:34pt" class="small muted"><div>For {e(company.get("name"))}<br><br>Authorised signatory</div><div>Received<br><br>{e(info.get("name"))}</div></div>
</body></html>"""
