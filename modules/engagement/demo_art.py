"""Placeholder pictures for the demo feed, drawn locally with Pillow (no external images).
Each function returns PNG bytes of a small flat illustration."""
from __future__ import annotations

import io
import math
import random

from PIL import Image, ImageDraw, ImageFilter

W, H = 1200, 800


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _gradient(top: str, bottom: str, w: int = W, h: int = H) -> Image.Image:
    a, b = _hex(top), _hex(bottom)
    img = Image.new("RGB", (w, h))
    px = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        px.line([(0, y), (w, y)], fill=tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    return img


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _glow(img: Image.Image, draw_fn, blur: int = 18) -> Image.Image:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(img.convert("RGBA"), layer)


def diwali(seed: int = 3) -> bytes:
    rnd = random.Random(seed)
    img = _gradient("#1e1b4b", "#4c1d95").convert("RGBA")
    # bokeh lights
    def bokeh(d):
        for _ in range(40):
            x, y, r = rnd.randint(0, W), rnd.randint(0, int(H * 0.7)), rnd.randint(10, 46)
            col = rnd.choice(["#f59e0b", "#f472b6", "#fde68a", "#fb923c"])
            d.ellipse([x - r, y - r, x + r, y + r], fill=(*_hex(col), rnd.randint(40, 110)))
    img = _glow(img, bokeh, 8)
    d = ImageDraw.Draw(img)
    # rangoli-ish ring
    cx, cy = W // 2, int(H * 0.78)
    for i, col in enumerate(["#f43f5e", "#f59e0b", "#22c55e", "#3b82f6", "#a855f7"]):
        r = 260 - i * 46
        d.ellipse([cx - r, cy - r * 0.32, cx + r, cy + r * 0.32], outline=_hex(col), width=10)
    # diyas
    for x in (260, 470, 730, 940):
        y = int(H * 0.72)
        def flame(dd, x=x, y=y):
            dd.ellipse([x - 60, y - 140, x + 60, y - 20], fill=(255, 190, 80, 180))
        img = _glow(img, flame, 26)
        d = ImageDraw.Draw(img)
        d.chord([x - 70, y - 40, x + 70, y + 50], 0, 180, fill=_hex("#b45309"))
        d.ellipse([x - 70, y - 14, x + 70, y + 14], fill=_hex("#92400e"))
        d.polygon([(x, y - 100), (x - 16, y - 34), (x + 16, y - 34)], fill=_hex("#fde68a"))
        d.ellipse([x - 16, y - 54, x + 16, y - 22], fill=_hex("#fbbf24"))
    return _png(img)


def offsite(seed: int = 5) -> bytes:
    img = _gradient("#fde68a", "#fb7185").convert("RGBA")
    img = _glow(img, lambda d: d.ellipse([760, 140, 1000, 380], fill=(255, 255, 230, 230)), 30)
    d = ImageDraw.Draw(img)
    d.ellipse([800, 180, 960, 340], fill=_hex("#fff7d6"))
    layers = [("#c084fc", 0.48, 0.0042, 90), ("#7c3aed", 0.6, 0.006, 70), ("#4c1d95", 0.74, 0.0085, 55), ("#1e1b4b", 0.88, 0.011, 40)]
    for col, base, freq, amp in layers:
        pts = [(0, H)]
        for x in range(0, W + 20, 20):
            y = H * base - amp * math.sin(x * freq + base * 9) - amp * 0.6 * math.sin(x * freq * 2.3 + 1.7)
            pts.append((x, y))
        pts.append((W, H))
        d.polygon(pts, fill=_hex(col))
    # tents
    for x, s in ((300, 1.0), (420, 0.8)):
        y = H * 0.86
        d.polygon([(x - 70 * s, y), (x, y - 90 * s), (x + 70 * s, y)], fill=_hex("#f97316"))
        d.polygon([(x - 12 * s, y), (x, y - 50 * s), (x + 12 * s, y)], fill=_hex("#7c2d12"))
    return _png(img)


def hackathon(seed: int = 7) -> bytes:
    rnd = random.Random(seed)
    img = _gradient("#0f172a", "#1e3a8a").convert("RGBA")
    d = ImageDraw.Draw(img)
    for x in range(0, W, 60):
        d.line([(x, 0), (x, H)], fill=(255, 255, 255, 18), width=1)
    for y in range(0, H, 60):
        d.line([(0, y), (W, y)], fill=(255, 255, 255, 18), width=1)
    def neon(dd):
        dd.rounded_rectangle([180, 200, 700, 560], 28, outline=(56, 189, 248, 255), width=10)
        dd.ellipse([780, 180, 1040, 440], outline=(244, 114, 182, 255), width=10)
        dd.polygon([(820, 640), (960, 480), (1100, 640)], outline=(250, 204, 21, 255), width=10)
    img = _glow(img, neon, 14)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([180, 200, 700, 560], 28, outline=_hex("#bae6fd"), width=4)
    d.ellipse([780, 180, 1040, 440], outline=_hex("#fbcfe8"), width=4)
    d.polygon([(820, 640), (960, 480), (1100, 640)], outline=_hex("#fef08a"), width=4)
    for i, w in enumerate([300, 220, 360, 180, 260]):
        d.rounded_rectangle([230, 250 + i * 56, 230 + w, 270 + i * 56], 8,
                            fill=_hex(["#38bdf8", "#a78bfa", "#34d399", "#f472b6", "#facc15"][i]))
    for _ in range(70):
        x, y = rnd.randint(0, W), rnd.randint(0, H)
        d.rectangle([x, y, x + 6, y + 6], fill=_hex(rnd.choice(["#38bdf8", "#f472b6", "#facc15", "#a78bfa"])))
    return _png(img)


def cricket(seed: int = 9) -> bytes:
    img = _gradient("#7dd3fc", "#bae6fd").convert("RGBA")
    d = ImageDraw.Draw(img)
    d.ellipse([-300, 380, W + 300, H + 700], fill=_hex("#16a34a"))
    d.ellipse([-120, 460, W + 120, H + 560], fill=_hex("#22c55e"))
    d.polygon([(520, 470), (680, 470), (760, H), (440, H)], fill=_hex("#d6b98c"))
    for x in (580, 600, 620):
        d.rectangle([x - 3, 410, x + 3, 480], fill=_hex("#fef3c7"))
    d.rectangle([572, 406, 628, 412], fill=_hex("#fef3c7"))
    d.ellipse([820, 300, 870, 350], fill=_hex("#dc2626"))
    d.arc([826, 306, 864, 344], 300, 60, fill=_hex("#fecaca"), width=3)
    for x, y in ((150, 120), (420, 90), (960, 140)):
        d.ellipse([x, y, x + 140, y + 50], fill=(255, 255, 255, 230))
        d.ellipse([x + 40, y - 26, x + 120, y + 40], fill=(255, 255, 255, 230))
    return _png(img)


def volunteering(seed: int = 11) -> bytes:
    rnd = random.Random(seed)
    img = _gradient("#ecfccb", "#a7f3d0").convert("RGBA")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 600, W, H], fill=_hex("#92400e"))
    d.rectangle([0, 590, W, 612], fill=_hex("#78350f"))
    for i, x in enumerate(range(140, W, 190)):
        h = 140 + (i % 3) * 60
        d.line([(x, 600), (x, 600 - h)], fill=_hex("#166534"), width=8)
        for k in range(4):
            y = 600 - h + k * 34
            side = -1 if k % 2 else 1
            d.ellipse([x + side * 8 - 40 if side > 0 else x - 80, y - 20, x + 80 if side > 0 else x - 8 + 40, y + 20],
                      fill=_hex(rnd.choice(["#22c55e", "#16a34a", "#4ade80"])))
    d.ellipse([980, 60, 1120, 200], fill=_hex("#fde047"))
    return _png(img)


def team_lunch(seed: int = 13) -> bytes:
    rnd = random.Random(seed)
    img = _gradient("#fff7ed", "#fed7aa").convert("RGBA")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([120, 220, 1080, 700], 80, fill=_hex("#c2410c"))
    d.rounded_rectangle([140, 240, 1060, 680], 70, fill=_hex("#ea580c"))
    for cx, cy in ((300, 360), (600, 330), (900, 360), (300, 560), (600, 590), (900, 560)):
        d.ellipse([cx - 95, cy - 70, cx + 95, cy + 70], fill=_hex("#fffbeb"))
        d.ellipse([cx - 70, cy - 50, cx + 70, cy + 50], fill=_hex(rnd.choice(["#facc15", "#84cc16", "#f97316", "#ef4444"])))
        for _ in range(6):
            x, y = cx + rnd.randint(-45, 45), cy + rnd.randint(-30, 30)
            d.ellipse([x - 8, y - 8, x + 8, y + 8], fill=_hex(rnd.choice(["#fef3c7", "#166534", "#7c2d12"])))
    return _png(img)


PICTURES = {"diwali": diwali, "offsite": offsite, "hackathon": hackathon, "cricket": cricket,
            "volunteering": volunteering, "team_lunch": team_lunch}
