"""Kudos: thank a colleague for a company value. Each kudos is also a Buzz card, shows
on the recipient's profile and counts towards the monthly / quarterly leaderboard."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import today_for_company_id, tz_for
from modules.employees.models import Employee
from modules.engagement import content, people
from modules.engagement import settings as cfg
from modules.engagement.models import BuzzPost, Kudos, KudosRecipient, KudosValue
from modules.engagement.people import Actor
from modules.notifications.service import notify

COLORS = {
    # accent, soft background
    "coral": ("#e2524f", "#fdecea"), "amber": ("#d98a06", "#fdf3dc"), "violet": ("#6f48c9", "#f0ebfb"),
    "green": ("#2c8253", "#e5f3ea"), "teal": ("#17909b", "#e2f4f5"), "indigo": ("#4f46e5", "#eceafd"),
    "rose": ("#e11d74", "#fde8f1"), "sky": ("#0284c7", "#e3f2fb"),
}

DEFAULT_VALUES = [
    ("Thank you", "Simply say thanks for making a difference.", "🙏", "coral", 5),
    ("Above and beyond", "For the extra effort that makes a big difference.", "🚀", "amber", 15),
    ("Team player", "For collaboration, positivity and support.", "🤝", "green", 10),
    ("Customer first", "For putting customers at the heart of the work.", "💛", "rose", 10),
    ("Problem solver", "For finding solutions when it matters most.", "💡", "teal", 10),
    ("Inspiring leader", "For guiding, motivating and lifting others up.", "🌟", "violet", 15),
    ("Ownership", "For taking charge and seeing things through.", "🎯", "indigo", 10),
]


def ensure_default_values(db: Session, company_id: int) -> None:
    if db.query(KudosValue.id).filter(KudosValue.company_id == company_id).first():
        return
    for i, (name, desc, emoji, color, pts) in enumerate(DEFAULT_VALUES):
        db.add(KudosValue(company_id=company_id, name=name, description=desc, emoji=emoji, color=color, points=pts, position=i))
    db.flush()


def value_dict(v: KudosValue, uses: int | None = None) -> dict:
    accent, soft = COLORS.get(v.color, COLORS["amber"])
    return {"uuid": str(v.uuid), "name": v.name, "description": v.description, "emoji": v.emoji, "color": v.color,
            "accent": accent, "soft": soft, "points": v.points, "active": v.active, "position": v.position,
            **({"uses": uses} if uses is not None else {})}


def list_values(db: Session, company_id: int, include_inactive: bool = False) -> list[dict]:
    q = db.query(KudosValue).filter(KudosValue.company_id == company_id)
    if not include_inactive:
        q = q.filter(KudosValue.active.is_(True))
    rows = q.order_by(KudosValue.position, KudosValue.id).all()
    uses = dict(db.query(Kudos.value_id, func.count(Kudos.id)).filter(Kudos.company_id == company_id)
                .group_by(Kudos.value_id).all())
    return [value_dict(v, uses.get(v.id, 0)) for v in rows]


def _clean_value(data: dict) -> dict:
    out = {}
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise BadRequest("Give the value a name.")
        out["name"] = name[:80]
    if "description" in data:
        out["description"] = (data["description"] or "").strip()[:300] or None
    if "emoji" in data:
        emoji = (data["emoji"] or "").strip()
        if not emoji or len(emoji) > 16:
            raise BadRequest("Pick an emoji for the badge.")
        out["emoji"] = emoji
    if "color" in data:
        if data["color"] not in COLORS:
            raise BadRequest("Pick one of the colours.")
        out["color"] = data["color"]
    if "points" in data:
        pts = int(data["points"] or 0)
        if not 0 <= pts <= 1000:
            raise BadRequest("Points must be between 0 and 1,000.")
        out["points"] = pts
    if "active" in data:
        out["active"] = bool(data["active"])
    return out


def create_value(db: Session, company_id: int, data: dict) -> KudosValue:
    clean = _clean_value({"emoji": "👏", "color": "amber", "points": 10, **data})
    if "name" not in clean:
        raise BadRequest("Give the value a name.")
    pos = (db.query(func.max(KudosValue.position)).filter(KudosValue.company_id == company_id).scalar() or 0) + 1
    v = KudosValue(company_id=company_id, position=pos, **clean)
    db.add(v)
    db.flush()
    return v


def get_value(db: Session, company_id: int, uuid) -> KudosValue:
    v = db.query(KudosValue).filter(KudosValue.company_id == company_id, KudosValue.uuid == uuid).first()
    if v is None:
        raise NotFound("Value")
    return v


def update_value(db: Session, v: KudosValue, data: dict) -> KudosValue:
    for k, val in _clean_value(data).items():
        setattr(v, k, val)
    return v


def reorder_values(db: Session, company_id: int, uuids: list) -> None:
    rows = {str(v.uuid): v for v in db.query(KudosValue).filter(KudosValue.company_id == company_id).all()}
    for i, u in enumerate(uuids):
        if str(u) in rows and rows[str(u)].position != i:
            rows[str(u)].position = i


# ── Giving kudos ────────────────────────────────────────────────────

def _month_start_utc(db: Session, company_id: int) -> datetime:
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == company_id).first()
    tz = tz_for(company)
    today = datetime.now(tz).date()
    return datetime.combine(today.replace(day=1), time.min, tz).astimezone(timezone.utc)


def give(db: Session, actor: Actor, data: dict) -> Kudos:
    conf = cfg.section(db, actor.company_id, "kudos")
    if not conf.get("enabled", True):
        raise BadRequest("Kudos are switched off in your company.")
    recipients = [str(u) for u in (data.get("recipients") or [])]
    if not recipients:
        raise BadRequest("Choose who you want to thank.")
    if len(recipients) > (10 if conf.get("allow_multiple_recipients", True) else 1):
        raise BadRequest("Too many people in one kudos." if conf.get("allow_multiple_recipients", True)
                         else "Give kudos to one person at a time.")
    found = people.by_uuid(db, actor.company_id, recipients)
    emps = [found[u] for u in recipients if u in found and found[u].active]
    if len(emps) != len(set(recipients)):
        raise BadRequest("One of the people was not found.")
    if actor.employee_id and any(e.id == actor.employee_id for e in emps):
        raise BadRequest("Kudos are for thanking others, not yourself.")
    value = get_value(db, actor.company_id, data.get("value")) if data.get("value") else None
    if value is not None and not value.active:
        raise BadRequest("That value is switched off.")
    message = (data.get("message") or "").strip()
    if len(message) < 3:
        raise BadRequest("Add a few words about what they did.")
    if len(message) > 1000:
        raise BadRequest("Keep the message under 1,000 characters.")
    content.check_words(message, cfg.section(db, actor.company_id, "buzz").get("blocked_words") or [])
    limit = int(conf.get("monthly_limit") or 0)
    if limit and actor.employee_id:
        given = db.query(func.count(Kudos.id)).filter(Kudos.company_id == actor.company_id,
                                                      Kudos.giver_employee_id == actor.employee_id,
                                                      Kudos.created_at >= _month_start_utc(db, actor.company_id)).scalar()
        if given >= limit:
            raise BadRequest(f"You have given your {limit} kudos for this month. More next month!")
    k = Kudos(company_id=actor.company_id, giver_employee_id=actor.employee_id, value_id=value.id if value else None,
              message=message, points=value.points if value else 0)
    k.recipients = [KudosRecipient(company_id=actor.company_id, employee_id=e.id) for e in emps]
    db.add(k)
    db.flush()
    if conf.get("post_to_buzz", True):
        db.add(BuzzPost(company_id=actor.company_id, kind="kudos", author_employee_id=actor.employee_id,
                        body=message, kudos_id=k.id, mentions=[e.id for e in emps], hashtags=content.hashtags(message),
                        theme=value.color if value else "amber",
                        subject_employee_id=emps[0].id if len(emps) == 1 else None))
        db.flush()
    label = f"{value.emoji} {value.name}" if value else "kudos"
    notify(db, actor.company_id, [e.id for e in emps], kind="kudos", title=f"{actor.name} gave you kudos: {label}",
           body=message[:200], link="/dashboard/kudos?tab=mine", actor_employee_id=actor.employee_id, actor_name=actor.name)
    return k


def card(db: Session, company_id: int, k: Kudos, emps: dict | None = None) -> dict:
    ids = [r.employee_id for r in k.recipients] + [k.giver_employee_id]
    emps = emps if emps is not None else people.load(db, company_id, ids)
    giver = emps.get(k.giver_employee_id)
    return {
        "uuid": str(k.uuid), "message": k.message, "points": k.points, "created_at": k.created_at,
        "value": value_dict(k.value) if k.value else None,
        "giver": people.card(giver, owner=None if k.giver_employee_id else people.owner_name(db, company_id)),
        "recipients": [people.card(emps[r.employee_id]) for r in k.recipients if r.employee_id in emps],
    }


def list_kudos(db: Session, company_id: int, *, employee_id: int | None = None, given_by: int | None = None,
               value_uuid=None, page: int = 1, size: int = 20) -> dict:
    q = db.query(Kudos).options(joinedload(Kudos.value), joinedload(Kudos.recipients)).filter(Kudos.company_id == company_id)
    if employee_id:
        q = q.filter(Kudos.id.in_(db.query(KudosRecipient.kudos_id).filter(KudosRecipient.employee_id == employee_id)))
    if given_by:
        q = q.filter(Kudos.giver_employee_id == given_by)
    if value_uuid:
        q = q.join(KudosValue, KudosValue.id == Kudos.value_id).filter(KudosValue.uuid == value_uuid)
    total = q.order_by(None).count()
    rows = q.order_by(Kudos.created_at.desc()).offset((page - 1) * size).limit(size).all()
    ids = {r.employee_id for k in rows for r in k.recipients} | {k.giver_employee_id for k in rows}
    emps = people.load(db, company_id, ids)
    return {"items": [card(db, company_id, k, emps) for k in rows], "total": total, "page": page, "size": size}


def period_bounds(db: Session, company_id: int, period: str, offset: int = 0) -> tuple[date, date, str]:
    """Start (inclusive) and end (exclusive) dates of this month or quarter, in the
    company's own calendar, and a label."""
    today = today_for_company_id(db, company_id)
    if period == "quarter":
        # Indian financial-year quarters: Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        y, m = today.year, q_start_month - 3 * offset
        while m < 1:
            m += 12
            y -= 1
        start = date(y, m, 1)
        end_m, end_y = (m + 3, y) if m + 3 <= 12 else (m + 3 - 12, y + 1)
        end = date(end_y, end_m, 1)
        fy_q = {4: 1, 7: 2, 10: 3, 1: 4}[m]
        fy = y if m >= 4 else y - 1
        label = f"Q{fy_q} FY{str(fy)[2:]}-{str(fy + 1)[2:]}"
    else:
        y, m = today.year, today.month - offset
        while m < 1:
            m += 12
            y -= 1
        start = date(y, m, 1)
        end = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        label = start.strftime("%B %Y")
    return start, end, label


def leaderboard(db: Session, company_id: int, period: str = "month", offset: int = 0, limit: int = 10) -> dict:
    from modules.company.models import Company
    start, end, label = period_bounds(db, company_id, period, offset)
    tz = tz_for(db.query(Company).filter(Company.id == company_id).first())
    s_utc = datetime.combine(start, time.min, tz).astimezone(timezone.utc)
    e_utc = datetime.combine(end, time.min, tz).astimezone(timezone.utc)
    rows = (db.query(KudosRecipient.employee_id, func.count(KudosRecipient.id), func.coalesce(func.sum(Kudos.points), 0))
            .join(Kudos, Kudos.id == KudosRecipient.kudos_id)
            .join(Employee, Employee.id == KudosRecipient.employee_id)
            .filter(KudosRecipient.company_id == company_id, Kudos.created_at >= s_utc, Kudos.created_at < e_utc,
                    Employee.active.is_(True))
            .group_by(KudosRecipient.employee_id)
            .order_by(func.coalesce(func.sum(Kudos.points), 0).desc(), func.count(KudosRecipient.id).desc())
            .limit(limit).all())
    givers = (db.query(Kudos.giver_employee_id, func.count(Kudos.id))
              .filter(Kudos.company_id == company_id, Kudos.created_at >= s_utc, Kudos.created_at < e_utc,
                      Kudos.giver_employee_id.isnot(None))
              .group_by(Kudos.giver_employee_id).order_by(func.count(Kudos.id).desc()).limit(5).all())
    emps = people.load(db, company_id, [r[0] for r in rows] + [g[0] for g in givers])
    total = db.query(func.count(Kudos.id)).filter(Kudos.company_id == company_id, Kudos.created_at >= s_utc,
                                                  Kudos.created_at < e_utc).scalar()
    top_values = (db.query(KudosValue, func.count(Kudos.id)).join(Kudos, Kudos.value_id == KudosValue.id)
                  .filter(Kudos.company_id == company_id, Kudos.created_at >= s_utc, Kudos.created_at < e_utc)
                  .group_by(KudosValue.id).order_by(func.count(Kudos.id).desc()).limit(5).all())
    return {
        "period": period, "label": label, "start": start, "end": end, "total_kudos": total,
        "leaders": [{"rank": i + 1, "person": people.card(emps[eid]), "kudos": n, "points": int(pts)}
                    for i, (eid, n, pts) in enumerate(rows) if eid in emps],
        "top_givers": [{"person": people.card(emps[eid]), "given": n} for eid, n in givers if eid in emps],
        "top_values": [{**value_dict(v), "count": n} for v, n in top_values],
    }


def profile_summary(db: Session, company_id: int, employee_id: int) -> dict:
    received = (db.query(KudosValue, func.count(Kudos.id))
                .join(Kudos, Kudos.value_id == KudosValue.id)
                .join(KudosRecipient, KudosRecipient.kudos_id == Kudos.id)
                .filter(KudosRecipient.company_id == company_id, KudosRecipient.employee_id == employee_id)
                .group_by(KudosValue.id).order_by(func.count(Kudos.id).desc()).all())
    total, points = db.query(func.count(Kudos.id), func.coalesce(func.sum(Kudos.points), 0)).join(
        KudosRecipient, KudosRecipient.kudos_id == Kudos.id).filter(
        KudosRecipient.company_id == company_id, KudosRecipient.employee_id == employee_id).one()
    given = db.query(func.count(Kudos.id)).filter(Kudos.company_id == company_id, Kudos.giver_employee_id == employee_id).scalar()
    recent = list_kudos(db, company_id, employee_id=employee_id, size=5)["items"]
    return {"received": total, "points": int(points), "given": given,
            "badges": [{**value_dict(v), "count": n} for v, n in received], "recent": recent}


def check_can_manage(actor: Actor, perms: list[str]) -> None:
    if not (actor.is_owner or actor.role == "admin" or "engage.settings.manage" in perms):
        raise Forbidden()
