"""Payroll check assistant: plain-language flags on a calculated run, before approval.

Rule-based and local (no outside services). Each flag explains what looks odd and why it
matters, so the reviewer can fix it or move on:

  sudden_change      take-home pay moved a lot against last month, with the parts that moved
  duplicate_bank     two people are paid into the same bank account
  zero_pf            PF applies to the person but nothing was deducted
  zero_esi           ESI coverage is on but no ESI was worked out
  tds_jump           TDS is much higher than last month
  no_pan_tds         tax deducted at the higher rate because PAN is missing
  pay_without_basic  pay without Basic (structure or salary problem)
  zero_net           nothing to pay
  one_time_spike     gross is well above the monthly salary because of one-time pay
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from modules.payroll.calc import ZERO, d, inr as money_i
from modules.payroll.models import EmployeePayrollProfile, PayRun, Payslip


def _amount(p: Payslip, code: str):
    return sum((d(ln["amount"]) for ln in p.lines or [] if ln["code"] == code), ZERO)


def review(db: Session, company_id: int, run: PayRun, slips: list[Payslip]) -> list[dict]:
    flags: list[dict] = []
    live = [p for p in slips if p.status in ("calculated", "held")]
    if not live:
        return flags
    from modules.payroll import service as svc
    settings = svc.get_settings(db, company_id)
    threshold = d(settings.get("variance_percent", 20))
    ids = [p.employee_id for p in live]
    profiles = {p.employee_id: p for p in db.query(EmployeePayrollProfile).filter(
        EmployeePayrollProfile.company_id == company_id, EmployeePayrollProfile.employee_id.in_(ids)).all()}
    prev_run = (db.query(PayRun).filter(PayRun.company_id == company_id, PayRun.kind == "regular",
                                        PayRun.pay_group_id == run.pay_group_id, PayRun.period_start < run.period_start,
                                        PayRun.status.in_(("approved", "locked", "paid")))
                .order_by(PayRun.period_start.desc()).first()) if run.kind == "regular" else None
    prev = {p.employee_id: p for p in db.query(Payslip).filter(Payslip.pay_run_id == prev_run.id).all()} if prev_run else {}

    def flag(key, severity, p, title, explanation):
        flags.append({"key": key, "severity": severity, "employee_id": p.employee_id if p else None,
                      "employee_name": ((p.snapshot or {}).get("name") if p else None), "title": title,
                      "explanation": explanation})

    for p in live:
        name = (p.snapshot or {}).get("name") or "This person"
        old = prev.get(p.employee_id)
        if old is not None and d(old.net) > 0:
            change = (d(p.net) - d(old.net)) / d(old.net) * 100
            if abs(change) >= threshold:
                moved = []
                old_lines = defaultdict(lambda: ZERO)
                for ln in old.lines or []:
                    old_lines[(ln["code"], ln["name"])] += d(ln["amount"])
                new_lines = defaultdict(lambda: ZERO)
                for ln in p.lines or []:
                    new_lines[(ln["code"], ln["name"])] += d(ln["amount"])
                for key in set(old_lines) | set(new_lines):
                    diff = new_lines[key] - old_lines[key]
                    if abs(diff) >= 500 and key[0] not in ("PF_ER", "EPS_ER", "EDLI", "PF_ADMIN", "ESI_ER", "GRATUITY", "LWF_ER"):
                        moved.append((abs(diff), f"{key[1]} {'+' if diff > 0 else '−'}{money_i(abs(diff))}"))
                moved.sort(reverse=True)
                parts = ", ".join(m[1] for m in moved[:4]) or "several small changes"
                flag("sudden_change", "warn", p, f"Take-home changed {change.quantize(d('0.1'))}%",
                     f"{name}'s take-home pay went from {money_i(old.net)} to {money_i(p.net)}. Main changes: {parts}. "
                     f"Check unpaid days, revisions and one-time payments.")
        prof = profiles.get(p.employee_id)
        pf_wage = d(((p.statutory or {}).get("pf") or {}).get("pf_wage"))
        if (prof is None or prof.pf_applicable) and pf_wage > 0 and _amount(p, "PF_EE") == 0:
            flag("zero_pf", "warn", p, "No PF deducted",
                 f"PF applies to {name} and PF wages are {money_i(pf_wage)}, but no PF was deducted. Check the PF settings "
                 f"on the payroll profile.")
        esi = (p.statutory or {}).get("esi") or {}
        if esi.get("covered") and d(esi.get("wage")) > 0 and _amount(p, "ESI_ER") == 0:
            flag("zero_esi", "warn", p, "ESI covered but nothing worked out",
                 f"{name} is covered by ESI this contribution period, but no ESI was calculated.")
        if old is not None:
            t_old, t_new = _amount(old, "TDS"), _amount(p, "TDS")
            if t_new > 2000 and t_new > t_old * 2 + 1000:
                flag("tds_jump", "info", p, "TDS went up sharply",
                     f"Tax this month is {money_i(t_new)} against {money_i(t_old)} last month. This is usual after a raise, "
                     f"bonus or when declarations were not verified; check the tax worksheet.")
        if ((p.tax or {}).get("worksheet") or {}).get("no_pan_rate_applied"):
            flag("no_pan_tds", "warn", p, "Higher tax without PAN",
                 f"{name} has no valid PAN, so tax is deducted at 20% of taxable income. Collect the PAN to deduct normally.")
        if d(p.gross) > 0 and _amount(p, "BASIC") == 0 and run.kind == "regular":
            flag("pay_without_basic", "warn", p, "Paid without Basic",
                 f"{name} is paid {money_i(p.gross)} with no Basic. PF, gratuity and the 50% wage rule depend on Basic.")
        if d(p.net) == 0 and d(p.gross) > 0:
            flag("zero_net", "warn", p, "Nothing to pay",
                 f"Deductions take all of {name}'s pay this month. Check loan instalments and other recoveries.")
        one_time = sum((d(ln["amount"]) for ln in p.lines or [] if ln["code"] in ("BONUS", "INCENTIVE", "ARREARS", "LEAVE_ENC")), ZERO)
        if one_time > 0 and d(p.gross) > 0 and one_time / d(p.gross) > d("0.5"):
            flag("one_time_spike", "info", p, "Large one-time payment",
                 f"{money_i(one_time)} of {name}'s {money_i(p.gross)} gross is one-time pay (bonus, incentive, arrears or "
                 f"encashment). Tax on it is deducted in full this month.")
    # duplicate bank accounts
    from modules.employees.models import BankAccount
    acct_ids = [((p.snapshot or {}).get("bank") or {}).get("account_id") for p in live]
    rows = db.query(BankAccount).filter(BankAccount.id.in_([a for a in acct_ids if a] or [-1])).all()
    by_number = defaultdict(set)
    for b in rows:
        by_number[(b.account_number or "").strip()].add(b.employee_id)
    slip_by_emp = {p.employee_id: p for p in live}
    for number, emps in by_number.items():
        if number and len(emps) > 1:
            names = ", ".join(sorted((slip_by_emp[e].snapshot or {}).get("name") or "?" for e in emps))
            flag("duplicate_bank", "block", slip_by_emp[sorted(emps)[0]], "Same bank account for different people",
                 f"{names} are paid into the same account ending {number[-4:]}. This is a common sign of a typing "
                 f"mistake or fraud; confirm before paying.")
    order = {"block": 0, "warn": 1, "info": 2}
    flags.sort(key=lambda f: (order.get(f["severity"], 3), f["title"]))
    return flags
