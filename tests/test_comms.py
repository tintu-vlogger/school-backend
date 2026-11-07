"""Communication module: audiences, read receipts, who may message whom, PTM booking rules
and tenant isolation."""
import uuid
from datetime import date, timedelta

import pytest

from tests.conftest import Company, login_as


class Portal:
    def __init__(self, company: Company, login: str, password: str = "Welcome9#x"):
        r = company.client.post("/api/v1/auth/login", json={"email": login, "password": password},
                                headers={"X-Company-Slug": company.slug})
        assert r.status_code == 200, r.text
        self.client, self.slug, self.token = company.client, company.slug, r.json()["access_token"]

    headers = Company.headers
    get, post, put = Company.get, Company.post, Company.put


def _setup(company: Company):
    """Two sections of one class, a class teacher for A, another teacher, and one family per section."""
    r = company.post("/academics/quick-setup", json={"programs": ["Grade 8"], "sections": ["A", "B"],
                                                     "subjects": ["Maths"]})
    assert r.status_code == 200, r.text
    secs = {s["name"]: s for s in company.get("/academics/sections?size=50").json()["items"]}
    teacher = company.add_employee("Divya", "Menon")
    other = company.add_employee("Ravi", "Rao")
    _make_teachers(company, [teacher["id"], other["id"]])
    assert company.put(f"/academics/sections/{secs['A']['uuid']}", json={"class_teacher_id": teacher["id"]}).status_code == 200
    assert company.put(f"/academics/sections/{secs['B']['uuid']}", json={"class_teacher_id": other["id"]}).status_code == 200
    fams = {}
    for name in ("A", "B"):
        tag = uuid.uuid4().hex[:6]
        r = company.post("/students", json={
            "first_name": f"Kid{name}", "last_name": tag, "section_id": secs[name]["id"],
            "email": f"kid{name.lower()}-{tag}@example.com",
            "guardians": [{"first_name": f"Parent{name}", "last_name": tag, "email": f"p{name.lower()}-{tag}@example.com",
                           "relation": "father", "is_primary": True}]})
        assert r.status_code == 201, r.text
        st = r.json()
        g = st["guardians"][0]
        acc = company.post(f"/guardians/{g['guardian_uuid']}/portal-account", json={"password": "Welcome9#x"})
        assert acc.status_code == 200, acc.text
        sacc = company.post(f"/students/{st['uuid']}/portal-accounts")
        assert sacc.status_code == 200, sacc.text
        slogin = next(x for x in sacc.json()["new_logins"] if x["kind"] == "student")
        company.post(f"/portal-accounts/{_acc_uuid(company, slogin['login'])}/reset-password", json={"password": "Welcome9#x"})
        fams[name] = {"student": st, "guardian": g, "parent_login": acc.json()["login"], "student_login": slogin["login"]}
    return secs, teacher, other, fams


def _make_teachers(company, ids):
    """The staff API only creates admin/hr/manager/employee; school roles are set directly."""
    from core.db import tenant_session
    from modules.employees.models import Employee
    db = tenant_session(company.id)
    try:
        db.query(Employee).filter(Employee.id.in_(ids)).update({"role": "teacher"}, synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _acc_uuid(company, login):
    rows = company.get(f"/portal-accounts?q={login}").json()["items"]
    return next(r["uuid"] for r in rows if r["login"] == login)


@pytest.fixture
def school(company_a):
    secs, teacher, other, fams = _setup(company_a)
    return {"c": company_a, "secs": secs, "teacher": teacher, "other": other, "fams": fams}


def _notice(company, audience, **extra):
    r = company.post("/comms/notices", json={"title": "Sports day", "body": "Wear **white**.", "audience": audience, **extra})
    assert r.status_code == 201, r.text
    p = company.post(f"/comms/notices/{r.json()['uuid']}/publish", json={"when": "now"})
    assert p.status_code == 200, p.text
    return p.json()


def test_section_parents_get_notice_others_dont(school):
    c, secs, fams = school["c"], school["secs"], school["fams"]
    n = _notice(c, {"learners": {"parents": True, "scope": "sections", "section_ids": [secs["A"]["id"]]}})
    assert n["recipient_count"] == 1 and n["audience_label"].startswith("Parents of")
    pa = Portal(c, fams["A"]["parent_login"])
    pb = Portal(c, fams["B"]["parent_login"])
    sa = Portal(c, fams["A"]["student_login"])
    assert [x["uuid"] for x in pa.get("/comms/portal/notices").json()["items"]] == [n["uuid"]]
    assert pb.get("/comms/portal/notices").json()["items"] == []
    assert sa.get("/comms/portal/notices").json()["items"] == []  # parents only
    assert pb.get(f"/comms/portal/notices/{n['uuid']}").status_code == 404


def test_read_receipts_and_acknowledgement(school):
    c, secs, fams = school["c"], school["secs"], school["fams"]
    n = _notice(c, {"learners": {"parents": True, "students": True, "scope": "all"}}, require_ack=True)
    assert n["stats"]["recipients"] == 4 and n["stats"]["read"] == 0
    pa = Portal(c, fams["A"]["parent_login"])
    assert pa.get("/comms/portal/unread").json()["notices"] == 1
    assert pa.get(f"/comms/portal/notices/{n['uuid']}").json()["read_at"]
    assert pa.post(f"/comms/portal/notices/{n['uuid']}/acknowledge").json()["acknowledged_at"]
    rec = c.get(f"/comms/notices/{n['uuid']}/receipts").json()
    assert rec["stats"] == {"recipients": 4, "read": 1, "acknowledged": 1}
    unread = c.get(f"/comms/notices/{n['uuid']}/receipts?status=unread").json()
    assert unread["total"] == 3
    assert c.post(f"/comms/notices/{n['uuid']}/resend").json()["sent"] == 3


def test_teacher_can_only_notify_own_sections(school):
    c, secs, teacher = school["c"], school["secs"], school["teacher"]
    t = login_as(c, teacher)
    ok = t.post("/comms/notices", json={"title": "Homework", "audience": {"learners": {
        "parents": True, "scope": "sections", "section_ids": [secs["A"]["id"]]}}})
    assert ok.status_code == 201, ok.text
    bad = t.post("/comms/notices", json={"title": "Hi all", "audience": {"everyone": True}})
    assert bad.status_code == 403
    bad2 = t.post("/comms/notices", json={"title": "B", "audience": {"learners": {
        "parents": True, "scope": "sections", "section_ids": [secs["B"]["id"]]}}})
    assert bad2.status_code == 403


def test_parent_can_only_message_linked_staff(school):
    c, teacher, other, fams = school["c"], school["teacher"], school["other"], school["fams"]
    pa = Portal(c, fams["A"]["parent_login"])
    contacts = pa.get("/comms/portal/messages/contacts").json()
    assert [s["employee_id"] for s in contacts["staff"]] == [teacher["id"]]
    ok = pa.post("/comms/portal/messages/threads", json={"employee_id": teacher["id"], "subject": "Hello",
                                                          "body": "About homework"})
    assert ok.status_code == 201, ok.text
    no = pa.post("/comms/portal/messages/threads", json={"employee_id": other["id"], "subject": "Hi", "body": "x"})
    assert no.status_code == 403
    office = pa.post("/comms/portal/messages/threads", json={"to_office": True, "subject": "Fees", "body": "Query"})
    assert office.status_code == 201
    # the teacher sees it as unread, replies, and the parent sees the reply
    t = login_as(c, teacher)
    assert t.get("/comms/unread").json()["messages"] == 1
    th = t.get(f"/comms/messages/threads/{ok.json()['uuid']}").json()
    assert th["messages"][0]["body"] == "About homework"
    assert t.post(f"/comms/messages/threads/{th['uuid']}/messages", json={"body": "Sure"}).status_code == 201
    assert pa.get("/comms/portal/unread").json()["messages"] == 1
    # another teacher can't read it; the owner (audit) can, read-only
    assert login_as(c, other).get(f"/comms/messages/threads/{th['uuid']}").status_code == 404
    assert c.get(f"/comms/messages/threads/{th['uuid']}").json()["can_reply"] is False
    # teachers may write only to families of their own sections
    assert t.post("/comms/messages/threads", json={"student_id": fams["B"]["student"]["id"], "subject": "x",
                                                   "body": "y"}).status_code == 403


def test_students_message_only_staff(school):
    c, teacher, fams = school["c"], school["teacher"], school["fams"]
    sa = Portal(c, fams["A"]["student_login"])
    # there is no way to address another learner: only staff ids or the office are accepted
    other_student = fams["B"]["student"]["id"]
    r = sa.post("/comms/portal/messages/threads", json={"employee_id": other_student, "subject": "hey", "body": "x"})
    assert r.status_code == 403
    ok = sa.post("/comms/portal/messages/threads", json={"employee_id": teacher["id"], "subject": "Doubt", "body": "Q1?"})
    assert ok.status_code == 201
    assert ok.json()["party_kind"] == "student"
    # switched off: students can't start or reply
    assert c.put("/comms/settings", json={"students_can_message": False}).status_code == 200
    assert sa.post("/comms/portal/messages/threads", json={"employee_id": teacher["id"], "subject": "x",
                                                            "body": "y"}).status_code == 403
    assert sa.post(f"/comms/portal/messages/threads/{ok.json()['uuid']}/messages", json={"body": "z"}).status_code == 403
    # a parent of another family can't open the learner's thread
    pb = Portal(c, fams["B"]["parent_login"])
    assert pb.get(f"/comms/portal/messages/threads/{ok.json()['uuid']}").status_code == 404


def test_ptm_double_booking_prevented(school):
    c, secs, teacher, fams = school["c"], school["secs"], school["teacher"], school["fams"]
    day = (date.today() + timedelta(days=5)).isoformat()
    r = c.post("/comms/ptm/days", json={"title": "Term 1 PTM", "date": day, "start_time": "09:00", "end_time": "10:00",
                                        "slot_minutes": 15, "breaks": [{"start": "09:30", "end": "09:45"}],
                                        "section_ids": [secs["A"]["id"], secs["B"]["id"]]})
    assert r.status_code == 201, r.text
    d = r.json()
    me = next(t for t in d["teachers"] if t["employee_id"] == teacher["id"])
    assert me["slots"] == 3  # 09:00, 09:15, 09:45 (break skipped)
    assert c.post(f"/comms/ptm/days/{d['uuid']}/status", json={"status": "open"}).status_code == 200
    pa = Portal(c, fams["A"]["parent_login"])
    sid = fams["A"]["student"]["id"]
    days = pa.get(f"/comms/portal/ptm?student_id={sid}").json()
    tch = days[0]["teachers"][0]
    assert tch["employee_id"] == teacher["id"]
    slot = tch["slots"][0]["id"]
    b = pa.post("/comms/portal/ptm/book", json={"slot_id": slot, "student_id": sid})
    assert b.status_code == 201, b.text
    # same child, same teacher again -> refused
    again = pa.post("/comms/portal/ptm/book", json={"slot_id": tch["slots"][1]["id"], "student_id": sid})
    assert again.status_code == 409
    # family B cannot book family A's teacher (not their child's teacher) or another child
    pb = Portal(c, fams["B"]["parent_login"])
    assert pb.post("/comms/portal/ptm/book", json={"slot_id": slot, "student_id": sid}).status_code == 404
    assert pb.post("/comms/portal/ptm/book", json={"slot_id": slot, "student_id": fams["B"]["student"]["id"]}).status_code == 400
    # the database itself refuses a second live booking of one slot
    from sqlalchemy.exc import IntegrityError
    from core.db import tenant_session
    from modules.comms.models import PtmBooking
    db = tenant_session(c.id)
    try:
        first = db.query(PtmBooking).filter(PtmBooking.slot_id == slot).one()
        db.add(PtmBooking(company_id=c.id, ptm_day_id=first.ptm_day_id, ptm_teacher_id=first.ptm_teacher_id,
                          slot_id=slot, student_id=fams["B"]["student"]["id"]))
        with pytest.raises(IntegrityError):
            db.flush()
    finally:
        db.rollback()
        db.close()
    # cancel, then the slot is free again
    assert pa.post(f"/comms/portal/ptm/bookings/{b.json()['uuid']}/cancel").status_code == 200
    assert pa.post("/comms/portal/ptm/book", json={"slot_id": slot, "student_id": sid}).status_code == 201


def test_events_audience_and_rsvp(school):
    c, secs, fams = school["c"], school["secs"], school["fams"]
    d = (date.today() + timedelta(days=3)).isoformat()
    r = c.post("/comms/events", json={"title": "Grade 8A trip", "category": "trip", "start_date": d,
                                      "rsvp_required": True,
                                      "audience": {"learners": {"parents": True, "scope": "sections",
                                                                "section_ids": [secs["A"]["id"]]}}})
    assert r.status_code == 201, r.text
    pa, pb = Portal(c, fams["A"]["parent_login"]), Portal(c, fams["B"]["parent_login"])
    assert [e["title"] for e in pa.get("/comms/portal/events").json()["items"]] == ["Grade 8A trip"]
    assert pb.get("/comms/portal/events").json()["items"] == []
    ev = r.json()["uuid"]
    assert pa.post(f"/comms/portal/events/{ev}/rsvp", json={"response": "yes", "guests": 1}).status_code == 200
    assert pb.post(f"/comms/portal/events/{ev}/rsvp", json={"response": "yes"}).status_code == 404
    assert c.get(f"/comms/events/{ev}/rsvps").json()["counts"]["yes"] == 1
    feed = pa.get("/comms/portal/events/feed-link").json()["path"]
    ics = c.client.get(feed)
    assert ics.status_code == 200 and "Grade 8A trip" in ics.text
    assert c.client.get(feed.replace(".ics", "x.ics")).status_code == 404


def test_comms_tenant_isolation(school, company_b):
    c = school["c"]
    n = _notice(c, {"staff": {"scope": "all"}})
    r = company_b.post("/academics/quick-setup", json={"programs": ["Grade 1"], "sections": ["A"], "subjects": []})
    assert r.status_code == 200
    assert company_b.get("/comms/notices").json()["total"] == 0
    assert company_b.get(f"/comms/notices/{n['uuid']}").status_code == 404
    assert company_b.post(f"/comms/notices/{n['uuid']}/resend").status_code == 404
    assert company_b.get("/comms/messages/threads?box=all").json()["total"] == 0
    from core.db import tenant_session
    from modules.comms.models import Notice, NoticeRecipient
    db = tenant_session(company_b.id)
    try:
        assert db.query(Notice).count() == 0 and db.query(NoticeRecipient).count() == 0
    finally:
        db.close()
