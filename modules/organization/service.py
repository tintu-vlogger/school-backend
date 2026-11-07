"""Organisation rules: legal entities, regions, locations, grades, cost centres, teams,
tags (on any record) and skills. Records are never deleted, only switched off."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core import context
from core.errors import BadRequest, NotFound
from modules.organization.models import (CostCentre, EmployeeSkill, Grade, LegalEntity, Location, Region, Skill, Tag,
                                         TagLink, Team, TeamMember)

TAG_ENTITIES = {"employees": "Employees", "candidates": "Candidates", "assets": "Assets", "documents": "Documents"}
REGISTRATION_KINDS = {"pt": "Professional Tax", "lwf": "Labour Welfare Fund", "shops": "Shops and establishments"}
LOCATION_KINDS = {"head_office": "Head office", "branch": "Branch", "factory": "Factory", "warehouse": "Warehouse",
                  "site": "Site", "remote": "Remote"}
SKILL_LEVELS = {1: "Beginner", 2: "Basic", 3: "Intermediate", 4: "Advanced", 5: "Expert"}


def get_by_uuid(db: Session, model, company_id: int, uuid, what: str):
    row = db.query(model).filter(model.company_id == company_id, model.uuid == uuid).first()
    if row is None:
        raise NotFound(what)
    return row


def _by_id(db: Session, model, company_id: int, rid: int | None):
    if rid is None:
        return None
    return db.query(model).filter(model.company_id == company_id, model.id == rid).first()


def _unique_name(db: Session, model, company_id: int, name: str, exclude_id: int | None, what: str, **scope) -> str:
    name = (name or "").strip()
    if not name:
        raise BadRequest(f"Give the {what} a name.")
    q = db.query(model.id).filter(model.company_id == company_id, func.lower(model.name) == name.lower())
    for k, val in scope.items():
        col = getattr(model, k)
        q = q.filter(col.is_(None) if val is None else col == val)
    if exclude_id:
        q = q.filter(model.id != exclude_id)
    if q.first():
        raise BadRequest(f"A {what} called “{name}” already exists.")
    return name


def _unique_code(db: Session, model, company_id: int, code: str | None, exclude_id: int | None, what: str) -> str | None:
    code = (code or "").strip().upper() or None
    if code is None:
        return None
    q = db.query(model.id).filter(model.company_id == company_id, func.upper(model.code) == code)
    if exclude_id:
        q = q.filter(model.id != exclude_id)
    if q.first():
        raise BadRequest(f"Another {what} already uses the code {code}.")
    return code


def _check_state(db: Session, company_id: int, state_id) -> int | None:
    if state_id in (None, ""):
        return None
    from core.masters import validate_value_ids
    validate_value_ids(db, company_id, "state", [int(state_id)])
    return int(state_id)


def _check_employee(db: Session, company_id: int, employee_id, label: str) -> int | None:
    if employee_id in (None, ""):
        return None
    from modules.employees.models import Employee
    if not db.query(Employee.id).filter(Employee.company_id == company_id, Employee.id == int(employee_id)).first():
        raise BadRequest(f"The {label} was not found.")
    return int(employee_id)


def check_refs(db: Session, company_id: int, refs: dict) -> None:
    """Every id must belong to this company (used by the employee record)."""
    from modules.departments.models import Department
    from modules.designations.models import Designation
    models = {"location_id": (Location, "location"), "legal_entity_id": (LegalEntity, "legal entity"),
              "grade_id": (Grade, "grade"), "cost_centre_id": (CostCentre, "cost centre"),
              "department_id": (Department, "department"), "designation_id": (Designation, "designation")}
    for key, rid in refs.items():
        model, label = models[key]
        q = db.query(model.id).filter(model.id == int(rid))
        if model in (Department, Designation):
            q = q.filter(or_(model.company_id == company_id, model.is_global.is_(True)))
        else:
            q = q.filter(model.company_id == company_id)
        if not q.first():
            raise BadRequest(f"The {label} was not found.")


def _apply(row, data: dict, fields: tuple[str, ...]) -> None:
    for f in fields:
        if f in data:
            val = data[f]
            if isinstance(val, str):
                val = val.strip() or None
            setattr(row, f, val)


def _apply_custom(db: Session, company_id: int, entity: str, row, data: dict) -> None:
    """Custom field values: required ones are checked when the record is created."""
    from core import custom_fields
    new = row.id is None
    if "custom" in data or new:
        row.custom = custom_fields.merge_values(db, company_id, entity, data.get("custom"),
                                                None if new else row.custom, partial=not new)


# ── Legal entities ───────────────────────────────────────────────────

def _clean_entity(db: Session, company_id: int, data: dict, row: LegalEntity | None) -> dict:
    from modules.employees import validators as v
    data = dict(data)
    if "name" in data or row is None:
        data["name"] = _unique_name(db, LegalEntity, company_id, data.get("name"), row.id if row else None, "legal entity")
    if "code" in data:
        data["code"] = _unique_code(db, LegalEntity, company_id, data["code"], row.id if row else None, "legal entity")
    for f, fn in (("pan", lambda x: v.clean_pan(x, person=False)), ("tan", v.clean_tan), ("cin", v.clean_cin),
                  ("gstin", v.clean_gstin), ("pincode", v.clean_pincode)):
        if f in data:
            data[f] = fn(data[f])
    if data.get("pf_code"):
        data["pf_code"] = data["pf_code"].replace(" ", "").upper()
        import re
        if not re.fullmatch(r"[A-Z]{5}[0-9]{7}([0-9]{3})?", data["pf_code"]):
            raise BadRequest("PF establishment code looks like KABLR0012345 (5 letters and 7 digits).")
    if data.get("esi_code"):
        data["esi_code"] = data["esi_code"].replace(" ", "").replace("-", "")
        if not data["esi_code"].isdigit() or len(data["esi_code"]) != 17:
            raise BadRequest("ESI employer code has 17 digits.")
    if "state_id" in data:
        data["state_id"] = _check_state(db, company_id, data["state_id"])
    if data.get("gstin") and data.get("pan") and data["gstin"][2:12] != data["pan"]:
        raise BadRequest("The GSTIN should contain the entity's PAN (characters 3 to 12).")
    if "registrations" in data:
        regs = []
        for r in data["registrations"] or []:
            kind = r.get("kind")
            if kind not in REGISTRATION_KINDS:
                raise BadRequest("Each registration should be Professional Tax, Labour Welfare Fund or Shops.")
            number = (r.get("number") or "").strip()
            if not number:
                raise BadRequest(f"Enter the {REGISTRATION_KINDS[kind]} registration number.")
            regs.append({"kind": kind, "state_id": _check_state(db, company_id, r.get("state_id")), "number": number,
                         "valid_from": r.get("valid_from") or None, "valid_to": r.get("valid_to") or None})
        data["registrations"] = regs
    if "bank_accounts" in data:
        accounts = []
        for b in data["bank_accounts"] or []:
            number = v.clean_account_number(b.get("account_number"))
            ifsc = v.clean_ifsc(b.get("ifsc"))
            if not number or not ifsc:
                raise BadRequest("Each bank account needs an account number and IFSC.")
            accounts.append({"bank_id": b.get("bank_id"), "account_name": (b.get("account_name") or "").strip() or None,
                             "account_number": number, "ifsc": ifsc, "branch": (b.get("branch") or "").strip() or None,
                             "purpose": (b.get("purpose") or "").strip() or None, "primary": bool(b.get("primary"))})
        if accounts and not any(a["primary"] for a in accounts):
            accounts[0]["primary"] = True
        if sum(1 for a in accounts if a["primary"]) > 1:
            raise BadRequest("Only one bank account can be the main one.")
        data["bank_accounts"] = accounts
    if "signatory" in data:
        s = data["signatory"] or {}
        data["signatory"] = {k: (s.get(k) or None) for k in ("name", "designation", "pan", "email", "phone", "employee_id")}
        if data["signatory"].get("pan"):
            data["signatory"]["pan"] = v.clean_pan(data["signatory"]["pan"])
        if data["signatory"].get("employee_id"):
            data["signatory"]["employee_id"] = _check_employee(db, company_id, data["signatory"]["employee_id"], "signatory")
    return data


ENTITY_FIELDS = ("name", "legal_name", "code", "pan", "tan", "cin", "gstin", "pf_code", "esi_code", "lin",
                 "incorporation_date", "address_line1", "address_line2", "city", "state_id", "pincode", "email", "phone",
                 "registrations", "bank_accounts", "signatory", "is_primary", "active")


def save_entity(db: Session, company_id: int, data: dict, row: LegalEntity | None = None) -> LegalEntity:
    data = _clean_entity(db, company_id, data, row)
    if row is None:
        row = LegalEntity(company_id=company_id)
        db.add(row)
    if data.get("active") is False and row.id:
        from modules.employees.models import Employee
        n = db.query(func.count(Employee.id)).filter(Employee.company_id == company_id, Employee.legal_entity_id == row.id,
                                                     Employee.active.is_(True)).scalar()
        if n:
            raise BadRequest(f"{n} active {'employee works' if n == 1 else 'employees work'} for this entity. Move them first.")
    _apply_custom(db, company_id, "legal_entity", row, data)
    _apply(row, data, ENTITY_FIELDS)
    db.flush()
    if row.is_primary:
        db.query(LegalEntity).filter(LegalEntity.company_id == company_id, LegalEntity.id != row.id,
                                     LegalEntity.is_primary.is_(True)).update({"is_primary": False})
    return row


# ── Regions and locations ────────────────────────────────────────────

def save_region(db: Session, company_id: int, data: dict, row: Region | None = None) -> Region:
    data = dict(data)
    if "name" in data or row is None:
        data["name"] = _unique_name(db, Region, company_id, data.get("name"), row.id if row else None, "region")
    if "code" in data:
        data["code"] = _unique_code(db, Region, company_id, data["code"], row.id if row else None, "region")
    if "head_employee_id" in data:
        data["head_employee_id"] = _check_employee(db, company_id, data["head_employee_id"], "region head")
    if row is None:
        row = Region(company_id=company_id)
        db.add(row)
    _apply(row, data, ("name", "code", "description", "head_employee_id", "active"))
    db.flush()
    return row


LOCATION_FIELDS = ("name", "code", "kind", "region_id", "legal_entity_id", "address_line1", "address_line2", "city",
                   "state_id", "pincode", "timezone", "latitude", "longitude", "radius_m", "holiday_calendar_id",
                   "phone", "email", "active")


def save_location(db: Session, company_id: int, data: dict, row: Location | None = None) -> Location:
    from core.timeutil import is_valid_timezone
    from modules.employees import validators as v
    data = dict(data)
    if "name" in data or row is None:
        data["name"] = _unique_name(db, Location, company_id, data.get("name"), row.id if row else None, "location")
    if "code" in data:
        data["code"] = _unique_code(db, Location, company_id, data["code"], row.id if row else None, "location")
    if "kind" in data and data["kind"] not in LOCATION_KINDS:
        raise BadRequest("Pick what kind of place this is.")
    if "state_id" in data:
        data["state_id"] = _check_state(db, company_id, data["state_id"])
    if row is None and not data.get("state_id"):
        raise BadRequest("Pick the state. It decides Professional Tax, LWF and holidays for this location.")
    if "pincode" in data:
        data["pincode"] = v.clean_pincode(data["pincode"])
    if data.get("timezone") and not is_valid_timezone(data["timezone"]):
        raise BadRequest("That timezone isn't recognised.")
    if data.get("region_id") and not _by_id(db, Region, company_id, data["region_id"]):
        raise BadRequest("The region was not found.")
    if data.get("legal_entity_id") and not _by_id(db, LegalEntity, company_id, data["legal_entity_id"]):
        raise BadRequest("The legal entity was not found.")
    lat = data.get("latitude", row.latitude if row else None)
    lng = data.get("longitude", row.longitude if row else None)
    if (lat is None) != (lng is None):
        raise BadRequest("Give both latitude and longitude, or neither.")
    if lat is not None and not (-90 <= float(lat) <= 90 and -180 <= float(lng) <= 180):
        raise BadRequest("Latitude must be between -90 and 90 and longitude between -180 and 180.")
    if data.get("radius_m") is not None and not 10 <= int(data["radius_m"]) <= 50000:
        raise BadRequest("The attendance radius should be between 10 m and 50 km.")
    if data.get("active") is False and row is not None and row.id:
        from modules.employees.models import Employee
        n = db.query(func.count(Employee.id)).filter(Employee.company_id == company_id, Employee.location_id == row.id,
                                                     Employee.active.is_(True)).scalar()
        if n:
            raise BadRequest(f"{n} active {'employee is' if n == 1 else 'employees are'} at this location. Move them first.")
    if row is None:
        row = Location(company_id=company_id)
        db.add(row)
    _apply_custom(db, company_id, "location", row, data)
    _apply(row, data, LOCATION_FIELDS)
    db.flush()
    return row


# ── Grades and cost centres ──────────────────────────────────────────

def save_grade(db: Session, company_id: int, data: dict, row: Grade | None = None) -> Grade:
    data = dict(data)
    if "name" in data or row is None:
        data["name"] = _unique_name(db, Grade, company_id, data.get("name"), row.id if row else None, "grade")
    if "code" in data:
        data["code"] = _unique_code(db, Grade, company_id, data["code"], row.id if row else None, "grade")
    lo = data.get("min_ctc", row.min_ctc if row else None)
    hi = data.get("max_ctc", row.max_ctc if row else None)
    if lo is not None and hi is not None and lo > hi:
        raise BadRequest("The lowest CTC can't be more than the highest.")
    if row is None:
        row = Grade(company_id=company_id)
        if data.get("rank") is None:
            data["rank"] = (db.query(func.max(Grade.rank)).filter(Grade.company_id == company_id).scalar() or 0) + 1
        db.add(row)
    _apply(row, data, ("name", "code", "band", "rank", "min_ctc", "max_ctc", "description", "active"))
    db.flush()
    return row


def save_cost_centre(db: Session, company_id: int, data: dict, row: CostCentre | None = None) -> CostCentre:
    data = dict(data)
    if "name" in data or row is None:
        data["name"] = _unique_name(db, CostCentre, company_id, data.get("name"), row.id if row else None, "cost centre")
    if "code" in data:
        data["code"] = _unique_code(db, CostCentre, company_id, data["code"], row.id if row else None, "cost centre")
    if "owner_employee_id" in data:
        data["owner_employee_id"] = _check_employee(db, company_id, data["owner_employee_id"], "owner")
    if data.get("legal_entity_id") and not _by_id(db, LegalEntity, company_id, data["legal_entity_id"]):
        raise BadRequest("The legal entity was not found.")
    if row is None:
        row = CostCentre(company_id=company_id)
        db.add(row)
    _apply(row, data, ("name", "code", "description", "legal_entity_id", "owner_employee_id", "active"))
    db.flush()
    return row


# ── Teams ────────────────────────────────────────────────────────────

def save_team(db: Session, company_id: int, data: dict, row: Team | None = None) -> Team:
    data = dict(data)
    if "name" in data or row is None:
        data["name"] = _unique_name(db, Team, company_id, data.get("name"), row.id if row else None, "team")
    if "code" in data:
        data["code"] = _unique_code(db, Team, company_id, data["code"], row.id if row else None, "team")
    if "lead_employee_id" in data:
        data["lead_employee_id"] = _check_employee(db, company_id, data["lead_employee_id"], "team lead")
    if data.get("department_id"):
        check_refs(db, company_id, {"department_id": data["department_id"]})
    if row is None:
        row = Team(company_id=company_id)
        db.add(row)
    _apply(row, data, ("name", "code", "description", "lead_employee_id", "department_id", "color", "active"))
    db.flush()
    if "member_ids" in data:
        ids = set(int(i) for i in data["member_ids"] or [])
        if row.lead_employee_id:
            ids.add(row.lead_employee_id)
        set_team_members(db, company_id, row, sorted(ids))
    elif row.lead_employee_id:
        add_team_members(db, company_id, row, [row.lead_employee_id])
    return row


def set_team_members(db: Session, company_id: int, team: Team, employee_ids: list[int]) -> None:
    from modules.employees.models import Employee
    wanted = set(employee_ids)
    if wanted:
        found = {i for (i,) in db.query(Employee.id).filter(Employee.company_id == company_id, Employee.id.in_(wanted))}
        if found != wanted:
            raise BadRequest("Some of the people picked were not found.")
    current = {m.employee_id: m for m in db.query(TeamMember).filter(TeamMember.company_id == company_id,
                                                                    TeamMember.team_id == team.id)}
    for eid, m in current.items():
        if eid not in wanted:
            db.delete(m)
    for eid in wanted - set(current):
        db.add(TeamMember(company_id=company_id, team_id=team.id, employee_id=eid))
    db.flush()


def add_team_members(db: Session, company_id: int, team: Team, employee_ids: list[int]) -> None:
    current = {m.employee_id for m in db.query(TeamMember).filter(TeamMember.company_id == company_id,
                                                                 TeamMember.team_id == team.id)}
    set_team_members(db, company_id, team, sorted(current | set(employee_ids)))


def set_employee_teams(db: Session, company_id: int, employee_id: int, team_ids: list[int]) -> None:
    wanted = {int(t) for t in team_ids or []}
    teams = {t.id for t in db.query(Team).filter(Team.company_id == company_id, Team.id.in_(wanted))} if wanted else set()
    if teams != wanted:
        raise BadRequest("Some of the teams picked were not found.")
    current = {m.team_id: m for m in db.query(TeamMember).filter(TeamMember.company_id == company_id,
                                                                TeamMember.employee_id == employee_id)}
    for tid, m in current.items():
        if tid not in wanted:
            db.delete(m)
    for tid in wanted - set(current):
        db.add(TeamMember(company_id=company_id, team_id=tid, employee_id=employee_id))
    db.flush()


def teams_of(db: Session, company_id: int, employee_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    if not employee_ids:
        return out
    rows = (db.query(TeamMember.employee_id, Team.id, Team.uuid, Team.name, Team.color, Team.lead_employee_id)
            .join(Team, Team.id == TeamMember.team_id)
            .filter(TeamMember.company_id == company_id, TeamMember.employee_id.in_(employee_ids)).all())
    for eid, tid, tuuid, name, color, lead in rows:
        out.setdefault(eid, []).append({"id": tid, "uuid": str(tuuid), "name": name, "color": color, "is_lead": lead == eid})
    return out


# ── Tags ─────────────────────────────────────────────────────────────

def save_tag(db: Session, company_id: int, data: dict, row: Tag | None = None) -> Tag:
    data = dict(data)
    parent_id = data.get("parent_id", row.parent_id if row else None)
    if parent_id:
        parent = _by_id(db, Tag, company_id, parent_id)
        if parent is None:
            raise BadRequest("The parent tag was not found.")
        cursor, depth = parent, 1
        while cursor is not None:
            if row is not None and cursor.id == row.id:
                raise BadRequest("A tag can't sit under itself.")
            cursor = cursor.parent
            depth += 1
        if depth > 4:
            raise BadRequest("Tags can be nested at most three levels deep.")
    if "name" in data or row is None:
        data["name"] = _unique_name(db, Tag, company_id, data.get("name"), row.id if row else None, "tag",
                                    parent_id=parent_id)
    if "applies_to" in data:
        bad = [x for x in data["applies_to"] or [] if x not in TAG_ENTITIES]
        if bad:
            raise BadRequest("Tags can be used on employees, candidates, assets or documents.")
        data["applies_to"] = list(dict.fromkeys(data["applies_to"] or []))
    if row is None:
        row = Tag(company_id=company_id)
        if data.get("sort_order") is None:
            data["sort_order"] = (db.query(func.max(Tag.sort_order)).filter(Tag.company_id == company_id).scalar() or 0) + 1
        db.add(row)
    _apply(row, data, ("name", "parent_id", "color", "description", "applies_to", "sort_order", "active"))
    db.flush()
    return row


def tag_descendants(db: Session, company_id: int, tag_ids: list[int]) -> set[int]:
    """The tags and all their sub-tags (filtering by a tag includes its sub-tags)."""
    pairs = db.query(Tag.id, Tag.parent_id).filter(Tag.company_id == company_id).all()
    children: dict[int, list[int]] = {}
    for tid, pid in pairs:
        if pid:
            children.setdefault(pid, []).append(tid)
    out, stack = set(), [int(t) for t in tag_ids]
    while stack:
        t = stack.pop()
        if t in out:
            continue
        out.add(t)
        stack.extend(children.get(t, []))
    return out


def _check_tags(db: Session, company_id: int, entity_type: str, tag_ids) -> set[int]:
    if entity_type not in TAG_ENTITIES:
        raise BadRequest("Tags can't be put on this kind of record.")
    wanted = {int(t) for t in tag_ids or []}
    if not wanted:
        return wanted
    rows = db.query(Tag).filter(Tag.company_id == company_id, Tag.id.in_(wanted)).all()
    if len(rows) != len(wanted):
        raise BadRequest("Some of the tags were not found.")
    for t in rows:
        if t.applies_to and entity_type not in t.applies_to:
            raise BadRequest(f"The tag “{t.name}” can't be used on {TAG_ENTITIES[entity_type].lower()}.")
        if not t.active:
            raise BadRequest(f"The tag “{t.name}” is switched off.")
    return wanted


def set_tags(db: Session, company_id: int, entity_type: str, entity_id: int, tag_ids) -> None:
    wanted = _check_tags(db, company_id, entity_type, tag_ids)
    current = {link.tag_id: link for link in db.query(TagLink).filter(
        TagLink.company_id == company_id, TagLink.entity_type == entity_type, TagLink.entity_id == entity_id)}
    for tid, link in current.items():
        if tid not in wanted:
            db.delete(link)
    for tid in wanted - set(current):
        db.add(TagLink(company_id=company_id, tag_id=tid, entity_type=entity_type, entity_id=entity_id))
    db.flush()


def bulk_tag(db: Session, company_id: int, entity_type: str, entity_ids: list[int], add: list[int], remove: list[int]) -> int:
    add_set = _check_tags(db, company_id, entity_type, add)
    remove_set = {int(t) for t in remove or []}
    changed = 0
    existing = {(lk.tag_id, lk.entity_id): lk for lk in db.query(TagLink).filter(
        TagLink.company_id == company_id, TagLink.entity_type == entity_type, TagLink.entity_id.in_(entity_ids or [0]))}
    for eid in entity_ids:
        for tid in add_set:
            if (tid, eid) not in existing:
                db.add(TagLink(company_id=company_id, tag_id=tid, entity_type=entity_type, entity_id=eid))
                changed += 1
        for tid in remove_set:
            link = existing.get((tid, eid))
            if link is not None:
                db.delete(link)
                changed += 1
    db.flush()
    return changed


def tags_of(db: Session, company_id: int, entity_type: str, entity_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    if not entity_ids:
        return out
    parents = dict(db.query(Tag.id, Tag.name).filter(Tag.company_id == company_id))
    rows = (db.query(TagLink.entity_id, Tag).join(Tag, Tag.id == TagLink.tag_id)
            .filter(TagLink.company_id == company_id, TagLink.entity_type == entity_type,
                    TagLink.entity_id.in_(entity_ids)).order_by(Tag.sort_order, Tag.name).all())
    for eid, t in rows:
        out.setdefault(eid, []).append({"id": t.id, "uuid": str(t.uuid), "name": t.name, "color": t.color,
                                        "parent_id": t.parent_id, "parent_name": parents.get(t.parent_id)})
    return out


def tag_usage(db: Session, company_id: int) -> dict[int, int]:
    return dict(db.query(TagLink.tag_id, func.count(TagLink.id)).filter(TagLink.company_id == company_id)
                .group_by(TagLink.tag_id).all())


# ── Skills ───────────────────────────────────────────────────────────

def save_skill(db: Session, company_id: int, data: dict, row: Skill | None = None) -> Skill:
    data = dict(data)
    if "name" in data or row is None:
        data["name"] = _unique_name(db, Skill, company_id, data.get("name"), row.id if row else None, "skill")
    if data.get("category_id"):
        from core.masters import validate_value_ids
        validate_value_ids(db, company_id, "skill_category", [int(data["category_id"])])
    if row is None:
        row = Skill(company_id=company_id)
        db.add(row)
    _apply(row, data, ("name", "category_id", "description", "active"))
    db.flush()
    return row


def save_employee_skill(db: Session, company_id: int, employee_id: int, data: dict, row: EmployeeSkill | None = None,
                        can_verify: bool = False) -> EmployeeSkill:
    level = int(data.get("level", row.level if row else 3))
    if level not in SKILL_LEVELS:
        raise BadRequest("Skill level goes from 1 (beginner) to 5 (expert).")
    if row is None:
        skill = _by_id(db, Skill, company_id, data.get("skill_id"))
        if skill is None:
            raise BadRequest("Pick a skill from the library.")
        if db.query(EmployeeSkill.id).filter(EmployeeSkill.company_id == company_id, EmployeeSkill.employee_id == employee_id,
                                             EmployeeSkill.skill_id == skill.id).first():
            raise BadRequest(f"{skill.name} is already on the profile. Change its level instead.")
        row = EmployeeSkill(company_id=company_id, employee_id=employee_id, skill_id=skill.id)
        db.add(row)
    if data.get("years") is not None and not 0 <= float(data["years"]) <= 60:
        raise BadRequest("Years of experience should be between 0 and 60.")
    changed_level = row.level != level
    row.level = level
    if "years" in data:
        row.years = data["years"]
    if "notes" in data:
        row.notes = (data["notes"] or "").strip() or None
    if "verified" in data and can_verify:
        row.verified = bool(data["verified"])
        row.verified_by_name = context.current().actor_name if row.verified else None
        row.verified_at = datetime.now(timezone.utc) if row.verified else None
    elif changed_level and row.verified and not can_verify:
        row.verified, row.verified_by_name, row.verified_at = False, None, None  # a self-changed level needs checking again
    db.flush()
    return row


def skills_of(db: Session, company_id: int, employee_id: int) -> list[dict]:
    from core.masters import labels_for
    rows = (db.query(EmployeeSkill, Skill).join(Skill, Skill.id == EmployeeSkill.skill_id)
            .filter(EmployeeSkill.company_id == company_id, EmployeeSkill.employee_id == employee_id)
            .order_by(EmployeeSkill.level.desc(), Skill.name).all())
    cats = labels_for(db, company_id, [s.category_id for _, s in rows if s.category_id])
    return [{"uuid": str(es.uuid), "skill_id": s.id, "skill_name": s.name, "category_id": s.category_id,
             "category_name": cats.get(s.category_id), "level": es.level, "level_label": SKILL_LEVELS[es.level],
             "years": float(es.years) if es.years is not None else None, "verified": es.verified,
             "verified_by_name": es.verified_by_name, "verified_at": es.verified_at, "notes": es.notes}
            for es, s in rows]
