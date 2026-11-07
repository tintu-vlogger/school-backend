"""Sending notifications by email and push (listeners in dispatch.py), retries and the
daily digests."""
from core import periodic
from modules.notifications import dispatch  # noqa: F401  (registers the after-commit sender)

periodic.register("notifications.dispatch", dispatch.periodic_job)
