"""Notification channels: categories and choices, email through the delivery log with
retries, approval emails with one-time links, templates, digests and isolation."""
from datetime import datetime, timedelta, timezone

import pytest

from core.db import system_session, tenant_session
from modules.notifications import channels, dispatch
from modules.notifications.models import Notification, NotificationDelivery
from modules.notifications.service import notify
from tests.conftest import login_as


@pytest.fixture(autouse=True)
def _no_background():
    old = dispatch.MODE
    dispatch.MODE = "off"
    yield
    dispatch.MODE = old


def _dispatch():
    db = system_session()
    try:
        n = dispatch.dispatch(db)
        db.commit()
        return n
    finally:
        db.close()


def _deliveries(company, **params):
    r = company.get("/notifications/deliveries", params=params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_categories_from_message_types():
    assert channels.category_for("approval") == "approvals"
    assert channels.category_for("leave_approved") == "time"
    assert channels.category_for("payslip") == "payroll"
    assert channels.category_for("kudos") == "engagement"
    assert channels.category_for("letter") == "people"
    assert channels.category_for("something_else") == "general"


def test_preferences_decide_the_channels(company_a):
    emp = company_a.add_employee("Pallavi")
    me = login_as(company_a, emp)
    prefs = me.get("/notifications/preferences").json()["items"]
    by = {p["category"]: p for p in prefs}
    assert by["approvals"]["email"] is True and by["engagement"]["email"] is False
    # turn approval emails off and engagement emails on
    r = me.put("/notifications/preferences", json={"items": [
        {"category": "approvals", "email": False, "push": False},
        {"category": "engagement", "email": True, "push": False}]})
    assert r.status_code == 200
    with tenant_session(company_a.id) as db:
        notify(db, company_a.id, [emp["id"]], kind="approval", title="Something to approve")
        notify(db, company_a.id, [emp["id"]], kind="kudos", title="You got kudos")
        db.commit()
    _dispatch()
    rows = _deliveries(company_a, channel="email")
    subjects = [d["subject"] for d in rows if d["to_address"] == emp["email"]]
    assert "You got kudos" in subjects and "Something to approve" not in subjects
    # each notification is sent once only
    assert _dispatch() == 0


def test_company_defaults_and_settings_permission(company_a):
    emp = company_a.add_employee("Zoya")
    r = company_a.put("/notifications/settings", json={"categories": {"general": {"email": True, "push": False}},
                                                       "digest": {"manager": True, "hr": False, "hour": 9}})
    assert r.status_code == 200, r.text
    assert r.json()["digest"]["hour"] == 9
    me = login_as(company_a, emp)
    assert {p["category"]: p for p in me.get("/notifications/preferences").json()["items"]}["general"]["email"] is True
    assert me.get("/notifications/settings").status_code == 403
    assert me.get("/notifications/deliveries").status_code == 403
    assert company_a.put("/notifications/settings", json={"digest": {"hour": 30}}).status_code == 400


def test_email_without_smtp_is_logged_in_development_and_failures_retry(company_a, monkeypatch):
    r = company_a.post("/notifications/test-email", json={"to": "someone@example.com"})
    assert r.status_code == 200 and r.json()["status"] == "logged"

    # now pretend a mail server exists but refuses: the message is retried, then fails
    monkeypatch.setattr(channels, "smtp_for", lambda db, cid: {"host": "x", "port": 25, "from_email": "a@b.c"})

    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(channels, "_smtp_send", boom)
    with tenant_session(company_a.id) as db:
        channels.send_email(db, company_a.id, ["x@example.com"], "Hello", "Body")
        db.commit()
    d = [x for x in _deliveries(company_a) if x["to_address"] == "x@example.com"][0]
    assert d["status"] == "queued" and d["attempts"] == 1 and "refused" in d["last_error"]
    db = system_session()
    try:
        for i in range(10):
            db.query(NotificationDelivery).filter(NotificationDelivery.to_address == "x@example.com") \
                .update({NotificationDelivery.next_attempt_at: datetime.now(timezone.utc) - timedelta(seconds=1)})
            channels.retry_due(db, company_a.id)
        db.commit()
    finally:
        db.close()
    d = [x for x in _deliveries(company_a) if x["to_address"] == "x@example.com"][0]
    assert d["status"] == "failed" and d["attempts"] == channels.MAX_ATTEMPTS
    # a manual retry after the server is fixed
    monkeypatch.setattr(channels, "_smtp_send", lambda *a, **k: None)
    r = company_a.post(f"/notifications/deliveries/{d['uuid']}/retry")
    assert r.status_code == 200 and r.json()["status"] == "sent"


def test_approval_email_has_one_time_links(company_a):
    mgr = company_a.add_employee("Manoj", role="manager")
    emp = company_a.add_employee("Asha", reporting_manager_id=mgr["id"])
    me = login_as(company_a, emp)
    r = me.post("/me/profile-change", json={"changes": {"first_name": "Aasha"}, "reason": "Spelling"})
    assert r.status_code == 201
    _dispatch()
    mail = [d for d in _deliveries(company_a) if d["to_address"] in (mgr["email"], company_a.email)]
    assert mail, "the approver was emailed"
    body = company_a.get(f"/notifications/deliveries/{mail[0]['uuid']}").json()["body"]
    assert "/approve/" in body and "Approve:" in body


def test_templates_can_be_changed_previewed_and_reset(company_a):
    items = company_a.get("/notifications/templates").json()
    assert {t["key"] for t in items} >= {"notification", "approval.request", "digest.manager", "digest.hr"}
    r = company_a.put("/notifications/templates/notification", json={"subject": "[HR] {{title}}",
                                                                     "body": "Hi {{name}}, {{message}}"})
    assert r.status_code == 200
    p = company_a.post("/notifications/templates/notification/preview",
                       json={"subject": "[HR] {{title}}", "body": "Hi {{name}}"}).json()
    assert p["subject"].startswith("[HR] ") and p["body"] == "Hi Priya"
    emp = company_a.add_employee("Tanvi")
    with tenant_session(company_a.id) as db:
        notify(db, company_a.id, [emp["id"]], kind="payslip", title="Your payslip is ready")
        db.commit()
    _dispatch()
    assert any(d["subject"] == "[HR] Your payslip is ready" for d in _deliveries(company_a))
    company_a.delete("/notifications/templates/notification")
    assert not [t for t in company_a.get("/notifications/templates").json() if t["custom"]]


def test_daily_digests_once_a_day(company_a):
    mgr = company_a.add_employee("Dina", role="manager")
    emp = company_a.add_employee("Dev", reporting_manager_id=mgr["id"])
    company_a.add_employee("Hrishi", role="hr")
    login_as(company_a, emp).post("/me/profile-change", json={"changes": {"first_name": "Devan"}, "reason": "x"})
    r = company_a.post("/notifications/digests/run")
    assert r.status_code == 200 and r.json()["sent"] >= 2
    assert company_a.post("/notifications/digests/run").json()["sent"] == 0
    digests = _deliveries(company_a, source="digest")
    subjects = " | ".join(d["subject"] for d in digests)
    assert "approval(s) waiting for you" in subjects and "HR summary" in subjects


def test_deliveries_are_isolated(company_a, company_b):
    company_a.post("/notifications/test-email", json={"to": "only-a@example.com"})
    assert not [d for d in _deliveries(company_b) if d["to_address"] == "only-a@example.com"]
    d = _deliveries(company_a)[0]
    assert company_b.get(f"/notifications/deliveries/{d['uuid']}").status_code == 404
    assert company_b.post(f"/notifications/deliveries/{d['uuid']}/retry").status_code == 404


def test_old_style_rows_are_sent_too(company_a):
    """Modules that write Notification rows directly still reach email."""
    emp = company_a.add_employee("Old")
    with tenant_session(company_a.id) as db:
        db.add(Notification(company_id=company_a.id, employee_id=emp["id"], channel="in_app",
                            message_type="leave_approved", message_content="Your leave was approved", status="sent"))
        db.commit()
    _dispatch()
    assert any(d["to_address"] == emp["email"] for d in _deliveries(company_a))
