"""Travel: policy checks (pure), the trip flow through approval, travel desk bookings,
the advance and the settlement into an expense claim, permissions and tenant isolation."""
from datetime import date, timedelta
from decimal import Decimal

from modules.travel import policy as pol
from tests.conftest import login_as

TODAY = date(2026, 11, 7)


# ── Policy rules (no database) ──────────────────────────────────────

def _trip(**kw):
    base = {"travel_type": "domestic", "start_date": TODAY + timedelta(days=20), "end_date": TODAY + timedelta(days=22),
            "legs": [{"mode": "flight", "travel_class": "economy", "from_place": "Kochi", "to_place": "Mumbai",
                      "depart_date": TODAY + timedelta(days=20), "estimated_cost": 6000},
                     {"mode": "flight", "travel_class": "economy", "from_place": "Mumbai", "to_place": "Kochi",
                      "depart_date": TODAY + timedelta(days=22), "estimated_cost": 6000}],
            "stays": [{"city": "Bombay", "check_in": TODAY + timedelta(days=20), "check_out": TODAY + timedelta(days=22),
                       "nightly_cost": 4000}],
            "advance_amount": 0, "estimated_total": 0}
    base.update(kw)
    return base


def test_city_tiers_and_aliases():
    r = pol.default_rules()
    assert pol.city_tier(r, "Bangalore") == "X"
    assert pol.city_tier(r, " new  delhi ") == "X"
    assert pol.city_tier(r, "Kochi") == "Y"
    assert pol.city_tier(r, "Munnar") == "Z"
    assert pol.city_tier(r, "Dubai", international=True) == "international"


def test_band_by_grade_first_match_and_fallback():
    r = pol.default_rules()
    assert pol.band_for(r, "G7")["name"] == "Leadership"
    assert pol.band_for(r, "G4")["name"] == "Managers"
    assert pol.band_for(r, "G1")["name"] == "Everyone else"
    assert pol.band_for(r, None)["name"] == "Everyone else"


def test_trip_within_policy_has_no_flags_and_per_diem_uses_destination_tier():
    r = pol.default_rules()
    t = _trip()
    assert pol.check(r, "G1", t, TODAY) == []
    days, rate = pol.per_diem(r, "G1", t)
    assert days == 3 and rate == Decimal("1000")  # Mumbai is tier X for "everyone else"
    est = pol.estimate(r, "G1", t)
    assert est["total"] == Decimal("12000") + Decimal("8000") + Decimal("3000")


def test_class_mode_hotel_lead_time_and_advance_breaches():
    r = pol.default_rules()
    t = _trip(start_date=TODAY + timedelta(days=2))
    t["legs"][0]["travel_class"] = "business"
    t["legs"][1] = {"mode": "train", "travel_class": "1A", "from_place": "Mumbai", "to_place": "Kochi",
                    "depart_date": TODAY + timedelta(days=22), "estimated_cost": 3000}
    t["stays"][0]["nightly_cost"] = 9000
    t["estimated_total"] = 10000
    t["advance_amount"] = 9000
    codes = sorted(f["code"] for f in pol.check(r, "G1", t, TODAY))
    assert codes == ["advance", "class", "class", "hotel", "lead_time"]
    # the same trip is fine for leadership except the advance
    t["start_date"] = TODAY + timedelta(days=20)
    codes = [f["code"] for f in pol.check(r, "G7", t, TODAY)]
    assert codes == ["advance"]


def test_mode_not_allowed_and_dates_outside_trip():
    r = pol.default_rules()
    r["bands"][-1]["modes"].pop("flight")
    t = _trip()
    t["legs"][1]["depart_date"] = TODAY + timedelta(days=30)
    codes = [f["code"] for f in pol.check(r, "G1", t, TODAY)]
    assert codes.count("mode") == 2


def test_clean_rules_validation():
    r = pol.default_rules()
    r["bands"][0]["modes"]["flight"] = ["first"]
    try:
        pol.clean_rules(r)
        assert False
    except ValueError as exc:
        assert "Unknown class" in str(exc)
    r = pol.default_rules()
    r["bands"] = [b for b in r["bands"] if b["grade_codes"]]
    try:
        pol.clean_rules(r)
        assert False
    except ValueError as exc:
        assert "everyone else" in str(exc)


# ── API ─────────────────────────────────────────────────────────────

def _today_ist():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()


def _approve_all(session):
    for _ in range(6):  # later steps appear once earlier ones are decided
        items = session.get("/workflow/inbox").json()["items"]
        if not items:
            return
        for t in items:
            r = session.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"})
            assert r.status_code == 200, r.text


def _trip_body(start, **kw):
    body = {"purpose": "Client workshop", "travel_type": "domestic", "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=2)).isoformat(),
            "legs": [{"mode": "flight", "travel_class": "economy", "from_place": "Bengaluru", "to_place": "Pune",
                      "depart_date": start.isoformat(), "estimated_cost": 5500},
                     {"mode": "train", "travel_class": "3A", "from_place": "Pune", "to_place": "Bengaluru",
                      "depart_date": (start + timedelta(days=2)).isoformat(), "estimated_cost": 1800}],
            "stays": [{"city": "Pune", "check_in": start.isoformat(), "check_out": (start + timedelta(days=2)).isoformat(),
                       "nightly_cost": 4000}],
            "advance_amount": 5000}
    body.update(kw)
    return body


def test_policy_versions_are_effective_dated(company_a):
    r = company_a.get("/travel/policy")
    assert r.status_code == 200, r.text
    cur = r.json()["current"]
    assert cur["version"] == 1 and cur["in_force"]
    rules = cur["rules"]
    rules["bands"][-1]["hotel"]["Y"] = 2000
    future = (_today_ist() + timedelta(days=60)).isoformat()
    r = company_a.post("/travel/policy", json={"effective_from": future, "rules": rules, "change_note": "Tighter"})
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2 and r.json()["future"] and not r.json()["in_force"]
    versions = company_a.get("/travel/policy").json()
    assert versions["current"]["version"] == 1 and len(versions["versions"]) == 2
    # a bad policy is refused
    bad = dict(rules, advance_max_percent=150)
    assert company_a.post("/travel/policy", json={"effective_from": future, "rules": bad}).status_code == 400


def test_trip_outside_policy_needs_reason_then_goes_to_hr(company_a):
    emp = company_a.add_employee("Asha", "Nair")
    me = login_as(company_a, emp)
    start = _today_ist() + timedelta(days=30)
    body = _trip_body(start)
    body["legs"][0]["travel_class"] = "business"
    pre = me.post("/travel/trips/preview", json=body).json()
    assert pre["outside_policy"] and any(f["code"] == "class" for f in pre["flags"])
    r = me.post("/travel/trips", json={**body, "submit": True})
    assert r.status_code == 400 and "reason" in r.json()["detail"]
    r = me.post("/travel/trips", json={**body, "submit": True, "exception_reason": "Only seat left"})
    assert r.status_code == 201, r.text
    trip = r.json()
    assert trip["status"] == "pending" and trip["outside_policy"] and trip["number"]
    # the approver sees the policy warnings with the request
    assert company_a.get("/workflow/inbox").json()["total"] >= 1
    # someone else cannot see it
    other = login_as(company_a, company_a.add_employee("Bina", "Rao"))
    assert other.get(f"/travel/trips/{trip['uuid']}").status_code == 404
    assert other.get("/travel/trips?scope=all").status_code == 403


def test_full_flow_booking_advance_and_settlement(company_a):
    emp = company_a.add_employee("Charan", "Das")
    me = login_as(company_a, emp)
    start = _today_ist()  # starts today so it can be settled in the test (short notice: outside policy)
    r = me.post("/travel/trips", json={**_trip_body(start), "submit": True, "exception_reason": "Urgent client call"})
    assert r.status_code == 201, r.text
    uuid = r.json()["uuid"]
    _approve_all(company_a)  # owner approves (no manager) every step
    t = me.get(f"/travel/trips/{uuid}").json()
    assert t["status"] == "approved", t
    assert t["advance"] and t["advance"]["status"] == "approved" and t["advance"]["amount"] == "5000.00"

    # employees cannot use the desk
    leg = t["legs"][0]
    assert me.post(f"/travel/trips/{uuid}/bookings/leg/{leg['uuid']}", json={"booked_cost": 5200}).status_code == 403

    # desk books the flight (company paid) with the ticket; the traveller books the train and hotel themselves
    pdf = "data:application/pdf;base64,JVBERi0xLjQKJSVFT0YK"
    r = company_a.post(f"/travel/trips/{uuid}/bookings/leg/{leg['uuid']}",
                       json={"booked_cost": 5200, "vendor": "IndiGo 6E 512", "booking_ref": "PNR123", "paid_by": "company",
                             "file_data": pdf, "file_name": "ticket.pdf"})
    assert r.status_code == 200, r.text
    r = company_a.post(f"/travel/trips/{uuid}/bookings/leg/{t['legs'][1]['uuid']}",
                       json={"booked_cost": 1750, "paid_by": "employee", "booking_ref": "4521367890"})
    assert r.status_code == 200
    r = company_a.post(f"/travel/trips/{uuid}/bookings/stay/{t['stays'][0]['uuid']}",
                       json={"booked_cost": 7600, "paid_by": "employee", "vendor": "Hotel Deccan"})
    assert r.json()["status"] == "booked"
    f = me.get(f"/travel/trips/{uuid}/bookings/leg/{leg['uuid']}/file")
    assert f.status_code == 200 and f.headers["content-type"] == "application/pdf"

    # finance pays the advance through the desk
    r = company_a.post(f"/travel/trips/{uuid}/pay-advance", json={"reference": "UTR998"})
    assert r.status_code == 200 and r.json()["advance"]["status"] == "paid"

    # settle: a draft claim with the train, two hotel nights and three days' allowance (not the company flight)
    r = me.post(f"/travel/trips/{uuid}/settle")
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["status"] == "settling" and t["claim"]
    claim = me.get(f"/expenses/claims/{t['claim']['uuid']}").json()
    assert claim["status"] == "draft"
    assert len(claim["lines"]) == 1 + 2 + 3
    assert Decimal(claim["total"]) == Decimal("1750") + Decimal("7600") + 3 * Decimal("1000")
    assert not any("Flight" in (l["description"] or "") for l in claim["lines"])
    assert me.post(f"/travel/trips/{uuid}/settle").status_code == 400  # only once

    # the traveller edits the claim through the expense form (advance link is kept), sends it, it is approved
    lines = [{k: l[k] for k in ("category_uuid", "expense_date", "description", "merchant", "from_place", "to_place")}
             | {"amount": l["amount"], "keep_receipt_of": l["uuid"]} for l in claim["lines"]]
    r = me.put(f"/expenses/claims/{claim['uuid']}", json={"title": claim["title"], "lines": lines, "submit": True})
    assert r.status_code == 200, r.text
    _approve_all(company_a)
    claim = me.get(f"/expenses/claims/{claim['uuid']}").json()
    assert claim["status"] == "approved", claim
    assert claim["advance_adjusted"] == "5000.00"  # set off against the advance; the rest is paid with salary

    t = me.get(f"/travel/trips/{uuid}").json()
    assert t["status"] == "settled"
    assert t["advance"]["status"] == "settled" and t["advance"]["balance"] == "0.00"
    assert Decimal(t["actual_cost"]) == Decimal("5200") + Decimal("12350")

    # reports
    spend = company_a.get("/travel/reports/spend", params={"by": "department", "start": (start - timedelta(days=1)).isoformat(),
                                                           "end": (start + timedelta(days=5)).isoformat()}).json()
    assert spend["totals"]["trips"] == 1 and Decimal(spend["totals"]["spend"]) == Decimal("17550")
    br = company_a.get("/travel/reports/breaches", params={"start": (start - timedelta(days=1)).isoformat()}).json()
    assert br["by_kind"][0]["code"] == "lead_time" and len(br["trips"]) == 1


def test_unspent_advance_is_returned(company_a):
    emp = company_a.add_employee("Deepa", "Iyer")
    me = login_as(company_a, emp)
    start = _today_ist()
    body = _trip_body(start, advance_amount=9000)
    body["accommodation_needed"] = False
    body["stays"] = []
    r = me.post("/travel/trips", json={**body, "submit": True, "exception_reason": "Same-day plan"})
    uuid = r.json()["uuid"]
    _approve_all(company_a)
    t = me.get(f"/travel/trips/{uuid}").json()
    for leg in t["legs"]:
        company_a.post(f"/travel/trips/{uuid}/bookings/leg/{leg['uuid']}", json={"booked_cost": 1000, "paid_by": "company"})
    company_a.post(f"/travel/trips/{uuid}/pay-advance", json={"reference": "UTR1"})
    t = me.post(f"/travel/trips/{uuid}/settle").json()
    claim = me.get(f"/expenses/claims/{t['claim']['uuid']}").json()
    assert len(claim["lines"]) == 3  # only the daily allowance (Pune, tier X): 3 x 1000
    assert me.post(f"/expenses/claims/{claim['uuid']}/submit").status_code == 200
    _approve_all(company_a)
    t = me.get(f"/travel/trips/{uuid}").json()
    assert t["status"] == "settled" and t["advance"]["balance"] == "6000.00"
    r = company_a.post(f"/travel/trips/{uuid}/return-advance", json={"reference": "Cash returned"})
    assert r.status_code == 200, r.text
    assert r.json()["advance"]["status"] == "settled" and r.json()["advance"]["balance"] == "0.00"


def test_cancel_before_booking_cancels_the_advance(company_a):
    me = login_as(company_a, company_a.add_employee("Esha", "Menon"))
    r = me.post("/travel/trips", json={**_trip_body(_today_ist() + timedelta(days=40)), "submit": True})
    assert r.status_code == 201, r.text
    uuid = r.json()["uuid"]
    _approve_all(company_a)
    t = me.post(f"/travel/trips/{uuid}/cancel").json()
    assert t["status"] == "cancelled" and t["advance"]["status"] == "cancelled"


def test_manager_sees_team_trips(company_a):
    boss = company_a.add_employee("Farah", "Khan")
    emp = company_a.add_employee("Gopal", "Shetty", reporting_manager_id=boss["id"])
    me, mgr = login_as(company_a, emp), login_as(company_a, boss)
    r = me.post("/travel/trips", json={**_trip_body(_today_ist() + timedelta(days=30)), "submit": True})
    assert r.status_code == 201, r.text
    items = mgr.get("/travel/trips?scope=team").json()["items"]
    assert [i["uuid"] for i in items] == [r.json()["uuid"]]
    assert mgr.get(f"/travel/trips/{r.json()['uuid']}").status_code == 200
    # the manager gets the approval task
    _approve_all(mgr)
    assert me.get(f"/travel/trips/{r.json()['uuid']}").json()["status"] == "approved"


def test_travel_tenant_isolation(company_a, company_b):
    me = login_as(company_a, company_a.add_employee("Hari", "Pillai"))
    r = me.post("/travel/trips", json=_trip_body(_today_ist() + timedelta(days=30)))
    uuid = r.json()["uuid"]
    assert company_b.get(f"/travel/trips/{uuid}").status_code == 404
    assert company_b.post(f"/travel/trips/{uuid}/mark-booked").status_code == 404
    assert all(i["uuid"] != uuid for i in company_b.get("/travel/trips?scope=all").json()["items"])
    from core.db import tenant_session
    from modules.travel.models import TravelRequest
    db = tenant_session(company_b.id)
    try:
        assert db.query(TravelRequest).filter(TravelRequest.uuid == uuid).first() is None
    finally:
        db.close()
