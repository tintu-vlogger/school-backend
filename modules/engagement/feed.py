"""Buzz feed rules: posts, polls, reactions, comments, pinning, moderation and the feed
itself (audience filtered, newest first, cursor paginated for infinite scroll).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from core.security import _get_role_permissions
from modules.employees.models import Employee
from modules.engagement import audience as aud
from modules.engagement import content, people
from modules.engagement import settings as cfg
from modules.engagement.models import (Announcement, AnnouncementReceipt, BuzzComment, BuzzPollOption, BuzzPollVote,
                                       BuzzPost, BuzzReaction, BuzzReport, Kudos, KudosRecipient, Survey,
                                       SurveyParticipation, SurveyRun)
from modules.engagement.people import Actor
from modules.notifications.service import notify

CELEBRATION_KINDS = ("birthday", "work_anniversary", "wedding_anniversary", "welcome")
FILTERS = {
    "all": None,
    "posts": ("post",),
    "polls": ("poll",),
    "celebrations": CELEBRATION_KINDS,
    "kudos": ("kudos",),
    "announcements": ("announcement",),
    "surveys": ("survey",),
}
REPORT_REASONS = {"spam": "Spam", "offensive": "Offensive or abusive", "harassment": "Harassment",
                  "confidential": "Shares confidential information", "misinformation": "Misleading", "other": "Something else"}
PAGE = 10


def now() -> datetime:
    return datetime.now(timezone.utc)


def is_moderator(db: Session, actor: Actor) -> bool:
    if actor.is_owner or actor.role == "admin":
        return True
    return "engage.buzz.moderate" in _get_role_permissions(actor.company_id, actor.role, db)


def can_post(db: Session, actor: Actor, buzz: dict) -> bool:
    if not buzz.get("enabled", True):
        return is_moderator(db, actor)
    if actor.is_owner or buzz.get("who_can_post") == "everyone":
        return True
    return actor.role in (buzz.get("post_roles") or []) or is_moderator(db, actor)


# ── Loading ─────────────────────────────────────────────────────────

def _post(db: Session, company_id: int, uuid) -> BuzzPost:
    row = (db.query(BuzzPost).options(joinedload(BuzzPost.poll_options))
           .filter(BuzzPost.company_id == company_id, BuzzPost.uuid == uuid, BuzzPost.deleted_at.is_(None)).first())
    if row is None:
        raise NotFound("Post")
    return row


def visible_post(db: Session, actor: Actor, uuid) -> BuzzPost:
    post = _post(db, actor.company_id, uuid)
    mine = post.author_employee_id == actor.employee_id and not post.is_system and (
        actor.employee_id is not None or post.author_employee_id is None)
    if post.status != "published" and not (mine or is_moderator(db, actor)):
        raise NotFound("Post")
    if not mine and not aud.includes(db, actor.company_id, post.audience, actor.viewer_id) and not is_moderator(db, actor):
        raise NotFound("Post")
    return post


def _is_author(post: BuzzPost | BuzzComment, actor: Actor) -> bool:
    if isinstance(post, BuzzPost) and post.is_system:
        return False
    return post.author_employee_id == actor.employee_id


# ── Creating and editing posts ──────────────────────────────────────

def _clean_body(text: str | None, buzz: dict) -> str:
    text = (text or "").strip()
    if len(text) > 5000:
        raise BadRequest("Posts can be up to 5,000 characters.")
    content.check_words(content.plain(text), buzz.get("blocked_words") or [])
    return text


def _clean_preview(db: Session, company_id: int, preview: dict | None) -> dict | None:
    if not preview or not preview.get("url"):
        return None
    url = str(preview["url"])[:1000]
    if not url.lower().startswith(("http://", "https://")):
        return None
    image = None
    if isinstance(preview.get("image"), dict) and preview["image"].get("uuid"):
        try:
            image = content.files_for(db, company_id, [preview["image"]["uuid"]])[0]
        except BadRequest:
            image = None
    s = lambda v, n: (str(v).strip()[:n] or None) if v else None  # noqa: E731
    return {"url": url, "title": s(preview.get("title"), 200), "description": s(preview.get("description"), 300),
            "site": s(preview.get("site"), 100), "image": image}


def _mentioned_ids(db: Session, company_id: int, text: str | None) -> list[int]:
    found = people.by_uuid(db, company_id, content.mention_uuids(text))
    return [e.id for e in found.values() if e.active]


def create_post(db: Session, actor: Actor, data: dict) -> BuzzPost:
    buzz = cfg.section(db, actor.company_id, "buzz")
    if not can_post(db, actor, buzz):
        raise Forbidden("Posting on Buzz is limited to some roles in your company.")
    body = _clean_body(data.get("body"), buzz)
    attachments = content.files_for(db, actor.company_id, data.get("attachments")) if data.get("attachments") else []
    if attachments and not buzz.get("allow_attachments", True):
        raise BadRequest("Attachments are switched off for Buzz.")
    poll_in = data.get("poll")
    kind = "poll" if poll_in else "post"
    if kind == "poll" and not buzz.get("allow_polls", True):
        raise BadRequest("Polls are switched off for Buzz.")
    if not body and not attachments and not poll_in:
        raise BadRequest("Say something, or add a photo or a poll.")
    audience = aud.normalize(data.get("audience"))
    if not aud.is_everyone(audience) and not buzz.get("allow_audience", True) and not is_moderator(db, actor):
        raise BadRequest("Posts on Buzz go to everyone in your company.")
    post = BuzzPost(
        company_id=actor.company_id, kind=kind, author_employee_id=actor.employee_id, body=body,
        mentions=_mentioned_ids(db, actor.company_id, body), hashtags=content.hashtags(body),
        attachments=attachments, link_preview=_clean_preview(db, actor.company_id, data.get("link_preview")),
        audience=audience, allow_comments=bool(data.get("allow_comments", True)),
        status="pending" if buzz.get("review_posts") and not is_moderator(db, actor) else "published",
    )
    if poll_in:
        options = [str(o).strip()[:200] for o in (poll_in.get("options") or []) if str(o).strip()]
        if len(options) < 2 or len(options) > 8:
            raise BadRequest("A poll needs between 2 and 8 options.")
        if len(set(o.lower() for o in options)) != len(options):
            raise BadRequest("Poll options must be different from each other.")
        closes = poll_in.get("closes_at")
        if closes is not None and closes.tzinfo is None:
            closes = closes.replace(tzinfo=timezone.utc)
        if closes is not None and closes <= now():
            raise BadRequest("The poll must close in the future.")
        post.poll = {"multiple": bool(poll_in.get("multiple")), "closes_at": closes.isoformat() if closes else None,
                     "show_voters": bool(poll_in.get("show_voters", True))}
        post.poll_options = [BuzzPollOption(company_id=actor.company_id, label=o, position=i) for i, o in enumerate(options)]
    db.add(post)
    db.flush()
    if post.status == "pending":
        from core.workflow import service as workflow
        inst = workflow.submit(db, actor.company_id, "engagement.buzz_post",
                               {"post_id": post.id, "excerpt": content.plain(body)[:300]},
                               f"Buzz post by {actor.name}", subject_type="buzz_posts", subject_id=post.id)
        post.workflow_instance_id = inst.id
        db.flush()
        db.refresh(post)
    if post.status == "published":
        after_publish(db, post, actor)
    return post


def after_publish(db: Session, post: BuzzPost, actor: Actor | None) -> None:
    """Tell the people mentioned in a post that has just gone live."""
    if post.mentions:
        name = actor.name if actor else "Someone"
        notify(db, post.company_id, post.mentions, kind="mention", title=f"{name} mentioned you in a post",
               body=content.plain(post.body)[:200], link=f"/dashboard/buzz?post={post.uuid}",
               actor_employee_id=actor.employee_id if actor else None, actor_name=name,
               skip={actor.employee_id} if actor and actor.employee_id else None)


def update_post(db: Session, actor: Actor, post: BuzzPost, data: dict) -> BuzzPost:
    if not _is_author(post, actor):
        raise Forbidden("Only the author can edit this post.")
    if post.kind not in ("post", "poll"):
        raise BadRequest("This post can't be edited.")
    buzz = cfg.section(db, actor.company_id, "buzz")
    window = int(buzz.get("edit_window_minutes") or 0)
    if window and post.created_at and now() - post.created_at > timedelta(minutes=window):
        raise BadRequest(f"Posts can only be edited within {window} minutes.")
    old_mentions = set(post.mentions or [])
    if "body" in data:
        body = _clean_body(data["body"], buzz)
        post.body = body
        post.mentions = _mentioned_ids(db, actor.company_id, body)
        post.hashtags = content.hashtags(body)
    if "attachments" in data:
        post.attachments = content.files_for(db, actor.company_id, data["attachments"]) if data["attachments"] else []
    if "link_preview" in data:
        post.link_preview = _clean_preview(db, actor.company_id, data["link_preview"])
    if "audience" in data:
        post.audience = aud.normalize(data["audience"])
    if "allow_comments" in data:
        post.allow_comments = bool(data["allow_comments"])
    if not post.body and not post.attachments and post.kind != "poll":
        raise BadRequest("Say something, or add a photo.")
    post.edited_at = now()
    new = set(post.mentions or []) - old_mentions
    if new and post.status == "published":
        notify(db, post.company_id, new, kind="mention", title=f"{actor.name} mentioned you in a post",
               body=content.plain(post.body)[:200], link=f"/dashboard/buzz?post={post.uuid}",
               actor_employee_id=actor.employee_id, actor_name=actor.name, skip={actor.employee_id})
    return post


def delete_post(db: Session, actor: Actor, post: BuzzPost) -> None:
    mod = is_moderator(db, actor)
    if not (_is_author(post, actor) or mod):
        raise Forbidden("Only the author or a moderator can delete this post.")
    post.deleted_at = now()
    post.pinned_at = None
    audit.event(db, "buzz.post_deleted", "buzz_posts", post.id,
                {"by_moderator": mod and not _is_author(post, actor), "kind": post.kind},
                entity_uuid=str(post.uuid), entity_label=content.plain(post.body)[:120] or post.kind)


def set_pinned(db: Session, actor: Actor, post: BuzzPost, pinned: bool) -> None:
    if not is_moderator(db, actor):
        raise Forbidden("Only moderators can pin posts.")
    if pinned and post.status != "published":
        raise BadRequest("Only published posts can be pinned.")
    post.pinned_at = now() if pinned else None
    post.pinned_by_name = actor.name if pinned else None
    audit.event(db, "buzz.post_pinned" if pinned else "buzz.post_unpinned", "buzz_posts", post.id, {},
                entity_uuid=str(post.uuid), entity_label=content.plain(post.body)[:120] or post.kind)


def set_hidden(db: Session, actor: Actor, post: BuzzPost, hidden: bool, reason: str | None = None) -> None:
    if not is_moderator(db, actor):
        raise Forbidden("Only moderators can hide posts.")
    if hidden:
        post.status, post.hidden_reason, post.hidden_by_name, post.pinned_at = "hidden", (reason or "").strip()[:500] or None, actor.name, None
        if post.author_employee_id and not post.is_system:
            notify(db, post.company_id, [post.author_employee_id], kind="moderation",
                   title="A moderator hid your post", body=post.hidden_reason or "It went against the Buzz guidelines.",
                   link=f"/dashboard/buzz?post={post.uuid}", actor_name=actor.name)
    else:
        post.status, post.hidden_reason, post.hidden_by_name = "published", None, None
    db.query(BuzzReport).filter(BuzzReport.company_id == post.company_id, BuzzReport.post_id == post.id,
                                BuzzReport.status == "open").update(
        {"status": "actioned" if hidden else "dismissed", "resolved_by_name": actor.name, "resolved_at": now()},
        synchronize_session=False)
    audit.event(db, "buzz.post_hidden" if hidden else "buzz.post_restored", "buzz_posts", post.id,
                {"reason": post.hidden_reason}, entity_uuid=str(post.uuid), entity_label=content.plain(post.body)[:120] or post.kind)


# ── Reactions ───────────────────────────────────────────────────────

def react(db: Session, actor: Actor, post: BuzzPost, emoji: str | None, comment: BuzzComment | None = None) -> str | None:
    """Set, switch or remove (same emoji again, or None) the person's reaction."""
    allowed = cfg.section(db, actor.company_id, "buzz").get("reactions") or cfg.ALL_REACTIONS
    if emoji is not None and emoji not in allowed and emoji not in cfg.ALL_REACTIONS:
        raise BadRequest("That reaction isn't available.")
    q = db.query(BuzzReaction).filter(BuzzReaction.company_id == actor.company_id, BuzzReaction.post_id == post.id,
                                      BuzzReaction.actor_key == actor.key)
    q = q.filter(BuzzReaction.comment_id == comment.id) if comment else q.filter(BuzzReaction.comment_id.is_(None))
    current = q.first()
    if current is not None and (emoji is None or current.emoji == emoji):
        db.delete(current)
        return None
    if emoji is None:
        return None
    if current is not None:
        current.emoji = emoji
    else:
        db.add(BuzzReaction(company_id=actor.company_id, post_id=post.id, comment_id=comment.id if comment else None,
                            actor_key=actor.key, employee_id=actor.employee_id, emoji=emoji))
        owner_id = comment.author_employee_id if comment else (post.subject_employee_id if post.is_system else post.author_employee_id)
        if owner_id and owner_id != actor.employee_id:
            what = "your comment" if comment else ("your celebration" if post.is_system else "your post")
            key = f"react:{'c' if comment else 'p'}:{comment.id if comment else post.id}"
            count = db.query(func.count(BuzzReaction.id)).filter(
                BuzzReaction.post_id == post.id,
                BuzzReaction.comment_id == comment.id if comment else BuzzReaction.comment_id.is_(None)).scalar() or 0
            others = f" and {count} other{'s' if count > 1 else ''}" if count else ""
            notify(db, actor.company_id, [owner_id], kind="reaction", title=f"{actor.name}{others} reacted {emoji} to {what}",
                   link=f"/dashboard/buzz?post={post.uuid}", actor_employee_id=actor.employee_id,
                   actor_name=actor.name, group_key=key)
    db.flush()
    return emoji


def reactors(db: Session, company_id: int, post: BuzzPost, comment: BuzzComment | None = None) -> list[dict]:
    q = db.query(BuzzReaction).filter(BuzzReaction.company_id == company_id, BuzzReaction.post_id == post.id)
    q = q.filter(BuzzReaction.comment_id == comment.id) if comment else q.filter(BuzzReaction.comment_id.is_(None))
    rows = q.order_by(BuzzReaction.created_at.desc()).limit(500).all()
    emps = people.load(db, company_id, [r.employee_id for r in rows])
    owner = people.owner_name(db, company_id) if any(r.employee_id is None for r in rows) else None
    return [{"emoji": r.emoji, "person": people.card(emps.get(r.employee_id), owner=owner if r.employee_id is None else None),
             "at": r.created_at} for r in rows if r.employee_id is None or r.employee_id in emps]


# ── Comments ────────────────────────────────────────────────────────

def _comment(db: Session, company_id: int, post: BuzzPost, uuid) -> BuzzComment:
    row = db.query(BuzzComment).filter(BuzzComment.company_id == company_id, BuzzComment.post_id == post.id,
                                       BuzzComment.uuid == uuid, BuzzComment.deleted_at.is_(None)).first()
    if row is None:
        raise NotFound("Comment")
    return row


def add_comment(db: Session, actor: Actor, post: BuzzPost, body: str, parent_uuid=None) -> BuzzComment:
    buzz = cfg.section(db, actor.company_id, "buzz")
    if not buzz.get("allow_comments", True) or not post.allow_comments:
        raise BadRequest("Comments are closed on this post.")
    if post.status != "published":
        raise BadRequest("You can comment once the post is published.")
    body = (body or "").strip()
    if not body:
        raise BadRequest("Write a comment first.")
    if len(body) > 2000:
        raise BadRequest("Comments can be up to 2,000 characters.")
    content.check_words(content.plain(body), buzz.get("blocked_words") or [])
    parent = None
    if parent_uuid:
        parent = _comment(db, actor.company_id, post, parent_uuid)
        if parent.parent_id:  # keep threads two levels deep: reply to the thread's root
            parent = db.query(BuzzComment).filter(BuzzComment.id == parent.parent_id).first() or parent
    c = BuzzComment(company_id=actor.company_id, post_id=post.id, parent_id=parent.id if parent else None,
                    author_employee_id=actor.employee_id, body=body, mentions=_mentioned_ids(db, actor.company_id, body))
    db.add(c)
    db.flush()
    link = f"/dashboard/buzz?post={post.uuid}"
    excerpt = content.plain(body)[:200]
    skip = {actor.employee_id} if actor.employee_id else set()
    told: set = set()
    if c.mentions:
        notify(db, actor.company_id, c.mentions, kind="mention", title=f"{actor.name} mentioned you in a comment",
               body=excerpt, link=link, actor_employee_id=actor.employee_id, actor_name=actor.name, skip=skip)
        told |= set(c.mentions)
    if parent and parent.author_employee_id and parent.author_employee_id not in told | skip:
        notify(db, actor.company_id, [parent.author_employee_id], kind="reply", title=f"{actor.name} replied to your comment",
               body=excerpt, link=link, actor_employee_id=actor.employee_id, actor_name=actor.name)
        told.add(parent.author_employee_id)
    owner_id = post.subject_employee_id if post.is_system else post.author_employee_id
    if owner_id and owner_id not in told | skip:
        what = {"birthday": "wished you a happy birthday", "work_anniversary": "congratulated you on your work anniversary",
                "welcome": "welcomed you", "wedding_anniversary": "wished you a happy wedding anniversary",
                "kudos": "commented on your kudos"}.get(post.kind, "commented on your post")
        notify(db, actor.company_id, [owner_id], kind="comment", title=f"{actor.name} {what}", body=excerpt, link=link,
               actor_employee_id=actor.employee_id, actor_name=actor.name)
    elif owner_id is None and not post.is_system and post.author_employee_id is None and not actor.is_owner:
        notify(db, actor.company_id, [None], kind="comment", title=f"{actor.name} commented on your post", body=excerpt,
               link=link, actor_employee_id=actor.employee_id, actor_name=actor.name)
    return c


def update_comment(db: Session, actor: Actor, comment: BuzzComment, body: str) -> BuzzComment:
    if not _is_author(comment, actor):
        raise Forbidden("Only the author can edit this comment.")
    body = (body or "").strip()
    if not body:
        raise BadRequest("Write a comment first.")
    content.check_words(content.plain(body), cfg.section(db, actor.company_id, "buzz").get("blocked_words") or [])
    comment.body = body[:2000]
    comment.mentions = _mentioned_ids(db, actor.company_id, body)
    comment.edited_at = now()
    return comment


def delete_comment(db: Session, actor: Actor, comment: BuzzComment) -> None:
    if not (_is_author(comment, actor) or is_moderator(db, actor)):
        raise Forbidden("Only the author or a moderator can delete this comment.")
    comment.deleted_at = now()
    audit.event(db, "buzz.comment_deleted", "buzz_comments", comment.id, {}, entity_uuid=str(comment.uuid),
                entity_label=content.plain(comment.body)[:120])


def comments_for(db: Session, actor: Actor, post: BuzzPost) -> list[dict]:
    rows = (db.query(BuzzComment).filter(BuzzComment.company_id == actor.company_id, BuzzComment.post_id == post.id,
                                         BuzzComment.deleted_at.is_(None), BuzzComment.status == "published")
            .order_by(BuzzComment.created_at).all())
    return _comment_tree(db, actor, rows)


def _comment_tree(db: Session, actor: Actor, rows: list[BuzzComment]) -> list[dict]:
    if not rows:
        return []
    ids = [c.id for c in rows]
    emps = people.load(db, actor.company_id, [c.author_employee_id for c in rows])
    owner = people.owner_name(db, actor.company_id)
    counts: dict[int, dict] = {}
    mine: dict[int, str] = {}
    for cid, emoji, key in db.query(BuzzReaction.comment_id, BuzzReaction.emoji, BuzzReaction.actor_key).filter(
            BuzzReaction.company_id == actor.company_id, BuzzReaction.comment_id.in_(ids)).all():
        counts.setdefault(cid, {}).setdefault(emoji, 0)
        counts[cid][emoji] += 1
        if key == actor.key:
            mine[cid] = emoji
    mod = is_moderator(db, actor)
    out, by_id = [], {}
    for c in rows:
        d = {
            "uuid": str(c.uuid), "body": c.body, "created_at": c.created_at, "edited": c.edited_at is not None,
            "author": people.card(emps.get(c.author_employee_id), owner=None if c.author_employee_id else owner),
            "reactions": _reaction_list(counts.get(c.id, {})), "my_reaction": mine.get(c.id),
            "can_edit": _is_author(c, actor), "can_delete": _is_author(c, actor) or mod, "replies": [],
        }
        by_id[c.id] = d
        if c.parent_id and c.parent_id in by_id:
            by_id[c.parent_id]["replies"].append(d)
        else:
            out.append(d)
    return out


def _reaction_list(counts: dict) -> list[dict]:
    return [{"emoji": e, "count": n} for e, n in sorted(counts.items(), key=lambda kv: -kv[1])]


# ── Polls ───────────────────────────────────────────────────────────

def vote(db: Session, actor: Actor, post: BuzzPost, option_uuids: list) -> None:
    if post.kind != "poll" or not post.poll:
        raise BadRequest("This post is not a poll.")
    closes = post.poll.get("closes_at")
    if closes and datetime.fromisoformat(closes) <= now():
        raise BadRequest("This poll has closed.")
    options = {str(o.uuid): o for o in post.poll_options}
    chosen = [options[str(u)] for u in option_uuids if str(u) in options]
    if len(chosen) != len(option_uuids):
        raise BadRequest("That option is not part of this poll.")
    if not post.poll.get("multiple") and len(chosen) > 1:
        raise BadRequest("Pick one option.")
    db.query(BuzzPollVote).filter(BuzzPollVote.company_id == actor.company_id, BuzzPollVote.post_id == post.id,
                                  BuzzPollVote.actor_key == actor.key).delete(synchronize_session=False)
    for o in chosen:
        db.add(BuzzPollVote(company_id=actor.company_id, post_id=post.id, option_id=o.id, actor_key=actor.key,
                            employee_id=actor.employee_id))
    db.flush()


def poll_voters(db: Session, company_id: int, post: BuzzPost, option_uuid) -> list[dict]:
    if not (post.poll or {}).get("show_voters", True):
        raise Forbidden("Votes on this poll are private.")
    opt = next((o for o in post.poll_options if str(o.uuid) == str(option_uuid)), None)
    if opt is None:
        raise NotFound("Option")
    rows = db.query(BuzzPollVote).filter(BuzzPollVote.company_id == company_id, BuzzPollVote.option_id == opt.id).all()
    emps = people.load(db, company_id, [r.employee_id for r in rows])
    owner = people.owner_name(db, company_id)
    return [people.card(emps.get(r.employee_id), owner=None if r.employee_id else owner) for r in rows
            if r.employee_id is None or r.employee_id in emps]


# ── Reports ─────────────────────────────────────────────────────────

def report(db: Session, actor: Actor, post: BuzzPost, reason: str, note: str | None, comment: BuzzComment | None = None) -> BuzzReport:
    if reason not in REPORT_REASONS:
        raise BadRequest("Choose a reason.")
    existing = db.query(BuzzReport).filter(BuzzReport.company_id == actor.company_id, BuzzReport.post_id == post.id,
                                           BuzzReport.reporter_key == actor.key, BuzzReport.status == "open")
    existing = existing.filter(BuzzReport.comment_id == comment.id) if comment else existing.filter(BuzzReport.comment_id.is_(None))
    existing = existing.first()
    if existing:
        raise BadRequest("You have already reported this. A moderator will look at it.")
    r = BuzzReport(company_id=actor.company_id, post_id=post.id, comment_id=comment.id if comment else None,
                   reporter_key=actor.key, reporter_employee_id=actor.employee_id, reason=reason,
                   note=(note or "").strip()[:1000] or None)
    db.add(r)
    db.flush()
    limit = int(cfg.section(db, actor.company_id, "buzz").get("auto_hide_reports") or 0)
    if not comment and limit:
        n = db.query(func.count(func.distinct(BuzzReport.reporter_key))).filter(
            BuzzReport.company_id == actor.company_id, BuzzReport.post_id == post.id, BuzzReport.status == "open").scalar()
        if n >= limit and post.status == "published":
            post.status, post.hidden_reason, post.hidden_by_name, post.pinned_at = "hidden", "Hidden after several reports, waiting for review", "Automatic", None
            audit.event(db, "buzz.post_auto_hidden", "buzz_posts", post.id, {"reports": n}, entity_uuid=str(post.uuid))
    mods = moderator_ids(db, actor.company_id)
    notify(db, actor.company_id, mods + [None], kind="report", title="A Buzz post was reported",
           body=REPORT_REASONS[reason], link="/dashboard/buzz?tab=moderation", group_key="buzz-reports")
    return r


def moderator_ids(db: Session, company_id: int) -> list[int]:
    from modules.roles.models import Role
    roles = [r.role_name for r in db.query(Role).filter(Role.company_id == company_id).all()
             if r.role_name == "admin" or "engage.buzz.moderate" in (r.permissions or [])]
    if not roles:
        return []
    return [i for (i,) in db.query(Employee.id).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                                        Employee.role.in_(roles)).all()]


def moderation_queue(db: Session, actor: Actor) -> dict:
    if not is_moderator(db, actor):
        raise Forbidden()
    reports = (db.query(BuzzReport).filter(BuzzReport.company_id == actor.company_id, BuzzReport.status == "open")
               .order_by(BuzzReport.created_at.desc()).limit(200).all())
    post_ids = {r.post_id for r in reports}
    hidden = (db.query(BuzzPost).filter(BuzzPost.company_id == actor.company_id, BuzzPost.deleted_at.is_(None),
                                        or_(BuzzPost.status.in_(("hidden", "pending")), BuzzPost.id.in_(post_ids or {0})))
              .order_by(BuzzPost.created_at.desc()).limit(100).all())
    by_post: dict[int, list] = {}
    reporters = people.load(db, actor.company_id, [r.reporter_employee_id for r in reports])
    for r in reports:
        by_post.setdefault(r.post_id, []).append({
            "uuid": str(r.uuid), "reason": REPORT_REASONS.get(r.reason, r.reason), "note": r.note, "at": r.created_at,
            "on_comment": r.comment_id is not None,
            "by": people.full_name(reporters.get(r.reporter_employee_id)) or "Company owner",
        })
    posts = serialize(db, actor, hidden)
    for p, row in zip(posts, hidden):
        p["reports"] = by_post.get(row.id, [])
    return {"items": posts, "open_reports": len(reports)}


def dismiss_reports(db: Session, actor: Actor, post: BuzzPost) -> int:
    if not is_moderator(db, actor):
        raise Forbidden()
    n = db.query(BuzzReport).filter(BuzzReport.company_id == actor.company_id, BuzzReport.post_id == post.id,
                                    BuzzReport.status == "open").update(
        {"status": "dismissed", "resolved_by_name": actor.name, "resolved_at": now()}, synchronize_session=False)
    if post.status == "hidden" and post.hidden_by_name == "Automatic":
        post.status, post.hidden_reason, post.hidden_by_name = "published", None, None
    audit.event(db, "buzz.reports_dismissed", "buzz_posts", post.id, {"count": n}, entity_uuid=str(post.uuid))
    return n


# ── The feed ────────────────────────────────────────────────────────

def _base(db: Session, actor: Actor, dims):
    mine = BuzzPost.author_employee_id == actor.employee_id if actor.employee_id else and_(
        BuzzPost.author_employee_id.is_(None), BuzzPost.is_system.is_(False))
    visible = and_(BuzzPost.status == "published", aud.sql_condition(BuzzPost.audience, dims))
    return db.query(BuzzPost).filter(BuzzPost.company_id == actor.company_id, BuzzPost.deleted_at.is_(None),
                                     or_(visible, and_(mine, BuzzPost.status == "pending")))


def list_feed(db: Session, actor: Actor, *, filter_: str = "all", hashtag: str | None = None, author=None,
              about=None, q: str | None = None, before: str | None = None, limit: int = PAGE) -> dict:
    # The feed respects the audience for everyone with an employee record (moderators
    # included); the company owner sees every published post.
    dims = aud.viewer_dims(db, actor.company_id, actor.viewer_id)
    query = _base(db, actor, dims)
    kinds = FILTERS.get(filter_ or "all")
    if filter_ == "mine":
        query = query.filter(BuzzPost.author_employee_id == actor.employee_id, BuzzPost.is_system.is_(False)) \
            if actor.employee_id else query.filter(BuzzPost.author_employee_id.is_(None), BuzzPost.is_system.is_(False))
    elif kinds:
        query = query.filter(BuzzPost.kind.in_(kinds))
    if hashtag:
        query = query.filter(BuzzPost.hashtags.contains([hashtag.lower().lstrip("#")]))
    if author:
        emp = people.by_uuid(db, actor.company_id, [author]).get(str(author))
        query = query.filter(BuzzPost.author_employee_id == (emp.id if emp else -1), BuzzPost.is_system.is_(False))
    if about:
        emp = people.by_uuid(db, actor.company_id, [about]).get(str(about))
        eid = emp.id if emp else -1
        kudos_ids = db.query(KudosRecipient.kudos_id).filter(KudosRecipient.company_id == actor.company_id,
                                                             KudosRecipient.employee_id == eid)
        query = query.filter(or_(BuzzPost.subject_employee_id == eid, BuzzPost.kudos_id.in_(kudos_ids)))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(BuzzPost.body.ilike(like))
    pinned = []
    if not before and filter_ in (None, "", "all") and not (hashtag or author or about or q):
        pinned_rows = query.filter(BuzzPost.pinned_at.isnot(None)).order_by(BuzzPost.pinned_at.desc()).limit(5).all()
        pinned = serialize(db, actor, pinned_rows)
        query = query.filter(BuzzPost.pinned_at.is_(None))
    if before:
        try:
            ts, _, pid = before.partition("_")
            cursor_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            query = query.filter(or_(BuzzPost.created_at < cursor_ts,
                                     and_(BuzzPost.created_at == cursor_ts, BuzzPost.id < int(pid or 0))))
        except ValueError:
            raise BadRequest("Bad page cursor.")
    rows = query.order_by(BuzzPost.created_at.desc(), BuzzPost.id.desc()).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = None
    if more and rows:
        last = rows[-1]
        cursor = f"{last.created_at.isoformat()}_{last.id}"
    return {"pinned": pinned, "items": serialize(db, actor, rows), "next": cursor,
            "latest": rows[0].created_at if rows and not before else None}


def updates_since(db: Session, actor: Actor, since: datetime) -> dict:
    dims = aud.viewer_dims(db, actor.company_id, actor.viewer_id)
    q = _base(db, actor, dims).filter(BuzzPost.created_at > since, BuzzPost.status == "published")
    if actor.employee_id:
        q = q.filter(or_(BuzzPost.author_employee_id.is_(None), BuzzPost.author_employee_id != actor.employee_id))
    rows = q.order_by(BuzzPost.created_at.desc()).limit(20).all()
    authors = people.load(db, actor.company_id, [r.author_employee_id for r in rows])
    return {"count": len(rows), "authors": [people.card(authors[r.author_employee_id]) for r in rows
                                            if r.author_employee_id in authors][:3]}


def stats(db: Session, actor: Actor, uuids: list) -> dict:
    """Fresh reaction, comment and vote numbers for posts already on screen."""
    rows = db.query(BuzzPost).filter(BuzzPost.company_id == actor.company_id, BuzzPost.uuid.in_(uuids[:50])).all()
    out = {}
    for p in serialize(db, actor, rows, light=True):
        out[p["uuid"]] = p
    return out


def trending(db: Session, actor: Actor, days: int = 30, limit: int = 8) -> list[dict]:
    since = now() - timedelta(days=days)
    tag = func.jsonb_array_elements_text(BuzzPost.hashtags).label("tag")
    sub = (db.query(tag).filter(BuzzPost.company_id == actor.company_id, BuzzPost.deleted_at.is_(None),
                                BuzzPost.status == "published", BuzzPost.created_at >= since)
           .subquery())
    rows = db.query(sub.c.tag, func.count()).group_by(sub.c.tag).order_by(func.count().desc()).limit(limit).all()
    return [{"tag": t, "count": n} for t, n in rows]


# ── Serialisation ───────────────────────────────────────────────────

def serialize(db: Session, actor: Actor, posts: list[BuzzPost], light: bool = False) -> list[dict]:
    if not posts:
        return []
    cid = actor.company_id
    ids = [p.id for p in posts]
    emps = people.load(db, cid, [p.author_employee_id for p in posts] + [p.subject_employee_id for p in posts])
    owner = people.owner_name(db, cid)
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == cid).first()
    mod = is_moderator(db, actor)

    reactions: dict[int, dict] = {}
    mine: dict[int, str] = {}
    recent: dict[int, list] = {}
    for pid, emoji, key, eid in (db.query(BuzzReaction.post_id, BuzzReaction.emoji, BuzzReaction.actor_key, BuzzReaction.employee_id)
                                 .filter(BuzzReaction.company_id == cid, BuzzReaction.post_id.in_(ids),
                                         BuzzReaction.comment_id.is_(None))
                                 .order_by(BuzzReaction.created_at.desc()).all()):
        reactions.setdefault(pid, {}).setdefault(emoji, 0)
        reactions[pid][emoji] += 1
        if key == actor.key:
            mine[pid] = emoji
        if eid and len(recent.setdefault(pid, [])) < 3:
            recent[pid].append(eid)
    comment_counts = dict(db.query(BuzzComment.post_id, func.count(BuzzComment.id)).filter(
        BuzzComment.company_id == cid, BuzzComment.post_id.in_(ids), BuzzComment.deleted_at.is_(None),
        BuzzComment.status == "published").group_by(BuzzComment.post_id).all())
    reactor_people = people.load(db, cid, [e for lst in recent.values() for e in lst])

    # latest two top-level comments per post (and their replies count) for the card
    latest: dict[int, list] = {}
    if not light:
        rows = (db.query(BuzzComment).filter(BuzzComment.company_id == cid, BuzzComment.post_id.in_(ids),
                                             BuzzComment.deleted_at.is_(None), BuzzComment.status == "published")
                .order_by(BuzzComment.created_at.desc()).limit(len(ids) * 30).all())
        for c in rows:
            if c.parent_id is None and len(latest.setdefault(c.post_id, [])) < 2:
                latest[c.post_id].append(c)

    # polls
    poll_ids = [p.id for p in posts if p.kind == "poll"]
    votes: dict[int, int] = {}
    my_votes: set = set()
    voters: dict[int, int] = {}
    if poll_ids:
        for oid, n in db.query(BuzzPollVote.option_id, func.count(BuzzPollVote.id)).filter(
                BuzzPollVote.company_id == cid, BuzzPollVote.post_id.in_(poll_ids)).group_by(BuzzPollVote.option_id).all():
            votes[oid] = n
        my_votes = {oid for (oid,) in db.query(BuzzPollVote.option_id).filter(
            BuzzPollVote.company_id == cid, BuzzPollVote.post_id.in_(poll_ids), BuzzPollVote.actor_key == actor.key).all()}
        for pid, n in db.query(BuzzPollVote.post_id, func.count(func.distinct(BuzzPollVote.actor_key))).filter(
                BuzzPollVote.company_id == cid, BuzzPollVote.post_id.in_(poll_ids)).group_by(BuzzPollVote.post_id).all():
            voters[pid] = n

    # kudos, announcements and surveys behind their feed cards
    kudos_map, ann_map, survey_map = {}, {}, {}
    k_ids = [p.kudos_id for p in posts if p.kudos_id]
    if k_ids:
        from modules.engagement import kudos as kudos_mod
        for k in db.query(Kudos).options(joinedload(Kudos.value), joinedload(Kudos.recipients)).filter(
                Kudos.company_id == cid, Kudos.id.in_(k_ids)).all():
            kudos_map[k.id] = kudos_mod.card(db, cid, k)
    a_ids = [p.announcement_id for p in posts if p.announcement_id]
    if a_ids:
        from modules.engagement import announcements as ann_mod
        receipts = {r.announcement_id: r for r in db.query(AnnouncementReceipt).filter(
            AnnouncementReceipt.company_id == cid, AnnouncementReceipt.announcement_id.in_(a_ids),
            AnnouncementReceipt.employee_id == actor.employee_id).all()} if actor.employee_id else {}
        for a in db.query(Announcement).filter(Announcement.company_id == cid, Announcement.id.in_(a_ids)).all():
            ann_map[a.id] = ann_mod.card(a, receipts.get(a.id), actor)
    s_ids = [p.survey_id for p in posts if p.survey_id]
    if s_ids:
        for s in db.query(Survey).filter(Survey.company_id == cid, Survey.id.in_(s_ids)).all():
            run = (db.query(SurveyRun).filter(SurveyRun.survey_id == s.id).order_by(SurveyRun.number.desc()).first())
            part = None
            if run is not None and actor.employee_id:
                part = db.query(SurveyParticipation).filter(SurveyParticipation.run_id == run.id,
                                                            SurveyParticipation.employee_id == actor.employee_id).first()
            survey_map[s.id] = {
                "uuid": str(s.uuid), "title": s.title, "kind": s.kind, "identity": s.identity,
                "description": s.description, "questions": len(s.questions), "mandatory": s.mandatory,
                "closes_at": run.closes_at if run else None, "open": bool(run and run.status == "open"),
                "invited": part is not None, "answered": bool(part and part.status == "submitted"),
            }

    out = []
    for p in posts:
        author = emps.get(p.author_employee_id)
        if p.is_system:
            author_card = {"uuid": None, "id": None, "name": f"{company.company_name if company else 'Company'} Cheers",
                           "title": "Celebrating our people", "department": None, "image": None, "system": True}
        else:
            author_card = people.card(author, owner=None if p.author_employee_id else owner)
        poll = None
        if p.kind == "poll" and p.poll is not None:
            total = voters.get(p.id, 0)
            closes = p.poll.get("closes_at")
            closed = bool(closes and datetime.fromisoformat(closes) <= now())
            poll = {
                "multiple": p.poll.get("multiple", False), "closes_at": closes, "closed": closed,
                "show_voters": p.poll.get("show_voters", True), "voters": total,
                "options": [{"uuid": str(o.uuid), "label": o.label, "votes": votes.get(o.id, 0),
                             "mine": o.id in my_votes} for o in p.poll_options],
                "voted": any(o.id in my_votes for o in p.poll_options),
            }
        is_author = _is_author(p, actor) and (actor.employee_id is not None or p.author_employee_id is None)
        d = {
            "uuid": str(p.uuid), "kind": p.kind, "status": p.status,
            "reactions": _reaction_list(reactions.get(p.id, {})), "reaction_total": sum(reactions.get(p.id, {}).values()),
            "my_reaction": mine.get(p.id), "comment_count": comment_counts.get(p.id, 0), "poll": poll,
            "recent_reactors": [people.card(reactor_people[e]) for e in recent.get(p.id, []) if e in reactor_people],
        }
        if not light:
            subject = emps.get(p.subject_employee_id)
            d.update({
                "author": author_card, "body": p.body, "hashtags": p.hashtags or [],
                "attachments": content.with_urls(p.attachments),
                "link_preview": ({**p.link_preview, "image": content.file_card(p.link_preview["image"])
                                  if p.link_preview.get("image") else None} if p.link_preview else None),
                "audience": p.audience, "audience_label": aud.describe(db, cid, p.audience) if not aud.is_everyone(p.audience) else None,
                "subject": people.card(subject) if subject else None, "celebration_date": p.celebration_date,
                "years": p.years, "theme": p.theme, "kudos": kudos_map.get(p.kudos_id),
                "announcement": ann_map.get(p.announcement_id), "survey": survey_map.get(p.survey_id),
                "allow_comments": p.allow_comments, "pinned": p.pinned_at is not None, "pinned_by": p.pinned_by_name,
                "hidden_reason": p.hidden_reason if (mod or is_author) else None,
                "created_at": p.created_at, "edited": p.edited_at is not None,
                "comments": _comment_tree(db, actor, list(reversed(latest.get(p.id, [])))),
                "can_edit": is_author and p.kind in ("post", "poll"),
                "can_delete": (is_author and p.kind in ("post", "poll")) or mod,
                "can_pin": mod and p.status == "published", "can_moderate": mod,
                "is_mine": is_author,
            })
        out.append(d)
    return out
