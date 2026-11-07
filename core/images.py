"""Small image helpers: validate, square-crop, resize and encode as a data URL."""
import base64
import io

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def to_square_data_url(raw: bytes, size: int = 400, quality: int = 85) -> str:
    """Centre-crop to a square, resize, and return a JPEG data URL (~20-40 KB)."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img = ImageOps.fit(img, (size, size), Image.LANCZOS, centering=(0.5, 0.4))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
