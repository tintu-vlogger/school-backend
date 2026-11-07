"""Engagement sources: survey results (through the surveys service, which keeps anonymous
results hidden below the response threshold) and kudos."""
from __future__ import annotations

from modules.reports.registry import Field, Source, register
from modules.reports.sources._common import EmpJoin

SURVEYS = ("engage.surveys.manage",)


def _surveys_fetch(db, company_id, ids, params, ctx):
    from modules.engagement import surveys
    from modules.engagement.models import Survey
    rows = []
    for s in db.query(Survey).filter(Survey.company_id == company_id, Survey.status != "draft").all():
        res = surveys.results(db, s)
        run = res.get("run") or {}
        trend = res.get("trend") or []
        last = trend[-1] if trend else None
        rows.append({
            "survey": s.title, "kind": s.kind, "identity": s.identity, "status": s.status,
            "run_number": run.get("number"),
            "opens_at": run.get("opens_at"), "closes_at": run.get("closes_at"),
            "invited": res.get("invited") or 0, "responses": res.get("responses") or 0,
            "response_rate": res.get("response_rate") or 0,
            "score": last["value"] if last else None,
            "score_kind": ("eNPS" if last and last.get("metric") == "enps" else "Average rating") if last else None,
            "hidden": bool(res.get("locked")),
        })
    return rows


def _surveys() -> Source:
    return Source(
        key="survey_results", label="Survey results", category="engage", fetch=_surveys_fetch,
        description="Each survey's latest run: invited, responses, response rate and score (eNPS or average).",
        all_perms=SURVEYS, allow_team=False, allow_self=False,
        fields=[
            Field("survey", "Survey", "text", default=True),
            Field("kind", "Type", "enum", options=[("survey", "Survey"), ("pulse", "Pulse"), ("enps", "eNPS")],
                  default=True),
            Field("identity", "Answers", "enum", options=[("anonymous", "Anonymous"), ("optional", "Name optional"),
                                                           ("named", "Named")]),
            Field("status", "Status", "text"),
            Field("run_number", "Run", "number"),
            Field("opens_at", "Opened", "datetime", default=True),
            Field("closes_at", "Closes", "datetime"),
            Field("invited", "Invited", "number", default=True),
            Field("responses", "Responses", "number", default=True),
            Field("response_rate", "Response rate %", "percent", default=True, agg="avg"),
            Field("score", "Score", "number", default=True, agg="avg"),
            Field("score_kind", "Score type", "text", default=True),
            Field("hidden", "Hidden (too few answers)", "bool"),
        ])


def _kudos() -> Source:
    from modules.engagement.models import Kudos, KudosRecipient as R
    from sqlalchemy.orm import aliased
    j = EmpJoin("kd")
    k = aliased(Kudos, name="kd_kudos")
    fields = j.fields() + [
        Field("given_at", "Given on", "datetime", expr=k.created_at, default=True),
        Field("points", "Points", "number", expr=k.points, default=True),
    ]
    return Source(key="kudos_received", label="Kudos received", category="engage",
                  description="Kudos each person received, with points.",
                  fields=fields, select_from=lambda: j.join(R.__table__, R.employee_id).join(k, k.id == R.kudos_id),
                  company_col=R.company_id, employee_col=R.employee_id,
                  all_perms=("engage.settings.manage", "insights.reports.all_data"), date_field="given_at")


register(_surveys())
try:
    register(_kudos())
except Exception:  # pragma: no cover
    pass
