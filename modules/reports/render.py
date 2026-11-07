"""Turn a report result into a file: Excel, CSV or PDF, optionally locked with a password.

Excel files are encrypted with the standard Office password (opens in Excel, LibreOffice
and Numbers after typing the password); PDFs use AES-256. A password-protected CSV is
delivered as a protected Excel file, because a plain CSV cannot carry a password.
"""
from __future__ import annotations

import io
import re
import secrets
import string
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from core import csv_safe

CONTENT_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
    "pdf": "application/pdf",
}
FORMATS = {"xlsx": "Excel", "pdf": "PDF", "csv": "CSV"}


def new_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:80] or "report"


def _option_label(col: dict, v):
    for o in col.get("options") or []:
        if o["value"] == v:
            return o["label"]
    return v


def display(col: dict, v, zone: ZoneInfo, *, for_sheet: bool = False):
    """A value as people read it (for CSV/PDF), or a typed value for Excel."""
    if v is None:
        return "" if not for_sheet else None
    t = col.get("type")
    if t == "bool":
        return "Yes" if v else "No"
    if t == "enum" or col.get("options"):
        return _option_label(col, v)
    if isinstance(v, datetime):
        local = v.astimezone(zone) if v.tzinfo else v
        return local.replace(tzinfo=None) if for_sheet else local.strftime("%d %b %Y %H:%M")
    if isinstance(v, date):
        if col.get("grain") == "month":
            return v.strftime("%b %Y")
        if col.get("grain") == "year":
            return str(v.year)
        if col.get("grain") == "quarter":
            return f"Q{(v.month - 1) // 3 + 1} {v.year}"
        return v if for_sheet else v.strftime("%d %b %Y")
    if isinstance(v, Decimal) or isinstance(v, float):
        if for_sheet:
            return float(v)
        if t == "money":
            return f"{Decimal(str(v)):,.2f}"
        q = Decimal(str(v)).normalize()
        return f"{q:f}" if q == q.to_integral() else f"{Decimal(str(v)).quantize(Decimal('0.01'))}"
    if isinstance(v, (dict, list)):
        return str(v)
    return v


def _visible_columns(result: dict) -> list[dict]:
    return [c for c in result["columns"] if not c["key"].startswith("_")]


def to_csv(result: dict, zone: ZoneInfo) -> bytes:
    cols = _visible_columns(result)
    buf = io.StringIO()
    w = csv_safe.writer(buf)
    w.writerow([c["label"] for c in cols])
    for r in result["rows"]:
        w.writerow([display(c, r.get(c["key"]), zone) for c in cols])
    if result.get("totals"):
        w.writerow([("Total" if i == 0 else display(c, result["totals"].get(c["key"]), zone)
                     if c["key"] in result["totals"] else "") for i, c in enumerate(cols)])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_xlsx(result: dict, title: str, meta: list[str], zone: ZoneInfo) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    cols = _visible_columns(result)
    wb = Workbook()
    ws = wb.active
    ws.title = safe_name(title)[:31] or "Report"
    ws.append([title])
    ws["A1"].font = Font(size=13)
    for line in meta:
        ws.append([line])
        ws.cell(row=ws.max_row, column=1).font = Font(size=9, color="666666")
    ws.append([])
    ws.append([c["label"] for c in cols])
    header_row = ws.max_row
    fill = PatternFill("solid", fgColor="EEF0FB")
    for i in range(1, len(cols) + 1):
        cell = ws.cell(row=header_row, column=i)
        cell.fill = fill
        cell.font = Font(color="2F3A8F")
        cell.alignment = Alignment(vertical="center")
    widths = [len(str(c["label"])) for c in cols]
    for r in result["rows"]:
        values = []
        for i, c in enumerate(cols):
            v = display(c, r.get(c["key"]), zone, for_sheet=True)
            if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
                v = "'" + v
            values.append(v)
            widths[i] = min(60, max(widths[i], len(str(v)) if v is not None else 0))
        ws.append(values)
        row = ws.max_row
        for i, c in enumerate(cols, start=1):
            cell = ws.cell(row=row, column=i)
            if c.get("type") == "date" and not c.get("grain"):
                cell.number_format = "dd mmm yyyy"
            elif c.get("type") == "datetime":
                cell.number_format = "dd mmm yyyy hh:mm"
            elif c.get("type") == "money":
                cell.number_format = "#,##0.00"
    if result.get("totals"):
        ws.append([("Total" if i == 0 else (display(c, result["totals"].get(c["key"]), zone, for_sheet=True)
                                             if c["key"] in result["totals"] else None)) for i, c in enumerate(cols)])
        for i in range(1, len(cols) + 1):
            ws.cell(row=ws.max_row, column=i).font = Font(color="2F3A8F")
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(9, w + 2)
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(result: dict, title: str, meta: list[str], zone: ZoneInfo) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle
    cols = _visible_columns(result)
    page = landscape(A4) if len(cols) > 5 else A4
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=title)
    font_size = 8 if len(cols) <= 10 else (6.5 if len(cols) <= 20 else 5)
    cell = ParagraphStyle("c", fontName="Helvetica", fontSize=font_size, leading=font_size + 2)
    head = ParagraphStyle("h", parent=cell, textColor=colors.HexColor("#2F3A8F"))
    story = [Paragraph(title, ParagraphStyle("t", fontName="Helvetica", fontSize=14, leading=18)), Spacer(1, 2 * mm)]
    for line in meta:
        story.append(Paragraph(line, ParagraphStyle("m", fontName="Helvetica", fontSize=8, leading=10,
                                                    textColor=colors.HexColor("#666666"))))
    story.append(Spacer(1, 4 * mm))

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    data = [[Paragraph(esc(c["label"]), head) for c in cols]]
    for r in result["rows"][:20000]:
        data.append([Paragraph(esc(display(c, r.get(c["key"]), zone)), cell) for c in cols])
    if result.get("totals"):
        data.append([Paragraph(esc("Total" if i == 0 else (display(c, result["totals"].get(c["key"]), zone)
                                                            if c["key"] in result["totals"] else "")), head)
                     for i, c in enumerate(cols)])
    if not result["rows"]:
        data.append([Paragraph("No rows", cell)] + [""] * (len(cols) - 1))
    width = page[0] - 24 * mm
    t = LongTable(data, colWidths=[width / max(1, len(cols))] * len(cols), repeatRows=1)
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#2F3A8F")),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#DDDDDD")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(t)
    doc.build(story)
    return buf.getvalue()


def protect(content: bytes, fmt: str, password: str) -> bytes:
    if fmt == "pdf":
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(content))
        w = PdfWriter()
        for p in reader.pages:
            w.add_page(p)
        w.encrypt(user_password=password, owner_password=None, algorithm="AES-256")
        out = io.BytesIO()
        w.write(out)
        return out.getvalue()
    from msoffcrypto.format.ooxml import OOXMLFile
    f = OOXMLFile(io.BytesIO(content))
    out = io.BytesIO()
    f.encrypt(password, out)
    return out.getvalue()


def render(result: dict, fmt: str, title: str, meta: list[str], tz: str, password: str | None = None
           ) -> tuple[bytes, str, str]:
    """(content, content type, extension)."""
    zone = ZoneInfo(tz)
    if fmt not in FORMATS:
        from core.errors import BadRequest
        raise BadRequest("Choose Excel, PDF or CSV.")
    if password and fmt == "csv":
        fmt = "xlsx"
    if fmt == "xlsx":
        data = to_xlsx(result, title, meta, zone)
    elif fmt == "pdf":
        data = to_pdf(result, title, meta, zone)
    else:
        data = to_csv(result, zone)
    if password:
        data = protect(data, fmt, password)
    return data, CONTENT_TYPES[fmt], fmt
