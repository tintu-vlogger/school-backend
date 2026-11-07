# Ensure backend dir is on PYTHONPATH so absolute imports resolve correctly.
# e.g.  PYTHONPATH=/app or run with `uvicorn main:app` from the backend/ directory.

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from modules import all_routers, import_all_hooks, import_all_models, root_routers
from seed.seed_data import run_seed
from core.config import get_settings

logger = logging.getLogger("hrms")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: platform admin, system roles, development demo login
    get_settings().check()
    run_seed()
    import asyncio
    task = asyncio.create_task(_escalation_loop())
    yield
    task.cancel()


ESCALATION_EVERY_SECONDS = 15 * 60


def _escalate_once() -> int:
    """Move approvals that passed their time limit, for every company (RLS bypassed,
    every change audited as the system)."""
    from core.db import system_session
    from core.workflow.service import escalate_overdue
    from sqlalchemy import text
    db = system_session()
    try:
        # one worker at a time when several API processes run
        if not db.execute(text("SELECT pg_try_advisory_xact_lock(724001)")).scalar():
            return 0
        n = escalate_overdue(db)
        from core import periodic
        n += periodic.run_all(db)
        db.commit()
        return n
    except Exception as exc:  # never let the loop die
        db.rollback()
        logger.warning("Escalation check failed: %s", exc)
        return 0
    finally:
        db.close()


async def _escalation_loop():
    import asyncio
    while True:
        await asyncio.sleep(ESCALATION_EVERY_SECONDS)
        n = await asyncio.to_thread(_escalate_once)
        if n:
            logger.info("Escalated %s late approvals", n)


app = FastAPI(title="Next Gen HRMS API", version="0.1.0", lifespan=lifespan)

# ── HTTP cache headers on slow-changing public endpoints ──────────
# Tells the browser it can reuse the same response for a short window. Auth
# responses are marked private so proxies never cache user data.
_CACHEABLE_PATHS = (
    "/api/v1/public/companies/",
)


@app.middleware("http")
async def _browser_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.method == "GET":
        path = request.url.path
        if any(path.startswith(p) for p in _CACHEABLE_PATHS):
            # Short cache with SWR: browsers serve the stale copy instantly and
            # revalidate in the background.
            response.headers.setdefault("Cache-Control", "public, max-age=30, stale-while-revalidate=120")
    return response


from core.idempotency import IdempotencyMiddleware  # noqa: E402
from core.context import RequestContextMiddleware  # noqa: E402
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://frontend:3000",
        "https://nextgenhrms.com",
        "https://www.nextgenhrms.com",
        *[o.strip() for o in get_settings().CORS_ORIGINS.split(",") if o.strip()],
    ],
    allow_origin_regex=r"^https?://([a-z0-9-]+\.)?(nextgenhrms\.com|lvh\.me|localhost)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Cache CORS preflight for 24h so browsers stop re-OPTIONS-ing every 5 seconds.
    max_age=86400,
)
# Outermost: every request gets its own context (company, actor, IP, request id).
app.add_middleware(RequestContextMiddleware)


# ── Global Error Handlers ────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for err in exc.errors():
        field = " → ".join(str(loc) for loc in err.get("loc", []) if loc != "body")
        errors.append({"field": field, "message": err.get("msg", "Invalid value")})
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Please check your input and try again.",
            "errors": errors,
        },
    )


@app.exception_handler(IntegrityError)
async def integrity_exception_handler(request: Request, exc: IntegrityError):
    logger.error(f"Database integrity error: {exc}")
    msg = "A record with this information already exists."
    detail = str(exc.orig) if exc.orig else str(exc)
    if "unique" in detail.lower() or "duplicate" in detail.lower():
        if "email" in detail.lower():
            msg = "This email address is already registered."
        elif "phone" in detail.lower():
            msg = "This phone number is already registered."
        else:
            msg = "A record with this information already exists."
    return JSONResponse(status_code=409, content={"detail": msg})


@app.exception_handler(SQLAlchemyError)
async def database_exception_handler(request: Request, exc: SQLAlchemyError):
    logger.error(f"Database error: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "A database error occurred. Please try again later."},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error on {request.method} {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected error occurred. Please try again later."},
    )


# ── Routers ──────────────────────────────────────────────────────────
# Every modules/<name>/router.py is mounted under /api/v1 automatically.

import_all_models()
import_all_hooks()
for _router in all_routers():
    app.include_router(_router, prefix="/api/v1")
# routers called at fixed paths by outside devices (e.g. attendance machines: /iclock/...)
for _router in root_routers():
    app.include_router(_router)



@app.get("/")
def root():
    return {"message": "Next Gen HRMS API is running"}


@app.get("/api/v1/health")
def health_check():
    return {"status": "healthy"}


@app.get("/s/{code}")
def redirect_short_url(code: str):
    """Redirect short URLs to their original long URLs."""
    import redis as _redis_lib
    from fastapi.responses import RedirectResponse
    from core.config import get_settings as _gs
    s = _gs()
    r = _redis_lib.Redis.from_url(s.redis_url, decode_responses=True)
    long_url = r.get(f"url:{code}")
    if not long_url:
        raise HTTPException(status_code=404, detail="Link not found or expired")
    return RedirectResponse(url=long_url, status_code=302)
