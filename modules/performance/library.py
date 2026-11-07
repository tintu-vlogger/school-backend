"""Libraries used by reviews: rating scales, KRAs (tagged by department, designation and
location so they are assigned automatically), competencies (with behaviours per level
and linked skills) and review templates."""
from __future__ import annotations

import uuid as uuid_mod
from decimal import Decimal

from sqlalchemy.orm import Session

from core.errors import BadRequest
from modules.performance.common import Actor, emp_model, employee, name_of
from modules.performance.models import Competency, EmployeeKra, Kra, RatingScale, ReviewTemplate

SECTION_KINDS = ("goals", "kras", "competencies", "questions")


# ── Rating scales ───────────────────────────────────────────────────

def clean_points(points: list[dict]) -> list[dict]:
    if not points or not (3 <= len(points) <= 10):
        raise BadRequest("A rating scale has between 3 and 10 points.")
    out, seen = [], set()
    for p in points:
        try:
            v = float(p.get("value"))
        except (TypeError, ValueError):
            raise BadRequest("Every point needs a number.")
        label = (p.get("label") or "").strip()
        if not label:
            raise BadRequest("Every point needs a name.")
        if v in seen:
            raise BadRequest("Two points have the same number.")
        seen.add(v)
        out.append({"value": v, "label": label[:50], "description": (p.get("description") or "")[:300],
                    "color": p.get("color") or None})
    return sorted(out, key=lambda x: x["value"])


def scale_out(s: RatingScale) -> dict:
    return {"id": s.id, "uuid": str(s.uuid), "name": s.name, "description": s.description, "points": s.points or [],
            "is_default": s.is_default, "active": s.active}


def save_scale(db: Session, actor: Actor, data: dict, row: RatingScale | None = None) -> RatingScale:
    actor.require()
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Give the scale a name.")
    if row is None:
        row = RatingScale(company_id=actor.company_id)
        db.add(row)
    row.name, row.description = name[:120], data.get("description")
    row.points = clean_points(data.get("points") or [])
    if "active" in data:
        row.active = bool(data["active"])
    if data.get("is_default"):
        for other in db.query(RatingScale).filter(RatingScale.company_id == actor.company_id, RatingScale.is_default.is_(True)).all():
            other.is_default = False
        row.is_default = True
    db.flush()
    return row


# ── KRAs ────────────────────────────────────────────────────────────

def clean_tags(tags: dict | None) -> dict:
    tags = tags or {}
    return {k: [int(x) for x in (tags.get(k) or []) if str(x).isdigit()] for k in ("department_ids", "designation_ids", "location_ids")}


def tags_match(tags: dict | None, emp) -> bool:
    """Empty tag lists mean 'everyone'; a set list must contain the employee's value."""
    tags = tags or {}
    checks = (("department_ids", emp.department_id), ("designation_ids", emp.designation_id),
              ("location_ids", getattr(emp, "location_id", None)))
    for key, val in checks:
        wanted = tags.get(key) or []
        if wanted and val not in wanted:
            return False
    return True


def kra_out(k: Kra) -> dict:
    return {"id": k.id, "uuid": str(k.uuid), "title": k.title, "description": k.description, "measure": k.measure,
            "weight": float(k.weight), "tags": k.tags or {}, "active": k.active}


def save_kra(db: Session, actor: Actor, data: dict, row: Kra | None = None) -> Kra:
    actor.require()
    title = (data.get("title") or "").strip()
    if not title:
        raise BadRequest("Give the KRA a title.")
    w = Decimal(str(data.get("weight") if data.get("weight") is not None else 20))
    if w <= 0 or w > 100:
        raise BadRequest("Weight must be between 1 and 100.")
    if row is None:
        row = Kra(company_id=actor.company_id)
        db.add(row)
    row.title, row.description, row.measure, row.weight = title[:200], data.get("description"), data.get("measure"), w
    row.tags = clean_tags(data.get("tags"))
    if "active" in data:
        row.active = bool(data["active"])
    db.flush()
    return row


def auto_assign_kras(db: Session, company_id: int, employee_ids: list[int] | None = None) -> int:
    """Give every active employee the KRAs whose tags match them (keeps manual ones)."""
    E = emp_model()
    kras = db.query(Kra).filter(Kra.company_id == company_id, Kra.active.is_(True)).all()
    q = db.query(E).filter(E.company_id == company_id, E.active.is_(True))
    if employee_ids:
        q = q.filter(E.id.in_(employee_ids))
    have = {(r.employee_id, r.kra_id) for r in db.query(EmployeeKra).filter(EmployeeKra.company_id == company_id).all()}
    n = 0
    for emp in q.all():
        for k in kras:
            if (emp.id, k.id) not in have and tags_match(k.tags, emp) and any((k.tags or {}).get(x) for x in ("department_ids", "designation_ids", "location_ids")):
                db.add(EmployeeKra(company_id=company_id, employee_id=emp.id, kra_id=k.id, weight=k.weight, source="auto"))
                n += 1
    db.flush()
    return n


def employee_kras(db: Session, company_id: int, employee_id: int) -> list[dict]:
    """KRAs on a person's sheet: assigned ones, else the library KRAs that match them."""
    rows = (db.query(EmployeeKra).join(Kra, Kra.id == EmployeeKra.kra_id)
            .filter(EmployeeKra.company_id == company_id, EmployeeKra.employee_id == employee_id, Kra.active.is_(True)).all())
    if rows:
        return [{**kra_out(r.kra), "weight": float(r.weight), "source": r.source, "assignment_uuid": str(r.uuid)} for r in rows]
    emp = employee(db, company_id, employee_id)
    return [{**kra_out(k), "source": "library"} for k in db.query(Kra).filter(Kra.company_id == company_id, Kra.active.is_(True)).all()
            if tags_match(k.tags, emp) and any((k.tags or {}).get(x) for x in ("department_ids", "designation_ids", "location_ids"))]


def set_employee_kras(db: Session, actor: Actor, employee_id: int, items: list[dict]) -> list[dict]:
    from modules.performance.common import is_manager_of
    if not actor.is_admin and not is_manager_of(db, actor, employee_id):
        raise BadRequest("Only administrators and the person's manager can change their KRAs.")
    employee(db, actor.company_id, employee_id)
    total = sum(float(i.get("weight") or 0) for i in items)
    if items and abs(total - 100) > 0.01:
        raise BadRequest(f"KRA weights must add up to 100% (now {total:g}%).")
    db.query(EmployeeKra).filter(EmployeeKra.company_id == actor.company_id, EmployeeKra.employee_id == employee_id).delete()
    for i in items:
        k = db.query(Kra).filter(Kra.company_id == actor.company_id, Kra.id == i.get("kra_id")).first()
        if k is None:
            raise BadRequest("A KRA was not found.")
        db.add(EmployeeKra(company_id=actor.company_id, employee_id=employee_id, kra_id=k.id,
                           weight=Decimal(str(i.get("weight") or k.weight)), source="manual"))
    db.flush()
    return employee_kras(db, actor.company_id, employee_id)


# ── Competencies ────────────────────────────────────────────────────

def competency_out(c: Competency) -> dict:
    return {"id": c.id, "uuid": str(c.uuid), "name": c.name, "category": c.category, "description": c.description,
            "rating_scale_id": c.rating_scale_id, "weight": float(c.weight), "behaviours": c.behaviours or [],
            "skill_ids": c.skill_ids or [], "tags": c.tags or {}, "active": c.active}


def save_competency(db: Session, actor: Actor, data: dict, row: Competency | None = None) -> Competency:
    actor.require()
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Give the competency a name.")
    if row is None:
        row = Competency(company_id=actor.company_id)
        db.add(row)
    row.name, row.category, row.description = name[:200], (data.get("category") or None), data.get("description")
    row.rating_scale_id = data.get("rating_scale_id") or None
    row.weight = Decimal(str(data.get("weight") or 1))
    row.behaviours = [{"value": float(b.get("value")), "text": (b.get("text") or "")[:500]}
                      for b in (data.get("behaviours") or []) if b.get("value") is not None and (b.get("text") or "").strip()]
    row.skill_ids = [int(x) for x in (data.get("skill_ids") or []) if str(x).isdigit()]
    row.tags = clean_tags(data.get("tags"))
    if "active" in data:
        row.active = bool(data["active"])
    db.flush()
    return row


def competencies_for(db: Session, company_id: int, emp) -> list[Competency]:
    return [c for c in db.query(Competency).filter(Competency.company_id == company_id, Competency.active.is_(True))
            .order_by(Competency.category, Competency.name).all() if tags_match(c.tags, emp)]


# ── Templates ───────────────────────────────────────────────────────

def clean_sections(sections: list[dict]) -> list[dict]:
    if not sections:
        raise BadRequest("Add at least one section.")
    out, keys = [], set()
    for i, s in enumerate(sections, start=1):
        kind = s.get("kind")
        if kind not in SECTION_KINDS:
            raise BadRequest(f"Section {i}: pick goals, KRAs, competencies or questions.")
        key = (s.get("key") or f"{kind}{i}").strip()
        if key in keys:
            key = f"{key}{i}"
        keys.add(key)
        qs = []
        for j, q in enumerate(s.get("questions") or [], start=1):
            t = (q.get("text") or "").strip()
            if not t:
                continue
            qtype = q.get("type") or "text"
            if qtype not in ("rating", "text"):
                raise BadRequest(f"Section {i}, question {j}: type must be rating or text.")
            qs.append({"id": q.get("id") or uuid_mod.uuid4().hex[:8], "text": t[:500], "type": qtype,
                       "required": bool(q.get("required", True))})
        if kind == "questions" and not qs:
            raise BadRequest(f"Section {i}: add at least one question.")
        w = float(s.get("weight") or 0)
        if w < 0 or w > 100:
            raise BadRequest(f"Section {i}: weight must be between 0 and 100.")
        out.append({"key": key, "kind": kind, "title": (s.get("title") or kind.capitalize()).strip()[:150],
                    "description": (s.get("description") or "")[:500], "weight": w, "questions": qs})
    total = sum(s["weight"] for s in out)
    if abs(total - 100) > 0.01:
        raise BadRequest(f"Section weights must add up to 100% (now {total:g}%).")
    return out


def template_out(t: ReviewTemplate) -> dict:
    return {"id": t.id, "uuid": str(t.uuid), "name": t.name, "description": t.description,
            "rating_scale_id": t.rating_scale_id, "sections": t.sections or [], "active": t.active}


def save_template(db: Session, actor: Actor, data: dict, row: ReviewTemplate | None = None) -> ReviewTemplate:
    actor.require()
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Give the template a name.")
    if row is None:
        row = ReviewTemplate(company_id=actor.company_id)
        db.add(row)
    row.name, row.description = name[:200], data.get("description")
    row.rating_scale_id = data.get("rating_scale_id") or None
    row.sections = clean_sections(data.get("sections") or [])
    if "active" in data:
        row.active = bool(data["active"])
    db.flush()
    return row
