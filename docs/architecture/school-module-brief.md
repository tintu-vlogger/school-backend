# Brief for Campusly module builders

Campusly is a multi-tenant SaaS for running **any educational institution**: schools (K-12),
preschools, colleges, universities, coaching/tuition centres and training institutes. An owner
signs up, gets a tenant with its own sub-domain (`<slug>.campusly.app`, locally
`<slug>.localhost:3200`), sets the institution up and invites staff, parents and students.

It was built on a mature multi-tenant HR platform, so staff management (employees, staff
attendance, leave, payroll, recruitment, documents, approvals, notifications, audit, roles,
custom fields, masters, number series, workflows) already exists. Your job is to build **one
school module** to the same quality bar. Several builders work in parallel on different
modules. The product owner is not available: never ask questions, decide sensibly, and record
decisions in your final report.

## Read first
1. `backend/docs/architecture/platform.md`: architecture and coding rules (tenant isolation with
   row-level security, audit, time, masters, custom fields, number series, rules, workflow
   engine, API and web conventions, the UI kit, building blocks). Wherever it says company/
   employee/HR, read institution/staff. Follow it exactly.
2. This brief.
3. The foundation modules you build on (read the code, it is short):
   - `backend/modules/academics/` (models, service, router): sessions, terms, programs
     (= class / programme / course), sections (= section / batch / group), subjects, curriculum
     (`ProgramSubject`), teacher allocation (`SectionSubject`), rooms, houses, institution
     profile (type, the words used on screen, modules switched on, attendance mode, grading).
   - `backend/modules/students/` (models, service, router): `Student`, `Guardian`,
     `StudentGuardian`, `Enrollment` (student x session -> program, section, roll no, status),
     `PortalAccount` (parent / student sign-in), `StudentDocument`.
   - `backend/core/crud.py`: `crud_router(...)` gives list/create/read/update/archive endpoints
     for simple set-up tables in a few lines. Use it for plain lists.
   - Front end: `frontend/src/lib/institution.tsx` (`useInstitution()` -> `term("program")`,
     `term("sections")`, `modules`, `profile`), `frontend/src/lib/hr-nav.ts` (navigation; staff
     sections and `PORTAL_SECTIONS` for parents and students), `frontend/src/lib/access.tsx`
     (`useAccess()`: `can(...)`, `portalRole`).
4. Look at finished pages before writing yours: web `src/app/dashboard/departments`,
   `src/app/dashboard/staff`, `src/app/dashboard/inbox`, `src/components/ui-kit/*`
   (live showcase at `/dashboard/ui-kit`). Backend `modules/departments`, `modules/leave`
   (workflow hooks), `modules/helpdesk`.
5. Look at how the major institution systems handle your area (PowerSchool, Fedena, Entab
   CampusCare, Skolaro, MyClassboard, Teachmint, Classter, Ellucian, OpenEduCat, Moodle for
   learning) and cover what a real institution needs. Our design is our own.

## Flexibility is the point
- Never hard-code "class", "section", "teacher" or "parent" in UI copy where the institution's
  own word applies: use `term("program")`, `term("section")`, `term("teacher")`,
  `term("guardian")`, `term("student")` etc.
- Work for both one-year classes (schools) and term/semester-based programmes with credits
  (colleges, universities), and for short courses with batches (coaching, training).
- Everything configurable through settings screens with sensible defaults seeded per
  institution; nothing institution-specific in code.
- Each module has a key in the institution profile (`admissions, attendance, timetable, exams,
  lms, online_classes, fees, library, transport, hostel, wellbeing, comms`); your nav entries
  carry `module: "<key>"` so an institution that switches the module off does not see it.

## Who uses the product (system roles)
`admin` (owner / administrator: everything), `principal` (academic head), `hr` (office / HR),
`manager` (head of department), `teacher`, `accountant`, `librarian`, `employee` (other staff),
and the two **portal** roles `parent` and `student`.

- Staff sign in through their employee record (`current_user["employee_id"]`). A teacher's
  sections: `academics.service.teacher_section_ids(db, company_id, employee_id)` and
  `academics.service.my_classes(...)`.
- Parents and students sign in through `PortalAccount`. Their `current_user` has
  `role` = `"parent"`/`"student"`, `portal_id`, `student_id` (student) or `guardian_id`
  (parent), and `employee_id = None`.
  - Which learners they may see: `students.service.visible_student_ids(db, user)` (None for
    staff) and `students.service.require_student_access(db, user, student_id)`.
  - `core.security.require_portal("parent", "student")` dependency for portal-only endpoints.
  - **Portal tokens are blocked from every API path not explicitly opened.** In your module's
    `hooks.py` call `register_portal_prefix("/fees/portal")` (from `core.security`) for the
    path prefixes your portal endpoints use, and keep portal endpoints under a clear prefix
    such as `/<module>/portal/...`. Every portal endpoint must still check access itself.
- Permissions: in your module's `hooks.py` call
  `modules.roles.models.register_permissions({key: label}, {role: [keys]})`. Use your
  module prefix (`attendance.`, `timetable.`, `exams.`, `lms.`, `fees.`, `library.`,
  `transport.`, `hostel.`, `wellbeing.`, `comms.`, `admissions.`); give sensible defaults to
  principal / teacher / accountant / librarian / hr and to `parent` / `student` for portal
  keys. Administrators always get every key. The nav entries already reference the keys
  `<module>.view`, `<module>.manage` etc.; keep those names or update the nav entries.
- Number series: `core.numbering.register_series(key, name, prefix, pattern, reset)` from
  `hooks.py` (e.g. receipts, invoices, enquiries, library accession numbers).
- Notifications: staff -> `modules.notifications.service.notify(db, company_id,
  [employee_ids], kind=..., title=..., body=..., link=...)`; parents/students ->
  `notify_portal(db, company_id, portal_account_ids, ...)` with
  `students.service.portal_account_ids_for_students(db, company_id, student_ids,
  parents=True, students=True)`.
- Approvals (e.g. leave requested by a parent, fee concession, refunds): register a request
  type with the workflow engine in `hooks.py` (see `modules/leave/hooks.py`).

## Environment
- Backend `/Users/tintucraju/Projects/Personal/school/backend`, venv `.venv`, run with
  `PYTHONPATH=.` (e.g. `PYTHONPATH=. .venv/bin/python -m pytest ...`). Local Postgres DB `school`
  (user tintucraju); tests create and drop their own temporary database.
- The coordinator's API runs at http://127.0.0.1:8200 and the web app at http://localhost:3200
  (webpack dev server with hot reload; your new pages appear there by themselves). **Do not
  restart or stop them.** To try your endpoints run your own API copy on your own port:
  `cd backend && PYTHONPATH=. .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port <yours>`
  and stop it when done. The web app talks to :8200, which won't have your new code until the
  coordinator restarts it; check your API with your own copy and your pages with the type
  checker, then (optional) point a headless browser at your pages once :8200 is restarted.
- Logins (password `Welcome9#` for all; institution address http://campusly-demo.localhost:3200):
  owner/admin `tintu@test.com`, principal `kavitha.raghavan@example.com`, teacher
  `divya.menon@example.com` (class teacher of Grade 8 - A, teaches Computer Science in
  Grades 6-10), accountant `ganesh.pillai@example.com`, parent `parent@test.com` (children
  Aarav Kumar, Grade 8 - A and Anika Kumar, Grade 3 - A), student `student@test.com`
  (Aarav Kumar). Platform admin `admin@campusly.dev` / `Platform9#admin`.
- Headless browser (optional): `playwright-core` is in `frontend/node_modules`; use
  `executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"`, headless,
  and end scripts with `process.exit(0)`. Put scripts and screenshots in your scratchpad and
  delete screenshots after looking at them. Disk space is limited: never create extra
  databases or copy large files.
- **Other builders edit the same tree at the same time.** If the app fails to import because of
  someone else's half-written module, wait a minute and retry; never edit their files. Keep
  your own files importable at all times (write models first and check
  `PYTHONPATH=. .venv/bin/python -c "import main"` after each big change).

## Where your code goes
- Backend: `backend/modules/<module>/` with `__init__.py, models.py, schemas.py (or inline),
  service.py, router.py, hooks.py, seed.py`. Tests: `backend/tests/test_<module>*.py`.
  - `seed.py`: `setup_company(db, company_id)` for defaults every institution gets (must be
    idempotent and must never raise), and `seed_demo(db, company_id)` that fills the demo
    institution with realistic Indian sample data (idempotent; runs after the foundation demo
    data, so classes, sections, teachers, students and parents exist).
- Migrations: your own Alembic revision(s) with `down_revision = "sc1b2c3d4e5f"` for the first
  one and a revision id starting with your module's 2-3 letter prefix. Several heads are
  expected; **do not create merge revisions** (the coordinator merges). Run
  `PYTHONPATH=. .venv/bin/python -m alembic upgrade heads`. Every company-owned table needs
  `company_id NOT NULL` + FK, an index starting with `company_id`, and RLS in the migration:
  `for sql in enable_tenant_rls_sql("table"): op.execute(sql)` (see
  `alembic/versions/sc1a2b3c4d5e_*.py`). `tests/test_tenant_isolation.py` fails otherwise.
  Generate with `alembic revision --autogenerate` and then **delete anything in the generated
  file that is not your own tables** (other builders' tables may show up).
- Front end: staff pages under `frontend/src/app/dashboard/<route>/` (routes already listed in
  `src/lib/hr-nav.ts` for your module: build them, and remove `intro`/`features` from your
  entries once the real page exists; you may add or rename entries in your own area only).
  Portal pages (parents and students) under `frontend/src/app/dashboard/my/<area>/page.tsx`
  (entries already in `PORTAL_SECTIONS`). Components in `frontend/src/components/<module>/`.
  A parent with several children picks the child with a switcher; use
  `GET /students/mine` for the list.
- Home page cards: export `StaffHomeCard` (shown to staff who can see your module) and
  `PortalHomeCard({ studentId })` (shown to parents/students) from
  `frontend/src/components/<module>/home-cards.tsx`, backed by small summary endpoints
  (`GET /<module>/summary`, `GET /<module>/portal/summary?student_id=`). The coordinator wires
  them into the home page.
- Shared files you may touch with small careful edits: `src/lib/hr-nav.ts` (your entries),
  `core/master_seed.py` (append master types). Do not edit `core/security.py`,
  `modules/auth`, `modules/academics`, `modules/students`, `modules/roles` or the app shell; if
  you truly need a change there, describe it in your report.

## Quality bar
- Every endpoint declares its permission (or portal access check); services filter by
  `company_id` on top of RLS; records addressed by uuid in URLs; lists paginated
  (`{items, total, page, size}`) with `q` search and filters; money as `Decimal`.
- Every change audited (automatic for models; `audit.event()` for business events such as
  publishing results or collecting a payment). Times stored in UTC, shown with
  `src/lib/datetime.ts`. No hard deletes of business records.
- UI: the shared UI kit only. **Add / Edit / View details in centered modals (never separate
  pages, never drawers)**; `DataTable` + `FilterBar` for lists; `Select` / `MasterSelect`;
  `PageHeader` on every page; flat paper style, thin fonts (300/400), lucide icons with
  `strokeWidth` about 1.4; plain, short copy; empty states that tell people what to do next.
  It must look like a polished, modern original product, and work on a phone for the portal.
- Printable documents (receipts, report cards, hall tickets, certificates, ID cards) as PDF via
  the existing PDF helpers (`modules/documents/pdf.py`, weasyprint) or a print-friendly page.
- Tests: pytest for your core calculations and rules, permissions, portal access (a parent
  never sees another family's child) and tenant isolation of your tables. Run only your own
  test files plus `tests/test_tenant_isolation.py`. `npx tsc --noEmit -p .` (in frontend/)
  must be clean for your files.
- Never mention Claude, Anthropic or AI anywhere (code, comments, docs, copy, file names).
- **Do not run git commands that change anything** (no add/commit/push/stash/checkout); the
  coordinator commits.

## When done
Append a section for your module to `backend/docs/PROGRESS.md` (what is built, pages,
endpoints, tables, permissions, tests, decisions, anything unfinished) and reply with the same
summary, including the exact names of your Alembic revision ids and your home-card exports.
