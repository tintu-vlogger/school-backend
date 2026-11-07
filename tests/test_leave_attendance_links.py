"""Leave and attendance, second pass: absence to leave (and back), late-mark penalties from
leave, comp off from attendance, LOP cap and carry per pay period with the lock, half-pay
leave in the payroll export, Bradford score, expiry reminders, substitute holidays, the
clock-in kiosk, auto shift, statutory registers, SFTP pulling and timesheet billing."""
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from core.db import system_session, tenant_session
from tests.conftest import login_as
from tests.test_attendance import approve_all, day_row, enrol, hire, last_week_monday, make_device, push, today_ist


# ── helpers ─────────────────────────────────────────────────────────

def types(company):
    return {t["code"]: t for t in company.get("/leave/types").json()}


def adjust(company, emp, code, days, year=None):
    year = year or last_week_monday().year
    r = company.post("/leave/balances/adjust", json={"employee_uuid": emp["uuid"], "leave_type_uuid": types(company)[code]["uuid"],
                                                     "days": days, "reason": "test", "year": year})
    assert r.status_code == 201, r.text


def ledger_sum(company_id, emp_id, code, kind=None):
    from sqlalchemy import func
    from modules.leave.models import LeaveLedgerEntry, LeaveType
    db = system_session()
    try:
        q = (db.query(func.coalesce(func.sum(LeaveLedgerEntry.days), 0))
             .join(LeaveType, LeaveType.id == LeaveLedgerEntry.leave_type_id)
             .filter(LeaveLedgerEntry.employee_id == emp_id, LeaveType.code == code,
                     LeaveLedgerEntry.company_id == company_id))
        if kind:
            q = q.filter(LeaveLedgerEntry.kind == kind)
        return Decimal(str(q.scalar()))
    finally:
        db.close()


def setting(company, key, config):
    r = company.put(f"/attendance/settings/{key}", json={"config": config, "effective_from": "2020-01-01"})
    assert r.status_code == 200, r.text


def recompute(company, emp, start, end=None):
    r = company.post("/attendance/recompute", json={"start": start.isoformat(), "end": (end or start).isoformat(),
                                                    "employee_ids": [emp["id"]]})
    assert r.status_code == 200, r.text


def working_monday() -> date:
    """A Monday at least a week back whose week stays in one month (penalties are monthly)."""
    d = last_week_monday()
    while (d + timedelta(days=4)).month != d.month:
        d -= timedelta(days=7)
    return d


def links(company, kind=None):
    return company.get("/attendance/leave-links", params={"start": (today_ist() - timedelta(days=120)).isoformat(), "end": today_ist().isoformat(),
                                                           **({"kind": kind} if kind else {})}).json()["items"]


# ── pure rules ──────────────────────────────────────────────────────

def test_half_pay_leave_in_the_daily_engine():
    from zoneinfo import ZoneInfo
    from modules.attendance import engine
    tz = ZoneInfo("Asia/Kolkata")
    base = dict(date=date(2026, 3, 2), tz=tz, rule={}, shift=engine.ShiftSpec(None, "G", engine.parse_hhmm("09:30"),
                                                                               engine.parse_hhmm("18:30")))
    r = engine.compute_day(engine.DayInput(**base, leave={"unit": "full", "paid": True, "name": "SL", "pay_percent": 50}))
    assert r.status == "on_leave" and r.payable_days == Decimal("0.50") and r.lop_days == Decimal("0.50")
    assert r.pay_cut_days == Decimal("0.50") and "SL (50 % pay)" in r.notes[0]
    full = engine.compute_day(engine.DayInput(**base, leave={"unit": "full", "paid": True, "name": "EL"}))
    assert full.payable_days == 1 and full.pay_cut_days == 0
    unpaid = engine.compute_day(engine.DayInput(**base, leave={"unit": "full", "paid": False, "name": "LOP"}))
    assert unpaid.payable_days == 0 and unpaid.lop_days == 1 and unpaid.pay_cut_days == 0


def test_lop_cap_and_carry_rules():
    from modules.attendance.lop import compute
    r = compute({"cap_days": 3, "carry_excess": True}, Decimal("5"), Decimal("1"), Decimal("-0.5"))
    assert r["total"] == Decimal("5.50") and r["payable"] == Decimal("3.00") and r["carried_out"] == Decimal("2.50")
    r = compute({"cap_days": 3, "carry_excess": False}, Decimal("5"), Decimal("0"), Decimal("0"))
    assert r["payable"] == 3 and r["carried_out"] == 0 and r["waived"] == 2
    r = compute({"cap_days": None}, Decimal("2"), Decimal("0"), Decimal("-5"))
    assert r["total"] == 0 and r["payable"] == 0


def test_bradford_score_spells_and_levels():
    from modules.leave import bradford
    # Mon, Tue, then Fri and the next Mon (weekend between) = 2 spells, 4 days
    days = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 6), date(2026, 3, 9)]
    sp = bradford.spells_of(days, lambda d: d.weekday() < 5)
    assert [len(s) for s in sp] == [2, 2]
    assert bradford.score(len(sp), 4) == 16
    levels = [{"score": 50, "label": "Watch"}, {"score": 125, "label": "Talk"}]
    assert bradford.level_for(16, levels) == (0, None)
    assert bradford.level_for(125, levels) == (2, "Talk")
    # ten single days = 10 spells x 10 days = 1000
    assert bradford.score(10, 10) == 1000


def test_closest_shift_within_window():
    from zoneinfo import ZoneInfo
    from types import SimpleNamespace as S
    from datetime import time
    from modules.shifts.service import closest_shift
    tz = ZoneInfo("Asia/Kolkata")
    shifts = [S(id=1, start_time=time(6, 0)), S(id=2, start_time=time(14, 0)), S(id=3, start_time=time(22, 0))]
    d = date(2026, 3, 2)
    at = lambda h, m: datetime(2026, 3, 2, h, m, tzinfo=tz)  # noqa: E731
    assert closest_shift(at(5, 50), d, shifts, 120, tz).id == 1
    assert closest_shift(at(13, 20), d, shifts, 120, tz).id == 2
    assert closest_shift(at(21, 45), d, shifts, 120, tz).id == 3
    assert closest_shift(at(10, 0), d, shifts, 120, tz) is None


def test_gst_split_and_amount_in_words():
    from modules.timesheets.billing import amount_in_words, tax_split
    intra = tax_split(Decimal("10000.00"), Decimal("18"), "29", "29")
    assert intra["tax_mode"] == "intra" and intra["cgst"] == Decimal("900.00") and intra["sgst"] == Decimal("900.00")
    inter = tax_split(Decimal("10000.00"), Decimal("18"), "29", "27")
    assert inter["tax_mode"] == "inter" and inter["igst"] == Decimal("1800.00") and inter["cgst"] == 0
    odd = tax_split(Decimal("0.05"), Decimal("18"), "29", "29")
    assert odd["cgst"] + odd["sgst"] == Decimal("0.01")
    assert amount_in_words(Decimal("1234567.50")) == "Rupees twelve lakh thirty four thousand five hundred sixty seven and fifty paise only"


def test_sftp_pull_with_a_mocked_connection(monkeypatch):
    import paramiko
    from modules.attendance import devices

    files = {"/exports/a.csv": b"UserID,DateTime\n7,2026-03-02 09:30:00\n", "/exports/skip.txt": b"x"}
    removed, closed = [], []

    class FakeKey:
        def asbytes(self):
            return b"server-key"

    class FakeTransport:
        def __init__(self, addr):
            assert addr == ("sftp.example.com", 2222)
            self.banner_timeout = None

        def connect(self, username=None, password=None, pkey=None):
            assert username == "bio" and password == "pw"

        def get_remote_server_key(self):
            return FakeKey()

        def close(self):
            closed.append("transport")

    class FakeSFTP:
        def listdir(self, path):
            assert path == "/exports"
            return ["skip.txt", "a.csv"]

        def getfo(self, full, buf):
            buf.write(files[full])

        def remove(self, full):
            removed.append(full)

        def close(self):
            closed.append("sftp")

    monkeypatch.setattr(paramiko, "Transport", FakeTransport)
    monkeypatch.setattr(paramiko.SFTPClient, "from_transport", classmethod(lambda cls, t: FakeSFTP()))
    cfg = {"protocol": "sftp", "host": "sftp.example.com", "port": 2222, "username": "bio", "password": "pw",
           "path": "/exports", "file_pattern": "*.csv",
           "host_key": devices.host_key_fingerprint(FakeKey())}
    got = list(devices._list_and_fetch(cfg))
    assert [(n, c) for n, c, _ in got] == [("a.csv", files["/exports/a.csv"])]
    got[0][2]()
    assert removed == ["/exports/a.csv"] and closed == ["sftp", "transport"]
    # a server with another key is refused
    with pytest.raises(RuntimeError, match="not the one saved"):
        list(devices._list_and_fetch({**cfg, "host_key": "SHA256:somethingelse"}))


def test_sftp_device_pull_inserts_punches(client, company_a, monkeypatch):
    import paramiko
    from modules.attendance import devices
    from modules.attendance.models import Device
    emp = hire(company_a, "Sefa")
    d = working_monday()
    body = f"UserID,DateTime\nS1,{d} 09:31:00\nS1,{d} 18:40:00\n".encode()

    class T:
        def __init__(self, addr):
            pass

        def connect(self, **kw):
            pass

        def close(self):
            pass

    class C:
        def listdir(self, path):
            return ["punches.csv"]

        def getfo(self, full, buf):
            buf.write(body)

        def remove(self, full):
            pass

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "Transport", T)
    monkeypatch.setattr(paramiko.SFTPClient, "from_transport", classmethod(lambda cls, t: C()))
    r = company_a.post("/attendance/devices", json={
        "name": "Back gate", "vendor": "eSSL", "kind": "biometric", "connection": "ftp",
        "ftp_config": {"protocol": "sftp", "host": "sftp.example.com", "username": "u", "password": "p",
                       "path": "/out", "file_pattern": "*.csv", "every_minutes": 30},
        "column_mapping": {"employee": "UserID", "timestamp": "DateTime", "match_by": "device_user_id"}})
    assert r.status_code == 201, r.text
    enrol(company_a, emp, "S1")
    db = tenant_session(company_a.id)
    try:
        dev = db.query(Device).filter(Device.uuid == r.json()["uuid"]).first()
        logs = devices.poll_device(db, dev)
        db.commit()
        assert logs and logs[0].inserted == 2, [(l.status, l.message) for l in logs]
    finally:
        db.close()
    assert day_row(company_a, emp, d)["status"] == "present"
    assert company_a.get(f"/attendance/devices/{r.json()['uuid']}").json()["ftp_config"]["password"] == "••••••"


# ── absence to leave ────────────────────────────────────────────────

def test_absence_becomes_leave_and_comes_back_when_corrected(client, company_a):
    setting(company_a, "leave_link", {"absence": {"enabled": True, "after_days": 0, "action": "leave",
                                                  "leave_type_codes": ["CL"], "needs_approval": False}})
    emp = hire(company_a, "Abdul")
    adjust(company_a, emp, "CL", 2)
    d = working_monday()
    recompute(company_a, emp, d)
    row = day_row(company_a, emp, d)
    assert row["status"] == "on_leave" and row["payable_days"] == "1.00", row
    assert ledger_sum(company_a.id, emp["id"], "CL") == Decimal("1")
    mine = [l for l in links(company_a, "absence") if l["employee_id"] == emp["id"]]
    assert mine and mine[0]["status"] == "applied" and mine[0]["leave_type"] == "Casual leave"
    # the device sends the punches later: the leave is cancelled and the balance restored
    dev = make_device(company_a)
    enrol(company_a, emp, "AB1")
    push(client, dev["api_key"], [{"device_user_id": "AB1", "timestamp": f"{d}T09:28:00"},
                                  {"device_user_id": "AB1", "timestamp": f"{d}T18:35:00"}])
    row = day_row(company_a, emp, d)
    assert row["status"] == "present", row
    assert ledger_sum(company_a.id, emp["id"], "CL") == Decimal("2")
    mine = [l for l in links(company_a, "absence") if l["employee_id"] == emp["id"]]
    assert mine[0]["status"] == "reversed"


def test_absence_with_approval_and_lop_fallback(client, company_a):
    setting(company_a, "leave_link", {"absence": {"enabled": True, "after_days": 0, "action": "leave",
                                                  "leave_type_codes": ["CL"], "needs_approval": True}})
    emp = hire(company_a, "Bhavna")
    adjust(company_a, emp, "CL", 1)
    d = working_monday() + timedelta(days=1)
    recompute(company_a, emp, d)
    assert day_row(company_a, emp, d)["status"] == "absent"  # waiting for the manager (here the owner)
    approve_all(company_a)
    row = day_row(company_a, emp, d)
    assert row["status"] == "on_leave" and row["lop_days"] == "0.00"
    assert ledger_sum(company_a.id, emp["id"], "CL") == 0
    # no balance left: the next absence becomes loss of pay (an LOP leave entry)
    setting(company_a, "leave_link", {"absence": {"enabled": True, "after_days": 0, "leave_type_codes": ["CL"],
                                                  "needs_approval": False}})
    d2 = d + timedelta(days=1)
    recompute(company_a, emp, d2)
    row = day_row(company_a, emp, d2)
    assert row["status"] == "on_leave" and row["lop_days"] == "1.00" and row["payable_days"] == "0.00"
    mine = {l["date"]: l for l in links(company_a, "absence") if l["employee_id"] == emp["id"]}
    assert mine[d2.isoformat()]["status"] == "lop"


def test_absence_waits_the_set_number_of_days(client, company_a):
    setting(company_a, "leave_link", {"absence": {"enabled": True, "after_days": 30, "leave_type_codes": ["CL"]}})
    emp = hire(company_a, "Chetan")
    adjust(company_a, emp, "CL", 3)
    d = working_monday()
    recompute(company_a, emp, d)
    assert day_row(company_a, emp, d)["status"] == "absent"  # still time to regularise
    assert ledger_sum(company_a.id, emp["id"], "CL") == 3


# ── late-mark penalties from leave ──────────────────────────────────

def test_late_penalty_taken_from_leave_and_given_back(client, company_a):
    setting(company_a, "leave_link", {"late_penalty": {"enabled": True, "leave_type_codes": ["CL", "EL"]}})
    emp = hire(company_a, "Deepa")
    adjust(company_a, emp, "CL", 2)
    dev = make_device(company_a)
    enrol(company_a, emp, "LP1")
    monday = working_monday()
    punches = []
    for i in range(3):
        d = monday + timedelta(days=i)
        punches += [{"device_user_id": "LP1", "timestamp": f"{d}T10:05:00"},
                    {"device_user_id": "LP1", "timestamp": f"{d}T19:30:00"}]
    push(client, dev["api_key"], punches)
    third = day_row(company_a, emp, monday + timedelta(days=2))
    assert third["is_late"] and third["penalty_lop_days"] == "0.00"
    assert any("taken from Casual leave" in n for n in third["notes"]), third["notes"]
    assert ledger_sum(company_a.id, emp["id"], "CL") == Decimal("1.5")
    # HR corrects the first day: only two late marks are left, so the penalty is given back
    first = day_row(company_a, emp, monday)
    r = company_a.post(f"/attendance/records/{first['uuid']}/override", json={"status": "present", "note": "Bus strike"})
    assert r.status_code == 200, r.text
    assert ledger_sum(company_a.id, emp["id"], "CL") == Decimal("2")
    assert [l for l in links(company_a, "late_penalty") if l["employee_id"] == emp["id"]][0]["status"] == "reversed"


def test_late_penalty_without_balance_stays_loss_of_pay(client, company_a):
    setting(company_a, "leave_link", {"late_penalty": {"enabled": True, "leave_type_codes": ["CL"]}})
    emp = hire(company_a, "Esha")
    dev = make_device(company_a)
    enrol(company_a, emp, "LP2")
    monday = working_monday()
    push(client, dev["api_key"], [p for i in range(3) for p in (
        {"device_user_id": "LP2", "timestamp": f"{monday + timedelta(days=i)}T10:10:00"},
        {"device_user_id": "LP2", "timestamp": f"{monday + timedelta(days=i)}T19:40:00"})])
    assert day_row(company_a, emp, monday + timedelta(days=2))["penalty_lop_days"] == "0.50"


# ── comp off from attendance ────────────────────────────────────────

def test_comp_off_from_work_on_a_weekly_off_and_taken_back(client, company_a):
    setting(company_a, "leave_link", {"comp_off": {"enabled": True, "full_day_hours": 6, "half_day_hours": 4}})
    emp = hire(company_a, "Farhan")
    dev = make_device(company_a)
    enrol(company_a, emp, "CO1")
    sunday = working_monday() - timedelta(days=1)
    saturday = sunday - timedelta(days=1)
    push(client, dev["api_key"], [{"device_user_id": "CO1", "timestamp": f"{sunday}T09:00:00"},
                                  {"device_user_id": "CO1", "timestamp": f"{sunday}T17:00:00"},
                                  {"device_user_id": "CO1", "timestamp": f"{saturday}T10:00:00"},
                                  {"device_user_id": "CO1", "timestamp": f"{saturday}T12:00:00"}])
    assert day_row(company_a, emp, sunday)["status"] == "weekly_off"
    assert ledger_sum(company_a.id, emp["id"], "CO") == Decimal("1")  # 8 h on Sunday; 2 h on Saturday is too little
    # the Sunday check-out was a mistake: the comp off goes again
    row = day_row(company_a, emp, sunday)
    detail = company_a.get(f"/attendance/records/{row['uuid']}").json()
    last = sorted(detail["punches"], key=lambda p: p["punched_at"])[-1]
    r = company_a.post(f"/attendance/punches/{last['uuid']}/ignore", json={"ignored": True, "note": "Wrong person"})
    assert r.status_code == 200, r.text
    assert ledger_sum(company_a.id, emp["id"], "CO") == Decimal("0")
    assert [l for l in links(company_a, "comp_off") if l["employee_id"] == emp["id"]][0]["status"] == "reversed"


# ── loss of pay per period ──────────────────────────────────────────

def test_lop_cap_carry_manual_and_lock(client, company_a):
    setting(company_a, "lop_control", {"cap_days": 2, "carry_excess": True})
    emp = hire(company_a, "Gita")
    d = working_monday()
    start, end = date(d.year, d.month, 1), (date(d.year + d.month // 12, d.month % 12 + 1, 1) - timedelta(days=1))
    recompute(company_a, emp, d, d + timedelta(days=2))  # three absent days
    rep = company_a.get("/attendance/lop", params={"start": start.isoformat(), "end": end.isoformat()}).json()
    mine = next(r for r in rep["rows"] if r["employee_id"] == emp["id"])
    absent = Decimal(mine["attendance_lop"])
    assert absent >= 3 and mine["payable"] == "2.00" and Decimal(mine["carried_out"]) == absent - 2
    r = company_a.post("/attendance/lop/adjustments", json={"employee_uuid": emp["uuid"], "start": start.isoformat(),
                                                            "end": end.isoformat(), "days": -1, "note": "Waived by HR"})
    assert r.status_code == 201, r.text
    mine = next(x for x in company_a.get("/attendance/lop", params={"start": start.isoformat(), "end": end.isoformat()})
                .json()["rows"] if x["employee_id"] == emp["id"])
    assert Decimal(mine["manual"]) == -1 and Decimal(mine["carried_out"]) == absent - 3
    pay = company_a.get("/attendance/reports/payroll", params={"start": start.isoformat(), "end": end.isoformat()}).json()
    prow = next(x for x in pay["rows"] if x["employee_id"] == emp["id"])
    assert Decimal(prow["lop_days"]) + Decimal(prow["adjustment_lop_days"]) == 2
    # lock: figures frozen, the excess carried into the next period, no more manual changes
    assert company_a.post("/attendance/locks", json={"start": start.isoformat(), "end": end.isoformat()}).status_code == 201
    assert company_a.post("/attendance/lop/adjustments", json={"employee_uuid": emp["uuid"], "start": start.isoformat(),
                                                               "end": end.isoformat(), "days": 1, "note": "x y z"}).status_code == 400
    ns = end + timedelta(days=1)
    ne = date(ns.year + ns.month // 12, ns.month % 12 + 1, 1) - timedelta(days=1)
    nxt = company_a.get("/attendance/lop", params={"start": ns.isoformat(), "end": ne.isoformat()}).json()
    carried = next(x for x in nxt["rows"] if x["employee_id"] == emp["id"])
    assert Decimal(carried["carried_in"]) == absent - 3
    # unlocking voids the frozen figures again
    lock = next(l for l in company_a.get("/attendance/locks").json()["items"] if l["period_start"] == start.isoformat())
    assert company_a.post(f"/attendance/locks/{lock['uuid']}/unlock", json={"note": "Fix"}).status_code == 200
    nxt = company_a.get("/attendance/lop", params={"start": ns.isoformat(), "end": ne.isoformat()}).json()
    assert all(Decimal(x["carried_in"]) == 0 for x in nxt["rows"] if x["employee_id"] == emp["id"])


def test_half_pay_sick_leave_goes_to_payroll(client, company_a):
    t = types(company_a)["SL"]
    rules = {**t["rules"], "pay_percent": 50}
    r = company_a.put(f"/leave/types/{t['uuid']}", json={"rules": rules, "effective_from": "2000-01-01", "change_note": "Half pay"})
    assert r.status_code == 200, r.text
    emp = hire(company_a, "Hari")
    adjust(company_a, emp, "SL", 5)
    d = working_monday()
    r = company_a.post("/leave/requests", json={"employee_uuid": emp["uuid"], "leave_type_uuid": t["uuid"],
                                                "from_date": d.isoformat(), "to_date": d.isoformat(), "reason": "Fever"})
    assert r.status_code == 201, r.text
    recompute(company_a, emp, d)
    row = day_row(company_a, emp, d)
    assert row["status"] == "on_leave" and row["payable_days"] == "0.50" and row["lop_days"] == "0.50"
    pay = company_a.get("/attendance/reports/payroll", params={"start": d.isoformat(), "end": d.isoformat()}).json()
    prow = next(x for x in pay["rows"] if x["employee_id"] == emp["id"])
    assert prow["reduced_pay_days"] == "0.50" and Decimal(prow["lop_days"]) == Decimal("0.5")
    lop = company_a.get("/leave/reports/lop", params={"period": f"{d.year}-{d.month}"}).json()
    lrow = next(x for x in lop["rows"] if x["employee_uuid"] == emp["uuid"])
    assert lrow["reduced_pay_days"] == 0.5
    # maternity must stay fully paid
    ml = types(company_a)["ML"]
    r = company_a.put(f"/leave/types/{ml['uuid']}", json={"rules": {**ml["rules"], "pay_percent": 50},
                                                          "effective_from": "2000-01-01", "change_note": "x"})
    assert r.status_code == 400


# ── Bradford, reminders, substitute holidays ────────────────────────

def test_bradford_report_and_alert(client, company_a):
    emp = hire(company_a, "Indu")
    adjust(company_a, emp, "SL", 10)
    sl = types(company_a)["SL"]["uuid"]
    monday = working_monday()
    for k in range(4):  # four separate one-day spells, a week apart
        d = monday - timedelta(days=7 * k)
        r = company_a.post("/leave/requests", json={"employee_uuid": emp["uuid"], "leave_type_uuid": sl,
                                                    "from_date": d.isoformat(), "to_date": d.isoformat(), "reason": "Ill"})
        assert r.status_code == 201, r.text
    company_a.put("/leave/settings", json={"bradford": {"include_absent": False,
                                                        "levels": [{"score": 50, "label": "Keep an eye"}]}})
    rep = company_a.get("/leave/reports/bradford").json()
    mine = next(r for r in rep["rows"] if r["employee_uuid"] == emp["uuid"])
    assert mine["spells"] == 4 and mine["days"] == 4 and mine["score"] == 64 and mine["label"] == "Keep an eye"
    assert company_a.post("/leave/bradford/alerts/run").json()["alerts"] >= 1
    assert company_a.post("/leave/bradford/alerts/run").json()["alerts"] == 0  # once per level
    me = login_as(company_a, emp)
    assert me.get("/leave/me/bradford").json()["score"]["score"] == 64
    assert me.get("/leave/reports/bradford").status_code == 403


def test_expiry_reminder_sent_once(client, company_a):
    from modules.leave import ledger
    from modules.leave.models import LeaveType
    emp = hire(company_a, "Jaya")
    db = system_session()
    try:
        db.info["company_id"] = company_a.id
        co = db.query(LeaveType).filter(LeaveType.company_id == company_a.id, LeaveType.code == "CO").first()
        td = today_ist()
        ledger.add_entry(db, company_a.id, emp["id"], co.id, td.year, td - timedelta(days=80), "comp_off", 1,
                         expires_on=td + timedelta(days=5), note="test credit")
        db.commit()
    finally:
        db.close()
    due = company_a.get("/leave/expiry-reminders/due").json()["items"]
    assert any(i["employee_id"] == emp["id"] and i["days"] == 1 for i in due)
    assert company_a.post("/leave/expiry-reminders/send").json()["employees"] >= 1
    assert company_a.post("/leave/expiry-reminders/send").json()["employees"] == 0


def test_substitute_day_off_for_a_holiday_on_a_weekly_off(client, company_a):
    from modules.leave.calendar import WorkCalendar
    from modules.employees.models import Employee
    emp = hire(company_a, "Kavya")
    cal_uuid = next(c for c in company_a.get("/leave/holiday-calendars").json() if c["is_default"])["uuid"]
    sunday = date(2027, 3, 7)
    r = company_a.post("/leave/holidays", json={"calendar_uuids": [cal_uuid], "name": "Test festival",
                                                "date": sunday.isoformat(), "kind": "festival"})
    assert r.status_code in (200, 201), r.text
    db = tenant_session(company_a.id)
    try:
        e = db.query(Employee).filter(Employee.id == emp["id"]).first()
        assert WorkCalendar(db, company_a.id).day(e, sunday + timedelta(days=1))["kind"] == "working"
    finally:
        db.close()
    assert company_a.put("/leave/settings", json={"substitute_holiday": {"enabled": True}}).status_code == 200
    db = tenant_session(company_a.id)
    try:
        e = db.query(Employee).filter(Employee.id == emp["id"]).first()
        day = WorkCalendar(db, company_a.id).day(e, sunday + timedelta(days=1))
        assert day["kind"] == "holiday" and day["holiday"] == "Substitute for Test festival"
    finally:
        db.close()
    me = login_as(company_a, emp)
    hols = me.get("/leave/me/holidays", params={"year": 2027}).json()["holidays"]
    assert any(h["kind"] == "substitute" and h["date"] == "2027-03-08" for h in hols)


# ── kiosk ───────────────────────────────────────────────────────────

def test_kiosk_punch_with_code_and_pin_and_offline_sync(client, company_a):
    emp = hire(company_a, "Lalit", employee_code="KS-101")
    r = company_a.post("/attendance/kiosks", json={"name": "Reception tablet", "photo": "optional"})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert token.startswith("ksk_")
    me = login_as(company_a, emp)
    assert me.put("/attendance/me/kiosk-pin", json={"pin": "1234"}).status_code == 400  # too easy
    assert me.put("/attendance/me/kiosk-pin", json={"pin": "4826"}).json()["has_pin"]
    info = client.get(f"/api/v1/attendance/kiosk/{token}")
    assert info.status_code == 200 and info.json()["name"] == "Reception tablet"
    assert client.get("/api/v1/attendance/kiosk/ksk_wrongwrongwrongwrongwrong").status_code == 404
    ok = client.post(f"/api/v1/attendance/kiosk/{token}/punch", json={"employee_code": "ks-101", "pin": "4826"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "ok" and ok.json()["first_name"] == "Lalit"
    # offline punches sent later keep their own time; a retry is a duplicate, not a second punch
    d = today_ist() - timedelta(days=3)
    at = datetime(d.year, d.month, d.day, 4, 0, tzinfo=timezone.utc).isoformat()  # 09:30 in India
    at2 = datetime(d.year, d.month, d.day, 13, 5, tzinfo=timezone.utc).isoformat()
    batch = {"punches": [{"employee_code": "KS-101", "pin": "4826", "at": at, "client_id": "a"},
                         {"employee_code": "KS-101", "pin": "4826", "at": at2, "client_id": "b"},
                         {"employee_code": "KS-101", "pin": "4826", "at": at, "client_id": "c"},
                         {"employee_code": "KS-101", "pin": "0000", "at": at2, "client_id": "d"}]}
    res = client.post(f"/api/v1/attendance/kiosk/{token}/sync", json=batch).json()["results"]
    assert [x["status"] for x in res] == ["ok", "ok", "duplicate", "error"], res
    assert res[0]["offline"] is True
    row = day_row(company_a, emp, d)
    assert row["punch_count"] == 2 and row["worked_minutes"] >= 480
    old = {"employee_code": "KS-101", "pin": "4826", "at": (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()}
    assert client.post(f"/api/v1/attendance/kiosk/{token}/punch", json=old).status_code == 400
    # five wrong PINs lock it for a while
    for _ in range(5):
        client.post(f"/api/v1/attendance/kiosk/{token}/punch", json={"employee_code": "KS-101", "pin": "9999"})
    locked = client.post(f"/api/v1/attendance/kiosk/{token}/punch", json={"employee_code": "KS-101", "pin": "4826"})
    assert locked.status_code == 400 and "Too many" in locked.json()["detail"]
    # HR can reset the PIN; a new link stops the old one
    assert company_a.put(f"/attendance/kiosk-pins/{emp['uuid']}", json={"pin": "7391"}).status_code == 200
    k = company_a.get("/attendance/kiosks").json()["items"][0]
    company_a.post(f"/attendance/kiosks/{k['uuid']}/link")
    assert client.get(f"/api/v1/attendance/kiosk/{token}").status_code == 404
    # photos required
    company_a.put(f"/attendance/kiosks/{k['uuid']}", json={"name": "Reception tablet", "photo": "required"})


# ── auto shift ──────────────────────────────────────────────────────

def test_auto_shift_from_check_in_time(client, company_a):
    emp = hire(company_a, "Mohan")
    shifts = {s["name"]: s for s in company_a.get("/shifts/").json()}
    morning = next(s for n, s in shifts.items() if n.startswith("Morning"))
    evening = next(s for n, s in shifts.items() if n.startswith("Evening"))
    r = company_a.put(f"/shifts/arrangements/{emp['uuid']}", json={
        "effective_from": "2020-01-01", "working_days": [0, 1, 2, 3, 4, 5, 6], "auto_shift": True})
    assert r.status_code == 200, r.text
    assert r.json()["auto_shift"] if "auto_shift" in r.json() else True
    dev = make_device(company_a)
    enrol(company_a, emp, "AS1")
    d = working_monday()
    m_start, e_start = morning["start_time"][:5], evening["start_time"][:5]
    eh, em = (int(x) for x in e_start.split(":"))
    push(client, dev["api_key"], [
        {"device_user_id": "AS1", "timestamp": f"{d}T{m_start}:00"},
        {"device_user_id": "AS1", "timestamp": f"{d}T{int(m_start[:2]) + 8:02d}:{m_start[3:]}:30"},
        {"device_user_id": "AS1", "timestamp": f"{d + timedelta(days=1)}T{eh:02d}:{em:02d}:00"},
        {"device_user_id": "AS1", "timestamp": f"{d + timedelta(days=1)}T{min(eh + 8, 23):02d}:{em:02d}:30"}])
    a, b = day_row(company_a, emp, d), day_row(company_a, emp, d + timedelta(days=1))
    assert a["shift_name"] == morning["name"] and any("check-in" in n for n in a["notes"]), a
    assert b["shift_name"] == evening["name"], b


# ── statutory registers ─────────────────────────────────────────────

def test_statutory_registers_in_every_format(client, company_a):
    emp = hire(company_a, "Nisha", employee_code="REG-1")
    dev = make_device(company_a)
    enrol(company_a, emp, "RG1")
    d = working_monday()
    push(client, dev["api_key"], [{"device_user_id": "RG1", "timestamp": f"{d}T09:25:00"},
                                  {"device_user_id": "RG1", "timestamp": f"{d}T21:30:00"}])
    company_a.put("/attendance/settings/registers", json={"config": {"establishment_name": "Alpha Works",
                                                                      "registration_no": "KA/123"}})
    formats = [f["key"] for f in company_a.get("/attendance/registers/formats").json()["items"]]
    assert set(formats) >= {"generic", "KA", "MH", "TN", "DL", "TS"}
    js = company_a.get("/attendance/registers", params={"format": "KA", "kind": "attendance", "year": d.year,
                                                         "month": d.month, "type": "json"}).json()
    row = next(r for r in js["rows"] if r[1] == "REG-1")
    assert "P" in row and "Overtime hours" in js["columns"]
    ot = company_a.get("/attendance/registers", params={"format": "generic", "kind": "overtime", "year": d.year,
                                                         "month": d.month, "type": "json"}).json()
    assert any(r[1] == "REG-1" for r in ot["rows"])
    for fmt in formats:
        x = company_a.get("/attendance/registers", params={"format": fmt, "year": d.year, "month": d.month, "type": "xlsx"})
        assert x.status_code == 200 and x.content[:2] == b"PK"
    p = company_a.get("/attendance/registers", params={"format": "MH", "kind": "overtime", "year": d.year,
                                                        "month": d.month, "type": "pdf"})
    assert p.status_code == 200 and p.content[:4] == b"%PDF"


# ── timesheet billing ───────────────────────────────────────────────

def _approved_hours(company, emp, entries):
    """Log hours [(project, date, hours)], submit the week and approve every line."""
    me = login_as(company, emp)
    for project, d, hours in entries:
        r = me.post("/timesheets/me/logs", json={"date": d.isoformat(), "project_id": project["id"], "hours": hours,
                                                 "description": "Work"})
        assert r.status_code == 201, r.text
    d = entries[0][1]
    week = d - timedelta(days=d.weekday())
    r = me.post("/timesheets/me/submit", json={"week_start": week.isoformat()})
    assert r.status_code == 200, r.text
    ts = next(t for t in company.get("/timesheets/approvals").json() if t["employee_id"] == emp["id"])
    detail = company.get(f"/timesheets/{ts['uuid']}").json()
    r = company.post(f"/timesheets/{ts['uuid']}/review", json={"lines": [
        {"log_uuid": l["uuid"], "decision": "approve"} for l in detail["lines"]]})
    assert r.status_code == 200, r.text


def test_timesheet_invoice_with_gst(client, company_a):
    company_a.put("/attendance/settings/billing", json={"config": {"supplier_name": "Alpha Works",
                                                                    "supplier_gstin": "29ABCDE1234F1Z5",
                                                                    "supplier_state": "29", "gst_rate": 18}})
    emp = hire(company_a, "Om")
    local = company_a.post("/timesheets/clients", json={"name": "Bengaluru Buyer", "gstin": "29AAACB1234C1Z1"}).json()
    far = company_a.post("/timesheets/clients", json={"name": "Mumbai Buyer", "state_code": "27"}).json()
    assert local["state_code"] == "29"
    p1 = company_a.post("/timesheets/projects", json={"name": "Portal", "client_id": local["id"], "billable": True})
    assert p1.status_code == 201, p1.text
    p1 = p1.json()
    p2 = company_a.post("/timesheets/projects", json={"name": "App", "client_id": far["id"], "billable": True}).json()
    d = working_monday()
    _approved_hours(company_a, emp, [(p1, d, 5), (p2, d + timedelta(days=1), 4)])
    # without a rate nothing can be billed
    pv = company_a.post("/timesheets/billing/invoices/preview", json={"client_uuid": local["uuid"],
                                                                      "period_start": d.isoformat(),
                                                                      "period_end": (d + timedelta(days=6)).isoformat()}).json()
    assert pv["lines"] == [] and pv["missing_rates"]
    assert company_a.post("/timesheets/billing/rates", json={"employee_uuid": emp["uuid"], "rate": 1000,
                                                             "effective_from": "2020-01-01"}).status_code == 201
    assert company_a.post("/timesheets/billing/rates", json={"project_uuid": p1["uuid"], "employee_uuid": emp["uuid"],
                                                             "rate": 2000, "effective_from": "2020-01-01"}).status_code == 201
    period = {"period_start": d.isoformat(), "period_end": (d + timedelta(days=6)).isoformat()}
    inv = company_a.post("/timesheets/billing/invoices", json={"client_uuid": local["uuid"], **period})
    assert inv.status_code == 201, inv.text
    inv = inv.json()
    assert inv["subtotal"] == "10000.00" and inv["cgst"] == "900.00" and inv["sgst"] == "900.00"
    assert inv["igst"] == "0.00" and inv["total"] == "11800.00" and inv["status"] == "draft"
    # the hours are on this invoice now, so a second one has nothing to bill
    assert company_a.post("/timesheets/billing/invoices", json={"client_uuid": local["uuid"], **period}).status_code == 400
    other = company_a.post("/timesheets/billing/invoices", json={"client_uuid": far["uuid"], **period}).json()
    assert other["igst"] == "720.00" and other["tax_mode"] == "inter" and other["cgst"] == "0.00"
    # status flow and the PDF
    assert company_a.post(f"/timesheets/billing/invoices/{inv['uuid']}/status", json={"status": "paid"}).status_code == 400
    sent = company_a.post(f"/timesheets/billing/invoices/{inv['uuid']}/status", json={"status": "sent"}).json()
    assert sent["number"] and sent["status"] == "sent"
    assert company_a.put(f"/timesheets/billing/invoices/{inv['uuid']}", json={"notes": "x"}).status_code == 400
    paid = company_a.post(f"/timesheets/billing/invoices/{inv['uuid']}/status",
                          json={"status": "paid", "reference": "UTR123"}).json()
    assert paid["status"] == "paid" and paid["paid_reference"] == "UTR123"
    pdf = company_a.get(f"/timesheets/billing/invoices/{inv['uuid']}/pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    # void frees the hours of the other invoice
    company_a.post(f"/timesheets/billing/invoices/{other['uuid']}/status", json={"status": "void"})
    again = company_a.post("/timesheets/billing/invoices", json={"client_uuid": far["uuid"], **period})
    assert again.status_code == 201
    # employees can't see billing
    me = login_as(company_a, emp)
    assert me.get("/timesheets/billing/invoices").status_code == 403


# ── access and tenant isolation ─────────────────────────────────────

def test_access_and_tenant_isolation(client, company_a, company_b):
    emp = hire(company_a, "Pooja")
    me = login_as(company_a, emp)
    assert me.get("/attendance/lop").status_code == 403
    assert me.get("/attendance/kiosks").status_code == 403
    assert me.get("/attendance/registers/formats").status_code == 403
    k = company_b.post("/attendance/kiosks", json={"name": "B tablet"}).json()
    assert company_a.put(f"/attendance/kiosks/{k['uuid']}", json={"name": "Taken"}).status_code == 404
    assert all(x["uuid"] != k["uuid"] for x in company_a.get("/attendance/kiosks").json()["items"])
    emp_b = hire(company_b, "Bobby", employee_code="B-1")
    login_as(company_b, emp_b).put("/attendance/me/kiosk-pin", json={"pin": "5829"})
    # A's kiosk can't punch B's people
    ka = company_a.post("/attendance/kiosks", json={"name": "A tablet"}).json()
    r = client.post(f"/api/v1/attendance/kiosk/{ka['token']}/punch", json={"employee_code": "B-1", "pin": "5829"})
    assert r.status_code == 400
    assert company_a.put(f"/attendance/kiosk-pins/{emp_b['uuid']}", json={"pin": "5829"}).status_code == 404
    client_b = company_b.post("/timesheets/clients", json={"name": "B client"}).json()
    assert company_a.post("/timesheets/billing/invoices/preview", json={
        "client_uuid": client_b["uuid"], "period_start": "2026-01-01", "period_end": "2026-01-31"}).status_code == 404
    db = tenant_session(company_a.id)
    try:
        for table in ("attendance_leave_links", "lop_period_entries", "attendance_kiosks", "kiosk_pins",
                      "ts_billing_rates", "ts_invoices", "leave_bradford_alerts", "leave_expiry_reminders"):
            n = db.execute(text(f"SELECT count(*) FROM {table} WHERE company_id = :b"), {"b": company_b.id}).scalar()
            assert n == 0, table
    finally:
        db.close()
