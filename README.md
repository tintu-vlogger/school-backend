# Campusly API

Multi-tenant SaaS for running any educational institution: schools, preschools, colleges,
universities, coaching and training institutes. Each institution is a tenant with its own
sub-domain; data is isolated with PostgreSQL row-level security.

Stack: FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, Redis.

## Run locally

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # DATABASE_URL, REDIS_*, SECRET_KEY ...
createdb school
PYTHONPATH=. .venv/bin/python -m alembic upgrade heads
PYTHONPATH=. .venv/bin/python -m uvicorn main:app --port 8200 --reload
```

In development the start-up seed creates a demo institution (`campusly-demo`) with staff,
classes, students and parents. Password for every demo login: `Welcome9#`

| Who | Sign-in |
|---|---|
| Owner / administrator | tintu@test.com |
| Principal | kavitha.raghavan@example.com |
| Teacher | divya.menon@example.com |
| Accountant | ganesh.pillai@example.com |
| Parent | parent@test.com |
| Student | student@test.com |
| Platform admin | admin@campusly.dev / Platform9#admin |

## Layout

- `core/` shared platform code: tenancy and RLS, security, audit, masters, custom fields,
  number series, rules, workflow engine, generic CRUD.
- `modules/<name>/` one folder per module (models, service, router, hooks, seed). Modules are
  discovered automatically.
- `alembic/` migrations. `tests/` pytest suite (each run uses its own temporary database).
- `docs/architecture/platform.md` coding rules, `docs/architecture/school-module-brief.md`
  school module guide, `docs/PROGRESS.md` what is built.

## Tests

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
```
