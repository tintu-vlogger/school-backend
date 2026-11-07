"""Defaults every company gets (shifts, the standard attendance rule, time settings) and,
in development, demo data for the demo company: rosters, two weeks of punches, devices,
requests and timesheets so the screens look alive.
"""
from __future__ import annotations

import logging
import random
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from core.context import RequestContext, use_context

log = logging.getLogger("hrms")
EPOCH = date(2020, 1, 1)


def setup_company(db: Session, company_id: int) -> None:
    from modules.attendance import service, settings_store
    from modules.shifts import service as shifts_service
    shifts_service.ensure_default_shifts(db, company_id)
    service.ensure_default_rule(db, company_id, EPOCH)
    settings_store.ensure_defaults(db, company_id, EPOCH)
    db.flush()


def _ctx(company_id: int, emp=None) -> RequestContext:
    if emp is None:
        return RequestContext(company_id=company_id, actor_type="owner", actor_name="Demo data", role="admin")
    return RequestContext(company_id=company_id, employee_id=emp.id, actor_type="employee",
                          actor_name=f"{emp.first_name} {emp.last_name}", role=emp.role)


def seed_demo(db: Session, company_id: int) -> None:
    from modules.attendance.models import Punch
    from modules.employees.models import Employee
    db.info["company_id"] = company_id
    emps = (db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True))
            .order_by(Employee.id).all())
    if not emps:
        return
    with use_context(_ctx(company_id)):
        first_time = not db.query(Punch.id).filter(Punch.company_id == company_id).first()
        if first_time:
            _setup(db, company_id, emps)
        fresh = [e for e in emps if not db.query(Punch.id).filter(Punch.employee_id == e.id).first()]
        if fresh:
            _arrangements(db, company_id, fresh)
            _punches(db, company_id, fresh)
            db.flush()
    if first_time:
        _requests(db, company_id, emps)
        with use_context(_ctx(company_id)):
            _roster(db, company_id, emps)
            _devices(db, company_id, emps)
            _timesheets(db, company_id, emps)
    db.flush()


def _setup(db, company_id, emps) -> None:
    from modules.attendance.models import Geofence
    if not db.query(Geofence.id).filter(Geofence.company_id == company_id).first():
        db.add(Geofence(company_id=company_id, name="Head office, Bengaluru", address="MG Road, Bengaluru 560001",
                        latitude=12.975526, longitude=77.606528, radius_m=300))
    # a woman for the night-shift consent demo
    from core.masters import MasterType, MasterValue
    female = (db.query(MasterValue).join(MasterType, MasterType.id == MasterValue.type_id)
              .filter(MasterValue.company_id == company_id, MasterType.key == "gender", MasterValue.code == "F").first())
    for e in emps:
        if e.first_name in ("Priya", "Asha", "Ananya", "Kavya", "Meera", "Sneha", "Divya") and female is not None:
            if hasattr(e, "gender_id") and not getattr(e, "gender_id", None):
                e.gender_id = female.id


def _arrangements(db, company_id, emps) -> None:
    from modules.shifts import service as shifts_service
    from modules.shifts.models import Shift, WorkArrangement
    general = db.query(Shift).filter(Shift.company_id == company_id, Shift.name == "General").first()
    start = date.today() - timedelta(days=90)
    for i, e in enumerate(emps):
        if db.query(WorkArrangement.id).filter(WorkArrangement.employee_id == e.id).first():
            continue
        data = {"effective_from": start, "shift_id": general.id if general else None, "working_days": [0, 1, 2, 3, 4],
                "punch_required": True, "note": "Office, Monday to Friday"}
        if i % 6 == 2:  # alternate Saturdays (1st and 3rd)
            data.update(working_days=[0, 1, 2, 3, 4, 5], alternate_days={"5": [1, 3]},
                        note="Monday to Friday, 1st and 3rd Saturday")
        if i % 6 == 4:  # senior / field staff: present by default
            data.update(punch_required=False, note="Field sales: present by default, no punching")
        shifts_service.save_arrangement(db, company_id, e.id, data)


def _punches(db, company_id, emps) -> None:
    """Two weeks of realistic punches."""
    from modules.attendance import service
    from modules.attendance.models import Punch
    from modules.shifts import service as shifts_service
    tz = service.company_tz(db, company_id)
    today = service.today(db, company_id)
    start = today - timedelta(days=14)
    rnd = random.Random(company_id * 7919)
    ids = [e.id for e in emps]
    plans = shifts_service.day_plans(db, company_id, ids, start, today)
    for n, e in enumerate(emps):
        for day in (start + timedelta(days=k) for k in range((today - start).days + 1)):
            plan = plans[(e.id, day)]
            if plan.day_type != "working" or not plan.punch_required or plan.shift is None:
                continue
            sp = shifts_service.spec(plan.shift)
            s, end = sp.window(day, tz)
            k = (day - start).days
            if (k + n) % 13 == 5:  # absent
                continue
            late = rnd.choice([-12, -8, -5, -3, 0, 4, 7, 9]) + (24 if (k + n) % 5 == 1 else 0)
            t_in = s + timedelta(minutes=late, seconds=rnd.randint(0, 59))
            stay = rnd.choice([0, 5, 10, 18, 25, 40])
            if (k + n) % 9 == 3:
                stay = 130  # overtime evening
            t_out = end + timedelta(minutes=stay, seconds=rnd.randint(0, 59))
            if (k + n) % 11 == 7:
                t_out = s + timedelta(hours=4, minutes=50)  # half day
            punches = [(t_in, "in")]
            if day == today:
                now = datetime.now(tz)
                if t_in > now:
                    continue
                if t_out <= now:
                    punches.append((t_out, "out"))
            elif (k + n) % 10 != 8:  # forgot to check out
                punches.append((t_out, "out"))
            for ts, direction in punches:
                db.add(Punch(company_id=company_id, employee_id=e.id, punched_at=ts.astimezone(timezone.utc).replace(microsecond=0),
                             direction=direction, source="device" if (k % 3) else "web",
                             latitude=12.9757 if (k % 3) == 0 else None, longitude=77.6066 if (k % 3) == 0 else None,
                             distance_m=40 if (k % 3) == 0 else None))
    db.flush()
    service.recompute(db, company_id, ids, start, today)


def _requests(db, company_id, emps) -> None:
    from core.masters import MasterType, MasterValue
    from core.workflow import service as workflow
    from core.workflow.models import WorkflowTask
    from modules.attendance import requests as att_requests
    from modules.attendance import service
    from modules.attendance.models import AttendanceDay
    today = service.today(db, company_id)
    nxt_monday = today + timedelta(days=7 - today.weekday())

    def cat(key, code):
        v = (db.query(MasterValue).join(MasterType, MasterType.id == MasterValue.type_id)
             .filter(MasterValue.company_id == company_id, MasterType.key == key, MasterValue.code == code).first())
        return v.id if v else None

    def ask(e, kind, data):
        with use_context(_ctx(company_id, e)):
            try:
                with db.begin_nested():
                    return att_requests.create(db, company_id, e, kind, data)[1]
            except Exception as exc:
                log.warning("demo %s request skipped: %s", kind, getattr(exc, "detail", exc))
                return None

    made = []
    # corrections for missed check-outs in the last week
    fixes = 0
    for e in emps:
        miss = (db.query(AttendanceDay).filter(AttendanceDay.employee_id == e.id, AttendanceDay.status == "missing_punch",
                                               AttendanceDay.date >= today - timedelta(days=6))
                .order_by(AttendanceDay.date.desc()).first())
        if miss and fixes < 3:
            inst = ask(e, "regularisation", {"date": miss.date, "out_time": "18:40", "reason": "Forgot to check out at the gate",
                                             "reason_code": "forgot_out"})
            if inst:
                made.append(inst)
                fixes += 1
    plans = [
        ("wfh", {"date": nxt_monday, "duration": "full_day", "category_id": cat("wfh_category", "personal"),
                 "reason": "Plumber visit at home"}),
        ("permission", {"date": nxt_monday + timedelta(days=1), "from_time": "16:30", "to_time": "18:00",
                        "reason": "Child's school meeting"}),
        ("on_duty", {"date": nxt_monday + timedelta(days=2), "duration": "full_day",
                     "category_id": cat("on_duty_type", "client_visit"), "reason": "Client workshop at Whitefield",
                     "place": "Whitefield"}),
        ("overtime", {"date": nxt_monday + timedelta(days=3), "hours": 2, "consent": True,
                      "reason": "Month-end closing"}),
        ("wfh", {"date": nxt_monday + timedelta(days=4), "duration": "second_half",
                 "category_id": cat("wfh_category", "health"), "reason": "Doctor's appointment in the afternoon"}),
    ]
    for i, (kind, data) in enumerate(plans):
        inst = ask(emps[(i + 1) % len(emps)], kind, data)
        if inst:
            made.append(inst)
    # approve a few so both pending and approved requests show
    from modules.employees.models import Employee
    for inst in made[::2]:
        task = db.query(WorkflowTask).filter(WorkflowTask.instance_id == inst.id, WorkflowTask.status == "pending").first()
        if task is None:
            continue
        approver = db.query(Employee).filter(Employee.id == task.approver_employee_id).first() \
            if task.approver_employee_id else None
        with use_context(_ctx(company_id, approver)):
            try:
                with db.begin_nested():
                    workflow.decide(db, company_id, task, "approve", "Okay")
            except Exception as exc:
                log.warning("demo approval skipped: %s", getattr(exc, "detail", exc))
    db.flush()


def _roster(db, company_id, emps) -> None:
    """A support desk of three on a weekly Morning / Evening / Night rotation: next week
    published, the week after still a draft."""
    from modules.attendance import service
    from modules.roster import service as roster
    from modules.shifts import adapters
    from modules.shifts import service as shifts_service
    from modules.shifts.models import Shift
    shifts = {s.name: s for s in db.query(Shift).filter(Shift.company_id == company_id).all()}
    if len(emps) < 4 or not {"Morning", "Evening", "Night"} <= set(shifts):
        return
    today = service.today(db, company_id)
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    desk = emps[4:7] if len(emps) >= 7 else emps[-3:]
    for e in desk:  # night-shift consent on file for the women on the desk
        if adapters.is_woman(db, e):
            shifts_service.record_consent(db, company_id, e.id, {
                "consent_date": today, "valid_from": today, "valid_to": today + timedelta(days=365),
                "safety_measures": "Company cab drop to the door with a guard; CCTV on the floor; women's help desk."})
    order = ["Morning", "Evening", "Night"]
    for w, publish in ((0, True), (1, False)):
        start = monday + timedelta(days=7 * w)
        for j, e in enumerate(desk):
            name = order[(j + w) % 3]
            roster.assign(db, company_id, {"employee_ids": [e.id], "start_date": start,
                                           "end_date": start + timedelta(days=5), "shift_id": shifts[name].id,
                                           "note": "Support desk rotation"}, source="pattern", publish=publish)
            roster.assign(db, company_id, {"employee_ids": [e.id], "dates": [start + timedelta(days=6)],
                                           "status": "off"}, publish=publish)
    # a weekly pattern people can reuse
    from modules.shifts.models import ShiftPattern
    if not db.query(ShiftPattern.id).filter(ShiftPattern.company_id == company_id).first():
        shifts_service.save_pattern(db, company_id, {"name": "Support desk week", "kind": "weekly", "slots": {
            "0": shifts["Morning"].id, "1": shifts["Morning"].id, "2": shifts["Evening"].id, "3": shifts["Evening"].id,
            "4": shifts["Morning"].id, "5": shifts["Evening"].id, "6": None}})
        shifts_service.save_pattern(db, company_id, {"name": "Three-shift cycle", "kind": "cycle", "slots": [
            shifts["Morning"].id, shifts["Morning"].id, shifts["Evening"].id, shifts["Evening"].id,
            shifts["Night"].id, shifts["Night"].id, None, None]})


def _devices(db, company_id, emps) -> None:
    from modules.attendance import devices
    from modules.attendance.models import Device
    if db.query(Device.id).filter(Device.company_id == company_id).first():
        return
    d, _key = devices.save(db, company_id, {"name": "Reception biometric", "vendor": "eSSL", "model": "X990",
                                            "serial_number": "ESSL-X990-2231", "kind": "biometric",
                                            "location_name": "Head office, ground floor", "connection": "push"})
    devices.save(db, company_id, {"name": "Warehouse face reader", "vendor": "ZKTeco", "model": "SpeedFace V5L",
                                  "kind": "face", "location_name": "Hoskote warehouse", "connection": "ftp",
                                  "ftp_config": {"protocol": "sftp", "host": "sftp.example.in", "port": 22,
                                                 "username": "attendance", "password": "change-me",
                                                 "path": "/exports/attlog", "file_pattern": "ATT_*.csv",
                                                 "every_minutes": 30},
                                  "column_mapping": {"employee": "EnrollNo", "timestamp": "DateTime",
                                                     "direction": "InOut", "match_by": "device_user_id"}})
    for i, e in enumerate(emps):
        devices.enrol(db, company_id, None, str(101 + i), e.id, card_number=f"0007{3410 + i}")
    tz_now = datetime.now(timezone.utc)
    devices.ingest(db, company_id, d, [], "push")  # an empty heartbeat shows up in the log
    d.last_seen_at = tz_now - timedelta(minutes=4)


def _timesheets(db, company_id, emps) -> None:
    from modules.attendance import service
    from modules.timesheets import service as ts
    from modules.timesheets.models import TsClient
    if db.query(TsClient.id).filter(TsClient.company_id == company_id).first():
        return
    acme = ts.save_client(db, company_id, {"name": "Acme Retail Pvt Ltd", "contact_name": "Rohit Bansal",
                                           "email": "rohit@acmeretail.in", "gstin": "29AABCA1234F1Z5"})
    ts.save_client(db, company_id, {"name": "Northwind Logistics", "contact_name": "Neha Iyer"})
    web = ts.save_project(db, company_id, {"name": "Website revamp", "code": "ACME-WEB", "client_id": acme.id,
                                           "owner_employee_id": emps[0].id, "billable": True, "budget_hours": 400,
                                           "status": "active"})
    internal = ts.save_project(db, company_id, {"name": "Internal tools", "code": "INT", "billable": False,
                                                "budget_hours": 120, "status": "active"})
    t1 = ts.save_task(db, company_id, web, {"name": "Design"})
    t2 = ts.save_task(db, company_id, web, {"name": "Development"})
    t3 = ts.save_task(db, company_id, internal, {"name": "Maintenance", "billable": False})
    today = service.today(db, company_id)
    last_monday = today - timedelta(days=today.weekday() + 7)
    for i, e in enumerate(emps[3:6] or emps[:3]):
        rows = [{"project_id": web.id, "task_id": (t1 if i == 0 else t2).id,
                 "hours": {(last_monday + timedelta(days=k)).isoformat(): [6, 5.5, 7, 6.5, 4][k] for k in range(5)}},
                {"project_id": internal.id, "task_id": t3.id,
                 "hours": {(last_monday + timedelta(days=k)).isoformat(): [1.5, 2, 1, 1.5, 3][k] for k in range(5)}}]
        with use_context(_ctx(company_id, e)):
            ts.save_week(db, company_id, e.id, last_monday, rows)
            this_week = today - timedelta(days=today.weekday())
            ts.save_week(db, company_id, e.id, this_week, [
                {"project_id": web.id, "task_id": t2.id,
                 "hours": {(this_week + timedelta(days=k)).isoformat(): 6 for k in range(min(today.weekday(), 5))}}])
            try:
                ts.submit_week(db, company_id, e, last_monday, "All done for the week")
            except Exception as exc:
                log.warning("demo timesheet not sent: %s", getattr(exc, "detail", exc))
