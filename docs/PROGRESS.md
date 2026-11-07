# Progress

## Foundations

Built 7 November 2026. Backend tests: **88 passed** (`PYTHONPATH=. .venv/bin/pytest`, database `hrms_test`). `alembic upgrade head` is applied to `hrms` and `alembic check` is clean; the migrations also downgrade and upgrade again cleanly. Web: `npx tsc --noEmit -p .` is clean. The key pages were checked in headless Chrome.

### Backend

**1. Restructure.** Shared code moved to `core/`: config, db, context, security, audit, cache, idempotency, timeutil, pagination, errors, images, email, expressions, masters, custom_fields, numbering, rls and workflow/. Each business domain now has its own folder under `modules/`:

- auth, company, employees, departments, designations, roster, roles, audit, notifications, whatsapp
- platform (admin and partner; this includes the UPI subscription payments), settings (subscription), public_companies
- new: masters, custom_fields, numbering, rules, workflow

Every `modules/*/router.py` that exposes `router` (plus `extra_routers`) is mounted under `/api/v1` automatically. Models are imported automatically for Alembic through `modules.import_all_models()`, and `hooks.py` files through `import_all_hooks()`. The API paths are exactly the same as before; I checked this by diffing the route lists.

**2. Tenant isolation (RLS).**
- **Request context.** `RequestContextMiddleware` creates one context per request: company, actor, IP, user agent and request id, plus an `X-Request-ID` response header. `get_current_user` fills it from the JWT and scopes the session to the company.
- **Transaction start.** Every transaction begins with `set_config('app.company_id', …, true)` and `set_config('app.bypass_rls', …, true)`.
- **Policy.** RLS is enabled and **forced** on every table that has `company_id`. Departments and designations also let every company read the shared rows where `company_id` is NULL.
- **Superuser login.** Superusers skip RLS, so when the API logs in as one it switches to the unprivileged `hrms_app` role on every new connection.
- **Explicit bypass.** Platform admin endpoints, sign-in lookups across companies, seeding and the escalation job all bypass RLS on purpose.
- **Schema change.** `employee_settings` and `roster_entries` gained `company_id`.
- **Tests.** Company A cannot list, read, update or deactivate company B's employees, departments, roles, audit log, lists, custom fields or approvals, either through the API or directly through a session (raw SELECT and UPDATE match nothing, and an INSERT for another company fails). A guard test fails if any table with `company_id` lacks forced RLS.

**3. Audit.**
- **Automatic.** `before_flush`/`after_flush` audit every insert, update and delete, with old and new values per field, the actor, IP, user agent and request id.
- **Masking.** Passwords, tokens and secrets are masked. Aadhaar, account, card and UAN numbers keep only their last 4 digits. Images and long text are shortened.
- **Append-only.** A trigger makes `audit_logs` append-only. The only exception is when the platform removes a company, and that removal is itself recorded as an event.
- **Events.** `audit.event()` records sign-in (company and platform), approval decisions, reassignment, escalation and company removal.
- **API.** `/audit/` is paginated and filters by record type, person, action, record and date range (the dates are in the viewer's timezone). `/audit/filters` supplies the filter choices.

**4. Timezone.**
- **Storage.** `employees.timezone` (empty means the company's timezone). The owner's own timezone is kept in the company config.
- **API.** `/auth/me` returns `timezone`, `timezone_source` and `company_timezone`, and `PUT /auth/me/timezone` saves it. Database connections use UTC and the API returns `…Z`.

**5. Masters.**
- **Tables.** `master_types` and `master_values`: values can have a parent, attributes, an order and an on/off switch.
- **Seeded lists, per company:**
  - People: gender, marital status, blood group, relation, nationality, religion
  - Work: employment type, employee status, exit reason with sub-reasons, document type with sub-types, education level, leave unit
  - Places and banks: 42 banks with IFSC prefixes, all 36 states and union territories with GST codes, 59 countries
  - Categories: skill category, asset category
- **API.** Search, add, edit, reorder and switch off, plus `/masters/options/{key}` for pickers.

**6.** The searchable select work is in the web section below.

**7. Custom fields.**
- **Tables.** `custom_field_sections` and `custom_field_defs`, with 11 field types. Choices can come from a master list or be typed in, and each field has required, help text, placeholder, validation and employee visibility/editing settings.
- **Storage.** Values go in `employees.custom` (JSONB). Values from a master list are stored as master ids.
- **Validation.** `clean_values()` checks every value on employee create and update.

**8. Number series.**
- **Tables.** `number_series` and `number_series_counters`.
- **Formats.** Tokens `{PREFIX} {YYYY} {YY} {MM} {DD} {FY} {SEQ:n}`; the counter can restart never, yearly, every financial year or monthly. Counters are bumped with one atomic upsert.
- **Default series.** Employee ID (off until a company switches it on), requests, letters, assets and tickets.
- **Employee codes.** When the employee series is on, new employees get the next code automatically. Employee codes are now unique within a company.

**9. Rules.** `core/expressions.py` is a tokeniser plus precedence-climbing parser and tree evaluator. It uses no `eval`, does its arithmetic in Decimal, and gives plain errors with the character position. It supports:
- and/or/not, comparisons, in/not in, between, + - * / %, lists and dotted fields
- min, max, round, floor, ceil, abs, if (the branch not taken is never evaluated), coalesce, len, lower, upper, contains
- today, date, days_between, months_between, years_between, add_days, year, month, day, weekday

`/rules/evaluate` and `/rules/functions` expose it. 47 unit tests cover it, including attempts to escape into Python.

**10. Workflow engine.**
- **Tables.** `workflow_definitions` (versioned by `lineage_id`, with an "applies when" condition and an order), `workflow_instances`, `workflow_tasks` and `workflow_delegations`.
- **Approvers.** reporting manager (`employees.reporting_manager_id` added, with loop checks), manager's manager, department head, role, a specific employee, a field on the request, or the company owner.
- **Steps.** Each step has an any/all mode, an optional condition, an SLA in hours, an escalation target, and a skip-if-requester setting.
- **Actions.** submit, approve, reject (a comment is required), cancel, reassign, delegate and escalate.
- **Escalation.** `escalate_overdue()` runs every 15 minutes in the API process; an advisory lock keeps it to one process at a time. It can also be triggered with `POST /workflow/escalations/run`.
- **Handlers.** A registry lets each module register its request types and handlers in `hooks.py`.
- **API.** Flow setup, the inbox (with counts and bulk decisions), requests and delegations.
- **Sample request type.** `employee.profile_change` (`POST /me/profile-change`) lets an employee change their name, title, phone, timezone or custom fields. It works end to end: by default it goes to HR, and to the company owner if nobody holds the HR role; on approval the change is applied and audited. Only one such request can be pending per employee.

### Web (frontend)
- **UI kit** in `src/components/ui-kit/`. It was built with a helper agent and is documented in platform.md §10:
  - containers: Modal (sizes sm to full, dirty guard), ConfirmDialog/useConfirm, Tabs, Stepper
  - lists: DataTable (sort, row menu, bulk bar, server paging, skeletons), FilterBar
  - inputs: all field types, Select/MultiSelect (async, keyboard, create inline), MasterSelect, date and range, FileDrop
  - display: Toast, Avatar, Badge, StatCard, Timeline, KeyValue, Skeleton
- **Modal pattern.** Employee, department and designation add, edit and view now open in modals.
- **Time.**
  - `src/lib/datetime.ts` provides formatDate, formatTime, formatDateTime, formatRelative and the other date helpers, all in the viewer's timezone. Legacy browser zone names such as Asia/Calcutta map to current ones.
  - Ad-hoc date formatting was replaced across the tenant and platform admin pages.
  - The app offers to save the browser's timezone, on the overview and profile pages, when it differs from the saved one. The profile page also has a timezone setting.
- **Pages:**
  - Audit log (field changes old → new, grouped by day, with filters)
  - Lists/Masters
  - Custom fields (with a live preview)
  - Numbering (with a live preview)
  - Rules tester
  - Approval flows (step editor, versions)
  - Approvals inbox (waiting / decided / my requests / delegation, bulk approve or reject, request timeline, pass on, withdraw, request a change of details)
- **CustomFields renderer.** Used on the employee form, the details modal and the profile.
- **Employee form.** Now has reporting manager and timezone. The employee list pages on the server. The last plain selects with more than five options were replaced.
- **Navigation.** Approvals, Rules, Approval flows, Custom fields and Numbering open the real pages, and Masters was added under Settings.

### New tables
`master_types`, `master_values`, `custom_field_sections`, `custom_field_defs`, `number_series`, `number_series_counters`, `workflow_definitions`, `workflow_instances`, `workflow_tasks`, `workflow_delegations`.

Columns added to existing tables:
- `employees`: `timezone`, `reporting_manager_id`, `custom`
- `employee_settings` and `roster_entries`: `company_id`
- `audit_logs`: `changes`, `actor_name`, `platform_user_id`, `entity_uuid`, `entity_label`, `ip_address`, `user_agent`, `request_id`

Migrations: p1q2r3s4t5u6 → w8x9y0z1a2b3.

### New endpoints (under /api/v1)
- `/audit/` (paginated), `/audit/filters`
- `/auth/me/timezone`
- `/employees/?page=` (paginated), `/employees/lookup`, `/me/profile`, `/me/profile-change`
- `/masters/types[/{key}[/values[/reorder]]]`, `/masters/values/{uuid}`, `/masters/options/{key}`
- `/custom-fields/meta`, `/custom-fields/{entity}`, `/custom-fields/{entity}/sections[/reorder]`, `/custom-fields/sections/{uuid}`, `/custom-fields/{entity}/fields[/reorder]`, `/custom-fields/fields/{uuid}`
- `/number-series/`, `/number-series/{uuid}`, `/number-series/preview`
- `/rules/functions`, `/rules/evaluate`
- `/workflow/meta`, `/workflow/request-types`, `/workflow/definitions[/{uuid}]`, `/workflow/inbox`, `/workflow/inbox/count`, `/workflow/tasks/{uuid}/decision`, `/workflow/tasks/bulk-decision`, `/workflow/tasks/{uuid}/reassign`, `/workflow/requests[/{uuid}[/cancel]]`, `/workflow/delegations[/{uuid}/stop]`, `/workflow/escalations/run`

### Tests
| File | Tests | What it proves |
|---|---|---|
| `test_tenant_isolation.py` | 8 | Isolation through the API and directly through a session, plus the guard that every company table has forced RLS |
| `test_audit.py` | 5 | Changes and sign-in are recorded, filters work, the log is append-only and isolated |
| `test_employees.py` | 4 | Timezone, manager loops, pagination, lookup |
| `test_masters.py` | 5 | Lists and values, including isolation between companies |
| `test_custom_fields.py` | 4 | Field definitions and value checks |
| `test_numbering.py` | 5 | Number formats and employee codes |
| `test_expressions.py` | 47 | The rules language |
| `test_workflow.py` | 10 | Default flow, rejection, multi-step any/all with a condition, cancel, reassign, delegate, escalation, versions, bulk approve, invalid flows, isolation |

All 88 pass.

### Decisions
- **RLS policy form.** The policy uses `NULLIF(current_setting('app.company_id', true), '')::int`, so an unset value never raises an error. Local superuser logins drop to the `hrms_app` role (setting `DB_APP_ROLE`); in production, connect as a non-superuser.
- **Tables without RLS.** `companies` (the tenant table itself), `platform_users`, `platform_settings` and `payouts` are not RLS tables because they have no `company_id`. Platform-level audit entries have a NULL `company_id` and are visible only with the bypass.
- **Shared templates.** Departments and designations keep NULL-company shared rows, which every company can read but not change.
- **Lists are copied per company.** Each company gets its own copy of the system lists rather than sharing one set, so it can rename, reorder and switch values off. System values can't be removed or have their code changed.
- **Custom field storage.** Custom values live in one JSONB column on the record; values from a master list store the master id.
- **Flow versions.** Steps are kept as JSON inside each flow version, so a version never changes after it is saved. A request with no matching flow is approved at once. When no approver can be found, the company owner decides. The owner's own requests skip the steps where the owner would be the approver.
- **Existing modules kept their routers.** The older modules (auth, departments, roster, platform …) were moved as they were; their logic is still in the routers. Employees now has a `service.py`, and every new module follows the router/service split.
- **List endpoints.** `/employees/` paginates only when `page` is passed, so older screens keep working. The older department and designation lists still return plain arrays; their per-row count queries (N+1) were removed.
- **No duplicate audit rows.** The old hand-written "employee.created/updated" audit lines were removed, because the automatic audit records the same changes field by field.

### Unfinished / next
- **Custom fields.** Only the Employee record type is enabled; other record types need `ENTITIES` entries and a `custom` column. Ordering uses up/down arrows; there is no drag-and-drop.
- **Approvals.** Approving from email or WhatsApp, automatic approval after a time limit, and attachments on requests are not built. Notifications are in-app rows only.
- **Number series per legal entity.** Not built yet, because there are no legal entities yet.
- **Older modules.** Their logic still needs moving into `service.py` files, and the older department and designation lists still need server pagination.
- **Demo data.** The demo company (campusly-demo) now has:
  - a test employee, priya.sharma@example.com / Welcome9#, with a pending change request REQ-2611-0001;
  - a custom section "Statutory details";
  - two inactive test departments from the UI checks.

## Organisation and People

Built by the Organisation and People module: backend `modules/organization` (new) and `modules/employees` (extended), plus departments and designations; web pages under People and Settings.

### What is built
- **Organisation structure.** Legal entities with PAN, TAN, CIN, GSTIN, PF and ESI codes, LIN, state-wise PT, LWF and shops registrations, bank accounts (numbers masked for people without settings access) and an authorised signatory. Also regions; locations (address, state from the State master, timezone, geo point and attendance radius, and a placeholder holiday calendar id for the holidays module); grades or bands with rank and CTC range; cost centres; and teams across departments (lead and members). Departments gained a parent department (sub-departments, with loops refused), a cost centre and a location. Designations gained a code, description and grade. Employment type and employee sub-status come from masters.
- **Tags** with sub-tags (up to 3 levels), which can be put on any record type (`tag_links.entity_type`; employees first). Each tag can be limited to some record types. Filtering by a tag includes its sub-tags. Tags can be assigned in bulk.
- **Skills library** (category from the `skill_category` master). Employee skills have a level from 1 to 5, years and a verified flag. HR, skills managers and the person's manager can verify; when an employee changes their own level, the skill becomes unverified again. People can be found by skill.
- **Full employee record:**
  - personal: middle and preferred name, gender, date of birth, marital status, marriage date, blood group, nationality, father's and spouse's name, photo;
  - contact: personal email, alternate and work phone, extension, current and permanent address with the State master;
  - statutory IDs: PAN checked for format and for an individual's 4th letter; Aadhaar encrypted with Fernet, with a keyed hash for duplicate checks and only the last 4 digits shown (a full view is audited); UAN; ESI; passport with expiry; driving licence;
  - job: legal entity, location, department, designation, grade, cost centre, teams, employment type, stage, joining, probation end, confirmation, notice period, reporting and secondary manager, work mode, timezone;
  - emergency contacts, family members, nominees (PF, EPS, gratuity, insurance; shares up to 100% per kind; a guardian is required for minors);
  - bank accounts (IFSC format and bank prefix check, one primary account, switched off rather than deleted);
  - education;
  - previous employment, including this financial year's income, TDS, PF and PT from the earlier employer for TDS;
  - documents: type from the master, number, issue and expiry dates, file upload up to 5 MB stored in the database, verification status with verifier and remarks (a rejection needs remarks), expiry reminders as in-app notifications, archived rather than deleted;
  - custom fields (existing engine).
- **History in the company** (`employee_job_history`). Every change to a job field is recorded with old and new values. "Record a change" takes a kind (transfer, promotion, designation, department, manager, company, grade, type, other), an effective date and a reason. A change dated in the future is scheduled and applied on its date; scheduled changes can be cancelled. Joining, confirmation, notice, exit and rehire are recorded too. The profile shows a timeline.
- **Lifecycle.** Stages are pre-joining, probation, confirmed, on notice, exited and archived, with allowed transitions only. Putting someone on notice works out the last working day from the notice period. Records are never deleted: `DELETE /employees/{uuid}` records an exit, or archives someone who has left or never joined. Rehire reopens the same record, so history, UAN and documents stay, and `rehire_count` goes up. Duplicate checks cover email, PAN, Aadhaar and UAN (blocking) and phone (warning); when the match is a former employee, the message suggests a rehire.
- **Self service.** `/me/profile/full` is the employee's own profile. Preferred name, personal email, alternate phone, blood group and addresses are edited directly (audited). Employees also add their own emergency contacts, family, education, previous jobs, documents and skills. Name, date of birth, gender, marital status, father's or spouse's name, phone, PAN, Aadhaar, UAN, passport and a new salary account go through the existing `employee.profile_change` approval. Aadhaar is kept encrypted inside the request, and the bank account is added as the primary account when the request is approved.
- **Defaults per company** (`organization.seed.ensure_defaults`, run from `setup_company`): 7 grades, 25 skills, starter tags with sub-tags, and a primary legal entity copied from the company's details.
- **Demo data** (development, `seed_demo_people`, once):
  - 2 legal entities (Karnataka and Maharashtra), 3 regions, 3 locations (Bengaluru, Pune, Chennai) with geo boundaries;
  - 6 cost centres, 7 departments plus 2 sub-departments, 22 designations with grades, 2 cross-department teams;
  - 25 employees with complete profiles, reporting lines, family, nominees, bank, education, previous jobs, job history (promotions, transfers, a scheduled transfer, a resignation), 95 documents (some pending or rejected, a passport expiring soon) and skills.
  - priya.sharma@example.com is one of the 25 and stays a plain employee for self-service tests.

### Pages (web)
- `/dashboard/staff`: directory.
  - Head-count cards, FilterBar (stage, department, location, legal entity, type, tag, team, grade) and search.
  - Saved views (personal or shared), a column chooser (kept in the browser), and list or card view.
  - Bulk actions: tags, change manager, export to Excel or CSV.
  - `?new=1` opens the 5-step add wizard with duplicate warnings; `?view=<uuid>` opens the profile in a large modal.
- Profile (`components/people/profile-view.tsx`):
  - Header card with photo upload, key facts, tags, a stage menu (confirm, extend probation, resignation, withdraw, exit, archive, rehire), Record a change, Edit, and View as employee.
  - Tabs: Overview, Job, Personal, Family & nominees, Documents, Bank & statutory, Education & experience, Skills & tags, History, and an Assets placeholder.
  - Also available as a full page at `/dashboard/staff/{uuid}`.
- `/dashboard/org-chart`: reporting-line tree with zoom (buttons or Ctrl+scroll), drag to pan, search to jump to a person, and expand or collapse. Clicking a person opens their profile. There is also a department view.
- `/dashboard/employee-documents`: every document, filtered by status, expiry window and kind, with a verify modal.
- `/dashboard/profile`: for employees, a "My record" tab (self service) and an "Account" tab.
- Settings and People pages: `/dashboard/entities`, `/dashboard/locations` (regions and locations), `/dashboard/teams`, `/dashboard/tags` (tree), `/dashboard/skills` (who has a skill), `/dashboard/grades`, `/dashboard/cost-centres`. Departments and designations gained the new fields.

### Endpoints
- `/organization/`:
  - `legal-entities`, `regions`, `locations`, `grades`, `cost-centres`, `teams` and `skills`: list and create, then GET or PUT on `/{uuid}`;
  - `tags`: list, create and PUT `/{uuid}`; `tags/assign` for bulk add and remove;
  - `skills/{uuid}/people`, `org-chart`, `summary`.
- `/employees/`:
  - lists: `/` (new filters and sort), `lookup`, `stats`, `duplicates`, `export?format=csv|xlsx`, `saved-views`, `bulk/manager`, `documents` (company-wide);
  - one employee: `{uuid}/profile`, `{uuid}/status`, `{uuid}/archive`, `{uuid}/rehire`, `{uuid}/history`, `{uuid}/job-changes` (plus `/cancel`), `{uuid}/aadhaar` (audited), `{uuid}/tags`;
  - sections: `{uuid}/details/{emergency-contacts|family|nominees|bank-accounts|education|experience}`;
  - documents: `{uuid}/documents` (multipart), with `/{doc}`, `/{doc}/file` (upload or download), `/{doc}/verify` and `/{doc}/archive`;
  - skills: `{uuid}/skills`.
- `/me/`: `profile/full`, `profile/personal`, `profile/photo`, `profile-change` (extended).

### Tables
- New in organisation: `legal_entities`, `regions`, `locations`, `grades`, `cost_centres`, `teams`, `team_members`, `tags`, `tag_links`, `skills`, `employee_skills`.
- New for employees: `employee_emergency_contacts`, `employee_family_members`, `employee_nominees`, `employee_bank_accounts`, `employee_education`, `employee_previous_employment`, `employee_job_history`, `employee_files`, `employee_documents`, `saved_views`.
- Every new table has `company_id`, an index starting with `company_id`, and forced RLS.
- Migration `org1a2b3c4d5` (from `w8x9y0z1a2b3`). It adds about 45 columns to `employees` (said here because it changes the shared employee model), 3 to `departments` and 3 to `designations`. Existing inactive employees become `exited`, and their joining date is set from `created_at`.

### Permissions added
- `people.employees.export`
- `people.sensitive.view` (full PAN, Aadhaar, bank and ID numbers; otherwise they are masked)
- `people.documents.verify`
- `people.teams.manage`, `people.tags.manage`, `people.skills.manage`
- `settings.organization.manage`

HR gets all of these except the organisation settings.

### Tests
`tests/test_organization.py` has 20 tests covering:
- defaults, legal entity checks (formats, masked account round trip), location state and geo checks, the department parent loop;
- tag hierarchy filter, teams, skill verification rules;
- tenant isolation of organisation tables and of another company's ids;
- permissions;
- PAN and Aadhaar formats, masking and audit masking, duplicates;
- job history and scheduled changes, lifecycle (no delete, archive, rehire), notice last day;
- section rules (nominee 100%, IFSC, primary account, previous job TDS);
- self service and the change request;
- document upload, verification and expiry;
- isolation of documents and details;
- export and bulk manager, saved views.

The full suite passes.

### Decisions
- **Lifecycle stage is a fixed column** (`employees.status`) because it drives sign-in and lists. The `employee_status` master is kept as an optional company-defined sub-status (suspended, long leave …). `active` follows the stage.
- **Rehire reopens the same record** rather than creating a linked one, so email uniqueness, history, UAN and documents stay intact. The rehire is a history entry.
- **Scheduled job changes** are applied when the directory or a profile is opened and at every start. There is no separate scheduler, so main.py was left unchanged.
- **Document files** are stored in the database (`employee_files`, not audited row by row; the document row is audited). They should move to object storage when one is set up.
- **Expiry reminders** are in-app notifications sent once per document, inside a savepoint so a notification problem never breaks a page.
- **Blood group master codes** are now explicit (`a_pos`, `a_neg` …). The old seed lost the negative groups because their slug codes collided.
- **The test fixtures** `company_a` and `company_b` now get unique names. The long-lived test database had about 650 "alpha-works-NNN" slugs, which made sign-up collide.
- **Priya Sharma** keeps the `employee` role. Her title is HR Manager, but she stays a plain employee because other builders use her login for self service.

### Unfinished / next
- Bulk import of employees from Excel; HR process builder (custom change processes with revoke); probation reminders and confirmation letters (needs templates); moving between legal entities with service continuity reports.
- Org chart download as image or PDF.
- Field-level visibility beyond masking (by role and team).
- Document files in object storage.
- Phone duplicates are a warning only.

## Attendance, shifts, rosters and timesheets

### Built
- **Shifts** (`modules/shifts`): shift master (start/end, night shifts that cross midnight, paid and unpaid breaks, grace in/out, half-day and full-day minimum hours, flexible shifts with core hours, overtime threshold, shift allowance with minimum hours, weekly offs, colour and code); five Indian defaults (General 09:30–18:30, Morning, Evening, Night with ₹150 allowance, Flexible). Effective-dated **work arrangements** per employee (working days, alternate Saturdays, default shift, "present by default" for people who don't punch, optional rule). Shift **patterns** (weekly, week of the month, N-day cycle). **Women's night-shift consent** register (OSH Code s.43). **Shift change** and **shift swap** requests through the workflow engine (swap: the colleague accepts first, then the manager).
- **Roster** (`modules/roster`, extended): grid API (roster entries over each person's usual week, drafts included for planners), assign by employee/team/department/date range/weekdays with replace or skip, apply pattern (with dry run), clear, publish (notifies employees and re-works attendance). Every change is checked against **rest rules** (`modules/shifts/rest_rules.py`, settings key `rest_rules`): daily and weekly hours (8–9 h / 48 h), minimum rest between shifts, weekly rest day (max 6 days in a row), consecutive nights, and the women's consent rule which always blocks and can't be switched off. Blocked days are skipped and reported; warnings are returned. The old `/roster/` endpoints used by the employee profile still work (entries set there are published straight away).
- **Daily processing** (`modules/attendance/engine.py`, pure functions): status per day (present, absent, half day, on leave, holiday, weekly off, WFH, on duty, missing punch, not in yet), worked minutes (first-in/last-out or in/out pairs, rounding, unpaid breaks deducted), late/early with grace, overtime (threshold, daily cap, early arrival not counted unless the rule says so, pre-approval mode, all hours on an off day), night minutes, night-shift and shift-allowance flags, permission hours counted towards minimum hours, half-day leave/WFH/on-duty, HR override. `apply_penalties` adds late-mark/early-exit penalties per month (default every 3rd late = half-day LOP). `service.recompute()` assigns punches to days (night shifts go to the day the shift started), runs the engine and stores `attendance_days`; it runs after every punch, device sync, approved request, roster publish, arrangement change and on demand. A periodic job makes yesterday's record for everyone (absent when nobody punched).
- **Attendance rules** (`attendance_rules`, versioned with lineage and effective dates; applies to employees/departments/designations/shifts by priority, or a default). Overtime rate must be at least 2× (Code on Wages); quarterly overtime cap 125 h with alert at 80 % (OSH Code), shown in the overtime report.
- **Time settings** (`time_settings`, versioned by key): rest_rules, clock_in (web on/off, geo-fence required, selfie required, allowed IPs, selfie retention), regularisation (monthly limit, window), wfh (weekly/monthly limits, blocked weekdays), permission (monthly count/minutes, per-request max, within shift), on_duty, overtime_request (consent wording), pay_period, timesheets.
- **Clock-in**: web clock in/out with optional geo-fence (own `attendance_geofences` plus the organisation module's locations with a map point) and selfie (camera in the browser; deleted after the retention period by a periodic job).
- **Devices**: register any vendor's biometric/RFID/face device; (a) REST push `POST /attendance/devices/push` with a per-device key (`X-Device-Key`, stored hashed, shown once, can be regenerated), (b) CSV/Excel upload with a column-mapping preview, (c) FTP/FTPS/SFTP pull configuration with a callable poller (`devices.poll_due_devices`, registered as a periodic job; SFTP needs `paramiko` installed). Enrolments (device user id or card → employee, per device or for all devices), duplicate punches ignored, sync log per batch/file.
- **Requests** via the workflow engine (`attendance.regularisation`, `attendance.on_duty`, `attendance.wfh`, `attendance.permission`, `attendance.overtime`; default flow: reporting manager): limits checked from settings; categories from masters `wfh_category`, `on_duty_type`, `regularisation_reason` (appended to `core/master_seed.py`); overtime pre-approval records the worker's consent (audited).
- **Locks**: lock a pay period (brings days up to date first, then freezes them); later changes to locked days become `attendance_adjustments` paid in the next open period. Unlock needs a note. Both audited.
- **Payroll export**: `attendance.service.payroll_summary(db, company_id, start, end)` → per employee payable days, LOP days (with late-mark penalties), present/half/absent/leave/holiday/weekly-off counts, OT hours, night shifts, shift-allowance days and amount, and adjustments from earlier locked periods. Also at `GET /attendance/reports/payroll` (JSON or CSV).
- **Reports**: muster roll (register style, CSV), late/early, overtime with quarter-to-date against the cap, payroll summary.
- **Timesheets** (`modules/timesheets`): clients, projects (owner, members, billable, budget hours, dates, status), tasks; time logs by hours or start/end; weekly grid; submit through the workflow (`timesheets.timesheet`) with **line-level approval** (partly approved weeks; rejected lines can be fixed and sent again); reminders (manual and a weekly periodic job); project hours report.
- **Leave integration**: `modules/shifts/adapters.py` uses the leave module's `WorkCalendar` (working/half/weekend/holiday days) and `leave_on_dates` (approved leave, paid or LOP, half days), falling back to Monday–Friday with no holidays when the leave module is absent. Precedence for a day: published roster → work arrangement → leave work calendar → profile timing → default shift's weekly offs.

### Pages (web)
`/dashboard/attendance` (my attendance: today card with clock in/out, month calendar with status colours and a day modal, request buttons, my requests), `/dashboard/attendance/team` (today board, monthly muster grid), `/dashboard/attendance/admin` (daily records with filters, day modal with hand status/punch add/ignore, recompute, lock period, locks, adjustments), `/dashboard/attendance/reports`, `/dashboard/attendance/settings` (Shifts, Attendance rules, Work from home incl. categories, Permission hours, Requests, Rest rules, Clock-in and office locations, Work arrangements, Pay period), `/dashboard/roster` (visual grid with drag and drop, assign, patterns, publish, consent; employee shift change and swap), `/dashboard/devices` (devices, API key, FTP config, enrolments, sync log, CSV/Excel import wizard), `/dashboard/timesheets` (my week, approvals, team, projects, clients). Shared helpers in `src/components/attendance/`.

### Endpoints
`/attendance/me/*` (access, today, clock, month, days, requests, request-options, schedule), `/attendance/team/*`, `/attendance/employees/{uuid}/month`, `/attendance/requests`, `/attendance/records*`, `/attendance/punches/*`, `/attendance/recompute`, `/attendance/locks*`, `/attendance/adjustments`, `/attendance/reports/*`, `/attendance/rules*`, `/attendance/settings*`, `/attendance/geofences*`, `/attendance/devices*` (incl. `/devices/push`), `/attendance/imports*`, `/attendance/enrolments*`, `/attendance/sync-logs`; `/shifts/*` (shifts, patterns, arrangements, consents, requests/change, requests/swap, colleagues); `/roster/grid|assign|check|apply-pattern|clear|publish`; `/timesheets/*`.

### Tables
shifts, shift_patterns, work_arrangements, night_shift_consents, attendance_rules, time_settings, attendance_geofences, attendance_devices, device_enrolments, device_sync_logs, attendance_punches, attendance_days, attendance_requests, attendance_locks, attendance_adjustments, ts_clients, ts_projects, ts_tasks, ts_timesheets, ts_time_logs; roster_entries gained shift_id, published, published_at, source, pattern_id, updated_at. All with company_id, index and forced RLS (migration `a7t1m2e3s4h5`, from `w8x9y0z1a2b3`).

### Permissions
New: `time.attendance.settings` (shifts, rules, devices, settings) and `time.timesheets.manage` (clients/projects, everyone's timesheets); given to HR by default. Managers see their direct and indirect reports; employees only themselves.

### Tests
`tests/test_attendance_engine.py` (18: normal day, grace, late, early, half day, missing punch, overtime threshold/cap/pre-approval, night shift across midnight, holiday/weekly off/leave/WFH/on duty, permission hours, no-punch staff, rounding and pairs, today's running hours, HR override, penalties, rest rules incl. women's consent) and `tests/test_attendance.py` (15: device push auth and dedupe, regularisation fixing a missing punch through approval, monthly limit, late-mark penalty, WFH/permission/overtime-consent limits, roster rest-rule blocks, draft vs published roster, women's night-shift consent, lock with adjustments carried to the next period, payroll summary and overtime report, employee/manager access, timesheet line-level approval, leave from the leave module, tenant isolation incl. RLS on a direct session).

### Decisions
- Attendance dates are in the company timezone; punches are UTC. A punch belongs to the day whose shift window holds it (nearest shift wins), so night-shift check-outs count for the day the shift began.
- Full-day minimum for the default General shift is 7.5 h of an 8 h shift so that a short late arrival is a late mark (with the late-mark penalty), not a half day.
- Roster drafts don't affect attendance until published; employees see only published entries.
- When a rest rule blocks, only the offending days are skipped; the rest of the assignment is saved and the problems are reported.
- Locked days are never rewritten; differences become adjustments dated to the first open day after the lock.
- Defaults per company come from `modules/attendance/seed.py: setup_company` (called through the shared `_module_setup` hook in `seed/seed_data.py`); `seed_demo` fills the demo company (rotating support-desk roster with a published and a draft week, two weeks of punches, arrangements incl. alternate Saturdays and a present-by-default person, two devices with enrolments, requests in several states, clients/projects and timesheets).

### Unfinished / next
- Mobile app clock-in, kiosk mode, face verification and offline sync are not built (web only).
- Auto shift assignment from check-in time, comp-off crediting (the leave module owns comp-off) and timesheet billing/invoices are not built.
- SFTP pulling needs the `paramiko` package, which is not in requirements.txt yet.
- Statutory state-format register layouts beyond the muster roll CSV are not built.

## Performance, pay reviews and learning

### Built
- **Goals & OKRs** (`modules/performance/okr.py`): goal periods (quarter, half, year, custom); objectives at company, department, team and individual level with alignment to a parent; key results of four kinds (number, %, milestones, yes/no) with start/target, weight, owner and check-in frequency. Progress rolls up from key results and aligned children by weight. Status is set automatically from progress against time elapsed (statuses and grace days are configurable) or by hand. Check-ins record value, confidence and a note; planned check-in dates are flagged on time, late or missed.
- **Libraries** (`library.py`): rating scales, KRAs (tagged by department, designation and location, with auto-assign), competencies with behaviours per level and linked skills, review templates (sections for goals, KRAs, competencies and questions, with weights).
- **Review cycles** (`reviews.py`): annual, half-yearly, quarterly or probation; participants by rule (departments, designations, joined before, include/exclude); stages self, peer, manager and skip-level with deadlines; peers nominated by the employee or manager, with optional approval; weightage across stages gives the final score and proposed rating. Reminders are rate-limited (at least 1 h apart, at most 2 a day); late reviews are escalated to the reviewer's manager after N days. Calibration against a target distribution (largest remainder), with a reason required for a change and manager acknowledgement; disagreements block publishing. Publish writes the final rating and a numbered review letter; the employee can respond. Nine-box from rating and potential (labels configurable).
- **1:1s, feedback and improvement plans** (`talent.py`): recurring 1:1s with shared agenda, notes and action items carried over, plus private manager notes; feedback (given or requested; private or shared with the manager; praise can also be posted as kudos through the engagement module's `kudos.give`, in a savepoint so a refused kudos never loses the feedback); PIPs with goals, check-ins, acknowledgement and an outcome.
- **Pay reviews and promotions** (`increments.py`): a pay review linked to a review cycle; guideline matrix (rating × compa-ratio band), pay ranges per designation, budgets per department as % or amount. Opening it creates a proposal for everyone, pre-filled from the guidelines. Managers adjust their team's proposals and send batches for approval through the workflow engine (`performance.increment`). Release writes numbered revision letters and effective-dated job changes. Promotions go into the employee job history through the employees service; pay changes stay in `perf_job_changes` and are applied through `payroll.apply_salary_revision` once a payroll module exists (`POST /performance/increments/apply-due`).
- **Learning** (`modules/learning`): courses with lessons (sequential locking), learning paths, enrolment rules, mandatory courses with due dates and re-certification, classroom batches with seats, waitlist and attendance, a question bank, assessments (random picks, timer, attempts, negative marking, pass mark), certificates, course requests through the workflow engine, and a summary report.
- **Demo data** (`modules/performance/seed.py`, `modules/learning/seed.py`): 26 objectives in an OKR tree, last year's published review, an active mid-year review about a third done, 1:1s, feedback, an improvement plan, an open pay review; 6 courses (including POSH awareness with a quiz) and 89 enrolments.

### Pages (web)
`/dashboard/goals` (my, team and company goals with the alignment tree; detail, add/edit and check-in modals), `/dashboard/performance` (Reviews: to do, my results, my team, cycles with the cycle wizard and dashboard), `/dashboard/performance/calibration` (distribution against target, adjust a rating with a reason, start calibration, publish), `/dashboard/performance/nine-box` (3×3 grid by potential and performance, set potential, people not yet placed), `/dashboard/performance/one-on-ones`, `/dashboard/performance/feedback`, `/dashboard/performance/pip`, `/dashboard/performance/settings` (goals, scales, KRAs, competencies, templates, review rules, nine-box names, letters), `/dashboard/promotions` (pay review setup with guideline matrix, pay ranges and letter; budgets; proposals grid with bulk send for approval; release letters; an employee's own revision letters), `/dashboard/learning`, `/dashboard/learning/my`, `/dashboard/learning/admin`. Shared pieces in `src/components/performance/` and `src/components/learning/`.

### Endpoints
`/performance/*`: me, settings, rating-scales, kras, competencies, templates, periods, objectives (incl. tree, summary, checkins), cycles (launch, status, remind, escalate, publish, participants, calibration, nine-box), participants (calibration, acknowledge, potential, letter, respond, peers), reviews (mine, results, team, acknowledgements, peer-approvals), forms, one-on-ones, feedback, pips, increment-cycles (open, budgets, submit, release), increment-proposals (letter), increments/mine, increments/apply-due, employees/{id}/pay-history. `/learning/*`: settings, me, catalogue, courses, my, enrolments, paths, rules, batches, questions, assessments, attempts, requests, reports/summary.

### Tables
Migration `pf1a2b3c4d5e` creates 34 tables, all with company_id, an index and forced RLS: perf_settings, perf_periods, perf_objectives, perf_key_results, perf_checkins, perf_rating_scales, perf_kras, perf_employee_kras, perf_competencies, perf_templates, perf_cycles, perf_participants, perf_review_forms, perf_one_on_ones, perf_one_on_one_notes, perf_feedback, perf_pips, perf_pip_checkins, perf_increment_cycles, perf_increment_budgets, perf_increment_proposals, perf_job_changes; learning_settings, learning_courses, learning_lessons, learning_paths, learning_rules, learning_enrolments, learning_batches, learning_batch_seats, learning_questions, learning_assessments, learning_attempts, learning_course_requests.

### Permissions
New: `talent.performance.admin` (cycles, calibration, settings, libraries), `talent.increments.manage` (pay reviews), `talent.learning.manage` (courses, batches, assessments, rules). Managers work on their direct and indirect reports; employees on their own records. Master list `course_category` added to `core/master_seed.py`.

### Tests
`tests/test_performance.py` (18: KR progress for all kinds, roll-up, auto status, check-in flags, form and cycle scoring, calibration distribution, nine-box and guidelines, goals through the API, a review cycle end to end, reminder rate limits, weightage validation, employee access, 1:1 private notes, feedback visibility and PIP, praise posted as kudos, pay review approval, tenant isolation) and `tests/test_learning.py` (12).

### Decisions
- Calibration changes keep the manager's proposed rating; the calibrated one wins at publish. A changed rating needs the manager's acknowledgement when the cycle asks for it, and a disagreement blocks publishing until it is settled.
- Nine-box boxes are numbered (potential − 1) × 3 + performance level, so box 9 is high/high.
- Until a payroll module exists, salary changes are kept as scheduled job changes and applied when one appears; designations are changed through the employees service on the effective date.

### Unfinished / next
- Pages have been type-checked; calibration, nine-box, promotions and goals/settings were opened in a browser. The 1:1s, feedback, PIP and learning pages have not had a browser check by this module's owner yet.
- The seeded mid-year cycle has no potentials set, so the nine-box starts empty for it.
- No payroll integration for increments yet (see Decisions).

## Leave, holidays and work calendar

Backend `modules/leave` (migration `lv1a2b3c4d5e`, down revision `w8x9y0z1a2b3`), web `src/app/dashboard/leave/*`, `src/app/dashboard/holidays`, `src/components/leave/*`, tests `tests/test_leave.py` (29 tests).

**Built**
- **Leave types, versioned.** `leave_types` (name, code, colour, icon, kind: standard / comp_off / lop / optional_holiday) and `leave_policy_versions` (rules JSON with `effective_from` / `effective_to`). Saving changed rules creates a new version from a chosen date; `rules_on(type, day)` gives the version in force, so accruals, requests and year end for old periods use old rules. Rules cover: paid / unpaid / LOP, statutory tag and legal basis, units (day, half, quarter, hour), applicability (gender, marital status, employment type, location, state, department, designation, grade, tags, service length, probation, named people in/out, or a rule expression), entitlement (fixed, accrual yearly/half-yearly/quarterly/monthly at start or end, days worked e.g. 1 per 20 after 180 days, grant on approval with options), proration on joining/exit (months with cut-off day or days, rounding to half/quarter/whole, up/down/nearest), probation (full/reduced/none, waiting period), experience levels, max balance, year end (leave year start month, reset, carry forward max/%/expiry/accumulation cap, encashment max/%/keep-at-least/on exit, rest lapses), restrictions (min/max per request, max in a row, notice above N days, past/future limits, count weekends/holidays, sandwich on weekends/holidays with threshold and "next to other leave", clubbing blocks, document above N days, negative balance: none / down to N / up to year-end accrual / excess as LOP, max requests per month/quarter/year/service, gap between requests, only on optional holidays, HR-only). Legal floors are enforced (maternity 26 / 12 weeks, adoption and commissioning 12 weeks, paid; annual leave not less than 1 per 20 days worked after at most 180 days).
- **Indian defaults per company** (`modules/leave/seed.py` → `defaults.py`, editable): EL (OSH Code days-worked, carry 30, encash 15, sandwich), CL (monthly, max 3, not next to EL, excess LOP), SL (12, document above 2 days, may go 2 below zero), Maternity (grant: 182 / 84 / adoption 84 / commissioning 84 / miscarriage 42, calendar days, women only), Paternity, Bereavement, Marriage, Comp off, Loss of pay, Optional holiday (2 a year, only on optional holidays). Work weeks: Monday–Friday (default), 2nd and 4th Saturday off, Monday–Saturday. Holiday calendars: All India (default; national, festivals, optional for 2026–27) and KA, MH, TN, TS, KL, WB, DL, GJ with state holidays (festival dates should be checked against the state notification).
- **Ledger** `leave_ledger` (opening, accrual, availed, restored, adjustment, encashed, lapsed, carried_out, carry_forward, expired, grant, comp_off) with idempotency keys. Accrual run (`ledger.run_accruals`) is idempotent and runs daily through `core.periodic` (`leave.daily`) and at start-up; expiry run uses oldest-expiring-first; manual adjustments with reason (audited); opening balance CSV import with check mode; year end with preview (`ledger.year_end(apply=False)`), carry forward into the next year, encashment entries marked `payroll_status: pending`, lapse.
- **Requests** `leave_requests` with a day-by-day breakdown: apply (full/half/quarter/hours), live preview (days counted, weekends/holidays, sandwich days, balance after, LOP split, clashes, rule errors, colleagues away), attachment (data URL, 5 MB), change while pending, withdraw, cancel approved leave fully or some days (sandwich days re-checked), extension (linked child request), HR records leave for someone (approved at once). Workflow request types in `hooks.py`: `leave.request` (default flow: reporting manager, then HR when `request.days > 3`), `leave.cancellation`, `leave.comp_off`. Approval writes `availed` (grants write `grant` + `availed`); cancellation writes `restored`.
- **Comp off** for work on a weekly off or holiday (request with approval, or `service.credit_comp_off_from_attendance(...)` for the attendance module), expiry from settings (default 90 days).
- **Work calendar service** for other modules: `modules.leave.calendar.working_days(db, employee, from, to)`, `is_working_day(db, employee, day)`, `day_map(...)`; calendar resolution: location's `holiday_calendar_id`, else matching calendar, else default. Holiday add/edit/remove re-works leave already applied on those dates.
- **Reports / payroll**: balances, leave taken by type and department, LOP summary per pay period (`reports.lop_summary`, `reports.lop_days(db, cid, emp_id, from, to)`), absentees on a day, encashments (`reports.encashments`); CSV export.
- **Permissions** added from `hooks.py` (no edit to roles): `time.leave.manage` (HR admin, HR), `settings.leave.manage`.

**Pages**: My leave `/dashboard/leave` (balance cards, apply modal, my requests with status timeline, comp off, holidays), Team leave `/dashboard/leave/team` (month calendar, pending decisions), Leave admin `/dashboard/leave/admin` (requests, balances with adjust/history/record leave, accrual/expiry/year-end with preview and run history, import, comp off), Leave reports `/dashboard/leave/reports`, Holidays `/dashboard/holidays` (my holidays and optional picks; HR: calendars, add holiday modal, import), Settings: Leave types `/dashboard/leave/types` (6-step editor, version history, company leave settings), Work week `/dashboard/leave/work-week`.

**Endpoints**: `/leave/access`, `/leave/me/summary`, `/leave/me/holidays`, `/leave/requests` (+ `/preview`, `/{uuid}`, `/{uuid}/cancel`, `/{uuid}/extend`, `/{uuid}/attachment`), `/leave/comp-offs`, `/leave/team/calendar`, `/leave/calendar/days`, `/leave/balances` (+ `/{emp}/ledger`, `/adjust`, `/import`, `/import/template`), `/leave/runs` (+ `/accrual`, `/expiry`, `/year-end`), `/leave/settings`, `/leave/options`, `/leave/options/{kind}`, `/leave/types` (+ `/{uuid}`, `/{uuid}/active`, `/validate-rule`), `/leave/defaults`, `/leave/holiday-calendars` (+ `/{uuid}/import`), `/leave/holidays`, `/leave/work-weeks`, `/leave/reports/{balances,taken,lop,absentees,encashments}`.

**Tables**: leave_settings, leave_types, leave_policy_versions, leave_ledger, leave_requests, comp_off_requests, holiday_calendars, holidays, work_week_patterns, leave_runs (all with company_id index and forced RLS).

**Decisions**
- Employee facts (joining date, gender, location, grade, probation, exit date) are read from the employee record when the column exists, else a custom field of the same name; without a joining date the record's creation date is used. Tags come from `tag_links`.
- Days worked for EL use the attendance module's `days_worked(db, employee, start, end)` if it exists; otherwise working days from the work calendar minus unpaid leave.
- Leave years are labelled by the calendar year in which they start; comp off does not reset (expiry only). A negative year-end balance moves to the next year.
- HR recording leave for someone skips approval and the soft limits (notice, past dates) but not clashes or balance.
- Per-employee work arrangement (employee_settings timing) is not used for working days; work-week patterns (with applicability) are.

**Not done yet**: leave pay rules (half-pay sick leave), Bradford score, calendar sync, leave expiry reminder emails, absence-to-leave conversion and late-mark deductions (need attendance hooks), LOP cap per pay period and payroll lock, leave administrators per group, substitute day off.

## Engagement: Buzz, celebrations, kudos, announcements, surveys and kiosks

### Built
- **Buzz** (`modules/engagement/feed.py`): posts with @mentions, #hashtags, pictures, files, link previews and polls; emoji reactions with who reacted; threaded comments with reactions; edit, soft delete (audited), pinning, reporting with auto-hide after N reports and a moderation queue; optional review before publishing through the workflow engine; audience targeting by department, location, team, tag, employment type, designation, role or named people with any/all matching. Cursor pages for infinite scroll; `/feed/updates` and `/feed/stats` for 30-second polling.
- **Celebrations** (`celebrations.py`, `jobs.py`): birthdays, work and wedding anniversaries and new joiners. Each person chooses what is shown (birth year never shown). A morning job at the company's local hour posts one card per person, kind and day (idempotent), and sends editable, switchable email templates.
- **Kudos** (`kudos.py`): configurable values/badges with points, a monthly giving limit, Buzz cards, profile summary, month and quarter leaderboards (Indian financial-year quarters).
- **Announcements** (`announcements.py`): sanitised rich text (pictures only from our own signed file links), cover designs or uploads, targeting, scheduled publish and expiry, pinned on Buzz, optional email, read and acknowledgement tracking with reminders.
- **Surveys** (`surveys.py`): single/multiple choice, star rating, agreement scale, text and eNPS; anonymous, optional-name or named; anonymous answers are never audited and keep only the day; anonymous results stay hidden below 5 responses; weekly/monthly/quarterly recurrence; required surveys with reminders; results by question and department; eNPS score and trend.
- **Kiosks** (`kiosk.py`): secret-link TV screens with no sign-in (`/api/v1/public/kiosk/{token}`).
- **Notifications**: the notifications module has unread state, an inbox, unread count and mark-read.
- Background jobs start from the engagement router's lifespan, so `main.py` is untouched. Uploaded files are stored in the database and served through signed links.

### Pages (web)
`/dashboard/buzz` (feed with hero, composer, filters, pinned posts, celebration cards, kudos, announcement and survey cards, polls, comments, moderation; celebrations, waiting-for-you, leaderboard, trending and new-joiner panels; a today strip on phones), `/dashboard/kudos` (wall of thanks filtered by value or person, leaderboard with podium, my kudos), `/dashboard/announcements`, `/dashboard/surveys`, `/dashboard/kiosk`, `/kiosk/[token]` (full-screen display outside the app shell), `/dashboard/engage-settings`. The Overview page shows today's celebrations, the latest announcement, a Buzz preview and what is waiting for the person. The employee profile shows kudos (totals, badges, recent); My profile → Account has "My celebrations" privacy. The header bell is the notification centre. Shared pieces in `src/components/engagement/`.

### Endpoints
`/engagement/*`: meta, settings (per section, email preview), jobs/run, audience (groups, options, preview), people, files, link-preview, feed (updates, stats, trending), posts (react, reactions, comments, vote, voters, report, wish, pin, hide, dismiss-reports), moderation, celebrations, me/celebrations, kudos (values, reorder, leaderboard, profile), announcements (publish, archive, acknowledge, receipts), surveys (mine, enps, meta, publish, close, extend, copy, remind, take, responses, results, participation), kiosks (new-link), home. Public: `/public/engagement/files/{uuid}`, `/public/kiosk/{token}`.

### Tables
Migration `en1g2a3g4e5m` (after `w8x9y0z1a2b3`) creates 22 tables, all with company_id, an index and forced RLS buzz_posts, buzz_comments, buzz_reactions, buzz_poll_options, buzz_poll_votes, buzz_reports, engagement_files, engagement_profiles, engagement_settings, celebration_events, kudos_values, kudos, kudos_recipients, announcements, announcement_receipts, surveys, survey_questions, survey_runs, survey_participations, survey_responses, survey_answers, kiosks.

### Permissions
`engage.announcements.manage` is in the role catalogue. The code also checks `engage.surveys.manage`, `engage.settings.manage` (settings, kiosks, kudos values) and `engage.buzz.moderate`; these are not in the catalogue yet, so only owners and admins have them until they are added.

### Tests
`tests/test_engagement.py` (17: audience targeting, the 5-response anonymity threshold, kudos limits, the celebration job run twice, company isolation, and more). Tenant isolation, workflow and audit tests pass.

### Decisions
- Pre-joining, exited and archived people are left out of audiences and celebrations.
- Celebration privacy defaults come from settings; a person's own choice wins.
- Files live in the database for now behind signed links; move to object storage later.

### Unfinished / next
- The demo company has three test "Asha Menon" records with today's joining date, so Buzz shows three welcome posts.
- Files should move to object storage.
- No push notifications or mobile app screens for Buzz yet.

## Workplace services: help desk, grievances and POSH, files and policies, letters and e-sign, tasks and assets

### Built
- **Help desk** (`modules/helpdesk`): categories and subcategories with owners, escalation contacts and agents per location (round-robin or manual assignment); response and resolution targets per category and priority, paused while waiting on the employee, with breach flags, escalation and a priority bump (periodic job); status flow with allowed moves, pick up, reassign, internal notes, saved replies and attachments; employees withdraw, close, reopen with a reason and rate (3 or 5 points); HR can raise a case for an employee; knowledge base with suggestions while typing and "was this helpful"; reports (volume by category, missed targets).
- **Grievances and POSH** (`modules/grievances`): named or anonymous grievances with a one-time tracking code (public tracking and replies at `/public/grievances`); committees per category; POSH complaints visible only to the Internal Committee, with the legal timeline (filing within 3 months, inquiry within 90 days, report within 10 days, action within 60 days), late filing recorded as "late" rather than an open deadline, committee-only notes, and the annual report (cases, pending over 90 days counted up to today in the current year, workshops, committee).
- **Files and policies** (`modules/documents`): company, department and HR folders with audiences, versions, expiry reminders and a private locker per employee (HR sees only usage); policies with versions, read-and-acknowledge, reminders and a CSV report. Files are stored in the database (`stored_files`) and served through signed links (`storage.py`).
- **Letters and e-sign**: letter templates with merge fields (filters for dates, ₹ amounts, amounts in words, capitals), extra fields, page settings, header and footer designs (letterheads) with `{{page.number}}`/`{{page.total}}`; PDF generation with WeasyPrint (`pdf.py`; the Dockerfile installs Pango and fonts); bulk generation (up to 200); employee letter requests through approval; signing with draw or typed signature and an emailed 6-digit code, signing order, expiry, reminders, recall and decline; signed copy with an audit page. Fourteen default templates (offer, appointment, confirmation, increment, relieving, experience, salary certificate …). Aadhaar e-sign is a provider stub (`esign_providers.py`).
- **Tasks** (`modules/tasks`): tasks with assignee, priority, due date, checklist, comments, attachments, tags, repeats (daily to yearly) and due-soon/overdue reminders (periodic job); source badge when created by another module.
- **Assets** (`modules/assets`): register with tag numbering, category, serial, purchase, warranty and location; hand-over with employee acknowledgement, return with condition, automatic next status and recovery amount for damage or loss (pending/recovered/waived, read by final settlement); maintenance log; employee asset requests through approval; summary report (by category, book value, warranty ending, late returns).
- **Shared**: `core/periodic.py` (modules register background jobs; run from `main.py`'s escalation loop) and the per-module `setup_company`/`seed_demo` hooks in `seed/seed_data.py`. New master lists: case type, grievance category, letter request reason, policy category, course category.

### Pages (web)
`/dashboard/helpdesk`, `/dashboard/grievances`, `/dashboard/documents` (company files, policies, my documents, my locker, settings), `/dashboard/documents/letters`, `/dashboard/templates`, `/dashboard/tasks` (list and board), `/dashboard/assets` (register, requests, recoveries, summary), `/dashboard/assets/mine`. Shared helpers in `src/components/workplace/`.

### Endpoints
`/helpdesk/*`, `/grievances/*` and `/public/grievances/*`, `/documents/*`, `/letters/*` and `/public/esign/*`, `/tasks/*`, `/assets/*`.

### Tables
doc_folders, document_settings, stored_files, doc_files, doc_file_versions, policies, policy_versions, policy_acknowledgements, letterheads, letter_templates, letters, esign_requests, esign_signers, helpdesk_settings, helpdesk_categories, helpdesk_agents, helpdesk_slas, helpdesk_cases, helpdesk_messages, helpdesk_canned_responses, helpdesk_articles, grievance_settings, grievance_committees, grievance_committee_members, grievances, grievance_messages, posh_programmes, tasks, task_checklist_items, task_comments, assets, asset_assignments, asset_maintenance, asset_requests. All with company_id, index and forced RLS (migration `wp1a2b3c4d5e`, from `w8x9y0z1a2b3`).

### Permissions
New: `workplace.helpdesk.manage`, `workplace.grievances.manage` (not POSH; POSH access comes only from Internal Committee membership), `workplace.files.manage`, `workplace.letters.manage`, `workplace.tasks.manage`, `workplace.assets.manage`; given to HR by default.

### Tests
`tests/test_workplace_helpdesk.py` (6: target due times and pause, breach escalation, status flow, rating and reopening, open counts for subcategories), `test_workplace_grievances.py` (6: Internal Committee make-up, POSH visible only to the committee, POSH deadlines, POSH flow and annual report, anonymous follow-up with the code, POSH can't be anonymous), `test_workplace_documents.py` (7: merge fields, a template and designs for every letter type, letter PDF filed, self-service letter request, e-sign with email code and signing order, policy acknowledgement, folders and private locker), `test_workplace_tasks_assets.py` (5: repeat dates, task flow and permissions, tasks from other modules, asset hand-over to recovery, asset request through approval), `test_workplace_isolation.py` (2: tenant isolation through the API and RLS). 26 passed.

### Decisions
- POSH cases are never shown to HR admins by role; only committee members see them, and the complainant through their own view or tracking code.
- An anonymous grievance's tracking code is shown once; only its hash is kept.
- Letter merge leaves page placeholders for the PDF engine, including in letterhead previews.
- A subcategory's open count is its own cases; a main category's count includes its subcategories' cases.

### Unfinished / next
- Aadhaar e-sign provider is a stub; email-code signing is the working method.
- Files should move to object storage (shared with Engagement).
- Not checked in the browser: help desk rating widget and settings save buttons.

## Payroll, statutory compliance, income tax, expenses, loans and final settlement

### Built
- **Statutory library** (`modules/payroll/statutory_data.py` seeds the platform table `statutory_rules`; `statutory.py` resolves by date): EPF (12%/12%, EPS 8.33% on the ₹15,000 ceiling capped at ₹1,250, EDLI 0.5%, admin 0.5% with the ₹500 minimum, restricted/unrestricted, VPF, no EPS at 58+), ESI (0.75%/3.25%, ₹21,000 or ₹25,000 for disability, April–September / October–March coverage, ₹176/day exemption, rounding up), Professional Tax for 21 states/UTs (monthly, half-yearly, annual; gender slabs; February amounts; ₹2,500 cap), LWF for 16 states, minimum wages (sample tables for 10 states, national floor wage, CSV/Excel import per company), income tax new and old regimes for FY 2025-26 and 2026-27 (Income-tax Act 2025), standard deduction, rebate with marginal relief, surcharge with marginal relief, cess, no-PAN rate, deduction sections and caps (with 2025 Act references), perquisite values, gratuity (15/26, 5 years, 1 for fixed-term from 21 Nov 2025, 4 years 240 days, ₹20 lakh), bonus (8.33–20%, ₹21,000, ₹7,000 or minimum wage), retrenchment and re-skilling fund, the 50% wage rule, due dates and form names. Old versions stay for old periods. Companies add their own versions (`statutory_overrides`) where the law allows; overrides below the legal floor are refused. Platform admins edit the library at `/platform/statutory/rules`.
- **Pure calculation** (`calc.py`, `taxcalc.py`, `engine.py`): every statutory amount, proration (calendar, 30, 26 or working days) for LOP, joiners, leavers and mid-month revisions, the Code on Wages wages with the 50% add-back (fed into PF wages), OT by formula, monthly TDS (projection over the months left, one-time pay taxed in its month, previous employer, opening balances, perquisites, declarations as declared until verified), HRA exemption, gratuity, bonus, retrenchment, EMI schedules, loan perquisite, working-day deadlines, Indian amount in words.
- **Set-up**: salary components (earning/deduction/employer/reimbursement; fixed, formula, balancing, typed in, system; taxability; wage class; PF/ESI wage; prorate; rounding; in CTC; flexible benefit with yearly limit; Hindi label; GL code), versioned salary structures by grade with salary range, CTC split solved iteratively so employer PF/ESI/gratuity inside CTC and the balancing allowance add up exactly (negative balance refused), range and 50% warnings, pay groups (cut-off, pay day, proration basis) with generated pay periods, employee payroll profile (pay group, payment mode, PF/EPS/VPF, ESI/disability, PT/LWF state, metro, minimum-wage state/zone/skill, fixed-term), effective-dated salary assignment and revision with reasons; back-dated revisions create arrears.
- **Pay runs** (`runs.py`): open period → inputs (automatic from attendance payable/LOP/OT/shift allowance, else leave LOP; loan instalments; approved expense claims; year-end leave encashment; manual and Excel inputs; holds; leave out) → pre-checks (pending leave/attendance, attendance not locked, missing bank/PAN/UAN, joiners, leavers, holds) with in-app and email reminders → background calculation with progress → review (register, variance by person and component, findings: below minimum wage and negative pay block until acknowledged with a reason, >50% deductions, big variance) and the **payroll check assistant** (`assistant.py`: sudden changes with the parts that moved, duplicate bank accounts, zero PF, zero ESI, TDS jumps, no-PAN rate, pay without Basic, zero net, one-time spikes) → approval through the workflow engine (`payroll.run`) → lock (loans, expenses, encashments and arrears marked; challan rows filled) → reopen (permission + reason, audited) → pay. Off-cycle runs: bonus, arrears, settlement, correction. Hold/release per payslip.
- **Files** (`exports.py`): bank transfer files for HDFC, ICICI, SBI, Axis, Kotak and standard NEFT/RTGS (layouts editable through settings), PF ECR 2.0 (`#~#`) and arrears ECR, ESI monthly file (Excel/CSV), PT return by state and slab, LWF return, journal (CSV by GL code and cost centre, Tally XML), register of wages, bonus register, gratuity provision, quarterly TDS return text file in the NSDL record layout (FH/BH/CD/DD/SD) plus Excel.
- **Payslips** (`pdf.py`): WeasyPrint HTML with logo, components, YTD, employer contributions, leave balance and tax summary, English with Hindi labels, amount in words, encrypted with pypdf (PAN, PAN+DOB or none); published to self-service and emailed as an attachment with the password hint.
- **Income tax** (`taxsvc.py`): regime per employee per year with side-by-side comparison, declarations (80C items, 80CCD(1B), 80D self/parents with senior flag, 80E, 80G, 80TTA, 24(b), LTA, HRA with landlord PAN above ₹1 lakh, previous employer, other income, extra TDS, flexible benefit choice), declaration and proof windows, proof uploads, HR verification (accept, reject, part with comments), perquisites (car, accommodation, other; concessional loans automatic), opening balances import, month-by-month worksheet, Form 16 Part B PDF (single and bulk zip) merged with uploaded Part A matched by PAN.
- **Compliance calendar** (`compliance.py`): PF, ESI, TDS, PT and LWF by state, salary payment, quarterly return and certificates per entity and month with challan status/reference; reminders 3 days before and when late (periodic job).
- **Expenses** (`modules/expenses`): categories with limits by grade (and location), daily/monthly limits, receipt rule, mileage rates, GST details, taxable or not, paid with salary or by transfer, flexible-benefit link; claims with several bills, photo/PDF receipts, policy checks (limits, missing receipt, duplicates, GSTIN format), approval (`expense.claim`), part approval per line, payment through payroll or direct; travel advances (`expense.advance`), settlement against claims, recovery.
- **Loans** (`modules/loans`): types with limits (amount or × gross), instalments, interest (reducing/flat), service and expression-based eligibility; requests (`loan.request`), disbursal, EMI schedule, deduction in payroll, skip and prepay with approval (`loan.change`), perquisite on concessional loans, balance recovered in final settlement.
- **Final settlement** (`settlement.py`): salary to the last day (with PF/ESI/PT), leave encashment (tax-exempt limit), gratuity, statutory bonus, notice pay or recovery, retrenchment compensation (15 days a year) and re-skilling fund (15 days), pending reimbursements, unsettled advances, loans, asset recoveries (assets module), HR adjustments, TDS on all of it; due date 2 working days after the last day on the employee's work calendar, countdown and alerts (drafts created automatically when an exit date arrives); approval (`payroll.settlement`), payment closes loans, advances, claims, asset recoveries and records leave encashment; settlement statement PDF.
- **Adapters** (`adapters.py`): attendance `payroll_summary`, leave LOP/balances/encashment/work calendar, organisation legal entities/locations/grades/cost centres (falls back to company fields and the State master), asset recoveries.

### Pages (web)
`/dashboard/payroll` (overview: cost trend, liabilities, due dates, alerts), `/dashboard/payroll/runs` and `/dashboard/payroll/runs/[uuid]` (stepper: Inputs, Checks, Calculate, Review, Approve, Lock and pay), `/dashboard/payroll/salaries`, `/dashboard/salary-structures`, `/dashboard/payroll/components`, `/dashboard/tax`, `/dashboard/statutory`, `/dashboard/expenses`, `/dashboard/loans`, `/dashboard/settlements`, `/dashboard/payroll/me` (My pay: payslips, salary, tax, expenses, loans), `/dashboard/payroll/settings` (general, payslips and tax, pay groups, statutory rules and minimum wage import, bank files, accounts). Components in `src/components/payroll/`. Nav: Payroll section entries, "My pay" under Home, "Payroll settings" under Settings.

### Endpoints
`/payroll/*` (access, dashboard, settings, bank-formats, components, structures, pay-groups, pay-periods, salaries, profiles, statutory rules/overrides/minimum-wages, states), `/payroll/runs/*`, `/payroll/payslips/*`, `/payroll/statutory/*`, `/payroll/tax/*`, `/payroll/settlements/*`, `/payroll/me/*`, `/platform/statutory/rules`, `/expenses/*`, `/loans/*`.

### Tables
Platform: statutory_rules. Company (company_id, index, forced RLS): statutory_overrides, payroll_settings, salary_components, salary_structures, pay_groups, pay_periods, employee_payroll_profiles, employee_salaries, pay_runs, pay_run_inputs, payslips, statutory_challans, tax_declarations, perquisites, payroll_files, final_settlements, expense_categories, expense_claims, expense_lines, expense_advances, loan_types, loans, loan_instalments. One migration `py1a2b3c4d5e` (from `w8x9y0z1a2b3`).

### Permissions
New: `payroll.salaries.manage`, `payroll.reopen`, `payroll.settings`, `payroll.tax.manage`, `payroll.statutory.manage`, `payroll.settlements.manage`, `expenses.manage`, `loans.manage`. HR gets run, salaries, tax, statutory, settlements, expenses and loans; approve, reopen and settings stay with the HR admin.

### Tests
`tests/test_payroll_calc.py` (65 worked examples: PF ceiling/unrestricted/58+/VPF/admin minimum, ESI rates, thresholds, disability, contribution periods, low daily wage, PT Maharashtra February and women, Karnataka, Tamil Nadu half-yearly, annual states and the ₹2,500 cap for every state, LWF, new regime rebate and marginal relief, old regime and seniors, surcharge marginal relief, HRA, monthly TDS, one-time payments, regime comparison, no-PAN, 50% rule, proration with LOP, joiners, leavers, fixed-30, mid-month revision, add-back into PF, warnings, gratuity eligibility and amount, bonus, retrenchment, EMI, loan perquisite, 2-working-day deadline, override floors). `tests/test_payroll_api.py` (19: CTC split, full run with approval, bank files, ECR format, journal, Tally, encrypted payslip, reopen; arrears on a back-dated revision without double payment; ESI coverage for the contribution period; minimum wage block and acknowledgement; Excel inputs; F&F retrenchment totals, deadline and payment; expenses through payroll; loan instalments; declarations and verification; overrides by date; tenant isolation; employee permissions).

### Decisions
- Statutory values are data (platform table + company overrides), never code; rules are looked up on the period's last day. A company override applies only from its own date and only while no newer library version starts after it.
- Before 21 Nov 2025 the library uses pre-Code values (no 50% rule, 5-year gratuity for fixed-term, no re-skilling fund).
- The 50% wage rule follows the requirements: basic + DA (+ retaining) are wages; other allowances are tested against 50% and the excess is added back (also to PF wages; switchable in settings).
- Minimum wage default skill is "skilled"; sample minimum wage tables must be replaced by imports of current notifications.
- Arrears are recomputed as "should have earned − paid − arrears already paid", so recalculating never pays twice.
- ESI coverage is decided at the start of each contribution period (or at joining) from the payslip of that month.
- Form names follow the library ("Form 16 (Form 130)", "Form 24Q (Form 138)" from FY 2026-27); 2025 Act section references are shown next to familiar names and must be confirmed by the tax consultant.
- Bank file layouts are standard bulk-upload layouts and editable; confirm with each bank before first use. The TDS return text follows the NSDL record structure and must be validated with the FVU.
- Payslip PDFs use WeasyPrint for proper Devanagari shaping and pypdf (new dependency, `requirements.txt`) for AES encryption. The Docker image needs a Devanagari font (e.g. `fonts-noto-core`).
- Demo data: the first demo month ignores attendance LOP (no punches before go-live) and opening balances for April–September are seeded as if imported.

### Unfinished / next
- Flexible benefit bills exemption uses approved claims in FBP-linked categories; a dedicated FBP claim screen is not built.
- Payout-partner integration, digital signing of Form 16, pay-equity and cost-vs-budget reports are not built.
- The F&F off-cycle run is not generated from a settlement; settlements are paid directly (single-row bank file not provided).
- Tests can't run against `hrms_test` while other modules' tables there are out of step with their migrations; they pass on a fresh database (conftest default).

## Insights and data: reports, dashboards, import/export, compliance, data assistant and MCP

Backend `modules/reports`, `modules/analytics`, `modules/dataio`, `modules/compliance`, `modules/assistant` (migration `in1s2i3g4h5t`, down revision `w8x9y0z1a2b3`); web `src/app/dashboard/{reports,dashboards,compliance,assistant}`, `src/app/dashboard/settings/{import-export,integrations}`, `src/components/insights/*`; tests `tests/test_insights.py` (26 tests).

### Built
- **Report engine** (`modules/reports/registry.py`, `engine.py`, `sources/*`). A registry of data sources, each with typed fields (text, number, money, date, datetime, bool, enum, hours, percent), joins, computed fields, and whose row it is. Query sources (SQL with joins; filters on plain fields run in SQL, computed fields in Python): employees (with age/service bands, next birthday, missing PAN/UAN/bank), employee documents, job changes, attendance by day, leave requests, payslips (PF/ESI/PT/LWF/TDS split from the lines), salaries, statutory challans, final settlements, loans, expense claims, training, review results, kudos, help desk cases (SLA, hours to reply/resolve), assets, tasks, policy acknowledgements, job applications (when recruitment is installed). Function sources call the modules' public report functions: headcount movement/attrition, leave balances, LOP (leave), attendance summary, late/early, overtime with the quarterly cap, muster roll (one column per day). A module can add its own with `modules/<name>/report_sources.py`.
- **Row-level scope**: My data / Me and my direct reports / Me and all my reports / Everyone. The widest scope comes from existing permissions (owner, HR admin or the source's "all" permissions → everyone; anyone with reports → team; else own rows). A report can narrow it, never widen it. Sensitive fields (PAN, UAN, pay amounts) are masked for other people's rows unless the viewer has the permission. Pay sources are closed to managers (own rows only).
- **Custom report builder**: columns, filters (incl. relative dates: this month, this FY, last/next N days, tokens like `@month_start`), group by (2 levels, dates by day/week/month/quarter/year), totals (count, distinct, sum, average, min, max), sort, top N, chart type; save, copy, share with roles and people (HR), schedule.
- **43 standard reports** (headcount, joiners, leavers, attrition, gender/age/service mix, diversity, birthdays, probation due, documents expiring, missing PAN/UAN/bank, leave balances/taken/register, LOP, attendance summary/today, late/early, overtime, muster roll, payroll register, statutory summary, payroll cost, CTC, challans, final settlements, loans, expenses, training completion/overdue, review progress, hiring funnel/sources, survey results, kudos, help desk SLA/open cases, asset register, pending policy acknowledgements). Kept in step at every start.
- **Exports**: Excel, PDF and CSV; every export (download, scheduled email, data export, assistant/MCP) is written to `export_logs` and the audit log (`export.created`, `export.downloaded`). Files can be locked with a generated password shown once (Excel: Office encryption via `msoffcrypto-tool`, added to requirements.txt; PDF: AES-256; a protected CSV comes as protected Excel). Files are kept 24 hours for the maker only.
- **Schedules**: daily/weekly/monthly at a local hour, Excel/PDF/CSV, to people and addresses; run with the access of the person who set it up; sent by the periodic job `reports.schedules` through the company SMTP (Integrations) or platform SMTP. Demo company: "Monday headcount".
- **Dashboards** (`modules/analytics`): widgets KPI, line, bar, stacked bar, area, pie, donut, table, heatmap, funnel, gauge, each from a saved report or a source with grouping; 12-column grid with drag to arrange and resize; share with roles/people; each viewer sees widgets with their own access. Ready dashboards: HR overview, Workforce, Attendance, Leave, Payroll cost, Engagement, Hiring. Charts are hand-made SVG (`src/components/insights/charts.tsx`) with themed tooltips.
- **Import and export wizard** (`modules/dataio`): employees, salaries, leave balances (add or set), attendance punches, holidays, assets, dropdown lists (masters), departments, designations. Upload Excel/CSV (10 MB, 20,000 rows) → auto-matched column mapping → add / update / add-or-update by a match key → validation with row-level errors and preview (nothing saved) → background run with progress (rows in savepoints, through the modules' own services) → error file; templates with a "How to fill" sheet; exports in the import format; history.
- **Compliance dashboard** (`modules/compliance`): per legal entity and state — PF, ESI, TDS, PT, LWF, salary payment, quarterly TDS return and Form 16 from the payroll compliance calendar; monthly registers, POSH annual report, bonus return (Form D) and registration renewals (from legal entity registrations) marked here; risks: below minimum wage (payslip warnings), missing PAN/UAN/bank, final settlements near the 2-working-day deadline, overtime near the quarterly limit, pending policy acknowledgements; law notices (built-in plus changes in the payroll statutory library); plain-language guidance for each.
- **Data assistant** (`modules/assistant`): chat with conversation history. Safe tools: search employees, headcount (grouped), joiners/leavers, leave balances, who is on leave, attendance summary, pending approvals, list reports, run a report — all through the report engine with the asker's scope; every call audited. With an AI model set up (Integrations → AI model: OpenAI-compatible base URL, Anthropic or Gemini; key encrypted with Fernet) the model calls the tools; without one a built-in question matcher answers common questions. If the model fails, it falls back to the built-in answer.
- **MCP server** at `/api/v1/mcp` (Streamable HTTP; `/api/v1/mcp/sse` + `/messages` for older SSE clients): initialize, tools/list, tools/call, ping. Personal access tokens (`ngh_pat_…`, hash only, shown once, optional expiry, chosen tools), created and revoked in Settings → Integrations, need `insights.mcp.connect`; a token acts as its owner with their current permissions and scope; uses and tool calls are audited.
- **Integrations page**: AI model, MCP tokens with connection instructions, email (SMTP, with a sign-in test), SMS and WhatsApp (configuration placeholders), accounting (Tally / Zoho Books ledger settings), Google and Microsoft sign-in, job boards (when recruitment is installed). Secrets are stored encrypted and only their last 4 characters are shown.

### Endpoints
`/reports/{meta,sources,sources/{key},share-options,preview,export,exports,exports/{uuid}/download,schedules,schedules/{uuid},schedules/{uuid}/send-now}`, `/reports/`, `/reports/{uuid}` (+ `/run`, `/export`, `/share`, `/clone`); `/dashboards/{meta,widget-preview}`, `/dashboards/`, `/dashboards/{uuid}` (+ `/clone`, `/widgets/{id}/data`); `/data/{entities,entities/{key}/template,imports,imports/{uuid},imports/{uuid}/validate,imports/{uuid}/run,imports/{uuid}/errors,jobs,exports}`; `/compliance/{dashboard,records}`; `/assistant/{status,ask,threads,threads/{uuid}}`; `/integrations/`, `/integrations/{kind}` (+ `/test`), `/integrations/tokens` (+ `/mine`, `/{uuid}`); `/mcp`, `/mcp/sse`, `/mcp/messages`.

### Tables
reports, report_schedules, export_logs, dashboards, data_jobs, compliance_records, integration_settings, access_tokens, assistant_threads — all with company_id, an index and forced RLS.

### Permissions (added from `modules/reports/hooks.py`)
`insights.reports.manage`, `insights.reports.all_data`, `insights.dashboards.manage`, `insights.data.import`, `insights.data.export`, `insights.compliance.view`, `insights.mcp.connect`, `settings.integrations.manage`. HR gets all but the last; managers get `insights.mcp.connect`. Imports and exports also need the owning module's permission (e.g. salaries need payroll).

### Tests
`tests/test_insights.py`: standard reports and dashboards seeded; a manager sees only themselves and their team (saved, grouped and ad hoc reports); employees see nothing until a report is shared, then only their own rows; pay stays own-rows-only for managers; masking of PAN; custom report with group/filter/sharing; export audit, password protection (Excel and PDF) and download limited to the maker; schedule logged; dashboard widgets with viewer access and saving a moved widget; import validation, update by employee ID, update mode refusing unmatched rows, error file, permission check, export round trip, masters upsert with duplicate rows; assistant tools by role; MCP token auth, tool scopes, revoke, cross-company isolation; AI key stored encrypted and the fallback when the model is unreachable; compliance alerts and marking; tenant isolation through the API and a scoped session; intent matcher.

### Decisions
- Query sources read other modules' tables read-only through SQL joins (a generic report builder can't be built on per-module service calls); standard reports that already exist as module functions (leave balances, LOP, attendance, overtime, muster roll, survey results) call those functions. Writes always go through the module services.
- A manager's team scopes include their own row ("Me and my reports").
- Grouping and totals run in Python over at most 100,000 rows per run; good for the companies we target.
- The Insights section is visible to everyone; Reports and Dashboards show only what is shared with or allowed for the person; Compliance and Audit log stay admin-only. Integrations moved to `/dashboard/settings/integrations`, and Import & export was added under Settings.
- Fresh-database migrations: `alembic upgrade heads` currently fails on an empty database because `tl1a2b3c4d5e` (talent lifecycle) uses `depends_on`; upgrading each head in turn works. My tests were run on `hrms_test` built that way.

### Unfinished / next
- SMS, WhatsApp, Google/Microsoft sign-in and job boards store their settings but don't connect yet; Zoho Books push is not built (Tally/Zoho settings are for the payroll journal export).
- Imports from Google Workspace/Microsoft 365 and ZIP document imports are not built; the wizard handles one sheet.
- Minimum-wage breaches come from payslip warnings, so they appear after a pay run.

## Integration: owner as employee, roles and access, permission-driven navigation

### Built
- **Owner as employee.** Every company owner has a linked employee record (`companies.owner_employee_id`), created at sign-up and backfilled once at start-up for older companies (`modules/company/owner.py`, called from `setup_company`; idempotent: links an existing employee with the company email before creating one). New records get the owner's name, role `admin`, designation "Managing Director" (created if missing) and a Leadership/Management department when one exists. The owner token now carries `employee_id` plus `owner: true`; `get_current_user` returns `is_owner`, the request context keeps `actor_type = "owner"`, and full admin rights are unchanged. `/auth/me` returns `is_owner`; password change/reset for the owner updates the company login and keeps the owner's employee record in step; timezone and name changes still go to the owner's account.
- **Things that assumed "owner has no employee", fixed:** approvals inbox shows the owner both "company owner" tasks and tasks addressed to their record, "my requests" shows both; the workflow engine never asks the owner to approve their own request (requests by the owner's record auto-approve when nobody else is in the chain, as before); POSH visibility for the owner; leave `is_owner`; Buzz/announcement audiences (the owner still sees everything; `Actor.owner`/`viewer_id`); the owner gets no "new joiner" welcome post; mobile QR sign-in keeps the owner flag.
- **Roles and access** (`modules/roles`): permission catalogue grouped by module (People, Time, Payroll, Talent, Engage, Insights, Settings) built from every key registered anywhere in the backend (`catalog.py`; module hooks still add their own keys); data scope per permission where it applies (own, direct reports, all reports, department, location, legal entity, everyone; widest wins across roles); sensitive-field visibility per role (salary, bank, PAN, Aadhaar: hidden, masked, full; most open wins); several roles per employee (`employee_roles`; `employees.role` stays the base system role, the most powerful system role held). Custom roles: create, edit, clone, delete; system roles can't be deleted, the HR admin role is locked to full access, other system roles can be edited (`roles.customised` stops start-up from resetting them) and reset to defaults. Assign roles per person and in bulk; only an HR admin can give or take away HR admin; the owner always keeps it. Every change is audited (role and assignment rows through the model audit, plus `roles.assigned` and `role.deleted` events).
- **Enforcement:** `_get_role_permissions` now unions the extra roles of the signed-in employee (cached per employee, cleared on any role change), so every existing `require_permission` and in-module check honours multi-role. The data scope of "See employees" limits the employee list; field visibility narrows PAN, Aadhaar and bank details on employee profiles (it never widens `people.sensitive.view`). Helpers for other modules: `modules/roles/access.py` (`my_access`, `permission_scope`, `scope_employee_ids`, `field_level`).
- **Endpoints:** `GET /roles/catalog`, `GET /roles/my-permissions` (roles, permissions, scopes, field access, is_owner, is_manager), `GET/POST /roles/`, `GET/PUT/DELETE /roles/{id}`, `POST /roles/{id}/clone`, `POST /roles/{id}/reset`, `GET /roles/assignments/people`, `PUT /roles/assignments/{employee_id}`, `POST /roles/assignments/bulk`. All management endpoints need `settings.roles.manage`.
- **Web:** Roles & access page (`/dashboard/roles`): role cards, create/edit/view modal with the grouped permission matrix (sticky headers, group select-all, search, scope per row) and sensitive-field controls, clone/reset/delete, People tab with multi-role assignment and bulk add/remove. Navigation is permission-driven: each entry in `src/lib/hr-nav.ts` declares `need` (any of these permissions), `team` (also for people with reports), `self` (needs an employee record) or `notOwner`; `src/lib/access.tsx` loads `/roles/my-permissions` once per sign-in (kept in sessionStorage) and the shell shows only usable sections and pages. A route guard in the shell shows "You don't have access to this page" (with a link home) instead of rendering the page; API 403s on reads now read "You don't have access to this." The Create menu follows permissions. Departments are read-only for people without `people.departments.manage`; the leave types crash for non-admins is fixed; HR-only Expenses and Loans pages are hidden from employees (they use My pay).
- **Demo clean-up:** `backend/scripts/demo_cleanup.py` (re-runnable): owner record as Tintu C Raju, Managing Director, Leadership; archives the test "Asha Menon" employees; removes the "Kit QA" departments (and their demo increment budgets); removes approval requests whose leave request no longer exists (the duplicates in Priya's approvals); marks older notifications read so nobody has more than 12 unread.
- **Migrations:** `mg1a2b3c4d5e` merges every module branch into one line; `ig1a2b3c4d5e` adds `roles.scopes`, `roles.field_access`, `roles.customised`, `companies.owner_employee_id` and `employee_roles` (with RLS).

### Tests
`tests/test_roles_access.py`: owner record, rights and self-service; idempotent backfill; custom role create/clone/assign/bulk remove/delete with audit; only role managers change roles; data scope on the employee list; field visibility hiding PAN; tenant isolation of roles and assignments.

### Decisions
- The owner signs in as the owner (company email and password); the employee record shares the password hash but is never used to sign in separately. The owner keeps `actor_type = "owner"` so owner-addressed approvals keep working.
- Scopes and field visibility restrict; they never grant beyond the permission itself. The HR admin role always sees everything.
- A customised system role no longer picks up new default permissions at start-up; "Reset to defaults" brings it back in line.

### Unfinished / next
- Data scopes are enforced on the employee list; other modules still use their own team/all rules and can adopt `permission_scope` / `scope_employee_ids` step by step. Salary visibility is surfaced to the web app and enforced through payroll permissions, not per field in payroll screens.
- Pages that make optional admin-only calls inside otherwise open pages should check `useAccess().can(...)` before calling.

## Payroll: second pass

### Built
- **Full and final as an off-cycle run** (`modules/payroll/settlement.py`): approving a settlement (workflow `payroll.settlement`) makes a pay run of kind `settlement` for that one person, already approved, with the pay date set to the 2-working-day deadline. Its payslip is built from the settlement (`fill_payslip`): the final month's salary components, gratuity (`GRATUITY_PAY`), leave encashment, notice pay, retrenchment, bonus, reimbursements, flexible-benefit bills and balance, and every recovery; PF/ESI/PT of the final month with the employer contributions (so the ECR, ESI file, compliance calendar and statutory liability include them) and the TDS worksheet in `tax.month` (so YTD and Form 16 count it). The run is locked, its bank file has one row (narration "Full and final settlement"), and marking it paid closes the settlement (loans, advances, assets, leave, FBP bills). `POST /settlements/{uuid}/pay` locks and pays the run in one step; cancelling a settlement cancels its run. A regular run leaves out anyone whose settlement pays that month's salary (`salary_covered`), so nobody is paid twice. The settlement PDF (and the run's payslip PDF) is a payslip-style statement (`pdf.fnf_statement_html`). The run page shows the deadline countdown and links to the settlement.
- **Flexible benefit plan** (`modules/payroll/fbp.py`): the FBP amount is the yearly total of the salary's FBP components. In the declaration window (settings `fbp.declaration_from/to`) the employee splits it among FBP components within each component's yearly limit (unsplit money is paid as taxable at the year end). FBP components are no longer paid with the monthly salary: the engine holds them back (`accrue_codes`; stored per payslip in `statutory.fbp_accrued`). Bills are uploaded from My pay → Flexible benefits (photo or PDF; the phone camera opens), payroll verifies them (accept in full, in part or not at all with a note), and approved amounts are paid with the next salary as reimbursement lines up to what is due so far (allocation × months elapsed / 12; the whole allocation in March or on exit). Each component says in which regimes approved bills are tax-free (`salary_components.fbp_tax_free_regimes`, defaults: fuel, phone and books both regimes, meal card old regime only). In March (or on exit) whatever accrued and was not claimed is paid as the taxable one-time earning `FBP_BAL`. Expense-claim based `bills_exempt` is switched off while the plan is on (no double exemption).
- **Increments → payroll** (`modules/payroll/revisions.py`): `payroll.service.apply_salary_revision(db, company_id, employee_id, ctc, effective_date, ...)` and `current_ctc(...)` now exist, which the pay review module already calls on the effective date. Each call becomes a pending revision (once per proposal), with the reason (promotion when the employee has a promotion on that date) and an arrears estimate for locked months. Payroll reviews it on **Salary revisions** (new split against the old, arrears per month), applies it (an effective-dated salary through `assign_salary`; the next pay run adds the arrears with the existing self-correcting logic) or rejects it with a reason; bulk apply. `revisions.auto_apply` skips the review.
- **Reports** (`modules/payroll/insights.py`, `modules/payroll/report_sources.py`, standard reports in `modules/reports/standard.py`): Pay equity by gender (per designation and grade: average and median of men and women, gap = (men − women) / men, flagged at `pay_equity.gap_percent`, default 5%, per Code on Wages s.3), Payroll cost against budget (departments and cost centres; cost so far, projection, budget left), CTC report (now a breakup: basic, DA, HRA, allowances, flexible, employer contributions), Payroll variance trend (month on month, flagged at `variance_percent`), Statutory liability summary (PF, ESI, TDS, PT, LWF owed, paid against challans, outstanding, days late). Budgets are typed in on **Payroll budgets** (`payroll_budgets`).
- **Form 16 digital signing** (`modules/payroll/signing.py`, pyHanko 0.38): upload the signatory's .pfx/.p12 (checked with its password, which is not stored or logged), then sign every Form 16 of the year with the password typed again; PAdES signature with a visible stamp on the last page. Signed copies (`payroll_files` kind `form16_signed`) are what HR, the zip and the employee download from then on. Without a certificate, signing runs in stub mode with a throwaway self-signed certificate and a "TEST SIGNATURE, not valid for filing" stamp. UI in Income tax → Returns and Form 16.
- **Benefits and group insurance** (new module `modules/benefits`): plans (group health, accident, term life, top-up, parental) with insurer, policy number, TPA, broker, contacts, policy year; coverage tiers (spouse, up to N children, up to N parents) with yearly premiums, optional per-parent age bands (checked for gaps and overlaps); sum insured fixed or a multiple of CTC with a floor and a cap; employer share as % or a flat amount; eligibility rule (safe expression over grade, grade_rank, department, location, designation, employment_type, age, months_of_service, annual_ctc, gender); waiting days and when cover starts; compulsory and auto-enrol flags; joining window (days from joining) and renewal window. Enrolments keep the covered family members (from the employee's family records), sum insured, premium split and monthly deduction, with history. Auto-enrol (every 15 minutes and on demand) enrols eligible joiners and ends cover for leavers and people no longer eligible. The employee's share is deducted in payroll as `INS_PREM` (pro-rated for part months) via `benefits.payroll_deductions`, picked up by the pay run's input sync. Monthly insurer file: additions and deletions, one row per insured person (Excel), with sent dates recorded.

### Pages (web)
`/dashboard/payroll/revisions`, `/dashboard/payroll/flexible-benefits` (bills to verify with the bill beside the ledger; employee splits and lock), `/dashboard/payroll/budgets`, `/dashboard/benefits` (plans, enrolments, insurer file), `/dashboard/benefits/me` (My benefits: cover, family, choose or opt out while a window is open), My pay → Flexible benefits tab, Income tax → Returns and Form 16 → Digital signature, settlement modal (pay run link) and run page (settlement banner). Nav: My benefits (Home), Salary revisions, Flexible benefits, Benefits & insurance, Payroll budgets (Payroll). Shared pieces in `src/components/payroll/fbp.tsx`, `src/components/payroll/signing.tsx`, `src/components/benefits/plan-modal.tsx`.

### Endpoints
`/payroll/revisions` (+ `/{uuid}`, `/apply`, `/reject`, `/apply-many`), `/payroll/fbp/claims` (+ `/{uuid}`, `/verify`), `/payroll/fbp/declarations` (+ `/lock`), `/payroll/fbp/files/{uuid}`, `/payroll/me/fbp` (GET/PUT), `/payroll/me/fbp/claims` (GET/POST, `/withdraw`), `/payroll/budgets` (GET/PUT), `/payroll/tax/signing` (GET/PUT), `/payroll/tax/signing/certificate` (POST/DELETE), `/payroll/tax/form16/sign`, `/payroll/settlements/{uuid}/pay-run`; `/benefits/access`, `/benefits/plans` (+ `/{uuid}`, `/insurer-file`), `/benefits/enrolments` (+ `/{uuid}`, `/terminate`), `/benefits/auto-enrol`, `/benefits/family/{employee_uuid}`, `/benefits/me`, `/benefits/me/enrol`, `/benefits/me/waive`.

### Tables
Migration `py2b3c4d5e6f` (down revision `py1a2b3c4d5e`, a separate head for the coordinator to merge): `salary_revision_requests`, `fbp_declarations`, `fbp_claims`, `payroll_budgets`, `benefit_plans`, `benefit_enrolments` (all with company_id, index and forced RLS) and the column `salary_components.fbp_tax_free_regimes`. New salary components (added to every company at start-up): `FBP_BAL`, `GRATUITY_PAY`, `NOTICE_PAY`, `RETRENCH`, `INS_PREM`. New settings: `fbp`, `pay_equity`, `signing`, `revisions`. New permission `benefits.manage` (admin and HR). Dependency added: `pyHanko==0.38.1`.

### Tests
`tests/test_payroll_second_pass.py` (11): settlement → off-cycle run (totals, final-month PF and employer PF, TDS, regular run leaves the leaver out, one-row bank file, ECR, statement PDF, paying the run closes the settlement), settlement pay in one step, FBP split limits and bills (accrual instead of pay, payout up to what is due, tax-free per regime), year-end taxable balance, pay-review revision queued once with arrears and paid after apply, pay-equity maths, all new reports run plus budgets, Form 16 signing in stub mode and with a certificate (wrong passwords refused, password not stored, signatures intact), insurance premium deducted and pro-rated plus insurer file, auto-enrol with salary-multiple cover and self-service rules, tenant isolation and employee access. Existing payroll tests updated for `GRATUITY_PAY` and the owner's own employee record.

### Decisions
- The settlement's approval is the run's approval (no second approval); reopening the run sends it through `payroll.run` again.
- FBP money accrues instead of being paid monthly, as the brief asks ("paid tax-free per the rules, unclaimed paid as taxable at year end"). Payout is capped by the allocation spread evenly over the year rather than by the exact accrual, so a mid-year joiner's bills wait for their months.
- New regime: only components listed for `new` are tax-free (official-use phone and fuel reimbursements are not perquisites; meal cards are an old-regime exemption). Companies can change this per component.
- Pay equity compares annual CTC (setting `pay_equity.basis` can be `monthly_gross`) within designation and grade; people with no gender recorded are counted but not compared.
- Benefits live in their own module; it only talks to payroll through `payroll.service.current_ctc` and the pay run's input sync.

### Unfinished / next
- Section 80D link from the employee's health premium share to the tax declaration is not wired yet.
- Insurer e-cards and policy documents are not stored; plans keep contact details only.
- The demo November run was calculated before FBP accrual existed; recalculating it moves the MGMT staff's fuel and phone allowance into accrual.

## Leave and attendance: second pass

Backend: `modules/attendance` (`leave_link.py`, `lop.py`, `kiosk.py`, `registers.py`, `router_extra.py`), `modules/leave` (`bradford.py`, `reminders.py`, calendar/rules/reports changes), `modules/shifts` (auto shift), `modules/timesheets/billing.py`; migration `la2b3c4d5e6f` (down revision `a7t1m2e3s4h5`, depends on `lv1a2b3c4d5e`). Web: attendance settings/reports/My attendance, `/clock-in/[token]`, timesheets Billing tab, leave types/settings/reports/admin. Tests: `tests/test_leave_attendance_links.py` (23).

### Built
- **Absence to leave** (time setting `leave_link.absence`): an absent day (or the missing half of a half day, or a missing punch) still absent `after_days` later is covered by leave from the listed types in order (first with balance, recorded as HR-recorded leave), else loss of pay (an LOP leave entry when an LOP type exists). With `needs_approval` it goes to the manager first (request type `attendance.absence_leave`; rejected = stays LOP). When attendance is corrected (punch, regularisation, WFH, on duty) the leave day is cancelled and the balance restored automatically. Runs after every recompute and once a day (`attendance.leave_link` job).
- **Late-mark penalties from leave** (`leave_link.late_penalty`): penalty days are taken (ledger `availed`, meta `late_penalty`) from the listed types in order, else stay LOP; given back (`restored`) when the late mark goes away.
- **Comp off from attendance** (`leave_link.comp_off`): work on a weekly off/holiday ≥ half-day hours credits a half or full day through `leave.service.credit_comp_off_from_attendance` (now with hour thresholds and an approval option); taken back (`withdraw_comp_off`, written as an expiry of that credit) when corrected below the minimum.
- Every step is a row in `attendance_leave_links` (audited), listed under Reports → Leave from attendance with a manual Undo.
- **LOP per pay period** (`lop_control`: cap_days, carry_excess): pre-payroll review (`GET /attendance/lop`, CSV) with attendance LOP, carried in, manual changes (+/-, with note), total, payable, carried out, waived, reduced-pay days and the LOP sent to payroll. Locking the attendance period freezes the figures (`final` entries) and carries the excess into the next period; unlocking voids them; no manual changes in a locked period. `payroll_summary` applies it for whole pay periods, so payroll's `lop_days + adjustment_lop_days` equals the final figure.
- **Half-pay leave**: leave rule `pay_percent` (100 default, maternity must stay 100). Attendance keeps that share as payable and the rest as LOP (`attendance_days.pay_cut_days`, `reduced_pay_days` in the payroll export); leave's LOP report counts it too (cap doesn't apply to it).
- **Bradford score** (leave setting `bradford`: window weeks, leave type codes, include absent days, levels with labels, alerts to HR/owner and manager): `GET /leave/reports/bradford` (+CSV), `/leave/me/bradford`, daily alerts once per level reached (`leave_bradford_alerts`).
- **Expiry reminders** (`expiry_reminders.days_before`, default 30 and 7): email + notification before comp off/carried leave lapses, once per credit and step (`leave_expiry_reminders`); "Lapsing soon" on Leave admin.
- **Substitute day off** (`substitute_holiday`: next or previous working day, national only option): a fixed holiday on a weekly off makes that day a holiday "Substitute for …" in the work calendar (so attendance and leave both see it) and in My holidays.
- **Kiosk clock-in**: kiosks (hashed link token shown once, new link, photo off/optional/required, allowed IPs); employees set a 4–6 digit PIN on My attendance (HR can set/clear); `/clock-in/<token>` page with keypad and camera, punches by employee code + PIN; offline queue in localStorage synced via `/attendance/kiosk/{token}/sync` (times up to 7 days old, duplicates within a minute ignored); 5 wrong PINs lock for 15 minutes.
- **Auto shift**: work arrangement flag `auto_shift`; each working day takes the shift starting closest to the first check-in within `auto_shift.window_minutes` (optionally from chosen shifts); rosters win; night-shift check-outs aren't reused for the next day.
- **Statutory registers**: register of attendance and of overtime, generic plus Karnataka, Maharashtra, Tamil Nadu, Delhi and Telangana layouts (Act names, establishment details from setting `registers`, state-specific columns), as Excel or PDF, with on-screen preview.
- **Timesheet billing**: rates per person-on-project > project > person, effective-dated; invoices per client per period from approved billable hours not yet billed (hours locked to the invoice; void frees them); GST from setting `billing` (company GSTIN/state, rate, SAC): CGST+SGST in the same state, IGST across states (place of supply = client's state, from its GSTIN if blank); numbering `INV/26-27/0001` when sent; draft → sent → paid (reference) / void; PDF with amount in words. Clients gained address and GST state.
- **SFTP**: `paramiko==5.1.0` in requirements and the venv; SFTP pull supports password or private key and an optional server key fingerprint check; saved password/key masked.

### Endpoints
`/attendance/leave-links` (+`/{uuid}/reverse`), `/attendance/lop` (+`/adjustments`, `/adjustments/{uuid}`), `/attendance/kiosks` (+`/{uuid}`, `/{uuid}/link`), `/attendance/kiosk-pins` (+`/{employee_uuid}`), `/attendance/me/kiosk-pin`, public `/attendance/kiosk/{token}` (+`/punch`, `/sync`), `/attendance/registers` (+`/formats`); `/leave/reports/bradford`, `/leave/me/bradford`, `/leave/bradford/alerts/run`, `/leave/expiry-reminders/due|send`; `/timesheets/billing/summary|rates|rates/{uuid}|invoices|invoices/preview|invoices/{uuid}|invoices/{uuid}/status|invoices/{uuid}/pdf`. New time settings: `leave_link`, `lop_control`, `auto_shift`, `billing`, `registers`.

### Tables
attendance_leave_links, lop_period_entries, attendance_kiosks, kiosk_pins, ts_billing_rates, ts_invoices, leave_bradford_alerts, leave_expiry_reminders (company_id, index, forced RLS); columns attendance_days.pay_cut_days, work_arrangements.auto_shift, ts_clients.address/state_code, ts_time_logs.invoice_id.

### Tests
`tests/test_leave_attendance_links.py`: half-pay in the engine, LOP cap/carry, Bradford spells and levels, closest shift, GST split and amount in words, SFTP with a mocked connection (files, delete, host key refusal) and a device pull inserting punches; absence → leave and back on correction, approval flow and LOP fallback, waiting days; late penalty from leave and given back, and without balance; comp off from a Sunday and taken back; LOP cap/manual/lock/carry/unlock and the payroll export; half-pay SL in payroll and leave LOP, maternity floor; Bradford report and one alert per level; expiry reminder once; substitute holiday; kiosk PIN rules, punch, offline sync with duplicate and wrong PIN, lockout, new link; auto shift; registers in every format (Excel/PDF/preview); invoice with CGST/SGST and IGST, double billing blocked, status flow, PDF, void; permissions and tenant isolation incl. RLS on all new tables. Also fixed `tests/test_leave.py` leaking a company into the test thread's context (it made `test_tenant_isolation` fail in a full run).

### Decisions
- Converted absences use the leave module's own requests (recorded by HR), so balances, reports and payroll all see them; "LOP" with no LOP leave type just stays an absent (LOP) day.
- Late penalties are taken straight from the ledger (no leave request) and only for days in open periods.
- State register form numbers are typed in settings (`registers.form_labels`) because states renumber forms; layouts should be checked against the latest state notification.
- Kiosk offline queue keeps the PIN in the tablet's storage until it is sent (needed to verify later); the queue is cleared after each sync.
- Billing supplier name/address default to the company's.

### Unfinished / next
- Kiosk face verification is not built (photo only). Statutory register wage columns (amounts) are not included (attendance data only). Invoices are not emailed to clients or pushed to accounting.



## Talent lifecycle: hiring, onboarding, probation and exit/alumni

### Built
- **Hiring** (`modules/recruitment`): manpower requisitions (department, designation, location, openings, new or replacement, CTC range, experience, justification) approved through the workflow engine (`recruitment.requisition`: department head, then HR); job openings created blank or from an approved requisition (prefilled) with description, required and good-to-have skills, experience, location and work mode, salary range with a show/hide switch, openings, hiring manager and team, custom application questions, per-job pipeline stages and screening rules; publish/hold/close/reopen; filled automatically when the openings are hired.
  - **Careers page** at `/careers` on the company address (public, branded from settings, department/location/work-mode filters), job page with an apply form (CV upload, custom questions, privacy notice and consent recorded with time and IP, honeypot against bots), confirmation email; an **embeddable widget** (iframe and script snippet that resizes through `postMessage`) shown in settings.
  - **Job boards**: share links (LinkedIn, WhatsApp, X, email), XML feed (Indeed format) and JSON feed at `/public/careers/{slug}/feed.xml|json`; API connectors for Naukri and LinkedIn behind settings with keys (write-only on screen); every posting result is recorded on the job and a failure never blocks hiring.
  - **Candidates**: profile, CV reading (text from PDF/DOCX/TXT, then rules for name, email, phone, LinkedIn, years, skills, education, current role and company, CTC, notice, city), source, tags, talent pool, owner, follow-up reminders, duplicate detection by email/phone and merge (the duplicate is marked merged, never deleted).
  - **Pipeline**: stages per job on a kanban board with ordered cards; moving to the hired stage needs an accepted offer; reject with a reason (optional email), reopen, notes, history; bulk move/reject/tag/talent pool.
  - **Screening**: rule-based score 0-100 against required skills (whole-word match), experience range, good-to-have skills and location, with a points breakdown and a plain explanation; auto-reject/auto-shortlist thresholds per job. Scorers are pluggable (`screening.register("ai", fn)`, chosen in settings) so an AI scorer can replace the rules without changing callers.
  - **Interviews**: rounds with mode, time, duration, panel, meeting link or place and an interview kit; invite emails with an `.ics` attachment (request/cancel with sequence numbers) to the candidate and panel, `.ics` download, reminders N hours before, reschedule and cancel; kits with competencies and questions; **scorecards** per interviewer (competency ratings 1-5, overall, recommendation, strengths, concerns, notes; interviewers see only their own); an **automatic evaluation summary** (average, per-competency averages, recommendation counts, hire/consider/hold/reject with a strong-no veto) and a manual override with a reason.
  - **Reference checks** (form sent by a 14-day link, public form, responses on the profile) and **background verification** status, vendor, notes and consent.
  - **Offers**: CTC break-up from the payroll salary structure when payroll is set up (otherwise a standard split); band from the job or requisition; outside the band the offer goes for approval (`recruitment.offer`, HR admin), inside it is approved at once; the offer letter is generated from the Letters template and sent for e-signature; the candidate's page shows the letter and break-up, signs (email code) and accepts with a typed name, or declines with a reason; expiry, withdraw, HR "accept on behalf" for paper signatures; status and history.
  - **Offer → pre-boarding**: acceptance creates the employee record in the pre-joining stage from the offer and candidate (no retyping; a former employee is rehired on the same record), files the CV and signed offer in their documents, marks the application hired, starts the referral bonus clock and starts onboarding.
  - **Referrals**: employees refer to open jobs (with CV) and follow the stage; bonus per job or default, due after a qualifying period if the hire is still active, approved and marked paid by HR.
  - **Privacy (DPDP)**: consent text, time and IP stored; retention period in settings (separate for the talent pool); purge (periodic job and on request) deletes names, contact details, CV, answers, notes and referee details, keeping an anonymous row so reports still count.
  - **Reports**: funnel, source effectiveness, time to hire, offer acceptance with decline reasons, days each job has been open.
- **Onboarding** (`modules/onboarding`): flows chosen by audience rules and priority, each with pre-joining and day-one stages, checklist tasks for HR, IT, admin, manager, buddy, the joiner or a named person (through the tasks module, due dates relative to joining), portal sections, required documents, induction sessions, courses and statutory forms.
  - **Pre-joining portal** (`/join/{token}`, expiring link that HR can switch off): personal, contact and addresses, family, nominees (shares total 100% per kind), bank, PAN/Aadhaar/UAN/ESI/passport (validated), previous employment with this year's income and TDS, tax regime, education, emergency contact and document uploads; completeness, submit and scheduled reminders.
  - **HR review field by field** (accept or send back with a remark; the joiner is emailed and sees it), accept all, and **write accepted details to the employee record** (personal, addresses, statutory IDs, family, nominees, bank, previous employment, education, emergency contact, documents; tax regime and previous income into payroll's tax declaration when present), each item written once.
  - **Appointment letter is mandatory** (OSH Code): generated with the offer's salary; onboarding can't be completed without it. **Statutory joining forms** as PDFs from the record: EPF Form 2 (revised), Form 11, ESI declaration, gratuity Form F, filed in the employee's documents.
  - Asset allocation from the register and account requests (email, laptop, access, ID card, desk, SIM as IT/admin tasks); induction sessions; enrolment in learning courses on day one.
  - **Day one** (automatic on the joining date or by HR): pre-joining → probation (or confirmed), day-one tasks, probation record, welcome email and a Buzz post, manager and buddy notified, 30/60/90-day check-ins (rating and notes; concerns alert HR; reminders).
  - Dashboard, board, timeline with time per stage and the longest stage; complete ("close anyway" for open tasks) and cancel (a joiner who never joins is archived).
- **Probation** (`modules/probation`): policies by audience with duration in days or months, review start N days before the end, extension rules (allowed, length, maximum count, maximum total months), multi-level reviewers (manager, skip-level, department head, role, named person) and feedback givers, auto-confirm and confirmation letter switches.
  - Records for everyone on probation; **reviews start automatically** N days before the end; levels open one after another; one batch reminder a day per reviewer.
  - Outcomes: **confirm** (confirmation date written to the employee record, stage confirmed, confirmation letter), **extend** within the limits (refused beyond count or total), **terminate** (starts a termination exit with the settlement clock), **on hold** with a reason; end-date override with a logged reason; cancel; bulk start review/confirm/remind/cancel; pending reviews per reviewer.
- **Exits** (`modules/offboarding`): resignation, termination, retrenchment, lay-off, end of contract, retirement, death and absconding, each with its own flow (audience, clearances, letters, exit interview, who may raise it, approval) seeded per company.
  - **Resignation** by the employee (reason from the exit reason master, proposed last day, notice preview) with the acceptance workflow (`offboarding.resignation`: manager, then HR) and **withdrawal before acceptance**; managers raise other exits for their team (`offboarding.exit`, HR approval); HR raises any type.
  - **Notice**: required days, served and shortfall from the dates; buy-out, waiver, recovery or pay in lieu with the amount from the payroll salary (monthly ÷ 30).
  - **Retrenchment** compensation (15 days' average pay per completed year, more than 6 months counting as a year, ÷ 26), re-skilling fund (15 days), notice pay and the government-permission warning at the headcount threshold; **lay-off** compensation (configurable % per laid-off day); **retirement** started automatically by age; **death** shows the nominees.
  - **Handover** items; delegate the leaver's pending approvals (reassigned now plus a delegation) and open tasks to a colleague.
  - **Clearances** from the departments in the flow (manager, IT, admin, assets, finance, HR) with owners, due dates relative to the last day (recalculated when it changes), item checklists, recovery amounts and remarks; the asset clearance lists outstanding assets and can't be cleared while they are out unless a recovery is recorded; delayed-clearance list and reminders.
  - **Exit interview** and **analytics** (averages by area, rejoin split, eNPS, reasons, exits by type).
  - **Full and final settlement handed to payroll** (`modules.payroll.settlement.create/update` with notice, recovery, pay-in-lieu and lay-off inputs) when the last clearance is done or on the last day, with the **2-working-day deadline** on the employee's work calendar as a countdown; payroll's status and net shown; leave encashment preview.
  - **Disabled and archived**: on completion (automatic the day after the last day, or by HR) the employee becomes exited (sign-in disabled, licence freed), letters are issued, the record is **archived, never deleted**, HR is reminded to update the PF exit date; **rehire eligibility** flag.
  - **Alumni portal** (`/alumni/{token}`) for a configurable period: letters, payslips, Form 16, career timeline and HR contact.

### Pages (web)
Hiring: `/dashboard/recruitment`, `/dashboard/recruitment/pipeline?job=`, `/dashboard/recruitment/candidates` (table and kanban, profile modal), `/dashboard/recruitment/interviews` (calendar and scorecards), `/dashboard/recruitment/offers`, `/dashboard/recruitment/requisitions`, `/dashboard/recruitment/referrals`, `/dashboard/recruitment/reports`, `/dashboard/recruitment/settings`. Public: `/careers`, `/careers/{slug}`, `/offer/{token}`, `/reference/{token}`.
Onboarding: `/dashboard/onboarding`, `/dashboard/onboarding/settings`; public `/join/{token}`. Probation: `/dashboard/probation` (board, table with bulk actions, reviewer view), `/dashboard/probation/settings`. Exits: `/dashboard/exits`, `/dashboard/exits/clearances`, `/dashboard/exits/interviews`, `/dashboard/exits/me`, `/dashboard/exits/settings`; public `/alumni/{token}`.

### Endpoints
`/recruitment/*`, `/public/careers/{slug}` (+ jobs, apply, feed.xml, feed.json), `/public/references/{token}`, `/public/offers/{token}`; `/onboarding/*`, `/public/joining/{token}`; `/probation/*`; `/exits/*`, `/public/alumni/{token}`.

### Tables
recruitment_settings, requisitions, jobs, candidates, applications, interview_kits, interviews, interview_scorecards, reference_checks, offers, referrals, onboarding_settings, onboarding_flows, onboardings, onboarding_checkins, probation_policies, probations, probation_reviews, offboarding_settings, exit_flows, exits, exit_clearances, exit_handovers, exit_interviews. All with company_id, an index and forced RLS (migration `tl1a2b3c4d5e`, from `w8x9y0z1a2b3`; works on an empty database in any order).

### Permissions, masters, numbering, jobs
- Permissions (from `modules/recruitment/hooks.py`, for HR and HR admin): `talent.offers.manage`, `talent.onboarding.manage`, `talent.probation.manage`, `talent.exits.manage`; hiring uses `talent.hiring.manage`.
- Masters: candidate rejection reason, offer decline reason, clearance department (exit reasons use `exit_reason`). Number series: requisition, job, offer, exit.
- Approval types: `recruitment.requisition`, `recruitment.offer`, `offboarding.resignation`, `offboarding.exit`.
- Periodic jobs: interview reminders, follow-ups, offer expiry, referral bonus, candidate purge; onboarding daily; probation daily; offboarding daily.
- Defaults per company: interview kits, two onboarding flows, two probation policies, one exit flow per type.

### Tests
`tests/test_talent_lifecycle.py` (15): screening score and explanation, CV parsing, pipeline stage moves and ordering, interviews and scorecards with evaluation and override, offer outside band needs approval, offer → e-sign → accept → pre-boarding employee and onboarding (portal, send back, accept all, written to record, completion blocked without appointment letter), probation auto review and extension limits and confirmation date, hold needs a reason, resignation → withdraw → accept → clearances → settlement handoff with the 2-working-day deadline, login disabled and record archived at exit with alumni access, retrenchment compensation, candidate purge, careers apply with consent and feed, requisition approval then job, tenant isolation (API and RLS). Full suite green on a fresh database.

### Decisions
- The four modules share small helpers in `modules/recruitment/common.py`.
- Columns pointing at tables of other migration branches (letters, e-sign requests, stored files, locations) are plain integers without a foreign key.
- A candidate's email becomes the new employee's work email; HR can change it.
- The offer page uses the documents module's public e-sign endpoints; acceptance needs the signature when e-sign is on.
- The settlement deadline lives on the exit (visible even before payroll can prepare the statement) and payroll's settlement is created through its service.
- Login is disabled by moving the employee to exited and the record is archived; alumni access is a separate expiring link.
- Purged candidates keep an anonymous row so reports keep their counts.

### Demo data
3 open jobs with 20 candidates across stages, interviews with scorecards, 2 offers (one sent, one accepted), 2 people onboarding, 3 on probation (one under review), 2 exits in progress (resignation and retirement).

### Unfinished / next
- Naukri and LinkedIn connectors need partner accounts to be verified for real.
- No candidate self-scheduling of interview slots; WhatsApp reminders not wired.
- Referral bonus is approved for payroll but not yet added to a pay run automatically.
- Not clicked through in the browser: signing and accepting an offer from the candidate page (covered by the API test), the reference form with a real link, the alumni page with a real link.

## Face attendance: face check at the clock-in kiosk, selfie match on the web

### What is built
- **Face check in the browser** with the open-source library `@vladmandic/human` (3.3.6). Its models (face detector, face mesh, iris, face description, anti-spoofing, liveness; about 13 MB) are copied by `frontend/scripts/copy-face-models.mjs` (runs on `npm install`) to `frontend/public/models/face` and served by the app itself, so no request leaves the app at run time. The engine (`src/components/face_attendance/face-engine.ts`) finds exactly one face, checks its size, runs the liveness challenge (blink, or head turn to one side and back, worked out from the face mesh), averages the anti-spoofing ("real") and liveness scores over the check, and makes face templates (1024 numbers each, L2-normalised).
- **Matching**: cosine similarity; a match needs `1 - similarity <= threshold` (default 0.40) and, on a kiosk (1:N), the best person must be ahead of the next by the margin (default 0.04). The same maths runs on the tablet (`identify`) and on the server (`modules/face_attendance/matching.py`).
- **Enrolment** (self-service in My attendance → Face sign-in, or HR in person on the Enrolled faces tab): the DPDP consent notice (editable, versioned; the version agreed to and the text are stored with the enrolment) must be accepted, then 4 samples are taken after a liveness challenge. Server checks: 3–5 samples, same size, all of one face (sample agreement), and the face is not already enrolled for another person (blocked and audited). Only templates are stored, encrypted with Fernet under a key derived per company; they are never returned to the web app (only to a kiosk for matching). Enrolling again replaces the old templates.
- **Withdrawal**: the employee withdraws in Face sign-in, or HR deletes face data with a reason (written request). Templates are deleted at once (status withdrawn/removed, the consent record kept without face data), the person's face check snapshots are deleted, and an audit event is written. People who leave: templates deleted by the periodic job.
- **Kiosk** (`/clock-in/{token}`, existing page extended): when HR sets a kiosk to *Face first* or *Face required*, the kiosk downloads the templates of the people at its location (`/gallery`, with a version so an unchanged list is not downloaded again), keeps them in **IndexedDB**, refreshes every `refresh_minutes` (default 30) and when back online. It recognises faces offline. Punches go to `/face-attendance/kiosk/{token}/punch`; offline they use the kiosk's existing queue (localStorage) and are sent with the PIN items through `/face-attendance/kiosk/{token}/sync`. The server checks every face punch again against the stored templates with the threshold in force, plus the liveness evidence, before recording the punch (source kiosk, note "face match NN%") and a face event with the score.
- **Fallback**: "Use employee ID and PIN" is always offered in *Face first*; in *Face required* only after a failed face check (the old PIN endpoint refuses punches at such kiosks). PIN punches after the face check are recorded through the face endpoint so failures are counted. After `flag_after_failures` failed tries the kiosk switches to PIN; a person who walks away is logged as "face not recognised".
- **Flags for HR review**: PIN at a face-required kiosk; PIN after N failed tries; enrolled person who needed the PIN N times in 7 days; liveness failed (possible photo/screen); kiosk matched but the server did not agree; faces not recognised several times in a row; repeated failed selfie matches. Review page: accept (optionally add the punch for a check without one) or reject with a note (the punch is kept but marked ignored and the day recomputed; refused in a locked pay period). Viewing a snapshot is audited.
- **Snapshots**: never / flagged only (default) / every check; small JPEG data URL (≤ ~90 KB); deleted after `snapshot_retention_days` (default 30) by the periodic job.
- **Web and phone clock-in with a selfie match**: setting off / chosen locations / everyone. When required, the Clock in button on My attendance opens the face check; `/face-attendance/me/clock` checks the selfie template 1:1 against the person's own templates and then records the punch through the attendance service (geo-fence, IP and selfie rules still apply). `/attendance/me/clock` refuses direct punches for those people. People not enrolled: allowed without a face check by default (consent must be free), or blocked by setting.
- **Settings** (Face attendance → Settings): company switch, threshold, margin, liveness level (off / basic: anti-spoofing + liveness score + face size / standard: + blink or head turn / strict: both challenges, higher scores), snapshots and retention, review counts, kiosk refresh, locations where the face check is allowed and where it is required, web selfie match, self-enrolment on/off, consent notice. Per kiosk: off / face first / required, location, own threshold and liveness level.

### Pages (web)
- `/dashboard/face-attendance` (Time → Face attendance, permission `time.face.manage`): tabs Review, Enrolled faces, Kiosks, Settings; all details and forms in modals.
- My attendance: "Face sign-in" button (set up with consent, see status, withdraw); Clock in opens the face check when the selfie match is required. Home "Today" card sends people to the attendance page in that case.
- `/clock-in/{token}`: face check panel with PIN fallback.
- Help: `src/components/help/topics/face-attendance.ts` (5 guides; not yet added to help-index).

### Endpoints (`/api/v1/face-attendance`)
Admin: `GET summary`, `GET/PUT settings`, `GET kiosks`, `PUT kiosks/{uuid}`, `GET enrolments`, `POST enrolments/{employee_uuid}` (HR enrol), `POST enrolments/{employee_uuid}/remove`, `GET events`, `GET events/{uuid}/snapshot`, `POST events/{uuid}/review`.
Self: `GET me`, `POST me/enrol`, `POST me/withdraw`, `POST me/clock`.
Kiosk (secret link, office IP rule applies): `GET kiosk/{token}/config`, `GET kiosk/{token}/gallery?since=`, `POST kiosk/{token}/punch` (method face | pin | miss), `POST kiosk/{token}/sync`.

### Tables
`face_settings`, `face_kiosk_settings`, `face_enrolments`, `face_events`: company_id, company-first indexes, forced RLS (migration `fa1c3e5a7b9d`, from `ig1a2b3c4d5e`). Enrolments, events and settings are not audited row by row (templates and photos must never reach the audit log); enrol, duplicate blocked, consent withdrawn, data removed, settings changed, templates downloaded by a kiosk, spoof suspected, snapshot viewed and review decisions are audit events.

### Changes outside the module (small)
- `modules/attendance/kiosk.py`: `punch()` split into PIN check + `record()` (used for face punches); PIN punches refused at face-required kiosks unless they come through the face fallback.
- `modules/attendance/service.py`: `clock()` refuses web punches that need a face check unless they come through `/face-attendance/me/clock`; today card returns `settings.face_required`.
- `modules/roles/models.py`: permission `time.face.manage` (HR admin through "all", and HR).
- Web: `src/app/clock-in/[token]/page.tsx`, `src/app/dashboard/attendance/page.tsx`, `src/components/home/today-card.tsx`, `src/lib/hr-nav.ts`, `package.json` (dependency and postinstall), `.gitignore` (models folder).

### Tests
`tests/test_face_attendance.py` (10): cosine similarity/distance, threshold edge, 1:N identify with margin and ambiguity, template validation and sample agreement, per-company encryption; kiosk gallery download and version, face punch accepted, kiosk/server disagreement rejected and flagged, liveness failure flagged with snapshot, stricter kiosk threshold rejects a weak match, offline replay is a duplicate; required kiosk: plain PIN refused, PIN fallback flagged, wrong PIN refused, review reject ignores the punch, repeated misses flagged; consent required and versioned, templates never returned, duplicate face blocked, web clock needs the selfie match, withdrawal deletes templates and snapshots and is audited, kiosk no longer gets them; employee cannot use admin endpoints; tenant isolation through the API, the kiosk and RLS.

### Decisions
- Library: `@vladmandic/human` rather than face-api.js, because it ships anti-spoofing and liveness models (the "texture" check) next to detection and templates. Its templates have 1024 numbers (the brief's 128/512 were examples); any size from 64 to 2048 is accepted and must match the enrolment.
- Models are not committed (13 MB); `npm install` copies them. Deployments must run `npm install`/`npm ci` (they do).
- Threshold is a cosine distance; screens show similarity as a percentage. The default 0.40 is a starting point; tune per site after watching the review list.
- Liveness runs in the browser, so its scores are reported by the client; the server enforces the level's minimums and the challenge count, and every match is re-checked on the server. A tampered tablet could still lie about liveness: keep kiosk links secret, use the office-network rule and the review list.
- Kiosks keep templates in IndexedDB (not encrypted on the device; the link token is in the address). Use a locked-down tablet. Offline queue items carry probe templates until sent.
- Face settings are one row per company (not effective-dated): they are operational settings, not pay rules; every change is an audit event with old and new values.
- Kiosk location is stored in `face_kiosk_settings` (the kiosk table has only a free-text note). A kiosk without a location gets everyone's templates; employees without a location are only on such kiosks.
- DPDP: consent is per person, free (PIN always remains; web check not forced on people not enrolled unless HR chooses "block"), specific notice stored with the version, withdrawal as easy as giving it, data deleted on withdrawal and on exit, snapshots time-limited.

### Unfinished / next
- The Expo mobile app has no clock-in yet; phone users use the web app (the selfie match works in mobile browsers).
- Help guides need sample data recorded (`scripts/help-record.mjs`) and adding to help-index by the coordinator.
- Not tried with a real camera in this environment (headless Chrome with a fake camera): matching quality and the default threshold should be checked on real tablets.

## Attendance devices: every way to connect (vendor API, database, ADMS, LAN, connector)

### What is built
- **Nine connections** on the Devices page, chosen in a step-by-step modal (Device, Connection, Settings, Fields and test / File columns):
  ADMS cloud push, our push API, vendor push (any JSON, Hikvision), vendor software API, database read, LAN pull (ZKTeco 4370), FTP/SFTP folder, email attachments, file upload. Everything ends in `devices.ingest()` (duplicates ignored, unmatched counted, days recomputed).
- **Vendor API pull** (`device_api.py`): GET/POST, query params, headers, body templates with `{from}` `{to}` `{from_utc}` `{to_utc}` (strftime formats), `{page}` `{offset}` `{size}` `{serial}` `{username}` `{password}` `{since_id}` `{tzoffset}`; sign-in none / API key (header or query) / Basic / Bearer / OAuth2 client credentials / "sign in first" (token from JSON path or header) / credentials in the body; paging by page number, offset, or next link/token; first-run look-back, overlap, largest date range per request (catches up range by range). Presets: eSSL eTimeTrackLite (SOAP WebAPIService, tab-separated text inside XML), Matrix COSEC (event-ta API), Realtime eTimeOffice, ZKTeco ZKBioTime (JWT, next-page links), Suprema BioStar 2 (bs-session-id, offset paging), Custom.
- **Field mapping** (`device_mapping.py`): JSON (dot paths, auto-found record list, "." = the whole body is one event), XML (namespaces ignored, DOCTYPE/entities refused), delimited text (also inside an XML/JSON field); employee field + match by enrolment or employee ID; one time field or date + time; format (`strftime`, iso, epoch, epoch_ms, .NET `/Date()/`); timezone override; direction field with in/out value lists; reader/door field matched to our devices by serial number or name (punch gets that device); increasing id. Guesses fields from column names.
- **Database read** (`device_db.py`): SQL Server (pymssql), MySQL/MariaDB (pymysql), PostgreSQL (psycopg2), Oracle (oracledb) through SQLAlchemy URLs; MS Access on the connector (pyodbc). Table/view + mapped columns, or a custom query with `:since`; incremental by punch time (`>=` last, duplicates dropped) or by id (`>`); monthly tables (`DeviceLogs_{M}_{YYYY}`, missing months skipped); batch limit with a quick follow-up read; read-only (one SELECT/WITH, write keywords and `EXEC`/`xp_` refused, rolled-back transaction, `SET TRANSACTION READ ONLY` where supported). Test shows the table's first rows, the incremental rows and how they read as punches; a missing table lists the tables and views found. Presets: Matrix COSEC (`Mx_ATDEventTrn`: UserID, Edatetime, EntryExitType, MID), eSSL eTimeTrackLite (`DeviceLogs_{M}_{YYYY}`: UserId, LogDate, Direction, DeviceId, DeviceLogId), ZKBioTime (`iclock_transaction`: emp_code, punch_time, punch_state, terminal_sn, id), ZKTime/ZKAccess Access file (CHECKINOUT join USERINFO on Badgenumber), Custom.
- **ADMS / iclock** (`device_adms.py`, mounted at the server root): `GET/POST /iclock/cdata` (handshake with stamps and our timezone; ATTLOG punches; OPERLOG user list), `GET /iclock/getrequest` (queued commands, device INFO), `POST /iclock/devicecmd` (results), `/iclock/ping`, plus the `.aspx` paths of older eSSL firmware. Device found by serial number (unique across companies, partial unique index); optional allowed IPs; device state keys ignored unless "trust in/out key" is on (most sites run every punch as state 0). Online = called in the last 5 minutes. Commands from the page: fetch the last 31 days again (`DATA QUERY ATTLOG`), ask for device details (`INFO`).
- **Vendor push** (`POST /attendance/devices/hook/{key}`): JSON body, form or multipart (Hikvision posts the event JSON next to a photo); events without a person (heartbeats, door events) skipped; the last post is kept as a sample for the mapping screen.
- **LAN pull** (`device_zk.py`, optional `pyzk`): reads attendance newer than the last read, device info, in/out state optional; on our server or the connector.
- **Email attachments** (`device_email.py`): IMAP (SSL), folder, sender and subject filters, attachment name pattern, messages remembered by id; the file column mapping as for uploads.
- **Office connector** (`backend/connector/`, Python, its own small program): downloads its settings with the device key (`GET /attendance/connector/config`), reads databases (all engines incl. MS Access) and ZKTeco machines, keeps punches in a local SQLite queue (retries with back-off up to an hour, bad punches dropped one by one), sends them to `/attendance/devices/push` (now accepts a `terminal`), reports a heartbeat every minute (`POST /attendance/connector/heartbeat`: version, computer, queue, last error, read position), reloads settings when they change. Commands `run`, `once`, `test`, `status`, `add`. README in plain words, `connector.spec` (PyInstaller, one .exe), `install-windows.bat` (start-up task as SYSTEM), `uninstall-windows.bat`, systemd unit. The page shows connector online/offline, computer, waiting punches and its last problem; "Connector settings file" downloads `connector.json` (makes a new key; keys are only stored hashed).
- **Server-side pulls** run from the shared periodic job (`attendance.device_pull`, every 15 minutes, each device on its own interval; a full batch schedules a read a minute later). "Pull now" on the device works for API, database, LAN and email devices run by the server.
- Secrets in the new settings are encrypted (Fernet, key from the app secret) and shown masked; an empty or masked field keeps the saved one.
- Server-side API/database/LAN/IMAP addresses on a private, loopback or link-local network are refused in production (they belong on the connector); `DEVICE_ALLOW_PRIVATE_HOSTS=1` allows them. SQLite databases are only accepted outside production (tests).

### Pages
- `/dashboard/devices`: status column (Online / Offline / Waiting / Problem / Active / Off), "Office connector" under the connection, step-by-step add/edit modal with presets, sample rows and a punch preview, detail modal per connection (ADMS address and port, device IP, firmware, last handshake, commands; connector status and settings file; push / vendor push address; read position and last problem; pull now), sync log with the new kinds.
- Help guide: `frontend/src/components/help/topics/devices.ts` (topic `devices-connect`; to be added to help-index by the coordinator).

### Endpoints
- `GET /attendance/device-presets`, `POST /attendance/devices/test` (nothing saved), `POST /attendance/devices/{uuid}/command`, `GET /attendance/devices/{uuid}/device-users`, `POST /attendance/devices/{uuid}/connector-file`, `GET /attendance/connector/config`, `POST /attendance/connector/heartbeat`, `POST /attendance/devices/hook/{key}`; root `/iclock/cdata|getrequest|devicecmd|ping` (+ `.aspx`). `POST /attendance/devices/{uuid}/pull` and `POST /attendance/devices/push` extended. All admin endpoints need `time.attendance.settings`.

### Tables
- `attendance_devices`: `source_config` (JSONB, secrets encrypted inside), `runs_on` (server | connector), `sync_state` (read position, ADMS stamps, queued commands, reported users, last error, last vendor-push sample), `connector_seen_at`, `connector_info`; unique index on `serial_number` for ADMS devices. Migration `dv1a2b3c4d5e` (down_revision `ig1a2b3c4d5e`). No new tables. Punch sources add `api` and `database` (labels added on the attendance pages).

### Tests
- `tests/test_attendance_device_sources.py` (21): mapping JSON/XML/text/epoch/date+time/direction values/timezone, XML entity refusal, secrets, ADMS parsing and commands, incremental DB reads by time and by id with batches (SQLite), custom query with `:since`, read-only checks, monthly tables, missing driver message, API sign-in + next-link paging + window, Basic + page numbers + 401 message, OAuth2 + SOAP escaping, capped window, ZK pull with a fake device.
- `tests/test_attendance_device_connections.py` (8): ADMS end to end (handshake, ATTLOG, duplicates, stamp, OPERLOG users, INFO, commands, online status, serial unique across companies, switched-off device refused), trusted states, database device test → save → pull → incremental pull with door matching (SQLite as the vendor DB), bad query, missing table list, API device pull (mock HTTP), Hikvision multipart push and wrong key, connector config/heartbeat/push/settings file/key rotation, permissions, tenant isolation throughout.
- `tests/test_attendance_connector.py` (4): connector reader by time and id, offline queue with retry back-off and heartbeat, fake ZK device, a full connector cycle against the real API.
- Existing `tests/test_attendance.py` still passes.

### Decisions
- The connector lives in `backend/connector/` (same repo for now; it has no imports from the backend and can move to its own repo). It runs as a Windows start-up task (Task Scheduler, SYSTEM) rather than a Windows service, which needs no extra packages and works from a single PyInstaller .exe.
- The connector reads databases and ZKTeco machines; vendor web APIs are read by our server only (most vendor APIs that matter are cloud or published).
- ADMS punches ignore the device's in/out state by default (first in, next out) because most installations leave every punch at state 0, which would leave days open.
- ADMS devices are recognised by serial number only (the protocol has no key); allowed IPs can be set per device.
- Driver packages are optional (`requirements-devices.txt`); the page says when a driver is missing on the server and suggests the connector.
- Shared files changed: `main.py` and `modules/__init__.py` (a small `root_routers` hook so a module can mount routes at the server root), `modules/attendance/devices.py`, `router.py`, `hooks.py`, `models.py`; web `attendance/page.tsx` and `admin-day-modal.tsx` (two source labels).

### Deployment notes
- Devices call `http(s)://<api host>:<port>/iclock/...`: the reverse proxy must send `/iclock/` to the API, not the web app. Many older machines only speak plain HTTP; offering an HTTP-only device address (or port) is a hosting decision for the coordinator.
- Install `requirements-devices.txt` on the API server to read SQL Server, MySQL, Oracle and ZKTeco machines from the server.

### Unfinished / next
- Not tried against real eSSL, COSEC, Realtime, BioTime, BioStar or Hikvision systems here: the presets follow the vendors' published formats and the test step is where field names are confirmed. COSEC column `Edatetime` and the eSSL web API's text layout in particular should be checked on a live install.
- Not added (listed for later): Hikvision ISAPI pull (needs digest sign-in) and Hikvision/Dahua via the connector; ZKTeco BioTime/ZKBioAccess "push to third party" webhooks; Matrix COSEC event push; Suprema BioStar 2 webhooks; Google Drive / OneDrive / SharePoint folder pickup; vendor API reads through the office connector; ADMS user/fingerprint sync back to devices (adding staff to machines from the HR system); ESSL "eBioServer" cloud; ZKTeco "PUSH 3.0" registry endpoints (`/iclock/registry`, `/iclock/push`) for the newest firmware; a signed Windows installer for the connector.
- Help guide needs sample data recorded and adding to help-index.

## Travel and recycle bin

### Travel (backend `modules/travel`)
Business travel from plan to settlement.
- **Trips**: purpose, domestic or international, dates, several journeys (flight, train, bus, cab, own vehicle, with class, places, date, preferred time, fare or distance), hotel stays (city, check-in/out, budget a night), project from timesheets with a billable switch, travel advance, notes. Drafts can be edited; sending runs the policy check.
- **Travel policy** (`travel_policies`): versioned, effective-dated (a change is a new version from a date; a trip is checked against the version in force on its start date). Grade bands (first match wins, one band for everyone else): modes and classes allowed, hotel limit and daily allowance per city tier X / Y / Z / international, how many days ahead to ask (domestic, international); advance limit as % of the estimate; X and Y city lists (HRA classification, with old spellings such as Bangalore, Bombay, Gurgaon); expense category codes used at settlement. Defaults seeded per company (version 1 from 2000-01-01).
- **Policy check** (`modules/travel/policy.py`, pure): mode not allowed, class above grade, hotel above the tier limit, journey outside trip dates, short notice, advance above limit. Estimate = fares + hotel nights + daily allowance (days × highest tier rate of the destinations). Outside-policy trips need a reason before sending.
- **Approval**: request type `travel.request` (variables `request.amount`, `request.advance`, `request.outside_policy`, `request.flags`, `request.international`, `request.billable`); default flow reporting manager, then HR when outside policy or international.
- **Advance**: on approval an approved expense advance is opened in Expenses (`expenses.service.create_advance`); the travel desk (or finance in Expenses) pays it; it is withdrawn if the trip is cancelled before payment.
- **Travel desk**: records each journey/stay booking (booked / not yet / cancelled, paid by company or traveller, operator or hotel, PNR, cost, ticket or voucher file up to 2 MB, PDF or photo); a trip becomes Booked when nothing is open (or "Mark all booked"); desk cancel; pay advance; record an unspent advance given back.
- **Settle**: after the trip has started the traveller clicks Settle trip; a draft expense claim is made with the journeys and hotels the traveller paid (company-paid bookings are never claimed; own vehicle with distance uses the mileage category), one line per hotel night and per day of daily allowance (new `PERDIEM` category "Daily allowance (travel)" seeded per company), linked to the advance. The employee attaches bills and sends it through the normal expense flow; on approval Expenses sets the claim off against the advance and pays the rest with salary. The trip follows the claim (settling → settled; a withdrawn claim lets the trip be settled again). A trip with nothing to claim is settled at once.
- **Reports**: spend by department (at submission), project, employee, mode or type (company bookings + approved claims; estimates for open trips; billable share; count outside policy) and policy breaches by kind with the reasons.

API `/api/v1/travel`: `GET access`, `GET options`, `GET projects`, `GET/POST policy`, `GET policy/for-me`, `GET trips?scope=mine|team|all&status=&type=&outside_policy=&needs_booking=&q=`, `POST trips/preview`, `POST trips`, `GET/PUT trips/{uuid}`, `POST trips/{uuid}/submit|cancel|settle`, `POST trips/{uuid}/bookings/{leg|stay}/{item}`, `GET .../file`, `POST trips/{uuid}/mark-booked|desk-cancel|pay-advance|return-advance`, `GET summary`, `GET reports/spend`, `GET reports/breaches`.
Permissions: `payroll.travel.manage` (travel desk, every trip, reports), `payroll.travel.policy` (policy); HR admin (all) and HR by default. Managers see their direct reports' trips; approvers see trips they have a task on.
Tables: `travel_policies`, `travel_requests`, `travel_legs`, `travel_stays` (company_id, company-first indexes, forced RLS; migration `tv1a2b3c4d5e` from `ig1a2b3c4d5e`).

Web: Home → **My trips** `/dashboard/travel` (trips + "My travel policy" tab; plan modal with live estimate and warnings; detail modal with tickets, advance, settlement claim) and **Team trips** `/dashboard/travel/team`; Payroll → **Travel desk** `/dashboard/travel/desk` (To book, All trips, Reports) and **Travel policy** `/dashboard/travel/policy` (bands view, versions, full-screen "Change policy" modal). Components in `src/components/travel/trip-modals.tsx`.

### Recycle bin (core `core/recycle_bin.py`, backend `modules/recycle_bin`)
- **Registry**: models declare support with `recycle_bin.register(BinKind(key, label, group, model, flag, ...))`; flag is `archived_at`, `deleted_at`, `active` or `status` (with the values meaning "in the bin" and the value to restore to). Options: title/subtitle, purgeable + reason kept, extra filter, restore check / on-restore hook, fields that must stay unique, hidden fields.
- **Catalogue** (`modules/recycle_bin/catalogue.py`): 66 kinds registered (people: employees archived before joining, employee documents; organisation; lists, list values, custom fields and sections, number series, notification templates; folders, company files, policies, letter templates, letterheads; leave types, holiday calendars, work weeks, shifts, attendance rules, devices, kiosks, geofences, timesheet clients; salary components and structures, pay groups, expense categories, loan types, benefit plans; talent set-up; announcements (archived), buzz posts and comments (deleted), kudos values, grievance committees, help desk set-up; assets (retired); dashboards, reports, schedules, automation rules, webhooks, approval flows). `EXCLUDED` lists tables whose flag is not a deletion (companies, platform users, exits — employees archived through exits stay with exits —, old bank accounts, perquisites, statutory overrides, committee membership history, billing rates, delegations). Any other model with such a column is picked up automatically under "Other" (restorable, never purged until reviewed); a test keeps this complete.
- **Who and when**: from `archived_at`/`archived_by` or the audit log (last change of the flag). Records created already switched off (samples) are not shown.
- **Restore**: refused while anything the record points to is itself in the bin (generic, from the foreign keys: a department's parent, a value's list, a field's section, a file's folder…) or while a live record uses the same code; bulk restore reports failures per record.
- **Purge**: only for purgeable kinds, never for built-in rows, only after the retention period (default 90 days, 30–3650, per company in `recycle_bin_settings`), and only when no other row points to it (checked first, then a plain SQL DELETE so no ORM cascade can unlink anything). Kept for good: payroll, salary, tax and expense set-up, leave/attendance/shift rules, employee records and documents, legal entities, policies with acknowledgements, POSH committees, asset register, approval flows. Optional automatic purge (off by default) as a periodic job.
- **Audit**: `recycle_bin.restored` and `recycle_bin.purged` events; a purge stores a masked copy of the row.

API `/api/v1/recycle-bin`: `GET items?kind=&group=&who=&start=&end=&q=&purge=due|kept` (with facets), `GET items/{kind}/{uuid}` (fields, restore problems, references, history), `POST items/{kind}/{uuid}/restore|purge`, `POST restore` (several), `GET kinds`, `GET/PUT settings`. Permissions `settings.recycle_bin.manage` (see, restore) and `settings.recycle_bin.purge` (remove for good, settings); HR admin only by default.
Web: Settings → **Recycle bin** `/dashboard/settings/recycle-bin` (filters by kind, person, removal state and dates, bulk restore, detail modal, settings modal).

### Changes outside the modules (small)
- `modules/expenses/service.py`: helpers `create_advance`, `advance_by_id`, `cancel_advance`, `claim_by_id`, `claims_by_ids`, `advances_by_ids`; `save_claim` leaves trip dates alone when not sent.
- `modules/expenses/router.py`: editing a claim keeps its advance link and trip dates when the form does not send them (the claim form never sent them, so editing a claim used to drop its advance).
- `frontend/src/lib/hr-nav.ts`: My trips, Team trips, Travel desk, Travel policy, Recycle bin.
- Help: `frontend/src/components/help/topics/travel-bin.ts` (5 guides; to be added to help-index by the coordinator).

### Tests
`tests/test_travel.py` (13): city tiers and aliases, band by grade, within-policy trip and per diem by destination tier, class/mode/hotel/lead-time/advance breaches, dates outside the trip, policy validation; effective-dated versions; outside-policy trip needs a reason and goes to approval, others cannot see it; full flow (approval opens the advance, desk bookings with ticket file, employees blocked from the desk, advance paid, settlement claim without company-paid flight with 6 lines, claim edited through the expense form keeps the advance, approval sets off the advance, trip settled, actual cost, spend and breach reports); unspent advance returned; cancel withdraws the advance; manager sees and approves team trips; tenant isolation (API and RLS).
`tests/test_recycle_bin.py` (8): deleted department listed with who/when, detail, restore audited; restore blocked until the parent is back (and bulk restore partial); restore blocked by a live duplicate code; purge only after retention, audited with a copy, retention limits; purge blocked while referenced; audit-relevant kinds never purged; every soft-deletable model registered or excluded; permissions and tenant isolation.

### Decisions
- Travel lives under Home (my trips, team trips) and Payroll (desk, policy) because its money flows through Expenses and salary.
- The advance is an Expenses advance (not a loan): Expenses already sets claims off against advances and final settlement recovers what is left; in-service recovery of a leftover advance from salary is recorded as "given back" by the desk rather than as a payroll deduction.
- Daily allowance is per day at the highest tier among the destinations; hotel lines are one per night so the per-day expense limits apply as they do for any claim.
- Recycle bin settings are a small table of their own (not `companies.config`) so the bin never writes the company row.
- Purge is allowed for set-up records only; anything a statutory register, payroll, tax or POSH record may rely on is "kept for good" and can only be restored.

### Unfinished / next
- Visa/forex for international trips, booking-tool integrations and group trips are not built.
- Help guides need sample data recorded and adding to help-index by the coordinator.
- Tests were run against a copy of the backend without `modules/custom_forms` (another builder's module had no migration yet at the time).

## Positions, succession and the internal job marketplace

Built as one backend module, `modules/positions` (positions, seats, headcount plans and position requests in `service.py`; succession, nine-box, bench strength and talent pools in `succession.py`; internal postings and applications in `marketplace.py`), with web pages under Talent.

### What is built
- **Positions** (switched on per company in Positions → Settings). A position is an approved seat: code from the `position` number series (or typed), title, designation, department, location, grade, cost centre, reports-to position, status budgeted / approved / closed, seats (headcount), FTE per seat, yearly CTC salary band (defaults from the grade's CTC range), key-position flag and required skills with levels. Loops in the reports-to line are refused; a position can't be closed while people sit in it or open positions report to it; closed positions can be reopened.
- **Seat control.** `position_seats` records who sits in a position from and to a date (active, reserved for a future start, ended). A full position refuses new people ("No seats are free…") unless the company allows more people than seats; seats can't be cut below the people in them; only approved positions can be filled. Placing someone ends their previous seat the day before and records an effective-dated job change on their record (department, designation, location, grade, cost centre and the manager, i.e. the person in the reports-to position) through `employees.service.record_job_change`. Reserved seats start, and seats of people who have left are freed, by the periodic job `positions.seats`.
- **Hires and job changes pick a vacant position when positions are on:** the Add employee wizard (Job step) and the Record a job change modal show a vacant-position picker (required when "Ask for a position on every hire and job change" is on). Picking one fills the job fields; saving places the person in the seat.
- **Position chart** (`/dashboard/positions/chart`): the tree by reports-to line with incumbents, vacant seats, roll-up counts below each position, search, expand/collapse and a single-branch view.
- **Headcount plans** per department per Indian financial year (e.g. 2026-27), approved through the workflow engine (`positions.headcount_plan`: department head, then HR admin). Once approved, the seats of the department's budgeted and approved positions can't go above the approved number (switchable). A revision keeps the old limit in force until the new number is approved.
- **Position requests** (new / change / close) from managers or HR, numbered (`position_request` series) and approved through `positions.request` (department head, then HR); approval creates, changes or closes the position, applying the same seat and plan rules.
- **Hiring link:** "Raise hiring request" on a vacant position creates and submits a hiring request in recruitment (`recruitment.service.save_requisition` / `submit_requisition`) with title, department, designation, location, salary band and the reports-to incumbent as hiring manager; openings are limited to vacant seats not already covered by open requests. Requests raised are listed on the position.
- **Succession:** plans for key positions (one active plan per position; starting one marks the position as key) with plan manager, risk of loss and impact of loss (low/medium/high → urgent / watch / stable), notes and review date. Successors with readiness (ready now, 1-2 years, 3+ years), rank, strengths and gaps; the holder can't be their own successor. Development steps per successor: a course (enrols them through `learning.service.enrol`; progress from the enrolment), a goal (links one of their goals or adds a new individual goal through `performance.okr.create_objective`; progress from the goal), mentoring, project, stretch assignment or other.
- **Nine-box and bench strength:** each person's latest review rating (final, else calibrated, else proposed) and potential from performance, placed on the nine-box with the company's box names (`performance.calc.nine_box`). Bench strength per plan (strong / moderate / weak / none against configurable targets, default 1 ready-now and 2 in all), coverage (% of key positions with a ready-now successor), key positions without a plan, urgent gaps and a department breakdown.
- **Talent pools:** named groups (e.g. High potentials) with a manager and members; suggestions from the high-potential boxes (6, 8, 9) of the nine-box.
- **Internal job marketplace:** postings for jobs (on their own, from an approved hiring request, or from a vacant position) and gigs (weeks and hours a week), numbered (`internal_posting` series), with skills, openings, work mode, dates and hiring manager. Employees browse open postings with their own skills match score and apply; when the posting asks for consent and the person has a manager, the application waits for that manager in the approvals inbox (`positions.internal_application`). Rules: months in current role before applying for jobs (default 6), open applications at a time (default 3), one application per posting, hiring managers can't apply to their own posting. HR and the hiring manager shortlist, select, turn down or reconsider; "Move to the role" places the person in the posting's position (seat rules apply) or records a job change to the posting's department, designation, location and hiring manager; gigs are started without a job change. Postings become filled when all openings are moved. Suggested people (best skills match who have not applied) are shown on each posting.
- **Match score** = average over the posting's skills of min(person's level ÷ level asked, 1) as a percentage; a missing skill counts 0.
- Notifications to the people concerned (placed in a position, development step added, application news, both managers on a move). Audit: business events `position.closed`, `position.seat_assigned`, `succession.readiness_changed`, `marketplace.applied`, `marketplace.moved` (row changes are audited automatically).

### Pages (all under Talent)
`/dashboard/positions` (positions list with seats meter, detail modal with overview / people / history / hiring, add-edit modal, put someone in, raise hiring request, close/reopen; tabs Headcount plans, Requests, Settings), `/dashboard/positions/chart`, `/dashboard/succession` (Plans, Bench strength, Nine-box, Talent pools; plan detail with successors and development steps; pool detail), `/dashboard/internal-jobs` (Open now for everyone; All postings for HR or My postings for hiring managers; posting form and detail modals), `/dashboard/internal-jobs/my` (my applications with progress steps and withdraw). Shared pieces in `src/components/positions/`. Help guides in `src/components/help/topics/positions.ts` (5 topics; not yet added to `help-index.ts`).

### Endpoints
- `/positions`: `meta`, `settings` (GET/PUT), `summary`, `positions` (list with q/status/department/fill/critical, create), `positions/{uuid}` (GET/PUT), `positions/{uuid}/close`, `/reopen`, `/seats` (place someone), `/hiring-request`, `seats/{uuid}/release`, `tree`, `vacant` (picker), `employees/{id}/seat`, `headcount-plans` (list, create, PUT `{uuid}`, `{uuid}/submit`, `{uuid}/withdraw`), `requests` (list, create, `{uuid}/cancel`).
- `/succession`: `plans` (list, create, GET/PUT `{uuid}`, `{uuid}/archive`, `{uuid}/candidates`), `candidates/{uuid}` (PUT, `/remove`, `/actions`), `actions/{uuid}` (PUT), `bench`, `nine-box`, `employees/{id}/talent`, `options/courses`, `options/goals`, `pools` (list, create, GET/PUT `{uuid}`, `{uuid}/members` POST, `{uuid}/members/{member}` DELETE, `{uuid}/suggestions`).
- `/internal-jobs`: `postings` (list `scope=open|manage`, create), `postings/{uuid}` (GET/PUT, `/status`, `/applications`, `/suggestions`, `/apply`), `applications` (list), `applications/mine`, `applications/{uuid}/withdraw`, `applications/{uuid}/decision` (shortlist, select, reject, reconsider, move, assign), `requisitions` (approved hiring requests not yet posted).

### Tables
`position_settings`, `positions`, `position_seats`, `headcount_plans`, `position_requests`, `succession_plans`, `succession_candidates`, `development_actions`, `talent_pools`, `talent_pool_members`, `internal_postings`, `internal_applications` — all with company_id, a company-first index and forced RLS (migration `pos1a2b3c4d5`, down revision `ig1a2b3c4d5e`).

### Permissions, flows, series
- Permissions (Talent group): `talent.positions.view`, `talent.positions.manage`, `talent.succession.manage`, `talent.marketplace.manage`. HR admin and HR get all four; managers get `talent.positions.view`. Everyone can browse internal jobs and apply; placing people in seats is also open to `people.employees.manage` (for the hire and job-change forms).
- Approval types: `positions.request`, `positions.headcount_plan`, `positions.internal_application` (default flows seeded per company).
- Number series: `position` (POS-0001), `position_request` (PRQ/2627/0001), `internal_posting` (IJP-0001).
- Settings: use positions, require a position on hire/job change, sync job details from the position, allow overfill, enforce the headcount plan, marketplace on/off, manager consent by default, months in role, open applications, good-match threshold, bench targets.

### Demo data (development company)
Positions are built from the current reporting lines (28 positions, 25 people seated, spare seats in two teams, plus Data Engineer, HR Business Partner and Regional Sales Manager vacancies and a budgeted SRE), salary bands by grade, approved headcount plans for this year and a draft for next year, a pending "Financial Analyst" request in the approvals inbox, four succession plans (VP Engineering, Head of Sales, Finance Controller, HR Manager) with 8 successors and development steps, two talent pools, two internal jobs and two gigs with six applications (one waiting for a manager's consent; Priya Sharma has applied for the Power BI gig).

### Tests
`tests/test_positions.py` (10): seat control and job sync (full position refused, seats can't drop below people, no loops, move ends the old seat, release, budgeted can't be filled, tree roll-ups), hiring request limited to vacant seats, headcount plan approval and limits with revision, position requests approved and rejected, bench strength and urgency rules, succession readiness and bench report and pools, match score, internal application flow (manager consent from the inbox → shortlist → select → move into the position; posting filled), marketplace rules (gig needs weeks/hours, open-application limit, declined consent, withdraw, months in role), permissions, tenant isolation (API and RLS). Green together with `tests/test_tenant_isolation.py`.

### Decisions
- One backend module for all three areas, since succession and the marketplace both hang off positions.
- Seat occupancy lives in `position_seats` (no new column on employees); the position's job values are written to the employee through the employees service, so the job history and scheduled changes work as before.
- New hires are placed in the seat after the employee is created (one extra call from the wizard); the employees create endpoint itself was not changed, so imports and other entry points do not enforce a position.
- The HR processes module (transfer/promotion requests) does not ask for a position yet.
- Succession and nine-box data read performance's review participants and learning's courses directly (read-only).
- Internal postings are created from approved hiring requests by HR rather than through a new "internal" flag on hiring requests, so recruitment's tables were not changed. Position ↔ hiring request links are kept on the position (`requisition_ids`).
- The workflow engine approves an owner's own request at once when the owner is the only approver; tests allow for that.

### Not done / notes
- No bulk import of positions, matrix (dotted-line) positions or position history beyond seat history.
- Nomination by peers for succession is not built; HR adds successors directly.
- While building, an `alembic downgrade` to the shared base on the shared development database also rolled back and re-applied two other builders' new revisions (`ss1a2b3c4d5e` SSO sign-in, `sb1a2b3c4d5e` salary bank files); their tables were re-created empty, so any test rows they had there are gone.

## Custom services: no-code forms (form builder)

Admins make their own request services (Training request, ID card request, Uniform request, Visitor pass …) without code. Each service has a versioned form, its own menu entry, list page, approval flow, number series, statuses and report source.

### What is built
- **Form designer** (Settings → Services & forms → a service → Form): drag field types from the palette onto the form (or click to add), drag to reorder, half/full width, duplicate, remove; properties panel per field. Field types: short text, long text, whole number, decimal, amount (₹), date, date-time, time, yes/no, choose one (dropdown or buttons), choose many, list from masters (single/multiple), employee, department, location (single/multiple), file upload (any/images/documents, up to 10 files), email, phone, web link, formula, section heading, table (line items with up to 25 columns, row formulas). Properties: label, field name, help text, placeholder, required, unique ("different on every record"), starting value (tokens `today`, `now`, `me`, `my`), min/max, length limits, decimals, options, show-if condition, formula with result type, show as list column, access per role (can edit / view only / hidden). Conditions and formulas use `core/expressions.py` (checked live in the designer); a table's key gives its totals (`items.amount`, `items.count`), `requester.*` gives the person filling the form.
- **Versioned JSON definitions**: saving the form creates a new version (`custom_form_versions`) with a change note; records keep the version they were filled in and always render with it. A field's kind (and single/multiple) can't change across versions. Drafts made on an older version move to the current one when submitted.
- **Behaviour per service**: who may submit (everyone, or roles / teams / departments / locations), who handles it (roles and named people; HR admins, the owner and holders of `people.services.records` always), needs approval, drafts on/off, withdraw on/off, working statuses (label, colour, final; first is where records start; statuses in use can't be removed), notifications (handlers on submit, requester on status change and comments), record title field, menu section, icon, number prefix/format.
- **Approval through the workflow engine**: every service registers its own request type `form.<company_id>.<code>` at run time (only the owning company sees it under Approval flows and Automation), with the form's fields as condition variables (`request.total_cost > 10000`, approver "field" `request.host`). A default flow is created when approval is on (template flows: e.g. manager → HR for training). Approve/reject/withdraw/cancel-after-approval move the record (`on_approved` → first status, `on_rejected`, `on_cancelled`, `on_revoked`). Inbox lines come from the form's summary fields.
- **Records**: submit or save draft, edit drafts, handlers edit the fields their role may change (every change is an "edit" activity with the changed field names, plus row-level audit), change status with a note, withdraw (cancels the running approval), delete own drafts only, comments with files (documents storage), activity timeline and approval steps. Unique fields are checked against non-withdrawn records of the service. Server works out show-if (hidden values dropped, hidden fields not required) and formulas; the browser asks `POST /evaluate` as you type so it shows exactly what will be stored.
- **Numbers**: one number series per service (`form.<code>`, e.g. `TRN-2611-0001`, monthly reset), editable in the service settings and on the Numbering page.
- **Lists**: per-service page from the form (summary fields as columns, column picker, filters for status/"all open" and the form's choice and yes/no fields, search, sort, pagination), My requests / All records tabs, saved views (private or shared by handlers), CSV and Excel export (audited as `export.downloaded`; formula-safe CSV), `?new=1` and `?view=<uuid>` deep links. Home → Services shows every service the person may use as cards plus "My requests" across services.
- **Reports**: each service's records are a report source `service_<code>` in a new "Services" category, with requester joins (department, designation, location, legal entity), status, dates and one column per form field (numbers/amounts numeric, dates as dates, lookups as names). Row scope: everyone/`people.services.records` → all, managers → team, others → own.
- **Navigation**: services appear in the menu automatically, under the section chosen for each (Home by default), only for people who may submit or handle them (`GET /custom-forms/nav`, merged into the menu by the app shell). Static entries: Home → Services, Settings → Services & forms.
- **Templates (one click)**: Training request (talent, manager → HR, cost formula, days formula, HR-only notes), ID card request (no approval, photo, replacement charge formula, HR-only card number/issue date), Business card request (manager approval), Visitor pass (no approval, host defaults to you, office to yours, last 4 digits of ID only, extra visitors table, security-desk fields view-only for employees), IT access request (manager → admin, line-items table of systems and levels, temporary access end date).

### Pages
- `/dashboard/services` — catalogue and My requests (all employees).
- `/dashboard/services/[code]` — one service's list, submit modal, details modal (Details / Activity / Approval), edit and status modals.
- `/dashboard/settings/services` — services list, New service (templates + blank), editor modal (Form designer / Settings / Versions), copy, archive/restore.
- Help: `frontend/src/components/help/topics/custom-forms.ts` (3 guides).

### Endpoints (`/api/v1/custom-forms`)
`meta`, `services` (list, create), `services/{code}` (GET, PUT), `services/{code}/form` (PUT, new version), `services/{code}/versions`, `services/{code}/copy`, `services/{code}/archive`, `services/{code}/restore`, `templates/{key}/install`, `check-expression`, `nav`, `catalog`, `services/{code}/definition`, `services/{code}/evaluate`, `services/{code}/records` (POST, GET `scope=mine|all`, `status`, `q`, `date_from`, `date_to`, `f_<field>`, `sort`), `services/{code}/export?format=csv|xlsx`, `my-requests`, `records/{uuid}` (GET, PUT, DELETE draft), `records/{uuid}/submit`, `records/{uuid}/status`, `records/{uuid}/cancel`, `records/{uuid}/comments`, `services/{code}/views` (GET, POST), `views/{uuid}` (PUT, DELETE), `lookups/{employee|department|location|team|role}`.

### Tables
`custom_services`, `custom_form_versions`, `custom_form_records`, `custom_form_comments`, `custom_form_views` — all with company_id, company-first indexes and forced RLS (migration `csv7f3a1b2c9`, down revision `ig1a2b3c4d5e`).

### Permissions
`settings.services.manage` (design services) and `people.services.records` (handle every service's records); HR admin and HR get both. Per-service access is set on the service (submit access, handler roles/people) and per field (role → hidden/view/edit; the company owner always edits).

### Tests
`tests/test_custom_forms.py` (13): definition checks (duplicate names, options, bad formulas, table column types, starting values), value types (amount decimals, whole numbers, limits, email/phone/link, date-time to UTC, choices), required/unknown fields and tables with row formulas and totals, show-if (fields and sections, requester conditions), field access by role, default tokens; API: template install → request type on Approval flows with field variables, default flow, numbering `TRN-YYMM-0001` in sequence, manager → HR approval → first status, status changes by handlers only, rejection; no-approval service with view-only fields ignored on submit, image-only file check, handler edits, other employees blocked; versions keep old records readable and kind changes refused; drafts, unique values, withdraw cancelling the approval, draft delete; submit access by role, menu, switch off, statuses in use, unique names; lists with field filters, my requests, shared saved views, CSV/XLSX export, comments with files, report source and preview; tenant isolation (API, request types, report sources, lookups, cross-company ids, RLS on all five tables). Green with `tests/test_tenant_isolation.py`, `tests/test_workflow*.py`, `tests/test_automation.py` and `tests/test_insights.py`.

### Decisions
- Small additive changes to shared code: `core/workflow/registry.py` and `modules/reports/registry.py` gained `add_provider(...)` for company-owned request types and report sources defined at run time (built-in ones are unchanged; `all_types()` / `all_sources()` add the current company's own). The provider looks a key up from the database (cached two minutes, refreshed at once in the process that changed it), so several API processes stay correct. `app-shell.tsx` and `hr-nav.ts` gained optional "extra pages" for the service menu entries.
- "Groups" for submit access are teams (plus departments and locations); there is no separate group module.
- Choice options are stored as their text, so old records stay readable after options are renamed; amounts and decimals are stored as exact text ("1500.00"), whole numbers as integers, date-times in UTC.
- Formulas and show-if run on the server (preview through `/evaluate`); there is no JavaScript copy of the expression language.
- Report "all rows" access uses `people.services.records` (or HR admin); a service's handler roles alone don't widen report scope.
- Nothing is installed for new companies; the demo company gets Training request, ID card request and Visitor pass.

### Not done / notes
- No multi-page forms, field-wise review/send back, embedded public web forms, OCR, "related forms", address field or reason-on-edit.
- Sorting the list by a form field and filtering by lookup fields from the filter bar are not offered (API supports `f_<field>` for lookups).
- Help guides need demo data for the guide frames (coordinator's demo fetch).

## Salary bank files and Google / Microsoft sign-in

### Salary bank files (`modules/salary_bank`)
- **Ready-made layouts** (`formats.py`, data only): standard NEFT/RTGS/IMPS CSV, SBI CINB/CMP, HDFC ENet (28 fields, no header, I/N/R), ICICI CIB (21 columns, Pay Mod I/N/R, DD-MMM-YYYY), Axis corporate banking, Kotak CMS (19 columns, IFT/NEFT/RTGS/IMPS, client code), Yes Bank, IDFC FIRST (32 columns, BT/LBT), IndusInd, Bank of Baroda, PNB, Canara, Union Bank (published samples: pipe NEFT/RTGS file with FILEHDR, separate within-bank salary CSV whose DR line equals the credits), Federal Bank, South Indian Bank. Each layout records its sources and a confidence level (high = bank's own sample, medium = vendor/ERP documentation, low = bank hands its template out only inside the portal; a common layout is used and the screen says to compare).
- **Engine** (`engine.py`): separated (any delimiter, quoting choice) or fixed width (width, align, pad, cut), header and trailer lines with `{count}`, `{total}`, `{total_paise}`, `{account_hash}` (hash total), date and amount formats (2 decimals, whole rupees, paise), per-mode codes, split rules (none / own bank vs other banks / per mode), per-group variants (Union), text cleaning, file name patterns.
- **Rules**: same bank as the debit account = fund transfer; otherwise NEFT, RTGS from a configurable amount (never below ₹2 lakh) or IMPS up to ₹5 lakh (payroll settings → Bank files). IFSC `AAAA0XXXXXX`, account 6–20 letters/digits (errors leave the person out only when the user agrees); account length per big bank, letters in account and duplicate accounts are warnings. Debit account checked only where the layout prints it; client code / corporate id where the bank needs it.
- **Debit accounts** per legal entity (default per entity, or one for all entities), each with its format, client code and corporate id; can be copied from the entity's saved bank accounts. Without any, the old single account in payroll settings is used and the file is still made (warning).
- **Files**: one per debit account / format / group; each stored (`bank_file_batches`) with SHA-256, count, total, value date, payslips, people left out, who made it; downloads counted and audited (`payroll.bank_file_generated`, `export.downloaded`). Making files again marks earlier ones superseded. The old `GET /payroll/runs/{run}/files/bank` now uses this engine (zip when several files).
- **Pages**: Payroll settings → Bank files (rules, debit accounts table + modal, formats table, layout view modal with columns / sample / sources, custom format builder modal with live preview); pay run → Lock and pay → Make bank files (check modal with files, totals and problems; files list with download, history of replaced files).
- **Endpoints** (`/api/v1/payroll/bank`): `formats` (GET, POST), `formats/{uuid}` (PUT), `formats/preview`, `accounts` (GET, POST), `accounts/{uuid}` (GET full number, PUT), `entity-accounts`, `suggest?ifsc=`, `runs/{run}/check`, `runs/{run}/files` (GET, POST), `files`, `files/{uuid}/download`.
- **Tables**: `bank_debit_accounts`, `bank_file_formats`, `bank_file_batches` (company_id, company-first index, forced RLS; migration `sb1a2b3c4d5e`).
- **Permission**: `payroll.bank_files` (make and download bank files; HR admin and HR). Formats and debit accounts need `payroll.settings`.
- **Tests**: `tests/test_salary_bank.py` (16): every layout renders and has sources; column layout, codes and dates of generic, HDFC, ICICI, SBI split, Axis, Kotak, IDFC, Yes, Union (NEFT file + salary file with DR total), PSU header totals; IFSC/account checks; fixed-width custom format with header, trailer, paise and hash total; spec checks; API: builder and preview, debit accounts per entity, split by entity and bank, Union needing corporate id, audit rows, superseded files, zip on the old address, tenant isolation, permissions, leave-out flow, approved-run rule.

### Google and Microsoft sign-in (`modules/sso`)
- Same flow as the clinic product's Google sign-in (API builds the provider address, provider returns to the API, API sends the browser to the company's address) with: a signed `state` tied to an HttpOnly cookie, a one-time code (2 minutes, single use through Redis) instead of the session token in the address, verified emails only, and no self sign-up (the old Google code that created companies is removed from `modules/auth/router.py`).
- Google: OpenID Connect userinfo, `email_verified` required. Microsoft (Entra ID, multi-tenant, `organizations` by default; `common` or a tenant id possible): the Graph `userPrincipalName` is used (never the editable `mail` attribute, to avoid the "nOAuth" takeover); guest accounts are refused.
- Matching: company owner email or an active employee, case-insensitive. On a company address only that company; on the main site every company that allows the provider: one match signs in and moves to the company's address, several ask for the company's address. Company must have the provider switched on and, if domains are listed, the email's domain must be one of them.
- **Pages**: login page shows "Sign in with Google / Microsoft" only when available (company address: company switched it on; main site: platform ready and some company uses it) with plain error messages; `/auth/callback` swaps the one-time code; Settings → Sign-in options (`/dashboard/settings/sign-in`, `settings.company.manage`); platform admin → Sign-in apps (`/admin/sign-in`): client ids, secrets (encrypted, never returned), Microsoft tenant, redirect address to register.
- **Endpoints**: `GET /auth/sso/options`, `GET /auth/sso/{provider}/start`, `GET /auth/sso/{provider}/callback`, `POST /auth/sso/exchange`, `GET/PUT /sso/settings`, `GET /platform/admin/sso`, `PUT /platform/admin/sso/{provider}`.
- **Tables**: `sso_providers` (platform level), `sso_company_settings` (RLS) — migration `ss1a2b3c4d5e`.
- **Tests**: `tests/test_sso.py` (13) with the provider call mocked: platform set-up needs platform admin, switches and domain checks, employee on company address, owner via Microsoft from main site, main site finds the company, same email in two companies, refusals (switched off, other domain, unverified, unknown person, other company's address, guest), inactive employee, forged/garbage state, permissions and per-company settings; plus the real Google and Microsoft exchange code with HTTP mocked (UPN used instead of `mail`, guests refused, unverified Google email flagged).

### Help
`frontend/src/components/help/topics/bank-sso.ts` (3 guides: bank file settings, making bank files, sign-in options).

### Decisions
- Bank layouts are configuration, not code, so a company can copy any layout and adjust it to its bank's portal sample without a release. Only Union Bank (and ICICI's field rules) come from documents the bank publishes; SBI, Axis, Yes, IndusInd, BoB, PNB, Canara, Federal and SIB do not publish theirs, so those are marked "Check with your bank".
- The standard CSV and the SBI layout keep the columns this product used before, so existing files and tests stay the same.
- `modules/auth/router.py` helpers (`_owner_token`, `_employee_token`, payloads, `_signed_in`) are imported by the SSO module rather than duplicated.
- The company-level "Google sign-in" / "Microsoft sign-in" entries in Integrations (`modules/assistant/integrations.py`) are now redundant: sign-in uses the platform apps. Left in place (another module's file); the coordinator may remove them.
- Small shared edits: `payroll/exports.py` (bank file delegates to the new module), `payroll/defaults.py` (bank rules), `payroll/router.py` (access list includes `payroll.bank_files`), payroll settings and pay run pages, login and auth callback pages, `hr-nav.ts`, admin layout nav.

### Not done / notes
- Encrypted (PGP / bank PKI) bank files and host-to-host (SFTP/API) upload to banks are not built; files are downloaded and uploaded in the bank portal.
- Bank response (reverse) files are not imported; payment status is still set with Mark as paid.
- The Content-Disposition file name is not exposed by CORS, so the browser falls back to the name shown in the list (same as other downloads).
