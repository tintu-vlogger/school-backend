"""People sources: employees, documents, job history and monthly headcount movement."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import and_, exists, func
from sqlalchemy.orm import aliased
from sqlalchemy.orm import join as ojoin

from modules.reports.registry import Field, Param, Source, register
from modules.reports.sources._common import (MONTHS, STATUS_OPTIONS, WORK_MODES, EmpJoin, age_on, band, models,
                                             next_birthday, years_between)

HR_ALL = ("people.employees.manage", "insights.reports.all_data")
SENSITIVE = "people.sensitive.view"

AGE_BANDS = [(25, "Under 25"), (30, "25–29"), (35, "30–34"), (40, "35–39"), (50, "40–49")]
TENURE_BANDS = [(1, "Under 1 year"), (3, "1–3 years"), (5, "3–5 years"), (10, "5–10 years")]


def _employees() -> Source:
    Employee, Department, Designation, Location, LegalEntity, Grade, CostCentre, MasterValue = models()
    from modules.employees.models import BankAccount
    j = EmpJoin("p")
    e = j.e
    g = aliased(Grade, name="p_grade")
    cc = aliased(CostCentre, name="p_cc")
    mgr = aliased(Employee, name="p_mgr")
    gen = aliased(MasterValue, name="p_gender")
    mar = aliased(MasterValue, name="p_marital")
    blood = aliased(MasterValue, name="p_blood")
    etype = aliased(MasterValue, name="p_etype")
    state = aliased(MasterValue, name="p_state")

    def frm():
        f = (ojoin(e, j.d, j.d.id == e.department_id, isouter=True)
             .outerjoin(j.ds, j.ds.id == e.designation_id).outerjoin(j.l, j.l.id == e.location_id)
             .outerjoin(j.le, j.le.id == e.legal_entity_id).outerjoin(g, g.id == e.grade_id)
             .outerjoin(cc, cc.id == e.cost_centre_id).outerjoin(mgr, mgr.id == e.reporting_manager_id)
             .outerjoin(gen, gen.id == e.gender_id).outerjoin(mar, mar.id == e.marital_status_id)
             .outerjoin(blood, blood.id == e.blood_group_id).outerjoin(etype, etype.id == e.employment_type_id)
             .outerjoin(state, state.id == j.l.state_id))
        return f

    has_bank = exists().where(and_(BankAccount.employee_id == e.id, BankAccount.active.is_(True)))
    fields = [
        Field("employee_id", "Employee id", "number", expr=e.id, hidden=True),
        Field("employee_code", "Employee ID", "text", expr=e.employee_code, default=True),
        Field("employee_name", "Name", "text", expr=func.concat_ws(" ", e.first_name, e.last_name), default=True),
        Field("first_name", "First name", "text", expr=e.first_name),
        Field("last_name", "Last name", "text", expr=e.last_name),
        Field("email", "Work email", "text", expr=e.email),
        Field("phone", "Mobile", "text", expr=e.phone),
        Field("personal_email", "Personal email", "text", expr=e.personal_email),
        Field("department", "Department", "text", expr=j.d.department_name, default=True),
        Field("designation", "Designation", "text", expr=j.ds.designation_name, default=True),
        Field("grade", "Grade", "text", expr=g.name),
        Field("location", "Location", "text", expr=j.l.name, default=True),
        Field("state", "State", "text", expr=state.label),
        Field("legal_entity", "Legal entity", "text", expr=j.le.name),
        Field("cost_centre", "Cost centre", "text", expr=cc.name),
        Field("employment_type", "Employment type", "text", expr=etype.label),
        Field("status", "Stage", "enum", expr=e.status, options=STATUS_OPTIONS, default=True),
        Field("active", "Active", "bool", expr=e.active),
        Field("work_mode", "Work mode", "enum", expr=e.work_mode, options=WORK_MODES),
        Field("reporting_manager", "Reporting manager", "text", expr=func.concat_ws(" ", mgr.first_name, mgr.last_name)),
        Field("reporting_manager_id", "Manager id", "number", expr=e.reporting_manager_id, hidden=True),
        Field("gender", "Gender", "text", expr=gen.label),
        Field("marital_status", "Marital status", "text", expr=mar.label),
        Field("blood_group", "Blood group", "text", expr=blood.label),
        Field("date_of_birth", "Date of birth", "date", expr=e.date_of_birth),
        Field("age", "Age", "number", needs=("date_of_birth",), agg="avg",
              compute=lambda r, c: age_on(r.get("date_of_birth"), c["today"])),
        Field("age_band", "Age group", "text", needs=("date_of_birth",),
              compute=lambda r, c: band(age_on(r.get("date_of_birth"), c["today"]), AGE_BANDS, "50 and over")),
        Field("birth_month", "Birthday month", "enum", options=MONTHS, needs=("date_of_birth",),
              compute=lambda r, c: r["date_of_birth"].month if r.get("date_of_birth") else None),
        Field("next_birthday", "Next birthday", "date", needs=("date_of_birth",),
              compute=lambda r, c: next_birthday(r.get("date_of_birth"), c["today"])),
        Field("date_of_joining", "Date of joining", "date", expr=e.date_of_joining, default=True),
        Field("probation_end_date", "Probation ends", "date", expr=e.probation_end_date),
        Field("confirmation_date", "Confirmed on", "date", expr=e.confirmation_date),
        Field("resignation_date", "Resigned on", "date", expr=e.resignation_date),
        Field("exit_date", "Exit date", "date", expr=e.exit_date),
        Field("notice_period_days", "Notice period (days)", "number", expr=e.notice_period_days, agg="avg"),
        Field("tenure_years", "Years of service", "number", needs=("date_of_joining", "exit_date"), agg="avg",
              compute=lambda r, c: years_between(r.get("date_of_joining"), r.get("exit_date") or c["today"])),
        Field("tenure_band", "Service group", "text", needs=("date_of_joining", "exit_date"),
              compute=lambda r, c: band(years_between(r.get("date_of_joining"), r.get("exit_date") or c["today"]),
                                        TENURE_BANDS, "10 years and over")),
        Field("pan", "PAN", "text", expr=e.pan, sensitive=SENSITIVE),
        Field("uan", "UAN", "text", expr=e.uan, sensitive=SENSITIVE),
        Field("esi_number", "ESI number", "text", expr=e.esi_number, sensitive=SENSITIVE),
        Field("has_pan", "PAN on file", "bool", expr=e.pan.isnot(None)),
        Field("has_uan", "UAN on file", "bool", expr=e.uan.isnot(None)),
        Field("has_bank", "Bank account on file", "bool", expr=has_bank),
        Field("missing_details", "Missing details", "text", needs=("has_pan", "has_uan", "has_bank"),
              compute=lambda r, c: ", ".join(n for k, n in (("has_pan", "PAN"), ("has_uan", "UAN"),
                                                            ("has_bank", "bank account")) if not r.get(k)) or None),
        Field("passport_expiry", "Passport expires", "date", expr=e.passport_expiry),
        Field("rehire_count", "Times rehired", "number", expr=e.rehire_count),
    ]
    return Source(
        key="employees", label="Employees", category="people",
        description="Everyone on the records, with job, personal and statutory details.",
        fields=fields, select_from=frm, company_col=e.company_id, employee_col=e.id,
        base_where=lambda: [e.archived_at.is_(None)], all_perms=HR_ALL, date_field="date_of_joining")


def _documents() -> Source:
    from modules.employees.models import EmployeeDocument
    from core.masters import MasterValue
    j = EmpJoin("doc")
    dt = aliased(MasterValue, name="doc_type")
    D = EmployeeDocument

    def frm():
        return j.join(D.__table__, D.employee_id).outerjoin(dt, dt.id == D.document_type_id)

    fields = j.fields() + [
        Field("document_type", "Document type", "text", expr=dt.label, default=True),
        Field("title", "Title", "text", expr=D.title, default=True),
        Field("document_number", "Number", "text", expr=D.document_number, sensitive=SENSITIVE),
        Field("issue_date", "Issued on", "date", expr=D.issue_date),
        Field("expiry_date", "Expires on", "date", expr=D.expiry_date, default=True),
        Field("days_to_expiry", "Days to expiry", "number", needs=("expiry_date",), agg="min",
              compute=lambda r, c: (r["expiry_date"] - c["today"]).days if r.get("expiry_date") else None,
              default=True),
        Field("verification_status", "Verification", "enum", expr=D.verification_status,
              options=[("pending", "Pending"), ("verified", "Verified"), ("rejected", "Rejected")], default=True),
    ]
    return Source(
        key="employee_documents", label="Employee documents", category="people",
        description="Documents on file for each person, with expiry and verification.",
        fields=fields, select_from=frm, company_col=D.company_id, employee_col=D.employee_id,
        base_where=lambda: [D.archived_at.is_(None)], all_perms=HR_ALL + ("people.documents.verify",),
        date_field="expiry_date")


def _job_history() -> Source:
    from modules.employees.models import JobChange
    j = EmpJoin("jh")
    H = JobChange
    kinds = [("joining", "Joined"), ("transfer", "Transfer"), ("promotion", "Promotion"),
             ("redesignation", "Designation change"), ("department_change", "Department change"),
             ("manager_change", "Manager change"), ("grade_change", "Grade change"), ("confirmation", "Confirmation"),
             ("exit", "Exit"), ("rehire", "Rehired"), ("status", "Stage change"), ("update", "Job details updated"),
             ("other", "Other change")]
    fields = j.fields() + [
        Field("change_type", "Change", "enum", expr=H.change_type, options=kinds, default=True),
        Field("effective_date", "Effective", "date", expr=H.effective_date, default=True),
        Field("change_status", "Status", "enum", expr=H.status,
              options=[("applied", "Applied"), ("scheduled", "Scheduled"), ("cancelled", "Cancelled")]),
        Field("reason", "Reason", "text", expr=H.reason),
    ]
    return Source(
        key="job_history", label="Job changes", category="people",
        description="Joining, transfers, promotions, confirmations and exits over time.",
        fields=fields, select_from=lambda: j.join(H.__table__, H.employee_id), company_col=H.company_id,
        employee_col=H.employee_id, all_perms=HR_ALL, date_field="effective_date")


def _movement_fetch(db, company_id, ids, params, ctx):
    """Opening headcount, joiners, leavers, closing and attrition % per month."""
    Employee = models()[0]
    start: date = params["from"].replace(day=1)
    end: date = params["to"]
    q = db.query(Employee.id, Employee.date_of_joining, Employee.exit_date, Employee.status).filter(
        Employee.company_id == company_id, Employee.status != "pre_joining")
    if ids is not None:
        q = q.filter(Employee.id.in_(ids or {-1}))
    people = q.all()
    rows = []
    m = start
    while m <= end:
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = nxt - timedelta(days=1)

        def on_roll(day):
            return sum(1 for _, doj, ex, st in people if (doj or date.min) <= day and (ex is None or ex >= day)
                       and not (st in ("exited", "archived") and ex is None))
        opening = on_roll(m - timedelta(days=1))
        joiners = sum(1 for _, doj, _ex, _st in people if doj and m <= doj <= last)
        leavers = sum(1 for _, _doj, ex, _st in people if ex and m <= ex <= last)
        closing = on_roll(last)
        avg = (opening + closing) / 2 or 1
        rows.append({"month": m, "month_label": m.strftime("%b %Y"), "opening": opening, "joiners": joiners,
                     "leavers": leavers, "closing": closing, "attrition_percent": round(leavers * 100 / avg, 2)})
        m = nxt
    return rows


def _movement() -> Source:
    return Source(
        key="headcount_movement", label="Headcount movement and attrition", category="people",
        description="Each month: people at the start, joiners, leavers, people at the end and attrition rate.",
        fetch=_movement_fetch, all_perms=HR_ALL, allow_team=False, allow_self=False,
        params=[Param("from", "From", "date", "@year_start"), Param("to", "To", "date", "@today")],
        fields=[
            Field("month", "Month", "date", default=True),
            Field("month_label", "Month name", "text"),
            Field("opening", "At the start", "number", default=True),
            Field("joiners", "Joiners", "number", default=True),
            Field("leavers", "Leavers", "number", default=True),
            Field("closing", "At the end", "number", default=True),
            Field("attrition_percent", "Attrition %", "percent", default=True, agg="avg"),
        ], date_field="month")


register(_employees())
register(_documents())
register(_job_history())
register(_movement())
