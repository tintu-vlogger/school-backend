"""Dashboards: access, saving the widget grid, and the data behind each widget (always run
with the viewer's own access, so a manager sees their team on a shared dashboard)."""
from __future__ import annotations

import uuid as _uuid

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from modules.analytics.models import Dashboard
from modules.reports import engine, registry
from modules.reports.registry import Principal

MANAGE = "insights.dashboards.manage"
VIEW = "insights.reports.view"
TYPES = {"kpi": "KPI tile", "line": "Line", "bar": "Bar", "stacked_bar": "Stacked bar", "area": "Area",
         "pie": "Pie", "donut": "Donut", "table": "Table", "heatmap": "Heatmap", "funnel": "Funnel",
         "gauge": "Gauge"}
MAX_WIDGETS = 30


def can_manage(p: Principal) -> bool:
    return p.is_owner or p.role == "admin" or p.can(MANAGE)


def can_view(p: Principal, d: Dashboard) -> bool:
    if can_manage(p) or (d.owner_employee_id and d.owner_employee_id == p.employee_id):
        return True
    if p.role in (d.shared_roles or []) or (p.employee_id and p.employee_id in (d.shared_employee_ids or [])):
        return True
    return bool(d.is_standard and p.can(VIEW))


def can_edit(p: Principal, d: Dashboard) -> bool:
    if p.is_owner or p.role == "admin":
        return True
    if d.is_standard:
        return can_manage(p)
    return bool(d.owner_employee_id and d.owner_employee_id == p.employee_id)


def get(db: Session, p: Principal, uuid) -> Dashboard:
    d = db.query(Dashboard).filter(Dashboard.company_id == p.company_id, Dashboard.uuid == uuid,
                                   Dashboard.archived_at.is_(None)).first()
    if d is None or not can_view(p, d):
        raise NotFound("Dashboard")
    return d


def out(p: Principal, d: Dashboard, full: bool = False) -> dict:
    o = {"uuid": str(d.uuid), "key": d.key, "name": d.name, "description": d.description, "icon": d.icon,
         "is_standard": d.is_standard, "owner_name": d.owner_name or "Company owner",
         "shared_roles": d.shared_roles or [], "shared_employee_ids": d.shared_employee_ids or [],
         "can_edit": can_edit(p, d), "can_share": can_manage(p), "widget_count": len(d.widgets or []),
         "updated_at": d.updated_at}
    if full:
        o["widgets"] = d.widgets or []
    return o


def list_dashboards(db: Session, p: Principal) -> list[dict]:
    rows = db.query(Dashboard).filter(Dashboard.company_id == p.company_id, Dashboard.archived_at.is_(None)).order_by(
        Dashboard.is_standard.desc(), Dashboard.sort_order, Dashboard.name).all()
    return [out(p, d) for d in rows if can_view(p, d)]


def clean_widget(db: Session, p: Principal, w: dict) -> dict:
    t = w.get("type")
    if t not in TYPES:
        raise BadRequest("Choose a chart type.")
    title = (w.get("title") or "").strip()[:120]
    if not title:
        raise BadRequest("Give every widget a title.")
    out = {"id": str(w.get("id") or _uuid.uuid4().hex[:8])[:20], "type": t, "title": title,
           "x": max(0, min(11, int(w.get("x") or 0))), "y": max(0, int(w.get("y") or 0)),
           "w": max(2, min(12, int(w.get("w") or 4))), "h": max(2, min(10, int(w.get("h") or 4))),
           "options": w.get("options") or {}}
    if w.get("report_uuid"):
        from modules.reports import service as reports
        r = reports.get(db, p, w["report_uuid"])
        out["report_uuid"] = str(r.uuid)
        out["config"] = w.get("config") or {}
    else:
        src = registry.get(w.get("source") or "")
        if src is None:
            raise BadRequest(f"Choose the data for “{title}”.")
        params = engine.resolve_params(src, (w.get("config") or {}).get("params"), _today(db, p))
        engine.clean_config(src, w.get("config") or {}, params)
        out["source"], out["config"] = src.key, w.get("config") or {}
    if out["x"] + out["w"] > 12:
        out["x"] = 12 - out["w"]
    return out


def _today(db: Session, p: Principal):
    from core.timeutil import today_for_company_id
    return today_for_company_id(db, p.company_id)


def save(db: Session, p: Principal, data: dict, d: Dashboard | None = None) -> Dashboard:
    if d is None and not (can_manage(p) or p.can(VIEW)):
        raise Forbidden("You can't build dashboards.")
    if d is not None and not can_edit(p, d):
        raise Forbidden("Only the person who made this dashboard can change it. Copy it to make your own.")
    name = (data.get("name") or (d.name if d else "")).strip()
    if not name:
        raise BadRequest("Give the dashboard a name.")
    if d is None:
        d = Dashboard(company_id=p.company_id, owner_employee_id=p.employee_id, owner_name=p.name or "Company owner")
        db.add(d)
    d.name = name[:120]
    if "description" in data:
        d.description = (data.get("description") or "").strip() or None
    if "icon" in data:
        d.icon = data.get("icon")
    if "widgets" in data:
        ws = data.get("widgets") or []
        if len(ws) > MAX_WIDGETS:
            raise BadRequest(f"A dashboard can have up to {MAX_WIDGETS} widgets.")
        d.widgets = [clean_widget(db, p, w) for w in ws]
        flag_modified(d, "widgets")
    if "shared_roles" in data or "shared_employee_ids" in data:
        if not can_manage(p):
            raise Forbidden("Only HR can share dashboards.")
        d.shared_roles = [str(x) for x in data.get("shared_roles") or []]
        d.shared_employee_ids = [int(x) for x in data.get("shared_employee_ids") or []]
        audit.event(db, "dashboard.shared", "dashboards", d.id,
                    {"roles": d.shared_roles, "people": d.shared_employee_ids}, entity_label=d.name)
    db.flush()
    return d


def clone(db: Session, p: Principal, d: Dashboard) -> Dashboard:
    return save(db, p, {"name": f"{d.name} (copy)", "description": d.description, "icon": d.icon,
                        "widgets": d.widgets or []})


def archive(db: Session, p: Principal, d: Dashboard) -> None:
    if d.is_standard or not can_edit(p, d):
        raise Forbidden("This dashboard can't be removed.")
    from datetime import datetime, timezone
    d.archived_at = datetime.now(timezone.utc)


# ── Widget data ────────────────────────────────────────────────────

def widget_data(db: Session, p: Principal, w: dict) -> dict:
    """The rows behind one widget, shaped for the chart."""
    base = {"id": w.get("id"), "type": w.get("type"), "title": w.get("title"), "options": w.get("options") or {}}
    try:
        if w.get("report_uuid"):
            from modules.reports import service as reports
            r = reports.get(db, p, w["report_uuid"])
            res = reports.run_report(db, p, r, w.get("config") or None)
            base["report_uuid"] = str(r.uuid)
        else:
            src = registry.get(w.get("source") or "")
            if src is None:
                return {**base, "status": "unavailable", "message": "This data is not available."}
            if registry.max_scope(db, p, src) is None:
                return {**base, "status": "no_access", "message": "You don't have access to this data."}
            res = engine.run(db, p, src, w.get("config") or {})
    except (Forbidden, NotFound) as exc:
        return {**base, "status": "no_access", "message": getattr(exc, "detail", "No access")}
    except BadRequest as exc:
        return {**base, "status": "error", "message": exc.detail}
    data = engine.to_json(res)
    cols = data["columns"]
    measures = [c for c in cols if c.get("measure")]
    groups = [c for c in cols if not c.get("measure")] if data["grouped"] else []
    opts = w.get("options") or {}
    out = {**base, "status": "ok", "scope_label": data["scope_label"], "columns": cols, "rows": data["rows"],
           "totals": data.get("totals"), "masked": data.get("masked") or [], "row_count": data["row_count"]}
    out["x"] = groups[0] if groups else None
    out["series"] = groups[1] if len(groups) > 1 else None
    out["measures"] = measures
    if w.get("type") in ("kpi", "gauge"):
        m = measures[0]["key"] if measures else None
        if m and data["rows"] and not groups:
            out["value"] = data["rows"][0].get(m)
        elif m:
            out["value"] = (data.get("totals") or {}).get(m)
        else:
            out["value"] = data["row_count"]
        if opts.get("percent") and out["value"] is not None:
            out["value"] = round(float(out["value"]) * 100, 1)
            out["max"] = 100
        else:
            out["max"] = opts.get("max")
    return out
