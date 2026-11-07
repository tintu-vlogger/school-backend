"""Private messages between staff and families.

A thread always has exactly two sides: one staff member (or the office inbox) and one parent
or learner. There is no learner-to-learner or parent-to-parent messaging by design. Messages
are never edited or deleted; people with comms.messages.audit can read every thread.

Who may start a thread:
- a parent: their children's class teacher and subject teachers (teacher allocation), or the
  office (when the office inbox is on);
- a learner: the same staff, when the institution allows learners to message;
- a teacher: the families (parents or the learner) of learners in their own sections;
- people with comms.messages.all: any family.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core import audit
from modules.academics import service as academics
from modules.comms import service
from modules.comms.models import Broadcast, Message, Thread
from modules.employees.models import Employee
from modules.students.models import Guardian, PortalAccount, Student, StudentGuardian

MAX_BODY = 5000


# ── Output ───────────────────────────────────────────────────────────

def thread_out(t: Thread, side: str, office_hours: dict | None = None) -> dict:
    party = t.guardian.full_name if t.party_kind == "parent" and t.guardian else (t.student.full_name if t.student else "")
    staff = "Office" if t.to_office and not t.staff_employee_id else service.employee_name(t.staff)
    return {
        "id": t.id, "uuid": str(t.uuid), "subject": t.subject, "status": t.status, "to_office": t.to_office,
        "party_kind": t.party_kind, "party_name": party, "guardian_id": t.guardian_id, "student_id": t.student_id,
        "staff_employee_id": t.staff_employee_id, "staff_name": staff,
        "about_student_id": t.about_student_id,
        "about_student_name": t.about_student.full_name if t.about_student else None,
        "unread": t.staff_unread if side == "staff" else t.party_unread,
        "message_count": t.message_count, "last_message_at": t.last_message_at,
        "last_message_preview": t.last_message_preview, "started_by": t.started_by,
        "is_broadcast": bool(t.broadcast_id), "closed_at": t.closed_at, "closed_by": t.closed_by,
        "created_at": t.created_at, "office_hours": office_hours,
    }


def message_out(db: Session, m: Message, side: str) -> dict:
    from modules.documents import storage
    mine = (m.sender_kind == "staff") if side == "staff" else (m.sender_kind in ("parent", "student"))
    return {"id": m.id, "uuid": str(m.uuid), "sender_kind": m.sender_kind, "sender_name": m.sender_name,
            "sender_employee_id": m.sender_employee_id, "body": m.body, "mine": mine,
            "attachments": storage.with_links(db, m.company_id, m.attachments or []), "created_at": m.created_at}


def _opts(q):
    return q.options(joinedload(Thread.staff), joinedload(Thread.guardian), joinedload(Thread.student),
                     joinedload(Thread.about_student))


# ── Staff side ───────────────────────────────────────────────────────

def staff_threads(db: Session, user: dict, box: str = "mine", status: str | None = None, q: str | None = None):
    cid, emp = user["company_id"], user.get("employee_id")
    query = _opts(db.query(Thread)).filter(Thread.company_id == cid)
    if box == "office":
        service.require(db, user, "comms.messages.office")
        query = query.filter(Thread.to_office.is_(True))
    elif box == "all":
        service.require(db, user, "comms.messages.audit")
    else:
        query = query.filter(Thread.staff_employee_id == (emp or -1))
    if status in ("open", "closed"):
        query = query.filter(Thread.status == status)
    elif status == "unread":
        query = query.filter(Thread.staff_unread > 0)
    if q:
        like = f"%{q.strip()}%"
        query = (query.outerjoin(Guardian, Guardian.id == Thread.guardian_id)
                 .outerjoin(Student, Student.id == Thread.about_student_id)
                 .filter(or_(Thread.subject.ilike(like), Guardian.first_name.ilike(like),
                             Guardian.last_name.ilike(like), Student.first_name.ilike(like),
                             Student.last_name.ilike(like))))
    return query.order_by(Thread.last_message_at.desc().nullslast(), Thread.id.desc())


def staff_unread(db: Session, user: dict) -> int:
    from sqlalchemy import func
    cid, emp = user["company_id"], user.get("employee_id")
    conds = [Thread.staff_employee_id == (emp or -1)]
    if service.has(db, user, "comms.messages.office"):
        conds.append(Thread.to_office.is_(True))
    return int(db.query(func.coalesce(func.sum(Thread.staff_unread), 0)).filter(
        Thread.company_id == cid, or_(*conds)).scalar() or 0)


def staff_access(db: Session, user: dict, t: Thread, write: bool = False) -> None:
    emp = user.get("employee_id")
    if emp and t.staff_employee_id == emp:
        return
    if t.to_office and service.has(db, user, "comms.messages.office"):
        return
    if not write and service.has(db, user, "comms.messages.audit"):
        return
    raise HTTPException(status_code=404, detail="Conversation not found")


def staff_contacts(db: Session, user: dict, q: str | None, section_id: int | None = None, limit: int = 30) -> list[dict]:
    """Learners (with their parents) the signed-in staff member may write to."""
    from modules.students import service as students
    cid = user["company_id"]
    allowed = None if service.has(db, user, "comms.messages.all") else \
        academics.teacher_section_ids(db, cid, user.get("employee_id"))
    if allowed is not None and not allowed:
        return []
    query = students.search_students(db, cid, q, section_id=section_id)
    if allowed is not None:
        from modules.students.models import Enrollment
        query = query.filter(Enrollment.section_id.in_(allowed))
    rows = query.limit(limit).all()
    ids = [s.id for s, _ in rows]
    links = db.query(StudentGuardian).options(joinedload(StudentGuardian.guardian)).filter(
        StudentGuardian.company_id == cid, StudentGuardian.student_id.in_(ids or [-1])).all()
    gids = [l.guardian_id for l in links]
    accounts = service.portal_ids_for(db, cid, gids, ids)
    out = []
    for s, e in rows:
        out.append({**students.student_brief(s, e), "has_sign_in": f"s:{s.id}" in accounts,
                    "guardians": [{"id": l.guardian_id, "name": l.guardian.full_name, "relation": l.relation,
                                   "has_sign_in": f"g:{l.guardian_id}" in accounts}
                                  for l in links if l.student_id == s.id]})
    return out


def start_by_staff(db: Session, user: dict, *, student_id: int, guardian_id: int | None, subject: str, body: str,
                   attachments: list | None) -> Thread:
    cid = user["company_id"]
    if not user.get("employee_id"):
        raise HTTPException(status_code=400, detail="Messages are sent from a staff profile. Link your login to a staff record first.")
    student = db.query(Student).filter(Student.company_id == cid, Student.id == student_id).first()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    if not service.staff_can_message_student(db, user, student_id):
        raise HTTPException(status_code=403, detail="You can message only families of learners in your own sections.")
    from modules.students import service as students
    t = Thread(company_id=cid, subject=subject.strip()[:200] or "Message", staff_employee_id=user["employee_id"],
               about_student_id=student_id, section_id=students.student_section_id(db, cid, student_id),
               started_by="staff")
    if guardian_id:
        link = db.query(StudentGuardian).filter(StudentGuardian.company_id == cid, StudentGuardian.student_id == student_id,
                                                StudentGuardian.guardian_id == guardian_id).first()
        if link is None:
            raise HTTPException(status_code=400, detail="This parent is not linked to the learner.")
        t.party_kind, t.guardian_id = "parent", guardian_id
    else:
        t.party_kind, t.student_id = "student", student_id
    db.add(t)
    db.flush()
    post(db, user, t, body, attachments, side="staff")
    return t


def broadcast(db: Session, user: dict, *, section_id: int, subject: str, body: str, attachments: list | None) -> Broadcast:
    """One message to the parents of every learner in a section; each family gets its own
    thread so their replies stay private."""
    from modules.documents import storage
    from modules.students import service as students
    cid = user["company_id"]
    if not user.get("employee_id"):
        raise HTTPException(status_code=400, detail="Messages are sent from a staff profile.")
    if not service.has(db, user, "comms.messages.all") and \
            section_id not in academics.teacher_section_ids(db, cid, user.get("employee_id")):
        raise HTTPException(status_code=403, detail="You can broadcast only to your own sections.")
    rows = students.section_students(db, cid, section_id)
    if not rows:
        raise HTTPException(status_code=400, detail="There are no learners in this section yet.")
    files = storage.attachments_from(db, cid, attachments or [], limit=5)
    name = service.actor_name(db, user)
    b = Broadcast(company_id=cid, section_id=section_id, sender_employee_id=user["employee_id"], sender_name=name,
                  subject=subject.strip()[:200], body=body.strip()[:MAX_BODY], attachments=files)
    db.add(b)
    db.flush()
    family: dict[int, int] = {}
    for s, _ in rows:
        for l in db.query(StudentGuardian).filter(StudentGuardian.company_id == cid, StudentGuardian.student_id == s.id,
                                                  StudentGuardian.receives_notifications.is_(True)).all():
            family.setdefault(l.guardian_id, s.id)
    for gid, sid in family.items():
        t = Thread(company_id=cid, subject=b.subject, staff_employee_id=user["employee_id"], party_kind="parent",
                   guardian_id=gid, about_student_id=sid, section_id=section_id, broadcast_id=b.id, started_by="staff")
        db.add(t)
        db.flush()
        _append(db, t, sender_kind="staff", sender_employee_id=user["employee_id"], sender_name=name,
                body=b.body, files=files, broadcast_id=b.id)
    b.recipient_count = len(family)
    accounts = service.portal_ids_for(db, cid, family.keys())
    if accounts:
        from modules.notifications.service import notify_portal
        notify_portal(db, cid, list(accounts.values()), kind="comms_message", title=f"Message from {name}",
                      body=b.subject, link="/dashboard/my/messages", actor_name=name)
    audit.event(db, "comms.broadcast_sent", "comms_broadcasts", b.id, {"section_id": section_id,
                                                                         "families": len(family)},
                entity_uuid=str(b.uuid), entity_label=b.subject)
    db.flush()
    return b


# ── Portal side ──────────────────────────────────────────────────────

def portal_threads(db: Session, ctx: service.PortalCtx, company_id: int, status: str | None = None,
                   student_id: int | None = None):
    query = _opts(db.query(Thread)).filter(Thread.company_id == company_id)
    query = _portal_filter(query, ctx)
    if status in ("open", "closed"):
        query = query.filter(Thread.status == status)
    if student_id:
        query = query.filter(Thread.about_student_id == student_id)
    return query.order_by(Thread.last_message_at.desc().nullslast(), Thread.id.desc())


def _portal_filter(query, ctx: service.PortalCtx):
    if ctx.role == "parent":
        return query.filter(Thread.party_kind == "parent", Thread.guardian_id == (ctx.guardian_id or -1))
    return query.filter(Thread.party_kind == "student", Thread.student_id == (ctx.student_id or -1))


def portal_unread(db: Session, ctx: service.PortalCtx, company_id: int) -> int:
    from sqlalchemy import func
    q = db.query(func.coalesce(func.sum(Thread.party_unread), 0)).filter(Thread.company_id == company_id)
    return int(_portal_filter(q, ctx).scalar() or 0)


def portal_access(ctx: service.PortalCtx, t: Thread) -> None:
    ok = (t.party_kind == "parent" and ctx.role == "parent" and t.guardian_id == ctx.guardian_id) or \
         (t.party_kind == "student" and ctx.role == "student" and t.student_id == ctx.student_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation not found")


def start_by_portal(db: Session, user: dict, ctx: service.PortalCtx, *, employee_id: int | None, to_office: bool,
                    student_id: int | None, subject: str, body: str, attachments: list | None) -> Thread:
    cid = user["company_id"]
    if ctx.role == "student":
        student_id = ctx.student_id
    if not student_id and len(ctx.children) == 1:
        student_id = ctx.children[0]
    if not student_id or student_id not in ctx.children:
        raise HTTPException(status_code=400, detail="Pick the child this message is about.")
    if not to_office and not employee_id:
        raise HTTPException(status_code=400, detail="Pick who to write to.")
    if not service.can_portal_message(db, cid, ctx, employee_id, to_office=to_office, student_id=student_id):
        if ctx.role == "student" and not service.get_settings(db, cid).get("students_can_message", True):
            raise HTTPException(status_code=403, detail="Messaging staff is switched off for students.")
        raise HTTPException(status_code=403, detail="You can message only your child's teachers and the office.")
    t = Thread(company_id=cid, subject=subject.strip()[:200] or "Message", to_office=to_office,
               staff_employee_id=None if to_office else employee_id, party_kind=ctx.role,
               guardian_id=ctx.guardian_id if ctx.role == "parent" else None,
               student_id=ctx.student_id if ctx.role == "student" else None, about_student_id=student_id,
               section_id=ctx.sections.get(student_id), started_by=ctx.role)
    db.add(t)
    db.flush()
    post(db, user, t, body, attachments, side="portal")
    return t


# ── Posting, reading, closing ────────────────────────────────────────

def _append(db: Session, t: Thread, *, sender_kind: str, sender_name: str, body: str, files: list,
            sender_employee_id: int | None = None, sender_portal_id: int | None = None,
            broadcast_id: int | None = None) -> Message:
    m = Message(company_id=t.company_id, thread_id=t.id, sender_kind=sender_kind, sender_employee_id=sender_employee_id,
                sender_portal_id=sender_portal_id, sender_name=sender_name, body=body, attachments=files,
                broadcast_id=broadcast_id)
    db.add(m)
    t.message_count = (t.message_count or 0) + 1
    t.last_message_at = service.now()
    t.last_message_preview = (body or ("Attachment" if files else ""))[:200]
    if sender_kind == "staff":
        t.party_unread = (t.party_unread or 0) + 1
        t.staff_unread = 0
    else:
        t.staff_unread = (t.staff_unread or 0) + 1
        t.party_unread = 0
    db.flush()
    return m


def post(db: Session, user: dict, t: Thread, body: str, attachments: list | None, side: str) -> Message:
    from modules.documents import storage
    from modules.notifications.service import notify, notify_portal
    body = (body or "").strip()
    files = storage.attachments_from(db, t.company_id, attachments or [], limit=5)
    if not body and not files:
        raise HTTPException(status_code=400, detail="Write a message or attach a file.")
    if len(body) > MAX_BODY:
        raise HTTPException(status_code=400, detail=f"Keep messages under {MAX_BODY} characters.")
    if t.status == "closed":
        if side == "portal":
            raise HTTPException(status_code=400, detail="This conversation is closed. Start a new one.")
        t.status, t.closed_at, t.closed_by = "open", None, None  # staff replying reopens it
    name = service.actor_name(db, user)
    if side == "staff":
        m = _append(db, t, sender_kind="staff", sender_name=name, body=body, files=files,
                    sender_employee_id=user.get("employee_id"))
        if t.to_office and not t.staff_employee_id and user.get("employee_id"):
            pass  # office threads stay in the shared inbox
        accounts = service.portal_ids_for(db, t.company_id, [t.guardian_id] if t.guardian_id else [],
                                          [t.student_id] if t.student_id else [])
        if accounts:
            notify_portal(db, t.company_id, list(accounts.values()), kind="comms_message",
                          title=f"New message from {name}", body=t.subject,
                          link=f"/dashboard/my/messages?thread={t.uuid}", actor_name=name)
    else:
        m = _append(db, t, sender_kind=t.party_kind, sender_name=name, body=body, files=files,
                    sender_portal_id=user.get("portal_id"))
        targets = [t.staff_employee_id] if t.staff_employee_id else _office_staff(db, t.company_id)
        if targets:
            notify(db, t.company_id, targets, kind="comms_message", title=f"New message from {name}",
                   body=t.subject, link=f"/dashboard/comms/messages?thread={t.uuid}", actor_name=name,
                   group_key=f"comms_thread:{t.id}")
    return m


def _office_staff(db: Session, company_id: int) -> list[int]:
    from modules.roles.models import DEFAULT_ROLE_PERMISSIONS, Role
    key = "comms.messages.office"
    roles = {r.role_name: list(r.permissions or []) for r in db.query(Role).filter(Role.company_id == company_id).all()}
    names = {n for n, p in roles.items() if key in p} | {"admin"}
    names |= {n for n, p in DEFAULT_ROLE_PERMISSIONS.items() if key in p and n not in roles}
    return [i for (i,) in db.query(Employee.id).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                                       Employee.role.in_(names)).limit(50).all()]


def mark_read(db: Session, t: Thread, side: str) -> None:
    if side == "staff":
        t.staff_unread = 0
    else:
        t.party_unread = 0
    db.flush()


def set_status(db: Session, user: dict, t: Thread, status: str) -> None:
    if status not in ("open", "closed"):
        raise HTTPException(status_code=400, detail="Unknown status.")
    t.status = status
    if status == "closed":
        t.closed_at, t.closed_by = service.now(), service.actor_name(db, user)
    else:
        t.closed_at, t.closed_by = None, None
    audit.event(db, f"comms.thread_{'closed' if status == 'closed' else 'reopened'}", "comms_threads", t.id, {},
                entity_uuid=str(t.uuid), entity_label=t.subject)
    db.flush()


def messages_of(db: Session, t: Thread) -> list[Message]:
    return db.query(Message).filter(Message.company_id == t.company_id, Message.thread_id == t.id).order_by(
        Message.created_at, Message.id).all()


def portal_contacts(db: Session, company_id: int, ctx: service.PortalCtx, student_id: int | None) -> dict:
    settings = service.get_settings(db, company_id)
    allowed = settings.get("students_can_message", True) if ctx.role == "student" else settings.get("parents_can_message", True)
    staff = service.messageable_staff(db, company_id, ctx, student_id) if allowed else []
    return {"enabled": bool(allowed), "staff": staff, "office": bool(allowed and settings.get("office_inbox", True)),
            "office_hours": service.office_hours_state(db, company_id)}


def get_account(db: Session, portal_id: int | None) -> PortalAccount | None:
    return db.query(PortalAccount).filter(PortalAccount.id == portal_id).first() if portal_id else None
