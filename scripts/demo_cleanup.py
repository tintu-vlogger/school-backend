"""Tidy the demo company (development data only). Safe to run any number of times.

    cd backend && PYTHONPATH=. .venv/bin/python scripts/demo_cleanup.py

- the owner's employee record: Tintu C Raju, Managing Director, Leadership
- archives the test "Asha Menon" employees
- removes the "Kit QA" test departments (deactivated if something still points at them)
- removes approval requests whose leave request no longer exists (they showed up twice in
  the approver's inbox next to the real ones)
- marks older notifications as read so each person has at most a handful unread
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from core import context  # noqa: E402
from core.db import system_session  # noqa: E402

DEMO_SLUG = "nextgenhrms-admin"
OWNER_NAME = ("Tintu", "C Raju")
MAX_UNREAD = 12


def main() -> None:
    import main as _app  # noqa: F401  (loads every model and module hook)
    from modules.company.models import Company
    from modules.company.owner import ensure_owner_employee
    from modules.departments.models import Department
    from modules.employees import service as emp_service
    from modules.employees.models import Employee
    from modules.notifications.models import Notification
    from core.workflow.models import WorkflowInstance, WorkflowTask

    db = system_session()
    ctx = context.current()
    ctx.actor_type, ctx.actor_name = "system", "Demo clean-up"
    try:
        company = db.query(Company).filter(Company.slug == DEMO_SLUG).first()
        if company is None:
            print("No demo company here; nothing to do.")
            return
        cid = company.id
        db.info["company_id"] = cid
        ctx.company_id = cid

        # 1. The owner as an employee.
        config = dict(company.config or {})
        if config.get("owner_name") != " ".join(OWNER_NAME):
            config["owner_name"] = " ".join(OWNER_NAME)
            company.config = config
        owner = ensure_owner_employee(db, cid)
        if (owner.first_name, owner.last_name) != OWNER_NAME:
            owner.first_name, owner.last_name = OWNER_NAME
        dept = (db.query(Department).filter(Department.company_id == cid, Department.department_name == "Leadership")
                .first())
        if dept and owner.department_id != dept.id:
            owner.department_id = dept.id
        print(f"Owner: {owner.first_name} {owner.last_name} (employee #{owner.id})")

        # 2. Test employees.
        archived = 0
        for emp in (db.query(Employee).filter(Employee.company_id == cid, Employee.first_name == "Asha",
                                              Employee.last_name == "Menon", Employee.status != "archived").all()):
            if emp.status not in ("exited", "pre_joining"):
                emp_service.set_status(db, cid, emp, "exited", reason="Test record")
            emp_service.archive(db, cid, emp, reason="Test record")
            archived += 1
        print(f"Archived test employees: {archived}")

        # 3. Test departments.
        removed = kept = 0
        for d in db.query(Department).filter(Department.company_id == cid, Department.department_name.ilike("Kit QA%")).all():
            try:
                with db.begin_nested():
                    # Demo increment budgets were seeded for every department, these included.
                    db.execute(text("DELETE FROM perf_increment_budgets WHERE department_id = :d"), {"d": d.id})
                    db.delete(d)
                    db.flush()
                removed += 1
            except Exception:
                d.active = False
                kept += 1
        print(f"Removed test departments: {removed}" + (f" (deactivated {kept} still in use)" if kept else ""))

        # 4. Approval requests whose leave request is gone (duplicates in the inbox).
        from modules.leave.models import LeaveRequest
        live = {i for (i,) in db.query(LeaveRequest.workflow_instance_id).filter(LeaveRequest.company_id == cid,
                                                                             LeaveRequest.workflow_instance_id.isnot(None))}
        live |= {i for (i,) in db.query(LeaveRequest.cancel_instance_id).filter(LeaveRequest.company_id == cid,
                                                                            LeaveRequest.cancel_instance_id.isnot(None))}
        orphans = [i for i in db.query(WorkflowInstance).filter(WorkflowInstance.company_id == cid,
                                                                WorkflowInstance.subject_type == "leave_requests").all()
                   if i.id not in live]
        for inst in orphans:
            db.query(WorkflowTask).filter(WorkflowTask.instance_id == inst.id).delete()
            db.delete(inst)
        print(f"Removed duplicate approval requests: {len(orphans)}")

        # 5. Notifications: keep the newest few unread per person.
        now = datetime.now(timezone.utc)
        marked = 0
        people = {e for (e,) in db.query(Notification.employee_id).filter(Notification.company_id == cid,
                                                                       Notification.read_at.is_(None)).distinct()}
        for emp_id in people:
            q = db.query(Notification.id).filter(Notification.company_id == cid, Notification.read_at.is_(None))
            q = q.filter(Notification.employee_id == emp_id) if emp_id is not None else q.filter(Notification.employee_id.is_(None))
            old = [i for (i,) in q.order_by(Notification.created_at.desc(), Notification.id.desc()).offset(MAX_UNREAD).all()]
            if old:
                marked += db.query(Notification).filter(Notification.id.in_(old)).update({"read_at": now},
                                                                                        synchronize_session=False)
        print(f"Marked older notifications as read: {marked}")

        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
