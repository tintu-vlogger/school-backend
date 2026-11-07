"""Every custom service's records as a report source ("Services" category in Reports).

Sources belong to one company, so they come from a provider that reads the company in the
request context (see modules/reports/registry.py). Columns follow the service's current
form; values are read from the record's JSON (field keys are checked when the form is
saved, so they are safe to place in SQL).
"""
from __future__ import annotations

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, cast, func, literal_column, select
from sqlalchemy.orm import aliased

from modules.custom_forms import fields as F
from modules.reports import registry
from modules.reports.registry import Field, Source

registry.CATEGORIES.setdefault("services", "Services")


def _value_expr(R, f: dict):
    key = f["key"]
    if not F.KEY_RE.match(key):
        return None, None
    raw = R.values[key].astext
    t = f["type"] if f["type"] != "formula" else f.get("result", "number")
    if t in ("number", "decimal"):
        return cast(raw, Numeric), "number"
    if t == "currency":
        return cast(raw, Numeric), "money"
    if t == "date":
        return cast(raw, Date), "date"
    if t == "datetime":
        return cast(raw, DateTime(timezone=True)), "datetime"
    if t == "boolean":
        return cast(raw, Boolean), "bool"
    if t == "multi_choice":
        return literal_column(
            f"(SELECT string_agg(x, ', ') FROM jsonb_array_elements_text(custom_form_records.values->'{key}') x)"), "text"
    if t in F.LOOKUPS:
        if f.get("multiple"):
            table, col = {"master": ("master_values", "label"), "department": ("departments", "department_name"),
                          "location": ("locations", "name"),
                          "employee": ("employees", "concat_ws(' ', first_name, last_name)")}[t]
            return literal_column(
                f"(SELECT string_agg({col}, ', ') FROM {table} WHERE id IN (SELECT (jsonb_array_elements_text("
                f"custom_form_records.values->'{key}'))::int))"), "text"
        from core.masters import MasterValue
        from modules.departments.models import Department
        from modules.employees.models import Employee
        from modules.organization.models import Location
        ref = cast(raw, Integer)
        if t == "master":
            mv = aliased(MasterValue)
            return select(mv.label).where(mv.id == ref).scalar_subquery(), "text"
        if t == "department":
            d = aliased(Department)
            return select(d.department_name).where(d.id == ref).scalar_subquery(), "text"
        if t == "location":
            loc = aliased(Location)
            return select(loc.name).where(loc.id == ref).scalar_subquery(), "text"
        e = aliased(Employee)
        return select(func.concat_ws(" ", e.first_name, e.last_name)).where(e.id == ref).scalar_subquery(), "text"
    if t in ("table", "file"):
        return func.jsonb_array_length(func.coalesce(R.values[key], literal_column("'[]'::jsonb"))), "number"
    return raw, "text"


def source_for(s, definition: dict) -> Source:
    from modules.custom_forms.models import CustomFormRecord as R
    from modules.reports.sources._common import EmpJoin
    j = EmpJoin(f"cf{s.id}")
    statuses = [(x["key"], x["label"]) for x in (s.statuses or [])] + [
        ("pending", "In approval"), ("rejected", "Rejected"), ("cancelled", "Withdrawn")]
    fields = [
        Field("reference_no", "Reference", "text", expr=R.reference_no, default=True),
        Field("status", "Status", "enum", expr=R.status, options=statuses, default=True),
        *j.fields(default=False),
        Field("requester_name", "Requested by", "text", expr=R.requester_name, default=True),
        Field("submitted_at", "Submitted", "datetime", expr=R.submitted_at, default=True),
        Field("closed_at", "Closed", "datetime", expr=R.closed_at),
        Field("form_version", "Form version", "number", expr=R.version_id, hidden=True),
    ]
    taken = {f.key for f in fields}
    for f in F.input_fields(definition):
        expr, ftype = _value_expr(R, f)
        if expr is None:
            continue
        key = f"f_{f['key']}"
        if key in taken:
            continue
        taken.add(key)
        label = f["label"] if f["type"] not in ("table", "file") else f"{f['label']} (count)"
        opts = [(o, o) for o in f.get("options") or []] if f["type"] == "single_choice" else None
        fields.append(Field(key, label[:120], "enum" if opts else ftype, expr=expr, options=opts,
                            default=bool(f.get("summary")),
                            sensitive="people.services.records" if (f.get("permissions") or {}).get("employee") == "hidden"
                            else None))
    sid = s.id
    return Source(
        key=f"service_{s.code}"[:60], label=s.name, category="services",
        description=s.description or f"Records of the {s.name} service.",
        fields=fields,
        select_from=lambda: j.join(R.__table__, R.requester_employee_id, outer=True),
        company_col=R.company_id, employee_col=R.requester_employee_id,
        base_where=lambda: [R.service_id == sid, R.status != "draft"],
        all_perms=("people.services.records",), allow_team=True, date_field="submitted_at",
    )


def _provider() -> list[Source]:
    from modules.custom_forms.service import report_sources
    return report_sources()


registry.add_provider(_provider)
