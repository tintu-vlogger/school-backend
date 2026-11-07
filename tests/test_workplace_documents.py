"""Files, policies with acknowledgement, letter templates with merge fields, PDF letters,
self-service letter requests and e-signatures."""
from modules.documents import merge
from tests.conftest import login_as


def test_merge_field_rendering():
    html, missing = merge.render(
        "<p>Dear {{ employee.first_name }}, CTC {{salary.ctc|money}} ({{salary.ctc|words}}) from {{job.joining_date}}"
        " {{employee.full_name|upper}} {{employee.pan}} {{company.logo}}</p>",
        {"employee.first_name": "Asha <b>", "salary.ctc": "1250000.5", "job.joining_date": "2026-06-03",
         "employee.full_name": "Asha Menon", "company.logo": '<img src="data:image/png;base64,x">'})
    assert "Dear Asha &lt;b&gt;," in html  # values are escaped
    assert "₹12,50,000.50" in html
    assert "Rupees twelve lakh fifty thousand and fifty paise only" in html
    assert "3 June 2026" in html and "ASHA MENON" in html
    assert '<img src="data:image/png;base64,x">' in html  # image fields stay HTML
    assert missing == ["employee.pan"]
    assert merge.number_in_words(10_05_00_007) == "ten crore five lakh seven"
    assert merge.indian_grouping(123456789) == "12,34,56,789"
    assert merge.fields_used("{{a.b}} {{ c.d | money }}") == ["a.b", "c.d"]


def test_every_letter_type_has_a_template_and_designs(company_a):
    types = {t["value"] for t in company_a.get("/letters/meta").json()["letter_types"]}
    have = {t["letter_type"] for t in company_a.get("/letters/templates").json()["items"]}
    assert types - {"other"} <= have
    kinds = [lh["kind"] for lh in company_a.get("/letters/letterheads").json()["items"]]
    assert kinds.count("header") >= 2 and kinds.count("footer") >= 2


def test_generate_letter_pdf_and_file_it(company_a):
    emp = company_a.add_employee("Priya", "Sharma")
    tpl = next(t for t in company_a.get("/letters/templates", params={"letter_type": "increment"}).json()["items"])
    prev = company_a.post("/letters/preview", json={"template_uuid": tpl["uuid"], "employee_id": emp["id"],
                                                     "values": {"salary.ctc": "1500000"}}).json()
    assert "Priya" in prev["html"] and "₹15,00,000.00" in prev["html"]
    # live preview of unsaved designer changes
    live = company_a.post("/letters/preview", json={"template_uuid": tpl["uuid"], "template": {
        "name": "x", "letter_type": "increment", "body_html": "<p>Hello {{employee.first_name}}</p><script>x()</script>"},
        "employee_id": emp["id"]}).json()
    assert "Hello Priya" in live["html"] and "<script" not in live["html"]
    # required extra fields are enforced
    r = company_a.post("/letters/generate", json={"template_uuid": tpl["uuid"], "employee_id": emp["id"], "values": {}})
    assert r.status_code == 400
    r = company_a.post("/letters/generate", json={"template_uuid": tpl["uuid"], "employee_id": emp["id"],
                                                   "values": {"salary.ctc": "1500000", "salary.effective_from": "2026-04-01"}})
    assert r.status_code == 201, r.text
    letter = r.json()
    assert letter["reference_no"].startswith("LTR/") and letter["status"] == "issued"
    pdf = company_a.client.get(letter["file"]["url"])
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    # a tampered link is refused
    assert company_a.client.get(letter["file"]["url"].replace("sig=", "sig=0")).status_code == 403
    # the employee finds it in My documents
    me = login_as(company_a, emp)
    assert any(f["title"] == letter["title"] for f in me.get("/documents/me/files").json()["items"])
    assert any(l["uuid"] == letter["uuid"] for l in me.get("/letters/me").json()["items"])


def test_self_service_letter_request_through_approval(company_a):
    emp = company_a.add_employee("Arun")
    me = login_as(company_a, emp)
    options = me.get("/letters/me/requestable").json()["items"]
    tpl = next(t for t in options if t["letter_type"] == "address_proof")
    r = me.post("/letters/me/requests", json={"template_uuid": tpl["uuid"], "reason_text": "Passport", "purpose": "Passport office"})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    task = company_a.get("/workflow/inbox").json()["items"][0]
    assert company_a.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": "approve"}).json()["status"] == "approved"
    reqs = me.get("/letters/me/requests").json()["items"]
    assert reqs[0]["status"] == "approved" and reqs[0]["letter_uuid"]
    assert me.get(f"/letters/{reqs[0]['letter_uuid']}").status_code == 200
    # a letter that is not self-service can't be requested
    hr_only = next(t for t in company_a.get("/letters/templates", params={"letter_type": "warning"}).json()["items"])
    assert me.post("/letters/me/requests", json={"template_uuid": hr_only["uuid"]}).status_code == 400


def test_esign_with_email_code_and_signing_order(company_a):
    mgr = company_a.add_employee("Meena", "Manager")
    emp = company_a.add_employee("Dev", "Patel", reporting_manager_id=mgr["id"])
    tpl = next(t for t in company_a.get("/letters/templates", params={"letter_type": "confirmation"}).json()["items"])
    letter = company_a.post("/letters/generate", json={"template_uuid": tpl["uuid"], "employee_id": emp["id"]}).json()
    r = company_a.post(f"/letters/{letter['uuid']}/esign", json={"signers": [{"kind": "employee"}, {"kind": "manager"}],
                                                               "signing_order": True})
    assert r.status_code == 201, r.text
    signers = r.json()["esign"]["signers"]
    assert [s["status"] for s in signers] == ["pending", "waiting"]
    e, m = login_as(company_a, emp), login_as(company_a, mgr)
    msig = m.get("/letters/me/signatures").json()["items"][0]
    assert m.post(f"/letters/sign/{msig['uuid']}/otp").status_code == 400  # not their turn yet
    esig = e.get("/letters/me/signatures").json()["items"][0]
    otp = e.post(f"/letters/sign/{esig['uuid']}/otp").json()
    assert otp["dev_code"] and "•" in otp["sent_to"]
    assert e.post(f"/letters/sign/{esig['uuid']}", json={"code": "000000", "typed_name": "Dev Patel"}).status_code == 400 \
        or otp["dev_code"] == "000000"
    png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    assert e.post(f"/letters/sign/{esig['uuid']}", json={"code": otp["dev_code"], "signature_image": png}).json()["status"] == "out_for_signature"
    code = m.post(f"/letters/sign/{msig['uuid']}/otp").json()["dev_code"]
    assert m.post(f"/letters/sign/{msig['uuid']}", json={"code": code, "typed_name": "Meena M"}).json()["status"] == "completed"
    done = company_a.get(f"/letters/{letter['uuid']}").json()
    assert done["status"] == "signed" and done["esign"]["signed_file"]
    signed = company_a.client.get(done["esign"]["signed_file"]["url"])
    assert signed.content[:5] == b"%PDF-"
    titles = [f["title"] for f in e.get("/documents/me/files").json()["items"]]
    assert any(t.endswith("(signed)") for t in titles)
    # Aadhaar eSign is designed in but not connected
    l2 = company_a.post("/letters/generate", json={"template_uuid": tpl["uuid"], "employee_id": emp["id"]}).json()
    assert company_a.post(f"/letters/{l2['uuid']}/esign", json={"provider": "aadhaar"}).status_code == 400


def test_policy_acknowledgement_tracking(company_a):
    a, b = company_a.add_employee("Anu"), company_a.add_employee("Bhanu")
    p = company_a.post("/documents/policies", json={"title": "Code of conduct", "ack_due_days": 10}).json()
    assert company_a.post(f"/documents/policies/{p['uuid']}/versions", json={}).status_code == 400
    v1 = company_a.post(f"/documents/policies/{p['uuid']}/versions", json={"body_html": "<p>Be kind</p>"})
    assert v1.status_code == 201 and v1.json()["ack_due_on"]
    stats = company_a.get(f"/documents/policies/{p['uuid']}").json()["stats"]
    assert stats["assigned"] >= 2 and stats["acknowledged"] == 0
    sa = login_as(company_a, a)
    mine = sa.get("/documents/me/policies").json()["items"]
    assert next(x for x in mine if x["uuid"] == p["uuid"])["my_status"] == "pending"
    assert sa.get("/documents/me/pending-count").json()["policies"] >= 1
    assert sa.post(f"/documents/policies/{p['uuid']}/acknowledge").status_code == 200
    rep = company_a.get(f"/documents/policies/{p['uuid']}/acknowledgements").json()["items"]
    status = {r["employee_id"]: r["status"] for r in rep}
    assert status[a["id"]] == "acknowledged" and status[b["id"]] == "pending"
    assert company_a.get(f"/documents/policies/{p['uuid']}/acknowledgements", params={"format": "csv"}).text.count("\n") >= 3
    assert company_a.post(f"/documents/policies/{p['uuid']}/remind").json()["reminded"] >= 1
    # a new version asks everyone again; a minor one carries acknowledgements over
    company_a.post(f"/documents/policies/{p['uuid']}/versions", json={"body_html": "<p>Typo fixed</p>", "ask_again": False})
    rep = company_a.get(f"/documents/policies/{p['uuid']}/acknowledgements").json()["items"]
    assert {r["employee_id"]: r["status"] for r in rep}[a["id"]] == "acknowledged"
    company_a.post(f"/documents/policies/{p['uuid']}/versions", json={"body_html": "<p>New rules</p>"})
    rep = company_a.get(f"/documents/policies/{p['uuid']}/acknowledgements").json()["items"]
    assert {r["employee_id"]: r["status"] for r in rep}[a["id"]] == "pending"
    # people who join later are asked too
    late = company_a.add_employee("Late")
    assert any(x["uuid"] == p["uuid"] and x["my_status"] == "pending"
               for x in login_as(company_a, late).get("/documents/me/policies").json()["items"])


def test_files_folders_and_private_locker(company_a):
    emp = company_a.add_employee("Lata")
    me = login_as(company_a, emp)
    up = me.post("/documents/uploads", files={"file": ("passport.pdf", b"%PDF-1.4 test", "application/pdf")}, json=None)
    assert up.status_code == 201, up.text
    locker = next(f for f in me.get("/documents/folders").json()["items"] if f["area"] == "personal")
    r = me.post("/documents/files", json={"folder_uuid": locker["uuid"], "upload_uuid": up.json()["uuid"], "title": "Passport"})
    assert r.status_code == 201
    # the owner (admin) can't list or open someone's locker, only see usage
    assert company_a.get("/documents/files", params={"folder": locker["uuid"]}).status_code == 403
    assert company_a.get(f"/documents/files/{r.json()['uuid']}").status_code == 404
    assert any(u["employee_id"] == emp["id"] and u["files"] == 1 for u in company_a.get("/documents/lockers").json()["items"])
    # executables are refused
    bad = me.post("/documents/uploads", files={"file": ("setup.exe", b"MZ\x90\x00", "application/octet-stream")}, json=None)
    assert bad.status_code == 400
    # company files follow their audience
    folder = next(f for f in company_a.get("/documents/folders", params={"area": "company"}).json()["items"])
    up2 = company_a.post("/documents/uploads", files={"file": ("hr.pdf", b"%PDF-1.4 x", "application/pdf")}, json=None).json()
    hidden = company_a.post("/documents/files", json={"folder_uuid": folder["uuid"], "upload_uuid": up2["uuid"],
                                                      "title": "Managers only", "audience": {"all": False, "roles": ["manager"]}})
    assert hidden.status_code == 201
    seen = [f["title"] for f in me.get("/documents/files", params={"folder": folder["uuid"]}).json()["items"]]
    assert "Managers only" not in seen
