"""Talent sources: training (learning enrolments) and review results."""
from __future__ import annotations

from sqlalchemy.orm import aliased

from modules.reports.registry import Field, Source, register
from modules.reports.sources._common import EmpJoin

ENROL_STATUS = [("enrolled", "Enrolled"), ("in_progress", "In progress"), ("completed", "Completed"),
                ("failed", "Failed"), ("waitlisted", "Waitlisted"), ("pending", "Waiting for approval"),
                ("cancelled", "Cancelled"), ("expired", "Expired")]


def _enrolments() -> Source:
    from modules.learning.models import Course, Enrolment as N
    j = EmpJoin("le")
    c = aliased(Course, name="le_course")
    fields = j.fields() + [
        Field("course", "Course", "text", expr=c.title, default=True),
        Field("mandatory", "Mandatory", "bool", expr=N.mandatory, default=True),
        Field("status", "Status", "enum", expr=N.status, options=ENROL_STATUS, default=True),
        Field("progress", "Progress %", "percent", expr=N.progress, agg="avg", default=True),
        Field("due_date", "Due", "date", expr=N.due_date, default=True),
        Field("completed_at", "Completed", "datetime", expr=N.completed_at),
        Field("score", "Score", "number", expr=N.score, agg="avg"),
        Field("passed", "Passed", "bool", expr=N.passed),
        Field("expires_on", "Certificate expires", "date", expr=N.expires_on),
        Field("completed", "Completed (count)", "number", needs=("status",),
              compute=lambda r, ctx: 1 if r.get("status") == "completed" else 0),
        Field("overdue", "Overdue", "bool", needs=("status", "due_date"),
              compute=lambda r, ctx: bool(r.get("due_date") and r["due_date"] < ctx["today"]
                                          and r.get("status") not in ("completed", "cancelled"))),
    ]
    return Source(key="learning_enrolments", label="Training", category="talent",
                  description="Course enrolments: mandatory training, progress, completion, scores and due dates.",
                  fields=fields,
                  select_from=lambda: j.join(N.__table__, N.employee_id).join(c, c.id == N.course_id),
                  company_col=N.company_id, employee_col=N.employee_id,
                  all_perms=("talent.learning.manage", "insights.reports.all_data"), date_field="due_date")


def _reviews() -> Source:
    from modules.performance.models import ReviewCycle, ReviewParticipant as P
    j = EmpJoin("rv")
    cy = aliased(ReviewCycle, name="rv_cycle")
    fields = j.fields() + [
        Field("cycle", "Review cycle", "text", expr=cy.name, default=True),
        Field("cycle_status", "Cycle status", "text", expr=cy.status),
        Field("status", "Review status", "enum", expr=P.status, default=True,
              options=[("in_progress", "In progress"), ("reviewed", "Reviewed"), ("calibrated", "Calibrated"),
                       ("published", "Published")]),
        Field("computed_score", "Score", "number", expr=P.computed_score, agg="avg"),
        Field("final_rating", "Final rating", "number", expr=P.final_rating, agg="avg", default=True),
        Field("potential", "Potential", "number", expr=P.potential, agg="avg"),
    ]
    return Source(key="review_results", label="Review results", category="talent",
                  description="Each person's review status, score and final rating per cycle.",
                  fields=fields,
                  select_from=lambda: j.join(P.__table__, P.employee_id).join(cy, cy.id == P.cycle_id),
                  company_col=P.company_id, employee_col=P.employee_id,
                  all_perms=("talent.performance.admin",), date_field=None)


for _f in (_enrolments, _reviews):
    try:
        register(_f())
    except Exception:  # pragma: no cover - module missing
        pass
