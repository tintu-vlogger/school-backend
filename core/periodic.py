"""Periodic background jobs shared by modules (SLA escalation, reminders, recurring work).

A module registers a job in its hooks.py:

    from core import periodic
    periodic.register("helpdesk.sla", escalate_breaches)   # fn(db) -> int (things done)

Every job runs every 15 minutes in the API process, in the same loop as approval
escalations (one process at a time, see main.py), with a system session that bypasses
RLS. Each job runs inside its own savepoint so one failing job never undoes another.
Jobs must be idempotent: running twice in a row does nothing the second time.
"""
from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.orm import Session

logger = logging.getLogger("hrms")

_JOBS: dict[str, Callable[[Session], int]] = {}


def register(name: str, fn: Callable[[Session], int]) -> None:
    _JOBS[name] = fn


def names() -> list[str]:
    return sorted(_JOBS)


def run_all(db: Session) -> int:
    total = 0
    for name, fn in sorted(_JOBS.items()):
        try:
            with db.begin_nested():
                total += int(fn(db) or 0)
        except Exception as exc:  # never let one job stop the others
            logger.warning("Periodic job %s failed: %s", name, exc)
    return total
