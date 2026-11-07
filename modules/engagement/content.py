"""Content helpers: uploads with signed addresses, mentions and hashtags, safe HTML for
announcements, and link previews.

Mentions are written in post text as @[Name](uuid); hashtags as #word. The web app turns
both into links.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import ipaddress
import re
import socket
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from sqlalchemy.orm import Session

from core.config import get_settings
from core.errors import BadRequest
from modules.engagement.models import EngagementFile

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
FILE_TYPES = {
    "application/pdf", "text/plain", "text/csv",
    "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/zip",
}

# ── Files ───────────────────────────────────────────────────────────


def _sign(file_uuid: str) -> str:
    key = (get_settings().SECRET_KEY + ":engagement-files").encode()
    return hmac.new(key, str(file_uuid).encode(), hashlib.sha256).hexdigest()[:32]


def check_signature(file_uuid: str, sig: str) -> bool:
    return hmac.compare_digest(_sign(file_uuid), sig or "")


def file_url(file_uuid, thumb: bool = False) -> str:
    u = str(file_uuid)
    return f"/api/v1/public/engagement/files/{u}?s={_sign(u)}" + ("&thumb=1" if thumb else "")


def file_card(f: EngagementFile | dict) -> dict:
    d = f if isinstance(f, dict) else {
        "uuid": str(f.uuid), "kind": f.kind, "name": f.name, "mime": f.mime, "size": f.size,
        "width": f.width, "height": f.height,
    }
    return {**d, "url": file_url(d["uuid"]), "thumb_url": file_url(d["uuid"], thumb=d["kind"] == "image")}


def _image(raw: bytes, max_side: int, quality: int = 84) -> tuple[bytes, int, int, str]:
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    has_alpha = img.mode in ("RGBA", "LA", "P")
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    if has_alpha:
        img = img.convert("RGBA")
        img.save(buf, format="WEBP", quality=quality, method=4)
        mime = "image/webp"
    else:
        img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        mime = "image/jpeg"
    return buf.getvalue(), img.width, img.height, mime


def store_upload(db: Session, company_id: int, employee_id: int | None, name: str, mime: str, raw: bytes) -> EngagementFile:
    mime = (mime or "").lower()
    name = (name or "file").strip()[-200:] or "file"
    if mime in IMAGE_TYPES:
        if len(raw) > MAX_IMAGE_BYTES:
            raise BadRequest("Images must be under 8 MB.")
        try:
            if mime == "image/gif":
                from PIL import Image
                im = Image.open(io.BytesIO(raw))
                data, w, h, out_mime = raw, im.width, im.height, "image/gif"
                thumb = _image(raw, 480)[0]
            else:
                data, w, h, out_mime = _image(raw, 1920)
                thumb = _image(raw, 480, 78)[0]
        except Exception:
            raise BadRequest("That image could not be read.")
        row = EngagementFile(company_id=company_id, uploaded_by_employee_id=employee_id, kind="image", name=name,
                             mime=out_mime, size=len(data), width=w, height=h, data=data, thumb=thumb)
    else:
        if mime not in FILE_TYPES:
            raise BadRequest("Attach images, PDFs, Office documents, text or zip files.")
        if len(raw) > MAX_FILE_BYTES:
            raise BadRequest("Files must be under 10 MB.")
        row = EngagementFile(company_id=company_id, uploaded_by_employee_id=employee_id, kind="file", name=name,
                             mime=mime, size=len(raw), data=raw)
    db.add(row)
    db.flush()
    return row


def files_for(db: Session, company_id: int, uuids) -> list[dict]:
    """Attachment cards for uploaded file ids, in the order given; unknown ids are refused."""
    uuids = [str(u) for u in (uuids or [])][:10]
    if not uuids:
        return []
    rows = {str(f.uuid): f for f in db.query(EngagementFile).filter(EngagementFile.company_id == company_id,
                                                                     EngagementFile.uuid.in_(uuids)).all()}
    missing = [u for u in uuids if u not in rows]
    if missing:
        raise BadRequest("An attachment is missing. Upload it again.")
    return [{"uuid": u, "kind": rows[u].kind, "name": rows[u].name, "mime": rows[u].mime, "size": rows[u].size,
             "width": rows[u].width, "height": rows[u].height} for u in uuids]


def with_urls(attachments) -> list[dict]:
    return [file_card(a) for a in (attachments or [])]


# ── Mentions and hashtags ───────────────────────────────────────────

MENTION_RE = re.compile(r"@\[([^\]\n]{1,120})\]\(([0-9a-fA-F-]{36})\)")
HASHTAG_RE = re.compile(r"(?<![\w#&])#([A-Za-z][A-Za-z0-9_]{1,49})")


def mention_uuids(text: str | None) -> list[str]:
    return list(dict.fromkeys(m.group(2).lower() for m in MENTION_RE.finditer(text or "")))


def hashtags(text: str | None) -> list[str]:
    clean = MENTION_RE.sub(" ", text or "")
    return list(dict.fromkeys(m.group(1).lower() for m in HASHTAG_RE.finditer(clean)))[:20]


def plain(text: str | None) -> str:
    """Post text without mention markup: "@[Priya Sharma](…)" -> "@Priya Sharma"."""
    return MENTION_RE.sub(lambda m: "@" + m.group(1), text or "")


def check_words(text: str, blocked: list[str]) -> None:
    low = (text or "").lower()
    for w in blocked or []:
        if w and re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low):
            raise BadRequest("Your post contains a word your company does not allow on Buzz. Please rephrase it.")


def first_url(text: str | None) -> str | None:
    m = re.search(r"https?://[^\s<>()\"']+", plain(text))
    return m.group(0).rstrip(".,;:!?") if m else None


# ── Safe HTML (announcements) ───────────────────────────────────────

_ALLOWED = {"p", "br", "strong", "b", "em", "i", "u", "s", "h2", "h3", "h4", "ul", "ol", "li", "a", "blockquote",
            "hr", "span", "div", "code", "pre"}
_VOID = {"br", "hr", "img"}
# Pictures only from our own signed file links (uploaded through the editor).
_IMG_SRC = re.compile(r"^(?:https?://[^/\s\"']+)?(/api/v1/public/engagement/files/([0-9a-f-]{36})\?s=([\w-]+)(?:&(?:amp;)?thumb=1)?)$", re.I)


def _safe_img(attrs) -> str:
    m = _IMG_SRC.match((dict(attrs).get("src") or "").strip())
    if not m or not hmac.compare_digest(_sign(m.group(2)), m.group(3)):
        return ""
    alt = escape((dict(attrs).get("alt") or "")[:200], quote=True)
    return f'<img src="{escape(m.group(1).replace("&amp;", "&"), quote=True)}" alt="{alt}">'
_DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript", "template", "svg", "math"}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "img":
            self.out.append(_safe_img(attrs))
            return
        if tag not in _ALLOWED:
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if not re.match(r"^(https?:|mailto:|/)", href.strip(), re.I):
                href = ""
            self.out.append(f'<a href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">')
        else:
            self.out.append(f"<{tag}>")
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if self.skip:
            return
        if tag == "img":
            self.out.append(_safe_img(attrs))
        elif tag in _VOID:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in _ALLOWED or tag in _VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def safe_html(html: str | None) -> str:
    p = _Sanitizer()
    p.feed(html or "")
    p.close()
    return p.result().strip()


def html_text(html: str | None, limit: int = 300) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text).strip()
    from html import unescape
    text = unescape(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── Link previews ───────────────────────────────────────────────────

class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "title":
            self._in_title = True
        elif tag == "link" and "icon" in (a.get("rel") or "").lower() and a.get("href") and "icon" not in self.meta:
            self.meta["icon"] = a["href"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data


def _public_host(url: str) -> bool:
    """Refuse addresses inside our own network (no server-side request forgery)."""
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def _fetch(url: str, limit: int) -> tuple[bytes, str, str] | None:
    import httpx
    for _ in range(4):  # follow redirects by hand so every hop is checked
        if urlparse(url).scheme not in ("http", "https") or not _public_host(url):
            return None
        with httpx.Client(timeout=4.0, follow_redirects=False,
                          headers={"User-Agent": "NextGenHRMS-LinkPreview/1.0", "Accept": "text/html,image/*"}) as c:
            with c.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200:
                    return None
                buf = b""
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > limit:
                        break
                return buf[:limit], r.headers.get("content-type", ""), str(r.url)
    return None


def link_preview(db: Session, company_id: int, url: str) -> dict | None:
    """Title, description, site and a stored copy of the preview image for a web address.
    Returns None when the page can't be reached; never raises."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I) or len(url) > 1000:
        return None
    try:
        got = _fetch(url, 600_000)
        if not got:
            return None
        body, ctype, final = got
        host = urlparse(final).hostname or ""
        if "html" not in ctype.lower():
            return {"url": url, "title": host, "description": None, "site": host, "image": None}
        p = _MetaParser()
        p.feed(body.decode("utf-8", errors="ignore"))
        m = p.meta
        title = (m.get("og:title") or m.get("twitter:title") or p.title or host).strip()[:200]
        desc = (m.get("og:description") or m.get("twitter:description") or m.get("description") or "").strip()[:300] or None
        site = (m.get("og:site_name") or host.removeprefix("www.")).strip()[:100]
        image = None
        img_url = m.get("og:image") or m.get("twitter:image")
        if img_url:
            try:
                img = _fetch(urljoin(final, img_url), MAX_IMAGE_BYTES)
                if img and img[1].split(";")[0].strip().lower() in IMAGE_TYPES:
                    f = store_upload(db, company_id, None, "preview", img[1].split(";")[0].strip().lower(), img[0])
                    image = file_card(f)
            except Exception:
                image = None
        return {"url": url, "title": title, "description": desc, "site": site, "image": image}
    except Exception:
        return None
