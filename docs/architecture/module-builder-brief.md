# Brief for module builders

You are building one module of Campusly, a multi-tenant HR and payroll SaaS for Indian companies. Several builders work in parallel on different modules. Work autonomously to completion: the product owner is not available, so do not ask questions; decide sensibly and record your decisions.

## Read first
1. `backend/docs/architecture/platform.md`: architecture and coding rules (tenant isolation, audit, time, masters, custom fields, number series, rules, workflow engine, API and web conventions, the **UI kit** in §10, building blocks in §13). Follow it exactly.
2. `backend/docs/architecture/naming.md`
3. `backend/docs/requirements/hrms-payroll-requirements.md`: the law section (§3), your module's section, Appendix A (your module) and Appendix B (your module).
4. Competitor research for your module: `/Users/tintucraju/Projects/Personal/hrai/docs/research/zoho-people/features/<file>.md` (features, settings, options, forms and gaps). Use it for features and rules only; our design is our own.
5. Reference app for Indian HR features and UI ideas (read only): `/Users/tintucraju/Projects/Personal/hrai/../../Python/Django/ihna-platform` (i.e. `/Users/tintucraju/Projects/Python/Django/ihna-platform`). It serves India and Australia; take only what applies to India.
6. Look at an existing finished module before writing yours: backend `modules/employees`, `modules/masters`, `modules/workflow`; web `src/app/dashboard/staff`, `src/app/dashboard/settings/masters`, `src/app/dashboard/inbox`.

## Environment
- Backend `/Users/tintucraju/Projects/Personal/hrai/backend` (venv `.venv`, run with `PYTHONPATH=.`). API runs with --reload at http://127.0.0.1:8100 (restart in the background if it dies: `cd backend && PYTHONPATH=. .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8100 --reload`).
- Web `/Users/tintucraju/Projects/Personal/hrai/frontend` (Next.js; read `frontend/AGENTS.md`). Dev server at http://localhost:3100 in webpack mode (if it dies: `npx next dev --webpack -p 3100` from frontend/; Turbopack currently fails on the Google font). Disk space is limited (about 10 GB free): never create extra databases (tests make and drop their own), delete your screenshots after checking them, and do not copy large files.
- DB `hrms` (local Postgres, user tintucraju), tests use `hrms_test`.
- Logins: owner `tintu@test.com` / `Welcome9#` (company web address `campusly-demo`; sign-in at http://localhost:3100/login redirects to http://campusly-demo.localhost:3100), employee `priya.sharma@example.com` / `Welcome9#`, platform admin `admin@campusly.dev` / `Platform9#admin`.
- Headless browser checks: playwright-core at `/private/tmp/claude-502/-Users-tintucraju-Projects-Personal-hrai/6d822b4a-6b83-471a-91bf-1ca474f9bde1/scratchpad/shot` with `executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"`, headless. Always end scripts with `process.exit(0)`. Never touch the Chrome on port 9333.
- Never touch `/Users/tintucraju/Projects/Personal/clinimate` or `~/Projects/hrms`.

## Working in parallel safely
- Put your code in your own folders: backend `modules/<your_module>/` (and `tests/test_<your_module>*.py`), web `src/app/dashboard/<your pages>/` and `src/components/<your_module>/`. Add generic UI to `src/components/ui-kit/` only if it is truly reusable, and never change an existing kit component's behaviour.
- Shared files you may edit with small, careful changes: `src/lib/hr-nav.ts` (your entries only; remove `intro`/`features` from entries you implement so the real page opens), `core/master_seed.py` (append your master types), the employee model only if your module truly needs a column there (say so in your report).
- **Migrations:** create your own Alembic revisions with `down_revision = "ig1a2b3c4d5e"` (the current single head) for your first one (other builders do the same, so Alembic will show several heads; that is expected). Run `alembic upgrade heads`. Do NOT create merge revisions; the coordinator merges at the end. Every new company-owned table needs `company_id`, the index, and the RLS policy (see platform.md §3 and `core/rls.py`; the guard test fails otherwise).
- **Git:** commit only your own files (`git add <paths>`; never `git add -A`). Before pushing: `git pull --rebase origin main`, then `git push origin main`. Commit messages are plain and descriptive, with NO "Co-Authored-By" line and no mention of Claude, Anthropic or AI anywhere (code, comments, docs, commits).
- Default data per company: when a company is created (and once for existing companies), seed sensible Indian defaults for your module through a seed function called from company creation and from startup seeding (look at how masters are seeded per company).

## Quality bar
- Everything configurable through settings screens, not code; versioned policies where rules change over time (effective dates).
- Every request that needs approval goes through the workflow engine (register your request types and handlers in `modules/<module>/hooks.py`; seed a sensible default flow per company).
- Every change audited (automatic for models; `audit.event()` for business events).
- Times stored in UTC, shown with `src/lib/datetime.ts`.
- Indian law built in where it applies (see requirements §3), with values as configurable, effective-dated settings.
- UI: the shared UI kit only; **Add / Edit / View details in centered modals (never separate pages, never drawers)**; `DataTable` + `FilterBar` for lists; `MasterSelect` for master-backed fields; searchable `Select` for other lists; `PageHeader` on every page; flat paper style, thin fonts (300/400), lucide icons strokeWidth about 1.4; plain, short copy. It must look like a polished, modern original product.
- Employee self-service and manager views where the module has them (my requests, team requests, approve from the inbox).
- Tests: pytest for your core calculations and rules, permissions and tenant isolation of your tables; `pytest` green, `alembic check` clean for your models, `npx tsc --noEmit -p .` clean.
- Check your main pages in headless Chrome (signed in as owner and as the employee) with no page errors, and look at the screenshots yourself.

## When done
Append a section for your module to `backend/docs/PROGRESS.md` (what is built, pages, endpoints, tables, tests, decisions, anything unfinished), commit and push it, and reply with the same summary.

## Added for the second wave
- Permissions are pre-approved for this project; never stop to ask anything. Decide and note decisions in your report.
- Pytest: run only your own test files plus `tests/test_rls_guard*.py` (the full suite is slow and other builders run at the same time). Each run creates and drops its own temporary database.
- The API runs WITHOUT auto-reload at :8100. To try new endpoints, run your own copy: `cd backend && PYTHONPATH=. .venv/bin/uvicorn main:app --host 127.0.0.1 --port <8101..8109, your own>` and stop it when done. Do not restart :8100.
- Help: write a guide for each page you add in `frontend/src/components/help/topics/<your_module>.ts` exporting `TOPICS: HelpTopic[]` (format in `frontend/src/components/help/help-types.ts`; look at `topics/time-pay.ts` as an example). Do not edit `help-index.ts`; the coordinator adds your file.
- Navigation: add your pages to `frontend/src/lib/hr-nav.ts` with the right `need` permissions; add permissions to the roles catalogue (`backend/modules/roles/models.py` or wherever the catalogue lives) and give HR admin all of them.
- Commit only your own paths. Then `git pull --rebase --autostash origin main` and `git push origin main` in each repo you touched. No attribution lines, no mention of Claude/Anthropic/AI.
