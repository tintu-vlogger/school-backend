"""Letters to PDF (WeasyPrint) and to an on-screen page preview.

A letter is a body (merged HTML) plus an optional header and footer design. The header
and footer repeat on every page in the PDF (CSS running elements) and sit at the top and
bottom of each page in the preview. In footers, {{page.number}} and {{page.total}} become
page numbers.
"""
from __future__ import annotations

import html as html_mod
import logging
import re

logger = logging.getLogger("hrms")

PAGE_SIZES_MM = {"A4": (210, 297), "Letter": (216, 279), "Legal": (216, 356), "A5": (148, 210)}
FONTS = {
    "Helvetica": "'Helvetica Neue', Helvetica, Arial, 'DejaVu Sans', sans-serif",
    "Georgia": "Georgia, 'Times New Roman', 'DejaVu Serif', serif",
    "Times": "'Times New Roman', Times, 'DejaVu Serif', serif",
    "Noto Sans": "'Noto Sans', 'DejaVu Sans', Arial, sans-serif",
}

# Tags and attributes allowed in template HTML (anything else is dropped before rendering)
_SCRIPT_RE = re.compile(r"<\s*(script|iframe|object|embed|link|meta|style)\b[^>]*>.*?<\s*/\s*\1\s*>|<\s*(script|iframe|object|embed|link|meta)\b[^>]*/?>",
                        re.I | re.S)
_ON_ATTR_RE = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_JS_URL_RE = re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.I)


def sanitize(html: str) -> str:
    """Remove scripts, frames, event handlers and javascript: links from template HTML."""
    html = _SCRIPT_RE.sub("", html or "")
    html = _ON_ATTR_RE.sub("", html)
    html = _JS_URL_RE.sub(r'\1=""', html)
    return html


def _page_tokens(html: str, pdf: bool) -> str:
    if pdf:
        html = html.replace("{{page.number}}", '<span class="pg-n"></span>').replace("{{page.total}}", '<span class="pg-t"></span>')
    else:
        html = html.replace("{{page.number}}", "1").replace("{{page.total}}", "1")
    return html


BASE_CSS = """
  body { margin: 0; color: #1f2430; line-height: 1.5; }
  p { margin: 0 0 0.7em; }
  h1, h2, h3 { font-weight: 600; margin: 0 0 0.5em; line-height: 1.25; }
  h1 { font-size: 1.6em; } h2 { font-size: 1.3em; } h3 { font-size: 1.1em; }
  table { border-collapse: collapse; width: 100%; margin: 0.4em 0 0.9em; }
  td, th { border: 0.6pt solid #c9ccd4; padding: 4pt 6pt; text-align: left; vertical-align: top; }
  th { background: #f3f4f7; font-weight: 600; }
  table.plain td, table.plain th { border: none; padding: 2pt 0; background: none; }
  img { max-width: 100%; }
  hr { border: none; border-top: 0.6pt solid #c9ccd4; margin: 0.8em 0; }
  .muted { color: #6b7080; }
  .right { text-align: right; } .center { text-align: center; }
  ul, ol { margin: 0 0 0.7em 1.2em; padding: 0; }
"""


def _layout(page_size: str, orientation: str) -> tuple[int, int]:
    w, h = PAGE_SIZES_MM.get(page_size, PAGE_SIZES_MM["A4"])
    return (h, w) if orientation == "landscape" else (w, h)


def pdf_html(body: str, *, header: str | None, footer: str | None, header_mm: int, footer_mm: int,
             page_size: str = "A4", orientation: str = "portrait", margins=(20, 20, 20, 20),
             font_family: str = "Helvetica", font_size: int = 11, title: str = "Letter") -> str:
    top, right, bottom, left = margins
    header_mm = header_mm if header else 0
    footer_mm = footer_mm if footer else 0
    font = FONTS.get(font_family, FONTS["Helvetica"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html_mod.escape(title)}</title><style>
  @page {{
    size: {page_size} {orientation};
    margin: {top + header_mm}mm {right}mm {bottom + footer_mm}mm {left}mm;
    @top-center {{ content: element(pageHeader); vertical-align: top; padding-top: {max(top - 8, 4)}mm; }}
    @bottom-center {{ content: element(pageFooter); vertical-align: bottom; padding-bottom: {max(bottom - 8, 4)}mm; }}
  }}
  #pageHeader {{ position: running(pageHeader); width: 100%; }}
  #pageFooter {{ position: running(pageFooter); width: 100%; }}
  .pg-n::after {{ content: counter(page); }}
  .pg-t::after {{ content: counter(pages); }}
  body {{ font-family: {font}; font-size: {font_size}pt; }}
  {BASE_CSS}
</style></head><body>
<div id="pageHeader">{_page_tokens(sanitize(header or ''), True)}</div>
<div id="pageFooter">{_page_tokens(sanitize(footer or ''), True)}</div>
<main>{sanitize(body)}</main>
</body></html>"""


def preview_html(body: str, *, header: str | None, footer: str | None, header_mm: int, footer_mm: int,
                 page_size: str = "A4", orientation: str = "portrait", margins=(20, 20, 20, 20),
                 font_family: str = "Helvetica", font_size: int = 11) -> str:
    """A self-contained page for an <iframe srcdoc>: one sheet with the header at the top
    and the footer at the bottom, sized in millimetres like the PDF."""
    top, right, bottom, left = margins
    w, h = _layout(page_size, orientation)
    font = FONTS.get(font_family, FONTS["Helvetica"])
    header_block = f'<div class="hd" style="height:{header_mm}mm">{_page_tokens(sanitize(header), False)}</div>' if header else ""
    footer_block = f'<div class="ft" style="height:{footer_mm}mm">{_page_tokens(sanitize(footer), False)}</div>' if footer else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
  html, body {{ margin: 0; background: #eceae6; }}
  .sheet {{ box-sizing: border-box; width: {w}mm; min-height: {h}mm; margin: 0 auto; background: #fff;
    padding: {max(top - 8, 4) if header else top}mm {right}mm {max(bottom - 8, 4) if footer else bottom}mm {left}mm;
    display: flex; flex-direction: column; font-family: {font}; font-size: {font_size}pt; }}
  .hd, .ft {{ overflow: hidden; flex: none; }}
  .hd {{ margin-bottom: 8mm; }} .ft {{ margin-top: 8mm; }}
  .bd {{ flex: 1; }}
  {BASE_CSS}
</style></head><body><div class="sheet">{header_block}<div class="bd">{sanitize(body)}</div>{footer_block}</div></body></html>"""


def render_pdf(document_html: str) -> bytes:
    """HTML to PDF bytes with WeasyPrint; a plain ReportLab fallback keeps letters working
    on machines without the Pango libraries."""
    try:
        from weasyprint import HTML  # noqa: WPS433
        return HTML(string=document_html, base_url=".").write_pdf()
    except (ImportError, OSError) as exc:  # pragma: no cover - only without Pango
        logger.warning("WeasyPrint unavailable (%s); using the plain PDF fallback", exc)
        return _fallback_pdf(document_html)


def _fallback_pdf(document_html: str) -> bytes:  # pragma: no cover
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    body = re.sub(r"(?s)<style.*?</style>", "", document_html)
    blocks = re.split(r"(?i)</p>|<br\s*/?>|</h\d>|</li>|</tr>|</div>", body)
    styles = getSampleStyleSheet()
    story = []
    for b in blocks:
        text = html_mod.unescape(re.sub(r"<[^>]+>", " ", b)).strip()
        if text:
            story.append(Paragraph(html_mod.escape(text), styles["BodyText"]))
            story.append(Spacer(1, 4))
    buf = BytesIO()
    SimpleDocTemplate(buf, pagesize=A4).build(story or [Paragraph(" ", styles["BodyText"])])
    return buf.getvalue()


def signature_page(title: str, rows: list[dict]) -> str:
    """HTML appended to a signed letter: who signed, when, how, from where."""
    items = []
    for r in rows:
        img = f'<img src="{r["image"]}" style="height:18mm;max-width:70mm">' if r.get("image") else \
            f'<span style="font-family:cursive;font-size:20pt">{html_mod.escape(r.get("typed") or r["name"])}</span>'
        items.append(f"""<tr><td style="width:45%">{img}<div style="margin-top:2mm">{html_mod.escape(r['name'])}</div>
          <div class="muted">{html_mod.escape(r.get('label') or '')}</div></td>
          <td><div>Signed on {html_mod.escape(r['signed_at'])}</div><div class="muted">{html_mod.escape(r['email'])}</div>
          <div class="muted">Verified with a one-time code sent to the email address · IP {html_mod.escape(r.get('ip') or '—')}</div></td></tr>""")
    return f"""<div style="page-break-before: always"></div>
<h2>Signature record</h2>
<p class="muted">{html_mod.escape(title)} was signed electronically. Each signer confirmed their identity with a
one-time code sent to their email address before signing (Information Technology Act, 2000, section 3A).</p>
<table>{''.join(items)}</table>"""
