"""Demo asset register for the development company: 30 assets with assignments,
returns, a recovery, maintenance and requests."""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.assets.models import Asset, AssetAssignment, AssetMaintenance, AssetRequest

MODELS = [
    # category label, name, brand, model, value, warranty years
    ("Laptop", "Dell Latitude 5440", "Dell", "Latitude 5440", 78500, 3),
    ("Laptop", "Lenovo ThinkPad E14 Gen 5", "Lenovo", "ThinkPad E14", 68900, 3),
    ("Laptop", "Apple MacBook Air M3", "Apple", "MacBook Air 13 M3", 114900, 1),
    ("Laptop", "HP ProBook 440 G10", "HP", "ProBook 440 G10", 62500, 3),
    ("Desktop", "Dell OptiPlex 7010", "Dell", "OptiPlex 7010", 54000, 3),
    ("Monitor", "LG 24\" IPS monitor", "LG", "24MP400", 9800, 3),
    ("Monitor", "Dell 27\" monitor", "Dell", "P2723DE", 24500, 3),
    ("Mobile phone", "Samsung Galaxy A35", "Samsung", "Galaxy A35 5G", 30999, 1),
    ("Mobile phone", "Apple iPhone 15", "Apple", "iPhone 15 128GB", 69900, 1),
    ("Tablet", "Samsung Galaxy Tab S9 FE", "Samsung", "Tab S9 FE", 36999, 1),
    ("Headset", "Jabra Evolve2 30", "Jabra", "Evolve2 30", 7900, 2),
    ("ID card", "Employee ID card", None, "PVC card", 250, 0),
    ("Access card", "Office access card", "HID", "iCLASS", 600, 0),
]


def seed_demo(db: Session, company_id: int) -> None:
    from core import numbering
    from core.masters import get_type, values_query
    from modules.assets import service
    from modules.employees.models import Employee
    if db.query(Asset.id).filter(Asset.company_id == company_id).first():
        return
    emps = db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True)).order_by(Employee.id).all()
    if not emps:
        return
    cats = {v.label: v.id for v in values_query(db, company_id, get_type(db, company_id, "asset_category")).all()}
    try:  # include sub-values (Laptop, Monitor …)
        from core.masters import MasterValue
        mtype = get_type(db, company_id, "asset_category")
        cats.update({v.label: v.id for v in db.query(MasterValue).filter(MasterValue.company_id == company_id,
                                                                          MasterValue.type_id == mtype.id)})
    except Exception:
        pass
    today = service._today(db, company_id)
    assets = []
    for i in range(30):
        cat, name, brand, model, value, years = MODELS[i % len(MODELS)]
        bought = today - timedelta(days=330 + (i * 37) % 700)
        a = Asset(company_id=company_id, asset_tag=numbering.next_number(db, company_id, "asset") or f"AST{i + 1:05d}",
                  name=name, category_id=cats.get(cat), brand=brand, model=model,
                  serial_number=f"{(brand or 'GEN')[:3].upper()}{bought.year % 100}{i:03d}{(i * 7919) % 10000:04d}",
                  purchase_date=bought, purchase_value=Decimal(value), vendor=["Croma Business", "Redington India",
                  "Ingram Micro", "Local vendor"][i % 4], invoice_no=f"INV/{bought.year}/{1000 + i}",
                  warranty_until=bought + timedelta(days=365 * years) if years else None,
                  condition="new" if i % 5 == 0 else "good", status="available")
        db.add(a)
        assets.append(a)
    db.flush()
    for a in assets:
        db.refresh(a)
    # 19 handed over (3 still waiting for the employee to confirm)
    for i, a in enumerate(assets[:19]):
        emp = emps[i % len(emps)]
        x = service.assign(db, company_id, a, emp.id, assigned_on=today - timedelta(days=200 - i * 9))
        if i % 6 != 5:
            x.status, x.acknowledged_at = "active", service._now() - timedelta(days=150 - i * 7)
    # returns: one fine, one damaged with a recovery, one lost
    for j, (cond, amount, reason) in enumerate((("good", None, None), ("damaged", Decimal("4500.00"), "Broken screen"),
                                                ("lost", Decimal("12000.00"), "Phone lost while travelling"))):
        a = assets[19 + j]
        emp = emps[(j + 1) % len(emps)]
        x = service.assign(db, company_id, a, emp.id, assigned_on=today - timedelta(days=300))
        x.status, x.acknowledged_at = "active", service._now() - timedelta(days=299)
        service.return_asset(db, company_id, x, returned_on=today - timedelta(days=20 - j * 5), condition=cond,
                             recovery_amount=amount, recovery_reason=reason,
                             notes="Returned at exit" if j == 0 else None)
    assets[23].status = "in_repair"
    assets[24].status = "retired"
    assets[24].retired_on = today - timedelta(days=40)
    for a, kind, cost, desc, st in ((assets[20], "repair", 4500, "Screen replaced at authorised service centre", "in_progress"),
                                    (assets[23], "repair", 2200, "Keyboard and battery replaced", "in_progress"),
                                    (assets[0], "service", 0, "Annual check-up and cleaning", "done"),
                                    (assets[4], "upgrade", 3800, "RAM upgraded to 16 GB", "done")):
        db.add(AssetMaintenance(company_id=company_id, asset_id=a.id, kind=kind, performed_on=today - timedelta(days=10),
                                vendor="Authorised service centre", cost=Decimal(cost), description=desc, status=st,
                                created_by_name="IT team", next_due_on=today + timedelta(days=180) if kind == "service" else None))
    if len(emps) > 1:
        db.add(AssetRequest(company_id=company_id, employee_id=emps[-1].id, category_id=cats.get("Monitor"),
                            details="A second monitor for financial modelling work.", needed_by=today + timedelta(days=7),
                            status="approved"))
        db.add(AssetRequest(company_id=company_id, employee_id=emps[1].id, category_id=cats.get("Headset"),
                            details="Headset for customer calls.", status="pending"))
    db.flush()
