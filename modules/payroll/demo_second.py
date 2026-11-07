"""Demo data for the second payroll pass: flexible benefit splits and bills, a salary
revision from the pay review waiting for payroll, and department budgets. Idempotent."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session


def _png() -> bytes:
    import io
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (420, 560), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(["CITY FUEL STATION", "Invoice 4471", "Petrol 32.5 L", "Amount Rs 3,450.00", "Paid by card"]):
        d.text((30, 40 + i * 40), line, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def seed(db: Session, company_id: int) -> None:
    from core.context import RequestContext, use_context
    from core.timeutil import today_for_company_id
    from modules.payroll import calc, fbp, insights, revisions
    from modules.payroll import service as svc
    from modules.payroll.models import EmployeeSalary, FbpClaim, PayrollBudget, SalaryRevisionRequest, SalaryStructure
    today = today_for_company_id(db, company_id)
    fy = calc.fy_of(today)
    with use_context(RequestContext(company_id=company_id, actor_type="system", actor_name="Demo data")):
        if not db.query(FbpClaim.id).filter(FbpClaim.company_id == company_id).first():
            mgmt = {s.id for s in db.query(SalaryStructure).filter(SalaryStructure.company_id == company_id, SalaryStructure.code == "MGMT").all()}
            rows = db.query(EmployeeSalary).filter(EmployeeSalary.company_id == company_id, EmployeeSalary.status == "active",
                                                   EmployeeSalary.structure_id.in_(mgmt or [-1])).limit(3).all()
            for i, s in enumerate(rows):
                x = fbp.declaration(db, company_id, s.employee_id, fy)
                x.allocation, x.status = {"FUEL": 30000, "PHONE": 18000, "MEAL": 12000}, "submitted"
                x.total = fbp.basket(db, company_id, s.employee_id, fy)
                if sum(Decimal(str(v)) for v in x.allocation.values()) > x.total:
                    x.allocation = {k: float(v) for k, v in fbp._salary_split(db, company_id, s.employee_id, fy).items()}
                c = fbp.submit_claim(db, company_id, s.employee_id,
                                     {"component_code": "FUEL", "bill_date": today - timedelta(days=12 + i), "amount": 3450,
                                      "bill_number": f"4471{i}", "vendor": "City Fuel Station"},
                                     [("fuel-bill.png", "image/png", _png())])
                if i == 0:
                    fbp.verify(db, company_id, c, approved_amount=3450, comment=None)
                fbp.submit_claim(db, company_id, s.employee_id,
                                 {"component_code": "PHONE", "bill_date": today - timedelta(days=5), "amount": 999,
                                  "bill_number": f"JIO-{i}", "vendor": "Jio postpaid"}, [("phone-bill.png", "image/png", _png())])
        if not db.query(SalaryRevisionRequest.id).filter(SalaryRevisionRequest.company_id == company_id).first():
            Employee = svc.employee_model()
            for e in db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True)).limit(40).all():
                cur = svc.current_salary(db, company_id, e.id, today)
                if cur is None or not cur.structure_id:
                    continue
                revisions.request(db, company_id, e.id, (Decimal(cur.annual_ctc) * Decimal("1.08")).quantize(Decimal("1000")),
                                  today.replace(day=1) - timedelta(days=31 if today.day < 28 else 0), source="pay_review",
                                  source_ref=f"demo-{e.id}", note="Mid-year pay review")
                if db.query(SalaryRevisionRequest).filter(SalaryRevisionRequest.company_id == company_id).count() >= 3:
                    break
        if not db.query(PayrollBudget.id).filter(PayrollBudget.company_id == company_id, PayrollBudget.fy == fy).first():
            names = {v: k for k, v in insights.budget_names(db, company_id, "department").items()}
            for r in insights.cost_vs_budget(db, company_id, fy, "department", today):
                if r["name"] in names and r["projected"]:
                    factor = Decimal("0.95") if len(r["name"]) % 3 == 0 else Decimal("1.1")
                    db.add(PayrollBudget(company_id=company_id, fy=fy, dimension="department", ref_id=names[r["name"]],
                                         amount=(Decimal(str(r["projected"])) * factor).quantize(Decimal("10000"))))
    db.flush()
