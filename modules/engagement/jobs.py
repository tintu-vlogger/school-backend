"""Background work for the engagement module, run every few minutes inside the API
process (one process at a time through a Postgres advisory lock):

- the morning celebration posts and emails (once the company's local hour has passed),
- scheduled announcements going live and expired ones leaving the top of Buzz,
- acknowledgement reminders,
- survey runs opening and closing, recurrences and mandatory-survey reminders.

Every step is idempotent, so running it twice (or after a restart) changes nothing.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from sqlalchemy import text

log = logging.getLogger("hrms.engagement")

EVERY_SECONDS = 5 * 60
LOCK_ID = 724101


def run_company(db, company, force_celebrations: bool = False) -> dict:
    from modules.engagement import announcements, celebrations, surveys
    from modules.engagement import settings as cfg
    out = {"celebrations": celebrations.run_morning(db, company, force=force_celebrations)}
    out["announcements"] = announcements.run_due(db, company.id)
    out["ack_reminders"] = announcements.remind_ack(
        db, company.id, int(cfg.section(db, company.id, "announcements").get("remind_ack_after_days") or 0))
    out["surveys"] = surveys.run_due(db, company.id)
    return out


def run_all() -> int:
    """One pass over every active company. Returns how many companies were processed."""
    from core.db import system_session
    from modules.company.models import Company
    db = system_session()
    done = 0
    try:
        if not db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": LOCK_ID}).scalar():
            return 0
        ids = [i for (i,) in db.query(Company.id).filter(Company.active.is_(True)).all()]
        db.commit()
    except Exception as exc:
        db.rollback()
        log.warning("Engagement job could not start: %s", exc)
        return 0
    finally:
        db.close()
    for cid in ids:
        db = system_session()
        db.info["company_id"] = cid
        try:
            # one company at a time, under the same lock key per company
            if not db.execute(text("SELECT pg_try_advisory_xact_lock(:k, :c)"), {"k": LOCK_ID, "c": cid}).scalar():
                continue
            company = db.query(Company).filter(Company.id == cid).first()
            run_company(db, company)
            db.commit()
            done += 1
        except Exception as exc:  # never let one company stop the others
            db.rollback()
            log.warning("Engagement job failed for company %s: %s", cid, exc)
        finally:
            db.close()
    return done


async def _loop():
    await asyncio.sleep(20)  # let the app finish starting
    while True:
        try:
            await asyncio.to_thread(run_all)
        except Exception as exc:  # pragma: no cover
            log.warning("Engagement job loop error: %s", exc)
        await asyncio.sleep(EVERY_SECONDS)


@asynccontextmanager
async def lifespan(_app):
    """Merged into the app's lifespan by FastAPI when the router is included."""
    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
