"""Engagement: audience targeting, survey anonymity, kudos, the celebration job, and
tenant isolation of every engagement table."""
from datetime import date, datetime, timedelta, timezone

import pytest

from core.db import tenant_session
from modules.company.models import Company
from modules.engagement import celebrations, people
from modules.engagement.models import BuzzPost, CelebrationEvent, SurveyResponse
from tests.conftest import login_as


def _dept(company, name):
    r = company.post("/departments/", json={"department_name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _post(session, body, **extra):
    r = session.post("/engagement/posts", json={"body": body, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _feed_bodies(session, **params):
    r = session.get("/engagement/feed", params={"limit": 30, **params})
    assert r.status_code == 200, r.text
    d = r.json()
    return [p["body"] for p in d["pinned"] + d["items"]]


# ── Audience targeting ──────────────────────────────────────────────

def test_post_audience_by_department(company_a):
    sales, ops = _dept(company_a, "Sales"), _dept(company_a, "Ops")
    s1 = company_a.add_employee("Sana", "Shah", department_id=sales["id"])
    o1 = company_a.add_employee("Omar", "Ali", department_id=ops["id"])
    sana, omar = login_as(company_a, s1), login_as(company_a, o1)

    post = _post(company_a, "Sales kickoff on Monday", audience={"match": "any", "rules": {"department": [str(sales["id"])]}})
    assert post["audience_label"] == "Sales"
    _post(company_a, "Everyone: town hall at 5")

    assert "Sales kickoff on Monday" in _feed_bodies(sana)
    assert "Sales kickoff on Monday" not in _feed_bodies(omar)
    assert "Everyone: town hall at 5" in _feed_bodies(omar)
    # the post itself is hidden from people outside the audience
    assert omar.get(f"/engagement/posts/{post['uuid']}").status_code == 404
    assert omar.post(f"/engagement/posts/{post['uuid']}/react", json={"emoji": "👍"}).status_code == 404
    assert sana.post(f"/engagement/posts/{post['uuid']}/react", json={"emoji": "👍"}).status_code == 200
    # the owner sees everything
    assert "Sales kickoff on Monday" in _feed_bodies(company_a)


def test_audience_match_all_and_preview(company_a):
    sales = _dept(company_a, "Sales")
    s1 = company_a.add_employee("Mira", "Rao", department_id=sales["id"], role="manager")
    company_a.add_employee("Sid", "Rao", department_id=sales["id"])
    aud = {"match": "all", "rules": {"department": [str(sales["id"])], "role": ["manager"]}}
    r = company_a.post("/engagement/audience/preview", json={"audience": aud})
    assert r.status_code == 200 and r.json()["count"] == 1
    _post(company_a, "Sales managers only", audience=aud)
    assert "Sales managers only" in _feed_bodies(login_as(company_a, s1))
    others = [e for e in company_a.get("/employees/").json() if e["first_name"] == "Sid"]
    assert "Sales managers only" not in _feed_bodies(login_as(company_a, others[0]))


def test_announcement_targeting_and_acknowledgement(company_a):
    hr, eng = _dept(company_a, "HR"), _dept(company_a, "Engineering")
    h = login_as(company_a, company_a.add_employee("Hema", "Iyer", department_id=hr["id"]))
    e = login_as(company_a, company_a.add_employee("Eshan", "Das", department_id=eng["id"]))
    r = company_a.post("/engagement/announcements", json={
        "title": "Engineering on-call rota", "body_html": "<p>New rota <script>alert(1)</script>from Monday.</p>",
        "audience": {"rules": {"department": [str(eng["id"])]}}, "requires_ack": True, "action": "publish"})
    assert r.status_code == 201, r.text
    ann = r.json()
    assert "<script>" not in ann["body_html"] and ann["status"] == "published"
    assert ann["stats"] == {"audience": 1, "read": 0, "acknowledged": 0}
    assert [a["title"] for a in e.get("/engagement/announcements").json()["items"]] == ["Engineering on-call rota"]
    assert h.get("/engagement/announcements").json()["items"] == []
    assert h.get(f"/engagement/announcements/{ann['uuid']}").status_code == 404
    assert h.post(f"/engagement/announcements/{ann['uuid']}/acknowledge").status_code == 404
    assert e.post(f"/engagement/announcements/{ann['uuid']}/acknowledge").status_code == 200
    stats = company_a.get(f"/engagement/announcements/{ann['uuid']}").json()["stats"]
    assert stats == {"audience": 1, "read": 1, "acknowledged": 1}
    # it is pinned on Buzz for its audience only
    assert any(p["kind"] == "announcement" for p in e.get("/engagement/feed").json()["pinned"])
    assert not any(p["kind"] == "announcement" for p in h.get("/engagement/feed").json()["pinned"])


def test_scheduled_announcement_publishes_once(company_a):
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    r = company_a.post("/engagement/announcements", json={"title": "Holiday list", "body_html": "<p>Out soon</p>",
                                                          "publish_at": future, "action": "publish"})
    assert r.json()["status"] == "scheduled"
    from modules.engagement import announcements
    from modules.engagement.models import Announcement
    db = tenant_session(company_a.id)
    try:
        a = db.query(Announcement).filter(Announcement.uuid == r.json()["uuid"]).one()
        a.publish_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        assert announcements.run_due(db, company_a.id) == 1
        assert announcements.run_due(db, company_a.id) == 0
        db.commit()
        assert db.query(BuzzPost).filter(BuzzPost.announcement_id == a.id).count() == 1
    finally:
        db.close()


# ── Feed basics ─────────────────────────────────────────────────────

def test_reactions_comments_mentions_and_soft_delete(company_a):
    a = company_a.add_employee("Anu", "Varma")
    b = company_a.add_employee("Bala", "Nair")
    anu, bala = login_as(company_a, a), login_as(company_a, b)
    post = _post(anu, f"Thanks @[Bala Nair]({b['uuid']}) for the help! #teamwork")
    assert post["hashtags"] == ["teamwork"]
    inbox = bala.get("/notifications/inbox").json()
    assert inbox["unread"] >= 1 and inbox["items"][0]["kind"] == "mention"

    # one reaction per person: switch, then remove with the same emoji
    assert bala.post(f"/engagement/posts/{post['uuid']}/react", json={"emoji": "👍"}).json()["my_reaction"] == "👍"
    assert bala.post(f"/engagement/posts/{post['uuid']}/react", json={"emoji": "🎉"}).json()["reaction_total"] == 1
    assert bala.post(f"/engagement/posts/{post['uuid']}/react", json={"emoji": "🎉"}).json()["my_reaction"] is None

    tree = bala.post(f"/engagement/posts/{post['uuid']}/comments", json={"body": "Anytime!"}).json()
    reply = anu.post(f"/engagement/posts/{post['uuid']}/comments", json={"body": "🙏", "parent": tree[0]["uuid"]}).json()
    assert len(reply) == 1 and reply[0]["replies"][0]["body"] == "🙏"

    # only the author edits; a moderator (the owner) may delete; deletion is soft and audited
    assert bala.put(f"/engagement/posts/{post['uuid']}", json={"body": "hijack"}).status_code == 403
    assert anu.put(f"/engagement/posts/{post['uuid']}", json={"body": "Edited thanks"}).json()["edited"] is True
    assert bala.post(f"/engagement/posts/{post['uuid']}/pin", json={"pinned": True}).status_code == 403
    assert company_a.post(f"/engagement/posts/{post['uuid']}/pin", json={"pinned": True}).status_code == 200
    assert anu.delete(f"/engagement/posts/{post['uuid']}").status_code == 200
    assert bala.get(f"/engagement/posts/{post['uuid']}").status_code == 404
    db = tenant_session(company_a.id)
    try:
        row = db.query(BuzzPost).filter(BuzzPost.uuid == post["uuid"]).one()
        assert row.deleted_at is not None
    finally:
        db.close()
    events = company_a.get("/audit/", params={"action": "buzz.post_deleted"}).json()
    rows = events.get("items", events.get("data", []))
    assert any(r.get("action") == "buzz.post_deleted" for r in rows)


def test_poll_and_report_auto_hide(company_a):
    people_ = [login_as(company_a, company_a.add_employee(n, "K")) for n in ("Pia", "Qadir", "Ravi", "Sam")]
    poll = _post(people_[0], "Lunch?", poll={"options": ["Dosa", "Biryani"], "multiple": False})
    opt = poll["poll"]["options"][1]["uuid"]
    r = people_[1].post(f"/engagement/posts/{poll['uuid']}/vote", json={"options": [opt]})
    assert r.status_code == 200 and r.json()["poll"]["options"][1]["votes"] == 1
    assert people_[1].post(f"/engagement/posts/{poll['uuid']}/vote",
                           json={"options": [o["uuid"] for o in poll["poll"]["options"]]}).status_code == 400
    bad = _post(people_[0], "Something rude")
    for s in people_[1:]:
        assert s.post(f"/engagement/posts/{bad['uuid']}/report", json={"reason": "offensive"}).status_code == 201
    # three reports hide it automatically until a moderator looks
    assert "Something rude" not in _feed_bodies(people_[2])
    queue = company_a.get("/engagement/moderation").json()
    assert any(p["uuid"] == bad["uuid"] and len(p["reports"]) == 3 for p in queue["items"])
    assert people_[1].get("/engagement/moderation").status_code == 403


# ── Survey anonymity ────────────────────────────────────────────────

def _survey(company, **extra):
    body = {"title": "Pulse", "kind": "pulse", "identity": "anonymous", "action": "publish",
            "questions": [{"qtype": "rating", "text": "How is your week?", "settings": {"max": 5}},
                          {"qtype": "text", "text": "Anything else?", "required": False}], **extra}
    r = company.post("/engagement/surveys", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_anonymous_results_need_five_responses(company_a):
    staff = [login_as(company_a, company_a.add_employee(f"Emp{i}", "Test")) for i in range(6)]
    s = _survey(company_a)
    assert s["status"] == "active"
    qs = company_a.get(f"/engagement/surveys/{s['uuid']}").json()["questions"]
    for i, me in enumerate(staff[:4]):
        r = me.post(f"/engagement/surveys/{s['uuid']}/responses", json={"answers": {qs[0]["uuid"]: 4, qs[1]["uuid"]: f"note {i}"}})
        assert r.status_code == 201, r.text
    res = company_a.get(f"/engagement/surveys/{s['uuid']}/results").json()
    assert res["locked"] is True and res["needed"] == 1 and res["questions"] == []
    assert res["responses"] == 4
    # answering twice is refused
    assert staff[0].post(f"/engagement/surveys/{s['uuid']}/responses", json={"answers": {qs[0]["uuid"]: 1}}).status_code == 400
    staff[4].post(f"/engagement/surveys/{s['uuid']}/responses", json={"answers": {qs[0]["uuid"]: 2}})
    res = company_a.get(f"/engagement/surveys/{s['uuid']}/results").json()
    assert res["locked"] is False
    rating = res["questions"][0]
    assert rating["average"] == 3.6 and sum(d["count"] for d in rating["distribution"]) == 5
    assert all(t["by"] is None for t in res["questions"][1]["texts"])
    # no answer sheet carries a name, and employees can't read results
    db = tenant_session(company_a.id)
    try:
        assert db.query(SurveyResponse).filter(SurveyResponse.employee_id.isnot(None)).count() == 0
    finally:
        db.close()
    assert staff[5].get(f"/engagement/surveys/{s['uuid']}/results").status_code == 403


def test_threshold_cannot_go_below_five_and_optional_names(company_a):
    r = company_a.post("/engagement/surveys", json={"title": "x", "min_responses": 3})
    assert r.status_code == 400
    staff = [login_as(company_a, company_a.add_employee(f"Opt{i}", "Test")) for i in range(2)]
    s = _survey(company_a, identity="optional")
    q = company_a.get(f"/engagement/surveys/{s['uuid']}").json()["questions"][0]["uuid"]
    staff[0].post(f"/engagement/surveys/{s['uuid']}/responses", json={"answers": {q: 5}, "show_name": True})
    staff[1].post(f"/engagement/surveys/{s['uuid']}/responses", json={"answers": {q: 3}})
    db = tenant_session(company_a.id)
    try:
        named = db.query(SurveyResponse.employee_id).filter(SurveyResponse.employee_id.isnot(None)).all()
        assert [n for (n,) in named] == [staff[0].employee["id"]]
    finally:
        db.close()


def test_enps_score(company_a):
    staff = [login_as(company_a, company_a.add_employee(f"Np{i}", "Test")) for i in range(6)]
    r = company_a.post("/engagement/surveys", json={"title": "eNPS", "kind": "enps", "action": "publish"})
    assert r.status_code == 201, r.text
    s = r.json()
    q = s["questions"][0]
    assert q["qtype"] == "enps"
    for me, score in zip(staff, [10, 9, 9, 8, 3, 6]):
        assert me.post(f"/engagement/surveys/{s['uuid']}/responses", json={"answers": {q["uuid"]: score}}).status_code == 201
    res = company_a.get(f"/engagement/surveys/{s['uuid']}/results").json()
    e = res["questions"][0]["enps"]
    assert (e["promoters"], e["passives"], e["detractors"], e["score"]) == (3, 1, 2, 17)
    assert res["trend"][0]["value"] == 17


# ── Kudos ───────────────────────────────────────────────────────────

def test_kudos_feed_leaderboard_and_limits(company_a):
    giver = login_as(company_a, company_a.add_employee("Gita", "Rao"))
    r1, r2 = company_a.add_employee("Hari", "Lal"), company_a.add_employee("Isha", "Sen")
    values = giver.get("/engagement/kudos/values").json()
    assert len(values) >= 5
    k = giver.post("/engagement/kudos", json={"recipients": [r1["uuid"], r2["uuid"]], "value": values[1]["uuid"],
                                              "message": "Shipped the release over the weekend!"})
    assert k.status_code == 201, k.text
    assert {p["name"] for p in k.json()["recipients"]} == {"Hari Lal", "Isha Sen"}
    feed = giver.get("/engagement/feed", params={"filter": "kudos"}).json()["items"]
    assert feed[0]["kudos"]["uuid"] == k.json()["uuid"]
    board = giver.get("/engagement/kudos/leaderboard", params={"period": "month"}).json()
    assert {x["person"]["name"] for x in board["leaders"]} == {"Hari Lal", "Isha Sen"}
    assert board["leaders"][0]["points"] == values[1]["points"]
    assert giver.get(f"/engagement/kudos/profile/{r1['uuid']}").json()["received"] == 1
    # no kudos to yourself
    assert giver.post("/engagement/kudos", json={"recipients": [giver.employee["uuid"]], "message": "Me!"}).status_code == 400
    # monthly limit
    assert company_a.put("/engagement/settings/kudos", json={"monthly_limit": 1}).status_code == 200
    r = giver.post("/engagement/kudos", json={"recipients": [r1["uuid"]], "message": "Again, thanks a lot"})
    assert r.status_code == 400 and "1 kudos" in r.json()["detail"]


# ── Celebration job ─────────────────────────────────────────────────

def _company(db, company_id):
    return db.query(Company).filter(Company.id == company_id).one()


def test_celebration_job_is_idempotent_and_respects_privacy(company_a):
    shown = company_a.add_employee("Bina", "Das")
    hidden = company_a.add_employee("Chetan", "Mehra")
    vet = company_a.add_employee("Deepa", "Kurup")
    db = tenant_session(company_a.id)
    try:
        company = _company(db, company_a.id)
        from core.timeutil import today_for
        today = today_for(company)
        long_ago = today - timedelta(days=200)
        people.set_fallback_dates(db, company_a.id, shown["id"], birth=today.replace(year=1990), joining=long_ago)
        people.set_fallback_dates(db, company_a.id, hidden["id"], birth=today.replace(year=1991), joining=long_ago)
        people.set_fallback_dates(db, company_a.id, vet["id"], joining=today.replace(year=today.year - 3))
        people.profile_for(db, company_a.id, hidden["id"], create=True).hide_birthday = True
        db.commit()

        first = celebrations.run_morning(db, company, force=True)
        db.commit()
        second = celebrations.run_morning(db, company, force=True)
        db.commit()
        assert first["posted"] == 2 and second["posted"] == 0 and second["emailed"] == 0
        posts = db.query(BuzzPost).filter(BuzzPost.company_id == company_a.id, BuzzPost.celebration_date == today).all()
        assert sorted((p.kind, p.subject_employee_id) for p in posts) == sorted(
            [("birthday", shown["id"]), ("work_anniversary", vet["id"])])
        assert [p.years for p in posts if p.kind == "work_anniversary"] == [3]
        # the hidden birthday still gets a private email, recorded once
        ev = db.query(CelebrationEvent).filter(CelebrationEvent.company_id == company_a.id).all()
        assert len(ev) == 3 and {e.employee_id for e in ev} == {shown["id"], hidden["id"], vet["id"]}
    finally:
        db.close()
    # the celebrations list hides Chetan's birthday and never shows a birth year
    data = company_a.get("/engagement/celebrations").json()
    names = {i["person"]["name"] for i in data["today"]}
    assert "Bina Das" in names and "Chetan Mehra" not in names


def test_celebration_job_waits_for_local_hour(company_a):
    db = tenant_session(company_a.id)
    try:
        company = _company(db, company_a.id)
        from core.timeutil import now_for
        from modules.engagement import settings as cfg
        cfg.update(db, company_a.id, "celebrations", {"post_hour": 23})
        db.commit()
        if now_for(company).hour < 23:
            assert celebrations.run_morning(db, company)["waiting"] is True
    finally:
        db.close()


def test_birthday_on_29_february():
    assert celebrations.next_occurrence(date(1992, 2, 29), date(2027, 2, 1)) == date(2027, 2, 28)
    assert celebrations.next_occurrence(date(1992, 2, 29), date(2028, 2, 1)) == date(2028, 2, 29)


# ── Tenant isolation ────────────────────────────────────────────────

def test_engagement_is_isolated_between_companies(company_a, company_b):
    a_emp = login_as(company_a, company_a.add_employee("Asha", "A"))
    post = _post(a_emp, "Company A only")
    ann = company_a.post("/engagement/announcements", json={"title": "A news", "body_html": "<p>A</p>", "action": "publish"}).json()
    s = _survey(company_a)
    kudos_to = company_a.add_employee("Arun", "A")
    company_a.post("/engagement/kudos", json={"recipients": [kudos_to["uuid"]], "message": "Great work there"})

    assert "Company A only" not in _feed_bodies(company_b)
    assert company_b.get(f"/engagement/posts/{post['uuid']}").status_code == 404
    assert company_b.post(f"/engagement/posts/{post['uuid']}/react", json={"emoji": "👍"}).status_code == 404
    assert company_b.post(f"/engagement/posts/{post['uuid']}/comments", json={"body": "hi"}).status_code == 404
    assert company_b.delete(f"/engagement/posts/{post['uuid']}").status_code == 404
    assert company_b.get(f"/engagement/announcements/{ann['uuid']}").status_code == 404
    assert company_b.get(f"/engagement/surveys/{s['uuid']}/results").status_code == 404
    assert company_b.get("/engagement/kudos").json()["total"] == 0
    assert company_b.get(f"/engagement/kudos/profile/{kudos_to['uuid']}").status_code == 404
    assert all(k["name"] != "x" for k in company_b.get("/engagement/kudos/values").json())

    # kiosk links show only their own company
    a_kiosk = company_a.get("/engagement/kiosks").json()["items"][0]
    shown = company_a.client.get(f"/api/v1/public/kiosk/{a_kiosk['token']}").json()
    assert shown["company"]["name"].startswith("Alpha Works")
    assert all(x["title"] != "A news" for x in company_b.client.get(
        f"/api/v1/public/kiosk/{company_b.get('/engagement/kiosks').json()['items'][0]['token']}").json()["announcements"])
    assert company_a.client.get("/api/v1/public/kiosk/not-a-real-token-at-all-123").status_code == 404

    # directly through a session, B sees none of A's engagement rows
    db = tenant_session(company_b.id)
    try:
        assert db.query(BuzzPost).filter(BuzzPost.uuid == post["uuid"]).first() is None
        assert db.query(BuzzPost).filter(BuzzPost.company_id == company_a.id).count() == 0
    finally:
        db.close()


def test_signed_file_links(company_a):
    me = login_as(company_a, company_a.add_employee("Fiza", "Khan"))
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 50, 50)).save(buf, format="PNG")
    r = me.client.post("/api/v1/engagement/files", headers=me.headers,
                       files={"file": ("red.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200, r.text
    f = r.json()
    assert f["kind"] == "image" and f["width"] == 40
    assert me.client.get(f["url"]).status_code == 200
    assert me.client.get(f["url"].split("?")[0] + "?s=wrong").status_code == 404
    p = _post(me, "", attachments=[f["uuid"]])
    assert p["attachments"][0]["uuid"] == f["uuid"]


@pytest.mark.parametrize("html,expected", [
    ('<p onclick="x()">Hi <a href="javascript:alert(1)">x</a></p>', '<p>Hi <a href="" target="_blank" rel="noopener noreferrer">x</a></p>'),
    ("<h2>T</h2><iframe src=x></iframe><ul><li>a</li></ul>", "<h2>T</h2><ul><li>a</li></ul>"),
])
def test_announcement_html_is_sanitised(html, expected):
    from modules.engagement.content import safe_html
    assert safe_html(html) == expected
