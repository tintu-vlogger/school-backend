"""Job boards: share links, the XML and JSON feeds job sites read, and API connectors.

Indeed and most aggregators pull the XML feed (`/public/careers/{slug}/feed.xml`).
Naukri and LinkedIn need partner API access: once the company enters its keys in the
hiring settings, `publish()` posts the job through the connector; without keys HR uses
the share link. A connector failure is recorded on the job and never blocks hiring.
"""
from __future__ import annotations

import html
import json
import logging
from xml.sax.saxutils import escape

import httpx
from sqlalchemy.orm import Session

from core.errors import BadRequest
from modules.recruitment import common as c
from modules.recruitment import settings as rsettings
from modules.recruitment.models import Job

logger = logging.getLogger("hrms")

ENDPOINTS = {
    # Partner endpoints; overridable per company with boards.<name>.endpoint
    "naukri": "https://api.naukri.com/rms/v1/jobs",
    "linkedin": "https://api.linkedin.com/v2/simpleJobPostings",
    "indeed": None,  # Indeed reads the XML feed
}


def job_url(db: Session, company_id: int, job: Job) -> str:
    return f"{c.frontend_url(db, company_id)}/careers/{job.slug}"


def share_links(db: Session, company_id: int, job: Job) -> dict:
    from urllib.parse import quote
    url = job_url(db, company_id, job)
    return {
        "url": url,
        "linkedin": f"https://www.linkedin.com/sharing/share-offsite/?url={quote(url, safe='')}",
        "whatsapp": f"https://wa.me/?text={quote(job.title + ' - ' + url, safe='')}",
        "x": f"https://twitter.com/intent/tweet?text={quote('We are hiring: ' + job.title, safe='')}&url={quote(url, safe='')}",
        "email": f"mailto:?subject={quote('Job opening: ' + job.title)}&body={quote(url)}",
    }


def _plain(text: str | None) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def feed_items(db: Session, company_id: int, jobs: list[Job]) -> list[dict]:
    from modules.recruitment import service
    labels = c.org_labels(db, company_id, departments=[j.department_id for j in jobs],
                          locations=[j.location_id for j in jobs], masters=[j.employment_type_id for j in jobs])
    out = []
    for j in jobs:
        d = service.job_out(db, j, labels=labels, public=True)
        d["url"] = job_url(db, company_id, j)
        out.append(d)
    return out


def xml_feed(db: Session, company_id: int, jobs: list[Job]) -> str:
    comp = c.company(db, company_id)
    items = feed_items(db, company_id, jobs)
    parts = ['<?xml version="1.0" encoding="utf-8"?>', "<source>",
             f"<publisher>{escape(comp.company_name if comp else '')}</publisher>",
             f"<publisherurl>{escape(c.frontend_url(db, company_id))}/careers</publisherurl>"]
    for d in items:
        salary = ""
        if d.get("salary_min") or d.get("salary_max"):
            salary = f"INR {d.get('salary_min') or ''} - {d.get('salary_max') or ''} per year"
        desc = html.escape(_plain(d.get("description") or d.get("summary") or ""))
        parts.append("<job>"
                     f"<title><![CDATA[{d['title']}]]></title>"
                     f"<date>{(d.get('published_at').strftime('%a, %d %b %Y %H:%M:%S GMT') if d.get('published_at') else '')}</date>"
                     f"<referencenumber>{escape(d.get('code') or d['uuid'])}</referencenumber>"
                     f"<url>{escape(d['url'])}</url>"
                     f"<company><![CDATA[{comp.company_name if comp else ''}]]></company>"
                     f"<city><![CDATA[{d.get('location') or ''}]]></city>"
                     f"<country>IN</country>"
                     f"<description><![CDATA[{desc}]]></description>"
                     f"<salary>{escape(salary)}</salary>"
                     f"<jobtype>{escape(d.get('employment_type') or 'Full-time')}</jobtype>"
                     f"<category><![CDATA[{d.get('department') or ''}]]></category>"
                     f"<experience>{d.get('min_experience') or 0}-{d.get('max_experience') or ''} years</experience>"
                     f"<remotetype>{'Fully remote' if d.get('work_mode') == 'remote' else ('Hybrid' if d.get('work_mode') == 'hybrid' else '')}</remotetype>"
                     "</job>")
    parts.append("</source>")
    return "".join(parts)


def json_feed(db: Session, company_id: int, jobs: list[Job]) -> dict:
    comp = c.company(db, company_id)
    items = feed_items(db, company_id, jobs)
    return {"company": comp.company_name if comp else None, "careers_url": f"{c.frontend_url(db, company_id)}/careers",
            "jobs": json.loads(json.dumps(items, default=str))}


def publish(db: Session, company_id: int, job: Job, board: str) -> dict:
    """Post the job to one board. Returns the posting record stored on the job."""
    if board not in rsettings.BOARDS:
        raise BadRequest("Unknown job site.")
    if not job.published or job.status != "open":
        raise BadRequest("Publish the job on your careers page first.")
    conf = rsettings.get(db, company_id)["boards"].get(board, {})
    if not conf.get("enabled"):
        raise BadRequest(f"Switch on {rsettings.BOARDS[board]['label']} in the hiring settings first.")
    record = {"at": c.now().isoformat(), "by": c.actor_name()}
    if board == "indeed":
        record.update(status="in_feed", message="Listed in your XML feed. Indeed picks it up on its next crawl.")
    elif not conf.get("api_key"):
        record.update(status="shared", message="No API key: share the link on the site instead.",
                      share=share_links(db, company_id, job).get("linkedin" if board == "linkedin" else "url"))
    else:
        endpoint = conf.get("endpoint") or ENDPOINTS.get(board)
        body = {"title": job.title, "description": _plain(job.description or job.summary), "external_id": str(job.uuid),
                "apply_url": job_url(db, company_id, job), "location": job.location_text,
                "experience_min": job.min_experience, "experience_max": job.max_experience,
                "account_id": conf.get("account_id")}
        try:
            r = httpx.post(endpoint, json=body, headers={"Authorization": f"Bearer {conf['api_key']}"}, timeout=10)
            if r.status_code < 300:
                ref = None
                try:
                    ref = (r.json() or {}).get("id")
                except Exception:
                    pass
                record.update(status="posted", ref=ref, message="Posted through the API.")
            else:
                record.update(status="failed", message=f"The site answered {r.status_code}. Check the keys.")
        except Exception as exc:  # network or partner errors are recorded, not raised
            logger.warning("Job board %s failed: %s", board, exc)
            record.update(status="failed", message="Could not reach the job site. Try again later.")
    postings = dict(job.postings or {})
    postings[board] = record
    job.postings = postings
    return record


def widget_snippets(db: Session, company_id: int) -> dict:
    base = c.frontend_url(db, company_id)
    comp = c.company(db, company_id)
    slug = comp.slug if comp else ""
    iframe = (f'<iframe src="{base}/careers?embed=1" title="Careers" style="width:100%;min-height:720px;border:0" '
              f'loading="lazy"></iframe>')
    script = (f'<div id="hrms-careers"></div>\n<script>(function(){{var f=document.createElement("iframe");'
              f'f.src="{base}/careers?embed=1";f.title="Careers";f.style.cssText="width:100%;border:0;min-height:720px";'
              f'f.loading="lazy";document.getElementById("hrms-careers").appendChild(f);'
              f'window.addEventListener("message",function(e){{if(e.data&&e.data.hrmsCareersHeight)'
              f'f.style.height=e.data.hrmsCareersHeight+"px";}});}})();</script>')
    return {"careers_url": f"{base}/careers", "iframe": iframe, "script": script,
            # relative to the API base address (the web app prefixes it)
            "feed_xml": f"/public/careers/{slug}/feed.xml", "feed_json": f"/public/careers/{slug}/feed.json"}
