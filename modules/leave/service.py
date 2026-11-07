"""Leave requests: working out the days, checking the rules, applying, approving,
cancelling (fully or some days), extending and changing a pending request. Also
compensatory off requests and the self-service summary.

A request is worked out day by day (`evaluate`): each date gets a kind from the work
calendar (working, half, weekend, holiday) and an amount. Sandwich rules turn weekends
and holidays between (or next to) leave days into leave. The balance check then splits
the days into paid and loss of pay according to the type's negative-balance rule.

Approval goes through the workflow engine (request type leave.request). On approval the
paid days are taken from the ledger ("availed"); cancelling approved days puts them back
("restored"). Grant-based types (maternity, marriage ...) are credited on approval and
taken at once, so their balance stays at zero.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core import audit, context
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import today_for_company_id, utc_now
from modules.leave import ledger, policy
from modules.leave.calendar import WorkCalendar
from modules.leave.facts import applies
from modules.leave.ledger import D, ZERO, q2
from modules.leave.models import CompOffRequest, LeaveRequest, LeaveType

UNITS = {"day": "Full day", "half_day": "Half day", "quarter_day": "Quarter day", "hour": "Hours"}
ACTIVE = ("pending", "approved", "cancel_pending")
MAX_SPAN_DAYS = 400
MAX_ATTACHMENT = 5 * 1024 * 1024


def _employee_model():
    from modules.employees.models import Employee
    return Employee


def emp_name(emp) -> str:
    return f"{emp.first_name} {emp.last_name}".strip() if emp else ""


def _fmt(n) -> str:
    n = float(n or 0)
    return str(int(n)) if n == int(n) else f"{n:g}"


def days_text(n) -> str:
    return f"{_fmt(n)} day" + ("" if float(n) == 1 else "s")


def _history(req: LeaveRequest, action: str, note: str | None = None) -> None:
    req.history = [*(req.history or []), {"at": utc_now().isoformat().replace("+00:00", "Z"),
                                          "by": context.current().actor_name or "System", "action": action,
                                          **({"note": note} if note else {})}]


def get_employee(db: Session, company_id: int, employee_id: int):
    Employee = _employee_model()
    emp = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if emp is None:
        raise NotFound("Employee")
    return emp


def get_employee_by_uuid(db: Session, company_id: int, uuid):
    Employee = _employee_model()
    emp = db.query(Employee).filter(Employee.company_id == company_id, Employee.uuid == uuid).first()
    if emp is None:
        raise NotFound("Employee")
    return emp


# ── Leave already taken ─────────────────────────────────────────────

def leave_on_dates(db: Session, company_id: int, employee_id: int, start: date, end: date,
                   exclude_ids: tuple = ()) -> dict[date, list[tuple[LeaveRequest, dict]]]:
    rows = (db.query(LeaveRequest).options(joinedload(LeaveRequest.leave_type))
            .filter(LeaveRequest.company_id == company_id, LeaveRequest.employee_id == employee_id,
                    LeaveRequest.status.in_(ACTIVE), LeaveRequest.from_date <= end,
                    LeaveRequest.to_date >= start).all())
    out: dict[date, list] = defaultdict(list)
    for r in rows:
        if r.id in exclude_ids:
            continue
        for it in r.breakdown or []:
            d = date.fromisoformat(it["date"])
            if start <= d <= end and not it.get("cancelled") and D(it.get("amount")) > 0:
                out[d].append((r, it))
    return out


# ── Working out a request ───────────────────────────────────────────

def evaluate(db: Session, company_id: int, emp, t: LeaveType, data: dict, *, exclude_id: int | None = None,
             cal: WorkCalendar | None = None, enforce: bool = True, parent: LeaveRequest | None = None) -> dict:
    """Work out a request without saving it. Returns breakdown, totals, balance figures,
    warnings and (when `enforce`) raises BadRequest for any rule that is broken."""
    cal = cal or WorkCalendar(db, company_id)
    today = today_for_company_id(db, company_id)
    start, end = data["from_date"], data.get("to_date") or data["from_date"]
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)
    if end < start:
        raise BadRequest("The end date is before the start date.")
    if (end - start).days > MAX_SPAN_DAYS:
        raise BadRequest("A request can cover at most 400 days.")
    if not t.active:
        raise BadRequest(f"{t.name} is switched off.")
    version = policy.version_on(t, start)
    rules = policy.rules_on(t, start)
    r, ent = rules["restrictions"], rules["entitlement"]
    unit = data.get("unit") or "day"
    if unit not in UNITS:
        raise BadRequest("Choose full day, half day, quarter day or hours.")
    allowed = {"day": True, "half_day": rules["allow_half_day"], "quarter_day": rules["allow_quarter_day"],
               "hour": rules["allow_hourly"]}
    if not allowed[unit]:
        raise BadRequest(f"{t.name} can't be taken as {UNITS[unit].lower()}.")
    if unit != "day" and start != end:
        raise BadRequest(f"A {UNITS[unit].lower()} request is for one date only.")
    half = data.get("half") if unit in ("half_day", "quarter_day") else None
    if unit in ("half_day", "quarter_day") and half not in ("first", "second"):
        half = "first"
    hours = D(data.get("hours")) if unit == "hour" else None
    if unit == "hour" and (hours is None or hours <= 0 or hours > D(rules["hours_per_day"])):
        raise BadRequest(f"Enter between 0.5 and {_fmt(rules['hours_per_day'])} hours.")

    errors: list[str] = []
    warnings: list[str] = []
    f = cal.facts(emp)
    if enforce and not applies(rules["applicability"], f, start):
        raise BadRequest(f"{t.name} doesn't apply to {emp.first_name}.")
    if f.get("exit_date") and end > f["exit_date"]:
        errors.append(f"Leave can't go past the last working day ({f['exit_date']:%d %b %Y}).")

    # ── day by day ──
    items = []
    for x in cal.days(emp, start, end):
        d, kind = x["date"], x["kind"]
        if kind in ("working", "half"):
            amount = x["value"]
        elif kind == "weekend":
            amount = Decimal("1") if r["count_weekends"] else ZERO
        else:
            amount = Decimal("1") if r["count_holidays"] else ZERO
        it = {"date": d.isoformat(), "kind": kind, "amount": amount, "lop": ZERO}
        if x.get("holiday"):
            it["holiday"] = x["holiday"]
        if x.get("optional"):
            it["optional"] = x["optional"]
        items.append(it)

    if r["only_optional_holidays"] or t.kind == "optional_holiday":
        for it in items:
            if not it.get("optional"):
                errors.append(f"{date.fromisoformat(it['date']):%d %b} is not an optional holiday in your calendar.")
            else:
                it["amount"], it["kind"] = Decimal("1"), "optional"
                it["holiday"] = it["optional"][0]

    if t.kind == "optional_holiday" and not errors:
        errors += _optional_limit(db, company_id, cal, emp, t, items, exclude_id)

    if unit != "day":
        it = items[0]
        if it["amount"] <= 0:
            raise BadRequest(f"{start:%d %b %Y} is not a working day.")
        if unit == "half_day":
            it["amount"] = min(it["amount"], Decimal("0.5"))
        elif unit == "quarter_day":
            it["amount"] = Decimal("0.25")
        else:
            it["amount"] = q2(hours / D(rules["hours_per_day"]))
        it["part"] = half or "hours"

    # ── sandwich ──
    sw = r["sandwich"]
    if unit == "day" and (sw["weekends"] or sw["holidays"]):
        base_total = sum((it["amount"] for it in items if it["kind"] in ("working", "half")), ZERO)
        if base_total >= D(sw["min_days"] or 0):
            worked_idx = [i for i, it in enumerate(items) if it["kind"] in ("working", "half") and it["amount"] > 0]
            if worked_idx:
                lo, hi = worked_idx[0], worked_idx[-1]
                for i, it in enumerate(items):
                    if lo < i < hi and it["amount"] == 0 and _sandwichable(it, sw):
                        it["amount"], it["sandwich"] = Decimal("1"), True
            if sw["adjacent"]:
                taken = leave_on_dates(db, company_id, emp.id, start - timedelta(days=15), end + timedelta(days=15),
                                       exclude_ids=(exclude_id,) if exclude_id else ())
                before = _adjacent_block(cal, emp, start, -1, sw, taken)
                after = _adjacent_block(cal, emp, end, 1, sw, taken)
                items = [*before, *items, *after]

    total = sum((it["amount"] for it in items), ZERO)
    if total <= 0:
        raise BadRequest("These dates have no working days, so there is nothing to apply for.")

    # ── clashes with other leave ──
    taken = leave_on_dates(db, company_id, emp.id, date.fromisoformat(items[0]["date"]),
                           date.fromisoformat(items[-1]["date"]), exclude_ids=(exclude_id,) if exclude_id else ())
    for it in items:
        d = date.fromisoformat(it["date"])
        if it["amount"] <= 0 or d not in taken:
            continue
        clash = [(rq, x) for rq, x in taken[d]
                 if not (unit in ("half_day", "quarter_day", "hour") and x.get("part") and x.get("part") != it.get("part")
                         and D(x["amount"]) + it["amount"] <= 1)]
        if clash:
            rq = clash[0][0]
            errors.append(f"You already have {rq.leave_type.name.lower()} on {d:%d %b %Y}.")
            break

    # ── rules ──
    t_days = total
    span = (date.fromisoformat(items[-1]["date"]) - date.fromisoformat(items[0]["date"])).days + 1
    if r["min_days"] and t_days < D(r["min_days"]):
        errors.append(f"{t.name} must be at least {days_text(r['min_days'])} per request.")
    if r["max_days"] and t_days > D(r["max_days"]):
        errors.append(f"{t.name} can be at most {days_text(r['max_days'])} per request.")
    if r["max_consecutive"] and unit == "day" and span > D(r["max_consecutive"]):
        errors.append(f"{t.name} can't run for more than {days_text(r['max_consecutive'])} in a row.")
    lead = (start - today).days
    if r["notice_days"] and t_days > D(r["notice_above_days"] or 0) and 0 <= lead < r["notice_days"]:
        errors.append(f"Apply for {t.name.lower()} at least {days_text(r['notice_days'])} in advance"
                      + (f" when it is more than {days_text(r['notice_above_days'])}" if r["notice_above_days"] else "") + ".")
    if lead < 0 and r["backdated_days"] is not None and -lead > r["backdated_days"]:
        errors.append(f"{t.name} can be applied for at most {days_text(r['backdated_days'])} in the past."
                      if r["backdated_days"] else f"{t.name} can't be applied for past dates.")
    if r["future_days"] is not None and lead > r["future_days"]:
        errors.append(f"{t.name} can be applied for at most {days_text(r['future_days'])} ahead.")
    has_doc = bool(data.get("attachment") or data.get("has_attachment"))
    doc_needed = bool(r["document_after_days"] is not None and t_days > D(r["document_after_days"]))
    if doc_needed and not has_doc:
        errors.append(f"Attach a supporting document (needed for more than {days_text(r['document_after_days'])}).")
    errors += _request_count_rules(db, company_id, emp, t, rules, start, end, exclude_id, parent)
    errors += _clubbing(db, company_id, cal, emp, t, rules, items, exclude_id)

    # ── grants ──
    grant_option = None
    if ent["mode"] == "grant":
        opts = rules["grant"]["options"]
        if opts:
            key = data.get("grant_option") or (opts[0]["key"] if len(opts) == 1 else None)
            grant_option = next((o for o in opts if o["key"] == key), None)
            if grant_option is None:
                errors.append("Choose which kind of " + t.name.lower() + " this is.")
            elif t_days > D(grant_option["days"]) and parent is None:
                errors.append(f"{grant_option['label']} allows at most {days_text(grant_option['days'])}.")
        errors += _grant_rules(db, company_id, emp, t, rules, start, exclude_id, parent)

    # ── balance and loss of pay ──
    bal = _balance_split(db, company_id, cal, emp, t, rules, items, exclude_id, today)
    errors += bal.pop("errors")
    warnings += bal.pop("warnings")

    if enforce and errors:
        raise BadRequest(errors[0])
    lop = sum((it["lop"] for it in items), ZERO)
    return {
        "leave_type": t, "version": version, "rules": rules, "from_date": start, "to_date": end, "unit": unit,
        "half": half, "hours": hours, "items": items, "days": q2(total), "lop_days": q2(lop),
        "paid_days": q2(total - lop), "errors": errors, "warnings": warnings, "balance": bal,
        "document_needed": doc_needed, "grant_option": grant_option["key"] if grant_option else None,
        "sandwich_days": q2(sum((it["amount"] for it in items if it.get("sandwich")), ZERO)),
    }


def _optional_limit(db, company_id, cal, emp, t, items, exclude_id) -> list[str]:
    hc = cal.calendar_for(emp)
    limit = hc.optional_limit if hc else 0
    out = []
    years = defaultdict(int)
    for it in items:
        years[it["date"][:4]] += 1
    for y, n in years.items():
        q = db.query(LeaveRequest).filter(LeaveRequest.company_id == company_id, LeaveRequest.employee_id == emp.id,
                                          LeaveRequest.leave_type_id == t.id, LeaveRequest.status.in_(ACTIVE),
                                          LeaveRequest.from_date >= date(int(y), 1, 1),
                                          LeaveRequest.from_date <= date(int(y), 12, 31))
        if exclude_id:
            q = q.filter(LeaveRequest.id != exclude_id)
        used = sum(1 for r in q.all() for x in r.breakdown or [] if not x.get("cancelled"))
        if used + n > limit:
            out.append(f"You can take {limit} optional holiday{'s' if limit != 1 else ''} in {y}; "
                       f"{used} already used.")
    return out


def _sandwichable(it: dict, sw: dict) -> bool:
    return (it["kind"] == "weekend" and sw["weekends"]) or (it["kind"] == "holiday" and sw["holidays"])


def _adjacent_block(cal: WorkCalendar, emp, edge: date, step: int, sw: dict, taken: dict) -> list[dict]:
    """Weekends/holidays right before (step -1) or after (step 1) the request that touch
    other leave on their far side are counted too."""
    block, d = [], edge + timedelta(days=step)
    for _ in range(10):
        x = cal.day(emp, d)
        if x["kind"] in ("working", "half"):
            break
        if not _sandwichable({"kind": x["kind"]}, sw):
            return []
        block.append({"date": d.isoformat(), "kind": x["kind"], "amount": Decimal("1"), "lop": ZERO, "sandwich": True,
                      "adjacent": True, **({"holiday": x["holiday"]} if x.get("holiday") else {})})
        d += timedelta(days=step)
    else:
        return []
    if not block or d not in taken:
        return []
    return list(reversed(block)) if step < 0 else block


def _period_bounds(rules: dict, period: str, on: date) -> tuple[date | None, date | None]:
    if period == "month":
        s = date(on.year, on.month, 1)
        return s, date(on.year + on.month // 12, on.month % 12 + 1, 1) - timedelta(days=1)
    if period == "quarter":
        qm = (on.month - 1) // 3 * 3 + 1
        s = date(on.year, qm, 1)
        m = qm + 3
        return s, date(on.year + (m - 1) // 12, (m - 1) % 12 + 1, 1) - timedelta(days=1)
    if period == "year":
        _, s, e = policy.year_bounds(rules, on)
        return s, e
    return None, None


def _request_count_rules(db, company_id, emp, t, rules, start, end, exclude_id, parent) -> list[str]:
    r = rules["restrictions"]
    out = []
    if parent is not None:
        return out
    q = db.query(LeaveRequest).filter(LeaveRequest.company_id == company_id, LeaveRequest.employee_id == emp.id,
                                      LeaveRequest.leave_type_id == t.id, LeaveRequest.status.in_(ACTIVE),
                                      LeaveRequest.kind == "leave")
    if exclude_id:
        q = q.filter(LeaveRequest.id != exclude_id)
    mr = r["max_requests"]
    if mr.get("count"):
        s, e = _period_bounds(rules, mr["period"], start)
        qq = q.filter(LeaveRequest.from_date >= s, LeaveRequest.from_date <= e) if s else q
        if qq.count() >= int(mr["count"]):
            word = {"month": "a month", "quarter": "a quarter", "year": "a year", "tenure": "in total"}[mr["period"]]
            out.append(f"{t.name} can be requested at most {mr['count']} time{'s' if mr['count'] != 1 else ''} {word}.")
    if r["min_gap_days"]:
        gap = int(r["min_gap_days"])
        near = q.filter(LeaveRequest.from_date <= end + timedelta(days=gap),
                        LeaveRequest.to_date >= start - timedelta(days=gap)).first()
        if near is not None:
            out.append(f"Leave a gap of at least {days_text(gap)} between two {t.name.lower()} requests.")
    return out


def _grant_rules(db, company_id, emp, t, rules, start, exclude_id, parent) -> list[str]:
    g = rules["grant"]
    out = []
    if parent is not None:
        return out
    q = db.query(LeaveRequest).filter(LeaveRequest.company_id == company_id, LeaveRequest.employee_id == emp.id,
                                      LeaveRequest.leave_type_id == t.id, LeaveRequest.kind == "leave",
                                      LeaveRequest.status.in_(("pending", "approved", "cancel_pending")))
    if exclude_id:
        q = q.filter(LeaveRequest.id != exclude_id)
    if g.get("max_requests"):
        qq = q
        if g["period"] == "year":
            _, s, e = policy.year_bounds(rules, start)
            qq = q.filter(LeaveRequest.from_date >= s, LeaveRequest.from_date <= e)
        if qq.count() >= int(g["max_requests"]):
            out.append(f"{t.name} can be granted at most {g['max_requests']} time{'s' if g['max_requests'] != 1 else ''}"
                       + (" a year." if g["period"] == "year" else "."))
    if g.get("min_gap_days"):
        last = q.order_by(LeaveRequest.to_date.desc()).first()
        if last is not None and abs((start - last.to_date).days) < int(g["min_gap_days"]):
            out.append(f"{t.name} needs a gap of at least {days_text(g['min_gap_days'])} since the last one.")
    return out


def _clubbing(db, company_id, cal, emp, t, rules, items, exclude_id) -> list[str]:
    """Leave types that may not be taken right next to this one (either side, skipping
    weekly offs and holidays)."""
    blocked_here = set(rules["restrictions"]["clubbing_blocked"])
    first, last = date.fromisoformat(items[0]["date"]), date.fromisoformat(items[-1]["date"])
    neighbours = []
    for edge, step in ((first, -1), (last, 1)):
        d = edge + timedelta(days=step)
        for _ in range(10):
            if cal.day(emp, d)["kind"] in ("working", "half"):
                break
            d += timedelta(days=step)
        neighbours.append(d)
    taken = leave_on_dates(db, company_id, emp.id, min(neighbours), max(neighbours),
                           exclude_ids=(exclude_id,) if exclude_id else ())
    for d in neighbours:
        for rq, _ in taken.get(d, []):
            other = rq.leave_type
            if other.id == t.id:
                continue
            other_blocks = set(policy.rules_on(other, rq.from_date)["restrictions"]["clubbing_blocked"])
            if other.code in blocked_here or t.code in other_blocks:
                return [f"{t.name} can't be taken right next to {other.name.lower()} ({d:%d %b})."]
    return []


def _balance_split(db, company_id, cal, emp, t: LeaveType, rules: dict, items: list[dict], exclude_id,
                   today: date) -> dict:
    """Check the balance per leave year and mark loss-of-pay days (from the end)."""
    out = {"errors": [], "warnings": [], "years": []}
    unpaid = t.kind == "lop" or rules["paid"] in ("unpaid", "lop")
    if unpaid:
        for it in items:
            it["lop"] = it["amount"] if rules["paid"] == "lop" or t.kind == "lop" else ZERO
        if rules["paid"] == "unpaid" and t.kind != "lop":
            for it in items:
                it["lop"] = it["amount"]
        out["warnings"].append("This is unpaid leave; these days are deducted from pay.")
        return out
    if rules["entitlement"]["mode"] == "grant":
        return out
    by_year: dict[int, list[dict]] = defaultdict(list)
    for it in items:
        d = date.fromisoformat(it["date"])
        by_year[policy.year_bounds(policy.rules_on(t, d), d)[0]].append(it)
    neg = rules["restrictions"]["negative"]
    for year, its in sorted(by_year.items()):
        need = sum((x["amount"] for x in its), ZERO)
        bal = ledger.balance(db, company_id, emp.id, t, year)
        expiring = ledger.pending_expiry(db, company_id, emp.id, t, year, today) if t.kind == "comp_off" else ZERO
        book = ledger.booked(db, company_id, emp.id, t, year, exclude_request_id=exclude_id)
        available = bal - expiring - book
        extra = ZERO
        if neg["mode"] == "limit":
            extra = D(neg["limit"])
        elif neg["mode"] == "year_end":
            extra = ledger.remaining_accrual(db, cal, emp, t, year, today)
        info = {"year": year, "year_text": policy.year_text(rules, year), "balance": float(bal),
                "booked": float(book), "available": float(available), "requested": float(need),
                "after": float(available - need), "can_go_negative": float(extra)}
        out["years"].append(info)
        if need <= available:
            continue
        short = need - max(available, ZERO)
        if neg["mode"] == "lop":
            remaining = short
            for x in reversed(its):
                if remaining <= 0:
                    break
                take = min(x["amount"], remaining)
                x["lop"] = take
                remaining -= take
            out["warnings"].append(f"Only {days_text(max(available, ZERO))} available; "
                                   f"{days_text(short)} will be loss of pay.")
        elif neg["mode"] in ("limit", "year_end") and need <= available + extra:
            out["warnings"].append(f"Your balance will go to {_fmt(available - need)} days.")
        else:
            out["errors"].append(f"Only {days_text(max(available, ZERO))} of {t.name.lower()} available"
                                 + (f" in {info['year_text']}" if len(by_year) > 1 else "") + ".")
    return out


def _items_json(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        x = {k: v for k, v in it.items()}
        x["amount"] = float(it["amount"])
        x["lop"] = float(it.get("lop") or 0)
        out.append(x)
    return out


# ── Apply ───────────────────────────────────────────────────────────

def _decode_attachment(data: dict) -> tuple[str | None, str | None]:
    att = data.get("attachment")
    if not att:
        return None, None
    if not isinstance(att, str) or not att.startswith("data:"):
        raise BadRequest("Attach the document again.")
    if len(att) > MAX_ATTACHMENT * 1.4:
        raise BadRequest("The document is too large (5 MB at most).")
    return (data.get("attachment_name") or "document")[:255], att


def apply(db: Session, company_id: int, emp, t: LeaveType, data: dict, *, on_behalf: bool = False,
          parent: LeaveRequest | None = None) -> LeaveRequest:
    rules_now = policy.rules_on(t, date.fromisoformat(str(data["from_date"])))
    if rules_now["restrictions"]["hidden_from_employee"] and not on_behalf:
        raise BadRequest(f"{t.name} is recorded by HR. Ask HR to add it for you.")
    if t.kind == "comp_off" and not policy.get_settings(db, company_id)["comp_off"]["enabled"]:
        raise BadRequest("Comp off is switched off.")
    ev = evaluate(db, company_id, emp, t, data, enforce=not on_behalf, parent=parent)
    if on_behalf and ev["errors"]:
        # HR may record leave outside the usual limits, but never over a clash or a missing balance
        hard = [e for e in ev["errors"] if "already have" in e or "available" in e]
        if hard:
            raise BadRequest(hard[0])
    return create_from_evaluation(db, company_id, emp, t, ev, data, on_behalf=on_behalf, parent=parent)


def create_from_evaluation(db: Session, company_id: int, emp, t: LeaveType, ev: dict, data: dict, *,
                           on_behalf: bool = False, parent: LeaveRequest | None = None) -> LeaveRequest:
    name, att = _decode_attachment(data)
    req = LeaveRequest(
        company_id=company_id, employee_id=emp.id, leave_type_id=t.id,
        policy_version_id=ev["version"].id if ev["version"] else None,
        kind="extension" if parent is not None else "leave", parent_id=parent.id if parent is not None else None,
        from_date=ev["from_date"], to_date=ev["to_date"], unit=ev["unit"], half=ev["half"], hours=ev["hours"],
        days=ev["days"], paid_days=ev["paid_days"], lop_days=ev["lop_days"], breakdown=_items_json(ev["items"]),
        grant_option=ev["grant_option"], reason=(data.get("reason") or "").strip() or None,
        status="pending", attachment_name=name, attachment=att, applied_by_name=context.current().actor_name,
        on_behalf=on_behalf,
    )
    db.add(req)
    db.flush()
    _history(req, "Recorded by HR" if on_behalf else ("Extension asked" if parent is not None else "Applied"),
             req.reason)
    req.leave_type = t
    if on_behalf:
        req.reference_no = _reference(db, company_id)
        approve(db, req, note="Recorded by HR")
        audit.event(db, "leave.recorded", "leave_requests", req.id,
                    {"employee_id": emp.id, "leave_type": t.code, "days": float(req.days)},
                    entity_uuid=str(req.uuid), entity_label=_title(req, emp))
        return req
    from core.workflow import service as workflow
    inst = workflow.submit(db, company_id, "leave.request", _payload(req, emp, t), _title(req, emp),
                           subject_type="leave_requests", subject_id=req.id)
    req.workflow_instance_id = inst.id
    req.reference_no = inst.reference_no
    if parent is not None:
        _history(parent, "Extension asked", f"Until {req.to_date:%d %b %Y}")
    db.flush()
    return req


def _reference(db: Session, company_id: int) -> str | None:
    from core import numbering
    return numbering.next_number(db, company_id, "request", today_for_company_id(db, company_id))


def _payload(req: LeaveRequest, emp, t: LeaveType) -> dict:
    return {"request_id": req.id, "request_uuid": str(req.uuid), "employee_id": emp.id, "leave_type": t.code,
            "leave_type_name": t.name, "days": float(req.days), "paid_days": float(req.paid_days),
            "lop_days": float(req.lop_days), "from_date": req.from_date.isoformat(), "to_date": req.to_date.isoformat(),
            "unit": req.unit, "kind": req.kind, "reason": req.reason, "paid": t.kind != "lop"}


def _dates_text(a: date, b: date) -> str:
    if a == b:
        return f"{a:%d %b %Y}"
    if a.year == b.year:
        return f"{a:%d %b} – {b:%d %b %Y}"
    return f"{a:%d %b %Y} – {b:%d %b %Y}"


def _title(req: LeaveRequest, emp) -> str:
    t = req.leave_type
    unit = "" if req.unit == "day" else f" ({UNITS[req.unit].lower()})"
    prefix = "Extension of " if req.kind == "extension" else ""
    return f"{prefix}{t.name}{unit}: {emp_name(emp)}, {days_text(req.days)}, {_dates_text(req.from_date, req.to_date)}"


# ── Approval results ────────────────────────────────────────────────

def approve(db: Session, req: LeaveRequest, note: str | None = None) -> None:
    """Take the paid days from the balance (grant types are credited first)."""
    if req.status == "approved":
        return
    t = req.leave_type
    rules = policy.rules_on(t, req.from_date)
    grant = rules["entitlement"]["mode"] == "grant"
    unpaid = t.kind == "lop" or rules["paid"] != "paid"
    by_year: dict[int, Decimal] = defaultdict(lambda: ZERO)
    first_day: dict[int, date] = {}
    for it in req.breakdown or []:
        if it.get("cancelled"):
            continue
        d = date.fromisoformat(it["date"])
        y = policy.year_bounds(policy.rules_on(t, d), d)[0]
        by_year[y] += D(it["amount"]) - D(it.get("lop"))
        first_day.setdefault(y, d)
    if not unpaid:
        for y, amount in by_year.items():
            if amount <= 0:
                continue
            if grant:
                ledger.add_entry(db, req.company_id, req.employee_id, t.id, y, first_day[y], "grant", amount,
                                 request_id=req.id, version_id=req.policy_version_id, idem=f"grant:{req.id}:{y}",
                                 note=f"Granted for {req.reference_no or 'request'}")
            ledger.add_entry(db, req.company_id, req.employee_id, t.id, y, first_day[y], "availed", -amount,
                             request_id=req.id, version_id=req.policy_version_id, idem=f"avail:{req.id}:{y}",
                             note=f"Leave {req.reference_no or ''}".strip())
    req.status = "approved"
    req.decided_at = utc_now()
    _history(req, "Approved", note)
    db.flush()


def _restore(db: Session, req: LeaveRequest, items: list[dict], note: str) -> None:
    t = req.leave_type
    rules = policy.rules_on(t, req.from_date)
    if t.kind == "lop" or rules["paid"] != "paid":
        return
    grant = rules["entitlement"]["mode"] == "grant"
    by_year: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for it in items:
        d = date.fromisoformat(it["date"])
        by_year[policy.year_bounds(policy.rules_on(t, d), d)[0]] += D(it["amount"]) - D(it.get("lop"))
    today = today_for_company_id(db, req.company_id)
    for y, amount in by_year.items():
        if amount <= 0:
            continue
        ledger.add_entry(db, req.company_id, req.employee_id, t.id, y, today, "restored", amount, request_id=req.id,
                         version_id=req.policy_version_id, note=note)
        if grant:
            ledger.add_entry(db, req.company_id, req.employee_id, t.id, y, today, "grant", -amount, request_id=req.id,
                             version_id=req.policy_version_id, note="Grant reduced")


def _recalc(req: LeaveRequest) -> None:
    active = [it for it in req.breakdown if not it.get("cancelled")]
    req.days = q2(sum((D(it["amount"]) for it in active), ZERO))
    req.lop_days = q2(sum((D(it.get("lop")) for it in active), ZERO))
    req.paid_days = q2(D(req.days) - D(req.lop_days))
    dates = [date.fromisoformat(it["date"]) for it in active if D(it["amount"]) > 0]
    if dates:
        req.from_date, req.to_date = min(dates), max(dates)


def cancel_days(db: Session, req: LeaveRequest, dates: list[str] | None, reason: str | None) -> list[dict]:
    """Cancel some or all approved days, put the paid part back and drop sandwich days that
    are no longer between leave days."""
    items = [dict(it) for it in req.breakdown]
    wanted = set(dates or [it["date"] for it in items])
    gone = []
    for it in items:
        if it["date"] in wanted and not it.get("cancelled") and D(it["amount"]) > 0:
            it["cancelled"] = True
            gone.append(it)
    # sandwich days need leave on both sides
    live = [i for i, it in enumerate(items) if not it.get("cancelled") and not it.get("sandwich") and D(it["amount"]) > 0]
    for i, it in enumerate(items):
        if it.get("sandwich") and not it.get("cancelled") and not it.get("adjacent"):
            if not (live and live[0] < i < live[-1]):
                it["cancelled"] = True
                gone.append(it)
    if not live:
        for it in items:
            if not it.get("cancelled") and D(it["amount"]) > 0:
                it["cancelled"] = True
                gone.append(it)
    if not gone:
        raise BadRequest("Pick at least one day that is still on leave.")
    _restore(db, req, gone, f"Cancelled {req.reference_no or ''}".strip())
    req.breakdown = items
    _recalc(req)
    if all(it.get("cancelled") or D(it["amount"]) == 0 for it in items):
        req.status = "cancelled"
        _history(req, "Cancelled", reason)
    else:
        req.status = "approved"
        _history(req, f"{days_text(sum(D(g['amount']) for g in gone))} cancelled", reason)
    req.cancel_dates = None
    db.flush()
    return gone


# ── Self-service actions ────────────────────────────────────────────

def get_request(db: Session, company_id: int, uuid) -> LeaveRequest:
    row = (db.query(LeaveRequest).options(joinedload(LeaveRequest.leave_type), joinedload(LeaveRequest.employee))
           .filter(LeaveRequest.company_id == company_id, LeaveRequest.uuid == uuid).first())
    if row is None:
        raise NotFound("Leave request")
    return row


def _is_mine(req: LeaveRequest) -> bool:
    return context.current().employee_id == req.employee_id


def modify(db: Session, company_id: int, req: LeaveRequest, data: dict, *, can_manage: bool) -> LeaveRequest:
    if req.status != "pending":
        raise BadRequest("Only a pending request can be changed. Cancel or extend approved leave instead.")
    if not (_is_mine(req) or can_manage):
        raise Forbidden("Only the person on leave can change this request.")
    if not policy.get_settings(db, company_id)["modify_pending"] and not can_manage:
        raise BadRequest("Pending requests can't be changed. Withdraw it and apply again.")
    emp = req.employee
    merged = {"from_date": req.from_date, "to_date": req.to_date, "unit": req.unit, "half": req.half,
              "hours": req.hours, "reason": req.reason, "grant_option": req.grant_option,
              "has_attachment": bool(req.attachment), **{k: v for k, v in data.items() if v is not None}}
    parent = req.parent if req.kind == "extension" else None
    ev = evaluate(db, company_id, emp, req.leave_type, merged, exclude_id=req.id, parent=parent)
    if data.get("attachment"):
        req.attachment_name, req.attachment = _decode_attachment(data)
    req.from_date, req.to_date, req.unit, req.half, req.hours = ev["from_date"], ev["to_date"], ev["unit"], ev["half"], ev["hours"]
    req.days, req.paid_days, req.lop_days = ev["days"], ev["paid_days"], ev["lop_days"]
    req.breakdown = _items_json(ev["items"])
    req.grant_option = ev["grant_option"]
    req.reason = (merged.get("reason") or "").strip() or None
    req.policy_version_id = ev["version"].id if ev["version"] else None
    _history(req, "Changed", f"{days_text(req.days)}, {_dates_text(req.from_date, req.to_date)}")
    if req.workflow_instance_id:
        from core.workflow.models import WorkflowInstance
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == req.workflow_instance_id).first()
        if inst is not None:
            inst.payload = {**(inst.payload or {}), **_payload(req, emp, req.leave_type)}
            inst.title = _title(req, emp)[:250]
    db.flush()
    return req


def _cancel_window_ok(db: Session, company_id: int, req: LeaveRequest, dates: list[date]) -> bool:
    s = policy.get_settings(db, company_id)["cancellation"]
    today = today_for_company_id(db, company_id)
    earliest = min(dates) if dates else req.from_date
    if earliest >= today:
        return True
    w = s["window"]
    if w == "upcoming":
        return False
    if w == "current_month":
        return earliest >= date(today.year, today.month, 1)
    if w == "current_year":
        return earliest >= date(today.year, 1, 1)
    return True


def cancel(db: Session, company_id: int, req: LeaveRequest, dates: list[str] | None, reason: str | None, *,
           can_manage: bool) -> LeaveRequest:
    """Withdraw a pending request, or cancel approved days (all or some). Employees go
    through approval when the settings say so; HR cancels at once."""
    from core.workflow import service as workflow
    settings = policy.get_settings(db, company_id)["cancellation"]
    mine = _is_mine(req)
    if not (mine or can_manage):
        raise Forbidden("Only the person on leave or HR can cancel this leave.")
    reason = (reason or "").strip() or None
    if settings["reason_required"] and not reason and not can_manage:
        raise BadRequest("Say why the leave is being cancelled.")
    if req.status == "pending":
        if dates:
            raise BadRequest("A pending request can only be withdrawn as a whole, or changed.")
        if req.workflow_instance_id:
            from core.workflow.models import WorkflowInstance
            inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == req.workflow_instance_id).first()
            if inst is not None and inst.status == "pending":
                if mine or context.current().actor_type == "owner":
                    workflow.cancel(db, company_id, inst, reason)
                else:
                    audit.event(db, "workflow.cancelled", "workflow_instances", inst.id, {"reason": reason, "by": "hr"})
                    workflow._finish(db, inst, "cancelled", reason)
        if req.status == "pending":
            req.status = "cancelled"
            _history(req, "Withdrawn", reason)
        db.flush()
        return req
    if req.status == "cancel_pending":
        raise BadRequest("A cancellation is already waiting for approval.")
    if req.status != "approved":
        raise BadRequest("This leave can't be cancelled any more.")
    if dates and not settings["partial"] and not can_manage:
        raise BadRequest("Cancel the whole leave; cancelling some days is switched off.")
    active = {it["date"] for it in req.breakdown if not it.get("cancelled") and D(it["amount"]) > 0}
    picked = sorted(set(dates or active))
    unknown = [d for d in picked if d not in active]
    if unknown:
        raise BadRequest(f"{date.fromisoformat(unknown[0]):%d %b %Y} is not part of this leave.")
    if not can_manage:
        if not settings["allow_employee"]:
            raise BadRequest("Ask HR to cancel approved leave.")
        if not _cancel_window_ok(db, company_id, req, [date.fromisoformat(d) for d in picked]):
            raise BadRequest("This leave is too far in the past to cancel. Ask HR.")
    if can_manage and not mine or not settings["needs_approval"]:
        cancel_days(db, req, None if set(picked) == active else picked, reason)
        audit.event(db, "leave.cancelled", "leave_requests", req.id, {"dates": picked, "reason": reason},
                    entity_uuid=str(req.uuid), entity_label=req.reference_no)
        return req
    req.status = "cancel_pending"
    req.cancel_dates = picked
    req.cancel_reason = reason
    emp = req.employee
    title = f"Cancel {req.leave_type.name.lower()}: {emp_name(emp)}, {days_text(len(picked))}" \
            if set(picked) != active else f"Cancel {req.leave_type.name.lower()}: {emp_name(emp)}, {_dates_text(req.from_date, req.to_date)}"
    inst = workflow.submit(db, company_id, "leave.cancellation",
                           {"request_id": req.id, "dates": picked, "reason": reason, "days": float(len(picked)),
                            "leave_type": req.leave_type.code, "employee_id": emp.id},
                           title, subject_type="leave_requests", subject_id=req.id)
    req.cancel_instance_id = inst.id
    if req.status == "cancel_pending":
        _history(req, "Cancellation asked", reason)
    db.flush()
    return req


def extend(db: Session, company_id: int, req: LeaveRequest, to_date: date, reason: str | None, *,
           can_manage: bool) -> LeaveRequest:
    settings = policy.get_settings(db, company_id)["extension"]
    if not settings["enabled"] and not can_manage:
        raise BadRequest("Leave extension is switched off. Apply for new leave instead.")
    if not (_is_mine(req) or can_manage):
        raise Forbidden("Only the person on leave can extend it.")
    if req.status not in ("pending", "approved"):
        raise BadRequest("Only pending or approved leave can be extended.")
    if req.unit != "day":
        raise BadRequest("Only full-day leave can be extended.")
    if settings["reason_required"] and not (reason or "").strip():
        raise BadRequest("Say why the leave is being extended.")
    # extend from the last day of the whole chain
    last = req
    for child in db.query(LeaveRequest).filter(LeaveRequest.parent_id == req.id,
                                               LeaveRequest.status.in_(("pending", "approved"))).all():
        if child.to_date > last.to_date:
            last = child
    if to_date <= last.to_date:
        raise BadRequest(f"Pick a date after {last.to_date:%d %b %Y}.")
    data = {"from_date": last.to_date + timedelta(days=1), "to_date": to_date, "unit": "day", "reason": reason,
            "grant_option": req.grant_option, "has_attachment": bool(req.attachment)}
    on_behalf = can_manage and not _is_mine(req)
    child = apply(db, company_id, req.employee, req.leave_type, data, on_behalf=on_behalf, parent=req)
    if child.status == "approved":
        _history(req, "Extended", f"Until {child.to_date:%d %b %Y}")
    return child


# ── Workflow handlers ───────────────────────────────────────────────

def _req_from_instance(db: Session, instance) -> LeaveRequest | None:
    rid = (instance.payload or {}).get("request_id")
    return (db.query(LeaveRequest).options(joinedload(LeaveRequest.leave_type))
            .filter(LeaveRequest.company_id == instance.company_id, LeaveRequest.id == rid).first())


def on_request_approved(db: Session, instance) -> None:
    req = _req_from_instance(db, instance)
    if req is None or req.status != "pending":
        return
    approve(db, req, instance.result_note)
    if req.parent_id:
        parent = db.query(LeaveRequest).filter(LeaveRequest.id == req.parent_id).first()
        if parent is not None:
            _history(parent, "Extended", f"Until {req.to_date:%d %b %Y}")


def on_request_rejected(db: Session, instance) -> None:
    req = _req_from_instance(db, instance)
    if req is not None and req.status == "pending":
        req.status = "rejected"
        req.decided_at = utc_now()
        _history(req, "Rejected", instance.result_note)


def on_request_cancelled(db: Session, instance) -> None:
    req = _req_from_instance(db, instance)
    if req is not None and req.status == "pending":
        req.status = "cancelled"
        _history(req, "Withdrawn", instance.result_note)


def on_cancellation_approved(db: Session, instance) -> None:
    req = _req_from_instance(db, instance)
    if req is None or req.status != "cancel_pending":
        return
    active = {it["date"] for it in req.breakdown if not it.get("cancelled") and D(it["amount"]) > 0}
    dates = (instance.payload or {}).get("dates") or []
    req.status = "approved"
    cancel_days(db, req, None if set(dates) >= active else dates, (instance.payload or {}).get("reason"))


def on_cancellation_closed(db: Session, instance) -> None:
    req = _req_from_instance(db, instance)
    if req is not None and req.status == "cancel_pending":
        req.status = "approved"
        req.cancel_dates = None
        _history(req, "Cancellation not approved" if instance.status == "rejected" else "Cancellation withdrawn",
                 instance.result_note)


# ── Comp off ────────────────────────────────────────────────────────

def comp_off_type(db: Session, company_id: int) -> LeaveType:
    t = db.query(LeaveType).filter(LeaveType.company_id == company_id, LeaveType.kind == "comp_off",
                                   LeaveType.active.is_(True)).order_by(LeaveType.id).first()
    if t is None:
        raise BadRequest("There is no active compensatory off leave type.")
    return t


def comp_off_expiry(db: Session, company_id: int, worked: date) -> date | None:
    s = policy.get_settings(db, company_id)["comp_off"]
    if s.get("expiry_days"):
        return worked + timedelta(days=int(s["expiry_days"]))
    return date(worked.year, 12, 31)


def request_comp_off(db: Session, company_id: int, emp, data: dict, *, source: str = "request",
                     approved: bool = False) -> CompOffRequest:
    from core.workflow import service as workflow
    s = policy.get_settings(db, company_id)["comp_off"]
    if not s["enabled"]:
        raise BadRequest("Comp off is switched off.")
    t = comp_off_type(db, company_id)
    worked = data["worked_date"]
    worked = date.fromisoformat(worked) if isinstance(worked, str) else worked
    today = today_for_company_id(db, company_id)
    if worked > today and not s["allow_future"]:
        raise BadRequest("Ask for comp off after you have worked the day.")
    if s.get("backdated_days") and (today - worked).days > int(s["backdated_days"]) and source == "request":
        raise BadRequest(f"Comp off can be asked for up to {days_text(s['backdated_days'])} after the day worked.")
    day = WorkCalendar(db, company_id).day(emp, worked)
    if day["kind"] == "weekend" and not s["on_weekly_off"]:
        raise BadRequest("Comp off isn't given for working on a weekly off.")
    if day["kind"] == "holiday" and not s["on_holiday"]:
        raise BadRequest("Comp off isn't given for working on a holiday.")
    if day["kind"] in ("working", "half"):
        raise BadRequest(f"{worked:%d %b %Y} is a working day. Comp off is for working on a weekly off or a holiday.")
    unit = data.get("unit") or "day"
    if unit not in ("day", "half_day") or (unit == "half_day" and not s["half_day"]):
        raise BadRequest("Choose a full day or a half day.")
    reason = (data.get("reason") or "").strip() or None
    if s["reason_required"] and not reason and source == "request":
        raise BadRequest("Say what you worked on.")
    dup = (db.query(CompOffRequest.id).filter(CompOffRequest.company_id == company_id, CompOffRequest.employee_id == emp.id,
                                              CompOffRequest.worked_date == worked,
                                              CompOffRequest.status.in_(("pending", "approved"))).first())
    if dup:
        raise BadRequest(f"Comp off for {worked:%d %b %Y} has already been asked for.")
    row = CompOffRequest(company_id=company_id, employee_id=emp.id, worked_date=worked, day_kind=day["kind"],
                         unit=unit, days=Decimal("1") if unit == "day" else Decimal("0.5"),
                         hours=D(data["hours"]) if data.get("hours") else None, reason=reason, source=source,
                         status="pending", expires_on=comp_off_expiry(db, company_id, worked),
                         applied_by_name=context.current().actor_name)
    db.add(row)
    db.flush()
    if approved:
        approve_comp_off(db, row)
        return row
    name = emp_name(emp)
    where = day.get("holiday") or "weekly off"
    inst = workflow.submit(db, company_id, "leave.comp_off",
                           {"comp_off_id": row.id, "employee_id": emp.id, "days": float(row.days),
                            "worked_date": worked.isoformat(), "day_kind": day["kind"], "reason": reason},
                           f"Comp off: {name}, worked {worked:%d %b %Y} ({where})",
                           subject_type="comp_off_requests", subject_id=row.id)
    row.workflow_instance_id = inst.id
    db.flush()
    return row


def approve_comp_off(db: Session, row: CompOffRequest) -> None:
    if row.status == "approved":
        return
    t = comp_off_type(db, row.company_id)
    rules = policy.rules_on(t, row.worked_date)
    year = policy.year_bounds(rules, row.worked_date)[0]
    ledger.add_entry(db, row.company_id, row.employee_id, t.id, year, row.worked_date, "comp_off", row.days,
                     expires_on=row.expires_on, idem=f"co:{row.id}",
                     note=f"For working on {row.worked_date:%d %b %Y}", meta={"comp_off_id": row.id})
    row.status = "approved"
    db.flush()


def _comp_off_from_instance(db: Session, instance) -> CompOffRequest | None:
    return db.query(CompOffRequest).filter(CompOffRequest.company_id == instance.company_id,
                                           CompOffRequest.id == (instance.payload or {}).get("comp_off_id")).first()


def on_comp_off_approved(db: Session, instance) -> None:
    row = _comp_off_from_instance(db, instance)
    if row is not None and row.status == "pending":
        approve_comp_off(db, row)


def on_comp_off_closed(db: Session, instance) -> None:
    row = _comp_off_from_instance(db, instance)
    if row is not None and row.status == "pending":
        row.status = "rejected" if instance.status == "rejected" else "cancelled"


def credit_comp_off_from_attendance(db: Session, company_id: int, employee_id: int, worked: date,
                                    hours: float | None = None, *, full_day_hours: float = 6,
                                    half_day_hours: float = 4, approved: bool = True) -> CompOffRequest | None:
    """For the attendance module: credit comp off for a weekly off or holiday worked
    (full day from `full_day_hours`, half day from `half_day_hours` when hours are given).
    With approved=False the credit goes to the employee's approvers first."""
    emp = get_employee(db, company_id, employee_id)
    unit = "day"
    if hours is not None:
        if hours < float(half_day_hours):
            return None
        unit = "day" if hours >= float(full_day_hours) else "half_day"
    try:
        with db.begin_nested():
            return request_comp_off(db, company_id, emp, {"worked_date": worked, "unit": unit, "hours": hours},
                                    source="attendance", approved=approved)
    except BadRequest:
        return None


def withdraw_comp_off(db: Session, row: CompOffRequest, reason: str) -> None:
    """Undo a comp off credited from attendance when the attendance is corrected: a pending
    one is withdrawn, an approved one is taken back from the balance."""
    if row.status == "pending":
        row.status = "cancelled"
        if row.workflow_instance_id:
            from core.workflow.models import WorkflowInstance
            inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == row.workflow_instance_id).first()
            if inst is not None and inst.status == "pending":
                inst.status = "cancelled"
                inst.result_note = reason
                inst.completed_at = utc_now()
                for t in inst.tasks:
                    if t.status == "pending":
                        t.status = "cancelled"
    elif row.status == "approved":
        from modules.leave.models import LeaveLedgerEntry
        credit = (db.query(LeaveLedgerEntry).filter(LeaveLedgerEntry.company_id == row.company_id,
                                                    LeaveLedgerEntry.idem_key == f"co:{row.id}").first())
        if credit is not None:
            # written as an expiry of that credit so the expiry run never takes it again
            ledger.add_entry(db, row.company_id, row.employee_id, credit.leave_type_id, credit.year, row.worked_date,
                             "expired", -D(credit.days), idem=f"exp:{credit.id}", note=reason[:300],
                             meta={"credit_id": credit.id, "comp_off_id": row.id, "undo": True})
        row.status = "cancelled"
    db.flush()
