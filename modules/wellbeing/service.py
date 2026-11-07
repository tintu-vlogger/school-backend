"""Wellbeing rules: BMI, infirmary notices to parents, points and house totals, gate passes.

Functions other modules may use:
    bmi(height_cm, weight_kg)                        -> Decimal | None
    points_total(db, company_id, student_id)          -> int
    house_totals(db, company_id, since=None)          -> [{house_id, name, color, points}]
    authorised_pickups(db, company_id, student_id)    -> [StudentGuardian] with can_pickup

Rules:
- A gate pass for early pickup is issued only to a guardian linked to the learner with
  "can pick up" switched on; the parents are told at once.
- Confidential incidents are shown only to people with wellbeing.manage.
- Counselling notes need wellbeing.counselling (separate from everything else).
- An infirmary visit marked "parent informed" (or "sent home") notifies the parents.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core.context import current as current_ctx
from modules.wellbeing.models import WbHealthRecord, WbIncidentType, WbPoint

DEFAULT_TYPES = [("Late to class", "low", -1), ("Uniform not as per rules", "low", -1), ("Homework not done", "low", -1),
                 ("Disrupting class", "medium", -2), ("Use of mobile phone", "medium", -3), ("Bullying", "high", -5),
                 ("Fighting", "high", -5), ("Damage to property", "high", -4), ("Academic dishonesty", "high", -5),
                 ("Other", "low", 0)]
MERIT_CATEGORIES = ["Academics", "Sports", "Arts", "Conduct", "Leadership", "Service", "Other"]


def actor() -> str:
    return current_ctx().actor_name or "System"


def today(db: Session, company_id: int) -> date:
    from core.timeutil import today_for_company_id
    return today_for_company_id(db, company_id)


def ensure_defaults(db: Session, company_id: int) -> None:
    if db.query(WbIncidentType.id).filter(WbIncidentType.company_id == company_id).first():
        return
    for name, sev, pts in DEFAULT_TYPES:
        db.add(WbIncidentType(company_id=company_id, name=name, default_severity=sev, default_points=pts))
    db.flush()


def bmi(height_cm, weight_kg) -> Decimal | None:
    if not height_cm or not weight_kg:
        return None
    h = Decimal(height_cm) / 100
    if h <= 0:
        return None
    return (Decimal(weight_kg) / (h * h)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def bmi_band(value: Decimal | None) -> str | None:
    """Adult cut-offs as a rough guide; children are judged on growth charts by the doctor."""
    if value is None:
        return None
    if value < Decimal("18.5"):
        return "Underweight"
    if value < 25:
        return "Healthy"
    if value < 30:
        return "Overweight"
    return "Obese"


def health_record(db: Session, company_id: int, student_id: int, create: bool = False) -> WbHealthRecord | None:
    r = db.query(WbHealthRecord).filter(WbHealthRecord.company_id == company_id,
                                        WbHealthRecord.student_id == student_id).first()
    if r is None and create:
        r = WbHealthRecord(company_id=company_id, student_id=student_id)
        db.add(r)
        db.flush()
    return r


def points_total(db: Session, company_id: int, student_id: int) -> int:
    return int(db.query(func.coalesce(func.sum(WbPoint.points), 0)).filter(
        WbPoint.company_id == company_id, WbPoint.student_id == student_id, WbPoint.voided.is_(False)).scalar() or 0)


def house_totals(db: Session, company_id: int, since: date | None = None) -> list[dict]:
    from modules.academics.models import House
    from modules.students.models import Student
    q = (db.query(Student.house_id, func.coalesce(func.sum(WbPoint.points), 0),
                  func.coalesce(func.sum(func.greatest(WbPoint.points, 0)), 0),
                  func.coalesce(func.sum(func.least(WbPoint.points, 0)), 0))
         .join(Student, Student.id == WbPoint.student_id)
         .filter(WbPoint.company_id == company_id, WbPoint.voided.is_(False), Student.house_id.isnot(None)))
    if since:
        q = q.filter(WbPoint.awarded_on >= since)
    totals = {hid: (int(t), int(m), int(d)) for hid, t, m, d in q.group_by(Student.house_id).all()}
    houses = db.query(House).filter(House.company_id == company_id, House.active.is_(True)).order_by(House.name).all()
    out = [{"house_id": h.id, "name": h.name, "color": h.color, "points": totals.get(h.id, (0, 0, 0))[0],
            "merits": totals.get(h.id, (0, 0, 0))[1], "demerits": totals.get(h.id, (0, 0, 0))[2]} for h in houses]
    return sorted(out, key=lambda x: -x["points"])


def authorised_pickups(db: Session, company_id: int, student_id: int):
    from modules.students.models import StudentGuardian
    return (db.query(StudentGuardian).options(joinedload(StudentGuardian.guardian))
            .filter(StudentGuardian.company_id == company_id, StudentGuardian.student_id == student_id,
                    StudentGuardian.can_pickup.is_(True)).all())


def check_pickup(db: Session, company_id: int, student_id: int, guardian_id: int):
    from modules.students.models import Student, StudentGuardian
    s = db.query(Student).filter(Student.company_id == company_id, Student.id == student_id).first()
    if s is None:
        raise HTTPException(status_code=400, detail="The student was not found.")
    link = (db.query(StudentGuardian).options(joinedload(StudentGuardian.guardian))
            .filter(StudentGuardian.company_id == company_id, StudentGuardian.student_id == student_id,
                    StudentGuardian.guardian_id == guardian_id).first())
    if link is None:
        raise HTTPException(status_code=400, detail=f"This person is not a guardian of {s.full_name}. Early pickup is refused.")
    if not link.can_pickup:
        raise HTTPException(status_code=400, detail=f"{link.guardian.full_name} is not allowed to pick up {s.full_name}. Early pickup is refused.")
    return s, link


def tell_parents(db: Session, company_id: int, student_id: int, kind: str, title: str, body: str, link: str) -> int:
    try:
        from modules.notifications.service import notify_portal
        from modules.students.service import portal_account_ids_for_students
        ids = portal_account_ids_for_students(db, company_id, [student_id], parents=True, students=False)
        if ids:
            return notify_portal(db, company_id, ids, kind=kind, title=title, body=body, link=link)
    except Exception:
        pass
    return 0


def new_pass_no(db: Session, company_id: int, key: str, prefix: str) -> str | None:
    try:
        from core.numbering import next_number
        return next_number(db, company_id, key)
    except Exception:
        return None
