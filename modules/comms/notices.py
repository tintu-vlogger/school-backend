"""Notices and circulars: drafts, scheduling, publishing to an audience, delivery by every
channel, read receipts, acknowledgements and resending to people who have not read yet."""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, insert, or_
from sqlalchemy.orm import Session

from core import audit
from modules.comms import channels, service
from modules.comms.models import Notice, NoticeRecipient
from modules.employees.models import Employee
from modules.students.models import Guardian, Student


def _excerpt(md: str, n: int = 160) -> str:
    import re
    text = re.sub(r"[#*_>`\[\]()!-]+", " ", md or "")
    text = " ".join(text.split())
    return text[:n] + ("…" if len(text) > n else "")


def notice_out(n: Notice, extra: dict | None = None, attachments: list | None = None) -> dict:
    out = {
        "id": n.id, "uuid": str(n.uuid), "title": n.title, "body": n.body, "excerpt": _excerpt(n.body),
        "category": n.category, "category_label": service.NOTICE_CATEGORIES.get(n.category, n.category.title()),
        "attachments": attachments if attachments is not None else (n.attachments or []),
        "audience": service.clean_audience(n.audience), "audience_label": n.audience_label,
        "channels": n.channels or ["in_app"], "status": n.status, "publish_at": n.publish_at,
        "published_at": n.published_at, "expires_at": n.expires_at, "pinned": n.pinned, "require_ack": n.require_ack,
        "author_employee_id": n.author_employee_id, "author_name": n.author_name, "recipient_count": n.recipient_count,
        "delivery": n.delivery or {}, "created_at": n.created_at, "updated_at": n.updated_at,
        "expired": bool(n.expires_at and n.expires_at <= service.now()),
    }
    if extra:
        out.update(extra)
    return out


def stats_for(db: Session, notice_ids: list[int]) -> dict[int, dict]:
    if not notice_ids:
        return {}
    rows = db.query(NoticeRecipient.notice_id, func.count(NoticeRecipient.id),
                    func.count(NoticeRecipient.read_at), func.count(NoticeRecipient.acknowledged_at)).filter(
        NoticeRecipient.notice_id.in_(notice_ids)).group_by(NoticeRecipient.notice_id).all()
    return {nid: {"recipients": total, "read": read, "acknowledged": ack} for nid, total, read, ack in rows}


def apply_fields(db: Session, company_id: int, n: Notice, data: dict) -> None:
    from modules.documents import storage
    for key in ("title", "body", "category", "pinned", "require_ack", "publish_at", "expires_at"):
        if key in data:
            setattr(n, key, data[key])
    if n.category not in service.NOTICE_CATEGORIES:
        raise HTTPException(status_code=400, detail="Pick a category from the list.")
    if "attachments" in data:
        n.attachments = storage.attachments_from(db, company_id, data["attachments"] or [], limit=10)
    if "audience" in data:
        n.audience = service.clean_audience(data["audience"])
        n.audience_label = service.audience_label(db, company_id, n.audience)
    if "channels" in data:
        chans = [c for c in (data["channels"] or []) if c in service.CHANNELS]
        n.channels = ["in_app"] + [c for c in chans if c != "in_app"]
    if n.expires_at and n.publish_at and n.expires_at <= n.publish_at:
        raise HTTPException(status_code=400, detail="The expiry must be after the publish time.")


def publish(db: Session, n: Notice) -> int:
    """Send a notice to its audience now. Returns how many people it reached."""
    if service.audience_empty(n.audience):
        raise HTTPException(status_code=400, detail="Choose who should get this notice.")
    cid = n.company_id
    res = service.resolve_audience(db, cid, n.audience)
    if res.count == 0:
        raise HTTPException(status_code=400, detail="No one matches this audience yet.")
    portal = service.portal_ids_for(db, cid, res.guardians.keys(), res.students)
    names_g = {g.id: g.full_name for g in db.query(Guardian).filter(Guardian.id.in_(list(res.guardians) or [-1])).all()}
    names_s = {s.id: s.full_name for s in db.query(Student).filter(Student.id.in_(list(res.students) or [-1])).all()}
    existing = {k for (k,) in db.query(NoticeRecipient.recipient_key).filter(NoticeRecipient.notice_id == n.id).all()}
    now = service.now()
    rows = []
    for eid, e in res.staff.items():
        rows.append({"recipient_key": f"e:{eid}", "kind": "staff", "employee_id": eid, "name": service.employee_name(e)})
    for gid, via in res.guardians.items():
        rows.append({"recipient_key": f"g:{gid}", "kind": "parent", "guardian_id": gid, "via_student_ids": sorted(via),
                     "portal_account_id": portal.get(f"g:{gid}"), "name": names_g.get(gid)})
    for sid in res.students:
        rows.append({"recipient_key": f"s:{sid}", "kind": "student", "student_id": sid, "via_student_ids": [sid],
                     "portal_account_id": portal.get(f"s:{sid}"), "name": names_s.get(sid)})
    rows = [r for r in rows if r["recipient_key"] not in existing]
    for r in rows:
        r.update(company_id=cid, notice_id=n.id, channels_sent=list(n.channels or ["in_app"]), last_sent_at=now,
                 send_count=1)
        r.setdefault("via_student_ids", [])
    if rows:
        db.execute(insert(NoticeRecipient), rows)
    n.status = "published"
    n.published_at = n.published_at or now
    n.publish_at = n.publish_at or now
    n.recipient_count = len(existing) + len(rows)
    db.flush()
    _deliver(db, n, [r["recipient_key"] for r in rows])
    audit.event(db, "comms.notice_published", "comms_notices", n.id,
                {"recipients": n.recipient_count, "channels": n.channels}, company_id=cid,
                entity_uuid=str(n.uuid), entity_label=n.title)
    return n.recipient_count


def _deliver(db: Session, n: Notice, keys: list[str], resend: bool = False) -> None:
    """In-app to everyone with a sign-in, plus the notice's other channels."""
    from modules.notifications.service import notify, notify_portal
    if not keys:
        return
    cid = n.company_id
    recips = db.query(NoticeRecipient).filter(NoticeRecipient.notice_id == n.id,
                                              NoticeRecipient.recipient_key.in_(keys)).all()
    prefix = "Reminder: " if resend else ("Urgent: " if n.category == "emergency" else "")
    title = f"{prefix}{n.title}"
    body = _excerpt(n.body, 240)
    staff_ids = [r.employee_id for r in recips if r.employee_id]
    if staff_ids:
        notify(db, cid, staff_ids, kind="comms_notice", title=title, body=body,
               link=f"/dashboard/comms/notices?view={n.uuid}", actor_name=n.author_name,
               skip={n.author_employee_id} if n.author_employee_id and not resend else None)
    portal_ids = [r.portal_account_id for r in recips if r.portal_account_id]
    if portal_ids:
        notify_portal(db, cid, portal_ids, kind="comms_notice", title=title, body=body,
                      link=f"/dashboard/my/notices?view={n.uuid}", actor_name=n.author_name)
    chans = set(n.channels or [])
    if n.category == "emergency" and not resend:
        chans |= set(service.get_settings(db, cid).get("emergency_channels") or [])
    delivery = dict(n.delivery or {})
    if chans & {"email", "sms", "whatsapp"}:
        emp = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(staff_ids or [-1])).all()}
        gids = [r.guardian_id for r in recips if r.guardian_id]
        sids = [r.student_id for r in recips if r.student_id]
        gs = {g.id: g for g in db.query(Guardian).filter(Guardian.id.in_(gids or [-1])).all()}
        ss = {s.id: s for s in db.query(Student).filter(Student.id.in_(sids or [-1])).all()}

        def contact(r: NoticeRecipient, what: str) -> str | None:
            obj = emp.get(r.employee_id) if r.employee_id else gs.get(r.guardian_id) if r.guardian_id else ss.get(r.student_id)
            return getattr(obj, what, None) if obj is not None else None

        from modules.company.models import Company
        org = db.query(Company.company_name).filter(Company.id == cid).scalar() or ""
        if "email" in chans:
            text = f"{n.title}\n\n{n.body}\n\n— {n.author_name or org}\nOpen it in the app to see attachments" + \
                   (" and acknowledge it." if n.require_ack else ".")
            r = channels.send_email(db, cid, [(contact(x, "email"), x.recipient_key) for x in recips],
                                    f"{prefix}{n.title}"[:200], text)
            delivery["email"] = _add(delivery.get("email"), r)
        short = f"{org}: {prefix}{n.title}. {_excerpt(n.body, 200)}"
        for kind in ("sms", "whatsapp"):
            if kind in chans:
                r = channels.send_text(db, cid, kind, [(contact(x, "phone"), x.recipient_key) for x in recips], short)
                delivery[kind] = _add(delivery.get(kind), r)
    delivery["in_app"] = _add(delivery.get("in_app"), {"sent": len(staff_ids) + len(portal_ids),
                                                        "skipped": len(recips) - len(staff_ids) - len(portal_ids)})
    n.delivery = delivery
    db.flush()


def _add(old: dict | None, new: dict) -> dict:
    out = dict(old or {})
    for k, v in new.items():
        if isinstance(v, int):
            out[k] = out.get(k, 0) + v
        else:
            out[k] = v
    return out


def resend_unread(db: Session, n: Notice) -> int:
    if n.status != "published":
        raise HTTPException(status_code=400, detail="Only published notices can be sent again.")
    q = db.query(NoticeRecipient).filter(NoticeRecipient.notice_id == n.id)
    q = q.filter(NoticeRecipient.acknowledged_at.is_(None)) if n.require_ack else q.filter(NoticeRecipient.read_at.is_(None))
    rows = q.all()
    now = service.now()
    for r in rows:
        r.send_count = (r.send_count or 1) + 1
        r.last_sent_at = now
    db.flush()
    _deliver(db, n, [r.recipient_key for r in rows], resend=True)
    audit.event(db, "comms.notice_resent", "comms_notices", n.id, {"recipients": len(rows)},
                entity_uuid=str(n.uuid), entity_label=n.title)
    return len(rows)


def publish_due(db: Session) -> int:
    """Background: publish scheduled notices whose time has come (all companies)."""
    due = db.query(Notice).filter(Notice.status == "scheduled", Notice.publish_at <= service.now(),
                                  Notice.archived_at.is_(None)).limit(50).all()
    done = 0
    for n in due:
        db.info["company_id"] = n.company_id
        try:
            with db.begin_nested():
                publish(db, n)
                done += 1
        except HTTPException as exc:
            n.status = "draft"
            n.delivery = {**(n.delivery or {}), "error": exc.detail}
    db.info.pop("company_id", None)
    return done


# ── Reading ──────────────────────────────────────────────────────────

def live_filter(q):
    now = service.now()
    return q.filter(Notice.status == "published", Notice.archived_at.is_(None),
                    or_(Notice.expires_at.is_(None), Notice.expires_at > now))


def received_query(db: Session, company_id: int, key: str, include_expired: bool = False):
    q = (db.query(Notice, NoticeRecipient).join(NoticeRecipient, NoticeRecipient.notice_id == Notice.id)
         .filter(Notice.company_id == company_id, NoticeRecipient.recipient_key == key))
    if include_expired:
        q = q.filter(Notice.status == "published", Notice.archived_at.is_(None))
    else:
        q = live_filter(q)
    return q


def receipt_fields(r: NoticeRecipient | None) -> dict:
    return {"read_at": r.read_at if r else None, "acknowledged_at": r.acknowledged_at if r else None,
            "via_student_ids": (r.via_student_ids or []) if r else []}


def mark_read(db: Session, company_id: int, notice: Notice, key: str) -> NoticeRecipient:
    r = db.query(NoticeRecipient).filter(NoticeRecipient.company_id == company_id, NoticeRecipient.notice_id == notice.id,
                                         NoticeRecipient.recipient_key == key).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Notice not found")
    if r.read_at is None:
        r.read_at = service.now()
    db.flush()
    return r


def acknowledge(db: Session, company_id: int, notice: Notice, key: str) -> NoticeRecipient:
    r = mark_read(db, company_id, notice, key)
    if not notice.require_ack:
        raise HTTPException(status_code=400, detail="This notice does not need an acknowledgement.")
    if r.acknowledged_at is None:
        r.acknowledged_at = service.now()
        audit.event(db, "comms.notice_acknowledged", "comms_notices", notice.id, {"recipient": key},
                    entity_uuid=str(notice.uuid), entity_label=notice.title)
    db.flush()
    return r


def recipient_out(r: NoticeRecipient, students: dict[int, str]) -> dict:
    return {"id": r.id, "key": r.recipient_key, "kind": r.kind, "name": r.name, "employee_id": r.employee_id,
            "guardian_id": r.guardian_id, "student_id": r.student_id, "has_sign_in": bool(r.portal_account_id) or r.kind == "staff",
            "children": [students.get(s) for s in (r.via_student_ids or []) if students.get(s)] if r.kind == "parent" else [],
            "read_at": r.read_at, "acknowledged_at": r.acknowledged_at, "send_count": r.send_count,
            "last_sent_at": r.last_sent_at, "channels": r.channels_sent or []}


def receipts_query(db: Session, n: Notice, status: str | None, kind: str | None, q: str | None):
    query = db.query(NoticeRecipient).filter(NoticeRecipient.company_id == n.company_id,
                                             NoticeRecipient.notice_id == n.id)
    if kind:
        query = query.filter(NoticeRecipient.kind == kind)
    if status == "read":
        query = query.filter(NoticeRecipient.read_at.isnot(None))
    elif status == "unread":
        query = query.filter(NoticeRecipient.read_at.is_(None))
    elif status == "acknowledged":
        query = query.filter(NoticeRecipient.acknowledged_at.isnot(None))
    elif status == "pending":
        query = query.filter(NoticeRecipient.acknowledged_at.is_(None))
    if q:
        query = query.filter(NoticeRecipient.name.ilike(f"%{q.strip()}%"))
    return query.order_by(NoticeRecipient.kind, NoticeRecipient.name)


