"""Public company lookup by web address. No sign-in needed.

Used by the frontend to decide whether <slug>.campusly.app belongs to a company,
and to show the company's name and logo on its sign-in page."""
import base64

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from core.db import get_db
from modules.company.models import Company

router = APIRouter(prefix="/public/companies", tags=["Public companies"])


def _active_company(db: Session, slug: str) -> Company:
    company = db.query(Company).filter(Company.slug == slug, Company.active == True).first()  # noqa: E712
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.get("/{slug}")
def get_company(slug: str, db: Session = Depends(get_db)) -> dict:
    company = _active_company(db, slug)
    return {
        "company_name": company.company_name,
        "slug": company.slug,
        "logo_url": f"/api/v1/public/companies/{slug}/logo" if company.logo_url else "",
    }


@router.get("/{slug}/logo", include_in_schema=False)
def get_company_logo(slug: str, db: Session = Depends(get_db)) -> Response:
    data_url = _active_company(db, slug).logo_url or ""
    if not data_url.startswith("data:"):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        header, payload = data_url.split(",", 1)
        body = base64.b64decode(payload)
    except Exception:
        raise HTTPException(status_code=500, detail="Corrupt image data")
    mime = header.split(";")[0].removeprefix("data:") or "application/octet-stream"
    return Response(content=body, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})
