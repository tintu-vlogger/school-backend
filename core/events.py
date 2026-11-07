"""Business events inside the API process: a module announces that something happened and
any module that cares reacts, without the two importing each other.

    from core import events
    events.subscribe(on_event)                 # fn(db, event) -> None, usually in hooks.py
    events.emit(db, events.Event("approved", "workflow.leave.request", record_id, company_id, data={...}))

Subscribers run in the same transaction as the code that emits, each inside its own
savepoint, so a failing subscriber never undoes the change that caused the event.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.orm import Session

logger = logging.getLogger("hrms")


@dataclass
class Event:
    kind: str                  # created | updated | approved | rejected | cancelled | revoked | ...
    entity: str                # record type, e.g. "employee" or "workflow.leave.request"
    record_id: int | None
    company_id: int
    data: dict = field(default_factory=dict)        # the record as plain values
    changes: dict = field(default_factory=dict)     # {field: {"old": .., "new": ..}} for updates


_SUBSCRIBERS: list[Callable[[Session, Event], None]] = []


def subscribe(fn: Callable[[Session, Event], None]) -> None:
    if fn not in _SUBSCRIBERS:
        _SUBSCRIBERS.append(fn)


def emit(db: Session, event: Event) -> None:
    for fn in list(_SUBSCRIBERS):
        try:
            with db.begin_nested():
                fn(db, event)
        except Exception as exc:  # never let a reaction break the business action
            logger.warning("Event subscriber %s failed on %s/%s: %s", getattr(fn, "__name__", fn),
                           event.entity, event.kind, exc)
