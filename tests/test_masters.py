"""Masters: seeded system lists, adding values and sub-values, search, isolation."""


def test_system_lists_are_seeded(company_a):
    types = {t["key"]: t for t in company_a.get("/masters/types").json()}
    for key in ("gender", "blood_group", "bank", "state", "document_type", "employment_type", "leave_unit"):
        assert key in types and types[key]["is_system"]
    states = company_a.get("/masters/types/state/values", params={"size": 200}).json()
    assert states["total"] == 36
    kerala = next(v for v in states["items"] if v["label"] == "Kerala")
    assert kerala["attributes"]["gst_code"] == "32"


def test_add_value_sub_value_and_search(company_a):
    r = company_a.post("/masters/types/asset_category/values", json={"label": "Power tools"})
    assert r.status_code == 201, r.text
    parent = r.json()
    child = company_a.post("/masters/types/asset_category/values", json={"label": "Drill", "parent_id": parent["id"]}).json()
    assert child["parent_id"] == parent["id"]
    opts = company_a.get("/masters/options/asset_category", params={"q": "dri"}).json()["items"]
    assert [o["label"] for o in opts] == ["Drill"] and opts[0]["parent_label"] == "Power tools"
    # duplicates refused, sub-values refused on flat lists
    assert company_a.post("/masters/types/asset_category/values", json={"label": "power tools"}).status_code == 400
    assert company_a.post("/masters/types/gender/values", json={"label": "X", "parent_id": parent["id"]}).status_code == 400


def test_switch_off_and_reorder(company_a):
    values = company_a.get("/masters/types/blood_group/values").json()["items"]
    first = values[0]
    assert company_a.put(f"/masters/values/{first['uuid']}", json={"active": False}).status_code == 200
    active = company_a.get("/masters/options/blood_group").json()["items"]
    assert first["id"] not in [o["value"] for o in active]
    ids = [v["id"] for v in reversed(values)]
    assert company_a.post("/masters/types/blood_group/values/reorder", json={"ids": ids}).status_code == 200
    again = company_a.get("/masters/types/blood_group/values").json()["items"]
    assert [v["id"] for v in again] == ids


def test_custom_list(company_a):
    r = company_a.post("/masters/types", json={"name": "Shirt size", "hierarchical": False})
    assert r.status_code == 201
    assert company_a.post("/masters/types/shirt_size/values", json={"label": "XL"}).status_code == 201
    assert company_a.post("/masters/types", json={"name": "Shirt size"}).status_code == 400


def test_lists_are_per_company(company_a, company_b):
    company_b.post("/masters/types/bank/values", json={"label": "Beta Co-op Bank"})
    found = company_a.get("/masters/options/bank", params={"q": "Beta Co-op"}).json()["items"]
    assert found == []
    b_value = company_b.get("/masters/options/bank", params={"q": "Beta Co-op"}).json()["items"][0]
    b_uuid = company_b.get("/masters/types/bank/values", params={"q": "Beta Co-op"}).json()["items"][0]["uuid"]
    assert company_a.put(f"/masters/values/{b_uuid}", json={"label": "Mine"}).status_code == 404
    assert b_value["label"] == "Beta Co-op Bank"
