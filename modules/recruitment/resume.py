"""Read a CV: pull the text out of a PDF, Word (.docx) or text file, then fill candidate
fields with simple rules (email, phone, name, LinkedIn, years of experience, skills,
education, current role). Nothing here calls an outside service."""
from __future__ import annotations

import io
import re
import zipfile

COMMON_SKILLS = [
    "Python", "Java", "JavaScript", "TypeScript", "React", "Angular", "Vue", "Node.js", "Django", "FastAPI", "Flask",
    "Spring", "Go", "Golang", "C#", ".NET", "C++", "PHP", "Laravel", "Ruby", "Rails", "Kotlin", "Swift", "Flutter",
    "Android", "iOS", "SQL", "PostgreSQL", "MySQL", "MongoDB", "Redis", "AWS", "Azure", "GCP", "Docker",
    "Kubernetes", "Terraform", "Linux", "Git", "CI/CD", "HTML", "CSS", "Tailwind", "GraphQL", "REST", "Microservices",
    "Machine Learning", "Data Analysis", "Power BI", "Tableau", "Excel", "SAP", "Tally", "GST", "TDS", "Payroll",
    "Accounting", "Recruitment", "Sales", "Marketing", "SEO", "Customer Service", "Figma", "Photoshop",
    "Project Management", "Agile", "Scrum", "Selenium", "Testing", "Communication", "Leadership", "Negotiation",
    "Business Development", "Statutory Compliance", "Labour Law", "Employee Relations", "HRMS",
]

EDU = [
    (r"\b(ph\.?d|doctorate)\b", "PhD"), (r"\bm\.?\s?tech\b", "M.Tech"), (r"\bmba\b|\bpgdm\b", "MBA"),
    (r"\bm\.?\s?sc\b", "M.Sc"), (r"\bmca\b", "MCA"), (r"\bm\.?\s?com\b", "M.Com"), (r"\bb\.?\s?tech\b", "B.Tech"),
    (r"\bb\.?\s?e\.?\b(?!\w)", "B.E."), (r"\bbca\b", "BCA"), (r"\bb\.?\s?sc\b", "B.Sc"), (r"\bb\.?\s?com\b", "B.Com"),
    (r"\bb\.?\s?a\b(?!\w)", "B.A."), (r"\bchartered accountant\b|\bca\b(?= )", "CA"), (r"\bdiploma\b", "Diploma"),
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?91[\s-]?)?(?:0)?([6-9]\d{4}[\s-]?\d{5})\b")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9_-]+/?", re.I)
EXP_RE = re.compile(r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)(?:\s+of)?\s+(?:total\s+|overall\s+|work\s+|professional\s+)?(?:experience|exp)", re.I)
EXP_RE2 = re.compile(r"experience\s*(?:of|:)?\s*(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)", re.I)
ROLE_RE = re.compile(r"(?:currently|presently)?\s*(?:working\s+)?(?:as\s+(?:an?\s+)?)([A-Z][A-Za-z /&-]{2,40}?)\s+(?:at|with|in)\s+([A-Z][A-Za-z0-9 &.,-]{2,60}?)(?:[\n.,(]|$)")
CTC_RE = re.compile(r"(?:current\s+ctc|ctc)\s*[:\-]?\s*(?:rs\.?|inr|₹)?\s*([\d.,]+)\s*(lpa|lakhs?|l)?", re.I)
NOTICE_RE = re.compile(r"notice\s+period\s*[:\-]?\s*(\d{1,3})\s*(days?|months?)", re.I)


def extract_text(filename: str, data: bytes) -> str:
    """Plain text of a CV. Returns "" when the file can't be read (e.g. a scanned image)."""
    name = (filename or "").lower()
    try:
        if name.endswith(".pdf") or data[:4] == b"%PDF":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages[:10])[:60000]
        if name.endswith(".docx") or data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            return re.sub(r"<[^>]+>", "", xml)[:60000]
        if name.endswith((".txt", ".md", ".rtf")):
            return data.decode("utf-8", "ignore")[:60000]
    except Exception:
        return ""
    return ""


def _name_from(text: str, email: str | None) -> tuple[str | None, str | None]:
    for line in text.splitlines()[:8]:
        line = line.strip()
        if not line or EMAIL_RE.search(line) or any(ch.isdigit() for ch in line):
            continue
        if line.lower() in ("resume", "curriculum vitae", "cv", "profile"):
            continue
        words = [w for w in re.split(r"\s+", line) if w]
        if 1 < len(words) <= 4 and all(re.fullmatch(r"[A-Za-z.'-]+", w) for w in words):
            first = words[0].title()
            last = " ".join(w.title() for w in words[1:])
            return first, last
    if email:
        local = email.split("@")[0]
        bits = [b for b in re.split(r"[._-]", local) if b.isalpha()]
        if len(bits) >= 2:
            return bits[0].title(), bits[1].title()
    return None, None


def parse(text: str, extra_skills: list[str] | None = None) -> dict:
    """Fields found in the text. Only what was found is returned."""
    if not text:
        return {}
    out: dict = {}
    email = EMAIL_RE.search(text)
    if email:
        out["email"] = email.group(0).lower()
    phone = PHONE_RE.search(text)
    if phone:
        out["phone"] = re.sub(r"[\s-]", "", phone.group(1))
    li = LINKEDIN_RE.search(text)
    if li:
        url = li.group(0)
        out["linkedin_url"] = url if url.startswith("http") else "https://" + url
    first, last = _name_from(text, out.get("email"))
    if first:
        out["first_name"], out["last_name"] = first, last or ""
    years = [float(m.group(1)) for m in EXP_RE.finditer(text)] + [float(m.group(1)) for m in EXP_RE2.finditer(text)]
    if years:
        out["total_experience"] = max(y for y in years if y < 50) if any(y < 50 for y in years) else None
        if out["total_experience"] is None:
            out.pop("total_experience")
    low = text.lower()
    skills = []
    for s in list(dict.fromkeys([*(extra_skills or []), *COMMON_SKILLS])):
        pattern = r"(?<![a-z0-9+#])" + re.escape(s.lower()) + r"(?![a-z0-9+#])"
        if re.search(pattern, low):
            skills.append(s)
    if skills:
        out["skills"] = skills[:30]
    edu = [label for pat, label in EDU if re.search(pat, low)]
    if edu:
        out["education"] = ", ".join(dict.fromkeys(edu))[:300]
    role = ROLE_RE.search(text)
    if role:
        out["current_title"] = role.group(1).strip()[:150]
        out["current_company"] = role.group(2).strip().rstrip(".,")[:200]
    ctc = CTC_RE.search(text)
    if ctc:
        try:
            n = float(ctc.group(1).replace(",", ""))
            if ctc.group(2) or n < 200:
                n = n * 100000
            out["current_ctc"] = str(round(n, 2))
        except ValueError:
            pass
    notice = NOTICE_RE.search(text)
    if notice:
        n = int(notice.group(1))
        out["notice_days"] = n * 30 if notice.group(2).lower().startswith("month") else n
    for city in ("Bengaluru", "Bangalore", "Mumbai", "Pune", "Hyderabad", "Chennai", "Delhi", "Gurugram", "Gurgaon",
                 "Noida", "Kolkata", "Ahmedabad", "Kochi", "Coimbatore", "Jaipur", "Indore", "Chandigarh",
                 "Thiruvananthapuram", "Lucknow", "Nagpur", "Mysuru", "Visakhapatnam"):
        if re.search(r"\b" + city.lower() + r"\b", low[:1500]):
            out["location"] = "Bengaluru" if city == "Bangalore" else "Gurugram" if city == "Gurgaon" else city
            break
    return out
