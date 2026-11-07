"""Pay sources: payslips (payroll register and statutory summaries), salaries, challans,
final settlements, loans and expense claims. Amounts are visible to payroll users and to
each person for their own rows only."""
from __future__ import annotations

from sqlalchemy.orm import aliased

from modules.reports.registry import Field, Source, register
from modules.reports.sources._common import EmpJoin, num

PAY = "payroll.view"
PAY_ALL = ("payroll.view",)


def _has_payroll() -> bool:
    try:
        import modules.payroll.models  # noqa: F401
        return True
    except Exception:  # pragma: no cover
        return False


def _line(codes):
    codes = set(codes)

    def f(r, c):
        return round(sum(num(x.get("amount")) for x in (r.get("lines") or []) if x.get("code") in codes), 2)
    return f


RUN_STATUS = [("draft", "Draft"), ("calculating", "Calculating"), ("calculated", "Calculated"),
              ("submitted", "Waiting for approval"), ("approved", "Approved"), ("locked", "Locked"), ("paid", "Paid"),
              ("cancelled", "Cancelled")]


def _payslips() -> Source:
    from modules.payroll.models import Payslip as P, PayRun
    j = EmpJoin("ps")
    run = aliased(PayRun, name="ps_run")
    money = dict(type="money", sensitive=PAY)
    fields = j.fields() + [
        Field("lines", "Lines", "text", expr=P.lines, hidden=True),
        Field("period_start", "Period", "date", expr=P.period_start, default=True),
        Field("period_end", "Period end", "date", expr=P.period_end),
        Field("fy", "Financial year", "text", expr=P.fy),
        Field("run_number", "Pay run", "text", expr=run.number),
        Field("run_kind", "Run type", "enum", expr=run.kind,
              options=[("regular", "Regular"), ("bonus", "Bonus"), ("arrears", "Arrears"),
                       ("settlement", "Settlement"), ("correction", "Correction")]),
        Field("run_status", "Run status", "enum", expr=run.status, options=RUN_STATUS),
        Field("slip_status", "Payslip status", "enum", expr=P.status,
              options=[("calculated", "Calculated"), ("held", "Held"), ("skipped", "Skipped"), ("error", "Error")]),
        Field("payable_days", "Payable days", "number", expr=P.payable_days),
        Field("lop_days", "LOP days", "number", expr=P.lop_days),
        Field("gross", "Gross", expr=P.gross, default=True, **money),
        Field("deductions", "Deductions", expr=P.deductions, default=True, **money),
        Field("net", "Net pay", expr=P.net, default=True, **money),
        Field("employer_cost", "Employer cost", expr=P.employer_cost, **money),
        Field("basic", "Basic", needs=("lines",), compute=_line(["BASIC"]), **money),
        Field("pf_employee", "PF (employee)", needs=("lines",), compute=_line(["PF_EE", "VPF"]), **money),
        Field("pf_employer", "PF (employer)", needs=("lines",), compute=_line(["PF_ER", "EPS_ER", "EDLI", "PF_ADMIN"]),
              **money),
        Field("esi_employee", "ESI (employee)", needs=("lines",), compute=_line(["ESI_EE"]), **money),
        Field("esi_employer", "ESI (employer)", needs=("lines",), compute=_line(["ESI_ER"]), **money),
        Field("pt", "Professional Tax", needs=("lines",), compute=_line(["PT"]), **money),
        Field("lwf", "Labour Welfare Fund", needs=("lines",), compute=_line(["LWF_EE", "LWF_ER"]), **money),
        Field("tds", "TDS", needs=("lines",), compute=_line(["TDS"]), **money),
        Field("payment_status", "Payment", "enum", expr=P.payment_status,
              options=[("pending", "Pending"), ("paid", "Paid"), ("failed", "Failed")]),
    ]
    return Source(key="payslips", label="Payslips", category="pay", sensitive=True, available=_has_payroll,
                  description="One row per person per pay run: earnings, deductions, statutory amounts and net pay.",
                  fields=fields,
                  select_from=lambda: j.join(P.__table__, P.employee_id).join(run, run.id == P.pay_run_id),
                  company_col=P.company_id, employee_col=P.employee_id, all_perms=PAY_ALL, allow_team=False,
                  date_field="period_start")


def _salaries() -> Source:
    from modules.payroll.models import EmployeeSalary as S
    j = EmpJoin("sal")
    fields = j.fields() + [
        Field("effective_from", "From", "date", expr=S.effective_from, default=True),
        Field("effective_to", "To", "date", expr=S.effective_to),
        Field("annual_ctc", "Annual CTC", "money", expr=S.annual_ctc, sensitive=PAY, default=True),
        Field("monthly_gross", "Monthly gross", "money", expr=S.monthly_gross, sensitive=PAY, default=True),
        Field("salary_status", "Status", "enum", expr=S.status,
              options=[("active", "Current"), ("superseded", "Earlier"), ("cancelled", "Cancelled")]),
        Field("reason", "Reason", "text", expr=S.reason),
    ]
    return Source(key="salaries", label="Salaries and CTC", category="pay", sensitive=True, available=_has_payroll,
                  description="Salary history: CTC and monthly gross from each effective date.",
                  fields=fields, select_from=lambda: j.join(S.__table__, S.employee_id), company_col=S.company_id,
                  employee_col=S.employee_id, all_perms=PAY_ALL, allow_team=False, date_field="effective_from")


def _challans() -> Source:
    from modules.payroll.models import StatutoryChallan as C
    from modules.organization.models import LegalEntity
    le = aliased(LegalEntity, name="ch_le")
    kinds = [("pf", "PF"), ("esi", "ESI"), ("pt", "Professional Tax"), ("lwf", "Labour Welfare Fund"),
             ("tds", "TDS deposit"), ("tds_return", "TDS return"), ("certificate", "Form 16"), ("salary", "Salary payment")]
    fields = [
        Field("kind", "What", "enum", expr=C.kind, options=kinds, default=True),
        Field("legal_entity", "Legal entity", "text", expr=le.name, default=True),
        Field("state", "State", "text", expr=C.state),
        Field("period", "Period", "text", expr=C.period, default=True),
        Field("due_date", "Due", "date", expr=C.due_date, default=True),
        Field("amount", "Amount", "money", expr=C.amount, default=True),
        Field("status", "Status", "enum", expr=C.status, default=True,
              options=[("pending", "Pending"), ("paid", "Paid"), ("filed", "Filed"), ("not_applicable", "Not needed")]),
        Field("paid_on", "Paid or filed on", "date", expr=C.paid_on),
        Field("reference", "Reference", "text", expr=C.reference),
    ]
    return Source(key="statutory_challans", label="Statutory payments and filings", category="pay",
                  available=_has_payroll, sensitive=True,
                  description="PF, ESI, PT, LWF and TDS payments and returns recorded against each period.",
                  fields=fields, select_from=lambda: C.__table__.outerjoin(le, le.id == C.legal_entity_id),
                  company_col=C.company_id, employee_col=None, all_perms=PAY_ALL, date_field="due_date")


def _settlements() -> Source:
    from modules.payroll.models import FinalSettlement as F
    j = EmpJoin("fs")
    fields = j.fields() + [
        Field("number", "Number", "text", expr=F.number),
        Field("exit_kind", "Exit type", "text", expr=F.exit_kind, default=True),
        Field("last_working_day", "Last working day", "date", expr=F.last_working_day, default=True),
        Field("due_date", "Pay by", "date", expr=F.due_date, default=True),
        Field("status", "Status", "text", expr=F.status, default=True),
        Field("net", "Net amount", "money", expr=F.net, sensitive=PAY, default=True),
        Field("paid_on", "Paid on", "date", expr=F.paid_on),
        Field("days_late", "Days late", "number", needs=("due_date", "paid_on"),
              compute=lambda r, c: max(0, ((r.get("paid_on") or c["today"]) - r["due_date"]).days)
              if r.get("due_date") else None),
    ]
    return Source(key="final_settlements", label="Final settlements", category="pay", sensitive=True,
                  available=_has_payroll, description="Full and final settlements with the legal pay-by date.",
                  fields=fields, select_from=lambda: j.join(F.__table__, F.employee_id), company_col=F.company_id,
                  employee_col=F.employee_id, all_perms=PAY_ALL, allow_team=False, date_field="last_working_day")


def _loans() -> Source:
    from modules.loans.models import Loan as L, LoanType
    j = EmpJoin("ln")
    lt = aliased(LoanType, name="ln_type")
    fields = j.fields() + [
        Field("number", "Number", "text", expr=L.number),
        Field("loan_type", "Type", "text", expr=lt.name, default=True),
        Field("principal", "Amount", "money", expr=L.principal, sensitive=PAY, default=True),
        Field("emi", "Instalment", "money", expr=L.emi, sensitive=PAY, default=True),
        Field("instalments", "Instalments", "number", expr=L.instalments),
        Field("outstanding", "Outstanding", "money", expr=L.outstanding, sensitive=PAY, default=True),
        Field("status", "Status", "text", expr=L.status, default=True),
        Field("disbursed_on", "Paid out on", "date", expr=L.disbursed_on),
    ]
    return Source(key="loans", label="Loans and advances", category="pay", sensitive=True,
                  description="Loans and salary advances with instalments and the amount outstanding.",
                  fields=fields,
                  select_from=lambda: j.join(L.__table__, L.employee_id).join(lt, lt.id == L.loan_type_id),
                  company_col=L.company_id, employee_col=L.employee_id, all_perms=PAY_ALL + ("loans.manage",),
                  allow_team=False, date_field="disbursed_on")


def _expenses() -> Source:
    from modules.expenses.models import ExpenseClaim as X
    j = EmpJoin("ex")
    fields = j.fields() + [
        Field("number", "Number", "text", expr=X.number, default=True),
        Field("title", "Title", "text", expr=X.title, default=True),
        Field("status", "Status", "text", expr=X.status, default=True),
        Field("total", "Claimed", "money", expr=X.total, default=True),
        Field("approved_total", "Approved", "money", expr=X.approved_total, default=True),
        Field("submitted_at", "Submitted", "datetime", expr=X.submitted_at),
        Field("paid_on", "Paid on", "date", expr=X.paid_on),
    ]
    return Source(key="expense_claims", label="Expense claims", category="pay",
                  description="Reimbursement claims with amounts claimed, approved and paid.",
                  fields=fields, select_from=lambda: j.join(X.__table__, X.employee_id), company_col=X.company_id,
                  employee_col=X.employee_id, all_perms=PAY_ALL + ("expenses.manage",), date_field="submitted_at")


if _has_payroll():
    for _s in (_payslips(), _salaries(), _challans(), _settlements()):
        register(_s)
for _f in (_loans, _expenses):
    try:
        register(_f())
    except Exception:  # pragma: no cover - module not installed
        pass
