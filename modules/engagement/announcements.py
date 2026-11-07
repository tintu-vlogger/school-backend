"""Announcements and news: rich text with a cover, sent to chosen groups, published now or
at a set time, optionally expiring, pinned to the top of Buzz, and (when required)
acknowledged by every reader."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core import audit
from core.email import send_email
from core.errors import BadRequest, NotFound
from modules.employees.models import Employee
from modules.engagement import audience as aud
from modules.engagement import content, people
from modules.engagement.models import Announcement, AnnouncementReceipt, BuzzPost
from modules.engagement.people import Actor
from modules.notifications.service import notify

KINDS = {"announcement": "Announcement", "news": "News", "policy": "Policy update", "event": "Event"}
COVER_PRESETS = {"aurora", "sunrise", "ocean", "meadow", "lavender", "ember", "slate", "festive", "monsoon", "saffron"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def effective_status(a: Announcement) -> str:
    if a.status == "published" and a.expires_at and a.expires_at <= now():
        return "expired"
    return a.status


def _cover(db: Session, company_id: int, cover) -> dict | None:
    if not cover:
        return None
    if cover.get("preset"):
        if cover["preset"] not in COVER_PRESETS:
            raise BadRequest("Pick one of the cover designs.")
        return {"preset": cover["preset"]}
    if cover.get("file"):
        f = content.files_for(db, company_id, [cover["file"]])[0]
        if f["kind"] != "image":
            raise BadRequest("The cover must be an image.")
        return {"file": f["uuid"]}
    return None


def cover_out(cover: dict | None) -> dict | None:
    if not cover:
        return None
    if cover.get("file"):
        return {"file": cover["file"], "url": content.file_url(cover["file"]), "thumb_url": content.file_url(cover["file"], True)}
    return cover


def get(db: Session, company_id: int, uuid) -> Announcement:
    a = db.query(Announcement).filter(Announcement.company_id == company_id, Announcement.uuid == uuid).first()
    if a is None:
        raise NotFound("Announcement")
    return a


def _apply(db: Session, a: Announcement, data: dict, actor: Actor) -> None:
    if "title" in data:
        title = (data["title"] or "").strip()
        if not title:
            raise BadRequest("Give the announcement a title.")
        a.title = title[:200]
    if "kind" in data:
        if data["kind"] not in KINDS:
            raise BadRequest("Pick a type.")
        a.kind = data["kind"]
    if "body_html" in data:
        a.body_html = content.safe_html(data["body_html"])[:100_000]
    if "summary" in data:
        a.summary = (data["summary"] or "").strip()[:400] or None
    if "cover" in data:
        a.cover = _cover(db, actor.company_id, data["cover"])
    if "audience" in data:
        a.audience = aud.normalize(data["audience"])
    for key in ("requires_ack", "pinned", "show_on_kiosk", "allow_comments", "send_email"):
        if key in data and data[key] is not None:
            setattr(a, key, bool(data[key]))
    if "ack_due_on" in data:
        a.ack_due_on = data["ack_due_on"]
    if "publish_at" in data:
        a.publish_at = data["publish_at"]
    if "expires_at" in data:
        a.expires_at = data["expires_at"]
    if a.expires_at and a.publish_at and a.expires_at <= a.publish_at:
        raise BadRequest("The end date must be after the publish date.")
    if not content.html_text(a.body_html, 10) and not a.summary:
        raise BadRequest("Write the announcement first.")


def create(db: Session, actor: Actor, data: dict) -> Announcement:
    a = Announcement(company_id=actor.company_id, author_employee_id=actor.employee_id, author_name=actor.name,
                     audience={"all": True}, body_html="")
    _apply(db, a, {"kind": "announcement", **data}, actor)
    db.add(a)
    db.flush()
    _after_save(db, a, actor, data.get("action") or "draft")
    return a


def update(db: Session, actor: Actor, a: Announcement, data: dict) -> Announcement:
    if a.status == "archived":
        raise BadRequest("Archived announcements can't be changed.")
    _apply(db, a, data, actor)
    if a.status == "published":
        post = db.query(BuzzPost).filter(BuzzPost.company_id == a.company_id, BuzzPost.announcement_id == a.id,
                                         BuzzPost.deleted_at.is_(None)).first()
        if post is not None:
            post.audience = a.audience
            post.allow_comments = a.allow_comments
            post.body = a.summary or content.html_text(a.body_html, 280)
            if not a.pinned and post.pinned_at:
                post.pinned_at = None
            elif a.pinned and not post.pinned_at and effective_status(a) == "published":
                post.pinned_at = now()
    _after_save(db, a, actor, data.get("action"))
    return a


def _after_save(db: Session, a: Announcement, actor: Actor, action: str | None) -> None:
    if action == "publish":
        if a.publish_at and a.publish_at > now() + timedelta(minutes=1):
            a.status = "scheduled"
        else:
            publish(db, a, actor)
    elif action == "draft" and a.status != "published":
        a.status = "draft"


def publish(db: Session, a: Announcement, actor: Actor | None = None) -> None:
    """Make it live: Buzz card (pinned when asked), in-app notifications, optional email."""
    if a.status == "published":
        return
    a.status, a.published_at = "published", now()
    if a.publish_at is None or a.publish_at > a.published_at:
        a.publish_at = a.published_at
    post = BuzzPost(company_id=a.company_id, kind="announcement", author_employee_id=a.author_employee_id,
                    body=a.summary or content.html_text(a.body_html, 280), announcement_id=a.id, audience=a.audience,
                    allow_comments=a.allow_comments, pinned_at=now() if a.pinned else None,
                    pinned_by_name=a.author_name if a.pinned else None)
    db.add(post)
    db.flush()
    if a.notified_at is None:
        a.notified_at = now()
        ids = aud.resolve(db, a.company_id, a.audience)
        title = f"{KINDS.get(a.kind, 'Announcement')}: {a.title}"
        notify(db, a.company_id, ids, kind="announcement", title=title[:200],
               body=("Please read and acknowledge." if a.requires_ack else (a.summary or content.html_text(a.body_html, 160))),
               link=f"/dashboard/announcements?view={a.uuid}", actor_employee_id=a.author_employee_id,
               actor_name=a.author_name, skip={a.author_employee_id} if a.author_employee_id else None)
        if a.send_email:
            _email_audience(db, a, ids)
    audit.event(db, "announcement.published", "announcements", a.id, {"audience": aud.describe(db, a.company_id, a.audience)},
                entity_uuid=str(a.uuid), entity_label=a.title)


def _email_audience(db: Session, a: Announcement, ids: list[int]) -> None:
    from core.config import get_settings
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == a.company_id).first()
    link = f"{get_settings().FRONTEND_URL.rstrip('/')}/dashboard/announcements?view={a.uuid}"
    text = content.html_text(a.body_html, 4000)
    for (email,) in db.query(Employee.email).filter(Employee.company_id == a.company_id, Employee.id.in_(ids)).all():
        send_email(email, f"{company.company_name if company else ''}: {a.title}",
                   f"{text}\n\n{'Please acknowledge that you have read this: ' if a.requires_ack else 'Read it online: '}{link}")


def archive(db: Session, a: Announcement) -> None:
    a.status = "archived"
    db.query(BuzzPost).filter(BuzzPost.company_id == a.company_id, BuzzPost.announcement_id == a.id).update(
        {"pinned_at": None, "deleted_at": now()}, synchronize_session=False)
    audit.event(db, "announcement.archived", "announcements", a.id, {}, entity_uuid=str(a.uuid), entity_label=a.title)


def delete_draft(db: Session, a: Announcement) -> None:
    if a.status not in ("draft", "scheduled"):
        raise BadRequest("Published announcements are archived, not deleted.")
    audit.event(db, "announcement.draft_deleted", "announcements", a.id, {"title": a.title}, entity_uuid=str(a.uuid),
                entity_label=a.title)
    db.delete(a)


def run_due(db: Session, company_id: int) -> int:
    """Publish scheduled announcements whose time has come and unpin expired ones."""
    n = 0
    for a in db.query(Announcement).filter(Announcement.company_id == company_id, Announcement.status == "scheduled",
                                           Announcement.publish_at <= now()).all():
        publish(db, a, None)
        n += 1
    expired = db.query(Announcement.id).filter(Announcement.company_id == company_id, Announcement.status == "published",
                                               Announcement.expires_at <= now())
    db.query(BuzzPost).filter(BuzzPost.company_id == company_id, BuzzPost.announcement_id.in_(expired),
                              BuzzPost.pinned_at.isnot(None)).update({"pinned_at": None}, synchronize_session=False)
    return n


def remind_ack(db: Session, company_id: int, after_days: int) -> int:
    """Remind people once when they have not acknowledged after `after_days`."""
    if not after_days:
        return 0
    cutoff = now() - timedelta(days=after_days)
    n = 0
    for a in db.query(Announcement).filter(Announcement.company_id == company_id, Announcement.status == "published",
                                           Announcement.requires_ack.is_(True), Announcement.published_at <= cutoff,
                                           or_(Announcement.expires_at.is_(None), Announcement.expires_at > now())).all():
        done = {r.employee_id: r for r in db.query(AnnouncementReceipt).filter(AnnouncementReceipt.announcement_id == a.id).all()}
        due = [i for i in aud.resolve(db, company_id, a.audience)
               if not (done.get(i) and (done[i].acknowledged_at or done[i].reminded_at))]
        for i in due:
            r = done.get(i) or AnnouncementReceipt(company_id=company_id, announcement_id=a.id, employee_id=i)
            r.reminded_at = now()
            db.add(r)
        if due:
            notify(db, company_id, due, kind="announcement", title=f"Please acknowledge: {a.title}"[:200],
                   body="This announcement needs your acknowledgement.", link=f"/dashboard/announcements?view={a.uuid}")
            n += len(due)
    return n


# ── Reading ─────────────────────────────────────────────────────────

def card(a: Announcement, receipt: AnnouncementReceipt | None, actor: Actor) -> dict:
    return {
        "uuid": str(a.uuid), "kind": a.kind, "kind_label": KINDS.get(a.kind, a.kind), "title": a.title,
        "summary": a.summary or content.html_text(a.body_html, 220), "cover": cover_out(a.cover),
        "requires_ack": a.requires_ack, "ack_due_on": a.ack_due_on, "published_at": a.published_at,
        "expires_at": a.expires_at, "status": effective_status(a), "author_name": a.author_name,
        "read": bool(receipt and receipt.read_at), "acknowledged": bool(receipt and receipt.acknowledged_at),
        "acknowledged_at": receipt.acknowledged_at if receipt else None,
    }


def _receipt(db: Session, a: Announcement, employee_id: int) -> AnnouncementReceipt | None:
    return db.query(AnnouncementReceipt).filter(AnnouncementReceipt.company_id == a.company_id,
                                                AnnouncementReceipt.announcement_id == a.id,
                                                AnnouncementReceipt.employee_id == employee_id).first()


def for_reader(db: Session, actor: Actor, a: Announcement, manager: bool) -> Announcement:
    if not manager and (effective_status(a) not in ("published", "expired")
                        or not aud.includes(db, actor.company_id, a.audience, actor.viewer_id)):
        raise NotFound("Announcement")
    return a


def detail(db: Session, actor: Actor, a: Announcement, manager: bool) -> dict:
    receipt = _receipt(db, a, actor.employee_id) if actor.employee_id else None
    if actor.employee_id and effective_status(a) == "published":
        if receipt is None:
            receipt = AnnouncementReceipt(company_id=a.company_id, announcement_id=a.id, employee_id=actor.employee_id)
            db.add(receipt)
        if receipt.read_at is None:
            receipt.read_at = now()
        db.flush()
    out = {**card(a, receipt, actor), "body_html": a.body_html, "audience": a.audience,
           "audience_label": aud.describe(db, a.company_id, a.audience), "pinned": a.pinned,
           "show_on_kiosk": a.show_on_kiosk, "allow_comments": a.allow_comments, "send_email": a.send_email,
           "publish_at": a.publish_at, "can_manage": manager}
    post = db.query(BuzzPost.uuid).filter(BuzzPost.company_id == a.company_id, BuzzPost.announcement_id == a.id,
                                          BuzzPost.deleted_at.is_(None)).first()
    out["post_uuid"] = str(post[0]) if post else None
    if manager:
        out["stats"] = stats(db, a)
    return out


def acknowledge(db: Session, actor: Actor, a: Announcement) -> AnnouncementReceipt:
    if not actor.employee_id:
        raise BadRequest("Only employees acknowledge announcements.")
    if not a.requires_ack:
        raise BadRequest("This announcement does not need an acknowledgement.")
    if effective_status(a) != "published":
        raise BadRequest("This announcement is no longer active.")
    r = _receipt(db, a, actor.employee_id) or AnnouncementReceipt(company_id=a.company_id, announcement_id=a.id,
                                                                  employee_id=actor.employee_id)
    r.read_at = r.read_at or now()
    if r.acknowledged_at is None:
        r.acknowledged_at = now()
        audit.event(db, "announcement.acknowledged", "announcements", a.id, {}, entity_uuid=str(a.uuid), entity_label=a.title)
    db.add(r)
    db.flush()
    return r


def stats(db: Session, a: Announcement) -> dict:
    ids = aud.resolve(db, a.company_id, a.audience) if a.status in ("published", "archived") else aud.resolve(db, a.company_id, a.audience)
    read, acked = db.query(func.count(AnnouncementReceipt.read_at), func.count(AnnouncementReceipt.acknowledged_at)).filter(
        AnnouncementReceipt.announcement_id == a.id, AnnouncementReceipt.employee_id.in_(ids or [0])).one()
    return {"audience": len(ids), "read": read, "acknowledged": acked}


def receipts(db: Session, a: Announcement, status: str | None, q: str | None, page: int, size: int) -> dict:
    ids = aud.resolve(db, a.company_id, a.audience)
    rec = {r.employee_id: r for r in db.query(AnnouncementReceipt).filter(AnnouncementReceipt.announcement_id == a.id).all()}
    rows = people.load(db, a.company_id, ids)
    out = []
    for eid in ids:
        e = rows.get(eid)
        if e is None:
            continue
        r = rec.get(eid)
        st = "acknowledged" if r and r.acknowledged_at else "read" if r and r.read_at else "unread"
        if status and status != st and not (status == "pending" and st != "acknowledged"):
            continue
        if q and q.lower() not in people.full_name(e).lower():
            continue
        out.append({"person": people.card(e), "status": st, "read_at": r.read_at if r else None,
                    "acknowledged_at": r.acknowledged_at if r else None})
    out.sort(key=lambda x: ({"unread": 0, "read": 1, "acknowledged": 2}[x["status"]], x["person"]["name"]))
    total = len(out)
    return {"items": out[(page - 1) * size: page * size], "total": total, "page": page, "size": size}


def list_admin(db: Session, company_id: int, status: str | None, q: str | None, page: int, size: int) -> dict:
    query = db.query(Announcement).filter(Announcement.company_id == company_id)
    if status == "expired":
        query = query.filter(Announcement.status == "published", Announcement.expires_at <= now())
    elif status == "published":
        query = query.filter(Announcement.status == "published",
                             or_(Announcement.expires_at.is_(None), Announcement.expires_at > now()))
    elif status:
        query = query.filter(Announcement.status == status)
    if q:
        query = query.filter(Announcement.title.ilike(f"%{q}%"))
    total = query.count()
    rows = query.order_by(func.coalesce(Announcement.publish_at, Announcement.created_at).desc()).offset((page - 1) * size).limit(size).all()
    items = []
    for a in rows:
        st = stats(db, a) if a.status in ("published", "archived") else None
        items.append({**card(a, None, None), "audience_label": aud.describe(db, company_id, a.audience),
                      "publish_at": a.publish_at, "pinned": a.pinned, "stats": st, "created_at": a.created_at})
    return {"items": items, "total": total, "page": page, "size": size}


def list_for_reader(db: Session, actor: Actor, scope: str, page: int, size: int) -> dict:
    dims = aud.viewer_dims(db, actor.company_id, actor.viewer_id)
    query = db.query(Announcement).filter(Announcement.company_id == actor.company_id, Announcement.status == "published",
                                          aud.sql_condition(Announcement.audience, dims))
    if scope == "past":
        query = query.filter(Announcement.expires_at <= now())
    else:
        query = query.filter(or_(Announcement.expires_at.is_(None), Announcement.expires_at > now()))
    if scope == "pending" and actor.employee_id:
        acked = db.query(AnnouncementReceipt.announcement_id).filter(
            AnnouncementReceipt.employee_id == actor.employee_id, AnnouncementReceipt.acknowledged_at.isnot(None))
        query = query.filter(Announcement.requires_ack.is_(True), Announcement.id.notin_(acked))
    total = query.count()
    rows = query.order_by(Announcement.published_at.desc()).offset((page - 1) * size).limit(size).all()
    rec = {r.announcement_id: r for r in db.query(AnnouncementReceipt).filter(
        AnnouncementReceipt.company_id == actor.company_id, AnnouncementReceipt.employee_id == actor.employee_id,
        AnnouncementReceipt.announcement_id.in_([a.id for a in rows] or [0])).all()} if actor.employee_id else {}
    return {"items": [card(a, rec.get(a.id), actor) for a in rows], "total": total, "page": page, "size": size}


def pending_ack_count(db: Session, actor: Actor) -> int:
    if not actor.employee_id:
        return 0
    return list_for_reader(db, actor, "pending", 1, 1)["total"]
