"""Face attendance: template matching, the server-side check of kiosk and selfie punches,
consent and its withdrawal, flagged punches and their review, and tenant isolation."""
import math
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from core.db import tenant_session
from modules.face_attendance import crypto, matching
from tests.conftest import login_as

DIMS = 128
LIVE = {"passed": True, "real": 0.93, "live": 0.88, "face_px": 180, "challenges": ["blink"]}


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def face(seed: int):
    rnd = random.Random(seed)
    return _unit([rnd.gauss(0, 1) for _ in range(DIMS)])


def near(v, seed: int, noise: float = 0.08):
    rnd = random.Random(seed)
    return _unit([x + rnd.gauss(0, noise / math.sqrt(DIMS)) * 3 for x in v])


def samples_of(v, n=4):
    return [near(v, 1000 + i) for i in range(n)]


# ── Matching helper (pure) ──────────────────────────────────────────

def test_similarity_and_distance():
    a = face(1)
    assert matching.similarity(a, a) == pytest.approx(1.0)
    assert matching.distance(a, a) == pytest.approx(0.0, abs=1e-9)
    assert matching.similarity(a, [-x for x in a]) == pytest.approx(-1.0)
    # scale does not matter
    assert matching.similarity(a, [x * 7 for x in a]) == pytest.approx(1.0)
    # two random faces in 128 dimensions are far apart
    assert matching.distance(face(1), face(2)) > 0.7


def test_threshold_decides_match():
    assert matching.is_match(0.61, 0.4)
    assert matching.is_match(0.60, 0.4)  # exactly on the limit counts
    assert not matching.is_match(0.59, 0.4)
    me = face(3)
    ok, score = matching.verify(near(me, 7), samples_of(me), 0.4)
    assert ok and score > 0.9
    ok, score = matching.verify(face(4), samples_of(me), 0.4)
    assert not ok and score < 0.4
    # a stricter threshold rejects a weaker match
    probe = near(me, 9, noise=1.4)
    s = matching.best_similarity(probe, samples_of(me))
    assert matching.verify(probe, samples_of(me), 1 - s + 0.01)[0]
    assert not matching.verify(probe, samples_of(me), 1 - s - 0.01)[0]


def test_identify_with_margin():
    a, b = face(10), face(11)
    gallery = {"a": samples_of(a), "b": samples_of(b)}
    r = matching.identify(near(a, 5), gallery, 0.4, 0.04)
    assert r.matched and r.key == "a" and r.reason == "matched"
    r = matching.identify(face(12), gallery, 0.4, 0.04)
    assert not r.matched and r.reason == "below_threshold"
    # twins: two people with almost the same templates are never guessed between
    twin = {"a": samples_of(a), "a2": [near(s, 77, 0.02) for s in samples_of(a)]}
    r = matching.identify(near(a, 5), twin, 0.4, 0.2)
    assert not r.matched and r.reason == "ambiguous"
    assert matching.identify(a, {}, 0.4).reason == "empty"


def test_template_validation():
    with pytest.raises(matching.TemplateError):
        matching.clean_vector([0.1] * 10)  # too short
    with pytest.raises(matching.TemplateError):
        matching.clean_vector([float("nan")] + [0.1] * 127)
    with pytest.raises(matching.TemplateError):
        matching.clean_vector([0.0] * 128)
    with pytest.raises(matching.TemplateError):
        matching.clean_vector([0.1] * 128, dims=256)
    v = matching.clean_vector([3.0] * 128)
    assert sum(x * x for x in v) == pytest.approx(1.0)
    me = face(20)
    samples, quality = matching.check_samples(samples_of(me, 3), 0.4)
    assert len(samples) == 3 and quality > 0.9
    with pytest.raises(matching.TemplateError):
        matching.check_samples(samples_of(me, 2), 0.4)  # too few
    with pytest.raises(matching.TemplateError):
        matching.check_samples([face(21), face(22), face(23)], 0.4)  # three different faces


def test_templates_encrypted_per_company():
    vecs = samples_of(face(30), 3)
    sealed = crypto.seal(1, vecs)
    assert "0." not in sealed  # not readable
    back = crypto.open_(1, sealed)
    assert len(back) == 3 and matching.similarity(back[0], vecs[0]) == pytest.approx(1.0, abs=1e-5)
    assert crypto.open_(2, sealed) == []  # another company's key cannot open it


# ── API helpers ─────────────────────────────────────────────────────

def _setup(company, **cfg):
    r = company.put("/face-attendance/settings", json={"config": {"enabled": True, **cfg}})
    assert r.status_code == 200, r.text
    k = company.post("/attendance/kiosks", json={"name": "Front desk"}).json()
    r = company.put(f"/face-attendance/kiosks/{k['uuid']}", json={"mode": cfg.pop("kiosk_mode", "on")})
    assert r.status_code == 200, r.text
    return k


def _enrol(company, emp, vec):
    ver = company.get("/face-attendance/settings").json()["config"]["consent_version"]
    return company.post(f"/face-attendance/enrolments/{emp['uuid']}",
                        json={"samples": samples_of(vec), "consent": True, "consent_version": ver,
                              "model": "human-faceres"})


def _kiosk(company, token, path, body=None):
    if body is None:
        return company.client.get(f"/api/v1/face-attendance/kiosk/{token}/{path}")
    return company.client.post(f"/api/v1/face-attendance/kiosk/{token}/{path}", json=body)


# ── Kiosk punches checked on the server ─────────────────────────────

def test_kiosk_face_punch_checked_against_threshold(company_a):
    emp = company_a.add_employee("Meena", "Iyer")
    me = face(40)
    k = _setup(company_a)
    assert _enrol(company_a, emp, me).status_code == 201

    g = _kiosk(company_a, k["token"], "gallery").json()
    assert g["config"]["mode"] == "on" and len(g["items"]) == 1
    assert g["items"][0]["employee_uuid"] == emp["uuid"] and len(g["items"][0]["templates"]) == 4
    assert _kiosk(company_a, k["token"], f"gallery?since={g['version']}").json().get("unchanged") is True

    ok = _kiosk(company_a, k["token"], "punch", {"method": "face", "employee_uuid": emp["uuid"],
                                                 "descriptor": near(me, 3), "score": 0.95, "liveness": LIVE,
                                                 "client_id": "c-1"}).json()
    assert ok["status"] == "ok" and ok["first_name"] == "Meena" and ok["score"] > 0.9

    # a tablet that claims a match the server does not agree with: no punch, flagged
    bad = _kiosk(company_a, k["token"], "punch", {"method": "face", "employee_uuid": emp["uuid"],
                                                  "descriptor": face(41), "score": 0.99, "liveness": LIVE,
                                                  "at": (datetime.now(timezone.utc) + timedelta(minutes=0)).isoformat()}).json()
    assert bad["status"] == "rejected"

    # a photo held to the camera (liveness fails): no punch, flagged
    spoof = _kiosk(company_a, k["token"], "punch", {"method": "face", "employee_uuid": emp["uuid"],
                                                    "descriptor": near(me, 4), "liveness": {**LIVE, "real": 0.1},
                                                    "snapshot": "data:image/jpeg;base64,AAAA"}).json()
    assert spoof["status"] == "rejected"

    ev = company_a.get("/face-attendance/events", params={"status": "open"}).json()["items"]
    kinds = sorted(e["kind"] for e in ev)
    assert kinds == ["no_match", "spoof"]
    assert {r["code"] for e in ev for r in e["reasons"]} == {"server_mismatch", "spoof_suspected"}
    # snapshots are kept for flagged checks (default) and can be opened by HR
    sp = next(e for e in ev if e["kind"] == "spoof")
    assert sp["has_snapshot"]
    assert company_a.get(f"/face-attendance/events/{sp['uuid']}/snapshot").json()["image"].startswith("data:image/")

    # a stricter threshold on the kiosk rejects a weaker match
    weak = near(me, 8, noise=0.5)
    score = matching.best_similarity(weak, samples_of(me))
    company_a.put(f"/face-attendance/kiosks/{k['uuid']}", json={"mode": "on", "threshold": round(1 - score - 0.02, 3)})
    r = _kiosk(company_a, k["token"], "punch", {"method": "face", "employee_uuid": emp["uuid"], "descriptor": weak,
                                                "liveness": LIVE,
                                                "at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}).json()
    assert r["status"] == "rejected"

    # replaying the same offline item is safe
    again = _kiosk(company_a, k["token"], "sync", {"items": [{"method": "face", "employee_uuid": emp["uuid"],
                                                              "descriptor": near(me, 3), "liveness": LIVE,
                                                              "client_id": "c-1"}]}).json()
    assert again["results"][0]["status"] == "duplicate"


def test_required_kiosk_pin_fallback_is_flagged_and_reviewed(company_a):
    emp = company_a.add_employee("Arun", "Das", employee_code="FA0001")
    k = _setup(company_a, kiosk_mode="required")
    company_a.put(f"/face-attendance/kiosks/{k['uuid']}", json={"mode": "required"})
    assert company_a.put(f"/attendance/kiosk-pins/{emp['uuid']}", json={"pin": "4826"}).status_code == 200

    # the plain PIN endpoint is closed at a face-required kiosk
    r = company_a.client.post(f"/api/v1/attendance/kiosk/{k['token']}/punch", json={"employee_code": emp["employee_code"], "pin": "4826"})
    assert r.status_code == 400 and "face" in r.json()["detail"]

    # PIN after the face check works and waits for HR review
    r = _kiosk(company_a, k["token"], "punch", {"method": "pin", "employee_code": emp["employee_code"], "pin": "4826",
                                                "attempts": 3})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    wrong = _kiosk(company_a, k["token"], "punch", {"method": "pin", "employee_code": emp["employee_code"], "pin": "1357",
                                                    "attempts": 3})
    assert wrong.status_code == 400

    items = company_a.get("/face-attendance/events").json()["items"]
    assert len(items) == 1
    ev = items[0]
    assert ev["kind"] == "pin_fallback" and ev["punch"] and not ev["punch"]["ignored"]
    assert {"required_fallback", "repeated_failures"} <= {x["code"] for x in ev["reasons"]}

    assert company_a.post(f"/face-attendance/events/{ev['uuid']}/review", json={"decision": "reject"}).status_code == 400
    r = company_a.post(f"/face-attendance/events/{ev['uuid']}/review",
                       json={"decision": "reject", "note": "Not the employee on the snapshot"})
    assert r.status_code == 200 and r.json()["review_status"] == "rejected"
    done = company_a.get("/face-attendance/events", params={"status": "reviewed"}).json()["items"][0]
    assert done["punch"]["ignored"] is True
    assert company_a.post(f"/face-attendance/events/{ev['uuid']}/review",
                          json={"decision": "accept"}).status_code == 400  # already reviewed

    # misses at the kiosk: the third one in a row is flagged
    for i in range(3):
        out = _kiosk(company_a, k["token"], "punch", {"method": "miss", "attempts": 1, "client_id": f"m-{i}"}).json()
    assert out["flagged"] is True


# ── Consent and its withdrawal ──────────────────────────────────────

def test_consent_needed_and_withdrawal_deletes_templates(company_a):
    emp = company_a.add_employee("Kavya", "Nair")
    k = _setup(company_a, self_clock="everyone")
    me_vec = face(50)
    s = login_as(company_a, emp)
    status = s.get("/face-attendance/me").json()
    assert status["enrolled"] is False and "Digital Personal Data Protection Act" in status["consent_text"]

    no = s.post("/face-attendance/me/enrol", json={"samples": samples_of(me_vec), "consent": False,
                                                   "consent_version": status["consent_version"]})
    assert no.status_code == 400 and "consent" in no.json()["detail"].lower()
    old = s.post("/face-attendance/me/enrol", json={"samples": samples_of(me_vec), "consent": True,
                                                    "consent_version": status["consent_version"] + 5})
    assert old.status_code == 400
    r = s.post("/face-attendance/me/enrol", json={"samples": samples_of(me_vec), "consent": True,
                                                  "consent_version": status["consent_version"]})
    assert r.status_code == 201, r.text
    assert "templates" not in r.json()  # never shown back
    me = s.get("/face-attendance/me").json()
    assert me["enrolled"] and me["self_clock_required"]
    assert "templates" not in str(me["enrolment"])

    # someone else cannot enrol the same face
    other = company_a.add_employee("Imposter", "X")
    assert _enrol(company_a, other, me_vec).status_code == 400

    # web clock-in now needs the selfie match
    assert s.post("/attendance/me/clock", json={}).status_code == 400
    bad = s.post("/face-attendance/me/clock", json={"descriptor": face(51), "liveness": LIVE})
    assert bad.status_code == 400
    good = s.post("/face-attendance/me/clock", json={"descriptor": near(me_vec, 2), "liveness": LIVE})
    assert good.status_code == 200, good.text
    assert good.json()["punch"]["source"] == "web" and good.json()["score"] > 0.9

    # a failed check left a snapshot? only for flagged ones; add one to see it deleted on withdrawal
    db = tenant_session(company_a.id)
    try:
        db.execute(text("UPDATE face_events SET snapshot = 'data:image/jpeg;base64,AA' WHERE company_id = :c"),
                   {"c": company_a.id})
        db.commit()
    finally:
        db.close()

    w = s.post("/face-attendance/me/withdraw")
    assert w.status_code == 200 and w.json()["deleted"] == 1
    db = tenant_session(company_a.id)
    try:
        row = db.execute(text("SELECT status, templates, withdrawn_at FROM face_enrolments WHERE employee_id = :e"),
                         {"e": emp["id"]}).one()
        assert row.status == "withdrawn" and row.templates is None and row.withdrawn_at is not None
        assert db.execute(text("SELECT count(*) FROM face_events WHERE employee_id = :e AND snapshot IS NOT NULL"),
                          {"e": emp["id"]}).scalar() == 0
        logged = db.execute(text("SELECT count(*) FROM audit_logs WHERE company_id = :c AND action = 'face.consent_withdrawn'"),
                            {"c": company_a.id}).scalar()
        assert logged == 1
    finally:
        db.close()
    assert s.get("/face-attendance/me").json()["enrolled"] is False
    # gone from the kiosk at its next refresh, and web clock-in is open again (not enrolled, allowed)
    assert all(i["employee_uuid"] != emp["uuid"] for i in _kiosk(company_a, k["token"], "gallery").json()["items"])
    assert s.get("/face-attendance/me").json()["self_clock_required"] is False

    # HR removal on a written request is audited too
    assert _enrol(company_a, other, face(52)).status_code == 201
    assert company_a.post(f"/face-attendance/enrolments/{other['uuid']}/remove", json={"reason": "Asked in writing"}).status_code == 200
    lst = company_a.get("/face-attendance/enrolments", params={"status": "withdrawn"}).json()["items"]
    assert {i["employee_uuid"] for i in lst} == {emp["uuid"], other["uuid"]}


def test_employee_cannot_use_admin_endpoints(company_a):
    emp = company_a.add_employee("Raj")
    s = login_as(company_a, emp)
    assert s.get("/face-attendance/settings").status_code == 403
    assert s.get("/face-attendance/events").status_code == 403
    assert s.post(f"/face-attendance/enrolments/{emp['uuid']}", json={"samples": [], "consent": True}).status_code in (403, 422)


# ── Tenant isolation ────────────────────────────────────────────────

def test_tenant_isolation(company_a, company_b):
    a_emp = company_a.add_employee("Asha")
    b_emp = company_b.add_employee("Bala")
    ka = _setup(company_a)
    kb = _setup(company_b)
    assert _enrol(company_a, a_emp, face(60)).status_code == 201
    assert _enrol(company_b, b_emp, face(61)).status_code == 201
    _kiosk(company_b, kb["token"], "punch", {"method": "miss", "attempts": 5})

    # API: A sees none of B's people, checks or kiosks
    assert all(i["employee_uuid"] != b_emp["uuid"] for i in company_a.get("/face-attendance/enrolments").json()["items"])
    assert company_a.get("/face-attendance/events", params={"status": "flagged"}).json()["total"] == 0
    assert company_a.post(f"/face-attendance/enrolments/{b_emp['uuid']}", json={"samples": samples_of(face(62)),
                                                                                "consent": True, "consent_version": 1}).status_code == 404
    assert company_a.post(f"/face-attendance/enrolments/{b_emp['uuid']}/remove", json={"reason": "xxx"}).status_code == 404
    assert company_a.put(f"/face-attendance/kiosks/{kb['uuid']}", json={"mode": "off"}).status_code == 404
    b_ev = company_b.get("/face-attendance/events", params={"status": "flagged"}).json()["items"][0]
    assert company_a.post(f"/face-attendance/events/{b_ev['uuid']}/review", json={"decision": "accept"}).status_code == 404
    assert company_a.get(f"/face-attendance/events/{b_ev['uuid']}/snapshot").status_code == 404

    # kiosk A downloads only A's templates and cannot punch B's employee
    gal = _kiosk(company_a, ka["token"], "gallery").json()
    assert [i["employee_uuid"] for i in gal["items"]] == [a_emp["uuid"]]
    r = _kiosk(company_a, ka["token"], "punch", {"method": "face", "employee_uuid": b_emp["uuid"],
                                                 "descriptor": face(61), "liveness": LIVE})
    assert r.status_code == 400

    # database: row-level security hides B's rows from a session of A
    db = tenant_session(company_a.id)
    try:
        for t in ("face_settings", "face_kiosk_settings", "face_enrolments", "face_events"):
            n = db.execute(text(f"SELECT count(*) FROM {t} WHERE company_id = :b"), {"b": company_b.id}).scalar()
            assert n == 0, t
        assert db.execute(text("SELECT count(*) FROM face_enrolments")).scalar() == 1
    finally:
        db.close()
