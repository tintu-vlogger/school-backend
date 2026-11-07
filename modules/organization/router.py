"""Organisation API: /organization/... (legal entities, regions, locations, grades, cost
centres, teams, tags, skills, org chart). Lists are open to everyone signed in (they feed
pickers); changes need the matching permission."""
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.db import get_db
from core.masters import labels_for
from core.security import _get_role_permissions, get_current_user, require_permission
from modules.employees import validators as v
from modules.organization import schemas as s
from modules.organization import service
from modules.organization.models import (CostCentre, EmployeeSkill, Grade, LegalEntity, Location, Region, Skill, Tag,
                                         Team, TeamMember)

router = APIRouter(prefix="/organization", tags=["Organisation"])

ORG = require_permission("settings.organization.manage")
TEAMS = require_permission("people.teams.manage")
TAGS = require_permission("people.tags.manage")
SKILLS = require_permission("people.skills.manage")


def _perms(db: Session, user: dict) -> list[str]:
    return _get_role_permissions(user["company_id"], user["role"], db)


def _emp_names(db: Session, company_id: int, ids) -> dict[int, dict]:
    from modules.employees.models import Employee
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {e.id: {"id": e.id, "uuid": str(e.uuid), "name": f"{e.first_name} {e.last_name}".strip(),
                   "photo": e.profile_image_url}
            for e in db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(ids))}


def _active_counts(db: Session, company_id: int, column) -> dict[int, int]:
    from modules.employees.models import Employee
    return dict(db.query(column, func.count(Employee.id))
                .filter(Employee.company_id == company_id, Employee.active.is_(True), column.isnot(None))
                .group_by(column).all())


# ── Legal entities ───────────────────────────────────────────────────

def _entity_out(e: LegalEntity, states: dict, banks: dict, counts: dict, full: bool) -> dict:
    accounts = [{**a, "bank_name": banks.get(a.get("bank_id")),
                 "account_number": a["account_number"] if full else v.mask(a.get("account_number"))}
                for a in (e.bank_accounts or [])]
    return {
        "id": e.id, "uuid": str(e.uuid), "name": e.name, "legal_name": e.legal_name, "code": e.code,
        "pan": e.pan, "tan": e.tan, "cin": e.cin, "gstin": e.gstin, "pf_code": e.pf_code, "esi_code": e.esi_code,
        "lin": e.lin, "incorporation_date": e.incorporation_date, "address_line1": e.address_line1,
        "address_line2": e.address_line2, "city": e.city, "state_id": e.state_id, "state_name": states.get(e.state_id),
        "pincode": e.pincode, "email": e.email, "phone": e.phone,
        "registrations": [{**r, "state_name": states.get(r.get("state_id")),
                           "kind_label": service.REGISTRATION_KINDS.get(r.get("kind"))} for r in (e.registrations or [])],
        "bank_accounts": accounts, "signatory": e.signatory or {}, "is_primary": e.is_primary, "active": e.active,
        "custom": e.custom or {},
        "employee_count": counts.get(e.id, 0), "created_at": e.created_at, "updated_at": e.updated_at,
    }


def _entity_lookups(db: Session, company_id: int, rows: list[LegalEntity]):
    state_ids = [r.state_id for r in rows] + [g.get("state_id") for r in rows for g in (r.registrations or [])]
    bank_ids = [a.get("bank_id") for r in rows for a in (r.bank_accounts or [])]
    return labels_for(db, company_id, [i for i in state_ids if i]), labels_for(db, company_id, [i for i in bank_ids if i])


@router.get("/legal-entities")
def list_entities(active: bool | None = None, q: str | None = None, db: Session = Depends(get_db),
                  user: dict = Depends(get_current_user)):
    query = db.query(LegalEntity).filter(LegalEntity.company_id == user["company_id"])
    if active is not None:
        query = query.filter(LegalEntity.active.is_(active))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(LegalEntity.name.ilike(like), LegalEntity.legal_name.ilike(like),
                                 LegalEntity.pan.ilike(like), LegalEntity.code.ilike(like)))
    rows = query.order_by(LegalEntity.is_primary.desc(), LegalEntity.name).all()
    from modules.employees.models import Employee
    counts = _active_counts(db, user["company_id"], Employee.legal_entity_id)
    states, banks = _entity_lookups(db, user["company_id"], rows)
    full = "settings.organization.manage" in _perms(db, user)
    return [_entity_out(r, states, banks, counts, full) for r in rows]


@router.get("/legal-entities/{uuid}")
def get_entity(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    row = service.get_by_uuid(db, LegalEntity, user["company_id"], uuid, "Legal entity")
    from modules.employees.models import Employee
    states, banks = _entity_lookups(db, user["company_id"], [row])
    return _entity_out(row, states, banks, _active_counts(db, user["company_id"], Employee.legal_entity_id),
                       "settings.organization.manage" in _perms(db, user))


@router.post("/legal-entities", status_code=201)
def create_entity(body: s.LegalEntityIn, db: Session = Depends(get_db), user: dict = Depends(ORG)):
    row = service.save_entity(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return get_entity(row.uuid, db, user)


@router.put("/legal-entities/{uuid}")
def update_entity(uuid: UUID, body: s.LegalEntityIn, db: Session = Depends(get_db), user: dict = Depends(ORG)):
    row = service.get_by_uuid(db, LegalEntity, user["company_id"], uuid, "Legal entity")
    data = body.model_dump(exclude_unset=True)
    # Masked account numbers sent back unchanged keep the stored number.
    if "bank_accounts" in data:
        stored = {(a.get("ifsc"), v.mask(a.get("account_number"))): a.get("account_number") for a in row.bank_accounts or []}
        for a in data["bank_accounts"]:
            if "•" in (a.get("account_number") or ""):
                a["account_number"] = stored.get((a.get("ifsc"), a["account_number"]), a["account_number"])
    service.save_entity(db, user["company_id"], data, row)
    db.commit()
    return get_entity(uuid, db, user)


# ── Regions ──────────────────────────────────────────────────────────

def _region_out(r: Region, heads: dict, loc_counts: dict) -> dict:
    return {"id": r.id, "uuid": str(r.uuid), "name": r.name, "code": r.code, "description": r.description,
            "head_employee_id": r.head_employee_id, "head": heads.get(r.head_employee_id), "active": r.active,
            "location_count": loc_counts.get(r.id, 0), "created_at": r.created_at}


@router.get("/regions")
def list_regions(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    rows = db.query(Region).filter(Region.company_id == cid).order_by(Region.name).all()
    counts = dict(db.query(Location.region_id, func.count(Location.id)).filter(Location.company_id == cid,
                                                                             Location.active.is_(True))
                  .group_by(Location.region_id).all())
    heads = _emp_names(db, cid, [r.head_employee_id for r in rows])
    return [_region_out(r, heads, counts) for r in rows]


@router.post("/regions", status_code=201)
def create_region(body: s.RegionIn, db: Session = Depends(get_db), user: dict = Depends(ORG)):
    row = service.save_region(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return _region_out(row, _emp_names(db, user["company_id"], [row.head_employee_id]), {})


@router.put("/regions/{uuid}")
def update_region(uuid: UUID, body: s.RegionIn, db: Session = Depends(get_db), user: dict = Depends(ORG)):
    row = service.get_by_uuid(db, Region, user["company_id"], uuid, "Region")
    service.save_region(db, user["company_id"], body.model_dump(exclude_unset=True), row)
    db.commit()
    return _region_out(row, _emp_names(db, user["company_id"], [row.head_employee_id]), {})


# ── Locations ────────────────────────────────────────────────────────

def _location_out(r: Location, states: dict, counts: dict) -> dict:
    return {
        "id": r.id, "uuid": str(r.uuid), "name": r.name, "code": r.code, "kind": r.kind,
        "kind_label": service.LOCATION_KINDS.get(r.kind, r.kind), "region_id": r.region_id,
        "region_name": r.region.name if r.region else None, "legal_entity_id": r.legal_entity_id,
        "legal_entity_name": r.legal_entity.name if r.legal_entity else None, "address_line1": r.address_line1,
        "address_line2": r.address_line2, "city": r.city, "state_id": r.state_id, "state_name": states.get(r.state_id),
        "pincode": r.pincode, "timezone": r.timezone,
        "latitude": float(r.latitude) if r.latitude is not None else None,
        "longitude": float(r.longitude) if r.longitude is not None else None, "radius_m": r.radius_m,
        "holiday_calendar_id": r.holiday_calendar_id, "phone": r.phone, "email": r.email, "active": r.active,
        "custom": r.custom or {},
        "employee_count": counts.get(r.id, 0), "created_at": r.created_at,
    }


@router.get("/locations")
def list_locations(active: bool | None = None, region_id: int | None = None, q: str | None = None,
                   db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    query = (db.query(Location).options(joinedload(Location.region), joinedload(Location.legal_entity))
             .filter(Location.company_id == cid))
    if active is not None:
        query = query.filter(Location.active.is_(active))
    if region_id:
        query = query.filter(Location.region_id == region_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Location.name.ilike(like), Location.city.ilike(like), Location.code.ilike(like)))
    rows = query.order_by(Location.name).all()
    from modules.employees.models import Employee
    counts = _active_counts(db, cid, Employee.location_id)
    states = labels_for(db, cid, [r.state_id for r in rows if r.state_id])
    return [_location_out(r, states, counts) for r in rows]


def _location(db, cid, uuid):
    row = (db.query(Location).options(joinedload(Location.region), joinedload(Location.legal_entity))
           .filter(Location.company_id == cid, Location.uuid == uuid).first())
    if row is None:
        from core.errors import NotFound
        raise NotFound("Location")
    from modules.employees.models import Employee
    return _location_out(row, labels_for(db, cid, [row.state_id] if row.state_id else []),
                         _active_counts(db, cid, Employee.location_id))


@router.get("/locations/{uuid}")
def get_location(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return _location(db, user["company_id"], uuid)


@router.post("/locations", status_code=201)
def create_location(body: s.LocationIn, db: Session = Depends(get_db), user: dict = Depends(ORG)):
    row = service.save_location(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return _location(db, user["company_id"], row.uuid)


@router.put("/locations/{uuid}")
def update_location(uuid: UUID, body: s.LocationIn, db: Session = Depends(get_db), user: dict = Depends(ORG)):
    row = service.get_by_uuid(db, Location, user["company_id"], uuid, "Location")
    service.save_location(db, user["company_id"], body.model_dump(exclude_unset=True), row)
    db.commit()
    return _location(db, user["company_id"], uuid)


# ── Grades ───────────────────────────────────────────────────────────

def _grade_out(g: Grade, counts: dict, desig: dict) -> dict:
    return {"id": g.id, "uuid": str(g.uuid), "name": g.name, "code": g.code, "band": g.band, "rank": g.rank,
            "min_ctc": g.min_ctc, "max_ctc": g.max_ctc, "description": g.description, "active": g.active,
            "employee_count": counts.get(g.id, 0), "designation_count": desig.get(g.id, 0), "created_at": g.created_at}


def _grade_counts(db, cid):
    from modules.designations.models import Designation
    from modules.employees.models import Employee
    desig = dict(db.query(Designation.grade_id, func.count(Designation.id))
                 .filter(Designation.company_id == cid, Designation.grade_id.isnot(None))
                 .group_by(Designation.grade_id).all())
    return _active_counts(db, cid, Employee.grade_id), desig


@router.get("/grades")
def list_grades(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    rows = db.query(Grade).filter(Grade.company_id == cid).order_by(Grade.rank, Grade.name).all()
    counts, desig = _grade_counts(db, cid)
    return [_grade_out(g, counts, desig) for g in rows]


@router.post("/grades", status_code=201)
def create_grade(body: s.GradeIn, db: Session = Depends(get_db), user: dict = Depends(ORG)):
    row = service.save_grade(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return _grade_out(row, {}, {})


@router.put("/grades/{uuid}")
def update_grade(uuid: UUID, body: s.GradeIn, db: Session = Depends(get_db), user: dict = Depends(ORG)):
    row = service.get_by_uuid(db, Grade, user["company_id"], uuid, "Grade")
    service.save_grade(db, user["company_id"], body.model_dump(exclude_unset=True), row)
    db.commit()
    return _grade_out(row, *_grade_counts(db, user["company_id"]))


# ── Cost centres ─────────────────────────────────────────────────────

def _cc_out(c: CostCentre, owners: dict, counts: dict, dept: dict) -> dict:
    return {"id": c.id, "uuid": str(c.uuid), "name": c.name, "code": c.code, "description": c.description,
            "legal_entity_id": c.legal_entity_id, "legal_entity_name": c.legal_entity.name if c.legal_entity else None,
            "owner_employee_id": c.owner_employee_id, "owner": owners.get(c.owner_employee_id), "active": c.active,
            "employee_count": counts.get(c.id, 0), "department_count": dept.get(c.id, 0), "created_at": c.created_at}


def _cc_counts(db, cid):
    from modules.departments.models import Department
    from modules.employees.models import Employee
    dept = dict(db.query(Department.cost_centre_id, func.count(Department.id))
                .filter(Department.company_id == cid, Department.cost_centre_id.isnot(None))
                .group_by(Department.cost_centre_id).all())
    return _active_counts(db, cid, Employee.cost_centre_id), dept


@router.get("/cost-centres")
def list_cost_centres(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    rows = (db.query(CostCentre).options(joinedload(CostCentre.legal_entity))
            .filter(CostCentre.company_id == cid).order_by(CostCentre.code, CostCentre.name).all())
    counts, dept = _cc_counts(db, cid)
    owners = _emp_names(db, cid, [r.owner_employee_id for r in rows])
    return [_cc_out(r, owners, counts, dept) for r in rows]


@router.post("/cost-centres", status_code=201)
def create_cost_centre(body: s.CostCentreIn, db: Session = Depends(get_db), user: dict = Depends(ORG)):
    row = service.save_cost_centre(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(row)
    return _cc_out(row, _emp_names(db, user["company_id"], [row.owner_employee_id]), {}, {})


@router.put("/cost-centres/{uuid}")
def update_cost_centre(uuid: UUID, body: s.CostCentreIn, db: Session = Depends(get_db), user: dict = Depends(ORG)):
    row = service.get_by_uuid(db, CostCentre, user["company_id"], uuid, "Cost centre")
    service.save_cost_centre(db, user["company_id"], body.model_dump(exclude_unset=True), row)
    db.commit()
    db.refresh(row)
    return _cc_out(row, _emp_names(db, user["company_id"], [row.owner_employee_id]), *_cc_counts(db, user["company_id"]))


# ── Teams ────────────────────────────────────────────────────────────

def _team_out(db, cid, t: Team, members: list | None = None) -> dict:
    from modules.departments.models import Department
    dept = db.query(Department.department_name).filter(Department.id == t.department_id).scalar() if t.department_id else None
    ids = [m.employee_id for m in t.members]
    people = _emp_names(db, cid, ids + [t.lead_employee_id])
    out = {"id": t.id, "uuid": str(t.uuid), "name": t.name, "code": t.code, "description": t.description,
           "lead_employee_id": t.lead_employee_id, "lead": people.get(t.lead_employee_id), "department_id": t.department_id,
           "department_name": dept, "color": t.color, "active": t.active, "member_count": len(ids),
           "member_ids": ids, "preview": [people[i] for i in ids[:5] if i in people], "created_at": t.created_at}
    if members is not None:
        out["members"] = members
    return out


@router.get("/teams")
def list_teams(q: str | None = None, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    query = db.query(Team).options(joinedload(Team.members)).filter(Team.company_id == cid)
    if q:
        query = query.filter(Team.name.ilike(f"%{q.strip()}%"))
    return [_team_out(db, cid, t) for t in query.order_by(Team.name).all()]


@router.get("/teams/{uuid}")
def get_team(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from modules.employees.models import Employee
    cid = user["company_id"]
    t = service.get_by_uuid(db, Team, cid, uuid, "Team")
    rows = (db.query(Employee).options(joinedload(Employee.designation), joinedload(Employee.department))
            .join(TeamMember, TeamMember.employee_id == Employee.id)
            .filter(TeamMember.team_id == t.id, Employee.company_id == cid).order_by(Employee.first_name).all())
    members = [{"id": e.id, "uuid": str(e.uuid), "name": f"{e.first_name} {e.last_name}", "photo": e.profile_image_url,
                "designation": e.designation.designation_name if e.designation else None,
                "department": e.department.department_name if e.department else None,
                "is_lead": e.id == t.lead_employee_id, "active": e.active} for e in rows]
    return _team_out(db, cid, t, members)


@router.post("/teams", status_code=201)
def create_team(body: s.TeamIn, db: Session = Depends(get_db), user: dict = Depends(TEAMS)):
    row = service.save_team(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return get_team(row.uuid, db, user)


@router.put("/teams/{uuid}")
def update_team(uuid: UUID, body: s.TeamIn, db: Session = Depends(get_db), user: dict = Depends(TEAMS)):
    row = service.get_by_uuid(db, Team, user["company_id"], uuid, "Team")
    service.save_team(db, user["company_id"], body.model_dump(exclude_unset=True), row)
    db.commit()
    db.expire_all()
    return get_team(uuid, db, user)


# ── Tags ─────────────────────────────────────────────────────────────

def _tag_out(t: Tag, usage: dict, names: dict) -> dict:
    return {"id": t.id, "uuid": str(t.uuid), "name": t.name, "parent_id": t.parent_id, "parent_name": names.get(t.parent_id),
            "color": t.color, "description": t.description, "applies_to": t.applies_to or [], "sort_order": t.sort_order,
            "active": t.active, "usage": usage.get(t.id, 0), "created_at": t.created_at}


@router.get("/tags")
def list_tags(entity_type: str | None = None, active: bool | None = None, db: Session = Depends(get_db),
              user: dict = Depends(get_current_user)):
    """All tags, parents before children (the client builds the tree from parent_id)."""
    cid = user["company_id"]
    query = db.query(Tag).filter(Tag.company_id == cid)
    if active is not None:
        query = query.filter(Tag.active.is_(active))
    rows = query.order_by(Tag.sort_order, Tag.name).all()
    if entity_type:
        rows = [t for t in rows if not t.applies_to or entity_type in t.applies_to]
    names = {t.id: t.name for t in rows}
    usage = service.tag_usage(db, cid)
    return [_tag_out(t, usage, names) for t in rows]


@router.post("/tags", status_code=201)
def create_tag(body: s.TagIn, db: Session = Depends(get_db), user: dict = Depends(TAGS)):
    row = service.save_tag(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return _tag_out(row, {}, {})


@router.put("/tags/{uuid}")
def update_tag(uuid: UUID, body: s.TagIn, db: Session = Depends(get_db), user: dict = Depends(TAGS)):
    row = service.get_by_uuid(db, Tag, user["company_id"], uuid, "Tag")
    service.save_tag(db, user["company_id"], body.model_dump(exclude_unset=True), row)
    db.commit()
    return _tag_out(row, service.tag_usage(db, user["company_id"]), {})


@router.post("/tags/assign")
def assign_tags(body: s.TagAssign, db: Session = Depends(get_db), user: dict = Depends(TAGS)):
    """Add and/or remove tags on many records at once (bulk action on the employee list)."""
    cid = user["company_id"]
    ids = list(body.entity_ids)
    if body.entity_uuids and body.entity_type == "employees":
        from modules.employees.models import Employee
        ids += [i for (i,) in db.query(Employee.id).filter(Employee.company_id == cid,
                                                          Employee.uuid.in_(body.entity_uuids))]
    if body.entity_type == "employees" and ids:
        from modules.employees.models import Employee
        found = {i for (i,) in db.query(Employee.id).filter(Employee.company_id == cid, Employee.id.in_(ids))}
        if found != set(ids):
            from core.errors import BadRequest
            raise BadRequest("Some of the people picked were not found.")
    changed = service.bulk_tag(db, cid, body.entity_type, ids, body.add, body.remove)
    audit.event(db, "tags.bulk_assigned", body.entity_type, None,
                {"count": len(ids), "added": body.add, "removed": body.remove})
    db.commit()
    return {"changed": changed, "records": len(ids)}


# ── Skills ───────────────────────────────────────────────────────────

def _skill_out(sk: Skill, cats: dict, counts: dict, verified: dict) -> dict:
    return {"id": sk.id, "uuid": str(sk.uuid), "name": sk.name, "category_id": sk.category_id,
            "category_name": cats.get(sk.category_id), "description": sk.description, "active": sk.active,
            "people_count": counts.get(sk.id, 0), "verified_count": verified.get(sk.id, 0), "created_at": sk.created_at}


@router.get("/skills")
def list_skills(q: str | None = None, category_id: int | None = None, db: Session = Depends(get_db),
                user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    query = db.query(Skill).filter(Skill.company_id == cid)
    if q:
        query = query.filter(Skill.name.ilike(f"%{q.strip()}%"))
    if category_id:
        from core.masters import MasterValue
        subs = [i for (i,) in db.query(MasterValue.id).filter(MasterValue.company_id == cid,
                                                              MasterValue.parent_id == category_id)]
        query = query.filter(Skill.category_id.in_([category_id, *subs]))
    rows = query.order_by(Skill.name).all()
    cats = labels_for(db, cid, [r.category_id for r in rows if r.category_id])
    counts = dict(db.query(EmployeeSkill.skill_id, func.count(EmployeeSkill.id))
                  .filter(EmployeeSkill.company_id == cid).group_by(EmployeeSkill.skill_id).all())
    verified = dict(db.query(EmployeeSkill.skill_id, func.count(EmployeeSkill.id))
                    .filter(EmployeeSkill.company_id == cid, EmployeeSkill.verified.is_(True))
                    .group_by(EmployeeSkill.skill_id).all())
    return [_skill_out(r, cats, counts, verified) for r in rows]


@router.post("/skills", status_code=201)
def create_skill(body: s.SkillIn, db: Session = Depends(get_db), user: dict = Depends(SKILLS)):
    row = service.save_skill(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return _skill_out(row, labels_for(db, user["company_id"], [row.category_id] if row.category_id else []), {}, {})


@router.put("/skills/{uuid}")
def update_skill(uuid: UUID, body: s.SkillIn, db: Session = Depends(get_db), user: dict = Depends(SKILLS)):
    row = service.get_by_uuid(db, Skill, user["company_id"], uuid, "Skill")
    service.save_skill(db, user["company_id"], body.model_dump(exclude_unset=True), row)
    db.commit()
    return _skill_out(row, labels_for(db, user["company_id"], [row.category_id] if row.category_id else []), {}, {})


@router.get("/skills/{uuid}/people")
def skill_people(uuid: UUID, min_level: int = Query(1, ge=1, le=5), db: Session = Depends(get_db),
                 user: dict = Depends(require_permission("people.employees.view"))):
    """Find people by skill."""
    from modules.employees.models import Employee
    cid = user["company_id"]
    sk = service.get_by_uuid(db, Skill, cid, uuid, "Skill")
    rows = (db.query(EmployeeSkill, Employee).join(Employee, Employee.id == EmployeeSkill.employee_id)
            .options(joinedload(Employee.designation))
            .filter(EmployeeSkill.company_id == cid, EmployeeSkill.skill_id == sk.id, EmployeeSkill.level >= min_level,
                    Employee.active.is_(True))
            .order_by(EmployeeSkill.level.desc(), Employee.first_name).all())
    return [{"uuid": str(e.uuid), "name": f"{e.first_name} {e.last_name}", "photo": e.profile_image_url,
             "designation": e.designation.designation_name if e.designation else None, "level": es.level,
             "level_label": service.SKILL_LEVELS[es.level], "verified": es.verified,
             "years": float(es.years) if es.years is not None else None} for es, e in rows]


# ── Org chart ────────────────────────────────────────────────────────

@router.get("/org-chart")
def org_chart(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Everyone still with the company as flat nodes; the client builds the tree from
    manager_id. Departments come with their parent and head for the department view."""
    from modules.departments.models import Department
    from modules.employees.models import Employee
    cid = user["company_id"]
    rows = (db.query(Employee).options(joinedload(Employee.designation), joinedload(Employee.department),
                                       joinedload(Employee.location))
            .filter(Employee.company_id == cid, Employee.status.notin_(("exited", "archived")))
            .order_by(Employee.first_name, Employee.last_name).all())
    ids = {e.id for e in rows}
    reports: dict[int, int] = {}
    for e in rows:
        if e.reporting_manager_id in ids:
            reports[e.reporting_manager_id] = reports.get(e.reporting_manager_id, 0) + 1
    nodes = [{"id": e.id, "uuid": str(e.uuid), "name": f"{e.first_name} {e.last_name}".strip(),
              "preferred_name": e.preferred_name, "photo": e.profile_image_url, "code": e.employee_code,
              "designation": e.designation.designation_name if e.designation else None,
              "department_id": e.department_id, "department": e.department.department_name if e.department else None,
              "location": e.location.name if e.location else None,
              "manager_id": e.reporting_manager_id if e.reporting_manager_id in ids else None,
              "reports": reports.get(e.id, 0), "status": e.status, "date_of_joining": e.date_of_joining} for e in rows]
    depts = (db.query(Department).filter(or_(Department.company_id == cid, Department.is_global.is_(True)),
                                         Department.active.is_(True)).order_by(Department.department_name).all())
    counts: dict[int, int] = {}
    for e in rows:
        if e.department_id:
            counts[e.department_id] = counts.get(e.department_id, 0) + 1
    heads = _emp_names(db, cid, [d.head_employee_id for d in depts])
    departments = [{"id": d.id, "uuid": str(d.uuid), "name": d.department_name, "code": d.department_code,
                    "parent_id": d.parent_id, "head": heads.get(d.head_employee_id), "count": counts.get(d.id, 0)}
                   for d in depts]
    return {"nodes": nodes, "departments": departments}


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]

    def n(model):
        return db.query(func.count(model.id)).filter(model.company_id == cid, model.active.is_(True)).scalar()
    return {"legal_entities": n(LegalEntity), "regions": n(Region), "locations": n(Location), "grades": n(Grade),
            "cost_centres": n(CostCentre), "teams": n(Team), "tags": n(Tag), "skills": n(Skill)}
