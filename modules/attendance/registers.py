"""Statutory registers of attendance and overtime from the attendance records, as Excel or
PDF, in a generic layout and in the layouts the major states' shops and establishments
rules ask for (Karnataka, Maharashtra, Tamil Nadu, Delhi, Telangana).

Each state layout carries the Act and Rules it is kept under, the establishment details
from the `registers` setting, and the columns that state's register has. Form numbers are
printed from `registers.form_labels` (editable in settings) because states renumber their
forms from time to time; check the layout against the latest state notification.
"""
from __future__ import annotations

import html
import io
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.attendance import settings_store
from modules.attendance.models import AttendanceDay

CODES = {"present": "P", "absent": "A", "half_day": "HD", "on_leave": "L", "holiday": "H", "weekly_off": "WO",
         "wfh": "P", "on_duty": "OD", "missing_punch": "A", "not_yet": "", "upcoming": ""}

FORMATS: dict[str, dict] = {
    "generic": {
        "label": "Generic (all states)", "state": None,
        "act": "Register kept under the Code on Wages, 2019 and the OSH Code, 2020",
        "attendance": "Register of attendance (muster roll)", "overtime": "Register of overtime",
        "extra": [],
    },
    "KA": {
        "label": "Karnataka", "state": "Karnataka",
        "act": "Karnataka Shops and Commercial Establishments Act, 1961 and Rules, 1963",
        "attendance": "Combined muster roll (attendance and overtime)", "overtime": "Register of overtime",
        "extra": ["father_name", "joined", "ot_hours"],
    },
    "MH": {
        "label": "Maharashtra", "state": "Maharashtra",
        "act": "Maharashtra Shops and Establishments (Regulation of Employment and Conditions of Service) Act, 2017 "
               "and Rules, 2018",
        "attendance": "Muster roll", "overtime": "Register of overtime",
        "extra": ["gender", "hours", "ot_hours"],
    },
    "TN": {
        "label": "Tamil Nadu", "state": "Tamil Nadu",
        "act": "Tamil Nadu Shops and Establishments Act, 1947 and Rules, 1948",
        "attendance": "Register of employment (attendance)", "overtime": "Register of overtime",
        "extra": ["father_name", "leave_days", "worked"],
    },
    "DL": {
        "label": "Delhi", "state": "Delhi",
        "act": "Delhi Shops and Establishments Act, 1954 and Rules, 1954",
        "attendance": "Register of attendance and hours of work", "overtime": "Register of overtime",
        "extra": ["rest", "hours"],
    },
    "TS": {
        "label": "Telangana", "state": "Telangana",
        "act": "Telangana Shops and Establishments Act, 1988 and Rules, 1990",
        "attendance": "Register of attendance", "overtime": "Register of overtime",
        "extra": ["joined", "worked", "weekly_offs", "ot_hours"],
    },
}

EXTRA_LABELS = {"father_name": "Father's / husband's name", "joined": "Date of joining", "ot_hours": "Overtime hours",
                "gender": "Gender", "hours": "Hours worked", "leave_days": "Leave with wages", "worked": "Days worked",
                "rest": "Rest interval", "weekly_offs": "Weekly holidays"}


def formats() -> list[dict]:
    return [{"key": k, "label": v["label"], "act": v["act"]} for k, v in FORMATS.items()]


def _h(x) -> str:
    return html.escape("" if x is None else str(x))


def _hours(minutes: int) -> str:
    return f"{Decimal(minutes) / 60:.2f}"


def build(db: Session, company_id: int, fmt: str, kind: str, start: date, end: date, ids: list[int] | None) -> dict:
    """Rows of a register for the period (at most 31 days)."""
    from modules.attendance import service
    if fmt not in FORMATS:
        fmt = "generic"
    f = FORMATS[fmt]
    cfg = settings_store.get(db, company_id, "registers", end)
    emps = service.employees_query(db, company_id, ids, active_only=False).all()
    emp_ids = [e.id for e in emps]
    days = list(_range(start, end))
    rows = defaultdict(dict)
    for r in db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id,
                                            AttendanceDay.employee_id.in_(emp_ids or [-1]),
                                            AttendanceDay.date >= start, AttendanceDay.date <= end).all():
        rows[r.employee_id][r.date] = r
    shifts = service._shift_map(db, company_id)
    desig = _designations(db, company_id)
    genders = _genders(db, emps)
    title = f["attendance"] if kind == "attendance" else f["overtime"]
    form = (cfg.get("form_labels") or {}).get(f"{fmt}.{kind}") or ""
    out = {"format": fmt, "kind": kind, "title": title, "form": form, "act": f["act"], "state": f["state"],
           "start": start, "end": end, "days": days, "establishment": cfg, "rows": [], "columns": []}
    if kind == "attendance":
        extra = f["extra"]
        out["columns"] = ["No.", "Code", "Name", *[EXTRA_LABELS[x] for x in extra if x in ("father_name", "joined",
                                                                                        "gender")],
                          "Designation", *[f"{d.day}" for d in days], "Present", "Absent", "Leave", "Holidays",
                          "Payable", *[EXTRA_LABELS[x] for x in extra if x not in ("father_name", "joined", "gender")]]
        for n, e in enumerate(sorted(emps, key=lambda x: (x.employee_code or "", x.first_name or "")), 1):
            mine = rows.get(e.id, {})
            if not mine:
                continue
            marks = [CODES.get(mine[d].status, "") if d in mine else "" for d in days]
            vals = list(mine.values())
            present = sum(Decimal("1") if v.status in ("present", "wfh", "on_duty") else
                          Decimal("0.5") if v.status == "half_day" else Decimal("0") for v in vals)
            absent = sum(1 for v in vals if v.status in ("absent", "missing_punch"))
            leave = sum(1 for v in vals if v.status == "on_leave")
            hol = sum(1 for v in vals if v.status in ("holiday", "weekly_off"))
            pay = sum((v.payable_days - v.penalty_lop_days for v in vals), Decimal("0"))
            head = [n, e.employee_code or "", service.name_of(e)]
            for x in extra:
                if x == "father_name":
                    head.append(getattr(e, "father_name", None) or "")
                elif x == "joined":
                    j = getattr(e, "date_of_joining", None)
                    head.append(j.strftime("%d-%m-%Y") if j else "")
                elif x == "gender":
                    head.append(genders.get(e.id, ""))
            head.append(desig.get(e.designation_id, ""))
            tail = []
            for x in extra:
                if x == "ot_hours":
                    tail.append(_hours(sum(v.overtime_minutes for v in vals)))
                elif x == "hours":
                    tail.append(_hours(sum(v.worked_minutes for v in vals)))
                elif x == "leave_days":
                    tail.append(sum(1 for v in vals if v.status == "on_leave" and v.payable_days > 0))
                elif x == "worked":
                    tail.append(f"{present:g}")
                elif x == "rest":
                    sh = next((shifts.get(v.shift_id) for v in vals if v.shift_id in shifts), None)
                    tail.append(_rest(sh))
                elif x == "weekly_offs":
                    tail.append(sum(1 for v in vals if v.status == "weekly_off"))
            out["rows"].append([*head, *marks, f"{present:g}", absent, leave, hol, f"{pay:g}", *tail])
    else:
        out["columns"] = ["No.", "Code", "Name", "Designation", "Date", "Shift", "Hours worked", "Overtime hours",
                          "Rate", "Remarks"]
        n = 0
        for e in sorted(emps, key=lambda x: (x.employee_code or "", x.first_name or "")):
            for d in days:
                v = rows.get(e.id, {}).get(d)
                if v is None or not v.overtime_minutes:
                    continue
                n += 1
                sh = shifts.get(v.shift_id)
                remark = "Day off worked" if v.status in ("holiday", "weekly_off") else ""
                out["rows"].append([n, e.employee_code or "", service.name_of(e), desig.get(e.designation_id, ""),
                                    d.strftime("%d-%m-%Y"), sh.name if sh else "", _hours(v.worked_minutes),
                                    _hours(v.overtime_minutes), "2x ordinary rate", remark])
    return out


def _range(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _rest(sh) -> str:
    if sh is None or not sh.breaks:
        return ""
    return ", ".join(f"{b.get('start')}–{b.get('end')}" for b in sh.breaks if b.get("start"))


def _designations(db: Session, company_id: int) -> dict:
    try:
        from modules.designations.models import Designation
        return {d.id: d.designation_name for d in db.query(Designation).all()}
    except Exception:
        return {}


def _genders(db: Session, emps) -> dict:
    ids = {e.gender_id for e in emps if getattr(e, "gender_id", None)}
    if not ids:
        return {}
    from core.masters import MasterValue
    labels = {m.id: m.label for m in db.query(MasterValue).filter(MasterValue.id.in_(ids)).all()}
    return {e.id: labels.get(e.gender_id, "") for e in emps}


# ── Output ──────────────────────────────────────────────────────────

def _header_lines(data: dict) -> list[str]:
    est = data["establishment"]
    lines = [f"{data['title']}{(' – ' + data['form']) if data['form'] else ''}", data["act"],
             f"Name of the establishment: {est.get('establishment_name') or ''}",
             f"Registration number: {est.get('registration_no') or ''}",
             f"Address: {est.get('address') or ''}",
             f"Employer: {est.get('employer_name') or ''}",
             f"Period: {data['start']:%d-%m-%Y} to {data['end']:%d-%m-%Y}"]
    return lines


def to_xlsx(data: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance" if data["kind"] == "attendance" else "Overtime"
    for i, line in enumerate(_header_lines(data), 1):
        ws.cell(row=i, column=1, value=line).font = Font(bold=(i == 1), size=12 if i == 1 else 10)
    r0 = len(_header_lines(data)) + 2
    for c, col in enumerate(data["columns"], 1):
        cell = ws.cell(row=r0, column=c, value=col)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, row in enumerate(data["rows"], 1):
        for c, v in enumerate(row, 1):
            ws.cell(row=r0 + i, column=c, value=v)
    if data["kind"] == "attendance":
        ws.cell(row=r0 + len(data["rows"]) + 2, column=1,
                value="P present, A absent, HD half day, L leave, H holiday, WO weekly off, OD on duty")
    ws.cell(row=r0 + len(data["rows"]) + 4, column=1, value="Signature of the employer: ____________________")
    for col in ws.columns:
        letter = col[0].column_letter
        ws.column_dimensions[letter].width = 5 if len(str(col[r0 - 1].value or "")) <= 2 else 16
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(data: dict) -> bytes:
    from weasyprint import HTML
    head = "".join(f"<div class='{'t' if i == 0 else 'l'}'>{_h(x)}</div>" for i, x in enumerate(_header_lines(data)))
    cols = "".join(f"<th>{_h(c)}</th>" for c in data["columns"])
    body = "".join("<tr>" + "".join(f"<td>{_h(v)}</td>" for v in row) + "</tr>" for row in data["rows"]) \
        or f"<tr><td colspan='{len(data['columns'])}'>No entries for this period.</td></tr>"
    legend = ("<p class='l'>P present, A absent, HD half day, L leave, H holiday, WO weekly off, OD on duty</p>"
              if data["kind"] == "attendance" else "")
    size = "A3 landscape" if data["kind"] == "attendance" else "A4 landscape"
    doc = f"""<html><head><meta charset='utf-8'><style>
      @page {{ size: {size}; margin: 12mm; }}
      body {{ font-family: Helvetica, Arial, sans-serif; color: #111; font-size: 8pt; }}
      .t {{ font-size: 13pt; margin-bottom: 2mm; }} .l {{ font-size: 8.5pt; color: #333; }}
      table {{ border-collapse: collapse; width: 100%; margin-top: 4mm; }}
      th, td {{ border: 0.4pt solid #888; padding: 1.2mm 1mm; text-align: center; }}
      th {{ background: #f2f2f2; font-weight: normal; }}
      td:nth-child(3) {{ text-align: left; white-space: nowrap; }}
      .sign {{ margin-top: 10mm; font-size: 9pt; }}
    </style></head><body>{head}<table><thead><tr>{cols}</tr></thead><tbody>{body}</tbody></table>
    {legend}<div class='sign'>Signature of the employer: ____________________</div></body></html>"""
    return HTML(string=doc).write_pdf()
