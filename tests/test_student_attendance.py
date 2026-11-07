"""Student attendance: percentages, locking and corrections, leave pre-fill through the
workflow, holidays, portal access and tenant isolation."""
from datetime import date, timedelta

from modules.student_attendance.service import compute_percentage
from tests.conftest import login_as
from tests.school_helpers import portal_login, setup_school


def _today(c) -> date:
    return date.fromisoformat(c.get("/student-attendance/summary").json()["date"])


def _mark(session, school, day, codes, reason=None):
    marks = [{"student_id": sid, "code": code} for sid, code in zip(school["students"], codes)]
    body = {"section_id": school["section_id"], "date": day.isoformat(), "slot": "day", "marks": marks}
    if reason:
        body["reason"] = reason
    return session.post("/student-attendance/register", json=body)


def test_percentage_formula():
    assert compute_percentage([]) == {"present": 0, "total": 0, "pct": None}
    # present, absent, half day, late (counts present) -> 2.5 of 4
    r = compute_percentage([(1, True), (0, True), (0.5, True), (1, True)])
    assert r == {"present": 2.5, "total": 4, "pct": 62.5}
    # a status left out of the total does not count either way
    assert compute_percentage([(1, True), (0, False)])["pct"] == 100.0


def test_register_and_learner_percentages(company_a):
    school = setup_school(company_a)
    today = _today(company_a)
    for i, codes in enumerate([("P", "A", "P"), ("P", "HD", "L"), ("P", "A", "P"), ("A", "P", "P")]):
        r = _mark(company_a, school, today - timedelta(days=i), codes)
        assert r.status_code == 200, r.text
    reg = company_a.get(f"/student-attendance/register?section_id={school['section_id']}&on={today.isoformat()}").json()
    assert reg["register"]["total"] == 3 and reg["register"]["absent"] == 1
    rows = company_a.get(f"/student-attendance/reports/percentages?section_id={school['section_id']}"
                         f"&date_from={(today - timedelta(days=10)).isoformat()}&date_to={today.isoformat()}").json()["rows"]
    by = {r["student_id"]: r for r in rows}
    assert by[school["students"][0]]["pct"] == 75.0  # 3 of 4
    assert by[school["students"][1]]["pct"] == 37.5  # 0 + 0.5 + 0 + 1 of 4
    assert by[school["students"][2]]["pct"] == 100.0  # late counts as present
    assert by[school["students"][2]]["late"] == 1
    # defaulters below 75%
    d = company_a.get("/student-attendance/reports/defaulters?threshold=75&"
                      f"date_from={(today - timedelta(days=10)).isoformat()}&date_to={today.isoformat()}").json()
    assert [r["student_id"] for r in d["rows"]] == [school["students"][1]]
    # monthly register grid and CSV
    m = company_a.get(f"/student-attendance/reports/monthly?section_id={school['section_id']}&month={today:%Y-%m}").json()
    assert m["rows"][0]["cells"][today.isoformat()] == "P"
    csv = company_a.get(f"/student-attendance/reports/monthly?section_id={school['section_id']}&month={today:%Y-%m}&format=csv")
    assert csv.status_code == 200 and "Kid0" in csv.text


def test_teacher_marks_only_own_section_and_lock(company_a):
    school = setup_school(company_a)
    company_a.put("/student-attendance/settings", json={"lock_after_days": 2})
    teacher = login_as(company_a, school["teachers"][0])
    other = login_as(company_a, school["teachers"][1])
    today = _today(company_a)
    assert _mark(teacher, school, today, ("P", "P", "A")).status_code == 200
    # not the class teacher
    assert _mark(other, school, today, ("P", "P", "P")).status_code == 403
    # future dates are refused
    assert _mark(teacher, school, today + timedelta(days=1), ("P", "P", "P")).status_code == 400
    # within the lock window the class teacher can still change it
    assert _mark(teacher, school, today - timedelta(days=2), ("P", "P", "P")).status_code == 200
    # older registers are locked for the teacher
    old = today - timedelta(days=3)
    r = _mark(teacher, school, old, ("P", "P", "P"))
    assert r.status_code == 403 and "locked" in r.json()["detail"]
    # the office corrects with a reason; the change is logged
    assert _mark(company_a, school, old, ("P", "A", "P")).status_code == 400
    assert _mark(company_a, school, old, ("P", "A", "P"), reason="Register found on paper").status_code == 200
    r = _mark(company_a, school, old, ("P", "P", "P"), reason="Learner was at the nurse")
    assert r.status_code == 200
    corr = company_a.get("/student-attendance/corrections").json()["items"]
    assert any(c["old_code"] == "A" and c["new_code"] == "P" and c["reason"] == "Learner was at the nurse" for c in corr)


def test_holiday_is_respected(company_a):
    school = setup_school(company_a)
    today = _today(company_a)
    day = today - timedelta(days=1)
    r = company_a.post("/student-attendance/calendar", json={"date": day.isoformat(), "kind": "holiday", "name": "Founders' day"})
    assert r.status_code == 201, r.text
    r = _mark(company_a, school, day, ("P", "P", "P"))
    assert r.status_code == 400 and "Founders' day" in r.json()["detail"]
    reg = company_a.get(f"/student-attendance/register?section_id={school['section_id']}&on={day.isoformat()}").json()
    assert reg["day"]["working"] is False and reg["can_edit"] is False


def test_leave_request_approved_by_class_teacher_prefills_register(company_a):
    school = setup_school(company_a)
    today = _today(company_a)
    kid = school["students"][0]
    # Absent yesterday already, then the parent asks for leave for yesterday and tomorrow.
    assert _mark(company_a, school, today - timedelta(days=1), ("A", "P", "P")).status_code == 200
    parent = portal_login(company_a, school["parent_login"])
    r = parent.post("/student-attendance/portal/leave", json={
        "student_id": kid, "from_date": (today - timedelta(days=1)).isoformat(),
        "to_date": (today + timedelta(days=1)).isoformat(), "reason": "Fever"})
    assert r.status_code == 201, r.text
    leave = r.json()
    assert leave["status"] == "pending" and leave["days"] == 3
    # Another teacher can't decide it; the class teacher can.
    other = login_as(company_a, school["teachers"][1])
    assert other.post(f"/student-attendance/leave/{leave['uuid']}/decide", json={"decision": "approve"}).status_code == 403
    teacher = login_as(company_a, school["teachers"][0])
    mine = teacher.get("/student-attendance/leave?mine=true").json()["items"]
    assert [x["uuid"] for x in mine] == [leave["uuid"]] and mine[0]["can_decide"]
    r = teacher.post(f"/student-attendance/leave/{leave['uuid']}/decide", json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    # Yesterday's absent became leave; today's register is pre-filled with leave.
    y = company_a.get(f"/student-attendance/register?section_id={school['section_id']}&on={(today - timedelta(days=1)).isoformat()}").json()
    row = next(s for s in y["students"] if s["student_id"] == kid)
    assert row["code"] == "EX" and row["source"] == "leave"
    t = teacher.get(f"/student-attendance/register?section_id={school['section_id']}&on={today.isoformat()}").json()
    row = next(s for s in t["students"] if s["student_id"] == kid)
    assert row["code"] == "EX" and row["leave"] is not None
    # "All present" keeps the leave.
    assert teacher.post("/student-attendance/register/all-present",
                        json={"section_id": school["section_id"], "date": today.isoformat()}).status_code == 200
    t = teacher.get(f"/student-attendance/register?section_id={school['section_id']}&on={today.isoformat()}").json()
    assert next(s for s in t["students"] if s["student_id"] == kid)["code"] == "EX"
    # The family sees the outcome.
    items = parent.get("/student-attendance/portal/leave").json()["items"]
    assert items[0]["status"] == "approved"


def test_absence_alert_reaches_the_parent(company_a):
    school = setup_school(company_a)
    today = _today(company_a)
    assert _mark(company_a, school, today, ("A", "P", "P")).status_code == 200
    parent = portal_login(company_a, school["parent_login"])
    notes = parent.get("/notifications/").json()
    items = notes if isinstance(notes, list) else notes.get("items", notes.get("data", []))
    assert any("absent" in (n.get("title") or "").lower() for n in items)
    # marking again does not alert twice
    assert _mark(company_a, school, today, ("P", "P", "P")).status_code == 200
    assert _mark(company_a, school, today, ("A", "P", "P")).status_code == 200
    notes = parent.get("/notifications/").json()
    items = notes if isinstance(notes, list) else notes.get("items", notes.get("data", []))
    assert sum("absent" in (n.get("title") or "").lower() for n in items) == 1


def test_portal_isolation(company_a):
    school = setup_school(company_a)
    today = _today(company_a)
    _mark(company_a, school, today, ("P", "A", "P"))
    parent = portal_login(company_a, school["parent_login"])
    mine = parent.get(f"/student-attendance/portal/summary?student_id={school['students'][0]}")
    assert mine.status_code == 200 and mine.json()["today"]["state"] == "present"
    assert parent.get(f"/student-attendance/portal/calendar?student_id={school['students'][0]}").status_code == 200
    # another family's child, and a classmate who is not theirs
    for sid in (school["other_student"], school["students"][1]):
        assert parent.get(f"/student-attendance/portal/summary?student_id={sid}").status_code == 404
        assert parent.get(f"/student-attendance/portal/calendar?student_id={sid}").status_code == 404
        r = parent.post("/student-attendance/portal/leave", json={"student_id": sid, "from_date": today.isoformat(),
                                                                  "to_date": today.isoformat(), "reason": "x"})
        assert r.status_code == 404
    # staff endpoints are closed to portal sign-ins
    assert parent.get("/student-attendance/overview").status_code == 403
    assert parent.get(f"/student-attendance/register?section_id={school['section_id']}&on={today.isoformat()}").status_code == 403
    other = portal_login(company_a, school["other_parent_login"])
    items = other.get("/student-attendance/portal/leave").json()["items"]
    assert items == []


def test_tenant_isolation(company_a, company_b):
    school = setup_school(company_a)
    setup_school(company_b)
    today = _today(company_a)
    assert _mark(company_a, school, today, ("P", "P", "P")).status_code == 200
    r = company_a.post("/student-attendance/leave", json={"student_id": school["students"][1], "from_date": today.isoformat(),
                                                          "to_date": today.isoformat(), "reason": "Office recorded"})
    assert r.status_code == 201, r.text
    leave = r.json()
    assert leave["status"] == "approved"
    assert company_b.get(f"/student-attendance/leave/{leave['uuid']}").status_code == 404
    assert company_b.post(f"/student-attendance/leave/{leave['uuid']}/cancel", json={}).status_code == 404
    assert all(x["uuid"] != leave["uuid"] for x in company_b.get("/student-attendance/leave").json()["items"])
    # B can't open or mark A's section
    assert company_b.get(f"/student-attendance/register?section_id={school['section_id']}&on={today.isoformat()}").status_code == 404
    assert _mark(company_b, school, today, ("A", "A", "A")).status_code == 404
    ov = company_b.get("/student-attendance/overview").json()
    assert school["section_id"] not in {s["section_id"] for s in ov["sections"]}
