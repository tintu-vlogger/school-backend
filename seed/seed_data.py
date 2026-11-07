"""Startup data: the first platform admin, system roles for every company, and in
development a demo company owner with a known login."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core.config import get_settings
from core.db import system_session

DEMO_COMPANY = {
    "company_name": "Campusly Demo",
    "slug": "campusly-demo",
    "email": "tintu@test.com",
    "password": "Welcome9#",
    "owner_name": "Tintu C Raju",
}


def ensure_default_roles(db: Session, company_id: int) -> None:
    """Create the system roles (HR admin, HR, manager, employee) for a company, and keep
    their permissions in step with the product defaults. Custom roles are left alone."""
    from modules.roles.catalog import DEFAULT_FIELD_ACCESS, DEFAULT_SCOPES
    from modules.roles.models import DEFAULT_ROLE_PERMISSIONS, SYSTEM_ROLES, Role

    for role_name, (display, description) in SYSTEM_ROLES.items():
        perms = DEFAULT_ROLE_PERMISSIONS.get(role_name, [])
        scopes = DEFAULT_SCOPES.get(role_name, {})
        fields = DEFAULT_FIELD_ACCESS.get(role_name, {})
        role = db.query(Role).filter(Role.company_id == company_id, Role.role_name == role_name).first()
        if role is None:
            db.add(Role(company_id=company_id, role_name=role_name, display_name=display,
                        description=description, permissions=perms, scopes=scopes, field_access=fields,
                        is_system=True))
        elif role.is_system and (role_name == "admin" or not role.customised):
            # Only touch what differs, so restarts don't fill the audit log.
            if role.display_name != display:
                role.display_name = display
            if role.description != description:
                role.description = description
            if list(role.permissions or []) != list(perms):
                role.permissions = list(perms)
            if (role.scopes or {}) != scopes:
                role.scopes = dict(scopes)
            if (role.field_access or {}) != fields:
                role.field_access = dict(fields)
    db.commit()


def setup_company(db: Session, company_id: int) -> None:
    """Everything a company needs to start: system roles, master lists, number series
    and default approval flows. Safe to run again; it only adds what is missing."""
    from core.masters import ensure_system_masters

    ensure_default_roles(db, company_id)
    ensure_system_masters(db, company_id)
    _extra_setup(db, company_id)
    db.commit()


def _extra_setup(db: Session, company_id: int) -> None:
    """Hooks for later building blocks (number series, approval flows)."""
    try:
        from core.numbering import ensure_default_series
        ensure_default_series(db, company_id)
    except ImportError:
        pass
    try:
        from core.workflow.service import ensure_default_definitions
        ensure_default_definitions(db, company_id)
    except ImportError:
        pass
    try:
        from modules.organization.seed import ensure_defaults
        ensure_defaults(db, company_id)
    except ImportError:
        pass
    # The owner's own employee record (self-service for the owner).
    from modules.company.owner import ensure_owner_employee
    ensure_owner_employee(db, company_id)
    _module_setup(db, company_id)


def _module_setup(db: Session, company_id: int) -> None:
    """Each module may ship modules/<name>/seed.py with setup_company(db, company_id) for its
    own defaults (categories, templates, folders …). Called on sign-up and at every start."""
    import importlib
    from modules import module_names
    for name in module_names():
        try:
            mod = importlib.import_module(f"modules.{name}.seed")
        except ModuleNotFoundError as exc:
            if exc.name == f"modules.{name}.seed":
                continue
            raise
        fn = getattr(mod, "setup_company", None)
        if fn:
            fn(db, company_id)
            db.flush()


def _module_demo(db: Session, company_id: int) -> None:
    """Development only: modules/<name>/seed.py may define seed_demo(db, company_id) to fill
    the demo company with sample records. Each must be idempotent; a failure in one module
    never stops the others."""
    import importlib
    import logging
    from modules import module_names
    for name in module_names():
        try:
            mod = importlib.import_module(f"modules.{name}.seed")
        except ModuleNotFoundError:
            continue
        fn = getattr(mod, "seed_demo", None)
        if not fn:
            continue
        try:
            fn(db, company_id)
            db.commit()
        except Exception as exc:  # demo data must never block start-up
            db.rollback()
            db.info["company_id"] = company_id
            logging.getLogger("hrms").warning("Demo data for %s failed: %s", name, exc)


def seed_platform_admin(db: Session) -> None:
    """Create the first platform admin when none exists. Production must set the
    credentials in the environment; development gets a known login."""
    from core.security import hash_password
    from modules.platform.models import PlatformUser

    if db.query(PlatformUser.id).filter(PlatformUser.role == "admin").first():
        return
    settings = get_settings()
    email = (settings.PLATFORM_ADMIN_EMAIL or "").strip().lower()
    password = settings.PLATFORM_ADMIN_PASSWORD or ""
    if not email or len(password) < 10:
        if settings.is_production:
            print("[seed] No platform admin: set PLATFORM_ADMIN_EMAIL and PLATFORM_ADMIN_PASSWORD (10+ chars)")
            return
        email, password = "admin@campusly.dev", "Platform9#admin"
    db.add(PlatformUser(name="Platform admin", email=email, role="admin", password=hash_password(password)))
    db.commit()
    print(f"[seed] Platform admin created: {email}")


def seed_demo_company(db: Session) -> None:
    """Development only: a ready-to-use company owner login."""
    from core.security import hash_password
    from modules.company.models import Company

    d = DEMO_COMPANY
    if db.query(Company.id).filter(Company.email == d["email"]).first():
        return
    db.add(Company(
        company_name=d["company_name"],
        slug=d["slug"],
        email=d["email"],
        password=hash_password(d["password"]),
        country="India",
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=30),
        config={"owner_name": d["owner_name"], "plan": "standard"},
    ))
    db.commit()
    print(f"[seed] Demo company created: {d['email']} / {d['password']}")


def _daily_people_tasks(db: Session) -> None:
    """Apply job changes whose date has come and send document expiry reminders."""
    from modules.company.models import Company
    from modules.employees import documents, service
    for (company_id,) in db.query(Company.id).all():
        db.info["company_id"] = company_id
        service.apply_due_changes(db, company_id)
        documents.send_expiry_reminders(db, company_id)
    db.info.pop("company_id", None)
    db.commit()


def run_seed() -> None:
    from modules.company.models import Company

    db = system_session()
    try:
        seed_platform_admin(db)
        if not get_settings().is_production:
            seed_demo_company(db)
        demo_id = None
        for (company_id, slug) in db.query(Company.id, Company.slug).all():
            db.info["company_id"] = company_id
            setup_company(db, company_id)
            if not get_settings().is_production and slug == DEMO_COMPANY["slug"]:
                demo_id = company_id
        db.info.pop("company_id", None)
        if demo_id is not None:
            # Staff first, so the school demo data (classes, teachers, students) can use them.
            from modules.organization.seed import seed_demo_people
            seed_demo_people(db)
            db.info["company_id"] = demo_id
            _module_demo(db, demo_id)
            db.info.pop("company_id", None)
        _daily_people_tasks(db)
        print("Seed data ready.")
    except Exception as e:
        db.rollback()
        print(f"Seed error: {e}")
    finally:
        db.close()
