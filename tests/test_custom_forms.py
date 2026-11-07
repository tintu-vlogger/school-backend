"""Custom services (no-code forms): field validation, show-if, formulas, field access by
role, versioned forms, approval through the workflow engine, number series, statuses,
exports, saved views, reports and tenant isolation."""
from datetime import date

import pytest

from tests.conftest import login_as
from core.errors import BadRequest
from modules.custom_forms import fields as F


# ── Pure rules ──────────────────────────────────────────────────────

def _form(*fields):
    return F.clean_definition({"fields": list(fields)})


def test_definition_checks():
    with pytest.raises(BadRequest, match="two fields"):
        _form({"key": "a", "type": "text", "label": "A"}, {"key": "a", "type": "number", "label": "B"})
    with pytest.raises(BadRequest, match="at least one option"):
        _form({"type": "single_choice", "label": "Pick", "options": []})
    with pytest.raises(BadRequest, match="formula has a mistake"):
        _form({"type": "number", "label": "Qty"}, {"type": "formula", "label": "Total", "formula": "qty * price"})
    with pytest.raises(BadRequest, match="inside a table"):
        _form({"type": "table", "label": "Rows", "columns": [{"type": "file", "label": "Doc"}]})
    with pytest.raises(BadRequest, match="listed twice"):
        _form({"type": "multi_choice", "label": "Days", "options": ["Mon", "mon"]})
    with pytest.raises(BadRequest, match="starting value"):
        _form({"type": "single_choice", "label": "Size", "options": ["S", "M"], "default": "XL"})
    with pytest.raises(BadRequest, match="at least one field"):
        _form({"type": "section", "label": "Only a heading"})
    d = _form({"type": "text", "label": "Visitor name"}, {"type": "number", "label": "Qty", "min": 1})
    assert [f["key"] for f in d["fields"]] == ["visitor_name", "qty"]


def test_value_types():
    def one(f, v):
        return F.clean_value(f, v, lookups=None)
    assert one({"type": "currency", "label": "Fee"}, "1,500.5") == "1500.50"
    with pytest.raises(BadRequest, match="at most 2 decimal"):
        one({"type": "currency", "label": "Fee"}, "10.123")
    with pytest.raises(BadRequest, match="whole number"):
        one({"type": "number", "label": "Qty"}, "2.5")
    with pytest.raises(BadRequest, match="at least 1"):
        one({"type": "number", "label": "Qty", "min": 1}, 0)
    assert one({"type": "decimal", "label": "Hours", "decimals": 1}, 2.5) == "2.5"
    with pytest.raises(BadRequest, match="email"):
        one({"type": "email", "label": "Email"}, "not-an-email")
    with pytest.raises(BadRequest, match="phone"):
        one({"type": "phone", "label": "Mobile"}, "abc")
    with pytest.raises(BadRequest, match="http"):
        one({"type": "url", "label": "Link"}, "example.com")
    assert one({"type": "date", "label": "D"}, "2026-11-07") == "2026-11-07"
    assert one({"type": "datetime", "label": "At"}, "2026-11-07T10:00:00+05:30") == "2026-11-07T04:30:00Z"
    assert one({"type": "time", "label": "T"}, "09:30") == "09:30"
    assert one({"type": "boolean", "label": "B"}, "yes") is True
    assert one({"type": "single_choice", "label": "Size", "options": ["Small", "Large"]}, "small") == "Small"
    assert one({"type": "multi_choice", "label": "Days", "options": ["Mon", "Tue"]}, ["Tue", "Mon"]) == ["Tue", "Mon"]
    with pytest.raises(BadRequest, match="from the list"):
        one({"type": "single_choice", "label": "Size", "options": ["Small"]}, "Huge")
    with pytest.raises(BadRequest, match="at most 5"):
        one({"type": "text", "label": "Code", "max_length": 5}, "toolong")


def test_required_unknown_and_tables():
    d = _form({"key": "name", "type": "text", "label": "Name", "required": True},
              {"key": "items", "type": "table", "label": "Items", "required": True, "columns": [
                  {"key": "qty", "type": "number", "label": "Qty", "required": True},
                  {"key": "price", "type": "currency", "label": "Price"},
                  {"key": "amount", "type": "formula", "label": "Amount", "formula": "qty * coalesce(price, 0)",
                   "result": "currency"}]},
              {"key": "total", "type": "formula", "label": "Total", "formula": "items.amount", "result": "currency"},
              {"key": "lines", "type": "formula", "label": "Lines", "formula": "items.count", "result": "number",
               "decimals": 0})
    with pytest.raises(BadRequest, match="Name is required"):
        F.process(d, {"items": [{"qty": 1}]}, roles={"employee"})
    with pytest.raises(BadRequest, match="Unknown field"):
        F.process(d, {"name": "x", "nope": 1}, roles={"employee"})
    with pytest.raises(BadRequest, match="Items is required"):
        F.process(d, {"name": "x", "items": []}, roles={"employee"})
    two = _form({"key": "rows", "type": "table", "label": "Rows", "min_rows": 2,
                 "columns": [{"key": "n", "type": "text", "label": "N"}]})
    with pytest.raises(BadRequest, match="Rows needs at least 2 rows"):
        F.process(two, {"rows": [{"n": "a"}]}, roles={"employee"})
    with pytest.raises(BadRequest, match="row 1: Qty is required"):
        F.process(d, {"name": "x", "items": [{"price": "5"}]}, roles={"employee"})
    out = F.process(d, {"name": "Pens", "items": [{"qty": 2, "price": "10.50"}, {"qty": 3, "price": "1"}]},
                    roles={"employee"})
    assert out["items"][0]["amount"] == "21.00"
    assert out["total"] == "24.00" and out["lines"] == "2"
    # drafts skip required checks
    assert F.process(d, {}, roles={"employee"}, check_required=False) == {}


def test_show_if_and_sections():
    d = _form({"key": "kind", "type": "single_choice", "label": "Kind", "options": ["Internal", "External"],
               "required": True},
              {"key": "provider", "type": "text", "label": "Provider", "required": True,
               "show_if": "kind == 'External'"},
              {"type": "section", "label": "Travel", "show_if": "kind == 'External'"},
              {"key": "hotel", "type": "boolean", "label": "Hotel"},
              {"key": "nights", "type": "number", "label": "Nights", "required": True, "show_if": "hotel == true"},
              {"type": "section", "label": "Other"},
              {"key": "note", "type": "text", "label": "Note", "show_if": "requester.role == 'manager'"})
    # hidden fields are not required and their values are dropped
    out = F.process(d, {"kind": "Internal", "provider": "Acme", "hotel": True}, roles={"employee"},
                    requester={"role": "employee"})
    assert out == {"kind": "Internal"}
    with pytest.raises(BadRequest, match="Provider is required"):
        F.process(d, {"kind": "External"}, roles={"employee"})
    with pytest.raises(BadRequest, match="Nights is required"):
        F.process(d, {"kind": "External", "provider": "Acme", "hotel": True}, roles={"employee"})
    out = F.process(d, {"kind": "External", "provider": "Acme", "hotel": True, "nights": 2, "note": "hi"},
                    roles={"employee"}, requester={"role": "employee"})
    assert out["nights"] == 2 and "note" not in out
    out = F.process(d, {"kind": "Internal", "note": "hi"}, roles={"manager"}, requester={"role": "manager"})
    assert out["note"] == "hi"
    _, visible = F.compute(d, {"kind": "External", "hotel": False}, {"role": "employee"})
    assert "provider" in visible and "hotel" in visible and "nights" not in visible


def test_field_access_by_role():
    d = _form({"key": "reason", "type": "text", "label": "Reason", "required": True},
              {"key": "card_no", "type": "text", "label": "Card number", "required": True,
               "permissions": {"employee": "view"}},
              {"key": "notes", "type": "text", "label": "HR notes", "permissions": {"employee": "hidden",
                                                                                 "manager": "view"}})
    f_card, f_notes = d["fields"][1], d["fields"][2]
    assert F.access_for(f_card, {"employee"}) == "view"
    assert F.access_for(f_card, {"hr"}) == "edit"
    assert F.access_for(f_notes, {"employee", "manager"}) == "view"   # widest role wins
    assert F.access_for(f_notes, {"employee"}, full=True) == "edit"
    # the employee can't set (or be asked for) fields they may only view
    out = F.process(d, {"reason": "Lost", "card_no": "X1", "notes": "y"}, roles={"employee"})
    assert out == {"reason": "Lost"}
    # HR fills them in later
    out = F.process(d, {"card_no": "C-77"}, existing=out, roles={"hr"})
    assert out == {"reason": "Lost", "card_no": "C-77"}
    assert F.strip_hidden(d, {**out, "notes": "secret"}, {"employee"}) == {"reason": "Lost", "card_no": "C-77"}
    seen = F.visible_definition(d, {"employee"})
    assert [f["key"] for f in seen["fields"]] == ["reason", "card_no"]
    assert seen["fields"][1]["access"] == "view"


def test_defaults_and_tokens():
    d = _form({"key": "host", "type": "employee", "label": "Host", "default": "me"},
              {"key": "on", "type": "date", "label": "On", "default": "today"},
              {"key": "office", "type": "location", "label": "Office", "default": "my"})
    vals = F.defaults_for(d, {"id": 7, "location_id": 3, "today": date(2026, 11, 7)})
    assert vals == {"host": 7, "on": "2026-11-07", "office": 3}


# ── Through the API ─────────────────────────────────────────────────

def _install(company, key="training_request"):
    r = company.post(f"/custom-forms/templates/{key}/install")
    assert r.status_code == 201, r.text
    return r.json()


def _tasks(session):
    return session.get("/workflow/inbox").json()["items"]


TRAINING = {"training_title": "Advanced Excel", "training_type": "External classroom", "provider": "NIIT",
            "start_date": "2026-12-01", "end_date": "2026-12-03", "justification": "Monthly MIS reports",
            "fee": "12000", "travel_needed": True, "travel_cost": "3500.50"}


def test_template_flow_numbering_and_approval(company_a):
    svc = _install(company_a)
    assert svc["code"] == "training_request" and svc["request_type"] == f"form.{company_a.id}.training_request"
    assert svc["flows"] == 1 and svc["series"]["prefix"] == "TRN"
    # the request type is offered on the approval flows screen (with the form's fields)
    types = {t["key"]: t for t in company_a.get("/workflow/request-types").json()}
    rt = types[svc["request_type"]]
    assert rt["label"] == "Training request"
    assert any(v["name"] == "request.total_cost" for v in rt["variables"])
    # installing twice is refused
    assert company_a.post("/custom-forms/templates/training_request/install").status_code == 400

    hr = company_a.add_employee("Hema", role="hr")
    mgr = company_a.add_employee("Mohan", role="manager")
    emp = company_a.add_employee("Esha", reporting_manager_id=mgr["id"])
    es = login_as(company_a, emp)
    r = es.post("/custom-forms/services/training_request/records", json={"values": TRAINING})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["status"] == "pending"
    assert rec["values"]["total_cost"] == "15500.50" and rec["values"]["days"] == "3"
    assert rec["reference_no"].startswith("TRN-")
    first_no = int(rec["reference_no"].rsplit("-", 1)[1])
    rec2 = es.post("/custom-forms/services/training_request/records",
                   json={"values": {**TRAINING, "training_title": "SQL basics"}}).json()
    assert int(rec2["reference_no"].rsplit("-", 1)[1]) == first_no + 1
    # hidden HR notes are not part of the employee's form
    assert "hr_notes" not in [f.get("key") for f in rec["definition"]["fields"]]

    # manager approves, then HR
    ms = login_as(company_a, mgr)
    t = next(t for t in _tasks(ms) if t["request"]["title"].startswith("Training request"))
    assert ms.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"}).status_code == 200
    hs = login_as(company_a, hr)
    t = next(t for t in _tasks(hs) if t["request"]["title"] == rec["title"])
    assert hs.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"}).status_code == 200
    done = es.get(f"/custom-forms/records/{rec['uuid']}").json()
    assert done["status"] == "approved"
    assert [x["status"] for x in done["approval"]["tasks"]] == ["approved", "approved"]

    # the employee can't move statuses; HR can, and the employee is told
    assert es.post(f"/custom-forms/records/{rec['uuid']}/status", json={"status": "nominated"}).status_code == 403
    r = hs.post(f"/custom-forms/records/{rec['uuid']}/status", json={"status": "completed", "note": "Well done"})
    assert r.status_code == 200 and r.json()["closed"] is True
    assert any(a["kind"] == "status" and a["data"].get("to") == "completed" for a in r.json()["activity"])

    # a rejection closes the second record
    t = next(t for t in _tasks(ms) if t["request"]["title"] == rec2["title"])
    ms.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "reject", "comment": "Not this quarter"})
    assert es.get(f"/custom-forms/records/{rec2['uuid']}").json()["status"] == "rejected"


def test_no_approval_service_and_field_access(company_a):
    _install(company_a, "id_card_request")
    emp = company_a.add_employee("Ravi")
    hr = company_a.add_employee("Hari", role="hr")
    es, hs = login_as(company_a, emp), login_as(company_a, hr)
    up = es.post("/documents/uploads", files={"file": ("me.png", b"\x89PNG\r\n\x1a\nxxxx", "image/png")}, json=None)
    assert up.status_code == 201, up.text
    # card number is view-only for employees: whatever they send is ignored
    r = es.post("/custom-forms/services/id_card_request/records",
                json={"values": {"reason": "Lost", "reason_details": "Lost on the bus", "photo": [up.json()["uuid"]],
                                 "name_on_card": "Ravi K", "card_number": "HACK"}})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["status"] == "open" and "card_number" not in rec["values"]
    assert rec["values"]["replacement_fee"] == "200.00"
    assert rec["files"]["photo"][0]["name"] == "me.png"
    # a PDF is refused where only images are allowed
    pdf = es.post("/documents/uploads", files={"file": ("a.pdf", b"%PDF-1.4 x", "application/pdf")}, json=None).json()
    bad = es.post("/custom-forms/services/id_card_request/records",
                  json={"values": {"reason": "New joiner", "photo": [pdf["uuid"]], "name_on_card": "R"}})
    assert bad.status_code == 400 and "images" in bad.json()["detail"]
    # the requester can't edit after submitting; HR fills the HR fields
    assert es.put(f"/custom-forms/records/{rec['uuid']}", json={"values": {"name_on_card": "X"}}).status_code == 403
    r = hs.put(f"/custom-forms/records/{rec['uuid']}", json={"values": {"card_number": "IDC-0099"}})
    assert r.status_code == 200, r.text
    assert r.json()["values"]["card_number"] == "IDC-0099"
    assert any(a["kind"] == "edit" for a in r.json()["activity"])
    # employee sees it (view only)
    mine = es.get(f"/custom-forms/records/{rec['uuid']}").json()
    assert mine["values"]["card_number"] == "IDC-0099" and mine["can_edit"] is False
    # another employee can't open it
    other = login_as(company_a, company_a.add_employee("Nina"))
    assert other.get(f"/custom-forms/records/{rec['uuid']}").status_code == 404
    assert other.get("/custom-forms/services/id_card_request/records?scope=all").status_code == 403


def test_versions_keep_old_records_readable(company_a):
    s = company_a.post("/custom-forms/services", json={"name": "Uniform request", "needs_approval": False, "fields": [
        {"key": "size", "type": "single_choice", "label": "Size", "options": ["S", "M", "L"], "required": True}]})
    assert s.status_code == 201, s.text
    code = s.json()["code"]
    old = company_a.post(f"/custom-forms/services/{code}/records", json={"values": {"size": "M"}}).json()
    r = company_a.put(f"/custom-forms/services/{code}/form", json={"change_note": "Shirt size and pairs", "fields": [
        {"key": "shirt_size", "type": "single_choice", "label": "Shirt size", "options": ["S", "M", "L", "XL"],
         "required": True},
        {"key": "pairs", "type": "number", "label": "Pairs", "min": 1, "max": 3, "required": True}]})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    detail = company_a.get(f"/custom-forms/records/{old['uuid']}").json()
    assert detail["version"] == 1 and detail["values"] == {"size": "M"}
    assert [f["key"] for f in detail["definition"]["fields"]] == ["size"]
    assert detail["version_is_current"] is False
    new = company_a.post(f"/custom-forms/services/{code}/records", json={"values": {"shirt_size": "XL", "pairs": 2}})
    assert new.status_code == 201 and new.json()["version"] == 2
    # a field can't change kind across versions
    bad = company_a.put(f"/custom-forms/services/{code}/form", json={"fields": [
        {"key": "size", "type": "number", "label": "Size"}]})
    assert bad.status_code == 400 and "different kind" in bad.json()["detail"]
    versions = company_a.get(f"/custom-forms/services/{code}/versions").json()
    assert [v["version"] for v in versions] == [2, 1] and versions[1]["records"] == 1


def test_unique_drafts_and_cancel(company_a):
    s = company_a.post("/custom-forms/services", json={"name": "Parking pass", "needs_approval": True, "fields": [
        {"key": "vehicle", "type": "text", "label": "Vehicle number", "required": True, "unique": True},
        {"key": "kind", "type": "single_choice", "label": "Kind", "options": ["Car", "Bike"]}]}).json()
    code = s["code"]
    emp = company_a.add_employee("Uma")
    es = login_as(company_a, emp)
    # a draft may be incomplete and can be submitted later
    d = es.post(f"/custom-forms/services/{code}/records", json={"values": {"kind": "Car"}, "draft": True}).json()
    assert d["status"] == "draft" and d["reference_no"] is None
    assert es.post(f"/custom-forms/records/{d['uuid']}/submit").json()["detail"] == "Vehicle number is required."
    es.put(f"/custom-forms/records/{d['uuid']}", json={"values": {"vehicle": "KL07AB1234"}})
    sub = es.post(f"/custom-forms/records/{d['uuid']}/submit").json()
    assert sub["status"] == "pending" and sub["reference_no"]
    # drafts are invisible to others; unique values are enforced on submit
    dup = es.post(f"/custom-forms/services/{code}/records", json={"values": {"vehicle": "kl07ab1234"}})
    assert dup.status_code == 400 and "already used" in dup.json()["detail"]
    # withdrawing a pending record cancels its approval
    c = es.post(f"/custom-forms/records/{sub['uuid']}/cancel", json={"reason": "Sold the car"}).json()
    assert c["status"] == "cancelled" and c["approval"]["status"] == "cancelled"
    # once withdrawn the number can be used again
    again = es.post(f"/custom-forms/services/{code}/records", json={"values": {"vehicle": "KL07AB1234"}})
    assert again.status_code == 201
    # a draft can be deleted; a submitted record can't
    d2 = es.post(f"/custom-forms/services/{code}/records", json={"values": {}, "draft": True}).json()
    assert es.delete(f"/custom-forms/records/{d2['uuid']}").status_code == 204
    assert es.delete(f"/custom-forms/records/{again.json()['uuid']}").status_code == 400


def test_submit_access_nav_and_settings(company_a):
    s = company_a.post("/custom-forms/services", json={
        "name": "Laptop request", "nav_section": "people", "icon": "laptop",
        "submit_access": {"everyone": False, "roles": ["manager"]}}).json()
    code = s["code"]
    mgr, emp = company_a.add_employee("Manu", role="manager"), company_a.add_employee("Eli")
    ms, es = login_as(company_a, mgr), login_as(company_a, emp)
    assert [n["code"] for n in ms.get("/custom-forms/nav").json()] == [code]
    assert ms.get("/custom-forms/nav").json()[0]["nav_section"] == "people"
    assert es.get("/custom-forms/nav").json() == []
    assert es.post(f"/custom-forms/services/{code}/records", json={"values": {"details": "x"}}).status_code == 403
    assert ms.post(f"/custom-forms/services/{code}/records", json={"values": {"details": "x"}}).status_code == 201
    # only designers change services
    assert ms.put(f"/custom-forms/services/{code}", json={"name": "Mine"}).status_code == 403
    assert es.get("/custom-forms/services").status_code == 403
    # switched off: gone from the menu
    company_a.put(f"/custom-forms/services/{code}", json={"active": False})
    assert ms.get("/custom-forms/nav").json() == []
    # statuses in use can't be removed
    company_a.put(f"/custom-forms/services/{code}", json={"active": True, "needs_approval": False})
    rec = ms.post(f"/custom-forms/services/{code}/records", json={"values": {"details": "y"}}).json()
    assert rec["status"] == "open"
    r = company_a.put(f"/custom-forms/services/{code}", json={"statuses": [
        {"key": "in_progress", "label": "Working"}, {"key": "completed", "label": "Done", "closed": True}]})
    assert r.status_code == 400 and "used by records" in r.json()["detail"]
    # names are unique
    assert company_a.post("/custom-forms/services", json={"name": "laptop request"}).status_code == 400


def test_lists_views_export_comments_and_reports(company_a):
    _install(company_a, "visitor_pass")
    items = company_a.get("/masters/options/state", params={"q": "Kerala"}).json()["items"]
    state = next(i["value"] for i in items if i["label"] == "Kerala")
    loc = company_a.post("/organization/locations", json={"name": "Kochi office", "city": "Kochi", "state_id": state})
    assert loc.status_code == 201, loc.text
    assert [o["label"] for o in company_a.get("/custom-forms/lookups/location?q=kochi").json()] == ["Kochi office"]
    emp = company_a.add_employee("Vani", location_id=loc.json()["id"])
    es = login_as(company_a, emp)
    for name, purpose in (("Arun Rao", "Meeting"), ("Bela Shah", "Interview"), ("Chand", "Meeting")):
        r = es.post("/custom-forms/services/visitor_pass/records", json={"values": {
            "visitor_name": name, "visitor_phone": "+91 98450 12345", "purpose": purpose,
            "others": [{"name": "Driver"}]}})
        assert r.status_code == 201, r.text
        assert r.json()["values"]["host"] == emp["id"] and r.json()["values"]["group_size"] == "2"
    page = company_a.get("/custom-forms/services/visitor_pass/records?scope=all&f_purpose=Meeting").json()
    assert page["total"] == 2
    assert page["items"][0]["labels"].get(f"employee:{emp['id']}")
    mine = es.get("/custom-forms/my-requests").json()
    assert mine["total"] == 3 and mine["items"][0]["service"]["code"] == "visitor_pass"
    # saved views
    v = company_a.post("/custom-forms/services/visitor_pass/views",
                       json={"name": "Meetings", "filters": {"purpose": ["Meeting"]}, "columns": ["visitor_name"],
                             "shared": True}).json()
    assert v["shared"] is True
    assert [x["name"] for x in es.get("/custom-forms/services/visitor_pass/views").json()] == ["Meetings"]
    assert es.delete(f"/custom-forms/views/{v['uuid']}").status_code == 403
    # export
    csv = company_a.get("/custom-forms/services/visitor_pass/export?format=csv")
    assert csv.status_code == 200 and "Visitor name" in csv.text and "Arun Rao" in csv.text
    xlsx = company_a.get("/custom-forms/services/visitor_pass/export?format=xlsx")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    # comments with a file
    rec = page["items"][0]
    up = company_a.post("/documents/uploads", files={"file": ("pass.pdf", b"%PDF-1.4 y", "application/pdf")}, json=None)
    acts = company_a.post(f"/custom-forms/records/{rec['uuid']}/comments",
                          json={"body": "Pass printed", "attachment_uuids": [up.json()["uuid"]]}).json()
    assert acts[-1]["body"] == "Pass printed" and acts[-1]["attachments"][0]["name"] == "pass.pdf"
    # records show up as a report source
    sources = {s["key"]: s for s in company_a.get("/reports/sources").json()}
    assert "service_visitor_pass" in sources and sources["service_visitor_pass"]["category"] == "services"
    res = company_a.post("/reports/preview", json={"source": "service_visitor_pass", "config": {
        "columns": ["reference_no", "f_visitor_name", "f_purpose", "f_host", "f_group_size"]}})
    assert res.status_code == 200, res.text
    body = res.json()
    rows = body.get("rows") or body.get("items") or []
    assert len(rows) == 3


def test_tenant_isolation(company_a, company_b):
    svc_a = _install(company_a, "it_access_request")
    rec = company_a.post("/custom-forms/services/it_access_request/records", json={"values": {
        "accounts": [{"system": "VPN", "level": "Standard"}], "reason": "Remote work"}})
    assert rec.status_code == 201, rec.text
    rec = rec.json()
    assert rec["values"]["systems_count"] == "1"
    # company B sees nothing of A's
    assert company_b.get("/custom-forms/services/it_access_request").status_code == 404
    assert company_b.get(f"/custom-forms/records/{rec['uuid']}").status_code == 404
    assert company_b.get("/custom-forms/nav").json() == []
    assert svc_a["request_type"] not in {t["key"] for t in company_b.get("/workflow/request-types").json()}
    assert "service_it_access_request" not in {s["key"] for s in company_b.get("/reports/sources").json()}
    assert company_b.post("/reports/preview", json={"source": "service_it_access_request", "config": {}}).status_code in (400, 404)
    # B can add the same template on its own
    svc_b = _install(company_b, "it_access_request")
    assert svc_b["request_type"] != svc_a["request_type"]
    assert company_b.get("/custom-forms/services/it_access_request/records?scope=all").json()["total"] == 0
    # A's employee ids are not accepted on B's forms
    emp_a = company_a.add_employee("Asha")
    bad = company_b.post("/custom-forms/services/it_access_request/records", json={"values": {
        "access_for": emp_a["id"], "accounts": [{"system": "VPN", "level": "Standard"}], "reason": "x"}})
    assert bad.status_code == 400
    names = [o["label"] for o in company_b.get("/custom-forms/lookups/employee?q=Asha").json()]
    assert names == []
    # row-level security on every table of the module
    from core.db import tenant_session
    from modules.custom_forms.models import (CustomFormComment, CustomFormRecord, CustomFormVersion,
                                             CustomFormView, CustomService)
    db = tenant_session(company_b.id)
    try:
        for model in (CustomService, CustomFormVersion, CustomFormRecord, CustomFormComment, CustomFormView):
            assert db.query(model).filter(model.company_id == company_a.id).count() == 0
    finally:
        db.close()
