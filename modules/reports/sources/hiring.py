"""Hiring source: applications by job and stage (only when the recruitment module is
installed). Used for the hiring funnel."""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import aliased

from modules.reports.registry import Field, Source, register


def _available() -> bool:
    try:
        from core.db import Base
        import modules.recruitment.models  # noqa: F401
        return "applications" in Base.metadata.tables
    except Exception:
        return False


STAGE_ORDER = ["applied", "screening", "shortlisted", "interview", "assessment", "offer", "offered", "hired", "joined"]


def _applications() -> Source:
    from modules.recruitment.models import Application as A, Candidate, Job
    from modules.departments.models import Department
    job = aliased(Job, name="ap_job")
    cand = aliased(Candidate, name="ap_cand")
    dept = aliased(Department, name="ap_dept")
    fields = [
        Field("job", "Job", "text", expr=job.title, default=True),
        Field("department", "Department", "text", expr=dept.department_name),
        Field("candidate", "Candidate", "text",
              expr=func.concat_ws(" ", getattr(cand, "first_name", cand.id), getattr(cand, "last_name", cand.id))),
        Field("stage", "Stage", "text", expr=A.stage, default=True),
        Field("stage_rank", "Stage order", "number", needs=("stage",),
              compute=lambda r, c: STAGE_ORDER.index(r["stage"]) if r.get("stage") in STAGE_ORDER else 50),
        Field("status", "Status", "text", expr=A.status, default=True),
        Field("source", "Source", "text", expr=A.source, default=True),
        Field("score", "Screening score", "number", expr=A.score, agg="avg"),
        Field("applied_at", "Applied", "datetime", expr=A.applied_at, default=True),
        Field("hired_at", "Hired", "datetime", expr=A.hired_at),
    ]
    return Source(key="applications", label="Job applications", category="talent", available=_available,
                  description="Applications by job, stage and source, for the hiring funnel.",
                  fields=fields,
                  select_from=lambda: A.__table__.join(job, job.id == A.job_id).join(cand, cand.id == A.candidate_id)
                  .outerjoin(dept, dept.id == job.department_id),
                  company_col=A.company_id, employee_col=None, all_perms=("talent.hiring.manage",),
                  date_field="applied_at")


if _available():
    try:
        register(_applications())
    except Exception:  # pragma: no cover - module still being built
        pass
