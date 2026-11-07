"""Recycle bin: listing soft-deleted records of every kind, restore with dependency and
duplicate checks, purge only where allowed and after the retention period, audit of
every restore and purge, permissions and tenant isolation."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from core.db import system_session, tenant_session
from tests.conftest import login_as


def _dept(company, name, **kw):
    r = company.post("/departments/", json={"department_name": name, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _bin(company, **params):
    r = company.get("/recycle-bin/items", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _folder(company_id, name, days_ago=0, parent_id=None, archived=True):
    from modules.documents.models import DocFolder
    db = tenant_session(company_id)
    try:
        f = DocFolder(company_id=company_id, area="company", name=name, parent_id=parent_id,
                      archived_at=datetime.now(timezone.utc) - timedelta(days=days_ago) if archived else None)
        db.add(f)
        db.commit()
        return f.id, str(f.uuid)
    finally:
        db.close()


def _events(company_id, action):
    db = system_session()
    try:
        return db.execute(text("SELECT entity_type, entity_label, details FROM audit_logs WHERE company_id = :c AND action = :a"),
                          {"c": company_id, "a": action}).fetchall()
    finally:
        db.close()


def test_deleted_department_is_listed_with_who_and_when_and_restored(company_a):
    d = _dept(company_a, "Field sales", department_code="FS")
    assert company_a.delete(f"/departments/{d['uuid']}").status_code == 200
    data = _bin(company_a, kind="department")
    item = next(i for i in data["items"] if i["uuid"] == d["uuid"])
    assert item["title"] == "Field sales" and item["deleted_by"] and item["deleted_at"]
    assert item["purgeable"] and not item["can_purge_now"] and item["purge_after"]
    assert any(k["kind"] == "department" for k in data["facets"]["kinds"])
    detail = company_a.get(f"/recycle-bin/items/department/{d['uuid']}").json()
    assert detail["problems"] == [] and any(f["field"] == "department_name" for f in detail["fields"])
    r = company_a.post(f"/recycle-bin/items/department/{d['uuid']}/restore")
    assert r.status_code == 200, r.text
    assert all(i["uuid"] != d["uuid"] for i in _bin(company_a)["items"])
    assert company_a.get(f"/departments/{d['uuid']}").json()["active"] is True
    assert any(e[1] == "Field sales" for e in _events(company_a.id, "recycle_bin.restored"))


def test_restore_needs_the_parent_back_first(company_a):
    parent = _dept(company_a, "Operations")
    child = _dept(company_a, "Warehouse", parent_id=parent["id"])
    company_a.delete(f"/departments/{child['uuid']}")
    company_a.delete(f"/departments/{parent['uuid']}")
    r = company_a.post(f"/recycle-bin/items/department/{child['uuid']}/restore")
    assert r.status_code == 400 and "Operations" in r.json()["detail"]
    # restoring several at once reports the one that cannot come back
    r = company_a.post("/recycle-bin/restore", json={"items": [{"kind": "department", "uuid": child["uuid"]},
                                                               {"kind": "department", "uuid": parent["uuid"]}]})
    body = r.json()
    assert len(body["restored"]) == 1 and len(body["failed"]) == 1
    assert company_a.post(f"/recycle-bin/items/department/{child['uuid']}/restore").status_code == 200


def test_restore_refused_when_a_live_record_uses_the_same_code(company_a):
    old = _dept(company_a, "Accounts old", department_code="ACC")
    company_a.delete(f"/departments/{old['uuid']}")
    _dept(company_a, "Accounts", department_code="ACC")
    r = company_a.post(f"/recycle-bin/items/department/{old['uuid']}/restore")
    assert r.status_code == 400 and "ACC" in r.json()["detail"]


def test_purge_only_after_retention_and_audited(company_a):
    _, fresh = _folder(company_a.id, "Old drafts", days_ago=5)
    _, due = _folder(company_a.id, "Old scans", days_ago=120)
    r = company_a.post(f"/recycle-bin/items/doc_folder/{fresh}/purge")
    assert r.status_code == 400 and "90 days" in r.json()["detail"]
    items = {i["uuid"]: i for i in _bin(company_a, kind="doc_folder")["items"]}
    assert items[due]["can_purge_now"] and not items[fresh]["can_purge_now"]
    assert [i["uuid"] for i in _bin(company_a, purge="due")["items"]] == [due]
    r = company_a.post(f"/recycle-bin/items/doc_folder/{due}/purge")
    assert r.status_code == 200, r.text
    assert company_a.get(f"/recycle-bin/items/doc_folder/{due}").status_code == 404
    ev = _events(company_a.id, "recycle_bin.purged")
    assert ev and ev[0][1] == "Old scans" and ev[0][2]["record"]["name"] == "Old scans"
    # a longer retention period moves the date out; a shorter one than 30 days is refused
    assert company_a.put("/recycle-bin/settings", json={"retention_days": 10}).status_code == 400
    assert company_a.put("/recycle-bin/settings", json={"retention_days": 365}).json()["retention_days"] == 365


def test_purge_refused_while_other_records_point_to_it(company_a):
    pid, puuid = _folder(company_a.id, "Parent folder", days_ago=200)
    _folder(company_a.id, "Child folder", days_ago=200, parent_id=pid)
    r = company_a.post(f"/recycle-bin/items/doc_folder/{puuid}/purge")
    assert r.status_code == 400 and "still use" in r.json()["detail"]


def test_audit_relevant_records_are_never_purged(company_a):
    from modules.leave.models import LeaveType
    db = tenant_session(company_a.id)
    try:
        lt = db.query(LeaveType).filter(LeaveType.company_id == company_a.id).first()
        lt.active = False
        lt.archived_at = datetime.now(timezone.utc) - timedelta(days=400)
        db.commit()
        uuid, name = str(lt.uuid), lt.name
    finally:
        db.close()
    item = next(i for i in _bin(company_a, kind="leave_type")["items"] if i["uuid"] == uuid)
    assert not item["purgeable"] and item["keep_reason"] and not item["can_purge_now"]
    r = company_a.post(f"/recycle-bin/items/leave_type/{uuid}/purge")
    assert r.status_code == 400 and "cannot be removed" in r.json()["detail"]
    assert company_a.post(f"/recycle-bin/items/leave_type/{uuid}/restore").status_code == 200, name


def test_every_soft_deletable_model_is_known_to_the_bin():
    from core import recycle_bin as rb
    from core.db import Base
    from modules.recycle_bin import catalogue, service
    service.ensure_registry()
    known = set(rb.by_table()) | set(catalogue.EXCLUDED)
    missing = [m.local_table.name for m in Base.registry.mappers
               if {"archived_at", "deleted_at", "active"} & {c.name for c in m.local_table.c}
               and {"company_id", "uuid"} <= {c.name for c in m.local_table.c} and m.local_table.name not in known]
    assert missing == []
    for key in ("department", "master_value", "custom_field", "doc_file", "letter_template", "leave_type",
                "salary_component", "asset", "announcement", "employee"):
        assert rb.get(key), key
    for key in ("salary_component", "leave_type", "attendance_rule", "employee", "expense_category"):
        assert not rb.get(key).purgeable, key


def test_bin_permissions_and_tenant_isolation(company_a, company_b):
    d = _dept(company_a, "Secret projects")
    company_a.delete(f"/departments/{d['uuid']}")
    emp = login_as(company_a, company_a.add_employee("Ira", "Bose"))
    assert emp.get("/recycle-bin/items").status_code == 403
    assert emp.post(f"/recycle-bin/items/department/{d['uuid']}/restore").status_code == 403
    assert all(i["uuid"] != d["uuid"] for i in _bin(company_b)["items"])
    assert company_b.get(f"/recycle-bin/items/department/{d['uuid']}").status_code == 404
    assert company_b.post(f"/recycle-bin/items/department/{d['uuid']}/restore").status_code == 404
    assert company_b.post(f"/recycle-bin/items/department/{d['uuid']}/purge").status_code == 404
    assert any(i["uuid"] == d["uuid"] for i in _bin(company_a)["items"])
