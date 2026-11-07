"""Salary bank files: every ready-made layout's output, the custom format builder (separated and
fixed width, header and trailer with totals and checksum), checks on IFSC and account numbers,
debit accounts per legal entity, splitting, the record of every file, permissions and isolation."""
import csv
import io
from decimal import Decimal

import pytest

from modules.salary_bank import engine, formats, service
from modules.salary_bank.engine import Debit, Line, Payee
from tests.conftest import login_as
from tests.test_payroll_api import _approve_and_lock, _assign, _calculate, _emp, _open_run

ON = __import__("datetime").date(2026, 11, 30)
CTX = {"date": ON, "narration": "Salary Nov 2026", "period": "202611", "month": "Nov 2026", "batch": 1,
       "batch_ref": "SAL20261101", "file_date": ON}

P_OWN = Payee("Priya Sharma", "50100234567891", "HDFC0001234", Decimal("48250"), "EMP001", "priya@example.com", "9876543210", "HDFC Bank")
P_NEFT = Payee("Arjun Nair", "012345678901", "ICIC0000456", Decimal("61800.5"), "EMP002", "arjun@example.com", "9812345678", "ICICI Bank")
P_RTGS = Payee("Meena Iyer", "30214567891", "SBIN0007890", Decimal("235000"), "EMP003", "meena@example.com", "9900112233", "SBI")


def _render(key: str, payees, debit_ifsc: str, rules=None, **debit):
    spec = formats.BUILTIN[key]["spec"]
    d = Debit(debit.get("account", "000111222333"), debit_ifsc, "Sample Pvt Ltd", debit.get("client_code", "CL001"),
              debit.get("corporate_id", "CORP01"))
    lines = [Line(p, engine.mode_for(p, d.ifsc, spec, rules or {})) for p in payees]
    groups = {}
    for ln in lines:
        groups.setdefault(engine.group_of(ln.mode, spec), []).append(ln)
    return {g: engine.render(engine.for_group(spec, g), ls, d, CTX).content.decode() for g, ls in groups.items()}


def _rows(text: str, delim=","):
    return list(csv.reader(io.StringIO(text), delimiter=delim))


# ── Ready-made layouts ───────────────────────────────────────────────

def test_every_builtin_layout_renders_and_documents_its_source():
    assert {"generic_neft", "sbi_cinb", "hdfc_enet", "icici_cib", "axis_cib", "kotak_cms", "yes_connect", "idfc_first",
            "indusind", "bob", "pnb", "canara", "union", "federal", "sib"} <= set(formats.BUILTIN)
    for key, f in formats.BUILTIN.items():
        assert f["sources"] and f["confidence"] in ("high", "medium", "low"), key
        engine.validate_spec(f["spec"])
        out = _render(key, [P_OWN, P_NEFT, P_RTGS], "HDFC0000001")
        assert out and all(t.endswith("\r\n") for t in out.values()), key


def test_generic_csv():
    t = _render("generic_neft", [P_OWN, P_NEFT, P_RTGS], "HDFC0000001")["all"]
    rows = _rows(t)
    assert rows[0][:5] == ["Payment mode", "Beneficiary name", "Account number", "IFSC", "Amount"]
    assert [r[0] for r in rows[1:]] == ["IFT", "NEFT", "RTGS"]
    assert rows[2][4] == "61800.50" and rows[2][5] == "000111222333" and rows[2][6] == "30/11/2026"
    # IMPS when chosen, only up to 5 lakh
    rows = _rows(_render("generic_neft", [P_NEFT, P_RTGS], "HDFC0000001", {"other_bank_mode": "imps"})["all"])
    assert [r[0] for r in rows[1:]] == ["IMPS", "IMPS"]
    big = Payee("Big", "123456789012", "ICIC0000001", Decimal("600000"))
    assert _rows(_render("generic_neft", [big], "HDFC0000001", {"other_bank_mode": "imps"})["all"])[1][0] == "RTGS"
    # NEFT only when RTGS is not wanted
    assert _rows(_render("generic_neft", [P_RTGS], "HDFC0000001", {"other_bank_mode": "neft"})["all"])[1][0] == "NEFT"


def test_hdfc_enet():
    t = _render("hdfc_enet", [P_OWN, P_NEFT, P_RTGS], "HDFC0000001")["all"]
    lines = t.splitlines()
    assert len(lines) == 3                                   # no header, no trailer
    f = [ln.split(",") for ln in lines]
    assert all(len(x) == 28 for x in f)                      # 28 fields, no quoting
    assert [x[0] for x in f] == ["I", "N", "R"]
    assert f[1][2] == "012345678901" and f[1][3] == "61800.50" and f[1][4] == "Arjun Nair"
    assert f[1][22] == "30/11/2026" and f[1][24] == "ICIC0000456" and f[1][27] == "arjun@example.com"
    # commas in names never shift columns
    odd = Payee("Rao, K.", "50100234567892", "HDFC0001234", Decimal("10"))
    assert len(_render("hdfc_enet", [odd], "HDFC0000001")["all"].strip().split(",")) == 28


def test_icici_cib():
    rows = _rows(_render("icici_cib", [P_OWN, P_NEFT, P_RTGS], "ICIC0000001")["all"])
    assert rows[0][:7] == ["Debit Ac No", "Beneficiary Ac No", "Beneficiary Name", "Amt", "Pay Mod", "Date", "IFSC"]
    assert len(rows[0]) == 21 and all(len(r) == 21 for r in rows)
    assert [r[4] for r in rows[1:]] == ["N", "I", "R"]      # ICICI to ICICI is a fund transfer (I)
    assert rows[1][5] == "30-Nov-2026" and rows[1][0] == "000111222333"


def test_sbi_splits_own_bank_and_other_banks():
    out = _render("sbi_cinb", [P_OWN, P_NEFT, P_RTGS], "SBIN0000001")
    assert set(out) == {"internal", "other"}
    assert out["internal"].strip().split("|")[:3] == ["30214567891", "SBIN0007890", "235000.00"]
    other = [ln.split("|") for ln in out["other"].splitlines()]
    assert [x[6] for x in other] == ["NEFT", "NEFT"] and other[1][2] == "61800.50"


def test_axis_kotak_idfc_yes_rows():
    axis = _rows(_render("axis_cib", [P_OWN, P_NEFT], "UTIB0000001")["all"])
    assert [r[0] for r in axis[1:]] == ["N", "N"] and axis[1][3] == "30-11-2026" and axis[1][1] == "CL001"
    kotak = _rows(_render("kotak_cms", [P_OWN, P_NEFT], "KKBK0000001")["all"])
    assert kotak[0][0] == "Client_Code" and len(kotak[0]) == 19
    assert kotak[1][:3] == ["CL001", "SALARY", "NEFT"] and kotak[1][4] == "30/11/2026"
    own = Payee("Own", "1234567890", "KKBK0000123", Decimal("100"))
    assert _rows(_render("kotak_cms", [own], "KKBK0000001")["all"])[1][2] == "IFT"
    idfc = _rows(_render("idfc_first", [P_NEFT, Payee("Own", "10012345678", "IDFB0040101", Decimal("5"))], "IDFB0040101")["all"])
    assert len(idfc[0]) == 32 and [r[2] for r in idfc[1:]] == ["LBT", "BT"] and idfc[1][12] == "NEFT"
    assert idfc[1][3] == "30-11-2026"
    yes = _rows(_render("yes_connect", [P_RTGS], "YESB0000001")["all"])
    assert yes[1][2] == "RTGS"


def test_union_bank_neft_file_and_salary_file():
    out = _render("union", [P_NEFT, P_RTGS, Payee("Own One", "566802070000001", "UBIN0556688", Decimal("1000")),
                            Payee("Own Two", "566802070000002", "UBIN0556688", Decimal("2500.50"))], "UBIN0556688",
                  account="566802070009999")
    neft = out["other"].splitlines()
    assert neft[0] == "FILEHDR|CORP01|000001|N|Salary Nov 2026"
    f = neft[1].split("|")
    assert len(f) == 11 and f[:7] == ["NEFT", "UBIN0556688", "566802070009999", "ICIC0000456", "012345678901", "INR", "61800.50"]
    sal = _rows(out["internal"])
    assert sal[0] == ["FILEHDR", "CORP01", "000001"]
    assert sal[1][1:5] == ["566802070009999", "INR", "DR", "3500.50"]          # debit line = total of credits
    assert [r[3] for r in sal[2:]] == ["CR", "CR"] and sum(Decimal(r[4]) for r in sal[2:]) == Decimal(sal[1][4])


def test_public_sector_layouts_carry_count_and_total():
    for key, prefix in (("pnb", "PUNB"), ("bob", "BARB"), ("canara", "CNRB")):
        out = _render(key, [P_NEFT, P_RTGS], f"{prefix}0000001")
        head = out["other"].splitlines()[0].split("|")
        assert head[0] == "FILEHDR" and head[4] == "2" and head[5] == "296800.50", key


# ── Checks ───────────────────────────────────────────────────────────

def test_ifsc_and_account_checks():
    codes = lambda p: {x.code: x.level for x in engine.check_payee(p)}  # noqa: E731
    assert codes(P_OWN) == {}
    assert codes(Payee("A", "50100234567891", "HDFC1001234", Decimal("1")))["bad_ifsc"] == "error"   # 5th must be 0
    assert codes(Payee("A", "50100234567891", "HDF0001234", Decimal("1")))["bad_ifsc"] == "error"
    assert codes(Payee("A", "12-34", "HDFC0001234", Decimal("1")))["bad_account"] == "error"
    assert codes(Payee("A", "", "HDFC0001234", Decimal("1")))["no_account"] == "error"
    assert codes(Payee("A", "123456789", "HDFC0001234", Decimal("1")))["account_length"] == "warn"
    assert codes(Payee("", "50100234567891", "HDFC0001234", Decimal("1")))["no_name"] == "error"
    assert codes(Payee("A", "50100234567891", "HDFC0001234", Decimal("0")))["no_amount"] == "error"


# ── Custom formats ───────────────────────────────────────────────────

FIXED = {
    "kind": "fixed", "extension": "txt", "line_ending": "lf", "date_format": "%Y%m%d", "amount": {"paise": True},
    "header_lines": [[{"label": "Type", "value": "H", "width": 1}, {"label": "Debit", "value": "{debit_account}", "width": 16},
                      {"label": "Date", "value": "{date}", "width": 8}]],
    "columns": [{"label": "Type", "value": "D", "width": 1}, {"label": "Seq", "value": "{seq}", "width": 5, "align": "right", "pad": "0"},
                {"label": "Account", "value": "{account}", "width": 18}, {"label": "IFSC", "value": "{ifsc}", "width": 11},
                {"label": "Amount", "value": "{amount_paise}", "width": 13, "align": "right", "pad": "0"},
                {"label": "Name", "value": "{name}", "width": 20, "case": "upper"}],
    "trailer_lines": [[{"label": "Type", "value": "T", "width": 1},
                       {"label": "Count", "value": "{count}", "width": 6, "align": "right", "pad": "0"},
                       {"label": "Total", "value": "{total_paise}", "width": 15, "align": "right", "pad": "0"},
                       {"label": "Hash", "value": "{account_hash}", "width": 15, "align": "right", "pad": "0"}]],
}


def test_fixed_width_custom_format_with_trailer_checksum():
    spec = service.clean_spec(FIXED)
    d = Debit("000111222333", "HDFC0000001")
    long_name = Payee("Venkataraghavan Subramaniam", "30214567891", "SBIN0007890", Decimal("1234.5"))
    r = engine.render(spec, [Line(P_NEFT, "neft"), Line(long_name, "neft")], d, CTX)
    lines = r.content.decode().split("\n")[:-1]
    assert lines[0] == "H" + "000111222333".ljust(16) + "20261130"
    assert len(lines[1]) == 1 + 5 + 18 + 11 + 13 + 20 and lines[1][:6] == "D00001"
    assert lines[1][24:35] == "ICIC0000456" and lines[1][35:48] == "0000006180050"
    assert lines[2][48:] == "VENKATARAGHAVAN SUBR"                    # cut to the width, capitals
    assert lines[3] == "T000002" + "6303500".rjust(15, "0") + str(12345678901 + 30214567891).rjust(15, "0")
    # amounts never get cut silently
    huge = Payee("X", "123456789012", "ICIC0000001", Decimal("999999999999.99"))
    with pytest.raises(engine.SpecError):
        engine.render(spec, [Line(huge, "neft")], d, CTX)


def test_spec_checks():
    with pytest.raises(Exception):
        service.clean_spec({"kind": "fixed", "columns": [{"label": "A", "value": "{account}"}]})    # no width
    with pytest.raises(Exception):
        service.clean_spec({"kind": "delimited", "columns": [{"label": "A", "value": "{salary}"}]})  # unknown token
    with pytest.raises(Exception):
        service.clean_spec({"kind": "delimited", "columns": [{"label": "A", "value": "{count}"}]})   # total token on a row
    s = service.clean_spec({"kind": "delimited", "delimiter": "tab", "columns": [{"label": "A", "value": "{account}"}]})
    assert s["extension"] == "txt" and engine.delimiter(s) == "\t"


# ── Through the API ──────────────────────────────────────────────────

def _locked_run(company, emps_ctc, month):
    for e, ctc in emps_ctc:
        _assign(company, e, ctc)
    run = _open_run(company, month, [e for e, _ in emps_ctc])
    run = _calculate(company, run)
    return _approve_and_lock(company, run)


def test_formats_builder_and_preview_api(company_a):
    r = company_a.get("/payroll/bank/formats")
    items = {f["key"]: f for f in r.json()["items"]}
    assert len([k for k, f in items.items() if f["builtin"]]) == len(formats.BUILTIN)
    pv = company_a.post("/payroll/bank/formats/preview", json={"spec": FIXED})
    assert pv.status_code == 200 and pv.json()["files"][0]["lines"][0].startswith("H")
    r = company_a.post("/payroll/bank/formats", json={"name": "Our co-op bank", "spec": {**FIXED, "bank": "Co-op"}})
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    assert key in {f["key"] for f in company_a.get("/payroll/bank/formats").json()["items"]}
    bad = company_a.post("/payroll/bank/formats", json={"name": "Bad", "spec": {"kind": "fixed", "columns": [{"value": "{x}"}]}})
    assert bad.status_code == 400
    # an employee without payroll settings rights cannot add one
    emp = company_a.add_employee("Nora")
    assert login_as(company_a, emp).post("/payroll/bank/formats", json={"name": "X", "spec": FIXED}).status_code == 403


def test_run_files_by_entity_validation_and_audit(company_a, company_b):
    # two legal entities, each with its own debit account and bank format
    e1 = company_a.post("/organization/legal-entities", json={"name": "Alpha Tech"}).json()
    e2 = company_a.post("/organization/legal-entities", json={"name": "Alpha Retail"}).json()
    r = company_a.post("/payroll/bank/accounts", json={"legal_entity_uuid": e1["uuid"], "account_number": "50200012345678",
                                                        "ifsc": "HDFC0000523", "format_key": "hdfc_enet", "is_default": True})
    assert r.status_code == 201, r.text
    assert r.json()["account_number"].endswith("5678") and "•" in r.json()["account_number"]
    r = company_a.post("/payroll/bank/accounts", json={"legal_entity_uuid": e2["uuid"], "account_number": "566802070009999",
                                                        "ifsc": "UBIN0556688", "format_key": "union", "is_default": True})
    assert r.status_code == 400 and "corporate id" in r.json()["detail"]          # Union needs the corporate id
    r = company_a.post("/payroll/bank/accounts", json={"legal_entity_uuid": e2["uuid"], "account_number": "566802070009999",
                                                        "ifsc": "UBIN0556688", "format_key": "union", "corporate_id": "ALPHARTL"})
    assert r.status_code == 201, r.text
    assert company_a.post("/payroll/bank/accounts", json={"account_number": "123", "ifsc": "HDFC0000523"}).status_code == 400
    assert company_a.post("/payroll/bank/accounts", json={"account_number": "123456789", "ifsc": "HDFC000052"}).status_code == 400

    a = _emp(company_a, "Anil")          # SBI account (from the test helper)
    b = _emp(company_a, "Bina")
    c = _emp(company_a, "Chetan")
    for e, ent in ((a, e1), (b, e1), (c, e2)):
        assert company_a.put(f"/employees/{e['uuid']}", json={"legal_entity_id": ent["id"]}).status_code == 200
    run = _locked_run(company_a, [(a, 600000), (b, 600000), (c, 600000)], "2026-07-01")

    chk = company_a.get(f"/payroll/bank/runs/{run['uuid']}/check").json()
    assert chk["blocking"] == [] and chk["errors"] == 0
    files = {(f["format_key"], f["group"]): f for f in chk["files"]}
    assert files[("hdfc_enet", "all")]["count"] == 2
    assert files[("union", "other")]["count"] == 1                          # SBI account paid from Union Bank: NEFT file

    gen = company_a.post(f"/payroll/bank/runs/{run['uuid']}/files", json={})
    assert gen.status_code == 201, gen.text
    made = gen.json()["items"]
    assert len(made) == 2 and all(len(m["sha256"]) == 64 for m in made)
    hdfc = next(m for m in made if m["format_key"] == "hdfc_enet")
    body = company_a.get(f"/payroll/bank/files/{hdfc['uuid']}/download")
    assert body.status_code == 200 and len(body.text.strip().splitlines()) == 2
    assert all(ln.split(",")[0] == "N" for ln in body.text.strip().splitlines())
    union = next(m for m in made if m["format_key"] == "union")
    assert company_a.get(f"/payroll/bank/files/{union['uuid']}/download").text.startswith("FILEHDR|ALPHARTL|")
    hist = company_a.get(f"/payroll/bank/runs/{run['uuid']}/files").json()["items"]
    assert next(h for h in hist if h["uuid"] == hdfc["uuid"])["download_count"] == 1
    from core.db import system_session
    from core.audit import AuditLog
    db = system_session()
    try:
        acts = {a for (a,) in db.query(AuditLog.action).filter(AuditLog.company_id == company_a.id,
                                                               AuditLog.entity_type == "bank_file_batches").all()}
    finally:
        db.close()
    assert {"payroll.bank_file_generated", "export.downloaded"} <= acts
    # making them again keeps the old ones, marked superseded
    company_a.post(f"/payroll/bank/runs/{run['uuid']}/files", json={"format": "generic_neft"})
    hist = company_a.get(f"/payroll/bank/runs/{run['uuid']}/files").json()["items"]
    assert len(hist) == 4 and sum(1 for h in hist if h["status"] == "superseded") == 2
    # the old download address still works (zip when several files)
    old = company_a.get(f"/payroll/runs/{run['uuid']}/files/bank")
    assert old.status_code == 200 and old.headers["content-type"] == "application/zip"

    # another company sees none of it
    assert company_b.get(f"/payroll/bank/files/{hdfc['uuid']}/download").status_code == 404
    assert all(x["uuid"] != hdfc["uuid"] for x in company_b.get("/payroll/bank/files").json()["items"])
    assert company_b.get("/payroll/bank/accounts").json()["total"] == 0
    # an employee cannot make or download bank files
    me = login_as(company_a, a)
    assert me.get(f"/payroll/bank/files/{hdfc['uuid']}/download").status_code == 403
    assert me.post(f"/payroll/bank/runs/{run['uuid']}/files", json={}).status_code == 403


def test_bad_bank_details_block_until_left_out(company_a):
    good = _emp(company_a, "Gita")
    bad = _emp(company_a, "Hari")
    run = _locked_run(company_a, [(good, 600000), (bad, 600000)], "2026-08-01")
    from core.db import system_session
    from modules.employees.models import BankAccount
    db = system_session()
    try:
        db.query(BankAccount).filter(BankAccount.account_number == bad["account_number"]).update({"ifsc": "SBIN1001234"})
        db.commit()
    finally:
        db.close()
    chk = company_a.get(f"/payroll/bank/runs/{run['uuid']}/check").json()
    assert chk["errors"] == 1 and any(x["code"] == "bad_ifsc" and x["name"].startswith("Hari") for x in chk["problems"])
    r = company_a.post(f"/payroll/bank/runs/{run['uuid']}/files", json={})
    assert r.status_code == 400 and "leave them out" in r.json()["detail"]
    r = company_a.post(f"/payroll/bank/runs/{run['uuid']}/files", json={"leave_out_problems": True})
    assert r.status_code == 201, r.text
    item = r.json()["items"][0]
    assert item["count"] == 1 and item["left_out"][0]["name"].startswith("Hari")


def test_bank_file_needs_an_approved_run(company_a):
    e = _emp(company_a, "Isha")
    _assign(company_a, e, 600000)
    run = _open_run(company_a, "2026-09-01", [e])
    assert company_a.get(f"/payroll/bank/runs/{run['uuid']}/check").status_code == 400
