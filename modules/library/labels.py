"""Printable copy labels: Code 128 barcode or QR code with the accession number, title and shelf.
A4 sheet, 3 columns x 8 rows (common 63.5 x 33.9 mm label stock)."""
import base64
import html
import io


def _barcode_svg(value: str) -> str:
    from reportlab.graphics import renderSVG
    from reportlab.graphics.barcode import createBarcodeDrawing
    d = createBarcodeDrawing("Code128", value=value, barHeight=28, barWidth=1.0, humanReadable=False, quiet=False)
    return renderSVG.drawToString(d)


def _qr_png(value: str) -> str:
    import qrcode
    img = qrcode.make(value, box_size=4, border=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def labels_pdf(copies, kind: str, institution: str) -> bytes:
    from modules.documents.pdf import render_pdf
    cells = []
    for c in copies:
        title = html.escape((c.book.title or "")[:60])
        shelf = html.escape(c.shelf or c.book.shelf or "")
        acc = html.escape(c.accession_no)
        if kind == "qr":
            code = f'<img class="qr" src="{_qr_png(c.accession_no)}"/>'
        else:
            svg = _barcode_svg(c.accession_no)
            svg = svg[svg.find("<svg"):]
            code = f'<img class="bc" src="data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}"/>'
        cells.append(f'<div class="label {kind}"><div class="inst">{html.escape(institution)}</div>{code}'
                     f'<div class="acc">{acc}</div><div class="title">{title}</div>'
                     f'<div class="shelf">{shelf}</div></div>')
    css = """
    @page { size: A4; margin: 12mm 7mm; }
    body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; margin: 0; }
    .sheet { display: flex; flex-wrap: wrap; }
    .label { width: 63.5mm; height: 33.9mm; box-sizing: border-box; padding: 2mm 3mm; text-align: center;
             overflow: hidden; border: 0.2mm dashed #ddd; page-break-inside: avoid; }
    .inst { font-size: 6.5pt; color: #666; text-transform: uppercase; letter-spacing: .04em; }
    .bc { height: 12mm; max-width: 56mm; margin-top: 1mm; }
    .qr { height: 17mm; float: left; margin-right: 2mm; }
    .label.qr { text-align: left; }
    .acc { font-size: 10pt; font-family: 'Courier New', monospace; margin-top: .5mm; }
    .title { font-size: 7pt; color: #222; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .shelf { font-size: 6.5pt; color: #666; }
    """
    doc = f'<html><head><meta charset="utf-8"><style>{css}</style></head><body><div class="sheet">{"".join(cells)}</div></body></html>'
    return render_pdf(doc)
