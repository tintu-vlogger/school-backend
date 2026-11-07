"""Full and final settlement.

compute(...) builds one statement for an exit:
  earnings    salary up to the last day (with its PF/ESI/PT), leave encashment, gratuity,
              statutory bonus, notice pay (when the company ends employment without notice),
              retrenchment compensation (15 days per year) and the re-skilling fund (15 days),
              pending reimbursements, anything HR adds
  deductions  notice shortfall recovery, loans, unsettled advances, asset recovery, PF/ESI/PT,
              anything HR adds, and income tax on all of it
The Code on Wages requires payment within 2 working days of the last working day; the due
date is counted on the employee's work calendar, shown as a countdown and alerted on.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest, NotFound
from core.timeutil import today_for_company_id
from modules.payroll import adapters, calc, engine, statutory, taxcalc
from modules.payroll import runs as runsvc
from modules.payroll import service as svc
from modules.payroll.calc import ZERO, d, money, rnd
from modules.payroll.models import FinalSettlement, PayRun, Payslip

EXIT_KINDS = {"resignation": "Resignation", "termination": "Termination", "retrenchment": "Retrenchment",
              "layoff": "Lay-off", "closure": "Closure of business", "retirement": "Retirement", "death": "Death",
              "contract_end": "End of contract", "absconding": "Absconding"}
STATUS = {"draft": "Draft", "pending_approval": "Waiting for approval", "approved": "Approved, to be paid",
          "paid": "Paid", "cancelled": "Cancelled"}


def get(db: Session, company_id: int, uuid) -> FinalSettlement:
    s = db.query(FinalSettlement).filter(FinalSettlement.company_id == company_id, FinalSettlement.uuid == uuid).first()
    if not s:
        raise NotFound("Final settlement")
    return s


def due_date_for(db: Session, employee, last_day: date, rules: dict) -> date:
    n = int(rules.get("settlement_working_days", 2))
    return calc.add_working_days(last_day, n, adapters.is_working_day_fn(db, employee))


def create(db: Session, company_id: int, employee, data: dict) -> FinalSettlement:
    if db.query(FinalSettlement).filter(FinalSettlement.company_id == company_id, FinalSettlement.employee_id == employee.id,
                                        FinalSettlement.status != "cancelled").first():
        raise BadRequest("This employee already has a final settlement.")
    lwd = data.get("last_working_day") or employee.exit_date
    if not lwd:
        raise BadRequest("Enter the last working day.")
    kind = data.get("exit_kind") or "resignation"
    if kind not in EXIT_KINDS:
        raise BadRequest("Choose how the employee is leaving.")
    book = statutory.RuleBook(db, company_id, lwd)
    s = FinalSettlement(company_id=company_id, employee_id=employee.id, exit_kind=kind, last_working_day=lwd,
                        resignation_date=data.get("resignation_date") or employee.resignation_date,
                        due_date=due_date_for(db, employee, lwd, book.get("wage_rule") or {}), status="draft",
                        inputs=data.get("inputs") or {}, created_by_name=svc.actor())
    from core import numbering
    s.number = numbering.next_number(db, company_id, "settlement", lwd) or None
    db.add(s)
    db.flush()
    recompute(db, company_id, s)
    audit.event(db, "payroll.settlement_created", "final_settlements", s.id,
                {"employee_id": employee.id, "last_working_day": lwd.isoformat(), "due": s.due_date.isoformat()},
                entity_uuid=str(s.uuid), entity_label=svc.name_of(employee))
    return s


def update(db: Session, company_id: int, s: FinalSettlement, data: dict) -> FinalSettlement:
    if s.status not in ("draft",):
        raise BadRequest("Only a draft settlement can be changed.")
    emp = svc.get_employee(db, company_id, s.employee_id)
    if data.get("exit_kind"):
        if data["exit_kind"] not in EXIT_KINDS:
            raise BadRequest("Choose how the employee is leaving.")
        s.exit_kind = data["exit_kind"]
    if data.get("last_working_day"):
        s.last_working_day = data["last_working_day"]
        book = statutory.RuleBook(db, company_id, s.last_working_day)
        s.due_date = due_date_for(db, emp, s.last_working_day, book.get("wage_rule") or {})
    if "resignation_date" in data:
        s.resignation_date = data["resignation_date"]
    if "inputs" in data:
        s.inputs = {**(s.inputs or {}), **(data["inputs"] or {})}
    recompute(db, company_id, s)
    return s


def _paid_through(db: Session, company_id: int, employee_id: int) -> date | None:
    row = (db.query(Payslip.period_end).join(PayRun, PayRun.id == Payslip.pay_run_id)
           .filter(Payslip.company_id == company_id, Payslip.employee_id == employee_id, PayRun.kind == "regular",
                   PayRun.status.in_(("approved", "locked", "paid")), Payslip.status != "skipped")
           .order_by(Payslip.period_end.desc()).first())
    return row[0] if row else None


def recompute(db: Session, company_id: int, s: FinalSettlement) -> FinalSettlement:
    emp = svc.get_employee(db, company_id, s.employee_id)
    lwd = s.last_working_day
    book = statutory.RuleBook(db, company_id, lwd)
    settings = svc.get_settings(db, company_id)
    st = settings.get("settlement", {})
    inp = s.inputs or {}
    comps = runsvc.comp_attrs(db, company_id)
    salary = svc.current_salary(db, company_id, emp.id, lwd)
    full = {c["code"]: d(c["monthly"]) for c in (salary.components if salary else [])}
    gross_m = d(salary.monthly_gross) if salary else ZERO
    basic_da = full.get("BASIC", ZERO) + full.get("DA", ZERO)
    core = basic_da
    excl = sum((v for k, v in full.items() if comps.get(k, {}).get("wage_class") == "exclusion"
                and comps.get(k, {}).get("kind") == "earning"), ZERO)
    wr = calc.wage_rule(core, excl, (book.get("wage_rule") or {}).get("min_wage_share_percent", 50))
    wages = d(wr["wages"])  # Code on Wages "wages" (with the 50% add-back) for gratuity, bonus, retrenchment
    lines: list[dict] = []
    workings: dict = {"wages": money(wages), "monthly_gross": money(gross_m), "wage_rule": wr}

    def add(code, label, kind, amount, *, taxable=None, detail=None, ref=None):
        amount = rnd(amount)
        if amount == 0 and not detail:
            return
        lines.append({"code": code, "label": label, "kind": kind, "amount": money(amount),
                      "taxable": money(amount if taxable is None else taxable), "detail": detail, "ref": ref})

    # 1. salary up to the last working day (months not yet paid by a regular pay run)
    paid_through = _paid_through(db, company_id, emp.id)
    start = (paid_through + timedelta(days=1)) if paid_through else (emp.date_of_joining or lwd.replace(day=1))
    start = max(start, lwd.replace(day=1)) if paid_through is None else start
    salary_tax = ZERO
    stat_deductions: dict[str, Decimal] = {}
    stat_names: dict[str, str] = {}
    employer_lines: dict[str, dict] = {}
    fbp_accrued = ZERO
    accrue = runsvc.fbp_accrue_codes(db, company_id)
    salary_basic_da = ZERO
    month = start.replace(day=1)
    lop_total = d(inp.get("lop_days") or 0)
    while start <= lwd and month <= lwd:
        p_start, p_end = month, calc.month_end(month)
        sal = svc.salaries_in(db, company_id, [emp.id], p_start, p_end).get(emp.id) or []
        if sal:
            lop = adapters.leave_lop(db, company_id, max(p_start, start), min(p_end, lwd), [emp.id]).get(emp.id, {}).get("lop_days", ZERO)
            if month == lwd.replace(day=1):
                lop += lop_total
            profile = svc.profile_for(db, company_id, emp)
            age = calc.age_on(emp.date_of_birth, lwd)
            company = svc._company(db, company_id)
            state = adapters.employee_state(db, emp, company, settings)
            pt_state = profile.pt_state or state
            ei = engine.EmployeeInput(
                employee_id=emp.id, period_start=p_start, period_end=p_end,
                segments=runsvc.segments_for(sal, p_start, p_end), comps=comps,
                rules={"epf": book.need("epf"), "esi": book.need("esi"), "gratuity": None,
                       "wage_rule": book.get("wage_rule") or {}},
                settings=settings, join_date=max(emp.date_of_joining or p_start, start) if paid_through else emp.date_of_joining,
                exit_date=lwd, lop_days=d(lop),
                profile={"pf_applicable": profile.pf_applicable, "pf_employee_basis": profile.pf_employee_basis,
                         "pf_employer_basis": profile.pf_employer_basis,
                         "eps_eligible": profile.eps_applicable and calc.eps_eligible(age, book.need("epf")),
                         "vpf_rate": profile.vpf_rate, "vpf_amount": profile.vpf_amount, "esi_applicable": profile.esi_applicable,
                         "pt_applicable": profile.pt_applicable, "lwf_applicable": False, "gender": svc.gender_of(db, emp),
                         "pt_state": pt_state},
                esi_covered=calc.esi_covered(sum((v for k, v in full.items() if comps.get(k, {}).get("esi_wage")), ZERO),
                                             book.need("esi"), disabled=profile.disability) and profile.esi_applicable,
                pt_rules=book.get("pt", pt_state) if pt_state else None, accrue_codes=accrue)
            res = engine.compute(ei)
            earned = d(res["gross"])
            add("SALARY", f"Salary {p_start:%b %Y} ({res['payable_days']} days)", "earning", earned,
                detail={"days": res["payable_days"], "lop": res["lop_days"], "days_in_period": res["days_in_period"],
                        "earnings": [{"code": x["code"], "name": x["name"], "amount": x["amount"]} for x in res["earnings"]]})
            salary_tax += earned
            salary_basic_da += sum((d(x["amount"]) for x in res["earnings"] if x["code"] in ("BASIC", "DA")), ZERO)
            for x in res["deductions"]:
                stat_deductions[x["code"]] = stat_deductions.get(x["code"], ZERO) + d(x["amount"])
                stat_names[x["code"]] = x["name"]
            for x in res["employer"]:
                row = employer_lines.setdefault(x["code"], {**x, "amount": ZERO})
                row["amount"] += d(x["amount"])
            fbp_accrued += sum((d(v) for v in (res.get("fbp_accrued") or {}).values()), ZERO)
            workings.setdefault("salary", []).append({"month": p_start.isoformat(), "statutory": res["statutory"],
                                                      "payable_days": res["payable_days"], "lop_days": res["lop_days"],
                                                      "days_in_period": res["days_in_period"],
                                                      "fbp_accrued": res.get("fbp_accrued") or {}})
        month = calc.add_months(month, 1)

    # 2. leave encashment
    enc_days = adapters.encashable_leave(db, company_id, emp, lwd)
    if inp.get("leave_days") is not None:
        enc_total_days = d(inp["leave_days"])
    else:
        enc_total_days = sum((x["days"] for x in enc_days), ZERO)
    base = gross_m if st.get("leave_encash_basis") == "gross" else basic_da
    enc_amount = rnd(base / d(st.get("leave_encash_divisor", 26)) * enc_total_days) if enc_total_days > 0 else ZERO
    enc_rules = book.get("leave_encashment") or {}
    enc_exempt = min(enc_amount, d(enc_rules.get("tax_exempt_limit", 0))) if s.exit_kind in ("retirement", "resignation", "contract_end") else ZERO
    if enc_amount:
        add("LEAVE_ENC", f"Leave encashment ({enc_total_days} days)", "earning", enc_amount, taxable=enc_amount - enc_exempt,
            detail={"days": [{"code": x["code"], "days": str(x["days"])} for x in enc_days], "per_day": money(base / d(st.get("leave_encash_divisor", 26)))})

    # 3. gratuity
    grat_rules = book.get("gratuity")
    if grat_rules and emp.date_of_joining:
        g = calc.gratuity(wages, emp.date_of_joining, lwd, grat_rules, fixed_term=svc.is_fixed_term(db, emp),
                          death_or_disablement=s.exit_kind == "death" or bool(inp.get("disablement")))
        workings["gratuity"] = g
        if g["eligible"] and not inp.get("skip_gratuity"):
            add("GRATUITY_PAY", f"Gratuity ({g['years_counted']} years)", "earning", d(g["amount"]), taxable=d(g["taxable"]),
                detail=g)

    # 4. statutory bonus for the accounting year so far
    bonus_rules = book.get("bonus")
    if bonus_rules and st.get("pay_bonus_on_exit", True) and not inp.get("skip_bonus"):
        fy_start = calc.fy_bounds(calc.fy_of(lwd))[0]
        months = []
        m = max(fy_start, (emp.date_of_joining or fy_start).replace(day=1))
        while m <= lwd:
            months.append({"wage": wages, "days_worked": min(30, (min(calc.month_end(m), lwd) - m).days + 1)})
            m = calc.add_months(m, 1)
        paid_bonus = sum((d(ln["amount"]) for p in db.query(Payslip).filter(Payslip.company_id == company_id, Payslip.employee_id == emp.id,
                                                                             Payslip.fy == calc.fy_of(lwd)).all()
                          for ln in p.lines or [] if ln["code"] == "BONUS"), ZERO)
        mw = statutory.minimum_wage(book, svc.profile_for(db, company_id, emp).min_wage_state, None, None)
        b = calc.bonus(months, bonus_rules, rate=inp.get("bonus_rate") or settings.get("bonus_rate", 8.33),
                       min_wage_monthly=mw["monthly"] if mw else None)
        workings["bonus"] = b
        if b["eligible"] and d(b["amount"]) - paid_bonus > 0:
            add("BONUS", f"Statutory bonus at {b['rate']}%", "earning", d(b["amount"]) - paid_bonus, detail=b)

    # 5. notice
    required = int(inp.get("notice_required_days") if inp.get("notice_required_days") is not None else (emp.notice_period_days or 30))
    served = 0
    if s.resignation_date:
        served = max(0, (lwd - s.resignation_date).days + 1)
    per_day = (gross_m if st.get("notice_basis") == "gross" else basic_da) / d(st.get("notice_divisor", 30))
    notice = {"required_days": required, "served_days": served, "waived": bool(inp.get("waive_notice")),
              "per_day": money(per_day)}
    if s.exit_kind == "resignation" and not inp.get("waive_notice"):
        short = max(required - served, 0) if s.resignation_date else 0
        if inp.get("notice_shortfall_days") is not None:
            short = int(inp["notice_shortfall_days"])
        notice["shortfall_days"] = short
        if short:
            add("NOTICE_REC", f"Notice pay recovery ({short} days)", "deduction", per_day * short)
    elif s.exit_kind in ("termination", "retrenchment", "layoff", "closure"):
        pay_days = int(inp.get("notice_pay_days") if inp.get("notice_pay_days") is not None else
                       (0 if s.resignation_date else required))
        notice["pay_days"] = pay_days
        if pay_days:
            add("NOTICE_PAY", f"Notice pay ({pay_days} days)", "earning", per_day * pay_days)
    s.notice = notice

    # 6. retrenchment compensation and re-skilling fund
    if s.exit_kind in ("retrenchment", "closure") and emp.date_of_joining:
        rr = book.get("retrenchment")
        if rr:
            r = calc.retrenchment(wages, emp.date_of_joining, lwd, rr)
            workings["retrenchment"] = r
            exempt = min(d(r["compensation"]), d(rr.get("tax_exempt_limit", 0)))
            add("RETRENCH", f"Retrenchment compensation ({r['years_counted']} years × 15 days)", "earning",
                d(r["compensation"]), taxable=d(r["compensation"]) - exempt)
            if s.exit_kind == "retrenchment":
                add("RESKILL", "Re-skilling fund (15 days' wages)", "earning", d(r["reskilling_fund"]), taxable=ZERO,
                    detail={"note": "Paid into the worker re-skilling fund for the employee"})

    # 7. pending reimbursements
    try:
        from modules.expenses import service as expenses
        for c in expenses.pending_for(db, company_id, emp.id):
            add("REIMB", f"Reimbursement {c['label']}", "earning", c["amount"], taxable=c["taxable"], ref=c["ref"])
        for a in expenses.advance_balance(db, company_id, emp.id):
            add("ADVANCE_REC", f"Unsettled advance {a['label']}", "deduction", a["amount"], ref=a["ref"])
    except ImportError:
        pass

    # 8. loans
    try:
        from modules.loans import service as loans
        for ln in loans.outstanding_for(db, company_id, emp.id):
            add("LOAN", f"{ln['name']} balance {ln['number'] or ''}".strip(), "deduction", ln["balance"], ref=ln["uuid"])
    except ImportError:
        pass

    # 8b. flexible benefits: approved bills not yet paid, and the unclaimed balance (taxable)
    try:
        from modules.payroll import fbp
        for x in fbp.settlement_lines(db, company_id, emp, lwd, fbp_accrued):
            add(x["code"], x["label"], "earning", x["amount"], taxable=x["taxable"], ref=x["ref"],
                detail={"fbp": True})
    except ImportError:
        pass

    # 9. assets
    for a in adapters.asset_recoveries(db, company_id, emp.id):
        if a.get("pending_return"):
            workings.setdefault("assets_not_returned", []).append(a["label"])
            continue
        add("ASSET", f"Asset recovery: {a['label']}", "deduction", a["amount"], ref=a["ref"])

    # 10. statutory deductions on the final salary
    for code, amt in stat_deductions.items():
        add(code, stat_names.get(code, code), "deduction", amt, detail={"statutory": True})
    workings["employer"] = [{**x, "amount": money(x["amount"])} for x in employer_lines.values()]
    workings["fbp_accrued"] = money(fbp_accrued)

    # 11. HR additions
    for x in inp.get("extra_earnings") or []:
        add("EXTRA", x.get("label") or "Other earning", "earning", d(x.get("amount")),
            taxable=d(x.get("amount")) if x.get("taxable", True) else ZERO)
    for x in inp.get("extra_deductions") or []:
        add("EXTRA_DED", x.get("label") or "Other deduction", "deduction", d(x.get("amount")))

    # 12. income tax on all of it
    fy = calc.fy_of(lwd)
    earnings_taxable = sum((d(x["taxable"]) for x in lines if x["kind"] == "earning" and x["code"] != "RESKILL"), ZERO)
    tax = _tax(db, company_id, emp, s, book, fy, earnings_taxable, salary_basic_da, stat_deductions, settings)
    workings["tax"] = tax
    if d(tax["tds"]) > 0:
        add("TDS", "Income tax (TDS)", "deduction", d(tax["tds"]))

    s.lines = lines
    s.total_earnings = sum((d(x["amount"]) for x in lines if x["kind"] == "earning" and x["code"] != "RESKILL"), ZERO)
    s.total_deductions = sum((d(x["amount"]) for x in lines if x["kind"] == "deduction"), ZERO)
    s.tax = d(tax["tds"])
    s.net = s.total_earnings - s.total_deductions
    s.workings = workings
    db.flush()
    return s


def _tax(db: Session, company_id: int, emp, s: FinalSettlement, book, fy: str, taxable_now: Decimal, basic_da_now: Decimal,
         stat: dict, settings: dict) -> dict:
    run_stub = PayRun(id=-1, period_start=s.last_working_day, period_end=s.last_working_day)
    ytd = runsvc._ytd(db, company_id, emp.id, fy, run_stub)
    decl = runsvc._declaration(db, company_id, emp.id, fy)
    regime = decl.regime if decl else settings["tax"].get("default_regime", "new")
    dec = runsvc.declaration_amounts(decl)
    pf_now = sum((v for k, v in stat.items() if k in ("PF_EE", "VPF")), ZERO)
    pt_now = sum((v for k, v in stat.items() if k == "PT"), ZERO)
    t = taxcalc.TaxInputs(
        regime=regime, regime_rules=book.need("income_tax", regime), sections=book.need("tax_sections"),
        age=calc.age_on(emp.date_of_birth, s.last_working_day), has_pan=bool(emp.pan and runsvc.PAN_RE.match(emp.pan.upper())),
        months_left=1, ytd_taxable=ytd["taxable"], ytd_basic_da=ytd["basic_da"], ytd_hra=ytd["hra"], ytd_pf=ytd["pf"],
        ytd_pt=ytd["pt"], ytd_tds=ytd["tds"], ytd_employer_nps=ytd["nps"], ytd_perquisites=ytd["perqs"],
        one_time_taxable=taxable_now, basic_da=basic_da_now, pf=pf_now, pt=pt_now,
        deductions=dec["deductions"], rent_paid=dec["rent"], lta_exempt=dec["lta"], previous_income=d(dec["prev"].get("income")),
        previous_tds=d(dec["prev"].get("tds")), previous_pf=d(dec["prev"].get("pf")), previous_pt=d(dec["prev"].get("pt")),
        other_income=dec["other_income"])
    return taxcalc.monthly_tds(t)


def submit(db: Session, company_id: int, s: FinalSettlement) -> FinalSettlement:
    if s.status != "draft":
        raise BadRequest("This settlement has already been sent for approval.")
    recompute(db, company_id, s)
    emp = svc.get_employee(db, company_id, s.employee_id)
    from core.workflow import service as workflow
    inst = workflow.submit(db, company_id, "payroll.settlement",
                           {"settlement_uuid": str(s.uuid), "employee_name": svc.name_of(emp), "net": money(s.net),
                            "amount": float(s.net), "exit_kind": s.exit_kind, "due_date": s.due_date.isoformat(),
                            "last_working_day": s.last_working_day.isoformat()},
                           f"Final settlement: {svc.name_of(emp)}", subject_type="final_settlement", subject_id=s.id)
    s.workflow_instance_id = inst.id
    if s.status == "draft":
        s.status = "pending_approval"
    db.flush()
    return s


def _from(db: Session, instance) -> FinalSettlement | None:
    return db.query(FinalSettlement).filter(FinalSettlement.company_id == instance.company_id,
                                            FinalSettlement.uuid == (instance.payload or {}).get("settlement_uuid")).first()


def on_approved(db: Session, instance) -> None:
    s = _from(db, instance)
    if s and s.status in ("pending_approval", "draft"):
        s.status = "approved"
        s.approved_at = instance.completed_at
        make_run(db, instance.company_id, s, approved=True)


def on_rejected(db: Session, instance) -> None:
    s = _from(db, instance)
    if s and s.status == "pending_approval":
        s.status = "draft"


# ── The off-cycle pay run ───────────────────────────────────────────

def run_of(db: Session, company_id: int, s: FinalSettlement) -> PayRun | None:
    if not s.pay_run_id:
        return None
    return db.query(PayRun).filter(PayRun.company_id == company_id, PayRun.id == s.pay_run_id).first()


def make_run(db: Session, company_id: int, s: FinalSettlement, *, approved: bool = False) -> PayRun:
    """An off-cycle pay run for this one person, worked out from the settlement. The
    settlement's approval approves the run; it is then locked, paid (bank file, payslip-style
    statement) and filed like any other run. Its pay date is the legal deadline."""
    run = run_of(db, company_id, s)
    if run is not None and run.status != "cancelled":
        return run
    emp = svc.get_employee(db, company_id, s.employee_id)
    run = runsvc.create_run(db, company_id, month=s.last_working_day.replace(day=1), group=None, kind="settlement",
                            title=f"Final settlement · {svc.name_of(emp)}", employee_ids=[emp.id],
                            options={"settlement_uuid": str(s.uuid)}, pay_date=s.due_date)
    s.pay_run_id = run.id
    db.flush()
    runsvc.calculate(db, company_id, run)
    if approved:
        run.status = "approved"
        runsvc._stage(run, "approved", f"Approved with the final settlement {s.number or ''}".strip())
    db.flush()
    return run


def salary_covered(db: Session, company_id: int, start: date, end: date) -> set[int]:
    """Employees whose settlement (not cancelled, not a draft) pays salary for days in this
    period, so a regular run must leave them out."""
    out = set()
    for s in db.query(FinalSettlement).filter(FinalSettlement.company_id == company_id,
                                              FinalSettlement.status.in_(("pending_approval", "approved", "paid")),
                                              FinalSettlement.last_working_day >= start).all():
        if not any(x.get("code") == "SALARY" for x in s.lines or []):
            continue
        for m in (s.workings or {}).get("salary") or []:
            if start.replace(day=1) <= date.fromisoformat(m["month"]) <= end:
                out.add(s.employee_id)
    return out


def fill_payslip(db: Session, company_id: int, run: PayRun, ps: Payslip, emp, bank, entity_cache: dict, company) -> None:
    """The settlement as a payslip: salary components of the final month(s), each settlement
    line, statutory deductions and employer contributions, the TDS worksheet and the
    snapshot (bank, entity) that files and reports read."""
    s = db.query(FinalSettlement).filter(FinalSettlement.company_id == company_id,
                                         FinalSettlement.uuid == (run.options or {}).get("settlement_uuid")).first()
    if s is None:
        raise BadRequest("The settlement for this run is missing.")
    comps = runsvc.comp_attrs(db, company_id)
    lines: list[dict] = []
    by_code: dict[str, dict] = {}

    def put(code, name, kind, amount, note=None, extra=None):
        amount = d(amount)
        if amount == 0:
            return
        key = (code, kind)
        if key in by_code:
            by_code[key]["amount"] = money(d(by_code[key]["amount"]) + amount)
            return
        c = comps.get(code) or {}
        row = {"code": code, "name": c.get("name") or name, "name_hi": c.get("name_hi"), "kind": kind,
               "amount": money(amount), "note": note, **(extra or {})}
        by_code[key] = row
        lines.append(row)

    for x in s.lines or []:
        if x["code"] == "SALARY":
            for ern in (x.get("detail") or {}).get("earnings") or []:
                put(ern.get("code") or "SALARY", ern["name"], "earning", ern["amount"])
        elif x["code"] == "RESKILL":
            put("RESKILL", x["label"], "employer", x["amount"], note="Paid into the re-skilling fund")
        elif x["kind"] == "earning":
            kind = "reimbursement" if x["code"] in ("REIMB",) or (x.get("detail") or {}).get("fbp") and x["code"] != "FBP_BAL" else "earning"
            put(x["code"], x["label"], kind, x["amount"], note=x["label"] if x["code"] in ("EXTRA", "REIMB") else None)
        else:
            put(x["code"], x["label"], "deduction", x["amount"],
                note=x["label"] if x["code"] in ("EXTRA_DED", "ASSET", "ADVANCE_REC", "LOAN") else None)
    w = s.workings or {}
    for x in w.get("employer") or []:
        put(x["code"], x["name"], "employer", x["amount"], extra={"show": x.get("show", True)})
    months = w.get("salary") or []
    # statutory details summed over the months the settlement pays salary for
    stat: dict = {}
    for m in months:
        for k, v in (m.get("statutory") or {}).items():
            if isinstance(v, dict):
                tgt = stat.setdefault(k, {})
                for kk, vv in v.items():
                    if isinstance(vv, (int, float, str)) and not isinstance(vv, bool) and kk not in ("state",):
                        try:
                            tgt[kk] = money(d(tgt.get(kk, 0)) + d(vv))
                            continue
                        except Exception:
                            pass
                    tgt[kk] = vv
            else:
                stat[k] = v
    employer_total = sum((d(x["amount"]) for x in lines if x["kind"] == "employer" and x["code"] != "RESKILL"), ZERO)
    stat["employer_contributions"] = money(employer_total)
    stat["fbp_accrued"] = {}
    stat["settlement"] = {"uuid": str(s.uuid), "number": s.number, "due_date": s.due_date.isoformat()}
    last = months[-1] if months else {}
    ps.days_in_period = d(last.get("days_in_period") or 0)
    ps.payable_days = sum((d(m.get("payable_days")) for m in months), ZERO)
    ps.lop_days = sum((d(m.get("lop_days")) for m in months), ZERO)
    ps.gross, ps.deductions, ps.net = d(s.total_earnings), d(s.total_deductions), d(s.net)
    ps.employer_cost = d(s.total_earnings) + employer_total
    ps.lines = lines
    ps.statutory = stat
    t = w.get("tax") or {}
    taxable = sum((d(x["taxable"]) for x in s.lines or [] if x["kind"] == "earning" and x["code"] != "RESKILL"), ZERO)
    basic_da = sum((d(e2["amount"]) for x in s.lines or [] if x["code"] == "SALARY"
                    for e2 in (x.get("detail") or {}).get("earnings") or [] if e2.get("code") in ("BASIC", "DA")), ZERO)
    hra = sum((d(e2["amount"]) for x in s.lines or [] if x["code"] == "SALARY"
               for e2 in (x.get("detail") or {}).get("earnings") or [] if e2.get("code") == "HRA"), ZERO)
    stat_ded = {x["code"]: d(x["amount"]) for x in lines if x["kind"] == "deduction"}
    ps.tax = {**t, "tds": money(s.tax),
              "month": {"taxable": money(taxable), "basic_da": money(basic_da), "hra": money(hra),
                        "pf": money(stat_ded.get("PF_EE", ZERO) + stat_ded.get("VPF", ZERO)), "pt": money(stat_ded.get("PT", ZERO)),
                        "tds": money(s.tax), "nps": "0.00", "perqs": "0.00",
                        "regime": (t.get("worksheet") or {}).get("regime")}}
    ps.wage_rule = w.get("wage_rule") or {}
    ps.arrears = []
    ps.warnings = [] if d(s.net) >= 0 else [{"code": "negative_net", "level": "block",
                                              "message": f"The settlement is negative ({calc.inr(s.net)}): recover it separately."}]
    # snapshot through the regular path's helper, with this person's details
    stub = engine.EmployeeInput(employee_id=emp.id, period_start=run.period_start, period_end=run.period_end, segments=[],
                                comps=comps, rules={}, settings={},
                                profile={"gender": svc.gender_of(db, emp), "pt_state": (stat.get("pt") or {}).get("state")})
    runsvc._store_snapshot(ps, stub, emp, bank, entity_cache, db, company, w.get("monthly_gross"))


def on_run_paid(db: Session, company_id: int, run: PayRun, *, reference: str | None, failed: bool = False) -> None:
    """The settlement's pay run was marked paid: close loans, advances, assets and leave."""
    s = db.query(FinalSettlement).filter(FinalSettlement.company_id == company_id,
                                         FinalSettlement.uuid == (run.options or {}).get("settlement_uuid")).first()
    if s is None or s.status == "paid" or failed:
        return
    from core.timeutil import today_for_company_id as _today
    _close(db, company_id, s, paid_on=_today(db, company_id), reference=reference, mode="bank")


def pay(db: Session, company_id: int, s: FinalSettlement, *, paid_on: date, reference: str | None, mode: str = "bank") -> FinalSettlement:
    """Record the payment. With a pay run (the usual case) the run is locked if needed and
    marked paid, which closes the settlement; without one, the settlement is closed directly."""
    if s.status != "approved":
        raise BadRequest("Approve the settlement before paying it.")
    run = run_of(db, company_id, s)
    if run is not None and run.status != "cancelled":
        if run.status == "approved":
            runsvc.lock(db, company_id, run)
        if run.status not in ("locked", "paid"):
            raise BadRequest("The settlement's pay run is being reviewed again; approve and lock it first.")
        s.paid_on = paid_on
        runsvc.mark_paid(db, company_id, run, reference=reference)
        if s.status != "paid":
            _close(db, company_id, s, paid_on=paid_on, reference=reference, mode=mode)
        else:
            s.paid_on, s.payment_mode = paid_on, mode
        return s
    return _close(db, company_id, s, paid_on=paid_on, reference=reference, mode=mode)


def _close(db: Session, company_id: int, s: FinalSettlement, *, paid_on: date, reference: str | None, mode: str = "bank") -> FinalSettlement:
    emp = svc.get_employee(db, company_id, s.employee_id)
    ref = s.number or str(s.uuid)[:8]
    refs = {}
    for x in s.lines or []:
        if x.get("ref"):
            refs.setdefault(x["code"], []).append(x["ref"])
    try:
        from modules.loans import service as loans
        loans.recover_in_settlement(db, company_id, emp.id, ref, paid_on)
    except ImportError:
        pass
    try:
        from modules.expenses import service as expenses
        expenses.mark_settled(db, company_id, refs.get("REIMB", []), paid_on, ref)
        expenses.recover_advances(db, company_id, refs.get("ADVANCE_REC", []))
    except ImportError:
        pass
    try:
        from modules.payroll import fbp
        fbp.settle_claims(db, company_id, [(x["ref"], d(x["amount"])) for x in s.lines or []
                                           if (x.get("detail") or {}).get("fbp") and x.get("ref")], paid_on)
    except ImportError:
        pass
    adapters.mark_assets_recovered(db, company_id, refs.get("ASSET", []), ref)
    if any(x["code"] == "LEAVE_ENC" for x in s.lines or []):
        adapters.encash_leave_on_exit(db, company_id, emp, s.last_working_day)
    s.status, s.paid_on, s.payment_ref, s.payment_mode = "paid", s.paid_on or paid_on, reference, mode
    late = s.paid_on > s.due_date
    audit.event(db, "payroll.settlement_paid", "final_settlements", s.id,
                {"net": money(s.net), "reference": reference, "late": late}, entity_uuid=str(s.uuid),
                entity_label=svc.name_of(emp))
    db.flush()
    return s


def cancel(db: Session, company_id: int, s: FinalSettlement) -> None:
    if s.status == "paid":
        raise BadRequest("A paid settlement cannot be cancelled.")
    run = run_of(db, company_id, s)
    if run is not None and run.status not in ("cancelled",):
        if run.status in ("locked", "paid"):
            raise BadRequest("Its pay run is locked; reopen the run first.")
        if run.status in ("approved", "pending_approval"):
            run.status = "review"
        runsvc.cancel_run(db, company_id, run)
    s.status = "cancelled"
    db.flush()


def statement_pdf(db: Session, company_id: int, s: FinalSettlement) -> bytes:
    """The payslip-style full and final statement."""
    from modules.payroll import pdf
    emp = svc.get_employee(db, company_id, s.employee_id)
    company = svc._company(db, company_id)
    run = run_of(db, company_id, s)
    ps = db.query(Payslip).filter(Payslip.pay_run_id == run.id).first() if run else None
    info = dict(ps.snapshot or {}) if ps is not None else {}
    if not info:
        designation = getattr(getattr(emp, "designation", None), "designation_name", None)
        department = getattr(getattr(emp, "department", None), "department_name", None)
        info = {"name": svc.name_of(emp), "employee_code": emp.employee_code, "designation": designation,
                "department": department, "pan": emp.pan, "uan": emp.uan,
                "date_of_joining": emp.date_of_joining.isoformat() if emp.date_of_joining else None}
    ent = adapters.legal_entity(db, company, getattr(emp, "legal_entity_id", None))
    settings = svc.get_settings(db, company_id)
    card = {"name": ent["legal_name"] or company.company_name, "address": ent["address"], "logo_url": company.logo_url}
    return pdf.render(pdf.fnf_statement_html(s, info, card, EXIT_KINDS.get(s.exit_kind, s.exit_kind),
                                             (settings.get("payslip") or {}).get("accent") or "#4f46e5"))


def countdown(s: FinalSettlement, today: date) -> dict:
    days = (s.due_date - today).days
    if s.status == "paid":
        state = "on_time" if s.paid_on and s.paid_on <= s.due_date else "paid_late"
    elif days < 0:
        state = "overdue"
    elif days == 0:
        state = "due_today"
    elif days <= 1:
        state = "due_soon"
    else:
        state = "on_track"
    return {"days_left": days, "state": state}


def out(s: FinalSettlement, emp=None, today: date | None = None, full: bool = False) -> dict:
    o = {"uuid": str(s.uuid), "number": s.number, "employee_id": s.employee_id,
         "employee_name": svc.name_of(emp) if emp else None, "employee_code": emp.employee_code if emp else None,
         "exit_kind": s.exit_kind, "exit_kind_label": EXIT_KINDS.get(s.exit_kind, s.exit_kind),
         "resignation_date": s.resignation_date.isoformat() if s.resignation_date else None,
         "last_working_day": s.last_working_day.isoformat(), "due_date": s.due_date.isoformat(), "status": s.status,
         "status_label": STATUS.get(s.status, s.status), "total_earnings": money(s.total_earnings),
         "total_deductions": money(s.total_deductions), "tax": money(s.tax), "net": money(s.net),
         "paid_on": s.paid_on.isoformat() if s.paid_on else None, "payment_ref": s.payment_ref,
         "countdown": countdown(s, today or date.today())}
    if full:
        o.update({"lines": s.lines or [], "notice": s.notice or {}, "inputs": s.inputs or {}, "workings": s.workings or {},
                  "payment_mode": s.payment_mode, "created_by_name": s.created_by_name})
    return o


def deadline_alerts(db: Session, company_id: int) -> int:
    """Draft a settlement for anyone whose exit date has come, and alert HR and the owner as
    the 2-working-day deadline nears or passes. Each alert is sent once."""
    from modules.notifications.service import notify
    today = today_for_company_id(db, company_id)
    Employee = svc.employee_model()
    sent = 0
    exiting = db.query(Employee).filter(Employee.company_id == company_id, Employee.exit_date.isnot(None),
                                        Employee.exit_date <= today + timedelta(days=7),
                                        Employee.exit_date >= today - timedelta(days=30)).all()
    have = {r[0] for r in db.query(FinalSettlement.employee_id).filter(FinalSettlement.company_id == company_id,
                                                                       FinalSettlement.status != "cancelled").all()}
    for e in exiting:
        if e.id in have or not svc.current_salary(db, company_id, e.id, e.exit_date):
            continue
        try:
            with db.begin_nested():
                create(db, company_id, e, {"last_working_day": e.exit_date,
                                           "exit_kind": "resignation" if e.resignation_date else "termination"})
            sent += 1
        except Exception:
            continue
    hr_ids = [r[0] for r in db.query(Employee.id).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                                          Employee.role.in_(("admin", "hr"))).all()]
    for s in db.query(FinalSettlement).filter(FinalSettlement.company_id == company_id,
                                              FinalSettlement.status.in_(("draft", "pending_approval", "approved"))).all():
        c = countdown(s, today)
        key = {"due_soon": "due_soon", "due_today": "due_today", "overdue": f"overdue:{today.isoformat()}"}.get(c["state"])
        if c["state"] == "on_track" and s.last_working_day <= today:
            key = "started"
        if not key or key in (s.alerts_sent or []):
            continue
        emp = db.query(Employee).filter(Employee.id == s.employee_id).first()
        msg = {"started": f"Final settlement for {svc.name_of(emp)} is due by {s.due_date:%d %b} (2 working days).",
               "due_soon": f"Final settlement for {svc.name_of(emp)} is due tomorrow ({s.due_date:%d %b}).",
               "due_today": f"Final settlement for {svc.name_of(emp)} is due today."}.get(key) or \
            f"Final settlement for {svc.name_of(emp)} is overdue (was due {s.due_date:%d %b})."
        notify(db, company_id, [None] + hr_ids, kind="payroll", title=msg, body=msg,
               link=f"/dashboard/settlements?view={s.uuid}", group_key=f"fnf:{s.uuid}")
        s.alerts_sent = list(s.alerts_sent or []) + [key]
        sent += 1
    return sent
