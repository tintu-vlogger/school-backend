"""Onboarding rules: choose a flow by rule, start from an accepted offer, the pre-joining
portal (secure expiring link), field-by-field HR review, writing accepted details to the
employee record, checklist tasks (HR, IT, admin, manager, buddy, joiner), the mandatory
appointment letter, statutory joining forms, assets and accounts, induction and courses,
the day-one switch, welcome email and Buzz post, and 30/60/90-day check-ins.

Functions other modules call:
    start_from_offer(db, company_id, employee, offer) -> Onboarding
    start(db, company_id, employee, ...) -> Onboarding
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from modules.onboarding import forms as sforms
from modules.onboarding import settings as osettings
from modules.onboarding.models import Onboarding, OnboardingCheckin, OnboardingFlow
from modules.recruitment import common as c

MANAGE = "talent.onboarding.manage"
STATUSES = {"pre_joining": "Pre-joining", "day_one": "Joining today", "in_progress": "Onboarding",
            "completed": "Completed", "cancelled": "Cancelled"}
LIST_SECTIONS = {"family", "nominees", "previous_employment", "education"}
DEFAULT_SECTIONS = ["personal", "contact", "family", "nominees", "bank", "statutory", "previous_employment", "tax",
                    "education", "emergency", "documents"]
REQUIRED_FIELDS = {
    "personal": ["first_name", "last_name", "date_of_birth", "gender_id", "father_name"],
    "contact": ["phone"],
    "bank": ["account_holder", "account_number", "ifsc"],
    "statutory": ["pan"],
    "tax": ["regime"],
    "emergency": ["name", "phone"],
}


def can_manage(db: Session, user: dict) -> bool:
    return c.has_perm(db, user, MANAGE)


def require_manage(db: Session, user: dict) -> None:
    if not can_manage(db, user):
        raise Forbidden("Only HR can manage onboarding.")


# ── Flows ───────────────────────────────────────────────────────────

def get_flow(db: Session, company_id: int, uuid) -> OnboardingFlow:
    f = db.query(OnboardingFlow).filter(OnboardingFlow.company_id == company_id, OnboardingFlow.uuid == uuid).first()
    if f is None:
        raise NotFound("Onboarding flow")
    return f


def _clean_tasks(db: Session, company_id: int, tasks) -> list[dict]:
    out = []
    for t in tasks or []:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        owner = t.get("owner") or "hr"
        if owner not in osettings.OWNERS:
            raise BadRequest(f"Unknown owner for “{title}”.")
        stage = t.get("stage") or "pre_joining"
        if stage not in ("pre_joining", "day_one"):
            raise BadRequest("A task belongs to pre-joining or to day one.")
        eid = t.get("employee_id")
        if owner == "employee":
            if not eid or c.employee(db, company_id, eid) is None:
                raise BadRequest(f"Pick the person for “{title}”.")
        out.append({"title": title[:200], "owner": owner, "employee_id": int(eid) if eid else None, "stage": stage,
                    "due_days": int(t.get("due_days") or 0), "checklist": [str(x).strip()[:200] for x in t.get("checklist") or [] if str(x).strip()],
                    "kind": t.get("kind") or None})
    return out


def save_flow(db: Session, company_id: int, data: dict, f: OnboardingFlow | None = None) -> OnboardingFlow:
    f = f or OnboardingFlow(company_id=company_id)
    name = (data.get("name") if "name" in data else f.name) or ""
    if not name.strip():
        raise BadRequest("Name the flow.")
    f.name = name.strip()[:150]
    for k in ("description",):
        if k in data:
            setattr(f, k, (data[k] or "").strip() or None)
    for k in ("pre_joining_enabled", "day_one_enabled", "require_admin_closure", "active"):
        if k in data:
            setattr(f, k, bool(data[k]))
    if "priority" in data:
        f.priority = int(data["priority"] or 100)
    if "audience" in data:
        f.audience = c.clean_audience(data["audience"])
    if "tasks" in data:
        f.tasks = _clean_tasks(db, company_id, data["tasks"])
    if "sections" in data:
        bad = [s for s in data["sections"] or [] if s not in osettings.SECTIONS]
        if bad:
            raise BadRequest("Unknown portal section.")
        f.sections = list(dict.fromkeys(data["sections"] or []))
    if "documents" in data:
        f.documents = [{"label": (d.get("label") or "").strip()[:120], "required": bool(d.get("required"))}
                       for d in data["documents"] or [] if (d.get("label") or "").strip()]
    if "induction" in data:
        f.induction = [{"title": (s.get("title") or "").strip()[:150], "day": max(1, int(s.get("day") or 1)),
                        "time": (s.get("time") or "10:00")[:5], "duration_minutes": int(s.get("duration_minutes") or 60),
                        "host_id": s.get("host_id") or None, "where": (s.get("where") or "")[:150]}
                       for s in data["induction"] or [] if (s.get("title") or "").strip()]
    if "course_ids" in data:
        f.course_ids = [int(x) for x in data["course_ids"] or []]
    if "statutory_forms" in data:
        f.statutory_forms = [x for x in data["statutory_forms"] or [] if x in sforms.FORMS]
    db.add(f)
    db.flush()
    return f


def pick_flow(db: Session, company_id: int, emp) -> OnboardingFlow | None:
    flows = (db.query(OnboardingFlow).filter(OnboardingFlow.company_id == company_id, OnboardingFlow.active.is_(True))
             .order_by(OnboardingFlow.priority, OnboardingFlow.id).all())
    for f in flows:
        if c.audience_match(f.audience, emp):
            return f
    return None


def flow_out(db: Session, f: OnboardingFlow, counts: dict | None = None) -> dict:
    names = c.names_for(db, f.company_id, [t.get("employee_id") for t in f.tasks or []] +
                        [s.get("host_id") for s in f.induction or []])
    return {"uuid": str(f.uuid), "name": f.name, "description": f.description, "audience": f.audience or {},
            "priority": f.priority, "pre_joining_enabled": f.pre_joining_enabled, "day_one_enabled": f.day_one_enabled,
            "tasks": [{**t, "employee_name": names.get(t.get("employee_id"))} for t in f.tasks or []],
            "sections": f.sections or DEFAULT_SECTIONS, "documents": f.documents or [],
            "induction": [{**s, "host_name": names.get(s.get("host_id"))} for s in f.induction or []],
            "course_ids": f.course_ids or [], "statutory_forms": f.statutory_forms or [],
            "require_admin_closure": f.require_admin_closure, "active": f.active,
            "in_use": (counts or {}).get(f.id, 0)}


# ── Starting an onboarding ──────────────────────────────────────────

def _log(o: Onboarding, text: str, kind: str = "event", **extra) -> None:
    hist = list(o.history or [])
    hist.append(c.event(kind=kind, text=text, **extra))
    o.history = hist[-300:]
    flag_modified(o, "history")


def _owner_id(db: Session, o: Onboarding, emp, owner: str, employee_id: int | None, conf: dict) -> int | None:
    if owner == "employee":
        return employee_id
    if owner == "joiner":
        return emp.id
    if owner == "manager":
        return emp.reporting_manager_id or o.hr_owner_employee_id
    if owner == "buddy":
        return o.buddy_employee_id or emp.reporting_manager_id
    if owner == "it" and conf.get("it_owner_id"):
        return int(conf["it_owner_id"])
    if owner == "admin" and conf.get("admin_owner_id"):
        return int(conf["admin_owner_id"])
    if o.hr_owner_employee_id:
        return o.hr_owner_employee_id
    ids = c.role_ids(db, o.company_id, ("hr",)) or c.role_ids(db, o.company_id, ("admin",))
    return ids[0] if ids else None


def _create_tasks(db: Session, o: Onboarding, emp, stage: str) -> int:
    from modules.tasks import service as tasks
    if o.flow is None:
        return 0
    conf = osettings.get(db, o.company_id)
    have = {t.title for t in tasks.tasks_for_source(db, o.company_id, "onboarding", str(o.uuid))}
    n = 0
    for t in o.flow.tasks or []:
        if t.get("stage") != stage or t["title"] in have:
            continue
        due = o.joining_date + timedelta(days=int(t.get("due_days") or 0))
        assignee = _owner_id(db, o, emp, t.get("owner") or "hr", t.get("employee_id"), conf)
        tasks.create_task(db, o.company_id, title=t["title"], assignee_employee_id=assignee, due_date=due,
                          description=f"Onboarding of {emp.first_name} {emp.last_name} (joining {o.joining_date.strftime('%d %b %Y')}).",
                          checklist=t.get("checklist") or [], labels=["onboarding", osettings.OWNERS.get(t.get("owner"), "HR")],
                          source_module="onboarding", source_ref=str(o.uuid),
                          priority="high" if due <= c.today(db, o.company_id) + timedelta(days=2) else "medium",
                          created_by_name="Onboarding")
        n += 1
    return n


def start(db: Session, company_id: int, emp, *, offer=None, flow: OnboardingFlow | None = None,
          joining_date: date | None = None, buddy_id: int | None = None, hr_owner_id: int | None = None,
          send_invite: bool = True) -> Onboarding:
    live = (db.query(Onboarding).filter(Onboarding.company_id == company_id, Onboarding.employee_id == emp.id,
                                        Onboarding.status.in_(("pre_joining", "day_one", "in_progress"))).first())
    if live is not None:
        return live
    conf = osettings.get(db, company_id)
    jd = joining_date or emp.date_of_joining or c.today(db, company_id)
    flow = flow or pick_flow(db, company_id, emp)
    o = Onboarding(company_id=company_id, employee_id=emp.id, offer_id=offer.id if offer else None,
                   flow_id=flow.id if flow else None, joining_date=jd,
                   status="pre_joining" if (emp.status == "pre_joining" and (flow is None or flow.pre_joining_enabled))
                   else "in_progress",
                   buddy_employee_id=buddy_id, hr_owner_employee_id=hr_owner_id or conf.get("hr_owner_id") or None,
                   portal_token=c.token(), portal_enabled=True,
                   portal_expires_at=c.now() + timedelta(days=int(conf["portal_link_days"])),
                   portal_data=_prefill(db, company_id, emp, offer))
    o.flow = flow
    db.add(o)
    db.flush()
    _log(o, f"Onboarding started with the flow “{flow.name if flow else 'none'}”"
            + (f" from offer {offer.number}" if offer is not None and offer.number else ""), "start")
    _create_tasks(db, o, emp, "pre_joining")
    if o.status == "in_progress":
        start_day_one(db, o, emp, quiet=True)
    elif send_invite:
        send_portal_invite(db, o, emp)
    return o


def start_from_offer(db: Session, company_id: int, emp, offer) -> Onboarding:
    return start(db, company_id, emp, offer=offer, joining_date=offer.joining_date)


def _prefill(db: Session, company_id: int, emp, offer) -> dict:
    personal = {"first_name": emp.first_name, "last_name": emp.last_name}
    contact = {"phone": emp.phone}
    if offer is not None and offer.candidate is not None:
        cand = offer.candidate
        personal["personal_email"] = cand.email
        prev = []
        if cand.current_company:
            prev.append({"company_name": cand.current_company, "designation": cand.current_title,
                         "last_ctc": str(cand.current_ctc) if cand.current_ctc else None})
        edu = [{"degree": cand.education}] if cand.education else []
        return {"personal": personal, "contact": contact, "previous_employment": prev, "education": edu}
    return {"personal": personal, "contact": contact}


def portal_link(db: Session, o: Onboarding) -> str:
    return f"{c.frontend_url(db, o.company_id)}/join/{o.portal_token}"


def _joiner_email(db: Session, o: Onboarding, emp) -> str | None:
    personal = (o.portal_data or {}).get("personal") or {}
    return emp.personal_email or personal.get("personal_email") or emp.email


def send_portal_invite(db: Session, o: Onboarding, emp=None, reminder: bool = False) -> None:
    from core.email import send_email
    emp = emp or c.employee(db, o.company_id, o.employee_id)
    to = _joiner_email(db, o, emp)
    if not to:
        return
    comp = c.company(db, o.company_id)
    cname = comp.company_name if comp else "us"
    exp = o.portal_expires_at.strftime("%d %b %Y") if o.portal_expires_at else ""
    send_email(to, f"{'Reminder: ' if reminder else ''}Complete your joining details for {cname}",
               f"Dear {emp.first_name},\n\n{'A reminder to' if reminder else 'Before you join on ' + o.joining_date.strftime('%d %b %Y') + ', please'} "
               f"fill in your personal, bank, nominee and tax details and upload your documents here:\n{portal_link(db, o)}\n\n"
               f"The link is personal to you and works until {exp}.\n\nWelcome aboard,\nHR team")
    o.last_reminded_at = c.now()
    if reminder:
        o.reminders_sent = (o.reminders_sent or 0) + 1
    _log(o, "Joining details reminder sent" if reminder else f"Joining link sent to {to}", "portal")


def get(db: Session, company_id: int, uuid) -> Onboarding:
    o = (db.query(Onboarding).options(joinedload(Onboarding.flow), joinedload(Onboarding.checkins))
         .filter(Onboarding.company_id == company_id, Onboarding.uuid == uuid).first())
    if o is None:
        raise NotFound("Onboarding")
    return o


# ── Pre-joining portal ──────────────────────────────────────────────

def by_token(db: Session, token: str) -> Onboarding:
    o = db.query(Onboarding).options(joinedload(Onboarding.flow)).filter(Onboarding.portal_token == token).first()
    if o is None:
        raise NotFound("Joining link")
    return o


def check_portal(o: Onboarding) -> None:
    if not o.portal_enabled:
        raise Forbidden("This joining link has been switched off. Please contact HR.")
    if o.portal_expires_at and o.portal_expires_at < c.now():
        raise Forbidden("This joining link has expired. Please ask HR for a new one.")
    if o.status in ("cancelled",):
        raise Forbidden("This joining link is no longer active.")


def sections_for(o: Onboarding) -> list[str]:
    return (o.flow.sections if o.flow and o.flow.sections else DEFAULT_SECTIONS)


def _iso(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _validate_section(section: str, data):
    from modules.employees import validators as v
    if section in LIST_SECTIONS:
        if not isinstance(data, list):
            raise BadRequest("This section should be a list.")
        rows = []
        for item in data[:20]:
            if not isinstance(item, dict):
                continue
            item = {k: _iso(val) for k, val in item.items() if val not in (None, "")}
            if section == "nominees":
                if item.get("kind") not in ("pf", "eps", "gratuity", "insurance", "other"):
                    raise BadRequest("Say what each nomination is for: PF, EPS, gratuity or insurance.")
                if not item.get("name"):
                    raise BadRequest("Enter each nominee's name.")
                try:
                    share = float(item.get("share_percent") or 0)
                except ValueError:
                    raise BadRequest("Shares are percentages.")
                if not 0 < share <= 100:
                    raise BadRequest("Each nominee's share should be between 1 and 100%.")
            if section in ("family", "education", "previous_employment"):
                key = {"family": "name", "education": "degree", "previous_employment": "company_name"}[section]
                if not item.get(key):
                    continue
            rows.append(item)
        if section == "nominees":
            totals: dict[str, float] = {}
            for r in rows:
                totals[r["kind"]] = totals.get(r["kind"], 0) + float(r["share_percent"])
            for kind, total in totals.items():
                if abs(total - 100) > 0.01:
                    raise BadRequest(f"{kind.upper()} nominee shares add up to {total:g}%. They should add up to 100%.")
        return rows
    if not isinstance(data, dict):
        raise BadRequest("This section could not be read.")
    out = {k: _iso(val) for k, val in data.items() if val not in (None, "")}
    if section == "statutory":
        for field, fn in (("pan", v.clean_pan), ("aadhaar", v.clean_aadhaar), ("uan", v.clean_uan),
                          ("esi_number", v.clean_esi), ("passport_number", v.clean_passport)):
            if field in out:
                out[field] = fn(out[field])
    if section == "bank":
        if "ifsc" in out:
            out["ifsc"] = v.clean_ifsc(out["ifsc"])
        if "account_number" in out:
            out["account_number"] = v.clean_account_number(out["account_number"])
    if section == "contact":
        for f in ("phone", "alternate_phone"):
            if f in out:
                out[f] = v.clean_phone(out[f])
        for key in ("current_address", "permanent_address"):
            if isinstance(out.get(key), dict) and out[key].get("pincode"):
                out[key]["pincode"] = v.clean_pincode(out[key]["pincode"])
    if section == "tax" and out.get("regime") not in (None, "old", "new"):
        raise BadRequest("Pick the old or the new tax regime.")
    if section == "personal" and out.get("date_of_birth"):
        try:
            dob = date.fromisoformat(str(out["date_of_birth"])[:10])
        except ValueError:
            raise BadRequest("Date of birth doesn't look right.")
        if dob > date.today():
            raise BadRequest("Date of birth can't be in the future.")
    return out


def save_section(db: Session, o: Onboarding, section: str, data) -> Onboarding:
    if section not in sections_for(o) or section == "documents":
        raise BadRequest("Unknown section.")
    if o.review_status == "accepted":
        raise BadRequest("HR has already accepted your details. Contact HR to change them.")
    clean = _validate_section(section, data)
    if section == "personal" and clean.get("date_of_birth"):
        conf = osettings.get(db, o.company_id)
        dob = date.fromisoformat(clean["date_of_birth"][:10])
        age = (o.joining_date - dob).days // 365
        if age < conf["minimum_age"]:
            raise BadRequest(f"You must be at least {conf['minimum_age']} years old on the joining date.")
    pdata = dict(o.portal_data or {})
    pdata[section] = clean
    o.portal_data = pdata
    flag_modified(o, "portal_data")
    # an edit re-opens fields HR sent back
    review = dict(o.review or {})
    for k in list(review):
        if k == section or k.startswith(section + "."):
            if review[k].get("status") == "sent_back":
                review[k] = {**review[k], "status": "updated", "updated_at": c.now().isoformat()}
    o.review = review
    flag_modified(o, "review")
    if o.portal_opened_at is None:
        o.portal_opened_at = c.now()
    return o


def add_upload(db: Session, o: Onboarding, label: str, blob) -> dict:
    if not (label or "").strip():
        raise BadRequest("Say which document this is.")
    ups = list(o.uploads or [])
    ups = [u for u in ups if u.get("label") != label.strip()]
    item = {"label": label.strip()[:120], "file_id": blob.id, "file_uuid": str(blob.uuid), "name": blob.filename,
            "size": blob.size, "status": "pending", "at": c.now().isoformat()}
    ups.append(item)
    o.uploads = ups
    flag_modified(o, "uploads")
    return item


def completeness(o: Onboarding) -> dict:
    pdata = o.portal_data or {}
    secs = sections_for(o)
    missing = []
    for s in secs:
        if s == "documents":
            needed = [d["label"] for d in (o.flow.documents if o.flow else []) if d.get("required")]
            have = {u["label"] for u in o.uploads or []}
            missing += [f"Document: {x}" for x in needed if x not in have]
            continue
        if s in LIST_SECTIONS:
            if s == "nominees" and not pdata.get(s):
                missing.append(osettings.SECTIONS[s])
            continue
        for f in REQUIRED_FIELDS.get(s, []):
            if not (pdata.get(s) or {}).get(f):
                missing.append(f"{osettings.SECTIONS[s]}: {f.replace('_id', '').replace('_', ' ')}")
    filled = sum(1 for s in secs if (pdata.get(s) if s != "documents" else o.uploads))
    return {"sections": len(secs), "filled": filled, "missing": missing,
            "percent": round(100 * filled / len(secs)) if secs else 100}


def submit_portal(db: Session, o: Onboarding) -> Onboarding:
    comp = completeness(o)
    if comp["missing"]:
        raise BadRequest("Please complete: " + "; ".join(comp["missing"][:6]))
    o.portal_submitted_at = c.now()
    o.review_status = "submitted"
    _log(o, "Joiner submitted their details for review", "portal")
    emp = c.employee(db, o.company_id, o.employee_id)
    msg = f"{emp.first_name} {emp.last_name} submitted their joining details. Review them in Onboarding."
    if o.hr_owner_employee_id:
        c.notify(db, o.company_id, o.hr_owner_employee_id, msg, "onboarding")
    else:
        c.notify_hr(db, o.company_id, msg, "onboarding")
    return o


# ── HR review ───────────────────────────────────────────────────────

def review(db: Session, o: Onboarding, items: list[dict]) -> Onboarding:
    """items: [{"key": "bank.ifsc" | "nominees" | "doc:PAN card", "status": "accepted" | "sent_back", "remark"}]"""
    rv = dict(o.review or {})
    sent_back = []
    for it in items:
        key, st = (it.get("key") or "").strip(), it.get("status")
        if not key or st not in ("accepted", "sent_back"):
            raise BadRequest("Accept or send back each item.")
        remark = (it.get("remark") or "").strip()
        if st == "sent_back" and not remark:
            raise BadRequest("Add a remark saying what needs fixing.")
        rv[key] = {"status": st, "remark": remark or None, "at": c.now().isoformat(), "by": c.actor_name()}
        if key.startswith("doc:"):
            ups = list(o.uploads or [])
            for u in ups:
                if u["label"] == key[4:]:
                    u["status"], u["remark"] = st, remark or None
            o.uploads = ups
            flag_modified(o, "uploads")
        if st == "sent_back":
            sent_back.append((key, remark))
    o.review = rv
    flag_modified(o, "review")
    if sent_back:
        o.review_status = "sent_back"
        emp = c.employee(db, o.company_id, o.employee_id)
        from core.email import send_email
        to = _joiner_email(db, o, emp)
        lines = "\n".join(f"- {k.replace('doc:', 'Document: ').replace('.', ' → ')}: {r}" for k, r in sent_back)
        if to:
            send_email(to, "Please update your joining details",
                       f"Dear {emp.first_name},\n\nHR reviewed your details and needs a few changes:\n{lines}\n\n"
                       f"Update them here: {portal_link(db, o)}\n\nHR team")
        _log(o, f"{len(sent_back)} item(s) sent back to the joiner", "review", items=[k for k, _ in sent_back])
        if o.portal_expires_at and o.portal_expires_at < c.now() + timedelta(days=3):
            o.portal_expires_at = c.now() + timedelta(days=7)
    else:
        _log(o, f"{len(items)} item(s) accepted", "review")
    return o


def review_keys(o: Onboarding) -> list[str]:
    keys = []
    pdata = o.portal_data or {}
    for s in sections_for(o):
        if s == "documents":
            keys += [f"doc:{u['label']}" for u in o.uploads or []]
        elif s in LIST_SECTIONS:
            if pdata.get(s):
                keys.append(s)
        else:
            keys += [f"{s}.{k}" for k in (pdata.get(s) or {})]
    return keys


def accept_all(db: Session, o: Onboarding) -> Onboarding:
    rv = o.review or {}
    pending = [k for k in review_keys(o) if (rv.get(k) or {}).get("status") != "accepted"]
    if pending:
        review(db, o, [{"key": k, "status": "accepted"} for k in pending])
    return o


def _d(v):
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def apply_to_record(db: Session, o: Onboarding) -> dict:
    """Write every accepted item to the employee record (once). Returns what was written."""
    from modules.employees import details, service as people
    emp = c.employee(db, o.company_id, o.employee_id)
    pdata, rv = o.portal_data or {}, dict(o.review or {})
    applied = dict(rv.get("_applied") or {})
    written: list[str] = []

    def ok(key):
        return (rv.get(key) or {}).get("status") == "accepted" and not applied.get(key)

    updates = {}
    for s in ("personal", "contact", "statutory"):
        for k, val in (pdata.get(s) or {}).items():
            key = f"{s}.{k}"
            if not ok(key):
                continue
            if k in ("date_of_birth", "passport_expiry", "marriage_date"):
                val = _d(val)
            if k in ("current_address", "permanent_address") and isinstance(val, dict):
                val = {kk: vv for kk, vv in val.items() if vv not in (None, "")}
            if k == "same_as_current":
                continue
            updates[k] = val
            applied[key] = True
    if (pdata.get("contact") or {}).get("same_as_current") and "current_address" in updates:
        updates["permanent_address"] = updates["current_address"]
    if updates:
        people.update(db, o.company_id, emp, updates, change_type="update", reason="Joining details")
        written += list(updates)
    kinds = {"family": "family", "nominees": "nominees", "previous_employment": "experience", "education": "education"}
    for s, kind in kinds.items():
        if not ok(s):
            continue
        family_ids = {}
        for item in pdata.get(s) or []:
            data = dict(item)
            for k in ("date_of_birth", "from_date", "to_date"):
                if k in data:
                    data[k] = _d(data[k])
            for k in ("end_year", "start_year"):
                if data.get(k):
                    data[k] = int(data[k])
            if s == "nominees":
                data["share_percent"] = str(data.get("share_percent"))
                if data.get("family_name") and data["family_name"] in family_ids:
                    data["family_member_id"] = family_ids[data["family_name"]]
                data.pop("family_name", None)
            for k in ("relation_id", "gender_id", "level_id"):
                if data.get(k) not in (None, ""):
                    data[k] = int(data[k])
            row = details.save_row(db, o.company_id, emp.id, kind, data)
            if s == "family":
                family_ids[row.name] = row.id
        applied[s] = True
        written.append(s)
    for s, kind in (("bank", "bank-accounts"), ("emergency", "emergency-contacts")):
        keys = [f"{s}.{k}" for k in (pdata.get(s) or {})]
        if keys and all((rv.get(k) or {}).get("status") == "accepted" for k in keys) and not applied.get(s):
            data = dict(pdata.get(s) or {})
            for k in ("bank_id", "relation_id"):
                if data.get(k) not in (None, ""):
                    data[k] = int(data[k])
            if s == "bank":
                data["is_primary"] = True
            else:
                data["is_primary"] = True
            details.save_row(db, o.company_id, emp.id, kind, data)
            applied[s] = True
            for k in keys:
                applied[k] = True
            written.append(s)
    if ok("tax.regime"):
        _save_tax(db, o.company_id, emp, pdata)
        applied["tax.regime"] = True
        written.append("tax")
    from modules.documents import service as documents, storage
    for u in o.uploads or []:
        key = f"doc:{u['label']}"
        if ok(key):
            blob = storage.by_id(db, o.company_id, u["file_id"])
            if blob is not None:
                documents.file_into_employee_documents(db, o.company_id, emp.id, blob, u["label"],
                                                       folder_name="Employee records", source="upload")
            applied[key] = True
            written.append(key)
    rv["_applied"] = applied
    o.review = rv
    flag_modified(o, "review")
    if all((rv.get(k) or {}).get("status") == "accepted" for k in review_keys(o)) and review_keys(o):
        o.review_status = "accepted"
    if written:
        _log(o, f"Accepted details written to the employee record ({len(written)} item(s))", "review")
    return {"written": written, "review_status": o.review_status}


def _save_tax(db: Session, company_id: int, emp, pdata: dict) -> None:
    """Tax regime and previous employer's income this year go to payroll's declaration when
    the payroll module is present; otherwise they stay on the onboarding record."""
    try:
        from modules.payroll.models import TaxDeclaration
    except ImportError:
        return
    from modules.payroll.calc import fy_of
    fy = fy_of(c.today(db, company_id))
    row = db.query(TaxDeclaration).filter(TaxDeclaration.company_id == company_id, TaxDeclaration.employee_id == emp.id,
                                          TaxDeclaration.fy == fy).first()
    if row is None:
        row = TaxDeclaration(company_id=company_id, employee_id=emp.id, fy=fy)
        db.add(row)
    regime = (pdata.get("tax") or {}).get("regime")
    if regime in ("old", "new") and not row.regime_locked:
        row.regime = regime
    prev = [p for p in pdata.get("previous_employment") or [] if p.get("fy_taxable_income") or p.get("fy_tds")]
    if prev:
        p = prev[0]
        row.previous_employer = {"income": str(p.get("fy_taxable_income") or 0), "tds": str(p.get("fy_tds") or 0),
                                 "pf": str(p.get("fy_pf") or 0), "pt": str(p.get("fy_pt") or 0),
                                 "employer_name": p.get("company_name")}
    db.flush()


# ── Joining documents ───────────────────────────────────────────────

def generate_appointment_letter(db: Session, o: Onboarding):
    from modules.documents import service as documents
    from modules.documents.models import Letter
    if o.appointment_letter_id:
        letter = db.get(Letter, o.appointment_letter_id)
        if letter is not None and letter.status != "cancelled":
            raise BadRequest("The appointment letter has already been issued.")
    emp = c.employee(db, o.company_id, o.employee_id)
    template = documents.default_template(db, o.company_id, "appointment")
    if template is None:
        raise BadRequest("Add an appointment letter template under Letters first.")
    values = {"job.joining_date": o.joining_date.isoformat()}
    if o.offer_id:
        from modules.recruitment.models import Offer
        from modules.recruitment import service as recruitment
        offer = db.get(Offer, o.offer_id)
        if offer is not None:
            values.update({k: v for k, v in recruitment.offer_values(db, offer).items()
                           if k.startswith("salary.") or k in ("job.probation_months", "job.notice_period_days")})
    if "salary.ctc" not in values:
        try:
            from modules.payroll import service as pay
            s = pay.current_salary(db, o.company_id, emp.id, o.joining_date)
            if s is not None and getattr(s, "annual_ctc", None):
                values["salary.ctc"] = str(s.annual_ctc)
        except Exception:
            pass
    if "salary.ctc" not in values:
        values["salary.ctc"] = "0"
    letter = documents.generate_letter(db, o.company_id, template, emp, values)
    o.appointment_letter_id = letter.id
    _log(o, "Appointment letter issued", "documents", letter=str(letter.uuid))
    return letter


def generate_forms(db: Session, o: Onboarding, kinds: list[str] | None = None) -> list[dict]:
    from modules.documents import service as documents, storage
    emp = c.employee(db, o.company_id, o.employee_id)
    kinds = kinds or (o.flow.statutory_forms if o.flow and o.flow.statutory_forms else list(sforms.FORMS))
    out = [f for f in (o.forms or []) if f["kind"] not in kinds]
    for kind in kinds:
        if kind not in sforms.FORMS:
            continue
        title, data = sforms.render(db, o.company_id, emp, kind)
        blob = storage.store(db, o.company_id, f"{title} - {emp.first_name} {emp.last_name}.pdf", data,
                             "application/pdf", "form")
        documents.file_into_employee_documents(db, o.company_id, emp.id, blob, title, folder_name="Employee records",
                                               source="letter")
        out.append({"kind": kind, "label": title, "file_id": blob.id, "file_uuid": str(blob.uuid),
                    "at": c.now().isoformat()})
    o.forms = out
    flag_modified(o, "forms")
    _log(o, f"Statutory forms generated: {', '.join(sforms.FORMS[k].split(':')[0] for k in kinds if k in sforms.FORMS)}",
         "documents")
    return out


def assign_asset(db: Session, o: Onboarding, asset_uuid) -> dict:
    from modules.assets import service as assets
    a = assets.get(db, o.company_id, asset_uuid)
    x = assets.assign(db, o.company_id, a, o.employee_id, assigned_on=max(o.joining_date, c.today(db, o.company_id)))
    reqs = list(o.account_requests or [])
    reqs.append({"kind": "asset", "label": f"{a.name} ({a.asset_tag})", "status": "assigned", "ref": str(x.uuid),
                 "at": c.now().isoformat()})
    o.account_requests = reqs
    flag_modified(o, "account_requests")
    _log(o, f"Asset assigned: {a.name} ({a.asset_tag})", "assets")
    return reqs[-1]


ACCOUNT_KINDS = {"email": ("Create work email account", "it"), "laptop": ("Prepare laptop", "it"),
                 "access": ("Set up system access (HRMS, tools)", "it"), "id_card": ("Print ID card", "admin"),
                 "desk": ("Allocate desk / seat", "admin"), "sim": ("Issue phone / SIM", "admin")}


def request_account(db: Session, o: Onboarding, kind: str, note: str | None = None) -> dict:
    from modules.tasks import service as tasks
    if kind not in ACCOUNT_KINDS:
        raise BadRequest("Unknown request.")
    emp = c.employee(db, o.company_id, o.employee_id)
    title, owner = ACCOUNT_KINDS[kind]
    conf = osettings.get(db, o.company_id)
    assignee = _owner_id(db, o, emp, owner, None, conf)
    due = max(o.joining_date - timedelta(days=1), c.today(db, o.company_id))
    t = tasks.create_task(db, o.company_id, title=f"{title} for {emp.first_name} {emp.last_name}",
                          assignee_employee_id=assignee, due_date=due, description=note,
                          labels=["onboarding", owner.upper() if owner == "it" else owner.title()],
                          source_module="onboarding", source_ref=str(o.uuid), priority="high", created_by_name="Onboarding")
    reqs = list(o.account_requests or [])
    reqs.append({"kind": kind, "label": title, "status": "requested", "task_uuid": str(t.uuid), "at": c.now().isoformat()})
    o.account_requests = reqs
    flag_modified(o, "account_requests")
    _log(o, f"Requested: {title}", "accounts")
    return reqs[-1]


# ── Day one, check-ins, completion ──────────────────────────────────

def start_day_one(db: Session, o: Onboarding, emp=None, quiet: bool = False) -> Onboarding:
    """On the joining date: the person becomes an employee on probation (or confirmed),
    day-one tasks and induction start, courses are assigned, welcome email and Buzz post
    go out and the 30/60/90-day check-ins are planned."""
    emp = emp or c.employee(db, o.company_id, o.employee_id)
    if o.status in ("completed", "cancelled"):
        raise BadRequest("This onboarding is closed.")
    if o.day_one_started_at:
        return o
    from modules.employees import service as people
    if emp.status == "pre_joining":
        probation_end = emp.probation_end_date
        target = "probation" if probation_end else "confirmed"
        people.set_status(db, o.company_id, emp, target, reason="Joined", effective=o.joining_date,
                          extra={"probation_end_date": probation_end} if probation_end else {"confirmation_date": o.joining_date})
    o.status, o.day_one_started_at = "in_progress", c.now()
    _create_tasks(db, o, emp, "day_one")
    _log(o, "Joined: day-one tasks and induction started", "day_one")
    try:
        from modules.probation import service as probation
        if emp.status == "probation":
            probation.start_for(db, o.company_id, emp)
    except ImportError:
        pass
    conf = osettings.get(db, o.company_id)
    _enrol_courses(db, o, emp)
    _plan_checkins(db, o, emp, conf)
    labels = c.org_labels(db, o.company_id, departments=[emp.department_id], designations=[emp.designation_id])
    fmt = {"first_name": emp.first_name, "name": f"{emp.first_name} {emp.last_name}",
           "joining_date": o.joining_date.strftime("%d %b %Y"),
           "designation": labels["designation"].get(emp.designation_id) or "a new team member",
           "department": labels["department"].get(emp.department_id) or "the team"}
    if conf.get("welcome_email") and not o.welcome_sent_at:
        from core.email import send_email
        send_email(emp.email, "Welcome aboard!", _fmt(conf.get("welcome_text"), fmt)
                   + "\n\nYour sign-in: use this email address and 'Forgot password' on the sign-in page to set your password.")
        o.welcome_sent_at = c.now()
    if conf.get("buzz_post") and not o.buzz_posted_at:
        try:
            from modules.engagement import service as engagement
            engagement.post_system_message(db, o.company_id, _fmt(conf.get("buzz_text"), fmt),
                                           subject_employee_id=emp.id)
            o.buzz_posted_at = c.now()
        except Exception:
            pass
    if emp.reporting_manager_id:
        c.notify(db, o.company_id, emp.reporting_manager_id, f"{emp.first_name} {emp.last_name} joins your team today.",
                 "onboarding")
    if o.buddy_employee_id:
        c.notify(db, o.company_id, o.buddy_employee_id, f"You are the buddy of {emp.first_name} {emp.last_name}, "
                                                        f"who joins today.", "onboarding")
    return o


def _fmt(text: str | None, values: dict) -> str:
    try:
        return (text or "").format(**values)
    except (KeyError, IndexError, ValueError):
        return text or ""


def _enrol_courses(db: Session, o: Onboarding, emp) -> int:
    ids = (o.flow.course_ids if o.flow else None) or []
    if not ids:
        return 0
    try:
        from modules.learning import service as learning
        from modules.learning.models import Course
    except ImportError:
        return 0
    n = 0
    for course in db.query(Course).filter(Course.company_id == o.company_id, Course.id.in_(ids)).all():
        try:
            with db.begin_nested():
                if learning.enrol(db, o.company_id, course, emp.id, source="assigned", by="Onboarding",
                                  mandatory=True) is not None:
                    n += 1
        except Exception:
            continue
    if n:
        _log(o, f"Enrolled in {n} induction course(s)", "learning")
    return n


def _plan_checkins(db: Session, o: Onboarding, emp, conf: dict) -> None:
    have = {ch.day for ch in db.query(OnboardingCheckin).filter(OnboardingCheckin.onboarding_id == o.id)}
    for day in conf.get("checkin_days") or []:
        if day in have:
            continue
        db.add(OnboardingCheckin(company_id=o.company_id, onboarding_id=o.id, employee_id=emp.id,
                                 manager_employee_id=emp.reporting_manager_id, day=int(day),
                                 due_on=o.joining_date + timedelta(days=int(day)), status="pending"))
    db.flush()


def save_checkin(db: Session, user: dict, ch: OnboardingCheckin, data: dict) -> OnboardingCheckin:
    eid = user.get("employee_id")
    if not (can_manage(db, user) or (eid and eid in (ch.manager_employee_id, ch.employee_id))):
        raise Forbidden("Only the manager or HR can record this check-in.")
    status = data.get("status") or "done"
    if status not in ("done", "skipped"):
        raise BadRequest("Mark the check-in done or skipped.")
    if data.get("rating") not in (None, ""):
        r = int(data["rating"])
        if not 1 <= r <= 5:
            raise BadRequest("Rate from 1 to 5.")
        ch.rating = r
    for k in ("went_well", "concerns", "support_needed"):
        if k in data:
            setattr(ch, k, (data[k] or "").strip() or None)
    if status == "done" and not (ch.went_well or ch.concerns or ch.rating):
        raise BadRequest("Add a rating or a few notes from the conversation.")
    ch.status, ch.completed_at, ch.completed_by = status, c.now(), c.actor_name()
    if ch.concerns:
        c.notify_hr(db, ch.company_id, f"Concern raised in a {ch.day}-day check-in. Open Onboarding to read it.", "onboarding")
    _log(ch.onboarding, f"{ch.day}-day check-in {status}", "checkin")
    return ch


def task_progress(db: Session, o: Onboarding) -> dict:
    from modules.tasks import service as tasks
    rows = tasks.tasks_for_source(db, o.company_id, "onboarding", str(o.uuid))
    names = c.names_for(db, o.company_id, [t.assignee_employee_id for t in rows])
    t0 = c.today(db, o.company_id)
    items = [{"uuid": str(t.uuid), "title": t.title, "status": t.status, "due_date": t.due_date,
              "assignee_name": names.get(t.assignee_employee_id), "labels": t.labels or [],
              "overdue": bool(t.due_date and t.due_date < t0 and t.status not in ("done", "cancelled"))} for t in rows]
    done = sum(1 for t in rows if t.status in ("done", "cancelled"))
    return {"items": items, "total": len(rows), "done": done, "overdue": sum(1 for i in items if i["overdue"])}


def complete(db: Session, o: Onboarding, force: bool = False) -> Onboarding:
    if o.status in ("completed", "cancelled"):
        raise BadRequest("This onboarding is already closed.")
    conf = osettings.get(db, o.company_id)
    if conf.get("require_appointment_letter", True):
        from modules.documents.models import Letter
        letter = db.get(Letter, o.appointment_letter_id) if o.appointment_letter_id else None
        if letter is None or letter.status in ("cancelled", "declined"):
            raise BadRequest("Issue the appointment letter first. The law requires one for every employee.")
    if not o.day_one_started_at:
        raise BadRequest("The person has not joined yet.")
    prog = task_progress(db, o)
    if prog["done"] < prog["total"] and not force:
        raise BadRequest(f"{prog['total'] - prog['done']} checklist task(s) are still open. Finish them or close anyway.")
    o.status, o.completed_at, o.completed_by = "completed", c.now(), c.actor_name()
    o.portal_enabled = False
    _log(o, "Onboarding completed" + (" with open tasks" if prog["done"] < prog["total"] else ""), "complete")
    audit.event(db, "onboarding.completed", "onboardings", o.id, {"employee_id": o.employee_id},
                entity_uuid=str(o.uuid), entity_label="Onboarding")
    return o


def cancel(db: Session, o: Onboarding, reason: str | None) -> Onboarding:
    """The joiner did not join: the pre-joining record is archived (never deleted)."""
    if o.status in ("completed", "cancelled"):
        raise BadRequest("This onboarding is already closed.")
    if not (reason or "").strip():
        raise BadRequest("Say why the onboarding is cancelled.")
    from modules.employees import service as people
    from modules.tasks import service as tasks
    o.status, o.cancel_reason, o.portal_enabled = "cancelled", reason.strip()[:300], False
    for t in tasks.tasks_for_source(db, o.company_id, "onboarding", str(o.uuid)):
        if t.status not in ("done", "cancelled"):
            t.status = "cancelled"
    emp = c.employee(db, o.company_id, o.employee_id)
    if emp is not None and emp.status == "pre_joining":
        people.archive(db, o.company_id, emp, f"Did not join: {o.cancel_reason}")
    _log(o, f"Cancelled: {o.cancel_reason}", "cancel")
    return o


# ── Lists, dashboard, timeline ──────────────────────────────────────

def out(db: Session, o: Onboarding, *, full: bool = False, emp=None, names: dict | None = None,
        progress: dict | None = None) -> dict:
    emp = emp or c.employee(db, o.company_id, o.employee_id)
    names = names if names is not None else c.names_for(db, o.company_id, [o.buddy_employee_id, o.hr_owner_employee_id,
                                                                           emp.reporting_manager_id if emp else None])
    labels = c.org_labels(db, o.company_id, departments=[emp.department_id], designations=[emp.designation_id],
                          locations=[emp.location_id])
    t0 = c.today(db, o.company_id)
    comp = completeness(o)
    base = {"uuid": str(o.uuid), "employee_id": o.employee_id, "employee_uuid": str(emp.uuid),
            "name": f"{emp.first_name} {emp.last_name}".strip(), "email": emp.email, "employee_code": emp.employee_code,
            "designation": labels["designation"].get(emp.designation_id),
            "department": labels["department"].get(emp.department_id),
            "location": (labels["location"].get(emp.location_id) or (None,))[0],
            "joining_date": o.joining_date, "days_to_join": (o.joining_date - t0).days,
            "status": o.status, "status_label": STATUSES.get(o.status), "employee_status": emp.status,
            "flow_name": o.flow.name if o.flow else None, "flow_uuid": str(o.flow.uuid) if o.flow else None,
            "buddy_employee_id": o.buddy_employee_id, "buddy_name": names.get(o.buddy_employee_id),
            "hr_owner_employee_id": o.hr_owner_employee_id, "hr_owner_name": names.get(o.hr_owner_employee_id),
            "manager_name": names.get(emp.reporting_manager_id),
            "portal_submitted_at": o.portal_submitted_at, "review_status": o.review_status,
            "portal_percent": comp["percent"], "appointment_letter": bool(o.appointment_letter_id),
            "forms_count": len(o.forms or []), "completed_at": o.completed_at, "created_at": o.created_at,
            "tasks": progress or None}
    if full:
        from modules.documents import service as documents
        from modules.documents.models import Letter
        base.update({
            "portal_link": portal_link(db, o) if o.portal_enabled else None, "portal_enabled": o.portal_enabled,
            "portal_expires_at": o.portal_expires_at, "portal_opened_at": o.portal_opened_at,
            "portal_data": _masked(o.portal_data or {}), "review": {k: v for k, v in (o.review or {}).items() if k != "_applied"},
            "applied": (o.review or {}).get("_applied") or {}, "review_keys": review_keys(o), "completeness": comp,
            "sections": [{"key": s, "label": osettings.SECTIONS[s]} for s in sections_for(o)],
            "required_documents": (o.flow.documents if o.flow else []) or [],
            "uploads": [{**u, "file": documents.file_link(db, o.company_id, u["file_id"])} for u in o.uploads or []],
            "forms": [{**f, "file": documents.file_link(db, o.company_id, f["file_id"])} for f in o.forms or []],
            "account_requests": o.account_requests or [],
            "induction": [{**s, "date": (o.joining_date + timedelta(days=int(s.get("day", 1)) - 1)).isoformat()}
                          for s in (o.flow.induction if o.flow else []) or []],
            "checkins": [checkin_out(ch, names) for ch in o.checkins],
            "history": list(reversed(o.history or [])), "cancel_reason": o.cancel_reason,
            "welcome_sent_at": o.welcome_sent_at, "buzz_posted_at": o.buzz_posted_at,
            "day_one_started_at": o.day_one_started_at, "stage_times": stage_times(o),
        })
        if o.appointment_letter_id:
            letter = db.get(Letter, o.appointment_letter_id)
            if letter is not None:
                base["appointment"] = {"uuid": str(letter.uuid), "status": letter.status, "reference_no": letter.reference_no,
                                       "file": documents.file_link(db, o.company_id, letter.stored_file_id)}
        base["tasks"] = task_progress(db, o)
    return base


def _masked(pdata: dict) -> dict:
    """Aadhaar and bank account numbers are shown masked."""
    from modules.employees.validators import mask
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in pdata.items()}
    if isinstance(out.get("statutory"), dict) and out["statutory"].get("aadhaar"):
        out["statutory"]["aadhaar"] = mask(out["statutory"]["aadhaar"])
    if isinstance(out.get("bank"), dict) and out["bank"].get("account_number"):
        out["bank"]["account_number"] = mask(out["bank"]["account_number"])
    return out


def stage_times(o: Onboarding) -> list[dict]:
    """Time spent in each stage, for the onboarding timeline."""
    marks = [("Offer accepted to link opened", o.created_at, o.portal_opened_at),
             ("Filling in details", o.portal_opened_at, o.portal_submitted_at),
             ("Pre-joining to day one", o.created_at, o.day_one_started_at),
             ("Day one to completion", o.day_one_started_at, o.completed_at)]
    out = []
    for label, a, b in marks:
        if a and b:
            out.append({"label": label, "days": round((b - a).total_seconds() / 86400, 1)})
    if out:
        longest = max(out, key=lambda x: x["days"])
        longest["longest"] = True
    return out


def checkin_out(ch: OnboardingCheckin, names: dict | None = None) -> dict:
    return {"uuid": str(ch.uuid), "day": ch.day, "due_on": ch.due_on, "status": ch.status, "rating": ch.rating,
            "went_well": ch.went_well, "concerns": ch.concerns, "support_needed": ch.support_needed,
            "completed_at": ch.completed_at, "completed_by": ch.completed_by,
            "manager_employee_id": ch.manager_employee_id,
            "manager_name": (names or {}).get(ch.manager_employee_id)}


def dashboard(db: Session, company_id: int) -> dict:
    t0 = c.today(db, company_id)
    q = db.query(Onboarding).filter(Onboarding.company_id == company_id)
    by_status = dict(db.query(Onboarding.status, func.count(Onboarding.id)).filter(Onboarding.company_id == company_id)
                     .group_by(Onboarding.status).all())
    joining_week = q.filter(Onboarding.status == "pre_joining", Onboarding.joining_date >= t0,
                            Onboarding.joining_date <= t0 + timedelta(days=7)).count()
    to_review = q.filter(Onboarding.review_status == "submitted",
                         Onboarding.status.in_(("pre_joining", "in_progress"))).count()
    no_letter = q.filter(Onboarding.status == "in_progress", Onboarding.appointment_letter_id.is_(None)).count()
    checkins_due = db.query(func.count(OnboardingCheckin.id)).filter(
        OnboardingCheckin.company_id == company_id, OnboardingCheckin.status == "pending",
        OnboardingCheckin.due_on <= t0 + timedelta(days=7)).scalar()
    return {"pre_joining": by_status.get("pre_joining", 0), "in_progress": by_status.get("in_progress", 0),
            "completed": by_status.get("completed", 0), "joining_this_week": joining_week,
            "to_review": to_review, "missing_appointment_letter": no_letter, "checkins_due": checkins_due}


# ── Background jobs ─────────────────────────────────────────────────

def run_daily(db: Session) -> int:
    """Day-one switch on the joining date, portal reminders and check-in reminders."""
    n = 0
    for o in db.query(Onboarding).filter(Onboarding.status == "pre_joining").all():
        db.info["company_id"] = o.company_id
        conf = osettings.get(db, o.company_id)
        t0 = c.today(db, o.company_id)
        if conf.get("auto_start_day_one", True) and o.joining_date <= t0:
            try:
                with db.begin_nested():
                    start_day_one(db, o)
                n += 1
            except Exception:
                pass
            continue
        if o.portal_submitted_at or not o.portal_enabled or (o.portal_expires_at and o.portal_expires_at < c.now()):
            continue
        first = o.created_at + timedelta(days=int(conf["reminder_first_after_days"]))
        every = timedelta(days=max(1, int(conf["reminder_every_days"])))
        last = o.last_reminded_at or o.created_at
        if c.now() >= first and c.now() - last >= every:
            send_portal_invite(db, o, reminder=True)
            n += 1
    for ch in db.query(OnboardingCheckin).filter(OnboardingCheckin.status == "pending").all():
        db.info["company_id"] = ch.company_id
        t0 = c.today(db, ch.company_id)
        if ch.due_on - timedelta(days=2) <= t0 and ch.reminded_on != t0 and (ch.reminded_on is None or ch.due_on <= t0):
            if ch.reminded_on is not None and ch.due_on > t0:
                continue
            emp = c.employee(db, ch.company_id, ch.employee_id)
            if emp is None:
                continue
            target = ch.manager_employee_id
            msg = f"{ch.day}-day check-in with {emp.first_name} {emp.last_name} is due on {ch.due_on.strftime('%d %b')}."
            if target:
                c.notify(db, ch.company_id, target, msg, "onboarding")
            else:
                c.notify_hr(db, ch.company_id, msg, "onboarding")
            ch.reminded_on = t0
            n += 1
    return n
