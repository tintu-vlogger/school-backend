"""Files payroll produces: bank transfer files, the PF ECR, the ESI monthly file, PT and
LWF returns, accounting journals (Tally XML, CSV by GL code and cost centre), the wage,
bonus and gratuity registers and the quarterly TDS return data file.

Each function returns (filename, media type, bytes).
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date
from xml.sax.saxutils import escape

from sqlalchemy.orm import Session

from core import csv_safe
from core.errors import BadRequest
from modules.payroll import adapters, calc, statutory
from modules.payroll import service as svc
from modules.payroll.calc import ZERO, d, money, rnd
from modules.payroll.models import PayRun, Payslip

CSV = "text/csv"
TXT = "text/plain"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XML = "application/xml"


def _slips(db: Session, run: PayRun, statuses=("calculated", "held")) -> list[Payslip]:
    return (db.query(Payslip).filter(Payslip.pay_run_id == run.id, Payslip.status.in_(statuses))
            .order_by(Payslip.id).all())


def _amount(p: Payslip, *codes) -> object:
    return sum((d(ln["amount"]) for ln in p.lines or [] if ln["code"] in codes), ZERO)


def _csv_bytes(header: list[str] | None, rows: list[list], delimiter: str = ",") -> bytes:
    buf = io.StringIO()
    w = csv_safe.writer(buf, delimiter=delimiter) if delimiter == "," else csv.writer(buf, delimiter=delimiter)
    if header:
        w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _xlsx(sheets: list[tuple[str, list[str], list[list]]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    wb.remove(wb.active)
    for title, header, rows in sheets:
        ws = wb.create_sheet(title[:31])
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append(r)
        for i, h in enumerate(header, start=1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = max(10, min(40, len(str(h)) + 4))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _period_label(run: PayRun) -> str:
    return run.period_start.strftime("%Y-%m")


# ── Bank transfer files ─────────────────────────────────────────────

def bank_formats(settings: dict) -> dict:
    """Old shape of the format list ({key: {label, columns ...}}); the layouts now live in
    modules/salary_bank/formats.py."""
    from modules.salary_bank.formats import BUILTIN
    return {k: {"label": f["name"], "delimiter": f["spec"].get("delimiter"), "extension": f["spec"].get("extension"),
                "header": f["spec"].get("header_row"), "columns": [[c["label"], c["value"]] for c in f["spec"]["columns"]]}
            for k, f in BUILTIN.items()}


def bank_file(db: Session, company_id: int, run: PayRun, fmt_key: str | None = None, *, released_only: bool = False) -> tuple[str, str, bytes]:
    """Salary bank file(s) for a run, made by modules/salary_bank (validated, split by debit
    account and by bank rules, and recorded). Several files come back as one zip."""
    from modules.salary_bank import service as bank
    rows = bank.generate(db, company_id, run, fmt_key or None, released_only, leave_out_problems=False)
    if len(rows) == 1:
        return rows[0].file_name, rows[0].content_type, rows[0].content
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            z.writestr(r.file_name, r.content)
    return f"salary-bank-files-{_period_label(run)}.zip", "application/zip", buf.getvalue()


# ── PF ECR (EPFO ECR 2.0 text) ─────────────────────────────────────

ECR_SEP = "#~#"


def ecr(db: Session, company_id: int, run: PayRun) -> tuple[str, str, bytes]:
    """One line per member: UAN, name, gross wages, EPF wages, EPS wages, EDLI wages, EPF
    contribution (employee, incl. VPF), EPS contribution, EPF-EPS difference (employer to
    PF), NCP days, refund of advances. Whole rupees, '#~#' between fields."""
    lines, missing = [], []
    for p in _slips(db, run):
        pf = (p.statutory or {}).get("pf") or {}
        if not pf.get("applicable", True) or d(pf.get("employee")) == 0 and d(pf.get("employer_total")) == 0:
            continue
        s = p.snapshot or {}
        uan = (s.get("uan") or "").strip()
        if not uan:
            missing.append(s.get("name"))
            continue
        gross = sum((d(ln["amount"]) for ln in p.lines or [] if ln["kind"] == "earning"), ZERO)
        fields = [uan, (s.get("name") or "").upper(), int(rnd(gross)), int(rnd(pf.get("epf_wage"))),
                  int(rnd(pf.get("eps_wage"))), int(rnd(pf.get("edli_wage"))),
                  int(rnd(d(pf.get("employee")) + d(pf.get("vpf")))), int(rnd(pf.get("employer_eps"))),
                  int(rnd(pf.get("employer_epf"))), int(rnd(pf.get("ncp_days"))), 0]
        lines.append(ECR_SEP.join(str(f) for f in fields))
    if not lines:
        raise BadRequest("No PF members in this run." + (f" Missing UAN: {', '.join(x for x in missing if x)}." if missing else ""))
    return f"ECR-{_period_label(run)}.txt", TXT, ("\n".join(lines) + "\n").encode("utf-8")


def ecr_arrears(db: Session, company_id: int, run: PayRun) -> tuple[str, str, bytes]:
    """Arrear ECR: UAN, name, arrear EPF wages, arrear EPS wages, arrear EDLI wages, arrear
    EPF (employee), arrear EPS, arrear EPF (employer)."""
    book = statutory.RuleBook(db, company_id, run.period_end)
    rules = book.need("epf")
    lines = []
    for p in _slips(db, run):
        arr = sum((d(a["amount"]) for a in p.arrears or [] if a.get("code") in ("BASIC", "DA")), ZERO)
        uan = (p.snapshot or {}).get("uan")
        if arr <= 0 or not uan:
            continue
        r = calc.pf(arr, {**rules, "wage_ceiling": 10 ** 9, "eps_wage_ceiling": rules["eps_wage_ceiling"]},
                    employee_basis="actual", employer_basis="actual")
        lines.append(ECR_SEP.join(str(x) for x in [uan, ((p.snapshot or {}).get("name") or "").upper(), int(arr), int(r.eps_wage),
                                                    int(r.edli_wage), int(r.employee), int(r.employer_eps), int(r.employer_epf)]))
    if not lines:
        raise BadRequest("No PF arrears in this run.")
    return f"ECR-arrears-{_period_label(run)}.txt", TXT, ("\n".join(lines) + "\n").encode("utf-8")


# ── ESI monthly contribution file ──────────────────────────────────

def esi_file(db: Session, company_id: int, run: PayRun, as_csv: bool = False) -> tuple[str, str, bytes]:
    header = ["IP Number", "IP Name", "No of Days for which wages paid/payable during the month", "Total Monthly Wages",
              "Reason Code for Zero workings days", "Last Working Day"]
    rows = []
    for p in _slips(db, run):
        esi = (p.statutory or {}).get("esi") or {}
        if not esi.get("covered"):
            continue
        s = p.snapshot or {}
        days = int(rnd(p.payable_days))
        exit_d = s.get("exit_date")
        reason = "" if days else ("2" if exit_d else "1")
        lwd = date.fromisoformat(exit_d).strftime("%d/%m/%Y") if exit_d and exit_d <= run.period_end.isoformat() else ""
        rows.append([(s.get("esi_number") or "").strip(), s.get("name"), days, int(rnd(esi.get("wage"))), reason or 0, lwd])
    if not rows:
        raise BadRequest("No one is covered by ESI in this run.")
    if as_csv:
        return f"ESI-{_period_label(run)}.csv", CSV, _csv_bytes(header, rows)
    return f"ESI-{_period_label(run)}.xlsx", XLSX, _xlsx([("ESI", header, rows)])


# ── Professional Tax and LWF returns ───────────────────────────────

def pt_return(db: Session, company_id: int, run: PayRun) -> tuple[str, str, bytes]:
    book = statutory.RuleBook(db, company_id, run.period_end)
    by_state = defaultdict(list)
    for p in _slips(db, run):
        pt = (p.statutory or {}).get("pt") or {}
        if pt.get("state"):
            by_state[pt["state"]].append((p, d(pt.get("gross")), d(pt.get("amount"))))
    sheets, summary = [], []
    for st, items in sorted(by_state.items()):
        rules = book.get("pt", st) or {}
        slab_rows = defaultdict(lambda: [0, ZERO])
        detail = []
        for p, gross, amount in items:
            s = p.snapshot or {}
            slab = next((x for x in rules.get("slabs") or []
                         if gross >= d(x.get("min")) and (x.get("max") is None or gross <= d(x["max"]))
                         and (not x.get("gender") or x.get("gender") == (s.get("gender") or "male"))), None)
            label = (f"{calc.inr(slab['min'])} – {'above' if slab.get('max') is None else calc.inr(slab['max'])}"
                     f"{' (' + slab['gender'] + ')' if slab and slab.get('gender') else ''}") if slab else "—"
            slab_rows[label][0] += 1
            slab_rows[label][1] += amount
            detail.append([s.get("employee_code"), s.get("name"), money(gross), money(amount), label])
        sheets.append((f"PT {st}", ["Employee ID", "Name", "Gross", "PT", "Slab"], detail))
        for label, (n, amt) in slab_rows.items():
            summary.append([st, label, n, money(amt)])
    if not summary:
        raise BadRequest("No Professional Tax in this run.")
    sheets.insert(0, ("Summary", ["State", "Slab", "Employees", "Amount"], summary))
    return f"PT-return-{_period_label(run)}.xlsx", XLSX, _xlsx(sheets)


def lwf_return(db: Session, company_id: int, run: PayRun) -> tuple[str, str, bytes]:
    rows, summary = [], defaultdict(lambda: [0, ZERO, ZERO])
    for p in _slips(db, run):
        lw = (p.statutory or {}).get("lwf") or {}
        ee, er = d(lw.get("employee")), d(lw.get("employer"))
        if ee == 0 and er == 0:
            continue
        s = p.snapshot or {}
        rows.append([lw.get("state"), s.get("employee_code"), s.get("name"), money(ee), money(er), money(ee + er)])
        x = summary[lw.get("state")]
        x[0] += 1
        x[1] += ee
        x[2] += er
    if not rows:
        raise BadRequest("No Labour Welfare Fund in this run (LWF is collected only in certain months).")
    return f"LWF-return-{_period_label(run)}.xlsx", XLSX, _xlsx([
        ("Summary", ["State", "Employees", "Employee share", "Employer share", "Total"],
         [[k, v[0], money(v[1]), money(v[2]), money(v[1] + v[2])] for k, v in sorted(summary.items())]),
        ("Detail", ["State", "Employee ID", "Name", "Employee", "Employer", "Total"], rows)])


# ── Accounting ──────────────────────────────────────────────────────

LIABILITY = {"PF_EE": "pf_payable", "VPF": "pf_payable", "PF_ER": "pf_payable", "EPS_ER": "pf_payable", "EDLI": "pf_payable",
             "PF_ADMIN": "pf_payable", "ESI_EE": "esi_payable", "ESI_ER": "esi_payable", "PT": "pt_payable",
             "LWF_EE": "lwf_payable", "LWF_ER": "lwf_payable", "TDS": "tds_payable", "LOAN": "loan_recovery",
             "ADVANCE": "loan_recovery"}


def journal(db: Session, company_id: int, run: PayRun) -> list[dict]:
    """Balanced entries grouped by cost centre: debit salary costs (earnings and employer
    contributions) by GL code, credit statutory liabilities, recoveries and net pay."""
    from modules.payroll.structures import component_map
    settings = svc.get_settings(db, company_id)
    gl = settings.get("gl") or {}
    comps = component_map(db, company_id)
    entries = defaultdict(lambda: [ZERO, ZERO])  # (gl, name, cc) -> [debit, credit]
    for p in _slips(db, run):
        cc = ((p.snapshot or {}).get("cost_centre") or {}).get("code") or ""
        for ln in p.lines or []:
            a = d(ln["amount"])
            c = comps.get(ln["code"])
            if ln["kind"] in ("earning", "reimbursement"):
                code = (c.gl_code if c else None) or gl.get("employer_cost_default", "5101")
                entries[(code, ln["name"], cc)][0] += a
            elif ln["kind"] == "employer":
                code = (c.gl_code if c else None) or gl.get("employer_cost_default", "5101")
                entries[(code, ln["name"], cc)][0] += a
                if ln["code"] == "GRATUITY":
                    entries[(gl.get("gratuity_payable", "2120"), "Gratuity provision", "")][1] += a
                else:
                    key = LIABILITY.get(ln["code"], "other_deductions")
                    entries[(gl.get(key, "2199"), key.replace("_", " ").capitalize(), "")][1] += a
            elif ln["kind"] == "deduction":
                key = LIABILITY.get(ln["code"], "other_deductions")
                entries[(gl.get(key, "2199"), key.replace("_", " ").capitalize(), "")][1] += a
        entries[(gl.get("salary_payable", "2101"), "Salary payable", "")][1] += d(p.net)
    out = [{"gl_code": k[0], "ledger": k[1], "cost_centre": k[2], "debit": money(v[0]), "credit": money(v[1])}
           for k, v in sorted(entries.items(), key=lambda x: (x[1][0] == 0, x[0]))]
    return out


def journal_csv(db: Session, company_id: int, run: PayRun) -> tuple[str, str, bytes]:
    rows = journal(db, company_id, run)
    date_s = (run.period_end).strftime("%d-%m-%Y")
    body = [[date_s, run.number or run.title, r["gl_code"], r["ledger"], r["cost_centre"], r["debit"], r["credit"],
             f"Payroll {run.period_start:%b %Y}"] for r in rows]
    return f"journal-{_period_label(run)}.csv", CSV, _csv_bytes(
        ["Date", "Voucher", "GL code", "Ledger", "Cost centre", "Debit", "Credit", "Narration"], body)


def tally_xml(db: Session, company_id: int, run: PayRun) -> tuple[str, str, bytes]:
    """A Tally Prime import (Journal vouchers, one per cost centre): debits are negative
    amounts with ISDEEMEDPOSITIVE Yes, credits positive with No."""
    rows = journal(db, company_id, run)
    company = svc._company(db, company_id)
    vdate = run.period_end.strftime("%Y%m%d")
    vouchers = []
    entries = []
    for r in rows:
        dr, cr = d(r["debit"]), d(r["credit"])
        amt = -dr if dr else cr
        cc_xml = (f"<CATEGORYALLOCATIONS.LIST><CATEGORY>Primary Cost Category</CATEGORY><COSTCENTREALLOCATIONS.LIST>"
                  f"<NAME>{escape(r['cost_centre'])}</NAME><AMOUNT>{money(amt)}</AMOUNT></COSTCENTREALLOCATIONS.LIST>"
                  f"</CATEGORYALLOCATIONS.LIST>") if r["cost_centre"] and dr else ""
        entries.append(f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{escape(r['ledger'])}</LEDGERNAME>"
                       f"<ISDEEMEDPOSITIVE>{'Yes' if dr else 'No'}</ISDEEMEDPOSITIVE><AMOUNT>{money(amt)}</AMOUNT>"
                       f"{cc_xml}</ALLLEDGERENTRIES.LIST>")
    vouchers.append(f"<TALLYMESSAGE xmlns:UDF=\"TallyUDF\"><VOUCHER VCHTYPE=\"Journal\" ACTION=\"Create\">"
                    f"<DATE>{vdate}</DATE><VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>"
                    f"<VOUCHERNUMBER>{escape(run.number or '')}</VOUCHERNUMBER>"
                    f"<NARRATION>{escape('Payroll ' + run.period_start.strftime('%B %Y'))}</NARRATION>"
                    f"{''.join(entries)}</VOUCHER></TALLYMESSAGE>")
    xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>"
           "<BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME><STATICVARIABLES>"
           f"<SVCURRENTCOMPANY>{escape(company.company_name)}</SVCURRENTCOMPANY></STATICVARIABLES></REQUESTDESC>"
           f"<REQUESTDATA>{''.join(vouchers)}</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>")
    return f"tally-payroll-{_period_label(run)}.xml", XML, xml.encode("utf-8")


# ── Registers ───────────────────────────────────────────────────────

def wage_register(db: Session, company_id: int, run: PayRun) -> tuple[str, str, bytes]:
    from modules.payroll.runs import register_rows
    header, rows = register_rows(db, company_id, run)
    header = header[:4] + ["Rate of wages (month)"] + header[4:] + ["Date of payment"]
    slips = {p.id: p for p in db.query(Payslip).filter(Payslip.pay_run_id == run.id).all()}
    ordered = sorted(slips.values(), key=lambda p: p.id)
    out = []
    for p, r in zip(ordered, rows):
        out.append(r[:4] + [(p.snapshot or {}).get("full_month_wages")] + r[4:] +
                   [run.pay_date.isoformat() if run.pay_date else ""])
    return f"wage-register-{_period_label(run)}.xlsx", XLSX, _xlsx([("Register of wages", header, out)])


def bonus_register(db: Session, company_id: int, fy: str) -> tuple[str, str, bytes]:
    a, b = calc.fy_bounds(fy)
    settings = svc.get_settings(db, company_id)
    book = statutory.RuleBook(db, company_id, b)
    rules = book.need("bonus")
    slips = (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
             .filter(Payslip.company_id == company_id, Payslip.period_start >= a, Payslip.period_start <= b,
                     PayRun.status.in_(("approved", "locked", "paid")), Payslip.status.in_(("calculated", "held"))).all())
    per = defaultdict(list)
    paid = defaultdict(lambda: ZERO)
    names = {}
    for p in slips:
        names[p.employee_id] = p.snapshot or {}
        if p.period_start.month and d(p.days_in_period):
            wages = d((p.statutory or {}).get("wages"))
            if wages > 0:
                per[p.employee_id].append({"wage": wages, "days_worked": d(p.payable_days)})
        paid[p.employee_id] += _amount(p, "BONUS")
    rows = []
    for eid, months in per.items():
        res = calc.bonus(months, rules, rate=settings.get("bonus_rate", 8.33))
        s = names[eid]
        rows.append([s.get("employee_code"), s.get("name"), s.get("designation"), len(months),
                     money(sum((m["days_worked"] for m in months), ZERO)), res["base"], res["rate"],
                     "Yes" if res["eligible"] else "No", res["amount"], money(paid[eid]),
                     money(max(d(res["amount"]) - paid[eid], ZERO))])
    header = ["Employee ID", "Name", "Designation", "Months", "Days worked", "Bonus base", "Rate %", "Eligible",
              "Bonus due", "Paid", "Balance"]
    return f"bonus-register-{fy}.xlsx", XLSX, _xlsx([("Bonus register", header, rows)])


def gratuity_provision(db: Session, company_id: int, on: date) -> tuple[str, str, bytes]:
    book = statutory.RuleBook(db, company_id, on)
    rules = book.need("gratuity")
    Employee = svc.employee_model()
    rows = []
    total = ZERO
    for e in db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True)).order_by(Employee.first_name).all():
        s = svc.current_salary(db, company_id, e.id, on)
        if not s or not e.date_of_joining:
            continue
        comps = {c["code"]: d(c["monthly"]) for c in s.components or []}
        wages = comps.get("BASIC", ZERO) + comps.get("DA", ZERO)
        y, m, _ = calc.service_length(e.date_of_joining, on)
        accrued = rnd(wages * d(rules["days_per_year"]) / d(rules["divisor"]) * (d(y) + d(m) / 12))
        accrued = min(accrued, d(rules.get("max_amount") or accrued))
        g = calc.gratuity(wages, e.date_of_joining, on, rules, fixed_term=svc.is_fixed_term(db, e))
        total += accrued
        rows.append([e.employee_code, svc.name_of(e), e.date_of_joining.isoformat(), f"{y}y {m}m", money(wages), money(accrued),
                     "Yes" if g["eligible"] else "Not yet", money(calc.gratuity_provision(wages, rules))])
    rows.append(["", "Total", "", "", "", money(total), "", ""])
    header = ["Employee ID", "Name", "Joined", "Service", "Wages (Basic+DA)", "Liability to date", "Eligible now",
              "Monthly provision"]
    return f"gratuity-provision-{on.isoformat()}.xlsx", XLSX, _xlsx([("Gratuity provision", header, rows)])


# ── Quarterly TDS return (24Q / its successor) ─────────────────────

QUARTER_MONTHS = {1: (4, 5, 6), 2: (7, 8, 9), 3: (10, 11, 12), 4: (1, 2, 3)}


def quarter_slips(db: Session, company_id: int, fy: str, quarter: int) -> list[Payslip]:
    y = int(fy[:4])
    months = QUARTER_MONTHS[quarter]
    starts = [date(y + (1 if m <= 3 else 0), m, 1) for m in months]
    return (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
            .filter(Payslip.company_id == company_id, Payslip.fy == fy, Payslip.period_start >= starts[0],
                    Payslip.period_start <= calc.month_end(starts[-1]), PayRun.status.in_(("locked", "paid")),
                    Payslip.status.in_(("calculated", "held"))).order_by(Payslip.period_start, Payslip.id).all())


def tds_return(db: Session, company_id: int, fy: str, quarter: int, *, fmt: str = "txt") -> tuple[str, str, bytes]:
    """Data for the quarterly return of TDS on salary, laid out like the NSDL/Protean text
    file (FH file header, BH batch header, CD challan, DD deductee and, in Q4, SD salary
    detail records; fields separated by '^') so it can be checked with the File Validation
    Utility. An Excel copy with the same data is available for review."""
    if quarter not in QUARTER_MONTHS:
        raise BadRequest("Choose a quarter from 1 to 4.")
    from modules.payroll.models import StatutoryChallan
    company = svc._company(db, company_id)
    ent = adapters.legal_entity(db, company, None)
    forms = statutory.rule(db, company_id, "forms", None, calc.fy_bounds(fy)[0]) or {}
    slips = quarter_slips(db, company_id, fy, quarter)
    if not slips:
        raise BadRequest("No locked pay runs in this quarter.")
    challans = {c.period: c for c in db.query(StatutoryChallan).filter(StatutoryChallan.company_id == company_id,
                                                                       StatutoryChallan.kind == "tds").all()}
    by_month = defaultdict(list)
    for p in slips:
        by_month[p.period_start.strftime("%Y-%m")].append(p)
    ay = f"{int(fy[:4]) + 1}{str(int(fy[:4]) + 2)[-2:]}"
    fy_code = f"{fy[:4]}{fy[-2:]}"
    lines = []
    lines.append("^".join(["1", "FH", "SL1", "R", date.today().strftime("%d%m%Y"), "1", "D", (ent["tan"] or ""), "1", "CMPSLY"] + [""] * 4))
    total_tds = sum((_amount(p, "TDS") for p in slips), ZERO)
    lines.append("^".join(["2", "BH", "1", str(len(by_month)), "24Q", "", "", "", "", "", "", (ent["tan"] or ""), "",
                           (ent["pan"] or ""), ay, fy_code, f"Q{quarter}", (ent["legal_name"] or "")[:75], "", "",
                           (ent["address"] or "")[:25], "", "", "", "", "", "", "", "", "", "", "", "",
                           money(total_tds), "", "", "", "", "", "N", "", "", "", "", "", ""]))
    n = 3
    xl_challans, xl_deductees = [], []
    for ci, (period, items) in enumerate(sorted(by_month.items()), start=1):
        c = challans.get(period)
        tds = sum((_amount(p, "TDS") for p in items), ZERO)
        deductees = [p for p in items if _amount(p, "TDS") > 0 or quarter == 4]
        lines.append("^".join([str(n), "CD", "1", str(ci), str(len(deductees)), "N", "", "", "", "",
                               (c.reference if c else "") or "", "", "", "", "", "",
                               (c.paid_on.strftime("%d%m%Y") if c and c.paid_on else ""), "", "", "", money(tds),
                               "0.00", "0.00", "0.00", "0.00", money(tds), "", money(tds), "", "0.00", "", "", "", "", "", "", "", ""]))
        n += 1
        xl_challans.append([period, c.reference if c else "", c.paid_on.isoformat() if c and c.paid_on else "", money(tds), len(deductees)])
        for di, p in enumerate(deductees, start=1):
            s = p.snapshot or {}
            pan = (s.get("pan") or "PANNOTAVBL").upper()
            gross = sum((d(ln["amount"]) for ln in p.lines or [] if ln["kind"] in ("earning",)), ZERO)
            t = _amount(p, "TDS")
            lines.append("^".join([str(n), "DD", "1", str(ci), str(di), "O", "", pan, "", "", (s.get("name") or "")[:75],
                                   money(t), "0.00", "0.00", money(t), "", money(t), "", money(gross),
                                   p.period_end.strftime("%d%m%Y"), p.period_end.strftime("%d%m%Y"), "", "", "", "", "",
                                   "C" if pan == "PANNOTAVBL" else "", "", "", "", "", "", "", "", ""]))
            n += 1
            xl_deductees.append([period, s.get("employee_code"), s.get("name"), pan, money(gross), money(t),
                                 p.period_end.isoformat()])
    xl_salary = []
    if quarter == 4:
        from modules.payroll.models import Payslip as P
        yearly = defaultdict(list)
        for p in db.query(P).join(PayRun, PayRun.id == P.pay_run_id).filter(
                P.company_id == company_id, P.fy == fy, PayRun.status.in_(("locked", "paid")),
                P.status.in_(("calculated", "held"))).all():
            yearly[p.employee_id].append(p)
        for si, (eid, ps) in enumerate(sorted(yearly.items()), start=1):
            last = max(ps, key=lambda x: x.period_start)
            ws = (last.tax or {}).get("worksheet") or {}
            s = last.snapshot or {}
            tds_year = sum((_amount(p, "TDS") for p in ps), ZERO)
            gross = sum((d(p.gross) for p in ps), ZERO)
            lines.append("^".join([str(n), "SD", "1", str(si), "A", (s.get("pan") or "PANNOTAVBL").upper(), "",
                                   (s.get("name") or "")[:75], "W" if (s.get("gender") == "female") else "G",
                                   min(ps, key=lambda x: x.period_start).period_start.strftime("%d%m%Y"),
                                   max(ps, key=lambda x: x.period_end).period_end.strftime("%d%m%Y"),
                                   money(gross), ws.get("taxable_income", "0.00"), ((ws.get("tax") or {}).get("total") or "0.00"),
                                   money(tds_year), "Y" if (last.tax or {}).get("month", {}).get("regime") == "new" else "N"]))
            n += 1
            xl_salary.append([s.get("employee_code"), s.get("name"), s.get("pan"), (last.tax or {}).get("month", {}).get("regime"),
                              money(gross), ws.get("taxable_income"), (ws.get("tax") or {}).get("total"), money(tds_year)])
    form = forms.get("quarterly_return", "Form 24Q")
    if fmt == "xlsx":
        sheets = [("Challans", ["Month", "Challan / BIN", "Deposited on", "TDS", "Deductees"], xl_challans),
                  ("Deductees", ["Month", "Employee ID", "Name", "PAN", "Amount paid", "TDS", "Date"], xl_deductees)]
        if quarter == 4:
            sheets.append(("Salary details", ["Employee ID", "Name", "PAN", "Regime", "Gross", "Taxable income", "Tax",
                                              "TDS for the year"], xl_salary))
        return f"{form.split(' (')[0].replace(' ', '')}-{fy}-Q{quarter}.xlsx", XLSX, _xlsx(sheets)
    return f"{form.split(' (')[0].replace(' ', '')}-{fy}-Q{quarter}.txt", TXT, ("\n".join(lines) + "\n").encode("utf-8")
