"""Approvals history report: every decision with the time it took and whether it was
within the step's time limit (SLA), plus totals per kind of request and per approver."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Query, Session, joinedload

from core.workflow import registry
from core.workflow.models import WorkflowInstance, WorkflowTask

DECIDED = ("approved", "rejected", "reassigned", "escalated")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _label(key: str) -> str:
    rt = registry.get(key)
    return rt.label if rt else key


def _hours(start: datetime | None, end: datetime | None) -> float | None:
    if not start or not end:
        return None
    return round((end - start).total_seconds() / 3600, 1)


def base_query(db: Session, company_id: int, *, date_from: date | None, date_to: date | None,
               request_type: str | None, approver_employee_id: int | None, status: str | None,
               q: str | None, tz) -> Query:
    query = (db.query(WorkflowTask).options(joinedload(WorkflowTask.instance))
             .join(WorkflowInstance, WorkflowTask.instance_id == WorkflowInstance.id)
             .filter(WorkflowTask.company_id == company_id))
    if status == "pending":
        query = query.filter(WorkflowTask.status == "pending", WorkflowInstance.status == "pending")
    elif status:
        query = query.filter(WorkflowTask.status == status)
    else:
        query = query.filter(WorkflowTask.status.in_(DECIDED + ("pending",)))
    if date_from:
        query = query.filter(WorkflowTask.created_at >= datetime.combine(date_from, time.min, tz))
    if date_to:
        query = query.filter(WorkflowTask.created_at < datetime.combine(date_to + timedelta(days=1), time.min, tz))
    if request_type:
        query = query.filter(WorkflowInstance.request_type == request_type)
    if approver_employee_id:
        query = query.filter(WorkflowTask.approver_employee_id == approver_employee_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(WorkflowInstance.title.ilike(like), WorkflowInstance.reference_no.ilike(like),
                                 WorkflowInstance.requester_name.ilike(like), WorkflowTask.approver_name.ilike(like)))
    return query


def row(t: WorkflowTask, now: datetime | None = None) -> dict:
    now = now or _now()
    end = t.decided_at if t.status != "pending" else None
    within = None
    if t.due_at:
        within = (end or now) <= t.due_at
    i = t.instance
    return {
        "task_uuid": str(t.uuid), "request_uuid": str(i.uuid), "reference_no": i.reference_no, "title": i.title,
        "request_type": i.request_type, "request_type_label": _label(i.request_type), "requester_name": i.requester_name,
        "step_name": t.step_name, "approver_name": t.approver_name, "status": t.status,
        "decided_by_name": t.decided_by_name, "decided_via": t.decided_via, "comment": t.comment,
        "created_at": t.created_at, "decided_at": t.decided_at, "due_at": t.due_at,
        "hours_taken": _hours(t.created_at, end), "hours_waiting": _hours(t.created_at, now) if not end else None,
        "within_sla": within, "escalated": t.escalated, "request_status": i.status,
    }


def summary(rows: list[WorkflowTask], now: datetime | None = None) -> dict:
    """Totals per kind of request and per approver."""
    now = now or _now()
    by_type: dict = {}
    by_approver: dict = {}
    overall = {"decided": 0, "pending": 0, "overdue": 0, "with_sla": 0, "within_sla": 0, "hours": 0.0, "auto": 0,
               "by_email": 0}

    def bump(bucket: dict, key: str, label: str, r: dict):
        b = bucket.setdefault(key, {"key": key, "label": label, "decided": 0, "approved": 0, "rejected": 0,
                                    "pending": 0, "overdue": 0, "with_sla": 0, "within_sla": 0, "hours": 0.0})
        if r["status"] == "pending":
            b["pending"] += 1
            if r["within_sla"] is False:
                b["overdue"] += 1
            return
        b["decided"] += 1
        if r["status"] in ("approved", "rejected"):
            b[r["status"]] += 1
        b["hours"] += r["hours_taken"] or 0
        if r["within_sla"] is not None:
            b["with_sla"] += 1
            b["within_sla"] += 1 if r["within_sla"] else 0

    for t in rows:
        r = row(t, now)
        bump(by_type, r["request_type"], r["request_type_label"], r)
        bump(by_approver, r["approver_name"] or "Unknown", r["approver_name"] or "Unknown", r)
        if r["status"] == "pending":
            overall["pending"] += 1
            overall["overdue"] += 1 if r["within_sla"] is False else 0
            continue
        overall["decided"] += 1
        overall["hours"] += r["hours_taken"] or 0
        overall["auto"] += 1 if r["decided_via"] == "auto" else 0
        overall["by_email"] += 1 if r["decided_via"] == "email" else 0
        if r["within_sla"] is not None:
            overall["with_sla"] += 1
            overall["within_sla"] += 1 if r["within_sla"] else 0

    def finish(b: dict) -> dict:
        b = dict(b)
        b["avg_hours"] = round(b.pop("hours") / b["decided"], 1) if b["decided"] else None
        b["sla_rate"] = round(100 * b["within_sla"] / b["with_sla"]) if b["with_sla"] else None
        return b

    return {"overall": finish(overall),
            "by_type": sorted((finish(b) for b in by_type.values()), key=lambda x: -(x["decided"] + x["pending"])),
            "by_approver": sorted((finish(b) for b in by_approver.values()),
                                  key=lambda x: -(x["decided"] + x["pending"]))[:50]}
