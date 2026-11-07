"""Safe tools over HR data, shared by the in-app assistant (with or without a model) and the
MCP server. Every tool runs through the report engine with the asking person's own
access (row-level scope and field masking), so nothing they may not see ever leaves.
Each call is written to the audit log."""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from modules.reports import engine, registry
from modules.reports.registry import Principal

MAX_ROWS = 50
GROUPS = {"department": "department", "location": "location", "designation": "designation", "gender": "gender",
          "employment_type": "employment_type", "grade": "grade", "legal_entity": "legal_entity", "stage": "status",
          "status": "status", "age_group": "age_band", "service": "tenure_band", "state": "state"}
PERIODS = ("this_month", "last_month", "this_quarter", "this_year", "this_fy")
ACTIVE = {"field": "status", "op": "in", "value": ["probation", "confirmed", "notice"]}


def _compact(res: dict, limit: int = MAX_ROWS) -> dict:
    data = engine.to_json(res)
    cols = [c for c in data["columns"] if not c["key"].startswith("_")]
    rows = [{c["label"]: r.get(c["key"]) for c in cols} for r in data["rows"][:limit]]
    out = {"scope": data["scope_label"], "row_count": data["row_count"], "rows": rows,
           "columns": [c["label"] for c in cols]}
    if data.get("totals"):
        out["totals"] = {c["label"]: data["totals"].get(c["key"]) for c in cols if c["key"] in data["totals"]}
    if data["row_count"] > limit:
        out["note"] = f"Showing {limit} of {data['row_count']} rows."
    if data.get("masked"):
        out["hidden_fields"] = [c["label"] for c in cols if c["key"] in data["masked"]]
    return out


def _run(db: Session, p: Principal, source: str, config: dict) -> dict:
    src = registry.get(source)
    if src is None:
        raise NotFound("That data")
    return engine.run(db, p, src, config)


def _people_filter(q: str) -> list[dict]:
    q = (q or "").strip()
    if "@" in q:
        return [{"field": "email", "op": "contains", "value": q}]
    if q and any(ch.isdigit() for ch in q) and " " not in q:
        return [{"field": "employee_code", "op": "eq", "value": q}]
    return [{"field": "employee_name", "op": "contains", "value": q}]


def _person_ids(db: Session, p: Principal, q: str | None) -> list[str] | None:
    """Employee names matching a reference, within the person's access."""
    if not q:
        return None
    res = _run(db, p, "employees", {"columns": ["employee_name"], "filters": _people_filter(q), "totals": False})
    names = [r["employee_name"] for r in res["rows"]]
    if not names:
        raise NotFound(f"Anyone called “{q}” you can see")
    return names


# ── Tools ──────────────────────────────────────────────────────────

def search_employees(db: Session, p: Principal, args: dict) -> dict:
    q = (args.get("query") or "").strip()
    if not q:
        raise BadRequest("Say who to look for.")
    res = _run(db, p, "employees", {
        "columns": ["employee_code", "employee_name", "designation", "department", "location", "email", "status",
                    "date_of_joining", "reporting_manager"],
        "filters": _people_filter(q), "sort": [{"field": "employee_name", "dir": "asc"}],
        "limit": int(args.get("limit") or 10), "totals": False})
    out = _compact(res)
    out["summary"] = (f"Found {res['row_count']} people matching “{q}”." if res["row_count"]
                      else f"No one matching “{q}” in what you can see.")
    return out


def headcount(db: Session, p: Principal, args: dict) -> dict:
    group = args.get("group_by")
    filters = [] if args.get("include_exited") else [ACTIVE]
    for k in ("department", "location", "designation"):
        if args.get(k):
            filters.append({"field": k, "op": "contains", "value": args[k]})
    cfg: dict[str, Any] = {"filters": filters, "measures": [{"agg": "count", "label": "People"}]}
    if group:
        if group not in GROUPS:
            raise BadRequest("Group by department, location, designation, gender, employment type, grade, "
                             "legal entity, stage, age group, service or state.")
        cfg["group_by"] = [GROUPS[group]]
        cfg["sort"] = [{"field": "count", "dir": "desc"}]
    res = _run(db, p, "employees", cfg)
    out = _compact(res)
    total = (res.get("totals") or {}).get("count") if group else (res["rows"][0]["count"] if res["rows"] else 0)
    where = " ".join(f"in {args[k]}" for k in ("department", "location", "designation") if args.get(k))
    out["value"] = total
    out["summary"] = f"{total} people {where}".strip() + (f" ({res['scope_label'].lower()})"
                                                         if res["scope"] != "all" else "") + "."
    if group:
        out["chart"] = {"type": "bar", "x": res["columns"][0]["label"], "y": "People"}
        first = res["columns"][0]["label"]
        parts = [f"{r.get(first) or 'Not set'} {r.get('People')}" for r in out["rows"][:6]]
        out["summary"] = out["summary"].rstrip(".") + f" — by {first.lower()}: " + ", ".join(parts) + (
            "…" if len(out["rows"]) > 6 else ".")
    return out


def joiners_leavers(db: Session, p: Principal, args: dict) -> dict:
    kind = args.get("kind") or "joiners"
    period = args.get("period") or "this_month"
    field = "exit_date" if kind == "leavers" else "date_of_joining"
    flt = {"field": field, "op": period}
    if period == "last_n_days":
        flt["value"] = int(args.get("days") or 30)
    elif period not in PERIODS:
        raise BadRequest("Choose this month, last month, this quarter, this year, this financial year or the "
                         "last N days.")
    res = _run(db, p, "employees", {
        "columns": ["employee_code", "employee_name", "department", "designation", field],
        "filters": [flt], "sort": [{"field": field, "dir": "desc"}], "totals": False})
    out = _compact(res)
    label = period.replace("_", " ").replace("fy", "financial year").replace("n days", f"{flt.get('value')} days")
    out["value"] = res["row_count"]
    out["summary"] = f"{res['row_count']} {'left' if kind == 'leavers' else 'joined'} {label}."
    return out


def leave_balances(db: Session, p: Principal, args: dict) -> dict:
    cfg: dict[str, Any] = {"columns": ["employee_name", "leave_type_name", "taken", "balance"], "filters": [],
                           "totals": False, "sort": [{"field": "employee_name", "dir": "asc"}]}
    who = args.get("employee")
    if who and who.lower() not in ("me", "my", "mine", "myself"):
        names = _person_ids(db, p, who)
        cfg["filters"].append({"field": "employee_name", "op": "in", "value": names})
    elif p.employee_id and (who or not args.get("everyone")):
        cfg["filters"].append({"field": "employee_name", "op": "in", "value": [_my_name(db, p)]})
    if args.get("leave_type"):
        cfg["filters"].append({"field": "leave_type_name", "op": "contains", "value": args["leave_type"]})
    res = _run(db, p, "leave_balances", cfg)
    out = _compact(res)
    if res["row_count"] == 0:
        out["summary"] = "No leave balances found."
    else:
        parts = [f"{r.get('Leave type')}: {float(r.get('Balance') or 0):g}" for r in out["rows"][:8]]
        who_txt = "Your" if (not who or who.lower() in ("me", "my", "mine", "myself")) and p.employee_id else "Balances"
        out["summary"] = f"{who_txt} leave balance — " + ", ".join(parts) + ("…" if len(out["rows"]) > 8 else "")
    return out


def _my_name(db: Session, p: Principal) -> str:
    from modules.employees.models import Employee
    e = db.query(Employee).filter(Employee.company_id == p.company_id, Employee.id == p.employee_id).first()
    return f"{e.first_name} {e.last_name}".strip() if e else ""


def who_is_on_leave(db: Session, p: Principal, args: dict) -> dict:
    day = args.get("date") or "@today"
    if day == "tomorrow":
        day = "@days:1"
    if day == "today":
        day = "@today"
    res = _run(db, p, "leave_requests", {
        "columns": ["employee_name", "department", "leave_type", "from_date", "to_date", "days"],
        "filters": [{"field": "status", "op": "in", "value": ["approved", "cancel_pending"]},
                    {"field": "from_date", "op": "lte", "value": day}, {"field": "to_date", "op": "gte", "value": day}],
        "sort": [{"field": "employee_name", "dir": "asc"}], "totals": False})
    out = _compact(res)
    when = engine.resolve(day, date.today()) if isinstance(day, str) and day.startswith("@") else day
    out["value"] = res["row_count"]
    names = [r.get("Employee") for r in out["rows"][:10]]
    out["summary"] = (f"{res['row_count']} on leave on {when}: " + ", ".join(names) + (
        "…" if res["row_count"] > 10 else "") if res["row_count"] else f"No one is on approved leave on {when}.")
    return out


def attendance_summary(db: Session, p: Principal, args: dict) -> dict:
    period = args.get("period") or "this_month"
    params = {"from": "@month_start", "to": "@today"} if period == "this_month" else (
        {"from": "@last_month_start", "to": "@last_month_end"} if period == "last_month" else
        {"from": args.get("from") or "@month_start", "to": args.get("to") or "@today"})
    cfg: dict[str, Any] = {"params": params, "filters": [], "sort": [{"field": "employee_name", "dir": "asc"}],
                           "columns": ["employee_name", "present_days", "absent_days", "leave_days", "late_marks",
                                       "lop_days"]}
    who = args.get("employee")
    if who and who.lower() not in ("me", "my", "mine", "myself"):
        cfg["filters"].append({"field": "employee_name", "op": "in", "value": _person_ids(db, p, who)})
    elif who and p.employee_id:
        cfg["filters"].append({"field": "employee_name", "op": "in", "value": [_my_name(db, p)]})
    res = _run(db, p, "attendance_summary", cfg)
    out = _compact(res)
    t = out.get("totals") or {}
    out["summary"] = (f"{res['row_count']} people: {t.get('Present', 0)} present days, {t.get('Absent', 0)} absent, "
                      f"{t.get('On leave', 0)} on leave and {t.get('Late marks', 0)} late marks "
                      f"({res['params'].get('from')} to {res['params'].get('to')}).")
    return out


def pending_approvals(db: Session, p: Principal, args: dict) -> dict:
    from core.workflow.models import WorkflowInstance, WorkflowTask
    q = (db.query(WorkflowTask, WorkflowInstance).join(WorkflowInstance, WorkflowInstance.id == WorkflowTask.instance_id)
         .filter(WorkflowTask.company_id == p.company_id, WorkflowTask.status == "pending",
                 WorkflowInstance.status == "pending"))
    if p.employee_id:
        q = q.filter(WorkflowTask.approver_employee_id == p.employee_id)
    else:
        q = q.filter(WorkflowTask.approver_kind == "owner")
    rows = q.order_by(WorkflowTask.due_at.asc().nullslast()).limit(MAX_ROWS).all()
    total = q.count()
    items = [{"Request": i.title, "Reference": i.reference_no, "From": i.requester_name, "Step": t.step_name,
              "Due": t.due_at.isoformat() if t.due_at else None} for t, i in rows]
    return {"rows": items, "columns": ["Request", "Reference", "From", "Step", "Due"], "row_count": total,
            "value": total, "scope": "Your approvals",
            "summary": f"{total} approval{'s' if total != 1 else ''} waiting for you." if total
            else "Nothing is waiting for your approval."}


def list_reports(db: Session, p: Principal, args: dict) -> dict:
    from modules.reports import service as reports
    items = reports.list_reports(db, p, q=args.get("query") or None)
    rows = [{"Report": r["name"], "Key": r["key"] or r["uuid"], "About": r["description"],
             "Area": registry.CATEGORIES.get(r["category"], r["category"])} for r in items[:80]]
    return {"rows": rows, "columns": ["Report", "Key", "About", "Area"], "row_count": len(items),
            "summary": f"{len(items)} reports you can run."}


def run_report(db: Session, p: Principal, args: dict) -> dict:
    from modules.reports import service as reports
    from modules.reports.models import Report
    ref = (args.get("report") or "").strip()
    if not ref:
        raise BadRequest("Say which report to run.")
    r = None
    q = db.query(Report).filter(Report.company_id == p.company_id, Report.archived_at.is_(None))
    for cond in (Report.key == ref, Report.name.ilike(ref), Report.name.ilike(f"%{ref}%")):
        for cand in q.filter(cond).order_by(Report.is_standard.desc()).all():
            if reports.can_view(db, p, cand):
                r = cand
                break
        if r:
            break
    if r is None:
        try:
            import uuid as _u
            r = reports.get(db, p, _u.UUID(ref))
        except (ValueError, NotFound):
            raise NotFound(f"A report called “{ref}” that you can open")
    overrides = {}
    if args.get("params"):
        overrides["params"] = args["params"]
    res = reports.run_report(db, p, r, overrides)
    out = _compact(res, int(args.get("limit") or MAX_ROWS))
    out["report"] = r.name
    out["summary"] = f"“{r.name}”: {res['row_count']} rows ({res['scope_label'].lower()})."
    if r.chart and res["grouped"]:
        out["chart"] = r.chart
    return out


TOOLS: list[dict] = [
    {"name": "search_employees", "fn": search_employees,
     "description": "Find people by name, employee ID or email. Returns job details of matching people the user "
                    "may see.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Name, employee ID or email"},
         "limit": {"type": "integer", "description": "How many to return (default 10)"}}, "required": ["query"]}},
    {"name": "headcount", "fn": headcount,
     "description": "Count people on the rolls, optionally grouped (department, location, designation, gender, "
                    "employment_type, grade, legal_entity, stage, age_group, service, state) or limited to a "
                    "department, location or designation.",
     "input_schema": {"type": "object", "properties": {
         "group_by": {"type": "string", "enum": sorted(GROUPS)},
         "department": {"type": "string"}, "location": {"type": "string"}, "designation": {"type": "string"},
         "include_exited": {"type": "boolean"}}}},
    {"name": "joiners_leavers", "fn": joiners_leavers,
     "description": "People who joined or left in a period.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["joiners", "leavers"]},
         "period": {"type": "string", "enum": [*PERIODS, "last_n_days"]},
         "days": {"type": "integer"}}, "required": ["kind"]}},
    {"name": "leave_balances", "fn": leave_balances,
     "description": "Leave balances by leave type. Without an employee it returns the user's own balance; use "
                    "everyone=true for all people the user may see.",
     "input_schema": {"type": "object", "properties": {
         "employee": {"type": "string", "description": "Name or employee ID; 'me' for the user"},
         "leave_type": {"type": "string"}, "everyone": {"type": "boolean"}}}},
    {"name": "who_is_on_leave", "fn": who_is_on_leave,
     "description": "People on approved leave on a date (default today).",
     "input_schema": {"type": "object", "properties": {
         "date": {"type": "string", "description": "YYYY-MM-DD, 'today' or 'tomorrow'"}}}},
    {"name": "attendance_summary", "fn": attendance_summary,
     "description": "Present, absent, leave, late marks and LOP days per person for a period.",
     "input_schema": {"type": "object", "properties": {
         "period": {"type": "string", "enum": ["this_month", "last_month", "custom"]},
         "from": {"type": "string"}, "to": {"type": "string"},
         "employee": {"type": "string", "description": "Name, employee ID or 'me'"}}}},
    {"name": "pending_approvals", "fn": pending_approvals,
     "description": "Requests waiting for the user's approval.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_reports", "fn": list_reports,
     "description": "Reports the user can run, with their keys.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "run_report", "fn": run_report,
     "description": "Run a saved or standard report by key or name (for example 'headcount', 'leavers', "
                    "'birthdays', 'probation_due', 'attrition', 'overtime').",
     "input_schema": {"type": "object", "properties": {
         "report": {"type": "string"}, "params": {"type": "object"}, "limit": {"type": "integer"}},
         "required": ["report"]}},
]
BY_NAME: dict[str, dict] = {t["name"]: t for t in TOOLS}


def specs(allowed: list[str] | None = None) -> list[dict]:
    return [{k: t[k] for k in ("name", "description", "input_schema")} for t in TOOLS
            if allowed is None or "*" in allowed or t["name"] in allowed]


def call(db: Session, p: Principal, name: str, args: dict | None, *, allowed: list[str] | None = None) -> dict:
    """Run one tool as the person. Errors come back as {"error": ...} so a model can recover."""
    t = BY_NAME.get(name or "")
    if t is None or (allowed is not None and "*" not in allowed and name not in allowed):
        return {"error": f"Unknown or not allowed tool: {name}"}
    args = args if isinstance(args, dict) else {}
    try:
        with db.begin_nested():
            result = t["fn"](db, p, args)
    except (BadRequest, Forbidden, NotFound) as exc:
        result = {"error": exc.detail}
    audit.event(db, "assistant.tool_called", "assistant", None,
                {"tool": name, "args": {k: v for k, v in args.items() if k != "password"}, "via": p.via,
                 "rows": result.get("row_count") if isinstance(result, dict) else None,
                 "error": result.get("error") if isinstance(result, dict) else None})
    return result


Handler = Callable[[Session, Principal, dict], dict]
