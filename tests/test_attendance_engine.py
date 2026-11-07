"""Daily status calculation and rest rules, without the database."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from modules.attendance.engine import DayInput, ShiftSpec, apply_penalties, compute_day, merged_rule
from modules.shifts.rest_rules import PlannedDay, check_schedule

IST = ZoneInfo("Asia/Kolkata")
D = date(2026, 11, 4)  # a Wednesday

GENERAL = ShiftSpec(id=1, name="General", start=time(9, 30), end=time(18, 30),
                    breaks=[{"name": "Lunch", "start": "13:30", "end": "14:30", "paid": False}],
                    grace_in=15, grace_out=10, half_day_hours=4, full_day_hours=8)
NIGHT = ShiftSpec(id=2, name="Night", start=time(22, 0), end=time(6, 0),
                  breaks=[{"name": "Meal", "start": "02:00", "end": "02:30", "paid": True}],
                  grace_in=10, grace_out=5, half_day_hours=4, full_day_hours=7.5, allowance_amount=150,
                  allowance_min_hours=6)


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=IST).astimezone(timezone.utc)


def day(punches, shift=GENERAL, **kw):
    return compute_day(DayInput(date=D, tz=IST, rule=kw.pop("rule", {}), shift=shift,
                                punches=[(p, None) for p in punches], **kw))


def test_normal_day_present_with_unpaid_lunch_deducted():
    r = day([at(D, 9, 25), at(D, 18, 35)])
    assert r.status == "present" and r.payable_days == Decimal("1.00") and r.lop_days == 0
    assert r.worked_minutes == 9 * 60 + 10 - 60 and r.break_minutes == 60
    assert not r.is_late and not r.is_early_exit and r.overtime_minutes == 0


def test_grace_period_then_late():
    assert not day([at(D, 9, 44), at(D, 18, 40)]).is_late  # 14 min, within 15 min grace
    r = day([at(D, 9, 50), at(D, 18, 50)])
    assert r.is_late and r.late_minutes == 20 and r.status == "present"
    assert any(n.startswith("Late by 20 min") for n in r.notes)


def test_early_exit_flag_and_half_day_by_hours():
    r = day([at(D, 9, 30), at(D, 18, 0)])
    assert r.is_early_exit and r.early_minutes == 30
    half = day([at(D, 9, 30), at(D, 14, 45)])  # 5 h 15 less 1 h lunch = 4 h 15
    assert half.status == "half_day" and half.payable_days == Decimal("0.50") and half.lop_days == Decimal("0.50")
    short = day([at(D, 9, 30), at(D, 12, 0)])
    assert short.status == "absent" and short.lop_days == 1


def test_missing_punch_and_absent():
    r = day([at(D, 9, 31)])
    assert r.status == "missing_punch" and r.payable_days == 0 and r.lop_days == 1
    assert day([]).status == "absent"
    lenient = day([at(D, 9, 31)], rule={"missing_punch_payable": 0.5})
    assert lenient.payable_days == Decimal("0.50")


def test_overtime_after_threshold_and_cap():
    r = day([at(D, 9, 30), at(D, 20, 30)])  # 2 h after shift end
    assert r.status == "present" and r.overtime_minutes == 120
    little = day([at(D, 9, 30), at(D, 18, 50)])  # 20 min extra: below the 30 min threshold
    assert little.overtime_minutes == 0
    capped = day([at(D, 9, 30), at(D, 23, 59)], rule={"overtime": {"daily_cap_minutes": 180}})
    assert capped.overtime_minutes == 180


def test_overtime_needs_preapproval_when_rule_says_so():
    rule = {"overtime": {"requires_preapproval": True}}
    r = day([at(D, 9, 30), at(D, 21, 30)], rule=rule)
    assert r.overtime_minutes == 0 and r.unapproved_overtime_minutes == 180
    ok = day([at(D, 9, 30), at(D, 21, 30)], rule=rule, overtime_approved_minutes=120)
    assert ok.overtime_minutes == 120 and ok.unapproved_overtime_minutes == 60


def test_night_shift_across_midnight():
    nxt = D + timedelta(days=1)
    r = day([at(D, 21, 55), at(nxt, 6, 10)], shift=NIGHT)
    assert r.status == "present" and r.is_night_shift and not r.is_late
    assert r.worked_minutes == 8 * 60 + 15  # paid meal break is not deducted
    assert r.night_minutes == 8 * 60  # only 22:00 to 06:00 counts
    assert r.shift_allowance
    late = day([at(D, 22, 40), at(nxt, 6, 0)], shift=NIGHT)
    assert late.is_late and late.late_minutes == 40


def test_holiday_weekly_off_leave_wfh_on_duty():
    assert day([], day_type="holiday", holiday_name="Diwali").status == "holiday"
    worked = day([at(D, 9, 30), at(D, 13, 30)], day_type="weekly_off")
    assert worked.status == "weekly_off" and worked.overtime_minutes == 240 and worked.payable_days == 1
    leave = day([], leave={"unit": "full", "paid": True, "name": "Casual leave"})
    assert leave.status == "on_leave" and leave.payable_days == 1
    lop = day([], leave={"unit": "full", "paid": False, "name": "Leave without pay"})
    assert lop.payable_days == 0 and lop.lop_days == 1
    assert day([], wfh="full_day").status == "wfh"
    assert day([], on_duty="full_day").status == "on_duty"
    half_leave = day([at(D, 14, 30), at(D, 18, 35)], leave={"unit": "first_half", "paid": True, "name": "CL"})
    assert half_leave.status == "half_day" and half_leave.payable_days == 1 and not half_leave.is_late


def test_permission_hours_count_towards_minimum_and_no_punch_staff():
    short = day([at(D, 9, 30), at(D, 16, 30)])  # 6 h worked: half day
    assert short.status == "half_day"
    with_perm = day([at(D, 9, 30), at(D, 16, 30)], permission_minutes=120)
    assert with_perm.status == "present"
    auto = day([], punch_required=False)
    assert auto.status == "present" and auto.payable_days == 1


def test_rounding_and_pairs():
    r = day([at(D, 9, 31), at(D, 18, 29)], rule={"rounding": {"in_minutes": 15, "out_minutes": 15}})
    assert r.first_in == at(D, 9, 31) and r.worked_minutes == (18 * 60 + 15) - (9 * 60 + 45) - 60
    pairs = day([at(D, 9, 30), at(D, 13, 30), at(D, 14, 30), at(D, 18, 30)], rule={"hours_calc": "pairs"})
    assert pairs.worked_minutes == 8 * 60 and pairs.break_minutes == 60


def test_today_before_shift_is_not_yet_and_running_hours():
    morning = datetime.combine(D, time(8, 0), tzinfo=IST)
    assert day([], now=morning).status == "not_yet"
    noon = datetime.combine(D, time(12, 0), tzinfo=IST)
    r = day([at(D, 9, 30)], now=noon)
    assert r.status == "present" and r.worked_minutes == 150 and "Still clocked in" in r.notes


def test_hr_override_wins():
    r = day([], override_status="on_duty")
    assert r.status == "on_duty" and r.payable_days == 1


def test_late_penalty_every_third_late():
    days = [{"date": D + timedelta(days=i), "is_late": True, "is_early_exit": False, "status": "present"} for i in range(7)]
    pen = apply_penalties(days, {})
    assert pen == {D + timedelta(days=2): Decimal("0.50"), D + timedelta(days=5): Decimal("0.50")}
    once = apply_penalties(days, {"late_penalty": {"every_multiple": False}})
    assert list(once) == [D + timedelta(days=2)]
    full = apply_penalties(days, {"late_penalty": {"count": 2, "deduct_days": 1.0}})
    assert sum(full.values()) == Decimal("3.00")
    assert apply_penalties(days, {"late_penalty": {"enabled": False}}) == {}


def test_rule_defaults_pay_overtime_at_twice_the_rate():
    assert merged_rule({})["overtime"]["rate_multiplier"] == 2.0


# ── Rest rules ──────────────────────────────────────────────────────

def plan(d: date, start_h: int, hours: int = 8, new=True, night=False, women_night=False) -> PlannedDay:
    s = datetime.combine(d, time(start_h), tzinfo=IST)
    return PlannedDay(date=d, working=True, start=s, end=s + timedelta(hours=hours), net_minutes=hours * 60,
                      night=night, women_night=women_night, shift_name="X", new=new)


def test_min_rest_between_shifts_blocks():
    days = [plan(D, 14), plan(D + timedelta(days=1), 6)]  # 22:00 to 06:00: 8 h rest
    v = check_schedule(days, {"min_rest_hours": {"value": 10, "action": "block"}})
    assert [x.rule for x in v] == ["min_rest_hours"] and v[0].action == "block"


def test_weekly_rest_day_and_weekly_hours():
    monday = D - timedelta(days=D.weekday())
    week = [plan(monday + timedelta(days=i), 9) for i in range(7)]
    v = check_schedule(week, {})
    rules = {x.rule for x in v}
    assert "max_consecutive_days" in rules and "max_weekly_hours" in rules
    six = week[:6]
    assert "max_consecutive_days" not in {x.rule for x in check_schedule(six, {})}


def test_old_problems_do_not_block_unrelated_changes():
    days = [plan(D, 14, new=False), plan(D + timedelta(days=1), 6, new=False), plan(D + timedelta(days=5), 9)]
    assert check_schedule(days, {}) == []


def test_women_night_shift_needs_consent():
    d = plan(D, 22, night=True, women_night=True)
    v = check_schedule([d], {}, is_woman=True, has_consent=lambda _d: False)
    assert v and v[0].rule == "women_night_consent" and v[0].action == "block"
    assert check_schedule([d], {}, is_woman=True, has_consent=lambda _d: True) == []
    assert check_schedule([d], {}, is_woman=False) == []
