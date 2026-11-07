"""Workplace sources: help desk cases (SLA), assets, tasks and policy acknowledgements."""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import aliased

from modules.reports.registry import Field, Source, register
from modules.reports.sources._common import EmpJoin


def _hours_between(a, b):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600, 1)


def _helpdesk() -> Source:
    from modules.employees.models import Employee
    from modules.helpdesk.models import HelpdeskCase as H, HelpdeskCategory
    j = EmpJoin("hd")
    cat = aliased(HelpdeskCategory, name="hd_cat")
    agent = aliased(Employee, name="hd_agent")
    fields = j.fields(default=False) + [
        Field("reference_no", "Case", "text", expr=H.reference_no, default=True),
        Field("subject", "Subject", "text", expr=H.subject, default=True),
        Field("category", "Category", "text", expr=cat.name, default=True),
        Field("priority", "Priority", "enum", expr=H.priority, default=True,
              options=[("low", "Low"), ("medium", "Medium"), ("high", "High"), ("urgent", "Urgent")]),
        Field("status", "Status", "text", expr=H.status, default=True),
        Field("assignee", "Assigned to", "text", expr=func.concat_ws(" ", agent.first_name, agent.last_name)),
        Field("created_at", "Raised", "datetime", expr=H.created_at, default=True),
        Field("first_response_at", "First reply", "datetime", expr=H.first_response_at),
        Field("resolved_at", "Resolved", "datetime", expr=H.resolved_at),
        Field("response_breached", "Reply SLA missed", "bool", expr=H.response_breached, default=True),
        Field("resolution_breached", "Resolution SLA missed", "bool", expr=H.resolution_breached, default=True),
        Field("hours_to_resolve", "Hours to resolve", "hours", needs=("created_at", "resolved_at"), agg="avg",
              compute=lambda r, c: _hours_between(r.get("created_at"), r.get("resolved_at"))),
        Field("hours_to_reply", "Hours to first reply", "hours", needs=("created_at", "first_response_at"), agg="avg",
              compute=lambda r, c: _hours_between(r.get("created_at"), r.get("first_response_at"))),
        Field("within_sla", "Within SLA (count)", "number", needs=("response_breached", "resolution_breached"),
              compute=lambda r, c: 0 if (r.get("response_breached") or r.get("resolution_breached")) else 1),
        Field("rating", "Rating", "number", expr=H.rating, agg="avg"),
        Field("reopened_count", "Times reopened", "number", expr=H.reopened_count),
    ]
    return Source(key="helpdesk_cases", label="Help desk cases", category="workplace",
                  description="Cases raised with HR: category, priority, response and resolution times against SLA.",
                  fields=fields,
                  select_from=lambda: j.join(H.__table__, H.requester_employee_id)
                  .join(cat, cat.id == H.category_id).outerjoin(agent, agent.id == H.assignee_employee_id),
                  company_col=H.company_id, employee_col=H.requester_employee_id,
                  all_perms=("workplace.helpdesk.manage",), allow_team=False, date_field="created_at")


def _assets() -> Source:
    from core.masters import MasterValue
    from modules.assets.models import Asset as A, AssetAssignment as X
    from sqlalchemy import and_
    from sqlalchemy.orm import join as ojoin
    cat = aliased(MasterValue, name="as_cat")
    cur = aliased(X, name="as_cur")
    j = EmpJoin("as")

    def frm():
        f = ojoin(A, cat, cat.id == A.category_id, isouter=True)
        f = f.outerjoin(cur, and_(cur.asset_id == A.id, cur.status.in_(("pending_ack", "active"))))
        return j.join(f, cur.employee_id, outer=True)

    fields = [
        Field("asset_tag", "Asset tag", "text", expr=A.asset_tag, default=True),
        Field("name", "Asset", "text", expr=A.name, default=True),
        Field("category", "Category", "text", expr=cat.label, default=True),
        Field("brand", "Brand", "text", expr=A.brand),
        Field("serial_number", "Serial number", "text", expr=A.serial_number),
        Field("status", "Status", "text", expr=A.status, default=True),
        Field("condition", "Condition", "text", expr=A.condition),
        Field("purchase_date", "Bought on", "date", expr=A.purchase_date),
        Field("purchase_value", "Cost", "money", expr=A.purchase_value),
        Field("warranty_until", "Warranty until", "date", expr=A.warranty_until),
        Field("assigned_to", "Assigned to", "text", expr=func.concat_ws(" ", j.e.first_name, j.e.last_name),
              default=True),
        Field("assigned_department", "Department", "text", expr=j.d.department_name),
        Field("assigned_on", "Assigned on", "date", expr=cur.assigned_on),
    ]
    return Source(key="assets", label="Assets", category="workplace",
                  description="The asset register with who has each asset now.",
                  fields=fields, select_from=frm, company_col=A.company_id, employee_col=None,
                  all_perms=("workplace.assets.manage",), date_field="purchase_date")


def _tasks() -> Source:
    from modules.tasks.models import Task as T
    j = EmpJoin("tk")
    fields = j.fields() + [
        Field("title", "Task", "text", expr=T.title, default=True),
        Field("priority", "Priority", "text", expr=T.priority),
        Field("status", "Status", "text", expr=T.status, default=True),
        Field("due_date", "Due", "date", expr=T.due_date, default=True),
        Field("completed_at", "Done", "datetime", expr=T.completed_at),
        Field("source_module", "From", "text", expr=T.source_module),
        Field("overdue", "Overdue", "bool", needs=("status", "due_date"),
              compute=lambda r, c: bool(r.get("due_date") and r["due_date"] < c["today"]
                                        and r.get("status") not in ("done", "cancelled"))),
    ]
    return Source(key="tasks", label="Tasks", category="workplace",
                  description="Tasks assigned to people, with due dates and status.",
                  fields=fields, select_from=lambda: j.join(T.__table__, T.assignee_employee_id),
                  company_col=T.company_id, employee_col=T.assignee_employee_id,
                  all_perms=("workplace.tasks.manage",), date_field="due_date")


def _policy_acks() -> Source:
    from modules.documents.models import Policy, PolicyAcknowledgement as K
    j = EmpJoin("pa")
    pol = aliased(Policy, name="pa_policy")
    fields = j.fields() + [
        Field("policy", "Policy", "text", expr=pol.title, default=True),
        Field("is_posh", "POSH policy", "bool", expr=pol.is_posh),
        Field("due_on", "Due", "date", expr=K.due_on, default=True),
        Field("acknowledged_at", "Acknowledged", "datetime", expr=K.acknowledged_at, default=True),
        Field("pending", "Pending", "bool", expr=K.acknowledged_at.is_(None), default=True),
        Field("reminder_count", "Reminders sent", "number", expr=K.reminder_count),
    ]
    return Source(key="policy_acknowledgements", label="Policy acknowledgements", category="workplace",
                  description="Who has read and accepted each policy, and who is still pending.",
                  fields=fields,
                  select_from=lambda: j.join(K.__table__, K.employee_id).join(pol, pol.id == K.policy_id),
                  company_col=K.company_id, employee_col=K.employee_id,
                  base_where=lambda: [pol.active.is_(True)],
                  all_perms=("workplace.files.manage",), date_field="due_on")


for _f in (_helpdesk, _assets, _tasks, _policy_acks):
    try:
        register(_f())
    except Exception as exc:  # pragma: no cover - module missing
        import logging
        logging.getLogger("hrms").warning("Report source %s not available: %s", _f.__name__, exc)
