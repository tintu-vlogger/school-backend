"""Timetable: bell schedules, clash detection, combined and elective classes, generator,
copy, publish, substitutions, portal access and tenant isolation."""
from datetime import date, timedelta

from tests.conftest import login_as
from tests.school_helpers import portal_login, setup_school


def _next(weekday: int) -> date:
    d = date.today()
    while d.isoweekday() != weekday:
        d += timedelta(days=1)
    return d


def _school(c):
    school = setup_school(c)
    # One simple schedule for every day: 4 periods and a break.
    r = c.post("/timetable/bell-schedules", json={"name": "Test day", "weekdays": [1, 2, 3, 4, 5, 6, 7], "is_default": True,
                                                  "periods": [{"kind": "class", "start": "09:00", "end": "09:40"},
                                                              {"kind": "class", "start": "09:40", "end": "10:20"},
                                                              {"kind": "break", "start": "10:20", "end": "10:40", "name": "Break"},
                                                              {"kind": "class", "start": "10:40", "end": "11:20"},
                                                              {"kind": "class", "start": "11:20", "end": "12:00"}]})
    assert r.status_code == 201, r.text
    assert [p["no"] for p in r.json()["periods"]] == [1, 2, None, 3, 4]
    return school


def _room(c, name):
    r = c.post("/academics/rooms", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_bell_schedule_validation(company_a):
    r = company_a.post("/timetable/bell-schedules", json={"name": "Bad", "weekdays": [1], "periods": [
        {"kind": "class", "start": "09:00", "end": "09:45"}, {"kind": "class", "start": "09:30", "end": "10:00"}]})
    assert r.status_code == 400 and "overlap" in r.json()["detail"]


def test_clash_detection(company_a):
    s = _school(company_a)
    t0, t1 = s["teachers"][0]["id"], s["teachers"][1]["id"]
    m, e = s["subject_ids"]
    room = _room(company_a, "Lab 1")
    r = company_a.post("/timetable/entries", json={"section_id": s["section_id"], "weekday": 1, "period_no": 1,
                                                   "subject_id": m, "teacher_id": t0, "room_id": room})
    assert r.status_code == 201, r.text
    # the same teacher in another section at the same time
    r = company_a.post("/timetable/entries", json={"section_id": s["section2_id"], "weekday": 1, "period_no": 1,
                                                   "subject_id": m, "teacher_id": t0})
    assert r.status_code == 409 and "already teaches" in r.json()["detail"]
    # the same room
    r = company_a.post("/timetable/entries", json={"section_id": s["section2_id"], "weekday": 1, "period_no": 1,
                                                   "subject_id": e, "teacher_id": t1, "room_id": room})
    assert r.status_code == 409 and "Lab 1" in r.json()["detail"]
    # saved anyway: the clash is listed
    r = company_a.post("/timetable/entries", json={"section_id": s["section2_id"], "weekday": 1, "period_no": 1,
                                                   "subject_id": m, "teacher_id": t0, "force": True})
    assert r.status_code == 201
    kinds = [c["kind"] for c in company_a.get("/timetable/clashes").json()["clashes"]]
    assert "teacher" in kinds
    grid = company_a.get(f"/timetable/grid?section_id={s['section_id']}").json()
    assert any(c["kind"] == "teacher" for c in grid["clashes"])
    # a period that does not exist
    r = company_a.post("/timetable/entries", json={"section_id": s["section_id"], "weekday": 1, "period_no": 9, "subject_id": m})
    assert r.status_code == 400


def test_combined_class_and_electives_are_not_clashes(company_a):
    s = _school(company_a)
    t0, t1 = s["teachers"][0]["id"], s["teachers"][1]["id"]
    m, e = s["subject_ids"]
    r = company_a.post("/timetable/entries", json={"section_ids": [s["section_id"], s["section2_id"]], "weekday": 2,
                                                   "period_no": 2, "subject_id": m, "teacher_id": t0})
    assert r.status_code == 201 and len(r.json()) == 2 and r.json()[0]["group_key"]
    # two electives in one cell
    for subj, t in ((m, t1), (e, None)):
        r = company_a.post("/timetable/entries", json={"section_id": s["section_id"], "weekday": 3, "period_no": 1,
                                                       "subject_id": subj, "teacher_id": t, "elective_group": "Option A"})
        assert r.status_code == 201, r.text
    assert company_a.get("/timetable/clashes").json()["clashes"] == []
    # the teacher's day shows the combined class once
    week = company_a.get(f"/timetable/teacher?employee_id={t0}").json()
    assert len([x for x in week["entries"] if x["weekday"] == 2]) == 2  # one row per section in the week view
    # a plain subject put in the cell replaces what was there
    r = company_a.post("/timetable/entries", json={"section_id": s["section_id"], "weekday": 3, "period_no": 1,
                                                   "subject_id": e, "teacher_id": t0})
    assert r.status_code == 201
    grid = company_a.get(f"/timetable/grid?section_id={s['section_id']}").json()
    assert len([x for x in grid["entries"] if x["weekday"] == 3 and x["period_no"] == 1]) == 1


def test_generate_copy_and_publish(company_a):
    s = _school(company_a)
    r = company_a.post("/timetable/generate", json={"section_ids": [s["section_id"], s["section2_id"]]})
    assert r.status_code == 200, r.text
    assert r.json()["placed"] == 12 and r.json()["unplaced"] == []
    assert [c for c in company_a.get("/timetable/clashes").json()["clashes"] if c["severity"] == "error"] == []
    alloc = company_a.get(f"/timetable/grid?section_id={s['section_id']}").json()["allocation"]
    assert all(a["placed"] == 3 for a in alloc)
    # copy A over B with B's own teachers
    r = company_a.post("/timetable/copy", json={"from_section_id": s["section_id"], "to_section_ids": [s["section2_id"]]})
    assert r.status_code == 200 and r.json()["copied"] == 6
    # parents only see published timetables
    parent = portal_login(company_a, s["parent_login"])
    kid = s["students"][0]
    assert parent.get(f"/timetable/portal/week?student_id={kid}").json()["published"] is False
    assert company_a.post("/timetable/publish", json={"section_ids": [s["section_id"]]}).json()["published"] == 1
    week = parent.get(f"/timetable/portal/week?student_id={kid}").json()
    assert week["published"] is True and len(week["entries"]) == 6
    assert all("teacher_id" not in e for e in week["entries"])
    # changes after publishing are flagged
    entry = week["entries"][0]
    assert company_a.delete(f"/timetable/entries/{entry['uuid']}").status_code == 200
    secs = {x["id"]: x for x in company_a.get("/timetable/sections").json()}
    assert secs[s["section_id"]]["changed"] is True


def test_substitution_suggests_free_teachers(company_a):
    s = _school(company_a)
    extra = company_a.add_employee("Free", "Teacher")
    t0, t1 = s["teachers"][0]["id"], s["teachers"][1]["id"]
    m, e = s["subject_ids"]
    day = _next(4)
    company_a.post("/timetable/entries", json={"section_id": s["section_id"], "weekday": 4, "period_no": 1, "subject_id": m, "teacher_id": t0})
    company_a.post("/timetable/entries", json={"section_id": s["section2_id"], "weekday": 4, "period_no": 1, "subject_id": e, "teacher_id": t1})
    company_a.post("/timetable/entries", json={"section_id": s["section2_id"], "weekday": 4, "period_no": 2, "subject_id": m,
                                               "teacher_id": extra["id"]})
    company_a.post("/timetable/publish", json={"section_ids": [s["section_id"], s["section2_id"]]})
    periods = company_a.get(f"/timetable/substitutions/periods?on={day.isoformat()}&teacher_id={t0}").json()
    assert len(periods) == 1
    suggested = [x["employee_id"] for x in periods[0]["suggestions"]]
    assert extra["id"] in suggested and t1 not in suggested and t0 not in suggested
    # a busy teacher is refused unless forced
    r = company_a.post("/timetable/substitutions", json={"entry_uuid": periods[0]["uuid"], "date": day.isoformat(),
                                                         "substitute_teacher_id": t1})
    assert r.status_code == 409
    r = company_a.post("/timetable/substitutions", json={"entry_uuid": periods[0]["uuid"], "date": day.isoformat(),
                                                         "substitute_teacher_id": extra["id"]})
    assert r.status_code == 201, r.text
    # the substitute's day has the class; the absent teacher's no longer does
    sub = login_as(company_a, extra)
    classes = sub.get(f"/timetable/mine/day?on={day.isoformat()}").json()["classes"]
    assert any(c["is_substitution"] and c["period_no"] == 1 for c in classes)
    absent = login_as(company_a, s["teachers"][0])
    assert absent.get(f"/timetable/mine/day?on={day.isoformat()}").json()["classes"] == []
    reg = company_a.get(f"/timetable/substitutions?date_from={day.isoformat()}&date_to={day.isoformat()}").json()
    assert reg["total"] == 1 and reg["items"][0]["substitute_teacher_name"] == "Free Teacher"
    # the wrong weekday is refused
    r = company_a.post("/timetable/substitutions", json={"entry_uuid": periods[0]["uuid"], "date": (day + timedelta(days=1)).isoformat(),
                                                         "substitute_teacher_id": extra["id"]})
    assert r.status_code == 400


def test_portal_isolation(company_a):
    s = _school(company_a)
    company_a.post("/timetable/generate", json={"section_ids": [s["section_id"], s["section2_id"]]})
    company_a.post("/timetable/publish", json={"section_ids": [s["section_id"], s["section2_id"]]})
    parent = portal_login(company_a, s["parent_login"])
    assert parent.get(f"/timetable/portal/summary?student_id={s['students'][0]}").status_code == 200
    for sid in (s["other_student"], s["students"][1]):
        assert parent.get(f"/timetable/portal/week?student_id={sid}").status_code == 404
        assert parent.get(f"/timetable/portal/summary?student_id={sid}").status_code == 404
    assert parent.get(f"/timetable/grid?section_id={s['section_id']}").status_code == 403
    assert parent.get("/timetable/mine").status_code == 403
    # a teacher can view but not change the timetable
    teacher = login_as(company_a, s["teachers"][0])
    assert teacher.get(f"/timetable/grid?section_id={s['section_id']}").status_code == 200
    assert teacher.post("/timetable/generate", json={"section_ids": [s["section_id"]]}).status_code == 403


def test_tenant_isolation(company_a, company_b):
    s = _school(company_a)
    _school(company_b)
    r = company_a.post("/timetable/entries", json={"section_id": s["section_id"], "weekday": 5, "period_no": 1,
                                                   "subject_id": s["subject_ids"][0]})
    entry = r.json()[0]
    assert company_b.delete(f"/timetable/entries/{entry['uuid']}").status_code == 404
    assert company_b.put(f"/timetable/entries/{entry['uuid']}", json={"period_no": 2}).status_code == 404
    assert company_b.get(f"/timetable/grid?section_id={s['section_id']}").status_code == 404
    r = company_b.post("/timetable/entries", json={"section_id": s["section_id"], "weekday": 5, "period_no": 1})
    assert r.status_code == 404
    sched = company_a.get("/timetable/bell-schedules").json()[0]
    assert company_b.put(f"/timetable/bell-schedules/{sched['uuid']}", json={"name": "Mine"}).status_code == 404
    assert sched["uuid"] not in {x["uuid"] for x in company_b.get("/timetable/bell-schedules").json()}
