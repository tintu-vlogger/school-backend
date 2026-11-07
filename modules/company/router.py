"""The signed-in company's own profile: name, registrations, address, logo and web address."""
import base64
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.db import get_db
from core.security import get_current_user, require_admin
from modules.company.models import Company
from modules.company.schemas import CompanyProfileResponse, CompanyProfileUpdate, OnboardingRequest
from modules.company.slugs import is_reserved, unique_slug

router = APIRouter(prefix="/company", tags=["Company"])


def _company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Institution not found")
    return company


def _validate_slug(slug: str, company_id: int, db: Session) -> str:
    """Normalise a web address and check it is well-formed, allowed and free."""
    s = (slug or "").strip().lower()
    if not s:
        raise HTTPException(status_code=400, detail="Enter a web address.")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", s):
        raise HTTPException(
            status_code=400,
            detail="Use only lowercase letters, numbers and hyphens (no spaces, no hyphen at the start or end).",
        )
    if len(s) < 3 or len(s) > 60:
        raise HTTPException(status_code=400, detail="Use 3 to 60 characters.")
    if is_reserved(s):
        raise HTTPException(status_code=400, detail=f"'{s}' is reserved for the platform. Please choose another web address.")
    if db.query(Company.id).filter(Company.slug == s, Company.id != company_id).first():
        raise HTTPException(status_code=400, detail="This web address is already taken.")
    return s


def _clean_ids(data: dict) -> None:
    """Upper-case and trim registration numbers (PAN, TAN, GSTIN, CIN)."""
    for key in ("pan", "tan", "gstin", "cin"):
        if key in data and data[key] is not None:
            data[key] = data[key].strip().upper() or None


def _merge_config(company: Company, extra: dict) -> None:
    config = dict(company.config) if isinstance(company.config, dict) else {}
    config.update(extra)
    company.config = config
    flag_modified(company, "config")


@router.get("/slug-check")
def slug_check(slug: str = "", current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Live check for the web-address field; suggests a free alternative when taken."""
    try:
        return {"slug": _validate_slug(slug, current_user["company_id"], db), "available": True}
    except HTTPException as e:
        out = {"slug": (slug or "").strip().lower(), "available": False, "reason": e.detail}
        if "taken" in str(e.detail) or "reserved" in str(e.detail):
            out["suggestion"] = unique_slug(db, slug)
        return out


@router.get("/profile", response_model=CompanyProfileResponse)
def get_profile(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return _company(db, current_user["company_id"])


@router.put("/profile", response_model=CompanyProfileResponse)
def update_profile(updates: CompanyProfileUpdate, current_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    company = _company(db, current_user["company_id"])
    data = updates.model_dump(exclude_unset=True)
    if data.get("slug") is not None:
        data["slug"] = _validate_slug(data["slug"], company.id, db)
    _clean_ids(data)
    if isinstance(data.get("config"), dict):
        _merge_config(company, data.pop("config"))
    for field, value in data.items():
        setattr(company, field, value)
    db.commit()
    db.refresh(company)
    return company


async def _image_data_url(file: UploadFile, max_bytes: int) -> str:
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")
    contents = await file.read()
    if len(contents) > max_bytes:
        raise HTTPException(status_code=400, detail=f"Image must be under {max_bytes // (1024 * 1024)}MB")
    return f"data:{file.content_type};base64,{base64.b64encode(contents).decode('utf-8')}"


@router.post("/profile/avatar")
async def upload_avatar(file: UploadFile = File(...), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """The company owner's own profile picture."""
    company = _company(db, current_user["company_id"])
    data_url = await _image_data_url(file, 2 * 1024 * 1024)
    _merge_config(company, {"avatar": data_url})
    db.commit()
    return {"avatar": data_url}


@router.post("/profile/logo")
async def upload_logo(file: UploadFile = File(...), current_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    """Company logo, shown in the app, on letters and payslips."""
    company = _company(db, current_user["company_id"])
    company.logo_url = await _image_data_url(file, 2 * 1024 * 1024)
    db.commit()
    return {"logo_url": company.logo_url}


@router.delete("/profile/logo")
def delete_logo(current_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    company = _company(db, current_user["company_id"])
    company.logo_url = None
    db.commit()
    return {"logo_url": ""}


@router.post("/onboarding", response_model=CompanyProfileResponse)
def complete_onboarding(req: OnboardingRequest, current_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    """First-time setup. A company must end up with a name and a web address."""
    company = _company(db, current_user["company_id"])
    data = req.model_dump(exclude_unset=True)

    name = (data.pop("name", None) or "").strip()
    if name:
        company.company_name = name
    if not (company.company_name or "").strip():
        raise HTTPException(status_code=400, detail="Enter your institution name.")
    if not data.get("slug") and not company.slug:
        raise HTTPException(status_code=400, detail="Choose your institution's web address.")
    if data.get("slug") is not None:
        data["slug"] = _validate_slug(data["slug"], company.id, db)
    _clean_ids(data)

    # Fields without their own column live in the company's settings.
    extra = {k: data.pop(k) for k in ("description", "address_line_2") if k in data}
    if isinstance(data.get("config"), dict):
        extra.update(data.pop("config"))
    if extra:
        _merge_config(company, extra)

    for field, value in data.items():
        setattr(company, field, value)
    db.commit()
    db.refresh(company)
    return company
