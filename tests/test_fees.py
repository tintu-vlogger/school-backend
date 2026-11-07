"""Fees: invoice amounts (concessions, sibling rule, tax), late fee, allocation oldest first,
receipt numbering, refunds through approval, test-mode online payment, portal access and
tenant isolation."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from core.db import tenant_session
from modules.fees import service


def D(x):
    return Decimal(str(x))


class School:
    """A company set up with one class and section, fee heads and helpers."""

    def __init__(self, company):
        self.c = company
        r = company.post("/academics/quick-setup", json={"programs": ["Grade 1"], "sections": ["A"], "subjects": []})
        assert r.status_code == 200, r.text
        db = tenant_session(company.id)
        try:
            row = db.execute(text("SELECT s.id, s.program_id, s.session_id FROM academic_sections s "
                                  "WHERE s.company_id = :c ORDER BY s.id LIMIT 1"), {"c": company.id}).first()
            self.section_id, self.program_id, self.session_id = row
        finally:
            db.close()
        heads = company.get("/fees/heads?all=true").json()["items"]
        self.heads = {h["code"]: h for h in heads}
        assert "TUITION" in self.heads

    def accountant(self):
        from tests.conftest import login_as
        emp = self.c.add_employee("Ganesh", role="admin")
        return login_as(self.c, emp)

    def settings(self, **kw):
        r = self.c.put("/fees/settings", json=kw)
        assert r.status_code == 200, r.text

    def structure(self, items, instalments, **kw):
        r = self.c.post("/fees/structures", json={
            "name": "Grade 1 fees", "session_id": self.session_id, "program_id": self.program_id, "plan": "custom",
            "instalments": instalments, "items": items, **kw})
        assert r.status_code == 201, r.text
        return r.json()

    def student(self, first, dob, guardian=None, email=None):
        g = guardian or {"first_name": f"Parent{first}", "last_name": "Rao", "relation": "father", "is_primary": True,
                         "email": email or f"p-{uuid.uuid4().hex[:8]}@example.com"}
        r = self.c.post("/students", json={"first_name": first, "last_name": "Rao", "date_of_birth": dob,
                                           "section_id": self.section_id, "guardians": [g],
                                           "create_portal_accounts": True})
        assert r.status_code == 201, r.text
        return r.json()

    def generate(self, **kw):
        r = self.c.post("/fees/invoices/generate", json={"session_id": self.session_id, **kw})
        assert r.status_code == 200, r.text
        return r.json()

    def invoices(self, student):
        r = self.c.get(f"/fees/invoices?student_id={student['id']}&size=50")
        assert r.status_code == 200, r.text
        return sorted(r.json()["items"], key=lambda i: (i["due_date"], i["id"]))

    def collect(self, student, amount, **kw):
        r = self.c.post("/fees/collect", json={"student_uuid": student["uuid"],
                                               "modes": [{"mode": "cash", "amount": str(amount)}], **kw})
        assert r.status_code == 201, r.text
        return r.json()


def portal_login(company, login, password):
    r = company.client.post("/api/v1/auth/login", json={"email": login, "password": password},
                            headers={"X-Company-Slug": company.slug})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    class P:
        headers = {"Authorization": f"Bearer {token}", "X-Company-Slug": company.slug}

        def get(self, path):
            return company.client.get(f"/api/v1{path}", headers=self.headers)

        def post(self, path, json=None):
            return company.client.post(f"/api/v1{path}", headers=self.headers, json=json)
    return P()


def _parent_login(st):
    return next(x for x in st["new_logins"] if x["kind"] == "parent")


FUTURE = (date.today() + timedelta(days=30)).isoformat()
FUTURE2 = (date.today() + timedelta(days=120)).isoformat()


# ── Pure calculations ───────────────────────────────────────────────

def test_late_fee_calculation():
    due = date(2026, 1, 1)
    # per day after 5 grace days, capped at 200
    assert service.late_fee_amount("per_day", 10, 5, 200, due, date(2026, 1, 4)) == D(0)
    assert service.late_fee_amount("per_day", 10, 5, 200, due, date(2026, 1, 6)) == D(0)
    assert service.late_fee_amount("per_day", 10, 5, 200, due, date(2026, 1, 10)) == D(40)
    assert service.late_fee_amount("per_day", 10, 5, 200, due, date(2026, 3, 1)) == D(200)
    assert service.late_fee_amount("per_day", "12.50", 0, None, due, date(2026, 1, 3)) == D("25.00")
    # fixed once after the grace period
    assert service.late_fee_amount("fixed", 100, 0, None, due, date(2026, 1, 1)) == D(0)
    assert service.late_fee_amount("fixed", 100, 0, None, due, date(2026, 1, 2)) == D(100)


def test_price_lines_percent_fixed_and_tax():
    lines = [{"head_id": 1, "amount": D(10000), "tax_rate": None, "kind": "fee"},
             {"head_id": 2, "amount": D(1000), "tax_rate": D(18), "kind": "fee"},
             {"head_id": 3, "amount": D(50), "tax_rate": None, "kind": "late_fee"}]
    service.price_lines(lines, [{"head_id": 1, "kind": "percent", "value": 10, "name": "Sibling"},
                                {"head_id": None, "kind": "fixed", "value": 1100, "name": "Aid"}])
    # fixed 1100 spread 1000 / 100 over the two fee lines; late fee untouched
    assert lines[0]["concession"] == D(2000) and lines[0]["net"] == D(8000)
    assert lines[1]["concession"] == D(100) and lines[1]["tax"] == D(162) and lines[1]["net"] == D(1062)
    assert lines[2]["concession"] == D(0) and lines[2]["net"] == D(50)
    assert lines[0]["concession_note"] == "Sibling, Aid"


# ── Through the API ─────────────────────────────────────────────────

@pytest.fixture
def school(company_a):
    return School(company_a)


def test_invoice_amounts_with_concession_sibling_and_tax(school):
    s = school
    s.c.put(f"/fees/heads/{s.heads['LAB']['uuid']}", json={"tax_rate": "18"})
    s.settings(concession_approval=False, sibling_discount={"enabled": True, "percent": 10, "head_codes": ["TUITION"]})
    s.structure(items=[{"head_id": s.heads["TUITION"]["id"], "amount": "10000"},
                       {"head_id": s.heads["LAB"]["id"], "amount": "1000"},
                       {"head_id": s.heads["ANNUAL"]["id"], "amount": "3000", "billing": "once", "instalment_no": 1}],
                instalments=[{"name": "Term 1", "due_date": FUTURE}, {"name": "Term 2", "due_date": FUTURE2}])
    elder = s.student("Ravi", "2015-05-01")
    gid = s.c.get(f"/students/{elder['uuid']}").json()["guardians"][0]["guardian_id"]
    younger = s.student("Riya", "2018-05-01", guardian={"id": gid, "relation": "father"})
    r = s.c.post("/fees/concessions", json={"student_uuid": elder["uuid"], "name": "Merit", "reason": "merit",
                                            "kind": "percent", "value": "20"})
    assert r.status_code == 201 and r.json()["status"] == "approved", r.text
    res = s.generate()
    assert res["created"] == 4
    e1, e2 = s.invoices(elder)
    # elder: 20% merit on everything; lab 1000 -> 800 + 18% GST 144
    assert D(e1["total"]) == D(8000 + 944 + 2400) and D(e1["concession"]) == D(2000 + 200 + 600)
    assert D(e2["total"]) == D(8000 + 944)
    y1, y2 = s.invoices(younger)
    # younger: sibling 10% on tuition only
    assert D(y1["total"]) == D(9000 + 1180 + 3000) and D(y1["concession"]) == D(1000)
    assert D(y2["total"]) == D(9000 + 1180)
    # generating again makes nothing new
    assert s.generate()["created"] == 0
    # cancelling the merit concession reprices the open invoices
    cid = s.c.get(f"/fees/concessions?q=Merit").json()["items"][0]["uuid"]
    assert s.c.post(f"/fees/concessions/{cid}/cancel", json={"reason": "Wrong learner"}).status_code == 200
    e1 = s.invoices(elder)[0]
    assert D(e1["total"]) == D(10000 + 1180 + 3000)


def test_concession_goes_through_approval(school):
    s = school
    s.structure(items=[{"head_id": s.heads["TUITION"]["id"], "amount": "5000"}],
                instalments=[{"name": "Q1", "due_date": FUTURE}])
    st = s.student("Kiran", "2016-01-01")
    s.generate()
    acc = s.accountant()
    r = acc.post("/fees/concessions", json={"student_uuid": st["uuid"], "reason": "need", "kind": "fixed", "value": "1000"})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    assert D(s.invoices(st)[0]["total"]) == D(5000)
    task = s.c.get("/workflow/inbox?request_type=fees.concession").json()["items"][0]
    assert s.c.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": "approve"}).status_code == 200
    assert D(s.invoices(st)[0]["total"]) == D(4000)


def test_partial_payment_pays_oldest_first_and_receipt_numbers(school):
    s = school
    s.structure(items=[{"head_id": s.heads["TUITION"]["id"], "amount": "6000"},
                       {"head_id": s.heads["EXAM"]["id"], "amount": "1000"}],
                instalments=[{"name": "Q1", "due_date": FUTURE}, {"name": "Q2", "due_date": FUTURE2}])
    st = s.student("Meena", "2016-02-02")
    s.generate()
    p1 = s.collect(st, 5000)
    q1, q2 = s.invoices(st)
    assert q1["status"] == "partial" and D(q1["paid"]) == D(5000) and D(q1["balance"]) == D(2000)
    assert q2["status"] == "open" and D(q2["paid"]) == D(0)
    # tuition line is paid before the exam line
    detail = s.c.get(f"/fees/invoices/{q1['uuid']}").json()
    tuition = next(ln for ln in detail["lines"] if ln["head_code"] == "TUITION")
    assert D(tuition["paid"]) == D(5000)
    p2 = s.collect(st, 3000)
    q1, q2 = s.invoices(st)
    assert q1["status"] == "paid" and q2["status"] == "partial" and D(q2["paid"]) == D(1000)
    # receipt numbers follow the series, one after another
    assert p1["number"].startswith("RCT/") and p2["number"].startswith("RCT/")
    assert int(p2["number"].rsplit("/", 1)[1]) == int(p1["number"].rsplit("/", 1)[1]) + 1
    # cancelling a receipt opens the dues again (audited, never deleted)
    r = s.c.post(f"/fees/payments/{p2['uuid']}/cancel", json={"reason": "Entered twice"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    q1, q2 = s.invoices(st)
    assert q1["status"] == "partial" and D(q1["balance"]) == D(2000) and q2["status"] == "open"
    assert s.c.get(f"/fees/payments/{p2['uuid']}/pdf").status_code == 200


def test_late_fee_added_on_collection(school):
    s = school
    past = (date.today() - timedelta(days=15)).isoformat()
    s.structure(items=[{"head_id": s.heads["TUITION"]["id"], "amount": "1000"}],
                instalments=[{"name": "Q1", "due_date": past}])
    r = s.c.post("/fees/late-rules", json={"name": "Daily", "kind": "per_day", "amount": "10", "grace_days": 5,
                                           "max_amount": "500"})
    assert r.status_code == 201, r.text
    st = s.student("Late", "2016-03-03")
    s.generate()
    acc = s.c.get(f"/fees/students/{st['uuid']}/account").json()
    assert D(acc["late_fee"]) == D(100) and D(acc["total_due"]) == D(1100)
    pay = s.collect(st, 1100)
    assert D(pay["late_fee"]) == D(100) and D(pay["allocated"]) == D(1100)
    assert s.invoices(st)[0]["status"] == "paid"


def test_cheque_bounce_reopens_dues_and_bills_charge(school):
    s = school
    s.structure(items=[{"head_id": s.heads["TUITION"]["id"], "amount": "2000"}], instalments=[{"name": "Q1", "due_date": FUTURE}])
    st = s.student("Chq", "2016-04-04")
    s.generate()
    r = s.c.post("/fees/collect", json={"student_uuid": st["uuid"], "modes": [
        {"mode": "cheque", "amount": "2000", "reference": "123456", "bank": "SBI", "instrument_date": date.today().isoformat()}]})
    assert r.status_code == 201, r.text
    r = s.c.post(f"/fees/payments/{r.json()['uuid']}/bounce", json={"reason": "Insufficient funds", "charge": "300"})
    assert r.status_code == 200 and r.json()["status"] == "bounced"
    invs = s.invoices(st)
    assert [i["status"] for i in invs] == ["open", "open"]
    assert any(D(i["total"]) == D(300) and i["source"] == "charge" for i in invs)


def test_add_charge_for_other_modules(school, company_a):
    s = school
    st = s.student("Lib", "2016-05-05")
    db = tenant_session(company_a.id)
    try:
        inv = service.add_charge(db, company_a.id, st["id"], "FINE", 75, "Library fine: late return", None)
        db.commit()
        assert inv.number.startswith("INV/") and service.q(inv.total) == D(75)
    finally:
        db.close()
    assert D(s.c.get(f"/fees/students/{st['uuid']}/account").json()["total_due"]) == D(75)


def test_refund_flow_with_approval(school):
    s = school
    s.structure(items=[{"head_id": s.heads["TUITION"]["id"], "amount": "1000"}], instalments=[{"name": "Q1", "due_date": FUTURE}])
    st = s.student("Refu", "2016-06-06")
    s.generate()
    pay = s.collect(st, 1500)
    assert D(pay["advance"]) == D(500)
    acc = s.c.get(f"/fees/students/{st['uuid']}/account").json()
    assert D(acc["wallet"]) == D(500)
    assert s.c.post("/fees/refunds", json={"student_uuid": st["uuid"], "amount": "600", "reason": "Too much",
                                           "source": "wallet"}).status_code == 400
    accountant = s.accountant()
    r = accountant.post("/fees/refunds", json={"student_uuid": st["uuid"], "amount": "500", "reason": "Excess paid",
                                        "source": "wallet"})
    assert r.status_code == 201 and r.json()["status"] == "pending", r.text
    ref = r.json()
    assert s.c.post(f"/fees/refunds/{ref['uuid']}/pay", json={"mode": "cash"}).status_code == 400
    task = s.c.get("/workflow/inbox?request_type=fees.refund").json()["items"][0]
    assert s.c.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": "approve"}).status_code == 200
    r = s.c.post(f"/fees/refunds/{ref['uuid']}/pay", json={"mode": "bank_transfer", "reference": "NEFT123"})
    assert r.status_code == 200 and r.json()["status"] == "paid", r.text
    assert D(s.c.get(f"/fees/students/{st['uuid']}/account").json()["wallet"]) == D(0)


def test_deposit_refund_on_leaving(school):
    s = school
    s.settings(concession_approval=False)
    s.structure(items=[{"head_id": s.heads["DEPOSIT"]["id"], "amount": "5000", "billing": "once", "instalment_no": 1}],
                instalments=[{"name": "Q1", "due_date": FUTURE}])
    st = s.student("Dep", "2016-07-07")
    s.generate()
    s.collect(st, 5000)
    r = s.accountant().post(f"/fees/students/{st['uuid']}/deposit-refund")
    assert r.status_code == 201 and D(r.json()["amount"]) == D(5000)
    task = s.c.get("/workflow/inbox?request_type=fees.refund").json()["items"][0]
    s.c.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": "approve"})
    assert s.c.post(f"/fees/refunds/{r.json()['uuid']}/pay", json={"mode": "cheque", "reference": "9"}).status_code == 200
    assert D(s.c.get(f"/fees/students/{st['uuid']}/account").json()["deposit_refundable"]) == D(0)


def test_test_mode_online_payment_by_parent(school):
    s = school
    s.structure(items=[{"head_id": s.heads["TUITION"]["id"], "amount": "4000"}],
                instalments=[{"name": "Q1", "due_date": FUTURE}, {"name": "Q2", "due_date": FUTURE2}])
    st = s.student("Online", "2016-08-08")
    s.generate()
    login = _parent_login(st)
    parent = portal_login(s.c, login["login"], login["password"])
    summ = parent.get(f"/fees/portal/summary?student_id={st['id']}")
    assert summ.status_code == 200 and D(summ.json()["total"]) == D(8000) and summ.json()["test_mode"] is True
    acc = parent.get(f"/fees/portal/account?student_id={st['id']}").json()
    q1, q2 = acc["open"]
    # cannot skip the older instalment
    assert parent.post("/fees/portal/orders", json={"student_id": st["id"], "invoice_uuids": [q2["uuid"]]}).status_code == 400
    r = parent.post("/fees/portal/orders", json={"student_id": st["id"], "invoice_uuids": [q1["uuid"]]})
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["test_mode"] is True and D(order["amount"]) == D(4000)
    r = parent.post(f"/fees/portal/orders/{order['order']}/test-pay", json={"succeed": True})
    assert r.status_code == 200 and r.json()["status"] == "paid" and r.json()["receipt"], r.text
    # paying twice does nothing more
    assert parent.post(f"/fees/portal/orders/{order['order']}/test-pay", json={}).json()["status"] == "paid"
    q1, q2 = s.invoices(st)
    assert q1["status"] == "paid" and q2["status"] == "open"
    pays = s.c.get(f"/fees/payments?student_id={st['id']}").json()["items"]
    assert len(pays) == 1 and pays[0]["source"] == "online"
    assert parent.get(f"/fees/portal/receipts/{r.json()['receipt']['uuid']}/pdf").status_code == 200


def test_parents_only_see_and_pay_their_own_children(school):
    s = school
    s.structure(items=[{"head_id": s.heads["TUITION"]["id"], "amount": "1000"}], instalments=[{"name": "Q1", "due_date": FUTURE}])
    mine = s.student("Mine", "2016-09-09")
    other = s.student("Other", "2016-10-10")
    s.generate()
    login = _parent_login(mine)
    parent = portal_login(s.c, login["login"], login["password"])
    assert parent.get(f"/fees/portal/summary?student_id={mine['id']}").status_code == 200
    assert parent.get(f"/fees/portal/summary?student_id={other['id']}").status_code == 404
    assert parent.get(f"/fees/portal/account?student_id={other['id']}").status_code == 404
    other_inv = s.invoices(other)[0]
    assert parent.post("/fees/portal/orders", json={"student_id": other["id"],
                                                    "invoice_uuids": [other_inv["uuid"]]}).status_code == 404
    assert parent.get(f"/fees/portal/invoices/{other_inv['uuid']}/pdf").status_code == 404
    # staff endpoints are closed to portal sign-ins
    assert parent.get("/fees/invoices").status_code == 403
    assert parent.post("/fees/collect", json={"student_uuid": mine["uuid"], "modes": []}).status_code == 403
    # an order for another family's receipt cannot be read
    other_login = _parent_login(other)
    other_parent = portal_login(s.c, other_login["login"], other_login["password"])
    o = other_parent.post("/fees/portal/orders", json={"student_id": other["id"], "invoice_uuids": []}).json()
    assert parent.get(f"/fees/portal/orders/{o['order']}").status_code == 404
    assert parent.post(f"/fees/portal/orders/{o['order']}/test-pay", json={}).status_code == 404


def test_teacher_cannot_collect(school, company_a):
    from tests.conftest import login_as
    emp = company_a.add_employee("Tara", role="employee")
    t = login_as(company_a, emp)
    assert t.get("/fees/invoices").status_code == 403
    assert t.post("/fees/invoices/generate", json={}).status_code == 403


def test_tenant_isolation(school, company_b):
    s = school
    s.structure(items=[{"head_id": s.heads["TUITION"]["id"], "amount": "1000"}], instalments=[{"name": "Q1", "due_date": FUTURE}])
    st = s.student("Iso", "2016-11-11")
    s.generate()
    pay = s.collect(st, 400)
    inv = s.invoices(st)[0]
    assert company_b.get(f"/fees/invoices/{inv['uuid']}").status_code == 404
    assert company_b.get(f"/fees/payments/{pay['uuid']}").status_code == 404
    assert company_b.post(f"/fees/payments/{pay['uuid']}/cancel", json={"reason": "hack"}).status_code == 404
    assert company_b.post(f"/fees/invoices/{inv['uuid']}/cancel", json={"reason": "hack"}).status_code == 404
    assert company_b.get(f"/fees/students/{st['uuid']}/account").status_code == 404
    assert company_b.post("/fees/collect", json={"student_uuid": st["uuid"],
                                                 "modes": [{"mode": "cash", "amount": "1"}]}).status_code == 404
    assert company_b.get("/fees/invoices").json()["total"] == 0
    assert company_b.get("/fees/payments").json()["total"] == 0
    assert D(company_b.get("/fees/summary").json()["outstanding"]) == D(0)
    db = tenant_session(company_b.id)
    try:
        for table in ("fee_invoices", "fee_invoice_lines", "fee_payments", "fee_allocations", "fee_structures"):
            n = db.execute(text(f"SELECT count(*) FROM {table} WHERE company_id = :a"), {"a": s.c.id}).scalar()
            assert n == 0, table
    finally:
        db.close()


def test_reports_and_dues(school):
    s = school
    past = (date.today() - timedelta(days=40)).isoformat()
    s.structure(items=[{"head_id": s.heads["TUITION"]["id"], "amount": "1000"}], instalments=[{"name": "Q1", "due_date": past}])
    a = s.student("Repa", "2016-01-12")
    s.student("Repb", "2016-01-13")
    s.generate()
    s.collect(a, 1000, waive_late_fee=False)
    dues = s.c.get("/fees/dues?overdue_only=true").json()
    assert dues["total"] == 1 and dues["items"][0]["bucket"] == "31_60"
    daily = s.c.get("/fees/reports/daily").json()
    assert D(daily["total"]) == D(1000)
    heads = s.c.get("/fees/reports/heads").json()
    assert heads["rows"][0]["code"] == "TUITION"
    csv = s.c.get("/fees/reports/register?format=csv")
    assert csv.status_code == 200 and "Repa" in csv.text
    r = s.c.post("/fees/dues/remind", json={"overdue_only": True})
    assert r.status_code == 200 and r.json()["learners"] == 1
    ov = s.c.get("/fees/overview").json()
    assert D(ov["collected_today"]) == D(1000) and ov["defaulters"] == 1


def test_gateway_secrets_masked_and_razorpay_webhook(school, company_a):
    import hashlib
    import hmac
    import json as _json
    s = school
    r = s.c.put("/fees/gateway", json={"provider": "razorpay", "test_mode": True, "config": {
        "key_id": "rzp_test_abc", "key_secret": "supersecret1234", "webhook_secret": "whsec_9876"}})
    assert r.status_code == 200, r.text
    body = s.c.get("/fees/gateway").text
    assert "supersecret1234" not in body and "whsec_9876" not in body and "…1234" in body
    s.structure(items=[{"head_id": s.heads["TUITION"]["id"], "amount": "2500"}], instalments=[{"name": "Q1", "due_date": FUTURE}])
    st = s.student("Hook", "2016-12-12")
    s.generate()
    inv = s.invoices(st)[0]
    db = tenant_session(company_a.id)
    try:
        from modules.fees.models import FeeOnlineOrder
        o = FeeOnlineOrder(company_id=company_a.id, student_id=st["id"], invoice_ids=[inv["id"]], amount=D(2500),
                           currency="INR", provider="razorpay", provider_order_id="order_TEST123")
        db.add(o)
        db.commit()
    finally:
        db.close()
    payload = _json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": "pay_XYZ", "order_id": "order_TEST123"}}}}).encode()
    url = f"/api/v1/public/fees/webhook/razorpay/{s.c.slug}"
    bad = s.c.client.post(url, content=payload, headers={"X-Razorpay-Signature": "nope"})
    assert bad.status_code == 400
    sig = hmac.new(b"whsec_9876", payload, hashlib.sha256).hexdigest()
    ok = s.c.client.post(url, content=payload, headers={"X-Razorpay-Signature": sig})
    assert ok.status_code == 200, ok.text
    assert s.invoices(st)[0]["status"] == "paid"
    # a repeated webhook does not pay twice
    assert s.c.client.post(url, content=payload, headers={"X-Razorpay-Signature": sig}).status_code == 200
    assert s.c.get(f"/fees/payments?student_id={st['id']}").json()["total"] == 1
