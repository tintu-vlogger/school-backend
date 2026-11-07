"""Time sources: attendance days, leave requests, and the attendance and leave modules'
own report functions (balances, LOP, attendance summary, late/early, overtime, muster roll)."""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import aliased

from modules.reports.registry import Field, Param, Source, register
from modules.reports.sources._common import EmpJoin, num

ATT_ALL = ("time.attendance.manage", "insights.reports.all_data")
LEAVE_ALL = ("time.leave.manage", "insights.reports.all_data")

ATT_STATUS = [("present", "Present"), ("absent", "Absent"), ("half_day", "Half day"), ("on_leave", "On leave"),
              ("holiday", "Holiday"), ("weekly_off", "Weekly off"), ("wfh", "Work from home"), ("on_duty", "On duty"),
              ("missing_punch", "Missing punch"), ("not_yet", "Not in yet")]
LEAVE_STATUS = [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected"), ("cancelled", "Cancelled"),
                ("cancel_pending", "Cancellation pending"), ("withdrawn", "Withdrawn")]


def _attendance() -> Source:
    from modules.attendance.models import AttendanceDay as A
    j = EmpJoin("att")
    fields = j.fields() + [
        Field("date", "Date", "date", expr=A.date, default=True),
        Field("status", "Status", "enum", expr=A.status, options=ATT_STATUS, default=True),
        Field("day_type", "Day type", "enum", expr=A.day_type,
              options=[("working", "Working day"), ("weekly_off", "Weekly off"), ("holiday", "Holiday")]),
        Field("first_in", "First in", "datetime", expr=A.first_in, default=True),
        Field("last_out", "Last out", "datetime", expr=A.last_out, default=True),
        Field("worked_hours", "Hours worked", "hours", expr=A.worked_minutes / 60.0, default=True),
        Field("late_minutes", "Late by (min)", "number", expr=A.late_minutes),
        Field("early_minutes", "Left early by (min)", "number", expr=A.early_minutes),
        Field("is_late", "Late", "bool", expr=A.is_late),
        Field("is_early_exit", "Left early", "bool", expr=A.is_early_exit),
        Field("overtime_hours", "Overtime hours", "hours", expr=A.overtime_minutes / 60.0),
        Field("night_shift", "Night shift", "bool", expr=A.is_night_shift),
        Field("payable_days", "Payable days", "number", expr=A.payable_days),
        Field("lop_days", "LOP days", "number", expr=A.lop_days),
        Field("penalty_lop_days", "Penalty LOP days", "number", expr=A.penalty_lop_days),
        Field("holiday_name", "Holiday", "text", expr=A.holiday_name),
        Field("present", "Present (count)", "number",
              needs=("status",), compute=lambda r, c: 1 if r.get("status") in ("present", "wfh", "on_duty") else 0),
        Field("absent", "Absent (count)", "number",
              needs=("status",), compute=lambda r, c: 1 if r.get("status") in ("absent", "missing_punch") else 0),
    ]
    return Source(key="attendance_days", label="Attendance by day", category="time",
                  description="One row per person per day: status, punches, hours, late marks, overtime and LOP.",
                  fields=fields, select_from=lambda: j.join(A.__table__, A.employee_id), company_col=A.company_id,
                  employee_col=A.employee_id, all_perms=ATT_ALL, date_field="date")


def _leave_requests() -> Source:
    from modules.leave.models import LeaveRequest as L, LeaveType
    j = EmpJoin("lr")
    lt = aliased(LeaveType, name="lr_type")
    fields = j.fields() + [
        Field("reference_no", "Reference", "text", expr=L.reference_no),
        Field("leave_type", "Leave type", "text", expr=lt.name, default=True),
        Field("from_date", "From", "date", expr=L.from_date, default=True),
        Field("to_date", "To", "date", expr=L.to_date, default=True),
        Field("days", "Days", "number", expr=L.days, default=True),
        Field("paid_days", "Paid days", "number", expr=L.paid_days),
        Field("lop_days", "LOP days", "number", expr=L.lop_days),
        Field("status", "Status", "enum", expr=L.status, options=LEAVE_STATUS, default=True),
        Field("applied_on", "Applied on", "datetime", expr=L.created_at),
        Field("on_behalf", "Recorded by HR", "bool", expr=L.on_behalf),
    ]
    return Source(key="leave_requests", label="Leave requests", category="time",
                  description="Every leave request with its type, dates, days and status.",
                  fields=fields,
                  select_from=lambda: j.join(L.__table__, L.employee_id).join(lt, lt.id == L.leave_type_id),
                  company_col=L.company_id, employee_col=L.employee_id, all_perms=LEAVE_ALL, date_field="from_date")


def _balances_fetch(db, company_id, ids, params, ctx):
    from modules.leave import reports
    year = params.get("year")
    out = reports.balance_report(db, company_id, int(year) if year else None, ids)
    return out["rows"]


def _balances() -> Source:
    return Source(
        key="leave_balances", label="Leave balances", category="time", per_employee=True,
        description="Opening, earned, taken, encashed, lapsed and balance per person and leave type.",
        fetch=_balances_fetch, all_perms=LEAVE_ALL,
        params=[Param("year", "Leave year", "year", None, required=False)],
        fields=[
            Field("employee_uuid", "Employee uuid", "text", hidden=True),
            Field("employee_code", "Employee ID", "text", default=True),
            Field("employee_name", "Employee", "text", default=True),
            Field("department", "Department", "text", default=True),
            Field("leave_type_name", "Leave type", "text", default=True),
            Field("year", "Year", "number"),
            Field("opening", "Opening", "number", default=True),
            Field("earned", "Earned", "number", default=True),
            Field("taken", "Taken", "number", default=True),
            Field("adjusted", "Adjusted", "number"),
            Field("encashed", "Encashed", "number"),
            Field("lapsed", "Lapsed", "number"),
            Field("balance", "Balance", "number", default=True),
        ])


def _lop_fetch(db, company_id, ids, params, ctx):
    from modules.leave import reports
    out = reports.lop_summary(db, company_id, params["from"], params["to"], ids)
    return [{**r, "dates_text": ", ".join(x["date"][5:] for x in r.get("dates", [])[:15])} for r in out["rows"]]


def _lop() -> Source:
    return Source(
        key="leave_lop", label="Loss of pay from leave", category="time", per_employee=True,
        description="Loss-of-pay days per person in a period, with pending requests listed separately.",
        fetch=_lop_fetch, all_perms=LEAVE_ALL + ("payroll.view",),
        params=[Param("from", "From", "date", "@month_start"), Param("to", "To", "date", "@month_end")],
        fields=[
            Field("employee_id", "Employee id", "number", hidden=True),
            Field("employee_code", "Employee ID", "text", default=True),
            Field("employee_name", "Employee", "text", default=True),
            Field("department", "Department", "text", default=True),
            Field("lop_days", "LOP days", "number", default=True),
            Field("pending_days", "Pending (not decided)", "number", default=True),
            Field("dates_text", "Dates", "text", default=True),
        ])


def _period_params():
    return [Param("from", "From", "date", "@month_start"), Param("to", "To", "date", "@today")]


def _summary_fetch(db, company_id, ids, params, ctx):
    from modules.attendance import service
    rows = service.payroll_summary(db, company_id, params["from"], params["to"], list(ids) if ids is not None else None)
    for r in rows:
        r["employee_name"] = r.pop("name", None)
        for k in ("payable_days", "lop_days", "penalty_lop_days", "overtime_hours", "shift_allowance_amount"):
            r[k] = num(r.get(k))
    return rows


def _summary() -> Source:
    return Source(
        key="attendance_summary", label="Attendance summary", category="time", per_employee=True,
        description="Per person for a period: present, absent, leave, holidays, late marks, payable and LOP days.",
        fetch=_summary_fetch, all_perms=ATT_ALL, params=_period_params(),
        fields=[
            Field("employee_id", "Employee id", "number", hidden=True),
            Field("employee_code", "Employee ID", "text", default=True),
            Field("employee_name", "Employee", "text", default=True),
            Field("department", "Department", "text"),
            Field("present_days", "Present", "number", default=True),
            Field("half_days", "Half days", "number", default=True),
            Field("absent_days", "Absent", "number", default=True),
            Field("leave_days", "On leave", "number", default=True),
            Field("holidays", "Holidays", "number"),
            Field("weekly_offs", "Weekly offs", "number"),
            Field("late_marks", "Late marks", "number", default=True),
            Field("payable_days", "Payable days", "number", default=True),
            Field("lop_days", "LOP days", "number", default=True),
            Field("overtime_hours", "Overtime hours", "hours"),
            Field("night_shifts", "Night shifts", "number"),
            Field("shift_allowance_amount", "Shift allowance", "money", sensitive="payroll.view"),
        ])


def _late_fetch(db, company_id, ids, params, ctx):
    from modules.attendance import service
    out = service.late_early_report(db, company_id, list(ids) if ids is not None else None, params["from"], params["to"])
    rows = []
    for r in out["rows"]:
        rows.append({"employee_id": r["employee_id"], "employee_code": r.get("employee_code"),
                     "employee_name": r.get("name"), "late": r["late"], "late_minutes": r["late_minutes"],
                     "early": r["early"], "early_minutes": r["early_minutes"], "penalty": num(r.get("penalty"))})
    return rows


def _late() -> Source:
    return Source(
        key="late_early", label="Late arrivals and early exits", category="time", per_employee=True,
        description="How often each person came late or left early, minutes lost and LOP penalty.",
        fetch=_late_fetch, all_perms=ATT_ALL, params=_period_params(),
        fields=[
            Field("employee_id", "Employee id", "number", hidden=True),
            Field("employee_code", "Employee ID", "text", default=True),
            Field("employee_name", "Employee", "text", default=True),
            Field("department", "Department", "text", default=True),
            Field("late", "Late marks", "number", default=True),
            Field("late_minutes", "Minutes late", "number", default=True),
            Field("early", "Early exits", "number", default=True),
            Field("early_minutes", "Minutes early", "number", default=True),
            Field("penalty", "Penalty LOP days", "number", default=True),
        ])


def _ot_fetch(db, company_id, ids, params, ctx):
    from modules.attendance import service
    out = service.overtime_report(db, company_id, list(ids) if ids is not None else None, params["from"], params["to"])
    rows = []
    for r in out["rows"]:
        cap = r.get("quarter_cap_hours") or 0
        rows.append({"employee_id": r["employee_id"], "employee_code": r.get("employee_code"),
                     "employee_name": r.get("name"), "overtime_hours": r["overtime_hours"],
                     "overtime_days": r["overtime_days"], "unapproved_hours": r["unapproved_hours"],
                     "quarter_hours": r["quarter_hours"], "quarter_cap_hours": cap,
                     "cap_used_percent": round(r["quarter_hours"] * 100 / cap, 1) if cap else None,
                     "alert": r["alert"]})
    return rows


def _overtime() -> Source:
    return Source(
        key="overtime", label="Overtime", category="time", per_employee=True,
        description="Overtime hours in a period, and this quarter's hours against the legal limit.",
        fetch=_ot_fetch, all_perms=ATT_ALL, params=_period_params(),
        fields=[
            Field("employee_id", "Employee id", "number", hidden=True),
            Field("employee_code", "Employee ID", "text", default=True),
            Field("employee_name", "Employee", "text", default=True),
            Field("department", "Department", "text"),
            Field("overtime_hours", "Overtime hours", "hours", default=True),
            Field("overtime_days", "Days with overtime", "number", default=True),
            Field("unapproved_hours", "Not approved (hours)", "hours"),
            Field("quarter_hours", "This quarter (hours)", "hours", default=True),
            Field("quarter_cap_hours", "Quarter limit (hours)", "hours", default=True, agg="max"),
            Field("cap_used_percent", "Limit used %", "percent", default=True, agg="max"),
            Field("alert", "Alert", "enum", options=[("ok", "Within limit"), ("near", "Close to limit"),
                                                     ("over", "Over limit")], default=True),
        ])


MUSTER_CODES = {"present": "P", "absent": "A", "half_day": "HD", "on_leave": "L", "holiday": "H", "weekly_off": "WO",
                "wfh": "WFH", "on_duty": "OD", "missing_punch": "MP", "not_yet": "", "upcoming": ""}


def _month_param(params) -> tuple[int, int]:
    v = params.get("month")
    if isinstance(v, date):
        return v.year, v.month
    y, m = (int(x) for x in str(v).split("-")[:2])
    return y, m


def _muster_fetch(db, company_id, ids, params, ctx):
    from modules.attendance import service
    y, m = _month_param(params)
    out = service.muster(db, company_id, list(ids) if ids is not None else None, y, m)
    rows = []
    for r in out["rows"]:
        row = {"employee_id": r["employee_id"], "employee_code": r.get("employee_code"), "employee_name": r["name"]}
        for day, cell in r["cells"].items():
            row[f"d{int(day[8:10]):02d}"] = MUSTER_CODES.get(cell.get("s"), "") if not cell.get("future") else ""
        s = r.get("summary") or {}
        row["present"] = s.get("present", 0)
        row["absent"] = s.get("absent", 0)
        row["leave"] = s.get("on_leave", s.get("leave", 0))
        rows.append(row)
    return rows


def _muster_fields(params) -> list[Field]:
    import calendar
    y, m = _month_param(params)
    days = calendar.monthrange(y, m)[1]
    return [Field(f"d{d:02d}", f"{d}", "text", default=True) for d in range(1, days + 1)]


def _muster() -> Source:
    return Source(
        key="muster_roll", label="Muster roll", category="time", per_employee=True,
        description="Register of attendance for a month, one column per day (P, A, L, H, WO …).",
        fetch=_muster_fetch, dynamic_fields=_muster_fields, all_perms=ATT_ALL,
        params=[Param("month", "Month", "month", "@month")],
        fields=[
            Field("employee_id", "Employee id", "number", hidden=True),
            Field("employee_code", "Employee ID", "text", default=True),
            Field("employee_name", "Employee", "text", default=True),
            Field("present", "Present", "number", default=True),
            Field("absent", "Absent", "number", default=True),
            Field("leave", "Leave", "number", default=True),
        ])


for _s in (_attendance(), _leave_requests(), _balances(), _lop(), _summary(), _late(), _overtime(), _muster()):
    register(_s)
