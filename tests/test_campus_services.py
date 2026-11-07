"""Campus services: library, transport, hostel and wellbeing.

Covers the core rules (loan limits, fines, renewals, route seats, bed conflicts, gate passes
to authorised guardians only), portal access (a parent never sees another family's child)
and tenant isolation of every table of the four modules."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from core.db import tenant_session


# ── Helpers ──────────────────────────────────────────────────────────

def _setup(company):
    r = company.post("/academics/quick-setup", json={"institution_type": "school", "programs": ["Grade 5"], "sections": ["A"],
                                                     "subjects": ["Maths"]})
    assert r.status_code == 200, r.text
    sections = company.get("/academics/section-options").json()
    return sections[0]["id"]


def _student(company, section_id, first="Ravi", gender="male", guardians=None):
    r = company.post("/students", json={"first_name": first, "last_name": "Test", "gender": gender, "section_id": section_id,
                                        "guardians": guardians or []})
    assert r.status_code == 201, r.text
    return r.json()


def _parent_session(company, student, email):
    """Give the student's first guardian a portal login and sign in as them."""
    detail = company.get(f"/students/{student['uuid']}").json()
    g = detail["guardians"][0]
    r = company.post(f"/guardians/{g['guardian_uuid']}/portal-account", json={"login": email, "password": "Secret123#"})
    assert r.status_code == 200, r.text
    r = company.client.post("/api/v1/auth/login", json={"email": email, "password": "Secret123#"},
                            headers={"X-Company-Slug": company.slug})
    assert r.status_code == 200, r.text

    class S:
        pass
    s = S()
    s.client, s.slug, s.token = company.client, company.slug, r.json()["access_token"]
    from tests.conftest import Company
    s.headers = {"Authorization": f"Bearer {s.token}", "X-Company-Slug": s.slug}
    s.get = lambda path, **kw: s.client.get(f"/api/v1{path}", headers=s.headers, **kw)
    s.post = lambda path, json=None, **kw: s.client.post(f"/api/v1{path}", headers=s.headers, json=json, **kw)
    return s


def _guardian(name, email=None, can_pickup=True):
    return {"first_name": name, "last_name": "Test", "phone": "+91 9000000000", "email": email, "relation": "father",
            "is_primary": True, "can_pickup": can_pickup}


@pytest.fixture
def school(company_a):
    company_a.section_id = _setup(company_a)
    return company_a


# ── Library ──────────────────────────────────────────────────────────

def _book(company, title="Test Book", copies=3, **extra):
    r = company.post("/library/books", json={"title": title, "authors": "A. Writer", "copies": copies, "price": "300.00", **extra})
    assert r.status_code == 201, r.text
    book = company.get(f"/library/books/{r.json()['uuid']}").json()
    return book, [c["accession_no"] for c in book["copy_list"]]


def _policy(company, member_type, **changes):
    pol = next(p for p in company.get("/library/policies").json()["items"] if p["member_type"] == member_type)
    r = company.put(f"/library/policies/{pol['uuid']}", json=changes)
    assert r.status_code == 200, r.text


def test_fine_calculation_rules():
    from modules.library.models import LibraryPolicy
    from modules.library.service import compute_fine
    p = LibraryPolicy(fine_per_day=Decimal("2.00"), grace_days=2, max_fine=Decimal("15.00"))
    due = date(2026, 1, 10)
    assert compute_fine(p, due, date(2026, 1, 10)) == (0, Decimal("0.00"))
    assert compute_fine(p, due, date(2026, 1, 12)) == (2, Decimal("0.00"))  # inside the grace days
    assert compute_fine(p, due, date(2026, 1, 15)) == (5, Decimal("6.00"))  # 3 chargeable days x 2
    assert compute_fine(p, due, date(2026, 2, 20))[1] == Decimal("15.00")  # capped
    p.max_fine = None
    assert compute_fine(p, due, date(2026, 2, 20))[1] == Decimal(str((41 - 2) * 2)) + Decimal("0.00")


def test_loan_limits_and_rules(school):
    s = _student(school, school.section_id)
    _policy(school, "student", max_books=2)
    b1, acc1 = _book(school, "Book One")
    b2, acc2 = _book(school, "Book Two")
    b3, acc3 = _book(school, "Book Three")
    ref, accr = _book(school, "Atlas", copies=1, reference_only=True)
    card = s["admission_no"]
    assert school.post("/library/issue", json={"member": card, "copies": [accr[0]]}).status_code == 400  # reference only
    r = school.post("/library/issue", json={"member": card, "copies": [acc1[0]]})
    assert r.status_code == 200, r.text
    # same title twice is refused, and an issued copy can't go out again
    assert "already has a copy" in school.post("/library/issue", json={"member": card, "copies": [acc1[1]]}).json()["detail"]
    assert school.post("/library/issue", json={"member": card, "copies": [acc1[0]]}).status_code == 400
    assert school.post("/library/issue", json={"member": card, "copies": [acc2[0]]}).status_code == 200
    r = school.post("/library/issue", json={"member": card, "copies": [acc3[0]]})
    assert r.status_code == 400 and "limit is 2" in r.json()["detail"]
    # a personal override raises the limit
    member = school.get(f"/library/members/lookup?code={card}").json()
    school.put(f"/library/members/{member['uuid']}", json={"max_books": 3})
    assert school.post("/library/issue", json={"member": card, "copies": [acc3[0]]}).status_code == 200
    assert school.get(f"/library/members/lookup?code={card}").json()["on_loan"] == 3


def _backdate(company, loan_uuid, days):
    db = tenant_session(company.id)
    try:
        db.execute(text("UPDATE library_loans SET due_on = due_on - :d, issued_on = issued_on - :d WHERE uuid = :u"),
                   {"d": days, "u": loan_uuid})
        db.commit()
    finally:
        db.close()


def test_return_creates_fine_and_waive_needs_reason(school):
    s = _student(school, school.section_id)
    _policy(school, "student", fine_per_day="2.00", grace_days=0, max_fine=None, block_fine_above="5.00")
    _b, acc = _book(school, "Late Book")
    loan = school.post("/library/issue", json={"member": s["admission_no"], "copies": [acc[0]]}).json()["issued"][0]
    _backdate(school, loan["uuid"], 18)  # 14-day loan, now 4 days late
    r = school.post("/library/return", json={"code": acc[0]})
    assert r.status_code == 200, r.text
    fine = r.json()["fines"][0]
    assert r.json()["late_days"] == 4 and fine["amount"] == "8.00" and fine["status"] == "pending"
    # unpaid fines above the limit block new issues
    blocked = school.post("/library/issue", json={"member": s["admission_no"], "copies": [acc[1]]})
    assert blocked.status_code == 400 and "Unpaid fines" in blocked.json()["detail"]
    assert school.post(f"/library/fines/{fine['uuid']}/settle", json={"action": "waive"}).status_code == 400
    r = school.post(f"/library/fines/{fine['uuid']}/settle", json={"action": "waive", "reason": "Was unwell"})
    assert r.status_code == 200 and r.json()["status"] == "waived"
    assert school.post("/library/issue", json={"member": s["admission_no"], "copies": [acc[1]]}).status_code == 200


def test_renew_rules(school):
    s1 = _student(school, school.section_id, "Asha", "female")
    s2 = _student(school, school.section_id, "Bala")
    _policy(school, "student", max_renewals=1, renew_when_overdue=False)
    _b, acc = _book(school, "Single Copy", copies=1)
    loan = school.post("/library/issue", json={"member": s1["admission_no"], "copies": [acc[0]]}).json()["issued"][0]
    r = school.post(f"/library/loans/{loan['uuid']}/renew")
    assert r.status_code == 200 and r.json()["renew_count"] == 1
    r = school.post(f"/library/loans/{loan['uuid']}/renew")
    assert r.status_code == 400 and "no renewals left" in r.json()["detail"]
    _policy(school, "student", max_renewals=5)
    # someone waiting for the only copy blocks renewal
    book = school.get("/library/books?q=Single").json()["items"][0]
    assert school.post("/library/reservations", json={"member": s2["admission_no"], "book_uuid": book["uuid"]}).status_code == 201
    r = school.post(f"/library/loans/{loan['uuid']}/renew")
    assert r.status_code == 400 and "waiting" in r.json()["detail"]
    # the return keeps the copy for the hold, and only that member can take it
    school.post("/library/return", json={"code": acc[0]})
    s3 = _student(school, school.section_id, "Chitra", "female")
    assert school.post("/library/issue", json={"member": s3["admission_no"], "copies": [acc[0]]}).status_code == 400
    assert school.post("/library/issue", json={"member": s2["admission_no"], "copies": [acc[0]]}).status_code == 200


def test_overdue_loan_cannot_be_renewed(school):
    s = _student(school, school.section_id)
    _b, acc = _book(school, "Overdue Book")
    loan = school.post("/library/issue", json={"member": s["admission_no"], "copies": [acc[0]]}).json()["issued"][0]
    _backdate(school, loan["uuid"], 20)
    r = school.post(f"/library/loans/{loan['uuid']}/renew")
    assert r.status_code == 400 and "overdue" in r.json()["detail"]
    overdue = school.get("/library/loans?overdue=true").json()
    assert overdue["total"] == 1 and overdue["items"][0]["overdue_days"] == 6


# ── Transport ────────────────────────────────────────────────────────

def _route(company, seats=2):
    v = company.post("/transport/vehicles", json={"registration_no": f"KA01{uuid.uuid4().hex[:4]}", "capacity": seats}).json()
    r = company.post("/transport/routes", json={"name": f"Route {uuid.uuid4().hex[:4]}", "vehicle_id": v["id"]})
    assert r.status_code == 201, r.text
    route = r.json()
    stop = company.post(f"/transport/routes/{route['uuid']}/stops", json={"name": "Main Gate", "pickup_time": "07:30",
                                                                          "fee": "1000.00"}).json()
    return route, stop


def test_route_capacity_and_route_change(school):
    route, stop = _route(school, seats=2)
    other, ostop = _route(school, seats=5)
    kids = [_student(school, school.section_id, f"Kid{i}") for i in range(3)]
    for k in kids[:2]:
        r = school.post("/transport/riders", json={"student_id": k["id"], "route_uuid": route["uuid"], "stop_uuid": stop["uuid"]})
        assert r.status_code == 201, r.text
        assert r.json()["fee_amount"] == "1000.00"
    r = school.post("/transport/riders", json={"student_id": kids[2]["id"], "route_uuid": route["uuid"], "stop_uuid": stop["uuid"]})
    assert r.status_code == 400 and "full" in r.json()["detail"]
    # a stop from another route is refused
    r = school.post("/transport/riders", json={"student_id": kids[2]["id"], "route_uuid": route["uuid"], "stop_uuid": ostop["uuid"]})
    assert r.status_code == 400
    # moving a rider frees the seat and keeps one live allocation; the learner flag follows
    start = (date.today() + timedelta(days=1)).isoformat()
    r = school.post("/transport/riders", json={"student_id": kids[0]["id"], "route_uuid": other["uuid"],
                                               "stop_uuid": ostop["uuid"], "start_date": start})
    assert r.status_code == 201, r.text
    hist = school.get(f"/transport/riders/for-student/{kids[0]['id']}").json()["history"]
    assert len(hist) == 2 and sum(1 for h in hist if h["end_date"] is None) == 1
    assert school.get(f"/students/{kids[0]['uuid']}").json()["uses_transport"] is True
    r = school.post("/transport/riders", json={"student_id": kids[2]["id"], "route_uuid": route["uuid"], "stop_uuid": stop["uuid"],
                                               "start_date": start})
    assert r.status_code == 201, r.text
    rider = r.json()
    school.post(f"/transport/riders/{rider['uuid']}/end", json={"end_date": (date.today() - timedelta(days=1)).isoformat()})
    assert school.get(f"/students/{kids[2]['uuid']}").json()["uses_transport"] is False


# ── Hostel ───────────────────────────────────────────────────────────

def _room(company, gender="boys", beds=2):
    b = company.post("/hostel/blocks", json={"name": f"Block {uuid.uuid4().hex[:4]}", "gender": gender}).json()
    r = company.post("/hostel/rooms", json={"block_id": b["id"], "room_no": "101", "beds": beds, "fee": "50000.00"})
    assert r.status_code == 201, r.text
    return b, r.json()


def test_bed_allocation_conflicts(school):
    block, room = _room(school, "boys", beds=2)
    bed1, bed2 = room["bed_list"][0]["uuid"], room["bed_list"][1]["uuid"]
    boy1, boy2 = _student(school, school.section_id, "Arun"), _student(school, school.section_id, "Bharat")
    girl = _student(school, school.section_id, "Gita", "female")
    r = school.post("/hostel/allocations", json={"student_id": boy1["id"], "bed_uuid": bed1})
    assert r.status_code == 201, r.text
    assert school.get(f"/students/{boy1['uuid']}").json()["is_boarder"] is True
    r = school.post("/hostel/allocations", json={"student_id": boy2["id"], "bed_uuid": bed1})
    assert r.status_code == 400 and "taken" in r.json()["detail"]
    r = school.post("/hostel/allocations", json={"student_id": girl["id"], "bed_uuid": bed2})
    assert r.status_code == 400 and "boys" in r.json()["detail"]
    r = school.post("/hostel/allocations", json={"student_id": boy1["id"], "bed_uuid": bed2})
    assert r.status_code == 400 and "Move them" in r.json()["detail"]
    r = school.post("/hostel/allocations", json={"student_id": boy1["id"], "bed_uuid": bed2, "move": True})
    assert r.status_code == 201, r.text
    assert school.post("/hostel/allocations", json={"student_id": boy2["id"], "bed_uuid": bed1}).status_code == 201
    # maintenance rooms take nobody; check-out clears the boarder flag
    school.put(f"/hostel/rooms/{room['uuid']}", json={"status": "maintenance"})
    alloc = school.get("/hostel/allocations").json()["items"]
    a = next(x for x in alloc if x["student_id"] == boy2["id"])
    assert school.post(f"/hostel/allocations/{a['uuid']}/check-out", json={}).status_code == 200
    assert school.post("/hostel/allocations", json={"student_id": boy2["id"], "bed_uuid": bed1}).status_code == 400
    assert school.get(f"/students/{boy2['uuid']}").json()["is_boarder"] is False


def test_parent_requests_hostel_leave(school):
    block, room = _room(school, "mixed", beds=1)
    s = _student(school, school.section_id, "Hari", guardians=[_guardian("Father", "hari.dad@example.com")])
    school.post("/hostel/allocations", json={"student_id": s["id"], "bed_uuid": room["bed_list"][0]["uuid"]})
    parent = _parent_session(school, s, "hari.dad@example.com")
    start = (date.today() + timedelta(days=2)).isoformat()
    r = parent.post("/hostel/portal/leaves", json={"student_id": s["id"], "kind": "leave", "from_at": f"{start}T09:00:00Z",
                                                  "to_at": f"{start}T18:00:00Z", "reason": "Family wedding"})
    assert r.status_code == 201, r.text
    lv = r.json()
    assert lv["status"] == "pending" and lv["requested_via"] == "portal"
    r = school.post(f"/hostel/leaves/{lv['uuid']}/decide", json={"action": "approve"})
    assert r.json()["status"] == "approved"
    assert school.post(f"/hostel/leaves/{lv['uuid']}/decide", json={"action": "back"}).status_code == 400


# ── Wellbeing ────────────────────────────────────────────────────────

def test_gate_pass_only_to_authorised_guardians(school):
    s = _student(school, school.section_id, "Isha", "female",
                 guardians=[_guardian("Allowed"), {**_guardian("Blocked", can_pickup=False), "relation": "uncle", "is_primary": False}])
    other = _student(school, school.section_id, "Other", guardians=[_guardian("Stranger")])
    people = school.get(f"/wellbeing/pickups/{s['id']}").json()["guardians"]
    allowed = next(g for g in people if g["can_pickup"])
    blocked = next(g for g in people if not g["can_pickup"])
    stranger = school.get(f"/wellbeing/pickups/{other['id']}").json()["guardians"][0]
    r = school.post("/wellbeing/gate-passes", json={"student_id": s["id"], "guardian_id": blocked["guardian_id"], "reason": "Doctor"})
    assert r.status_code == 400 and "not allowed" in r.json()["detail"]
    r = school.post("/wellbeing/gate-passes", json={"student_id": s["id"], "guardian_id": stranger["guardian_id"], "reason": "Doctor"})
    assert r.status_code == 400 and "not a guardian" in r.json()["detail"]
    r = school.post("/wellbeing/gate-passes", json={"student_id": s["id"], "guardian_id": allowed["guardian_id"], "reason": "Doctor"})
    assert r.status_code == 201, r.text
    assert r.json()["pass_no"]


def test_points_house_totals_and_bmi(school):
    from modules.wellbeing.service import bmi
    assert bmi(150, 45) == Decimal("20.0")
    assert bmi(None, 45) is None
    houses = school.get("/wellbeing/houses").json()
    s = _student(school, school.section_id)
    if houses:
        school.put(f"/students/{s['uuid']}", json={"house_id": houses[0]["house_id"]})
    r = school.post("/wellbeing/points", json={"student_ids": [s["id"]], "points": 5, "reason": "Quiz win"})
    assert r.status_code == 201, r.text
    school.post("/wellbeing/incidents", json={"student_ids": [s["id"]], "severity": "low", "description": "Late", "points": -2})
    pts = school.get(f"/wellbeing/points?student_id={s['id']}").json()
    assert sum(p["points"] for p in pts["items"]) == 3
    if houses:
        top = next(h for h in school.get("/wellbeing/houses").json() if h["house_id"] == houses[0]["house_id"])
        assert top["points"] == 3


def test_counselling_needs_its_own_permission(school):
    teacher = school.add_employee("Tara", role="hr")
    from tests.conftest import login_as
    t = login_as(school, teacher)
    assert t.get("/wellbeing/health").status_code == 200
    assert t.get("/wellbeing/counselling").status_code == 403
    assert school.get("/wellbeing/counselling").status_code == 200


# ── Portal isolation ─────────────────────────────────────────────────

def test_parent_sees_only_own_child(school):
    mine = _student(school, school.section_id, "Mine", guardians=[_guardian("Dad", "mine.dad@example.com")])
    theirs = _student(school, school.section_id, "Theirs", guardians=[_guardian("Other", "other.dad@example.com")])
    parent = _parent_session(school, mine, "mine.dad@example.com")
    for path in ("/library/portal/summary", "/library/portal/loans", "/transport/portal/summary", "/hostel/portal/summary",
                 "/wellbeing/portal/summary"):
        assert parent.get(f"{path}?student_id={mine['id']}").status_code == 200, path
        assert parent.get(f"{path}?student_id={theirs['id']}").status_code == 404, path
    # staff endpoints stay closed to portal sign-ins
    for path in ("/library/summary", "/transport/routes", "/hostel/blocks", "/wellbeing/incidents"):
        assert parent.get(path).status_code == 403, path
    _b, acc = _book(school, "Portal Book", copies=1)
    book = school.get("/library/books?q=Portal").json()["items"][0]
    r = parent.post("/library/portal/reservations", json={"student_id": theirs["id"], "book_uuid": book["uuid"]})
    assert r.status_code == 404
    r = parent.post("/library/portal/reservations", json={"student_id": mine["id"], "book_uuid": book["uuid"]})
    assert r.status_code == 201, r.text


# ── Tenant isolation ─────────────────────────────────────────────────

TABLES = []
for _mod in ("library", "transport", "hostel", "wellbeing"):
    TABLES += __import__(f"modules.{_mod}.models", fromlist=["TABLES"]).TABLES


def test_campus_records_are_separate_between_companies(company_a, company_b):
    sec_a, sec_b = _setup(company_a), _setup(company_b)
    s_b = _student(company_b, sec_b, guardians=[_guardian("Bdad")])
    book_b, acc_b = _book(company_b, "B Secret Book")
    route_b, stop_b = _route(company_b)
    block_b, room_b = _room(company_b, "mixed")
    company_b.post("/library/issue", json={"member": s_b["admission_no"], "copies": [acc_b[0]]})
    company_b.post("/transport/riders", json={"student_id": s_b["id"], "route_uuid": route_b["uuid"], "stop_uuid": stop_b["uuid"]})
    company_b.post("/hostel/allocations", json={"student_id": s_b["id"], "bed_uuid": room_b["bed_list"][0]["uuid"]})
    company_b.post("/wellbeing/infirmary", json={"student_id": s_b["id"], "complaint": "Headache"})
    company_b.post("/wellbeing/visitors", json={"name": "B Visitor", "purpose": "Meeting"})

    # company A sees none of it through the API
    assert company_a.get("/library/books?q=Secret").json()["total"] == 0
    assert company_a.get(f"/library/books/{book_b['uuid']}").status_code == 404
    assert company_a.put(f"/library/books/{book_b['uuid']}", json={"title": "Mine"}).status_code == 404
    assert company_a.get(f"/library/copies/lookup?code={acc_b[0]}").status_code == 404
    assert company_a.post("/library/issue", json={"member": s_b["admission_no"], "copies": [acc_b[1]]}).status_code == 404
    assert company_a.get("/library/loans").json()["total"] == 0
    assert company_a.get(f"/transport/routes/{route_b['uuid']}").status_code == 404
    assert company_a.get("/transport/riders").json()["total"] == 0
    assert company_a.post("/transport/riders", json={"student_id": s_b["id"], "route_uuid": route_b["uuid"],
                                                     "stop_uuid": stop_b["uuid"]}).status_code == 404
    assert company_a.get("/hostel/blocks").json()["total"] == 0
    assert company_a.post("/hostel/allocations", json={"student_id": s_b["id"],
                                                       "bed_uuid": room_b["bed_list"][1]["uuid"]}).status_code == 404
    assert company_a.get("/wellbeing/infirmary").json()["total"] == 0
    assert company_a.get("/wellbeing/visitors").json()["total"] == 0
    assert company_a.get(f"/wellbeing/pickups/{s_b['id']}").status_code == 400

    # and a company-A database session cannot see B's rows in any campus table
    db = tenant_session(company_a.id)
    try:
        for t in TABLES:
            n = db.execute(text(f"SELECT count(*) FROM {t} WHERE company_id = :b"), {"b": company_b.id}).scalar()
            assert n == 0, t
    finally:
        db.close()


def test_campus_tables_have_forced_rls():
    from core.db import system_session
    db = system_session()
    try:
        rows = db.execute(text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:t)"),
                          {"t": TABLES}).fetchall()
    finally:
        db.close()
    assert len(rows) == len(TABLES)
    assert all(r[1] and r[2] for r in rows), [r[0] for r in rows if not (r[1] and r[2])]
