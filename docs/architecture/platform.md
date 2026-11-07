# Campusly: platform architecture and coding rules

This is the reference every module follows. If code and this document disagree, fix one of them.

## 1. Stack

| Layer | Choice |
|---|---|
| API | FastAPI (Python 3.12), SQLAlchemy 2, Pydantic 2, Alembic |
| Database | PostgreSQL (row-level security for tenant isolation) |
| Cache / queues | Redis |
| Web | Next.js (App Router) + Tailwind 4, TypeScript strict |
| Mobile | Expo app (later) |

## 2. Backend layout

```
backend/
  core/            # shared platform code (no business rules)
    db.py          # engine, session, tenant context, RLS setup
    context.py     # request context (company, employee, ip, request id, timezone)
    security.py    # auth helpers, permissions
    audit.py       # automatic audit of every change
    pagination.py, errors.py, timeutil.py, money.py
    expressions.py # safe rule/formula evaluator (no eval)
    masters.py, custom_fields.py, numbering.py, workflow/ (engine)
  modules/
    <module>/      # one folder per business module
      models.py  schemas.py  service.py  router.py  hooks.py (optional)
  alembic/         # migrations (each module adds its own revisions)
  tests/
```

- `router.py` only parses input, checks permission and calls `service.py`. Business rules live in `service.py`.
- Modules are discovered automatically (every `modules/*/router.py` that exposes `router` is mounted under `/api/v1`). Models are imported automatically for Alembic.
- No module imports another module's internals except through its `service.py` functions.

## 3. Tenant isolation (non-negotiable)

1. Every company-owned table has `company_id INTEGER NOT NULL REFERENCES companies(id)` and an index starting with `company_id`.
2. Postgres **row-level security** is enabled and **forced** on every company-owned table, with the policy:
   `company_id = current_setting('app.company_id')::int OR current_setting('app.bypass_rls') = 'on'`.
3. The API sets `app.company_id` for every transaction from the signed-in user's token (`SET LOCAL` via `set_config(..., true)` at transaction start). Platform admin endpoints and background jobs set `app.bypass_rls` explicitly and are audited.
4. Services also filter by `company_id` (defence in depth).
5. Tests prove company A can never read or change company B's data through any endpoint.

## 4. Time

- All timestamps are stored as `timestamptz` in UTC. Dates without time (`date`) are calendar dates in the company's own timezone (e.g. attendance date, leave date).
- Every employee has a `timezone` (IANA name, e.g. `Asia/Kolkata`). It defaults to the company timezone and can be detected from the browser.
- The API always returns ISO 8601 UTC (`...Z`). The web app formats every time in the viewer's timezone with the shared helpers in `src/lib/datetime.ts`. Never format dates by hand.

## 5. Audit (every change)

- An SQLAlchemy `before_flush` hook records every insert, update and delete on audited models into `audit_logs`: table, record id, action, changed fields with old and new values, company, employee or platform user, IP address, user agent, request id and time.
- `audit_logs` is append-only: a database trigger rejects UPDATE and DELETE.
- Sensitive values (passwords, tokens, full Aadhaar, bank account numbers) are masked in the log.
- Business events that are not plain row changes (sign in, export, approval decision, payroll lock) are written with `audit.event(...)`.

## 6. No delete

- Employee and other business records are never hard-deleted. They are archived (`archived_at`, `archived_by`) or deactivated.
- Hard delete is allowed only for drafts and for platform maintenance, and is audited.

## 7. Everything configurable

| Building block | What it gives |
|---|---|
| **Masters** (`master_types`, `master_values`) | Any list used in a dropdown (blood group, marital status, employment type, document type, relation, education level, exit reason, skill category …). System types are seeded; companies add values or their own types. Values can have a parent (sub-values) and extra attributes. |
| **Custom fields** (`custom_field_defs` + `custom` JSONB column) | Extra fields on employees and other records: text, number, date, yes/no, single/multi choice (from a master), file, with sections, order, required flag and validation. |
| **Number series** (`number_series`) | Formats and counters for employee IDs, requests, letters, assets, tickets: tokens such as `{PREFIX}{YYYY}{MM}{SEQ:5}`, reset yearly or monthly, per legal entity. |
| **Rules** (`core/expressions.py`) | A safe expression language for conditions (`employee.grade in ['M1','M2'] and request.days > 3`) and formulas (`round(basic * 0.4, 0)`). Parsed and evaluated without Python `eval`. |
| **Workflows** (`core/workflow`) | Approval chains for any request type, configured on screen. |
| **Versioned policies** | Policies (leave types, attendance rules, payroll rules) carry `version`, `effective_from`, `effective_to`. Changing a policy creates a new version; history is kept and old periods use old rules. |

## 8. Workflow engine

- `workflow_definitions`: company, request type (e.g. `leave.request`), name, version, active, applies-when condition, and steps. Each step: approver type (`reporting_manager`, `skip_level_manager`, `department_head`, `role`, `employee`, `field` on the record), mode (`any` or `all`), optional condition, SLA hours, escalation target, and whether the step may be skipped when the approver is the requester.
- `workflow_instances` and `workflow_tasks` record every run and every decision.
- Delegation: an approver can delegate to someone else for a date range.
- Modules register a handler per request type (`on_submit`, `on_approved`, `on_rejected`, `on_cancelled`) in `hooks.py`.
- One **Approvals inbox** lists all pending tasks for the signed-in person, with bulk approve.

## 9. API conventions

- REST under `/api/v1/<module>/...`. Plural nouns, `uuid` in URLs (never integer ids).
- List endpoints are paginated (`?page=&size=`, default 25, max 200) and return `{ items, total, page, size }`; they accept `q` for search and simple filters.
- Errors: `{ "detail": "plain sentence the user can act on" }` with proper status codes.
- Validation in Pydantic schemas; money is `Decimal` (2 places), never float.
- Every endpoint declares its permission, e.g. `require_permission("leave.request.approve")`.

## 10. Web conventions

- Pages live under `src/app/dashboard/<module>/`. Every page uses the shell primitives in `src/components/shell/page.tsx` (`PageHeader`, `Paper`, `EmptyState`, `Chip`, `btn`).
- Look: flat paper surfaces, hairline borders, no shadows, light font weights (300/400, never bold), thin line icons (lucide, stroke about 1.4), one indigo accent.
- Forms use shared inputs. Every list that has a master uses `MasterSelect`; every other choice uses the searchable `Select` (no plain `<select>` for lists longer than five options).
- Data fetching through `src/lib/api.ts`; lists paginate on the server.
- All dates and times through `src/lib/datetime.ts` (viewer timezone).
- Copy is plain, short and non-technical.

### UI kit

Shared components live in `src/components/ui-kit/` (import from `@/components/ui-kit`; every file starts with a usage comment). A live showcase is at `/dashboard/ui-kit` (not in the navigation).

**The rule: modals, not drawers and not separate pages.** Add, Edit and View-details all open in a centered `Modal` over the list. Clicking a table row opens a detail modal; its Edit button opens the form modal. Confirmations use `useConfirm()` (or `ConfirmDialog`). Deep links keep modal state in the address where useful (the employee directory uses `?new=1`, `?edit=<uuid>`, `?view=<uuid>`; old `/new` and `/<id>/edit` routes redirect there).

| Component | Use it for |
|---|---|
| `Modal` | Every add/edit/details flow. Sizes `sm` 400px, `md` 560, `lg` 720, `xl` 960, `full` (near full screen for big forms). Sticky header and footer, scrolling body; Escape, X and backdrop close it; `dirty` asks "Discard changes?" first; `busy` blocks closing while saving. Use `ModalCancel` / `useModalClose()` for guarded Cancel buttons and submit forms from the footer with `<Button type="submit" form="...">`. |
| `useConfirm()` / `ConfirmDialog` | Small confirmation before destructive or notable actions: `if (await confirm({ title, message, confirmLabel, tone: "danger" }))`. |
| `DataTable` | Every list: sortable columns, row actions menu, row click, bulk select with a floating action bar, sticky header, skeleton and empty states, pagination `{ page, size, total, onPageChange }` (server-driven; `paginate()` for client lists). |
| `FilterBar` | Above a table: debounced search plus filter chips (single or multiple choice). |
| `Field` + `TextInput`, `Textarea`, `NumberInput`, `DateInput`, `DateRangeInput`, `TimeInput`, `Toggle`, `Checkbox`, `RadioCards`, `FileDrop` | All form controls. `Field` adds the label, hint and error and wires ids/aria to the control inside it. `FormSection` and `FormGrid` lay out longer forms. Date values are `YYYY-MM-DD` strings. |
| `Select` / `MultiSelect` | Any choice from a list: searchable, keyboard friendly, clearable, static `options` or async `loadOptions(query)`. `MasterSelect` builds on it for master lists. |
| `Tabs`, `Stepper` | Sections of a detail view; multi-step forms inside a `Modal`. |
| `Avatar`, `Badge`, `Chip`, `StatCard`, `Timeline`, `KeyValue`, `Section`, `Skeleton`, `Menu`, `Button`, `IconButton` | Display building blocks. |
| `useToast()` | Feedback after an action: `toast.success("Saved")`, optional `{ description, action }`. |

Floating layers (menus, select lists, date pickers) are portaled to `document.body`, so overflow containers never clip them. Modals trap focus and restore it on close; motion stays under 200 ms and respects reduced-motion settings.

## 11. Performance

- Indexes on every foreign key and every `company_id`-first lookup; no N+1 queries (use `selectinload`/`joinedload`).
- Pages load only what they show; heavy work (payroll run, imports, reports) runs as background jobs with progress.
- Responses are gzipped; static assets cached.

## 12. Tests

- `backend/tests/` with pytest against a separate database (`hrms_test`).
- Must-have tests: tenant isolation, audit on change, workflow approve/reject, each module's core calculation (leave balance, attendance status, payroll).

## 13. How the building blocks are implemented

Short notes for anyone adding a module. Read the module docstrings for detail.

**Request context and RLS** (`core/context.py`, `core/db.py`, `core/rls.py`)
- `RequestContextMiddleware` puts one mutable `RequestContext` per request in a context variable (company, employee, actor type and name, IP, user agent, request id). FastAPI runs sync code in threads with a copy of the context, so the object is mutated, never replaced.
- `get_current_user` fills it from the token and calls `set_tenant(db, company_id)`. Every transaction starts with `set_config('app.company_id', …, true)` and `set_config('app.bypass_rls', …, true)` (SQLAlchemy `after_begin`).
- The policy is `company_id = NULLIF(current_setting('app.company_id', true), '')::int OR current_setting('app.bypass_rls', true) = 'on'` (the `NULLIF`/`missing_ok` form so an unset value never errors). Departments and designations also let everyone read shared rows with `company_id IS NULL`.
- Superusers skip RLS, so when the API connects as one (local development) it switches to the unprivileged `hrms_app` role with `SET ROLE` on every new connection (`DB_APP_ROLE`). In production connect as a normal role; `FORCE ROW LEVEL SECURITY` makes the table owner obey the policy too.
- Platform admin endpoints (`current_platform_user`), cross-company sign-in lookups (`with bypass_rls(db)`), startup seeding and background jobs (`system_session()`) bypass RLS explicitly. `tenant_session(company_id)` gives a job a session scoped to one company.
- New company-owned table checklist: `company_id NOT NULL` + FK, an index starting with `company_id`, and in the migration `for sql in enable_tenant_rls_sql("table"): op.execute(sql)`. New rows get `company_id` from the context when left empty.

**Audit** (`core/audit.py`)
- `before_flush` collects inserts, updates (field old/new from attribute history) and deletes; `after_flush` writes them with one INSERT in the same transaction. Opt out with `__audit__ = False` (only for noise such as counters and notifications).
- Masking: names containing password/secret/token/otp/reset are replaced; aadhaar/account/card/UAN keep the last 4 characters; data URLs and long text become "(N characters)".
- `audit.event(db, "export.downloaded", "employees", id, {...})` for business events. Sign-in, approvals, escalations and company removal already use it.
- A trigger makes `audit_logs` append-only; only platform company removal sets `app.audit_purge` for its own transaction.

**Time** (`core/timeutil.py`, web `src/lib/datetime.ts`)
- DB connections use UTC. `iso_utc()` formats `…Z` for hand-built responses; Pydantic models already do.
- `employees.timezone` NULL means the company's. `/auth/me` returns `timezone`, `timezone_source` and `company_timezone`; `PUT /auth/me/timezone` saves the signed-in person's own (owner's lives in `companies.config.owner_timezone`).

**Masters** (`core/masters.py`, `core/master_seed.py`, API `/masters`)
- Each company owns its copy of every list (seeded by `ensure_system_masters`, called from `seed.setup_company`). System types/values cannot be removed or have their code changed; companies add values, rename, reorder and switch off.
- Store the master value **id** in records. Pickers use `GET /masters/options/{key}?q=&ids=`.

**Custom fields** (`core/custom_fields.py`, API `/custom-fields`)
- Values live in the record's `custom` JSONB column keyed by field key. Always pass submitted values through `clean_values(...)` (type, required, limits, master ids, unknown keys).

**Number series** (`core/numbering.py`, API `/number-series`)
- `next_number(db, company_id, "key")` returns the next formatted number or None when the series is off. Counters per period are bumped with one atomic upsert.

**Rules** (`core/expressions.py`, API `/rules/evaluate`)
- `evaluate(text, variables, today=…)`, `check_condition(text, variables)`, `validate(text, allowed_fields)`. Never use Python `eval`.

**Workflows** (`core/workflow/`, API `/workflow`)
- Register a request type in your module's `hooks.py` with `registry.register(RequestType(key=…, label=…, on_submit, on_approved, on_rejected, on_cancelled, variables, default_steps, summarize))`. Start a request with `workflow.service.submit(db, company_id, key, payload, title)`.
- Editing a flow creates a new version (same `lineage_id`); running requests keep their version. Without any matching flow a request is approved at once; when no approver can be found the company owner gets the task.
- `escalate_overdue()` runs every 15 minutes in the API process (one process at a time via an advisory lock) and can be triggered with `POST /workflow/escalations/run`.

**Company setup** (`seed/seed_data.py`)
- `setup_company(db, company_id)` gives a company its roles, master lists, number series and default approval flows. It runs on sign-up, when the platform admin creates a company and at every start (it only adds what is missing).
