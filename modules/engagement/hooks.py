"""Engagement hooks: permissions this module adds, and the approval request type used
when a company reviews Buzz posts before they appear.

engagement.buzz_post: with "Review posts before they appear" switched on, a new post
waits (status pending) until a moderator approves it in the Approvals inbox; rejected
posts stay visible only to their author with the reason.
"""
from sqlalchemy.orm import Session

from core.workflow.registry import RequestType, register
from modules.roles import models as roles

# Permissions owned by this module, added to the product list at start-up so the roles
# screen shows them and the system roles get sensible defaults.
PERMISSIONS = {
    "engage.buzz.moderate": "Moderate Buzz: pin, hide and review reported posts",
    "engage.surveys.manage": "Create surveys and see their results",
    "engage.settings.manage": "Change Buzz, celebration, kudos and kiosk settings",
}
for _key, _label in PERMISSIONS.items():
    roles.ALL_PERMISSIONS.setdefault(_key, _label)
for _key in PERMISSIONS:
    if _key not in roles.DEFAULT_ROLE_PERMISSIONS["admin"]:
        roles.DEFAULT_ROLE_PERMISSIONS["admin"].append(_key)
    if _key not in roles.DEFAULT_ROLE_PERMISSIONS["hr"]:
        roles.DEFAULT_ROLE_PERMISSIONS["hr"].append(_key)


def _post(db: Session, instance):
    from modules.engagement.models import BuzzPost
    return db.query(BuzzPost).filter(BuzzPost.company_id == instance.company_id,
                                     BuzzPost.id == instance.payload.get("post_id")).first()


def _on_approved(db: Session, instance) -> None:
    from modules.engagement import feed
    post = _post(db, instance)
    if post is not None and post.status == "pending" and post.deleted_at is None:
        post.status = "published"
        post.created_at = feed.now()  # appears at the top of the feed when approved
        feed.after_publish(db, post, None)
        if post.author_employee_id:
            from modules.notifications.service import notify
            notify(db, post.company_id, [post.author_employee_id], kind="moderation", title="Your Buzz post is live",
                   link=f"/dashboard/buzz?post={post.uuid}")


def _on_rejected(db: Session, instance) -> None:
    post = _post(db, instance)
    if post is not None and post.status == "pending":
        post.status = "rejected"
        post.hidden_reason = instance.result_note or "Not approved by a moderator"


def _on_cancelled(db: Session, instance) -> None:
    from modules.engagement import feed
    post = _post(db, instance)
    if post is not None and post.status == "pending":
        post.deleted_at = feed.now()


def _summarize(instance) -> list[str]:
    excerpt = (instance.payload or {}).get("excerpt") or ""
    return [excerpt] if excerpt else ["A photo or poll post"]


register(RequestType(
    key="engagement.buzz_post",
    label="Buzz post review",
    description="A new Buzz post waits for a moderator when the company reviews posts before they appear.",
    on_approved=_on_approved,
    on_rejected=_on_rejected,
    on_cancelled=_on_cancelled,
    summarize=_summarize,
    variables={"request.excerpt": "Start of the post"},
    default_steps=[{"name": "Moderator review", "approver_type": "role", "approver_value": "hr", "mode": "any"}],
))
