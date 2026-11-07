"""Attendance end to end: device push, daily status, corrections through approvals,
penalties, locks, roster rest rules, night-shift consent, timesheets and tenant isolation."""
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from core.db import tenant_session
from modules.attendance.models import AttendanceDay
from tests.conftest import login_as


def hire(company, first, last="Kumar", **extra):
    """An employee who joined long ago (so past days count)."""
    return company.add_employee(first, last, date_of_joining="2025-01-01", **extra)


def today_ist() -> date:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def last_week_monday() -> date:
    t = today_ist()
    return t - timedelta(days=t.weekday() + 7)


def make_device(company, name="Gate"):
    r = company.post("/attendance/devices", json={"name": name, "vendor": "eSSL", "kind": "biometric",
                                                  "connection": "push"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["api_key"].startswith("dev_")
    return body


def push(client, key, punches):
    return client.post("/api/v1/attendance/devices/push", json={"punches": punches}, headers={"X-Device-Key": key})


def enrol(company, emp, uid):
    r = company.post("/attendance/enrolments", json={"device_user_id": uid, "employee_id": emp["id"]})
    assert r.status_code == 201, r.text


def day_row(company, emp, d):
    r = company.get("/attendance/records", params={"start": d.isoformat(), "end": d.isoformat(), "q": emp["email"]})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    return items[0] if items else None


def approve_all(session):
    for t in session.get("/workflow/inbox").json()["items"]:
        r = session.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"})
        assert r.status_code == 200, r.text


def test_device_push_needs_a_valid_key_and_ignores_duplicates(client, company_a):
    emp = hire(company_a, "Dinesh")
    dev = make_device(company_a)
    enrol(company_a, emp, "501")
    d = last_week_monday()
    punches = [{"device_user_id": "501", "timestamp": f"{d}T09:28:00", "direction": "0"},
               {"device_user_id": "501", "timestamp": f"{d} 18:41:10", "direction": "1"},
               {"device_user_id": "999", "timestamp": f"{d}T09:30:00"}]
    assert push(client, "dev_wrong_key_wrong_key_wrong", punches).status_code == 401
    assert client.post("/api/v1/attendance/devices/push", json={"punches": punches}).status_code == 401
    r = push(client, dev["api_key"], punches)
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 2 and r.json()["unmatched"] == 1
    again = push(client, dev["api_key"], punches[:2]).json()
    assert again["inserted"] == 0 and again["duplicates"] == 2
    row = day_row(company_a, emp, d)
    assert row["status"] == "present" and row["punch_count"] == 2 and not row["is_late"]
    logs = company_a.get("/attendance/sync-logs").json()["items"]
    assert logs[0]["duplicates"] == 2 and logs[1]["inserted"] == 2
    # a new key stops the old one
    company_a.post(f"/attendance/devices/{dev['uuid']}/key")
    assert push(client, dev["api_key"], punches).status_code == 401


def test_missing_punch_fixed_by_approved_regularisation(client, company_a):
    emp = hire(company_a, "Revathi")
    dev = make_device(company_a)
    enrol(company_a, emp, "77")
    company_a.put("/attendance/settings/regularisation", json={"config": {"window_days": 30, "monthly_limit": 3},
                                                                "effective_from": "2020-01-01"})
    d = last_week_monday() + timedelta(days=1)
    push(client, dev["api_key"], [{"device_user_id": "77", "timestamp": f"{d}T09:35:00"}])
    assert day_row(company_a, emp, d)["status"] == "missing_punch"
    me = login_as(company_a, emp)
    r = me.post("/attendance/me/requests/regularisation",
                json={"date": d.isoformat(), "out_time": "18:40", "reason": "Forgot to check out"})
    assert r.status_code == 201, r.text
    assert r.json()["workflow_status"] == "pending"
    assert day_row(company_a, emp, d)["status"] == "missing_punch"  # nothing changes before approval
    approve_all(company_a)  # no manager: the company owner approves
    row = day_row(company_a, emp, d)
    assert row["status"] == "present" and row["punch_count"] == 2
    detail = company_a.get(f"/attendance/records/{row['uuid']}").json()
    assert {p["source"] for p in detail["punches"]} == {"device", "regularisation"}
    mine = me.get("/attendance/me/requests").json()["items"]
    assert mine[0]["status"] == "approved"


def test_regularisation_monthly_limit(client, company_a):
    emp = hire(company_a, "Limit")
    company_a.put("/attendance/settings/regularisation", json={"config": {"window_days": 60, "monthly_limit": 1},
                                                                "effective_from": "2020-01-01"})
    me = login_as(company_a, emp)
    d = today_ist() - timedelta(days=1)
    assert me.post("/attendance/me/requests/regularisation",
                   json={"date": d.isoformat(), "in_time": "09:30", "reason": "Device down"}).status_code == 201
    other = d - timedelta(days=1) if d.day > 1 else d
    r = me.post("/attendance/me/requests/regularisation",
                json={"date": other.isoformat(), "in_time": "09:30", "reason": "Device down"})
    assert r.status_code == 400


def test_late_marks_make_a_half_day_penalty(client, company_a):
    emp = hire(company_a, "Latif")
    dev = make_device(company_a)
    enrol(company_a, emp, "L1")
    monday = last_week_monday()
    punches = []
    for i in range(3):
        d = monday + timedelta(days=i)
        punches += [{"device_user_id": "L1", "timestamp": f"{d}T10:05:00"},
                    {"device_user_id": "L1", "timestamp": f"{d}T19:30:00"}]
    assert push(client, dev["api_key"], punches).json()["inserted"] == 6
    rows = [day_row(company_a, emp, monday + timedelta(days=i)) for i in range(3)]
    assert all(r["is_late"] and r["status"] == "present" for r in rows)
    # the 3rd late arrival in the month carries the penalty (unless the days span two months)
    if monday.month == (monday + timedelta(days=2)).month:
        assert rows[2]["penalty_lop_days"] == "0.50" and rows[0]["penalty_lop_days"] == "0.00"
    s = company_a.get("/attendance/reports/late-early",
                      params={"start": monday.isoformat(), "end": (monday + timedelta(days=4)).isoformat()}).json()
    assert any(r["employee_id"] == emp["id"] and r["late"] == 3 for r in s["rows"])


def test_wfh_weekly_limit_and_permission_limit(client, company_a):
    emp = hire(company_a, "Wahida")
    company_a.put("/attendance/settings/wfh", json={"config": {"weekly_limit": 1, "monthly_limit": 10}})
    me = login_as(company_a, emp)
    t = today_ist()
    nxt = t + timedelta(days=7 - t.weekday())  # next Monday
    cat = me.get("/masters/options/wfh_category").json()["items"][0]["value"]
    first = me.post("/attendance/me/requests/wfh", json={"date": nxt.isoformat(), "reason": "Home", "category_id": cat})
    assert first.status_code == 201, first.text
    r = me.post("/attendance/me/requests/wfh", json={"date": (nxt + timedelta(days=1)).isoformat(), "reason": "Home",
                                                    "category_id": cat})
    assert r.status_code == 400 and "1 days a week" in r.json()["detail"]
    ok = me.post("/attendance/me/requests/permission",
                 json={"date": (nxt + timedelta(days=2)).isoformat(), "from_time": "16:30", "to_time": "18:00",
                       "reason": "Bank"})
    assert ok.status_code == 201, ok.text
    too_long = me.post("/attendance/me/requests/permission",
                       json={"date": (nxt + timedelta(days=3)).isoformat(), "from_time": "10:00", "to_time": "13:00"})
    assert too_long.status_code == 400
    ot = me.post("/attendance/me/requests/overtime", json={"date": nxt.isoformat(), "hours": 2, "reason": "Audit"})
    assert ot.status_code == 400 and "consent" in ot.json()["detail"]


def test_roster_blocks_too_little_rest(client, company_a):
    emp = hire(company_a, "Rakesh")
    shifts = {s["name"]: s for s in company_a.get("/shifts/").json()}
    d = today_ist() + timedelta(days=10)
    r = company_a.post("/roster/assign", json={"employee_ids": [emp["id"]], "dates": [d.isoformat()],
                                               "shift_id": shifts["Evening"]["id"]})
    assert r.status_code == 200 and r.json()["saved"] == 1
    nxt = d + timedelta(days=1)
    r = company_a.post("/roster/assign", json={"employee_ids": [emp["id"]], "dates": [nxt.isoformat()],
                                               "shift_id": shifts["Morning"]["id"]})
    body = r.json()
    assert body["saved"] == 0 and body["blocked"][0]["rule"] == "min_rest_hours"
    # seven days in a row without a weekly rest day is blocked too
    start = d + timedelta(days=20)
    r = company_a.post("/roster/assign", json={"employee_ids": [emp["id"]], "start_date": start.isoformat(),
                                               "end_date": (start + timedelta(days=6)).isoformat(),
                                               "shift_id": shifts["General"]["id"]}).json()
    assert any(b["rule"] == "max_consecutive_days" for b in r["blocked"]) and r["saved"] == 6


def test_draft_roster_counts_only_after_publishing(client, company_a):
    emp = hire(company_a, "Pallavi")
    shifts = {s["name"]: s for s in company_a.get("/shifts/").json()}
    t = today_ist()
    d = t + timedelta(days=14 - t.weekday() + 1)  # a Tuesday two weeks ahead
    company_a.post("/roster/assign", json={"employee_ids": [emp["id"]], "dates": [d.isoformat()],
                                           "shift_id": shifts["Morning"]["id"]})
    me = login_as(company_a, emp)
    sched = me.get("/attendance/me/schedule", params={"start": d.isoformat(), "end": d.isoformat()}).json()
    assert sched[0]["shift"]["name"] != "Morning"
    assert company_a.post("/roster/publish", json={"start_date": d.isoformat(), "end_date": d.isoformat()}).json()["published"] == 1
    sched = me.get("/attendance/me/schedule", params={"start": d.isoformat(), "end": d.isoformat()}).json()
    assert sched[0]["shift"]["name"] == "Morning"


def _make_woman(company, emp):
    db = tenant_session(company.id)
    try:
        from modules.employees.models import Employee
        e = db.query(Employee).filter(Employee.id == emp["id"]).first()
        if hasattr(Employee, "gender_id"):
            fid = db.execute(text("SELECT v.id FROM master_values v JOIN master_types t ON t.id = v.type_id "
                                  "WHERE t.key = 'gender' AND v.code = 'F' AND v.company_id = :c"), {"c": company.id}).scalar()
            e.gender_id = fid
        else:
            e.custom = {**(e.custom or {}), "gender": "F"}
        db.commit()
    finally:
        db.close()


def test_womens_night_shift_needs_consent(client, company_a):
    emp = hire(company_a, "Nandini")
    _make_woman(company_a, emp)
    shifts = {s["name"]: s for s in company_a.get("/shifts/").json()}
    t = today_ist()
    d = t + timedelta(days=14 - t.weekday() + 4)  # a Friday: the next day is a weekly off, so rest is fine
    body = {"employee_ids": [emp["id"]], "dates": [d.isoformat()], "shift_id": shifts["Night"]["id"]}
    r = company_a.post("/roster/assign", json=body).json()
    assert r["saved"] == 0 and r["blocked"][0]["rule"] == "women_night_consent"
    # a man is not blocked
    man = hire(company_a, "Naveen")
    assert company_a.post("/roster/assign", json={**body, "employee_ids": [man["id"]]}).json()["saved"] == 1
    c = company_a.post("/shifts/consents", json={"employee_id": emp["id"], "consent_date": today_ist().isoformat(),
                                                 "valid_from": today_ist().isoformat(),
                                                 "safety_measures": "Cab drop with guard"})
    assert c.status_code == 201, c.text
    assert company_a.post("/roster/assign", json=body).json()["saved"] == 1
    # the law can't be switched off in settings
    bad = company_a.put("/attendance/settings/rest_rules", json={"config": {"women_night_consent": {"action": "off"}}})
    assert bad.status_code == 400


def test_lock_keeps_days_and_moves_changes_to_next_period(client, company_a):
    emp = hire(company_a, "Lokesh")
    dev = make_device(company_a)
    enrol(company_a, emp, "LK")
    company_a.put("/attendance/settings/regularisation", json={"config": {"window_days": 60, "monthly_limit": 5},
                                                                "effective_from": "2020-01-01"})
    d = last_week_monday() + timedelta(days=2)
    push(client, dev["api_key"], [{"device_user_id": "LK", "timestamp": f"{d}T09:30:00"}])
    assert day_row(company_a, emp, d)["status"] == "missing_punch"
    lock = company_a.post("/attendance/locks", json={"start": d.isoformat(), "end": d.isoformat()})
    assert lock.status_code == 201, lock.text
    me = login_as(company_a, emp)
    assert me.post("/attendance/me/requests/regularisation",
                   json={"date": d.isoformat(), "out_time": "18:35", "reason": "Forgot"}).status_code == 201
    approve_all(company_a)
    row = day_row(company_a, emp, d)
    assert row["status"] == "missing_punch" and row["locked"]  # the locked day stays as paid
    adj = [a for a in company_a.get("/attendance/adjustments").json() if a["employee_id"] == emp["id"]]
    assert adj and adj[0]["payable_days_delta"] == "1.00" and adj[0]["apply_from"] == (d + timedelta(days=1)).isoformat()
    pay = company_a.get("/attendance/reports/payroll", params={"start": (d + timedelta(days=1)).isoformat(),
                                                               "end": (d + timedelta(days=20)).isoformat()}).json()
    mine = next(r for r in pay["rows"] if r["employee_id"] == emp["id"])
    assert mine["adjustment_payable_days"] == "1.00"
    assert company_a.post("/attendance/locks", json={"start": d.isoformat(), "end": d.isoformat()}).status_code == 400


def test_payroll_summary_and_overtime_report(client, company_a):
    emp = hire(company_a, "Paresh")
    dev = make_device(company_a)
    enrol(company_a, emp, "P1")
    monday = last_week_monday()
    pts = []
    for i in range(5):
        d = monday + timedelta(days=i)
        pts += [{"device_user_id": "P1", "timestamp": f"{d}T09:25:00"},
                {"device_user_id": "P1", "timestamp": f"{d}T{'20:30' if i == 0 else '18:35'}:00"}]
    push(client, dev["api_key"], pts)
    rows = company_a.get("/attendance/reports/payroll", params={"start": monday.isoformat(),
                                                                "end": (monday + timedelta(days=6)).isoformat()}).json()["rows"]
    mine = next(r for r in rows if r["employee_id"] == emp["id"])
    assert mine["present_days"] == 5 and mine["weekly_offs"] == 2 and mine["payable_days"] == "7.00"
    assert mine["lop_days"] == "0.00" and mine["overtime_hours"] == "2.00"
    ot = company_a.get("/attendance/reports/overtime", params={"start": monday.isoformat(),
                                                               "end": (monday + timedelta(days=6)).isoformat()}).json()
    assert any(r["employee_id"] == emp["id"] and r["overtime_hours"] == 2.0 for r in ot["rows"])
    csv = company_a.get("/attendance/reports/muster", params={"year": monday.year, "month": monday.month, "format": "csv"})
    assert csv.status_code == 200 and "Payable days" in csv.text


def test_employee_sees_only_own_attendance(client, company_a):
    a = hire(company_a, "Esha")
    b = hire(company_a, "Farid")
    me = login_as(company_a, a)
    assert me.get("/attendance/team/today").status_code == 403
    assert me.get(f"/attendance/employees/{b['uuid']}/month", params={"year": 2026, "month": 1}).status_code == 403
    assert me.post("/attendance/locks", json={"start": "2026-01-01", "end": "2026-01-31"}).status_code == 403
    assert me.post("/shifts/", json={"name": "Mine", "start_time": "09:00", "end_time": "17:00"}).status_code == 403
    assert me.get("/attendance/devices").status_code == 403
    # a manager sees the team
    boss = hire(company_a, "Bhaskar", role="manager")
    company_a.put(f"/employees/{a['uuid']}", json={"reporting_manager_id": boss["id"]})
    mgr = login_as(company_a, boss)
    board = mgr.get("/attendance/team/today")
    assert board.status_code == 200 and [p["employee_id"] for p in board.json()["people"]] == [a["id"]]


def test_timesheet_line_level_approval(client, company_a):
    emp = hire(company_a, "Tara")
    p = company_a.post("/timesheets/projects", json={"name": "Portal", "billable": True, "budget_hours": 100}).json()
    task = company_a.post(f"/timesheets/projects/{p['uuid']}/tasks", json={"name": "Build"}).json()
    me = login_as(company_a, emp)
    monday = last_week_monday()
    week = me.put("/timesheets/me/week", json={"week_start": monday.isoformat(), "rows": [
        {"project_id": p["id"], "task_id": task["id"], "hours": {monday.isoformat(): 6,
                                                                 (monday + timedelta(days=1)).isoformat(): 7}}]})
    assert week.status_code == 200, week.text
    assert week.json()["total"] == "13.00"
    sent = me.post("/timesheets/me/submit", json={"week_start": monday.isoformat()})
    assert sent.status_code == 200 and sent.json()["status"] == "submitted"
    pending = company_a.get("/timesheets/approvals").json()
    ts = next(t for t in pending if t["employee_id"] == emp["id"])
    detail = company_a.get(f"/timesheets/{ts['uuid']}").json()
    assert detail["can_review"] and len(detail["lines"]) == 2
    l1, l2 = detail["lines"]
    r = company_a.post(f"/timesheets/{ts['uuid']}/review", json={"lines": [
        {"log_uuid": l1["uuid"], "decision": "approve"},
        {"log_uuid": l2["uuid"], "decision": "reject", "comment": "Too many hours"}]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "partly_approved" and r.json()["total_hours"] == "6.00"
    # the rejected line can be fixed and sent again
    again = me.get("/timesheets/me/week", params={"start": monday.isoformat()}).json()
    assert again["editable"]


def test_tenant_isolation_for_attendance_tables(client, company_a, company_b):
    dev_b = make_device(company_b, "B gate")
    emp_b = hire(company_b, "Bina")
    enrol(company_b, emp_b, "1")
    push(client, dev_b["api_key"], [{"device_user_id": "1", "timestamp": f"{last_week_monday()}T09:30:00"}])
    proj_b = company_b.post("/timesheets/projects", json={"name": "Secret"}).json()
    shift_b = company_b.post("/shifts/", json={"name": "B only", "start_time": "07:00", "end_time": "15:00"}).json()
    # company A sees none of it
    assert all(d["uuid"] != dev_b["uuid"] for d in company_a.get("/attendance/devices").json()["items"])
    assert company_a.get(f"/attendance/devices/{dev_b['uuid']}").status_code == 404
    assert all(p["uuid"] != proj_b["uuid"] for p in company_a.get("/timesheets/projects").json())
    assert company_a.get(f"/timesheets/projects/{proj_b['uuid']}").status_code == 404
    assert company_a.put(f"/shifts/{shift_b['uuid']}", json={"name": "Taken"}).status_code == 404
    assert all(s["uuid"] != shift_b["uuid"] for s in company_a.get("/shifts/").json())
    b_rows = company_b.get("/attendance/records", params={"start": "2020-01-01", "end": "2020-03-01"})
    assert b_rows.status_code == 200
    rec = day_row(company_b, emp_b, last_week_monday())
    assert company_a.get(f"/attendance/records/{rec['uuid']}").status_code == 404
    # A's device key can't write into B and enrolments don't cross
    dev_a = make_device(company_a)
    r = push(client, dev_a["api_key"], [{"device_user_id": "1", "timestamp": f"{last_week_monday()}T10:00:00"}]).json()
    assert r["inserted"] == 0 and r["unmatched"] == 1
    # row-level security on a direct session
    db = tenant_session(company_a.id)
    try:
        assert db.query(AttendanceDay).filter(AttendanceDay.employee_id == emp_b["id"]).count() == 0
        n = db.execute(text("SELECT count(*) FROM attendance_punches WHERE company_id = :b"), {"b": company_b.id}).scalar()
        assert n == 0
    finally:
        db.close()


def test_approved_leave_from_the_leave_module_shows_as_on_leave(client, company_a):
    types = {t["code"]: t for t in company_a.get("/leave/types").json()}
    if "CL" not in types:
        pytest.skip("leave module has no casual leave type")
    emp = hire(company_a, "Leela")
    company_a.post("/leave/balances/adjust", json={"employee_uuid": emp["uuid"], "leave_type_uuid": types["CL"]["uuid"],
                                                   "days": 5, "reason": "test", "year": today_ist().year})
    d = today_ist() - timedelta(days=1)
    while d.weekday() >= 5:  # the latest weekday (leave can be asked for up to 3 days back)
        d -= timedelta(days=1)
    me = login_as(company_a, emp)
    r = me.post("/leave/requests", json={"leave_type_uuid": types["CL"]["uuid"], "from_date": d.isoformat(),
                                         "to_date": d.isoformat(), "reason": "Family function"})
    assert r.status_code == 201, r.text
    approve_all(company_a)
    company_a.post("/attendance/recompute", json={"start": d.isoformat(), "end": d.isoformat(), "employee_ids": [emp["id"]]})
    row = day_row(company_a, emp, d)
    assert row["status"] == "on_leave" and row["payable_days"] == "1.00"
