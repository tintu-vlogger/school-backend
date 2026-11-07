"""Pay runs: open the period, collect inputs, pre-check, calculate, review, approve,
lock, pay. Off-cycle runs (bonus, arrears, final settlement, correction) use the same flow.

Stages (status):  inputs -> checked -> calculating -> review -> pending_approval -> approved
                  -> locked -> paid        (reopen: locked/approved -> review, with a reason)

Inputs are rows in pay_run_inputs. `sync_inputs` refreshes the automatic ones (attendance or
leave LOP, overtime, shift allowance, loan instalments, approved expense claims, leave
encashment) and never touches rows typed in by hand or uploaded from Excel.
"""
from __future__ import annotations

import io
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest, Conflict, NotFound
from modules.payroll import adapters, calc, engine, statutory, structures, taxcalc
from modules.payroll import service as svc
from modules.payroll.calc import ZERO, d, money, rnd
from modules.payroll.models import (EmployeePayrollProfile, EmployeeSalary, PayGroup, PayRun, PayRunInput, Payslip,
                                    Perquisite, TaxDeclaration)

log = logging.getLogger("hrms")

KINDS = {"regular": "Monthly payroll", "bonus": "Bonus", "arrears": "Arrears", "settlement": "Final settlement",
         "correction": "Correction"}
STATUS_LABELS = {"draft": "Draft", "inputs": "Collecting inputs", "checked": "Checked", "calculating": "Calculating",
                 "review": "In review", "pending_approval": "Waiting for approval", "approved": "Approved",
                 "locked": "Locked", "paid": "Paid", "cancelled": "Cancelled", "failed": "Failed"}
INPUT_KINDS = {"lop_days": "Loss of pay days", "overtime_hours": "Overtime hours", "earning": "Earning",
               "deduction": "Deduction", "hold": "Hold salary", "skip": "Leave out of this run",
               "tds_override": "TDS amount", "arrears": "Arrears"}
EDITABLE = ("inputs", "checked", "review", "draft", "failed")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def _now():
    return datetime.now(timezone.utc)


def _stage(run: PayRun, stage: str, note: str | None = None) -> None:
    run.history = list(run.history or []) + [{"stage": stage, "by": svc.actor(), "at": _now().isoformat(), "note": note}]


def get_run(db: Session, company_id: int, uuid) -> PayRun:
    r = db.query(PayRun).filter(PayRun.company_id == company_id, PayRun.uuid == uuid).first()
    if not r:
        raise NotFound("Pay run")
    return r


def runs(db: Session, company_id: int, status: str | None = None, kind: str | None = None, fy: str | None = None):
    q = db.query(PayRun).filter(PayRun.company_id == company_id)
    if status:
        q = q.filter(PayRun.status.in_(status.split(",")))
    if kind:
        q = q.filter(PayRun.kind.in_(kind.split(",")))
    if fy:
        a, b = calc.fy_bounds(fy)
        q = q.filter(PayRun.period_start >= a, PayRun.period_start <= b)
    return q.order_by(PayRun.period_start.desc(), PayRun.id.desc())


def run_out(r: PayRun, group_name: str | None = None) -> dict:
    return {"uuid": str(r.uuid), "number": r.number, "kind": r.kind, "kind_label": KINDS.get(r.kind, r.kind), "title": r.title,
            "period_start": r.period_start.isoformat(), "period_end": r.period_end.isoformat(),
            "pay_date": r.pay_date.isoformat() if r.pay_date else None, "status": r.status,
            "status_label": STATUS_LABELS.get(r.status, r.status), "progress": r.progress, "progress_total": r.progress_total,
            "totals": r.totals or {}, "checks": r.checks or [], "acknowledgements": r.acknowledgements or {},
            "assistant": r.assistant or [], "history": r.history or [], "error": r.error, "options": r.options or {},
            "pay_group_name": group_name, "employee_ids": r.employee_ids or [],
            "locked_at": r.locked_at.isoformat() if r.locked_at else None, "paid_at": r.paid_at.isoformat() if r.paid_at else None,
            "created_by_name": r.created_by_name, "created_at": r.created_at.isoformat() if r.created_at else None}


# ── Creating a run ──────────────────────────────────────────────────

def create_run(db: Session, company_id: int, *, month: date, group: PayGroup | None, kind: str = "regular",
               title: str | None = None, employee_ids: list[int] | None = None, options: dict | None = None,
               pay_date: date | None = None) -> PayRun:
    if kind not in KINDS:
        raise BadRequest("Choose the kind of pay run.")
    group = group or svc.default_pay_group(db, company_id)
    if group is None:
        raise BadRequest("Add a pay group first (Payroll settings).")
    period = svc.period_for(db, company_id, group, month)
    if kind == "regular":
        exists = db.query(PayRun).filter(PayRun.company_id == company_id, PayRun.pay_period_id == period.id,
                                         PayRun.kind == "regular", PayRun.status != "cancelled").first()
        if exists:
            raise Conflict(f"{period.label} already has a pay run.")
        if db.query(PayRun).filter(PayRun.company_id == company_id, PayRun.kind == "regular", PayRun.pay_group_id == group.id,
                                   PayRun.status.notin_(("locked", "paid", "cancelled")),
                                   PayRun.period_start < period.start_date).first():
            raise BadRequest("Lock the earlier month's pay run before opening this one.")
    elif not employee_ids:
        raise BadRequest("Choose who is paid in this run.")
    from core import numbering
    run = PayRun(company_id=company_id, pay_group_id=group.id, pay_period_id=period.id, kind=kind,
                 title=title or (f"{period.label} payroll" if kind == "regular" else f"{KINDS[kind]} · {period.label}"),
                 period_start=period.start_date, period_end=period.end_date, pay_date=pay_date or period.pay_date,
                 status="inputs", employee_ids=employee_ids or [], options=options or {}, created_by_name=svc.actor())
    run.number = numbering.next_number(db, company_id, "pay_run", period.start_date)
    run.number = run.number or f"PR-{period.start_date:%Y%m}-{kind[:3].upper()}"
    _stage(run, "opened")
    if kind == "regular":
        period.status = "open"
    db.add(run)
    db.flush()
    audit.event(db, "payroll.run_opened", "pay_runs", run.id, {"period": period.label, "kind": kind},
                entity_uuid=str(run.uuid), entity_label=run.title)
    if kind in ("regular", "arrears"):
        sync_inputs(db, company_id, run)
    return run


def run_employees(db: Session, company_id: int, run: PayRun) -> list:
    """Who is paid: off-cycle runs list their people; a regular run pays everyone in the pay
    group who was employed in the period and has a salary."""
    Employee = svc.employee_model()
    q = db.query(Employee).filter(Employee.company_id == company_id)
    if run.employee_ids:
        return q.filter(Employee.id.in_(run.employee_ids)).order_by(Employee.first_name).all()
    group = db.query(PayGroup).filter(PayGroup.id == run.pay_group_id).first()
    emps = q.filter((Employee.date_of_joining.is_(None)) | (Employee.date_of_joining <= run.period_end),
                    (Employee.exit_date.is_(None)) | (Employee.exit_date >= run.period_start),
                    Employee.archived_at.is_(None)).order_by(Employee.first_name).all()
    emps = [e for e in emps if e.active or (e.exit_date and e.exit_date >= run.period_start)]
    profiles = svc.profiles_for(db, company_id, [e.id for e in emps])
    out = []
    for e in emps:
        p = profiles.get(e.id)
        gid = p.pay_group_id if p else None
        if gid == group.id or (gid is None and group.is_default):
            out.append(e)
    with_salary = svc.salaries_in(db, company_id, [e.id for e in out], run.period_start, run.period_end)
    settled = settled_elsewhere(db, company_id, run)
    return [e for e in out if e.id in with_salary and e.id not in settled]


def settled_elsewhere(db: Session, company_id: int, run: PayRun) -> set[int]:
    """Leavers whose final settlement pays the salary of this month: a regular run leaves
    them out so nobody is paid twice."""
    if run.kind != "regular":
        return set()
    try:
        from modules.payroll import settlement
        return settlement.salary_covered(db, company_id, run.period_start, run.period_end)
    except ImportError:  # pragma: no cover
        return set()


# ── Inputs ──────────────────────────────────────────────────────────

def inputs_of(db: Session, company_id: int, run: PayRun) -> list[PayRunInput]:
    return (db.query(PayRunInput).filter(PayRunInput.company_id == company_id, PayRunInput.pay_run_id == run.id)
            .order_by(PayRunInput.employee_id, PayRunInput.kind, PayRunInput.id).all())


def _require_editable(run: PayRun) -> None:
    if run.status not in EDITABLE:
        raise BadRequest("This pay run is past review. Reopen it to change inputs.")


def add_input(db: Session, company_id: int, run: PayRun, employee, data: dict, source: str = "manual") -> PayRunInput:
    _require_editable(run)
    kind = data.get("kind")
    if kind not in INPUT_KINDS:
        raise BadRequest("Choose what this input is.")
    code = (data.get("component_code") or "").upper() or None
    if kind in ("earning", "deduction"):
        comps = structures.component_map(db, company_id)
        c = comps.get(code or "")
        if not c:
            raise BadRequest("Choose the salary component.")
        if kind == "earning" and c.kind not in ("earning", "reimbursement"):
            raise BadRequest(f"{c.name} is not an earning.")
        if kind == "deduction" and c.kind != "deduction":
            raise BadRequest(f"{c.name} is not a deduction.")
        if d(data.get("amount")) <= 0:
            raise BadRequest("Enter an amount above zero.")
    if kind in ("lop_days", "overtime_hours") and d(data.get("days") if kind == "lop_days" else data.get("amount")) < 0:
        raise BadRequest("The value cannot be negative.")
    if kind in ("lop_days",):
        for old in db.query(PayRunInput).filter(PayRunInput.pay_run_id == run.id, PayRunInput.employee_id == employee.id,
                                                PayRunInput.kind == "lop_days").all():
            db.delete(old)
    row = PayRunInput(company_id=company_id, pay_run_id=run.id, employee_id=employee.id, kind=kind, component_code=code,
                      amount=d(data["amount"]) if data.get("amount") not in (None, "") else None,
                      days=d(data["days"]) if data.get("days") not in (None, "") else None, note=data.get("note"),
                      source=source, source_ref=data.get("source_ref"), created_by_name=svc.actor())
    db.add(row)
    if run.status in ("checked", "review", "failed"):
        run.status = "inputs"
    db.flush()
    return row


def input_out(i: PayRunInput, names: dict) -> dict:
    return {"uuid": str(i.uuid), "employee_id": i.employee_id, "employee_name": names.get(i.employee_id), "kind": i.kind,
            "kind_label": INPUT_KINDS.get(i.kind, i.kind), "component_code": i.component_code,
            "amount": money(i.amount) if i.amount is not None else None, "days": str(i.days) if i.days is not None else None,
            "note": i.note, "source": i.source, "source_ref": i.source_ref, "created_by_name": i.created_by_name}


def sync_inputs(db: Session, company_id: int, run: PayRun) -> dict:
    """Refresh the automatic inputs from attendance (or leave), loans, expenses and leave
    encashment. Rows typed in or uploaded are kept as they are."""
    _require_editable(run)
    emps = run_employees(db, company_id, run)
    ids = [e.id for e in emps]
    for row in db.query(PayRunInput).filter(PayRunInput.pay_run_id == run.id,
                                            PayRunInput.source.in_(("attendance", "leave", "loan", "expense", "encashment", "fbp", "benefits"))).all():
        db.delete(row)
    db.flush()
    manual_lop = {i.employee_id for i in db.query(PayRunInput).filter(PayRunInput.pay_run_id == run.id,
                                                                       PayRunInput.kind == "lop_days").all()}
    counts = defaultdict(int)
    if run.kind == "regular":
        att = adapters.attendance_summary(db, company_id, run.period_start, run.period_end, ids)
        lv = adapters.leave_lop(db, company_id, run.period_start, run.period_end, ids) if att is None else {}
        for e in emps:
            if e.id not in manual_lop:
                if att is not None and e.id in att:
                    a = att[e.id]
                    lop = d(a.get("lop_days")) + d(a.get("adjustment_lop_days"))
                    src = "attendance"
                else:
                    lop = d((lv.get(e.id) or {}).get("lop_days"))
                    src = "leave"
                if lop > 0:
                    db.add(PayRunInput(company_id=company_id, pay_run_id=run.id, employee_id=e.id, kind="lop_days",
                                       days=lop, source=src, note="From attendance" if src == "attendance" else "Unpaid leave"))
                    counts["lop"] += 1
            if att is not None and e.id in att:
                a = att[e.id]
                ot = d(a.get("overtime_hours")) + d(a.get("adjustment_overtime_hours"))
                if ot > 0:
                    db.add(PayRunInput(company_id=company_id, pay_run_id=run.id, employee_id=e.id, kind="overtime_hours",
                                       amount=ot, source="attendance", note="Approved overtime"))
                    counts["overtime"] += 1
                if d(a.get("shift_allowance_amount")) > 0:
                    db.add(PayRunInput(company_id=company_id, pay_run_id=run.id, employee_id=e.id, kind="earning",
                                       component_code="SHIFT", amount=d(a["shift_allowance_amount"]), source="attendance",
                                       note=f"{a.get('shift_allowance_days')} shift days"))
                    counts["shift"] += 1
        # loan instalments due this month
        try:
            from modules.loans import service as loans
            for item in loans.due_instalments(db, company_id, ids, run.period_start):
                db.add(PayRunInput(company_id=company_id, pay_run_id=run.id, employee_id=item["employee_id"], kind="deduction",
                                   component_code=item["code"], amount=item["amount"], source="loan", source_ref=item["ref"],
                                   note=item["note"]))
                counts["loans"] += 1
        except ImportError:
            pass
        # approved expense claims paid with salary
        try:
            from modules.expenses import service as expenses
            for item in expenses.payable_claims(db, company_id, ids, run.period_end):
                db.add(PayRunInput(company_id=company_id, pay_run_id=run.id, employee_id=item["employee_id"], kind="earning",
                                   component_code="REIMB", amount=item["amount"], days=item["taxable"], source="expense",
                                   source_ref=item["ref"], note=item["note"]))
                counts["expenses"] += 1
        except ImportError:
            pass
        # flexible benefit bills approved by payroll (up to what is due so far; all of it at the year end)
        try:
            from modules.payroll import fbp
            year_end = {e.id for e in emps if run.period_end.month == 3 or (e.exit_date and e.exit_date <= run.period_end)}
            for item in fbp.payable_items(db, company_id, ids, run.period_start, run.period_end, year_end=year_end):
                db.add(PayRunInput(company_id=company_id, pay_run_id=run.id, employee_id=item["employee_id"], kind="earning",
                                   component_code=item["code"], amount=item["amount"], days=item["taxable"], source="fbp",
                                   source_ref=item["ref"], note=item["note"]))
                counts["flexible benefit bills"] += 1
        except ImportError:
            pass
        # employee share of insurance premiums
        try:
            from modules.benefits import service as benefits
            for item in benefits.payroll_deductions(db, company_id, ids, run.period_start, run.period_end):
                db.add(PayRunInput(company_id=company_id, pay_run_id=run.id, employee_id=item["employee_id"], kind="deduction",
                                   component_code=item["code"], amount=item["amount"], source="benefits",
                                   source_ref=item["ref"], note=item["note"]))
                counts["insurance premiums"] += 1
        except ImportError:
            pass
        # leave encashed (year end) waiting for payroll
        try:
            from modules.leave import reports
            enc = [x for x in reports.encashments(db, company_id, date(run.period_start.year - 1, 1, 1), run.period_end)
                   if x["payroll_status"] == "pending" and x["employee_id"] in set(ids) and x.get("reason") != "exit"]
            sal = {e.id: svc.current_salary(db, company_id, e.id, run.period_end) for e in emps}
            settings = svc.get_settings(db, company_id)
            for x in enc:
                s = sal.get(x["employee_id"])
                if not s:
                    continue
                amount = encashment_amount(s, d(x["days"]), settings)
                db.add(PayRunInput(company_id=company_id, pay_run_id=run.id, employee_id=x["employee_id"], kind="earning",
                                   component_code="LEAVE_ENC", amount=amount, source="encashment", source_ref=x["entry_uuid"],
                                   note=f"{x['days']} days of {x['leave_type']}"))
                counts["encashment"] += 1
        except ImportError:
            pass
    db.flush()
    _stage(run, "inputs_synced", ", ".join(f"{v} {k}" for k, v in counts.items()) or "nothing new")
    return dict(counts)


def encashment_amount(salary: EmployeeSalary, days: Decimal, settings: dict) -> Decimal:
    st = settings.get("settlement", {})
    comps = {c["code"]: d(c["monthly"]) for c in salary.components or []}
    if st.get("leave_encash_basis") == "gross":
        base = d(salary.monthly_gross)
    else:
        base = comps.get("BASIC", ZERO) + comps.get("DA", ZERO)
    return rnd(base / d(st.get("leave_encash_divisor", 26)) * days)


INPUT_COLUMNS = ["employee_code", "kind", "component", "amount", "days", "note"]


def input_template() -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Inputs"
    ws.append(INPUT_COLUMNS)
    ws.append(["EMP001", "earning", "INCENTIVE", 5000, None, "Q3 incentive"])
    ws.append(["EMP002", "lop_days", None, None, 2, "Absent without leave"])
    ws.append(["EMP003", "deduction", "OTHER_DED", 750, None, "Canteen"])
    ws.append(["EMP004", "overtime_hours", None, 6, None, None])
    ws.append(["EMP005", "hold", None, None, None, "Absconding"])
    guide = wb.create_sheet("Guide")
    for k, v in INPUT_KINDS.items():
        guide.append([k, v])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def import_inputs(db: Session, company_id: int, run: PayRun, filename: str, content: bytes, dry_run: bool = False) -> dict:
    """Bulk inputs from Excel (.xlsx) or CSV with the template's columns."""
    _require_editable(run)
    rows: list[dict] = []
    if filename.lower().endswith(".csv"):
        import csv
        text = content.decode("utf-8-sig", errors="replace")
        rows = [{(k or "").strip().lower(): v for k, v in r.items()} for r in csv.DictReader(io.StringIO(text))]
    else:
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception:
            raise BadRequest("Upload the Excel template (.xlsx) or a CSV file.")
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [str(h or "").strip().lower() for h in next(it, [])]
        for vals in it:
            if not any(v not in (None, "") for v in vals):
                continue
            rows.append(dict(zip(header, vals)))
    Employee = svc.employee_model()
    by_code = {(e.employee_code or "").upper(): e for e in db.query(Employee).filter(Employee.company_id == company_id).all()}
    errors, ok = [], 0
    for i, r in enumerate(rows, start=2):
        e = by_code.get(str(r.get("employee_code") or "").strip().upper())
        if not e:
            errors.append({"row": i, "message": f"No employee with ID {r.get('employee_code')}."})
            continue
        try:
            with db.begin_nested():
                add_input(db, company_id, run, e, {"kind": str(r.get("kind") or "").strip(), "component_code": r.get("component"),
                                                   "amount": r.get("amount"), "days": r.get("days"), "note": r.get("note")},
                          source="excel")
                if dry_run:
                    raise _DryRun()
            ok += 1
        except _DryRun:
            ok += 1
        except Exception as exc:
            errors.append({"row": i, "message": getattr(exc, "detail", str(exc))})
    if not dry_run:
        _stage(run, "inputs_uploaded", f"{ok} rows from {filename}")
        audit.event(db, "payroll.inputs_imported", "pay_runs", run.id, {"rows": ok, "errors": len(errors)},
                    entity_uuid=str(run.uuid))
    return {"rows": len(rows), "imported": ok if not dry_run else 0, "valid": ok, "errors": errors}


class _DryRun(Exception):
    pass


# ── Pre-checks ─────────────────────────────────────────────────────

def _has_bank(db: Session, company_id: int, ids: list[int]) -> dict[int, object]:
    from modules.employees.models import BankAccount
    rows = (db.query(BankAccount).filter(BankAccount.company_id == company_id, BankAccount.employee_id.in_(ids or [-1]),
                                         BankAccount.active.is_(True))
            .order_by(BankAccount.employee_id, BankAccount.is_primary.desc(), BankAccount.id).all())
    out = {}
    for b in rows:
        out.setdefault(b.employee_id, b)
    return out


def pre_checks(db: Session, company_id: int, run: PayRun) -> list[dict]:
    """Problems to fix (or acknowledge) before calculating. Findings after calculation
    (minimum wage, negative pay, deductions over 50%, big changes) are added by `calculate`."""
    emps = run_employees(db, company_id, run)
    ids = [e.id for e in emps]
    names = {e.id: svc.name_of(e) for e in emps}
    settings = svc.get_settings(db, company_id)
    profiles = svc.profiles_for(db, company_id, ids)
    found: list[dict] = []

    def add(key, level, title, message, who=None, action=None):
        found.append({"key": key, "level": level, "title": title, "message": message,
                      "employees": [{"id": i, "name": names.get(i)} for i in sorted(set(who or []), key=lambda x: names.get(x) or "")],
                      "action": action})

    if not emps:
        add("no_employees", "block", "Nobody to pay", "No employee in this pay group has a salary for the period.",
            action="salaries")
    if run.kind == "regular":
        pend = adapters.pending_approvals(db, company_id, run.period_start, run.period_end)
        lv = [p["employee_id"] for p in pend["leave"] if p["employee_id"] in names]
        if lv:
            add("pending_leave", "warn", "Leave waiting for approval",
                f"{len(lv)} leave request(s) in this period are not decided yet; unpaid days may change.", lv, "remind")
        at = [p["employee_id"] for p in pend["attendance"] if p["employee_id"] in names]
        if at:
            add("pending_attendance", "warn", "Attendance corrections waiting",
                f"{len(at)} attendance request(s) are still pending.", at, "remind")
        locked = adapters.attendance_locked(db, company_id, run.period_start, run.period_end)
        if locked is False:
            add("attendance_open", "warn", "Attendance not locked", "Lock attendance for the period so days cannot change "
                "after payroll is calculated.", action="attendance")
    banks = _has_bank(db, company_id, ids)
    no_bank = [e.id for e in emps if (profiles.get(e.id).payment_mode if profiles.get(e.id) else "bank") == "bank"
               and e.id not in banks]
    if no_bank:
        add("missing_bank", "block" if settings["checks"].get("missing_bank") == "block" else "warn", "Missing bank details",
            f"{len(no_bank)} employee(s) are paid by bank transfer but have no bank account.", no_bank, "remind")
    no_pan = [e.id for e in emps if not (e.pan and PAN_RE.match(e.pan.upper()))]
    if no_pan:
        add("missing_pan", "warn", "Missing or invalid PAN",
            f"{len(no_pan)} employee(s) have no valid PAN; tax will be deducted at the higher rate (20% or more).", no_pan, "remind")
    no_uan = [e.id for e in emps if (profiles.get(e.id) is None or profiles[e.id].pf_applicable) and not e.uan]
    if no_uan:
        add("missing_uan", "info", "UAN not recorded", f"{len(no_uan)} PF member(s) have no UAN; the PF return needs it.", no_uan)
    joiners = [e.id for e in emps if e.date_of_joining and run.period_start <= e.date_of_joining <= run.period_end]
    if joiners:
        add("joiners", "info", "New joiners", f"{len(joiners)} joined this month and are paid for the days worked.", joiners)
    leavers = [e.id for e in emps if e.exit_date and run.period_start <= e.exit_date <= run.period_end]
    if leavers:
        add("leavers", "info", "Leavers", f"{len(leavers)} leave this month; pay them up to the last day or through final "
            "settlement.", leavers)
    holds = [i.employee_id for i in inputs_of(db, company_id, run) if i.kind == "hold"]
    if holds:
        add("holds", "info", "Salaries on hold", f"{len(holds)} salary(ies) will be calculated but not paid.", holds)
    run.checks = found
    if run.status in ("inputs", "draft"):
        run.status = "checked"
    _stage(run, "checked", f"{sum(1 for f in found if f['level'] == 'block')} to fix, "
                           f"{sum(1 for f in found if f['level'] == 'warn')} warnings")
    db.flush()
    return found


def send_reminders(db: Session, company_id: int, run: PayRun) -> dict:
    """Notify approvers of pending leave/attendance and employees missing bank or PAN details,
    in the app and by email."""
    from core.email import send_email
    from modules.notifications.service import notify
    Employee = svc.employee_model()
    sent = defaultdict(int)
    for f in run.checks or []:
        ids = [x["id"] for x in f.get("employees") or []]
        if not ids:
            continue
        emps = db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(ids)).all()
        if f["key"] in ("pending_leave", "pending_attendance"):
            managers = {e.reporting_manager_id for e in emps if e.reporting_manager_id}
            what = "leave" if f["key"] == "pending_leave" else "attendance"
            sent["approvers"] += notify(db, company_id, list(managers) or [None], kind="payroll",
                                        title=f"Please decide pending {what} requests before payroll",
                                        body=f"Payroll for {run.title} is being prepared. Approve or reject pending {what} "
                                             f"requests so pay is right.", link="/dashboard/inbox")
            for m in db.query(Employee).filter(Employee.id.in_(managers or [-1])).all():
                send_email(m.email, f"Pending {what} requests before payroll",
                           f"Hello {m.first_name},\n\nPayroll for {run.title} is being prepared. Please decide the pending "
                           f"{what} requests of your team.\n")
        elif f["key"] in ("missing_bank", "missing_pan"):
            what = "bank account" if f["key"] == "missing_bank" else "PAN"
            sent["employees"] += notify(db, company_id, ids, kind="payroll", title=f"Add your {what} for salary",
                                        body=f"Your {what} is missing. Add it in your profile before payroll runs.",
                                        link="/dashboard/profile")
            for e in emps:
                send_email(e.email, f"Add your {what} for salary",
                           f"Hello {e.first_name},\n\nYour {what} is missing in Next Gen HRMS. Please add it in your "
                           f"profile before payroll for {run.title} is processed.\n")
    _stage(run, "reminders_sent", f"{sent['approvers']} approvers, {sent['employees']} employees")
    audit.event(db, "payroll.reminders_sent", "pay_runs", run.id, dict(sent), entity_uuid=str(run.uuid))
    return dict(sent)


def acknowledge(db: Session, company_id: int, run: PayRun, key: str, reason: str) -> None:
    if not (reason or "").strip():
        raise BadRequest("Give a reason for going ahead.")
    acks = dict(run.acknowledgements or {})
    acks[key] = {"by": svc.actor(), "at": _now().isoformat(), "reason": reason.strip()}
    run.acknowledgements = acks
    audit.event(db, "payroll.check_acknowledged", "pay_runs", run.id, {"check": key, "reason": reason},
                entity_uuid=str(run.uuid))
    db.flush()


def blocking(run: PayRun) -> list[dict]:
    acks = run.acknowledgements or {}
    return [f for f in run.checks or [] if f["level"] == "block" and f["key"] not in acks]


# ── Calculation ─────────────────────────────────────────────────────

def comp_attrs(db: Session, company_id: int) -> dict[str, dict]:
    return {c.code: {"code": c.code, "name": c.name, "name_hi": c.name_hi, "kind": c.kind, "wage_class": c.wage_class,
                     "pf_wage": c.pf_wage, "esi_wage": c.esi_wage, "prorate": c.prorate, "rounding": c.rounding,
                     "taxability": c.taxability, "tax_exemption": c.tax_exemption, "one_time": c.one_time,
                     "show_on_payslip": c.show_on_payslip, "show_if_zero": c.show_if_zero, "sort_order": c.sort_order,
                     "gl_code": c.gl_code, "fbp": c.fbp}
            for c in structures.components(db, company_id)}


def _salary_amounts(s: EmployeeSalary) -> dict[str, Decimal]:
    return {c["code"]: d(c["monthly"]) for c in s.components or [] if c.get("kind") in ("earning", "reimbursement", "employer")
            and c["code"] not in ("PF_ER", "ESI_ER", "GRATUITY")}


def segments_for(salaries: list[EmployeeSalary], start: date, end: date) -> list[engine.Segment]:
    segs = []
    for i, s in enumerate(salaries):
        a = max(s.effective_from, start)
        nxt = salaries[i + 1].effective_from if i + 1 < len(salaries) else None
        b = min(end, s.effective_to or end, (nxt - timedelta(days=1)) if nxt else end)
        if a <= b:
            segs.append(engine.Segment(start=a, end=b, amounts=_salary_amounts(s)))
    return segs


def _ytd(db: Session, company_id: int, employee_id: int, fy: str, run: PayRun) -> dict:
    rows = (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
            .filter(Payslip.company_id == company_id, Payslip.employee_id == employee_id, Payslip.fy == fy,
                    Payslip.pay_run_id != run.id, PayRun.status.in_(("approved", "locked", "paid")),
                    Payslip.status != "skipped", Payslip.period_start <= run.period_start).all())
    out = defaultdict(lambda: ZERO)
    for p in rows:
        m = (p.tax or {}).get("month") or {}
        for k in ("taxable", "basic_da", "hra", "pf", "pt", "tds", "nps", "perqs", "bills"):
            out[k] += d(m.get(k))
        out["gross"] += d(p.gross)
        out["net"] += d(p.net)
        for ln in p.lines or []:
            out[f"code:{ln['code']}"] += d(ln["amount"])
    decl = _declaration(db, company_id, employee_id, fy)
    for k, v in ((decl.opening_ytd or {}) if decl else {}).items():
        if k in ("taxable", "basic_da", "hra", "pf", "pt", "tds", "nps", "perqs", "gross"):
            out[k] += d(v)
    out["rows"] = rows
    return out


def _previous_regular(db: Session, company_id: int, employee_id: int, run: PayRun) -> Payslip | None:
    return (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
            .filter(Payslip.company_id == company_id, Payslip.employee_id == employee_id, PayRun.kind == "regular",
                    PayRun.status.in_(("approved", "locked", "paid")), Payslip.period_start < run.period_start)
            .order_by(Payslip.period_start.desc()).first())


def _esi_covered(db: Session, company_id: int, employee, run: PayRun, rules: dict, full_esi_wage: Decimal,
                 profile: EmployeePayrollProfile) -> bool:
    """Coverage holds for the whole contribution period once decided at its start (or at joining)."""
    if not profile.esi_applicable:
        return False
    p_start, _ = calc.esi_period(run.period_start, rules)
    if run.period_start > p_start:
        prev = (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
                .filter(Payslip.company_id == company_id, Payslip.employee_id == employee.id, PayRun.kind == "regular",
                        PayRun.status.in_(("approved", "locked", "paid")), Payslip.period_start >= p_start,
                        Payslip.period_start < run.period_start)
                .order_by(Payslip.period_start).first())
        if prev is not None:
            return bool(((prev.statutory or {}).get("esi") or {}).get("covered"))
    return calc.esi_covered(full_esi_wage, rules, disabled=profile.disability)


def _declaration(db: Session, company_id: int, employee_id: int, fy: str) -> TaxDeclaration | None:
    return db.query(TaxDeclaration).filter(TaxDeclaration.company_id == company_id, TaxDeclaration.employee_id == employee_id,
                                           TaxDeclaration.fy == fy).first()


def declaration_amounts(decl: TaxDeclaration | None) -> dict:
    """Declared amounts until proofs are verified, then the approved amounts."""
    out = {"deductions": {}, "rent": ZERO, "lta": ZERO, "prev": {}, "other_income": ZERO, "extra_tds": ZERO, "metro": None}
    if decl is None:
        return out
    verified = decl.status == "verified"
    for it in decl.items or []:
        amt = d(it.get("approved")) if verified else d(it.get("declared"))
        if it.get("section") == "lta":
            out["lta"] += amt
            continue
        if it.get("section") == "80D_parents" and it.get("senior"):
            out["deductions"]["parents_senior"] = True
        out["deductions"][it.get("section")] = out["deductions"].get(it.get("section"), ZERO) + amt
    hra = decl.hra or {}
    for m in hra.get("months") or []:
        out["rent"] += d(m.get("approved") if verified and m.get("approved") is not None else m.get("rent"))
    out["metro"] = hra.get("metro")
    out["prev"] = decl.previous_employer or {}
    out["other_income"] = d(decl.other_income)
    out["extra_tds"] = d(decl.extra_tds_monthly)
    return out


def perquisites_monthly(db: Session, company_id: int, employee_id: int, on: date) -> Decimal:
    rows = db.query(Perquisite).filter(Perquisite.company_id == company_id, Perquisite.employee_id == employee_id,
                                       Perquisite.active.is_(True), Perquisite.effective_from <= on).all()
    total = sum((d(p.monthly_value) for p in rows if not p.effective_to or p.effective_to >= on), ZERO)
    try:
        from modules.loans import service as loans
        total += loans.perquisite_value(db, company_id, employee_id, on)
    except ImportError:
        pass
    return total


def bills_exempt_ytd(db: Session, company_id: int, employee_id: int, fy: str, cap: Decimal) -> Decimal:
    try:
        from modules.expenses import service as expenses
        return min(expenses.fbp_bills(db, company_id, employee_id, fy), cap)
    except ImportError:
        return ZERO


def fbp_accrue_codes(db: Session, company_id: int) -> set[str]:
    try:
        from modules.payroll import fbp
        return fbp.accrue_codes(db, company_id)
    except ImportError:  # pragma: no cover
        return set()


def build_input(db: Session, company_id: int, run: PayRun, e, *, book: statutory.RuleBook, settings: dict,
                comps: dict, salaries: list[EmployeeSalary], inputs: list[PayRunInput], company) -> engine.EmployeeInput:
    profile = svc.profile_for(db, company_id, e)
    rules = {"epf": book.need("epf"), "esi": book.need("esi"), "gratuity": book.need("gratuity"),
             "wage_rule": book.get("wage_rule") or {}}
    group = db.query(PayGroup).filter(PayGroup.id == run.pay_group_id).first()
    basis = (group.proration_basis if group else None) or settings.get("proration_basis", "calendar")
    regular = run.kind == "regular"
    segs = segments_for(salaries, run.period_start, run.period_end) if regular else []
    fy = calc.fy_of(run.period_end)
    age = calc.age_on(e.date_of_birth, run.period_end)
    state = adapters.employee_state(db, e, company, settings)
    pt_state = profile.pt_state or state
    lwf_state = profile.lwf_state or state
    prof = {"pf_applicable": profile.pf_applicable, "pf_employee_basis": profile.pf_employee_basis,
            "pf_employer_basis": "actual" if profile.pf_employer_basis == "actual" or settings["pf"].get("employer_basis") == "actual" else "ceiling",
            "eps_eligible": profile.eps_applicable and calc.eps_eligible(age, rules["epf"]),
            "vpf_rate": profile.vpf_rate, "vpf_amount": profile.vpf_amount, "esi_applicable": profile.esi_applicable,
            "pt_applicable": profile.pt_applicable, "lwf_applicable": profile.lwf_applicable, "gender": svc.gender_of(db, e),
            "pt_state": pt_state, "lwf_state": lwf_state}
    if prof["pf_employee_basis"] == "ceiling" and settings["pf"].get("employee_basis") == "actual":
        prof["pf_employee_basis"] = "actual"
    mine = [i for i in inputs if i.employee_id == e.id]
    lop = sum((d(i.days) for i in mine if i.kind == "lop_days"), ZERO)
    ot = sum((d(i.amount) for i in mine if i.kind == "overtime_hours"), ZERO)
    earnings, deductions, reimb, loans = [], [], [], []
    for i in mine:
        if i.kind == "earning":
            if i.component_code == "REIMB":
                reimb.append({"amount": d(i.amount), "taxable": d(i.days), "ref": i.source_ref, "note": i.note})
            elif i.source == "fbp":
                reimb.append({"code": i.component_code, "amount": d(i.amount), "taxable": d(i.days), "ref": i.source_ref,
                              "note": i.note})
            elif i.component_code == "SHIFT" and i.source == "attendance":
                earnings.append({"code": "SHIFT", "amount": d(i.amount), "note": i.note})
            else:
                earnings.append({"code": i.component_code, "amount": d(i.amount), "note": i.note})
        elif i.kind == "deduction":
            (loans if i.source == "loan" else deductions).append({"code": i.component_code, "amount": d(i.amount),
                                                                  "note": i.note, "ref": i.source_ref})
    tds_override = next((d(i.amount) for i in mine if i.kind == "tds_override"), None)
    full = _salary_amounts(salaries[-1]) if salaries else {}
    full_esi = sum((v for k, v in full.items() if comps.get(k, {}).get("esi_wage")), ZERO)
    covered = _esi_covered(db, company_id, e, run, rules["esi"], full_esi, profile)
    ytd = _ytd(db, company_id, e.id, fy, run)
    # PT
    pt_rules = book.get("pt", pt_state) if pt_state and regular else None
    half_gross = None
    if pt_rules and pt_rules.get("basis") == "half_yearly":
        h_start = date(run.period_end.year, 4, 1) if run.period_end.month in range(4, 10) else \
            date(run.period_end.year if run.period_end.month >= 10 else run.period_end.year - 1, 10, 1)
        half_gross = sum((d(p.gross) for p in ytd["rows"] if p.period_start >= h_start), ZERO)
        months_after = len([m for m in range(1, 7) if calc.add_months(h_start, m - 1) > run.period_start])
        half_gross += sum((v for k, v in full.items() if comps.get(k, {}).get("kind") == "earning"), ZERO) * months_after
    annual_gross = None
    if pt_rules and pt_rules.get("basis") == "annual":
        later = calc.months_left_in_fy(run.period_start) - 1
        annual_gross = d(ytd["gross"]) + sum((v for k, v in full.items() if comps.get(k, {}).get("kind") == "earning"), ZERO) * (later + 1)
    lwf_rules = book.get("lwf", lwf_state) if lwf_state and regular else None
    # arrears
    arrears = []
    if run.kind in ("regular", "arrears"):
        arrears = compute_arrears(db, company_id, e, run, comps, rules, settings, basis)
    # minimum wage
    mw = statutory.minimum_wage(book, profile.min_wage_state or state, profile.min_wage_zone, profile.skill) if regular else None
    # tax
    decl = _declaration(db, company_id, e.id, fy)
    regime = decl.regime if decl else settings["tax"].get("default_regime", "new")
    dec = declaration_amounts(decl)
    months_left = calc.months_left_in_fy(run.period_start)
    if e.exit_date and e.exit_date <= run.period_end:
        months_left = 1
    later = months_left - 1
    future_taxable = sum((v for k, v in full.items() if comps.get(k, {}).get("kind") in ("earning", "reimbursement")
                          and comps.get(k, {}).get("taxability", "taxable") != "exempt"), ZERO) if regular else ZERO
    f_pf_wage = sum((v for k, v in full.items() if comps.get(k, {}).get("pf_wage")), ZERO)
    f_pf = calc.pf(f_pf_wage, rules["epf"], applicable=prof["pf_applicable"], employee_basis=prof["pf_employee_basis"],
                   vpf_rate=prof["vpf_rate"], vpf_amount=prof["vpf_amount"]) if regular else calc.PFResult()
    f_pt = calc.professional_tax(pt_rules, month=5, gross=sum((v for k, v in full.items() if comps.get(k, {}).get("kind") == "earning"), ZERO),
                                 gender=prof["gender"]) if pt_rules and pt_rules.get("basis") == "monthly" else ZERO
    perqs = perquisites_monthly(db, company_id, e.id, run.period_end) if regular else ZERO
    fbp_paid = sum((d(ytd[f"code:{k}"]) for k, c in comps.items() if c.get("fbp")), ZERO)
    accrue = fbp_accrue_codes(db, company_id)
    tax_inputs = taxcalc.TaxInputs(
        regime=regime, regime_rules=book.need("income_tax", regime), sections=book.need("tax_sections"), age=age,
        has_pan=bool(e.pan and PAN_RE.match(e.pan.upper())), months_left=months_left,
        ytd_taxable=ytd["taxable"], ytd_basic_da=ytd["basic_da"], ytd_hra=ytd["hra"], ytd_pf=ytd["pf"], ytd_pt=ytd["pt"],
        ytd_tds=ytd["tds"], ytd_employer_nps=ytd["nps"], ytd_perquisites=ytd["perqs"], perquisites=perqs,
        future_taxable=future_taxable if later else ZERO, future_basic_da=(full.get("BASIC", ZERO) + full.get("DA", ZERO)) if regular else ZERO,
        future_hra=full.get("HRA", ZERO) if regular else ZERO, future_pf=f_pf.employee + f_pf.vpf, future_pt=f_pt,
        future_employer_nps=full.get("NPS_ER", ZERO) if regular else ZERO, future_perquisites=perqs,
        deductions=dec["deductions"], rent_paid=dec["rent"], metro=bool(dec["metro"] if dec["metro"] is not None else profile.metro),
        lta_exempt=dec["lta"], bills_exempt=ZERO if accrue else bills_exempt_ytd(db, company_id, e.id, fy, fbp_paid),
        previous_income=d(dec["prev"].get("income")), previous_tds=d(dec["prev"].get("tds")),
        previous_pf=d(dec["prev"].get("pf")), previous_pt=d(dec["prev"].get("pt")), other_income=dec["other_income"],
        extra_tds=dec["extra_tds"] if regular else ZERO, tds_override=tds_override)
    prev = _previous_regular(db, company_id, e.id, run) if regular else None
    working = adapters.working_days(db, e, run.period_start, run.period_end) if basis == "working_days" else None
    return engine.EmployeeInput(
        employee_id=e.id, period_start=run.period_start, period_end=run.period_end, segments=segs, comps=comps, rules=rules,
        settings=settings, join_date=e.date_of_joining, exit_date=e.exit_date, lop_days=lop, basis=basis,
        working_days=working, overtime_hours=ot, earnings=earnings, deductions=deductions, arrears=arrears,
        reimbursements=reimb, loans=loans, profile=prof, esi_covered=covered, pt_rules=pt_rules, pt_paid_fy=ytd["pt"],
        pt_half_year_gross=half_gross, pt_annual_gross=annual_gross, lwf_rules=lwf_rules, tax=tax_inputs,
        min_wage_monthly=d(mw["monthly"]) if mw else None, previous_net=d(prev.net) if prev else None,
        skip_statutory=not regular and not arrears, accrue_codes=accrue)


def compute_arrears(db: Session, company_id: int, e, run: PayRun, comps: dict, rules: dict, settings: dict,
                    basis: str) -> list[dict]:
    """Back-dated revisions: for every locked month whose salary changed around or after it was
    locked, what should have been earned minus what was paid (including arrears paid since).
    The difference is self-correcting, so looking at a month again never pays twice."""
    past = (db.query(Payslip, PayRun.locked_at).join(PayRun, PayRun.id == Payslip.pay_run_id)
            .filter(Payslip.company_id == company_id, Payslip.employee_id == e.id, PayRun.kind == "regular",
                    PayRun.status.in_(("locked", "paid")), Payslip.period_start < run.period_start,
                    Payslip.status != "skipped")
            .order_by(Payslip.period_start).all())
    if not past:
        return []
    all_sal = svc.salaries_of(db, company_id, e.id)
    out = []
    paid_arrears = defaultdict(lambda: defaultdict(lambda: ZERO))
    later_arrears = (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
                     .filter(Payslip.company_id == company_id, Payslip.employee_id == e.id, Payslip.pay_run_id != run.id,
                             PayRun.status.in_(("approved", "locked", "paid"))).all())
    for p in later_arrears:
        for a in p.arrears or []:
            paid_arrears[a["period"]][a["code"]] += d(a["amount"])
    for p, locked_at in past:
        changed = [s for s in all_sal if s.effective_from <= p.period_end and (s.effective_to is None or s.effective_to >= p.period_start)
                   and s.created_at and locked_at and s.created_at > locked_at - timedelta(days=1)]
        if not changed:
            continue
        sal = svc.salaries_in(db, company_id, [e.id], p.period_start, p.period_end).get(e.id) or []
        inp = engine.EmployeeInput(employee_id=e.id, period_start=p.period_start, period_end=p.period_end,
                                   segments=segments_for(sal, p.period_start, p.period_end), comps=comps, rules=rules,
                                   settings=settings, join_date=e.date_of_joining, exit_date=e.exit_date,
                                   lop_days=d(p.lop_days), basis=basis, skip_statutory=True)
        res = engine.compute(inp)
        should = {ln["code"]: d(ln["amount"]) for ln in res["earnings"] if ln["code"] in comps and
                  comps[ln["code"]].get("kind") == "earning" and not comps[ln["code"]].get("one_time")
                  and ln["code"] not in ("OT", "SHIFT")}
        paid = {ln["code"]: d(ln["amount"]) for ln in p.lines or [] if ln.get("kind") == "earning"}
        label = p.period_start.strftime("%Y-%m")
        for code, amt in should.items():
            diff = amt - paid.get(code, ZERO) - paid_arrears[label][code]
            if abs(diff) >= 1:
                out.append({"code": code, "name": comps[code]["name"], "amount": rnd(diff), "period": label})
    return out


def calculate(db: Session, company_id: int, run: PayRun, employee_ids: list[int] | None = None) -> dict:
    """Work out every payslip of the run (or only some employees), then totals, the review
    findings and the payroll check assistant."""
    if run.status not in EDITABLE + ("calculating",):
        raise BadRequest("This pay run is past review. Reopen it to recalculate.")
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == company_id).first()
    emps = run_employees(db, company_id, run)
    if employee_ids:
        emps = [e for e in emps if e.id in set(employee_ids)]
    book = statutory.RuleBook(db, company_id, run.period_end)
    settings = svc.get_settings(db, company_id)
    comps = comp_attrs(db, company_id)
    sal = svc.salaries_in(db, company_id, [e.id for e in emps], run.period_start, run.period_end)
    inputs = inputs_of(db, company_id, run)
    skip = {i.employee_id for i in inputs if i.kind == "skip"}
    holds = {i.employee_id: i.note for i in inputs if i.kind == "hold"}
    banks = _has_bank(db, company_id, [e.id for e in emps])
    entity_cache: dict = {}
    run.status, run.progress, run.progress_total, run.error = "calculating", 0, len(emps), None
    db.flush()
    existing = {p.employee_id: p for p in db.query(Payslip).filter(Payslip.pay_run_id == run.id).all()}
    keep = set()
    for n, e in enumerate(emps, start=1):
        keep.add(e.id)
        ps = existing.get(e.id) or Payslip(company_id=company_id, pay_run_id=run.id, employee_id=e.id,
                                           period_start=run.period_start, period_end=run.period_end,
                                           fy=calc.fy_of(run.period_end))
        if ps.id is None:
            db.add(ps)
        try:
            if e.id in skip:
                ps.status, ps.lines, ps.gross, ps.deductions, ps.net, ps.employer_cost = "skipped", [], ZERO, ZERO, ZERO, ZERO
                ps.warnings = []
                continue
            if run.kind == "settlement":
                from modules.payroll import settlement
                settlement.fill_payslip(db, company_id, run, ps, e, banks.get(e.id), entity_cache, company)
                ps.status, ps.hold_reason = ("held", holds[e.id] or "On hold") if e.id in holds else ("calculated", None)
                run.progress = n
                continue
            inp = build_input(db, company_id, run, e, book=book, settings=settings, comps=comps,
                              salaries=sal.get(e.id, []), inputs=inputs, company=company)
            res = engine.compute(inp)
            if inp.accrue_codes and run.kind == "regular" and (run.period_end.month == 3 or (e.exit_date and e.exit_date <= run.period_end)):
                res = _fbp_year_end(db, company_id, run, e, inp, res)
            _store(ps, res, inp, e, banks.get(e.id), entity_cache, db, company)
            if e.id in holds:
                ps.status, ps.hold_reason = "held", holds[e.id] or "On hold"
            else:
                ps.status, ps.hold_reason = "calculated", None
        except Exception as exc:  # one person's problem must not stop the run
            log.exception("Payslip failed for employee %s", e.id)
            ps.status = "error"
            ps.warnings = [{"code": "error", "level": "block", "message": getattr(exc, "detail", None) or str(exc)}]
        run.progress = n
        if n % 10 == 0:
            db.flush()
    for eid, ps in existing.items():
        if eid not in keep and not employee_ids:
            db.delete(ps)
    db.flush()
    finish_calculation(db, company_id, run)
    return run.totals


def _fbp_year_end(db: Session, company_id: int, run: PayRun, e, inp: engine.EmployeeInput, res: dict) -> dict:
    """At the year end (or exit), flexible benefits not claimed are paid as taxable pay."""
    from modules.payroll import fbp
    claims_now = sum((d(r["amount"]) for r in inp.reimbursements if r.get("code") in inp.accrue_codes), ZERO)
    accrued_now = sum((d(v) for v in (res.get("fbp_accrued") or {}).values()), ZERO)
    bal = fbp.year_end_balance(db, company_id, e.id, calc.fy_of(run.period_end), run_id=run.id, accrued_now=accrued_now,
                               claims_now=claims_now)
    if bal <= 0:
        return res
    inp.earnings = [x for x in inp.earnings if x.get("code") != "FBP_BAL"] + \
        [{"code": "FBP_BAL", "amount": bal, "note": "Flexible benefits not claimed this year"}]
    return engine.compute(inp)


def _store(ps: Payslip, res: dict, inp: engine.EmployeeInput, e, bank, entity_cache: dict, db: Session, company) -> None:
    lines = res["earnings"] + res["deductions"] + [x for x in res["employer"]]
    ps.days_in_period, ps.payable_days, ps.lop_days = d(res["days_in_period"]), d(res["payable_days"]), d(res["lop_days"])
    ps.gross, ps.deductions, ps.net = d(res["gross"]), d(res["total_deductions"]), d(res["net"])
    ps.employer_cost = d(res["employer_cost"])
    ps.lines = [{k: v for k, v in ln.items()} for ln in lines]
    ps.statutory = {**res["statutory"], "employer_contributions": res["employer_contributions"],
                    "fbp_accrued": res.get("fbp_accrued") or {}}
    tax = res["tax"] or {}
    t = inp.tax
    month_tax = {}
    if t is not None:
        month_tax = {"taxable": money(t.regular_taxable + t.one_time_taxable), "basic_da": money(t.basic_da),
                     "hra": money(t.hra), "pf": money(t.pf), "pt": money(t.pt), "tds": tax.get("tds", "0.00"),
                     "nps": money(t.employer_nps), "perqs": money(t.perquisites),
                     "regime": t.regime}
    ps.tax = {**tax, "month": month_tax}
    ps.wage_rule = res["wage_rule"]
    ps.arrears = [{**a, "amount": money(a["amount"])} for a in inp.arrears]
    ps.warnings = res["warnings"]
    _store_snapshot(ps, inp, e, bank, entity_cache, db, company, res["full_month_wages"])


def _store_snapshot(ps: Payslip, inp: engine.EmployeeInput, e, bank, entity_cache: dict, db: Session, company,
                    full_month_wages) -> None:
    eid = getattr(e, "legal_entity_id", None)
    if eid not in entity_cache:
        entity_cache[eid] = adapters.legal_entity(db, company, eid)
    ent = entity_cache[eid]
    from core.masters import MasterValue
    bank_name = None
    if bank is not None and bank.bank_id:
        mv = db.query(MasterValue.label, MasterValue.code).filter(MasterValue.id == bank.bank_id).first()
        bank_name = mv[0] if mv else None
    designation = getattr(getattr(e, "designation", None), "designation_name", None) if hasattr(e, "designation") else None
    department = getattr(getattr(e, "department", None), "department_name", None) if hasattr(e, "department") else None
    ps.snapshot = {
        "name": svc.name_of(e), "employee_code": e.employee_code, "email": e.email, "phone": e.phone, "pan": e.pan,
        "uan": e.uan, "esi_number": e.esi_number, "date_of_joining": e.date_of_joining.isoformat() if e.date_of_joining else None,
        "exit_date": e.exit_date.isoformat() if e.exit_date else None, "designation": designation, "department": department,
        "date_of_birth": e.date_of_birth.isoformat() if e.date_of_birth else None,
        "gender": inp.profile.get("gender"), "pt_state": inp.profile.get("pt_state"),
        "bank": {"account_id": bank.id, "name": bank_name, "ifsc": bank.ifsc, "last4": (bank.account_number or "")[-4:],
                 "holder": bank.account_holder} if bank is not None else None,
        "entity": {"id": ent["id"], "name": ent["legal_name"], "pan": ent["pan"], "tan": ent["tan"], "pf_code": ent["pf_code"],
                   "esi_code": ent["esi_code"], "address": ent["address"]},
        "cost_centre": adapters.cost_centre(db, getattr(e, "cost_centre_id", None)),
        "full_month_wages": full_month_wages,
    }


def finish_calculation(db: Session, company_id: int, run: PayRun) -> None:
    from modules.payroll import assistant
    slips = db.query(Payslip).filter(Payslip.pay_run_id == run.id).all()
    run.totals = totals_of(slips)
    # review findings from payslip warnings
    names = {p.employee_id: (p.snapshot or {}).get("name") for p in slips}
    found = [f for f in run.checks or [] if f.get("stage") != "calculated"]
    by_code = defaultdict(list)
    for p in slips:
        for w in p.warnings or []:
            by_code[(w["code"], w.get("level", "warn"))].append(p.employee_id)
    titles = {"below_min_wage": "Below minimum wage", "negative_net": "Negative take-home pay",
              "deductions_over_limit": "Deductions above 50% of wages", "variance": "Big change from last month",
              "wage_rule": "50% wage rule add-back applied", "error": "Could not calculate", "overtime_formula": "Overtime formula"}
    settings = svc.get_settings(db, company_id)
    for (code, level), ids in by_code.items():
        if code == "below_min_wage" and not settings["checks"].get("block_below_min_wage", True):
            level = "warn"
        if code == "negative_net" and not settings["checks"].get("block_negative_net", True):
            level = "warn"
        found.append({"key": f"calc_{code}", "stage": "calculated", "level": level, "title": titles.get(code, code),
                      "message": f"{len(ids)} employee(s).", "employees": [{"id": i, "name": names.get(i)} for i in ids]})
    run.checks = found
    run.assistant = assistant.review(db, company_id, run, slips)
    run.status = "review"
    run.progress = run.progress_total
    _stage(run, "calculated", f"{run.totals.get('employees', 0)} payslips, net ₹{run.totals.get('net', '0')}")
    audit.event(db, "payroll.calculated", "pay_runs", run.id, {"employees": run.totals.get("employees"),
                                                               "net": run.totals.get("net")}, entity_uuid=str(run.uuid))
    db.flush()


def totals_of(slips: list[Payslip]) -> dict:
    t = defaultdict(lambda: ZERO)
    by_code: dict = {}
    count = 0
    for p in slips:
        if p.status in ("skipped", "error"):
            continue
        count += 1
        t["gross"] += d(p.gross)
        t["deductions"] += d(p.deductions)
        t["net"] += d(p.net)
        t["employer_cost"] += d(p.employer_cost)
        if p.status == "held":
            t["held"] += d(p.net)
        for ln in p.lines or []:
            row = by_code.setdefault(ln["code"], {"code": ln["code"], "name": ln["name"], "kind": ln["kind"], "amount": ZERO})
            row["amount"] += d(ln["amount"])
    for code, key in (("PF_EE", "pf_employee"), ("VPF", "vpf"), ("PF_ER", "pf_employer"), ("EPS_ER", "eps"),
                      ("EDLI", "edli"), ("PF_ADMIN", "pf_admin"), ("ESI_EE", "esi_employee"), ("ESI_ER", "esi_employer"),
                      ("PT", "pt"), ("LWF_EE", "lwf_employee"), ("LWF_ER", "lwf_employer"), ("TDS", "tds"),
                      ("GRATUITY", "gratuity_provision")):
        t[key] = by_code.get(code, {}).get("amount", ZERO)
    out = {k: money(v) for k, v in t.items()}
    out["employees"] = count
    out["held_count"] = sum(1 for p in slips if p.status == "held")
    out["error_count"] = sum(1 for p in slips if p.status == "error")
    out["by_component"] = [{**v, "amount": money(v["amount"])} for v in sorted(by_code.values(), key=lambda x: x["code"])]
    return out


# ── Approval, lock, reopen, pay ────────────────────────────────────

def submit_for_approval(db: Session, company_id: int, run: PayRun) -> PayRun:
    if run.status != "review":
        raise BadRequest("Calculate and review the pay run first.")
    open_blocks = blocking(run)
    if open_blocks:
        raise BadRequest(f"Fix or acknowledge first: {', '.join(f['title'] for f in open_blocks)}.")
    if any(p.status == "error" for p in db.query(Payslip).filter(Payslip.pay_run_id == run.id).all()):
        raise BadRequest("Some payslips could not be calculated. Fix them or leave those people out.")
    from core.workflow import service as workflow
    t = run.totals or {}
    inst = workflow.submit(db, company_id, "payroll.run",
                           {"run_uuid": str(run.uuid), "title": run.title, "employees": t.get("employees"),
                            "gross": t.get("gross"), "net": t.get("net"), "employer_cost": t.get("employer_cost"),
                            "amount": float(d(t.get("net"))), "kind": run.kind,
                            "flags": len(run.assistant or [])},
                           f"Approve {run.title}", subject_type="pay_run", subject_id=run.id)
    run.workflow_instance_id = inst.id
    if run.status == "review":  # still review unless the flow approved it at once
        run.status = "pending_approval"
        _stage(run, "submitted")
    db.flush()
    return run


def on_approved(db: Session, instance) -> None:
    run = db.query(PayRun).filter(PayRun.company_id == instance.company_id,
                                  PayRun.uuid == (instance.payload or {}).get("run_uuid")).first()
    if run and run.status in ("pending_approval", "review"):
        run.status = "approved"
        _stage(run, "approved", instance.result_note)
        db.flush()


def on_rejected(db: Session, instance) -> None:
    run = db.query(PayRun).filter(PayRun.company_id == instance.company_id,
                                  PayRun.uuid == (instance.payload or {}).get("run_uuid")).first()
    if run and run.status == "pending_approval":
        run.status = "review"
        _stage(run, "rejected", instance.result_note)
        db.flush()


def lock(db: Session, company_id: int, run: PayRun) -> PayRun:
    if run.status != "approved":
        raise BadRequest("Only an approved pay run can be locked.")
    slips = db.query(Payslip).filter(Payslip.pay_run_id == run.id).all()
    paid_ids = {p.employee_id for p in slips if p.status in ("calculated", "held")}
    refs = defaultdict(list)
    for i in inputs_of(db, company_id, run):
        if i.employee_id in paid_ids and i.source_ref:
            refs[i.source].append(i.source_ref)
    try:
        from modules.loans import service as loans
        loans.mark_deducted(db, company_id, refs["loan"], run.id, run.period_end)
    except ImportError:
        pass
    try:
        from modules.expenses import service as expenses
        expenses.mark_paid_in_run(db, company_id, refs["expense"], run.id, run.pay_date or run.period_end)
    except ImportError:
        pass
    if refs["encashment"]:
        _mark_encashments(db, company_id, refs["encashment"], "paid")
    if refs["fbp"]:
        from modules.payroll import fbp
        fbp.mark_paid(db, company_id, run, paid_ids)
    for s in db.query(EmployeeSalary).filter(EmployeeSalary.company_id == company_id, EmployeeSalary.employee_id.in_(paid_ids or [-1]),
                                             EmployeeSalary.arrears_run_id.is_(None)).all():
        if any(p.employee_id == s.employee_id and p.arrears for p in slips):
            s.arrears_run_id = run.id
    run.status = "locked"
    run.locked_at = _now()
    if run.pay_period_id and run.kind == "regular":
        from modules.payroll.models import PayPeriod
        per = db.query(PayPeriod).filter(PayPeriod.id == run.pay_period_id).first()
        if per:
            per.status = "locked"
    _stage(run, "locked")
    audit.event(db, "payroll.locked", "pay_runs", run.id, {"net": (run.totals or {}).get("net")}, entity_uuid=str(run.uuid),
                entity_label=run.title)
    db.flush()
    try:
        from modules.payroll import compliance
        compliance.refresh_for_run(db, company_id, run)
    except Exception as exc:
        log.warning("Compliance calendar refresh failed: %s", exc)
    return run


def _mark_encashments(db: Session, company_id: int, refs: list[str], status: str) -> None:
    try:
        from modules.leave.models import LeaveLedgerEntry
    except ImportError:
        return
    for e in db.query(LeaveLedgerEntry).filter(LeaveLedgerEntry.company_id == company_id, LeaveLedgerEntry.uuid.in_(refs)).all():
        e.meta = {**(e.meta or {}), "payroll_status": status}


def reopen(db: Session, company_id: int, run: PayRun, reason: str) -> PayRun:
    if run.status not in ("approved", "locked", "pending_approval"):
        raise BadRequest("Only an approved or locked pay run can be reopened.")
    if not (reason or "").strip():
        raise BadRequest("Give a reason for reopening.")
    if run.status == "paid" or run.paid_at:
        raise BadRequest("This pay run has been paid; make a correction run instead.")
    if run.status == "locked":
        later = db.query(PayRun).filter(PayRun.company_id == company_id, PayRun.kind == "regular",
                                        PayRun.pay_group_id == run.pay_group_id, PayRun.period_start > run.period_start,
                                        PayRun.status != "cancelled").first()
        if later and run.kind == "regular":
            raise BadRequest("A later month has a pay run already; make a correction run instead.")
        try:
            from modules.loans import service as loans
            loans.unmark_run(db, company_id, run.id)
        except ImportError:
            pass
        try:
            from modules.expenses import service as expenses
            expenses.unmark_run(db, company_id, run.id)
        except ImportError:
            pass
        from modules.payroll import fbp
        fbp.unmark_run(db, company_id, run)
        enc = [i.source_ref for i in inputs_of(db, company_id, run) if i.source == "encashment" and i.source_ref]
        if enc:
            _mark_encashments(db, company_id, enc, "pending")
        for s in db.query(EmployeeSalary).filter(EmployeeSalary.arrears_run_id == run.id).all():
            s.arrears_run_id = None
    if run.workflow_instance_id and run.status == "pending_approval":
        from core.workflow import service as workflow
        from core.workflow.models import WorkflowInstance
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == run.workflow_instance_id).first()
        if inst and inst.status == "pending":
            workflow.cancel(db, company_id, inst, "Pay run reopened")
    run.status = "review"
    run.locked_at = None
    _stage(run, "reopened", reason.strip())
    audit.event(db, "payroll.reopened", "pay_runs", run.id, {"reason": reason.strip()}, entity_uuid=str(run.uuid),
                entity_label=run.title)
    db.flush()
    return run


def mark_paid(db: Session, company_id: int, run: PayRun, *, reference: str | None = None,
              failed_employee_ids: list[int] | None = None) -> PayRun:
    if run.status not in ("locked", "paid"):
        raise BadRequest("Lock the pay run before marking it paid.")
    failed = set(failed_employee_ids or [])
    for p in db.query(Payslip).filter(Payslip.pay_run_id == run.id).all():
        if p.status == "calculated" or (p.status == "held" and p.released_at):
            p.payment_status = "failed" if p.employee_id in failed else "paid"
            p.payment_ref = reference
    run.status = "paid"
    run.paid_at = _now()
    _stage(run, "paid", reference)
    audit.event(db, "payroll.paid", "pay_runs", run.id, {"reference": reference, "failed": len(failed)},
                entity_uuid=str(run.uuid))
    db.flush()
    try:
        from modules.payroll import compliance
        compliance.mark_salary_paid(db, company_id, run)
    except Exception:
        pass
    if run.kind == "settlement":
        from modules.payroll import settlement
        settlement.on_run_paid(db, company_id, run, reference=reference, failed=bool(failed))
    return run


def hold(db: Session, company_id: int, ps: Payslip, reason: str) -> None:
    if ps.payment_status == "paid":
        raise BadRequest("This salary has already been paid.")
    ps.status, ps.hold_reason, ps.released_at = "held", reason or "On hold", None
    audit.event(db, "payroll.salary_held", "payslips", ps.id, {"reason": reason}, entity_uuid=str(ps.uuid))


def release(db: Session, company_id: int, ps: Payslip, note: str | None = None) -> None:
    if ps.status != "held":
        raise BadRequest("This salary is not on hold.")
    ps.released_at = _now()
    run = db.query(PayRun).filter(PayRun.id == ps.pay_run_id).first()
    if run and run.status in EDITABLE + ("pending_approval", "approved"):
        ps.status, ps.hold_reason, ps.released_at = "calculated", None, None
    audit.event(db, "payroll.salary_released", "payslips", ps.id, {"note": note}, entity_uuid=str(ps.uuid))


def cancel_run(db: Session, company_id: int, run: PayRun) -> None:
    if run.status in ("locked", "paid", "approved", "pending_approval"):
        raise BadRequest("Reopen the pay run before cancelling it.")
    run.status = "cancelled"
    _stage(run, "cancelled")
    if run.kind == "regular" and run.pay_period_id:
        from modules.payroll.models import PayPeriod
        per = db.query(PayPeriod).filter(PayPeriod.id == run.pay_period_id).first()
        if per:
            per.status = "upcoming"
    audit.event(db, "payroll.run_cancelled", "pay_runs", run.id, {}, entity_uuid=str(run.uuid))
    db.flush()


# ── Review ──────────────────────────────────────────────────────────

def payslip_row(p: Payslip) -> dict:
    s = p.snapshot or {}
    st = p.statutory or {}
    return {"uuid": str(p.uuid), "employee_id": p.employee_id, "employee_name": s.get("name"),
            "employee_code": s.get("employee_code"), "department": s.get("department"), "status": p.status,
            "payable_days": str(p.payable_days), "lop_days": str(p.lop_days), "days_in_period": str(p.days_in_period),
            "gross": money(p.gross), "deductions": money(p.deductions), "net": money(p.net),
            "employer_cost": money(p.employer_cost), "pf": (st.get("pf") or {}).get("employee"),
            "esi": (st.get("esi") or {}).get("employee"), "pt": (st.get("pt") or {}).get("amount"),
            "tds": (p.tax or {}).get("tds"), "warnings": p.warnings or [], "hold_reason": p.hold_reason,
            "payment_status": p.payment_status, "published": p.published, "arrears": p.arrears or [],
            "period_start": p.period_start.isoformat(), "period_end": p.period_end.isoformat()}


def variance(db: Session, company_id: int, run: PayRun) -> dict:
    """This run against the previous regular run: per person and per component."""
    prev = (db.query(PayRun).filter(PayRun.company_id == company_id, PayRun.kind == "regular",
                                    PayRun.pay_group_id == run.pay_group_id, PayRun.period_start < run.period_start,
                                    PayRun.status.in_(("approved", "locked", "paid")))
            .order_by(PayRun.period_start.desc()).first())
    cur = {p.employee_id: p for p in db.query(Payslip).filter(Payslip.pay_run_id == run.id).all()}
    old = {p.employee_id: p for p in db.query(Payslip).filter(Payslip.pay_run_id == prev.id).all()} if prev else {}
    people = []
    for eid in set(cur) | set(old):
        a, b = old.get(eid), cur.get(eid)
        na, nb = d(a.net) if a else ZERO, d(b.net) if b else ZERO
        ga, gb = d(a.gross) if a else ZERO, d(b.gross) if b else ZERO
        pct = ((nb - na) / na * 100).quantize(d("0.1")) if na else None
        people.append({"employee_id": eid, "employee_name": ((b or a).snapshot or {}).get("name"),
                       "previous_net": money(na), "net": money(nb), "change": money(nb - na), "change_percent": str(pct) if pct is not None else None,
                       "previous_gross": money(ga), "gross": money(gb),
                       "note": "Joined" if not a else "Not in this run" if not b else None})
    people.sort(key=lambda x: -abs(d(x["change"])))
    comps = defaultdict(lambda: [ZERO, ZERO, "", ""])
    for idx, src in ((0, old), (1, cur)):
        for p in src.values():
            for ln in p.lines or []:
                comps[ln["code"]][idx] += d(ln["amount"])
                comps[ln["code"]][2] = ln["name"]
                comps[ln["code"]][3] = ln["kind"]
    components = [{"code": k, "name": v[2], "kind": v[3], "previous": money(v[0]), "current": money(v[1]),
                   "change": money(v[1] - v[0])} for k, v in sorted(comps.items())]
    return {"previous_run": {"uuid": str(prev.uuid), "title": prev.title} if prev else None, "people": people,
            "components": components, "previous_totals": prev.totals if prev else None, "totals": run.totals}


def register_rows(db: Session, company_id: int, run: PayRun) -> tuple[list[str], list[list]]:
    """The full payroll sheet: one row per person, one column per component."""
    slips = db.query(Payslip).filter(Payslip.pay_run_id == run.id).order_by(Payslip.id).all()
    codes: list[tuple[str, str, str]] = []
    seen = set()
    for p in slips:
        for ln in p.lines or []:
            if ln["code"] not in seen:
                seen.add(ln["code"])
                codes.append((ln["code"], ln["name"], ln["kind"]))
    order = {"earning": 0, "reimbursement": 1, "deduction": 2, "employer": 3}
    codes.sort(key=lambda c: (order.get(c[2], 9), c[0]))
    header = ["Employee ID", "Name", "Department", "Status", "Days", "Paid days", "LOP"] + [c[1] for c in codes] + \
             ["Gross", "Deductions", "Net pay", "Employer cost"]
    rows = []
    for p in slips:
        s = p.snapshot or {}
        amounts = defaultdict(lambda: ZERO)
        for ln in p.lines or []:
            amounts[ln["code"]] += d(ln["amount"])
        rows.append([s.get("employee_code"), s.get("name"), s.get("department"), p.status, str(p.days_in_period),
                     str(p.payable_days), str(p.lop_days)] + [money(amounts[c[0]]) for c in codes] +
                    [money(p.gross), money(p.deductions), money(p.net), money(p.employer_cost)])
    return header, rows


def employee_ids_named(db: Session, company_id: int, ids: list[int]) -> dict[int, str]:
    Employee = svc.employee_model()
    return {e.id: svc.name_of(e) for e in db.query(Employee).filter(Employee.company_id == company_id,
                                                                     Employee.id.in_(ids or [-1])).all()}
