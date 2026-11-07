"""Engagement API: Buzz feed, celebrations, kudos, announcements, surveys, kiosks and
settings under /api/v1/engagement, plus two public addresses (signed files and kiosk
screens) under /api/v1/public."""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from core.db import bypass_rls, get_db
from core.errors import BadRequest, Forbidden
from core.security import _get_role_permissions, get_current_user
from modules.engagement import announcements as ann
from modules.engagement import audience as aud
from modules.engagement import celebrations, content, feed, jobs, kiosk, kudos, people, surveys
from modules.engagement import settings as cfg
from modules.engagement.models import EngagementFile
from modules.engagement.people import Actor
from modules.engagement.schemas import (AnnouncementIn, AudiencePreviewIn, CelebrationPrefs, CommentIn, CommentUpdate,
                                        EmailPreviewIn, ExtendIn, HideIn, KioskIn, KudosIn, KudosValueIn, PinIn,
                                        PostIn, PostUpdate, ReactIn, ReorderIn, ReportIn, SurveyAnswerIn, SurveyIn,
                                        VoteIn, WishIn, dump)

router = APIRouter(prefix="/engagement", tags=["Engagement"], lifespan=jobs.lifespan)
public_router = APIRouter(prefix="/public", tags=["Engagement (public)"])


# ── Who is asking ───────────────────────────────────────────────────

class Ctx:
    def __init__(self, db: Session, user: dict):
        self.db, self.user = db, user
        self.actor: Actor = people.actor_from_user(db, user)
        self._perms = None

    @property
    def perms(self) -> list[str]:
        if self._perms is None:
            self._perms = _get_role_permissions(self.user["company_id"], self.user["role"], self.db)
        return self._perms

    def can(self, perm: str) -> bool:
        return self.actor.is_owner or self.user.get("role") == "admin" or perm in self.perms

    def need(self, perm: str) -> None:
        if not self.can(perm):
            raise Forbidden("You don't have access to this.")


def ctx(db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> Ctx:
    return Ctx(db, user)


# ── Meta and settings ───────────────────────────────────────────────

@router.get("/meta")
def meta(c: Ctx = Depends(ctx)):
    conf = cfg.get(c.db, c.actor.company_id)
    return {
        "me": {"name": c.actor.name, "employee_uuid": None if c.actor.employee_id is None else str(
            people.load(c.db, c.actor.company_id, [c.actor.employee_id])[c.actor.employee_id].uuid),
               "is_owner": c.actor.is_owner,
               "image": None if c.actor.employee_id is None else people.load(c.db, c.actor.company_id, [c.actor.employee_id])[c.actor.employee_id].profile_image_url},
        "reactions": conf["buzz"]["reactions"], "all_reactions": cfg.ALL_REACTIONS,
        "can_post": feed.can_post(c.db, c.actor, conf["buzz"]),
        "allow_polls": conf["buzz"]["allow_polls"], "allow_attachments": conf["buzz"]["allow_attachments"],
        "allow_audience": conf["buzz"]["allow_audience"] or feed.is_moderator(c.db, c.actor),
        "allow_comments": conf["buzz"]["allow_comments"],
        "kudos_enabled": conf["kudos"]["enabled"], "show_leaderboard": conf["kudos"]["show_leaderboard"],
        "is_moderator": feed.is_moderator(c.db, c.actor),
        "can_manage_announcements": c.can("engage.announcements.manage"),
        "can_manage_surveys": c.can("engage.surveys.manage"),
        "can_manage_settings": c.can("engage.settings.manage"),
        "report_reasons": feed.REPORT_REASONS,
    }


@router.get("/settings")
def get_settings_all(c: Ctx = Depends(ctx)):
    c.need("engage.settings.manage")
    return {**cfg.get(c.db, c.actor.company_id), "placeholders": cfg.EMAIL_PLACEHOLDERS, "all_reactions": cfg.ALL_REACTIONS}


@router.put("/settings/{section}")
def put_settings(section: str, payload: dict, c: Ctx = Depends(ctx)):
    c.need("engage.settings.manage")
    out = cfg.update(c.db, c.actor.company_id, section, payload)
    c.db.commit()
    return out


@router.post("/settings/celebrations/preview-email")
def preview_email(req: EmailPreviewIn, c: Ctx = Depends(ctx)):
    c.need("engage.settings.manage")
    tpl = {"subject": req.subject, "body": req.body} if req.subject and req.body else None
    return celebrations.preview_email(c.db, c.actor.company_id, req.kind, tpl)


@router.post("/jobs/run")
def run_jobs_now(c: Ctx = Depends(ctx)):
    """Run the engagement jobs for this company now (celebrations ignore the hour)."""
    c.need("engage.settings.manage")
    from modules.company.models import Company
    company = c.db.query(Company).filter(Company.id == c.actor.company_id).first()
    out = jobs.run_company(c.db, company, force_celebrations=True)
    c.db.commit()
    return out


@router.get("/audience/groups")
def audience_groups(c: Ctx = Depends(ctx)):
    return aud.picker_meta(c.db, c.actor.company_id)


@router.get("/audience/options")
def audience_options(key: str, q: str | None = None, c: Ctx = Depends(ctx)):
    return aud.search_options(c.db, c.actor.company_id, key, q)


@router.post("/audience/preview")
def audience_preview(req: AudiencePreviewIn, c: Ctx = Depends(ctx)):
    a = aud.normalize(req.audience.as_dict() if req.audience else None)
    return {"count": len(aud.resolve(c.db, c.actor.company_id, a)), "label": aud.describe(c.db, c.actor.company_id, a)}


@router.get("/people")
def people_search(q: str | None = None, limit: int = Query(8, ge=1, le=30), c: Ctx = Depends(ctx)):
    """Small person cards for @mentions and kudos pickers."""
    from sqlalchemy import or_
    from sqlalchemy.orm import joinedload
    from modules.employees.models import Employee
    query = (c.db.query(Employee).options(joinedload(Employee.designation), joinedload(Employee.department))
             .filter(Employee.company_id == c.actor.company_id, people.working()))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_((Employee.first_name + " " + Employee.last_name).ilike(like), Employee.first_name.ilike(like),
                                 Employee.last_name.ilike(like)))
    return [people.card(e) for e in query.order_by(Employee.first_name, Employee.last_name).limit(limit).all()]


# ── Files and link previews ─────────────────────────────────────────

@router.post("/files")
async def upload_file(file: UploadFile = File(...), c: Ctx = Depends(ctx)):
    raw = await file.read()
    f = content.store_upload(c.db, c.actor.company_id, c.actor.employee_id, file.filename or "file",
                             file.content_type or "", raw)
    c.db.commit()
    return content.file_card(f)


@router.get("/link-preview")
def get_link_preview(url: str, c: Ctx = Depends(ctx)):
    out = content.link_preview(c.db, c.actor.company_id, url)
    c.db.commit()
    return out or {"url": url, "title": None}


@public_router.get("/engagement/files/{file_uuid}")
def public_file(file_uuid: UUID, s: str = "", thumb: int = 0, db: Session = Depends(get_db)):
    if not content.check_signature(str(file_uuid), s):
        raise HTTPException(status_code=404, detail="File not found")
    with bypass_rls(db):
        f = db.query(EngagementFile).filter(EngagementFile.uuid == file_uuid).first()
    if f is None:
        raise HTTPException(status_code=404, detail="File not found")
    data = f.thumb if thumb and f.thumb else f.data
    mime = ("image/webp" if data[:4] == b"RIFF" else "image/jpeg") if data is f.thumb else f.mime
    disposition = "inline" if f.kind == "image" or f.mime == "application/pdf" else "attachment"
    safe_name = "".join(ch for ch in f.name if ch.isalnum() or ch in "._- ")[:120] or "file"
    return Response(content=data, media_type=mime, headers={
        "Cache-Control": "private, max-age=31536000, immutable",
        "Content-Disposition": f'{disposition}; filename="{safe_name}"',
        "X-Content-Type-Options": "nosniff",
    })


# ── Feed ────────────────────────────────────────────────────────────

@router.get("/feed")
def get_feed(filter: str = "all", tag: str | None = None, author: UUID | None = None, about: UUID | None = None,
             q: str | None = None, before: str | None = None, limit: int = Query(10, ge=1, le=30), c: Ctx = Depends(ctx)):
    return feed.list_feed(c.db, c.actor, filter_=filter, hashtag=tag, author=author, about=about, q=q, before=before, limit=limit)


@router.get("/feed/updates")
def feed_updates(since: datetime, c: Ctx = Depends(ctx)):
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return feed.updates_since(c.db, c.actor, since)


@router.get("/feed/stats")
def feed_stats(ids: str, c: Ctx = Depends(ctx)):
    uuids = []
    for x in ids.split(","):
        try:
            uuids.append(UUID(x.strip()))
        except ValueError:
            continue
    return feed.stats(c.db, c.actor, uuids)


@router.get("/feed/trending")
def feed_trending(c: Ctx = Depends(ctx)):
    return feed.trending(c.db, c.actor)


@router.post("/posts", status_code=201)
def create_post(req: PostIn, c: Ctx = Depends(ctx)):
    data = dump(req)
    if req.poll:
        data["poll"] = req.poll.model_dump()
    post = feed.create_post(c.db, c.actor, data)
    c.db.commit()
    return feed.serialize(c.db, c.actor, [feed._post(c.db, c.actor.company_id, post.uuid)])[0]


@router.get("/posts/{uuid}")
def get_post(uuid: UUID, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    out = feed.serialize(c.db, c.actor, [post])[0]
    out["comments"] = feed.comments_for(c.db, c.actor, post)
    return out


@router.put("/posts/{uuid}")
def update_post(uuid: UUID, req: PostUpdate, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    feed.update_post(c.db, c.actor, post, dump(req))
    c.db.commit()
    return feed.serialize(c.db, c.actor, [post])[0]


@router.delete("/posts/{uuid}")
def delete_post(uuid: UUID, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    feed.delete_post(c.db, c.actor, post)
    c.db.commit()
    return {"detail": "Post deleted"}


@router.post("/posts/{uuid}/pin")
def pin_post(uuid: UUID, req: PinIn, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    feed.set_pinned(c.db, c.actor, post, req.pinned)
    c.db.commit()
    return {"pinned": req.pinned}


@router.post("/posts/{uuid}/hide")
def hide_post(uuid: UUID, req: HideIn, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    feed.set_hidden(c.db, c.actor, post, req.hidden, req.reason)
    c.db.commit()
    return {"hidden": req.hidden}


@router.post("/posts/{uuid}/dismiss-reports")
def dismiss_reports(uuid: UUID, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    n = feed.dismiss_reports(c.db, c.actor, post)
    c.db.commit()
    return {"dismissed": n}


@router.post("/posts/{uuid}/react")
def react_post(uuid: UUID, req: ReactIn, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    mine = feed.react(c.db, c.actor, post, req.emoji)
    c.db.commit()
    return {"my_reaction": mine, **feed.stats(c.db, c.actor, [post.uuid]).get(str(post.uuid), {})}


@router.get("/posts/{uuid}/reactions")
def post_reactions(uuid: UUID, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    return feed.reactors(c.db, c.actor.company_id, post)


@router.get("/posts/{uuid}/comments")
def post_comments(uuid: UUID, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    return feed.comments_for(c.db, c.actor, post)


@router.post("/posts/{uuid}/comments", status_code=201)
def add_comment(uuid: UUID, req: CommentIn, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    feed.add_comment(c.db, c.actor, post, req.body, req.parent)
    c.db.commit()
    return feed.comments_for(c.db, c.actor, post)


@router.put("/posts/{uuid}/comments/{comment_uuid}")
def edit_comment(uuid: UUID, comment_uuid: UUID, req: CommentUpdate, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    feed.update_comment(c.db, c.actor, feed._comment(c.db, c.actor.company_id, post, comment_uuid), req.body)
    c.db.commit()
    return feed.comments_for(c.db, c.actor, post)


@router.delete("/posts/{uuid}/comments/{comment_uuid}")
def remove_comment(uuid: UUID, comment_uuid: UUID, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    feed.delete_comment(c.db, c.actor, feed._comment(c.db, c.actor.company_id, post, comment_uuid))
    c.db.commit()
    return feed.comments_for(c.db, c.actor, post)


@router.post("/posts/{uuid}/comments/{comment_uuid}/react")
def react_comment(uuid: UUID, comment_uuid: UUID, req: ReactIn, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    feed.react(c.db, c.actor, post, req.emoji, feed._comment(c.db, c.actor.company_id, post, comment_uuid))
    c.db.commit()
    return feed.comments_for(c.db, c.actor, post)


@router.get("/posts/{uuid}/comments/{comment_uuid}/reactions")
def comment_reactions(uuid: UUID, comment_uuid: UUID, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    return feed.reactors(c.db, c.actor.company_id, post, feed._comment(c.db, c.actor.company_id, post, comment_uuid))


@router.post("/posts/{uuid}/vote")
def vote(uuid: UUID, req: VoteIn, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    feed.vote(c.db, c.actor, post, req.options)
    c.db.commit()
    return feed.stats(c.db, c.actor, [post.uuid]).get(str(post.uuid))


@router.get("/posts/{uuid}/poll/{option_uuid}/voters")
def poll_voters(uuid: UUID, option_uuid: UUID, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    return feed.poll_voters(c.db, c.actor.company_id, post, option_uuid)


@router.post("/posts/{uuid}/report", status_code=201)
def report_post(uuid: UUID, req: ReportIn, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    comment = feed._comment(c.db, c.actor.company_id, post, req.comment) if req.comment else None
    feed.report(c.db, c.actor, post, req.reason, req.note, comment)
    c.db.commit()
    return {"detail": "Thanks. A moderator will take a look."}


@router.post("/posts/{uuid}/wish")
def wish(uuid: UUID, req: WishIn, c: Ctx = Depends(ctx)):
    post = feed.visible_post(c.db, c.actor, uuid)
    out = celebrations.wish(c.db, c.actor, post, req.message, req.emoji)
    c.db.commit()
    return out


@router.get("/moderation")
def moderation(c: Ctx = Depends(ctx)):
    return feed.moderation_queue(c.db, c.actor)


# ── Celebrations ────────────────────────────────────────────────────

@router.get("/celebrations")
def get_celebrations(days: int | None = Query(None, ge=1, le=60), c: Ctx = Depends(ctx)):
    return celebrations.overview(c.db, c.actor, days)


@router.get("/me/celebrations")
def my_celebration_prefs(c: Ctx = Depends(ctx)):
    return celebrations.my_preferences(c.db, c.actor)


@router.put("/me/celebrations")
def update_celebration_prefs(req: CelebrationPrefs, c: Ctx = Depends(ctx)):
    out = celebrations.update_preferences(c.db, c.actor, req.model_dump(exclude_unset=True))
    c.db.commit()
    return out


# ── Kudos ───────────────────────────────────────────────────────────

@router.get("/kudos/values")
def kudos_values(include_inactive: bool = False, c: Ctx = Depends(ctx)):
    return kudos.list_values(c.db, c.actor.company_id, include_inactive and c.can("engage.settings.manage"))


@router.post("/kudos/values", status_code=201)
def add_kudos_value(req: KudosValueIn, c: Ctx = Depends(ctx)):
    c.need("engage.settings.manage")
    v = kudos.create_value(c.db, c.actor.company_id, req.model_dump(exclude_unset=True))
    c.db.commit()
    return kudos.value_dict(v)


@router.put("/kudos/values/{uuid}")
def edit_kudos_value(uuid: UUID, req: KudosValueIn, c: Ctx = Depends(ctx)):
    c.need("engage.settings.manage")
    v = kudos.update_value(c.db, kudos.get_value(c.db, c.actor.company_id, uuid), req.model_dump(exclude_unset=True))
    c.db.commit()
    return kudos.value_dict(v)


@router.post("/kudos/values/reorder")
def reorder_kudos_values(req: ReorderIn, c: Ctx = Depends(ctx)):
    c.need("engage.settings.manage")
    kudos.reorder_values(c.db, c.actor.company_id, req.uuids)
    c.db.commit()
    return kudos.list_values(c.db, c.actor.company_id, True)


@router.post("/kudos", status_code=201)
def give_kudos(req: KudosIn, c: Ctx = Depends(ctx)):
    k = kudos.give(c.db, c.actor, req.model_dump())
    c.db.commit()
    return kudos.card(c.db, c.actor.company_id, k)


@router.get("/kudos")
def list_kudos(employee: UUID | None = None, mine: str | None = None, value: UUID | None = None,
               page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=60), c: Ctx = Depends(ctx)):
    emp_id = given_by = None
    if employee:
        e = people.by_uuid(c.db, c.actor.company_id, [str(employee)]).get(str(employee))
        emp_id = e.id if e else -1
    if mine == "received":
        emp_id = c.actor.employee_id or -1
    elif mine == "given":
        given_by = c.actor.employee_id or -1
    return kudos.list_kudos(c.db, c.actor.company_id, employee_id=emp_id, given_by=given_by, value_uuid=value,
                            page=page, size=size)


@router.get("/kudos/leaderboard")
def kudos_leaderboard(period: str = "month", offset: int = Query(0, ge=0, le=24), c: Ctx = Depends(ctx)):
    if period not in ("month", "quarter"):
        raise BadRequest("Pick month or quarter.")
    return kudos.leaderboard(c.db, c.actor.company_id, period, offset)


@router.get("/kudos/profile/{employee_uuid}")
def kudos_profile(employee_uuid: UUID, c: Ctx = Depends(ctx)):
    e = people.by_uuid(c.db, c.actor.company_id, [str(employee_uuid)]).get(str(employee_uuid))
    if e is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    return kudos.profile_summary(c.db, c.actor.company_id, e.id)


# ── Announcements ───────────────────────────────────────────────────

@router.get("/announcements")
def list_announcements(scope: str = "current", status: str | None = None, q: str | None = None,
                       page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), c: Ctx = Depends(ctx)):
    if scope == "manage":
        c.need("engage.announcements.manage")
        return ann.list_admin(c.db, c.actor.company_id, status, q, page, size)
    return ann.list_for_reader(c.db, c.actor, scope, page, size)


@router.post("/announcements", status_code=201)
def create_announcement(req: AnnouncementIn, c: Ctx = Depends(ctx)):
    c.need("engage.announcements.manage")
    a = ann.create(c.db, c.actor, dump(req))
    c.db.commit()
    return ann.detail(c.db, c.actor, a, True)


@router.get("/announcements/{uuid}")
def get_announcement(uuid: UUID, c: Ctx = Depends(ctx)):
    manager = c.can("engage.announcements.manage")
    a = ann.for_reader(c.db, c.actor, ann.get(c.db, c.actor.company_id, uuid), manager)
    out = ann.detail(c.db, c.actor, a, manager)
    c.db.commit()
    return out


@router.put("/announcements/{uuid}")
def update_announcement(uuid: UUID, req: AnnouncementIn, c: Ctx = Depends(ctx)):
    c.need("engage.announcements.manage")
    a = ann.update(c.db, c.actor, ann.get(c.db, c.actor.company_id, uuid), dump(req))
    c.db.commit()
    return ann.detail(c.db, c.actor, a, True)


@router.post("/announcements/{uuid}/publish")
def publish_announcement(uuid: UUID, c: Ctx = Depends(ctx)):
    c.need("engage.announcements.manage")
    a = ann.get(c.db, c.actor.company_id, uuid)
    ann.update(c.db, c.actor, a, {"action": "publish"})
    c.db.commit()
    return ann.detail(c.db, c.actor, a, True)


@router.post("/announcements/{uuid}/archive")
def archive_announcement(uuid: UUID, c: Ctx = Depends(ctx)):
    c.need("engage.announcements.manage")
    a = ann.get(c.db, c.actor.company_id, uuid)
    ann.archive(c.db, a)
    c.db.commit()
    return {"detail": "Archived"}


@router.delete("/announcements/{uuid}")
def delete_announcement(uuid: UUID, c: Ctx = Depends(ctx)):
    c.need("engage.announcements.manage")
    ann.delete_draft(c.db, ann.get(c.db, c.actor.company_id, uuid))
    c.db.commit()
    return {"detail": "Deleted"}


@router.post("/announcements/{uuid}/acknowledge")
def acknowledge(uuid: UUID, c: Ctx = Depends(ctx)):
    a = ann.for_reader(c.db, c.actor, ann.get(c.db, c.actor.company_id, uuid), False)
    r = ann.acknowledge(c.db, c.actor, a)
    c.db.commit()
    return {"acknowledged_at": r.acknowledged_at}


@router.get("/announcements/{uuid}/receipts")
def announcement_receipts(uuid: UUID, status: str | None = None, q: str | None = None,
                          page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200), c: Ctx = Depends(ctx)):
    c.need("engage.announcements.manage")
    return ann.receipts(c.db, ann.get(c.db, c.actor.company_id, uuid), status, q, page, size)


# ── Surveys ─────────────────────────────────────────────────────────

@router.get("/surveys")
def list_surveys(status: str | None = None, kind: str | None = None, q: str | None = None,
                 page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100), c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    return surveys.list_admin(c.db, c.actor.company_id, status, kind, q, page, size)


@router.get("/surveys/mine")
def my_surveys(c: Ctx = Depends(ctx)):
    return surveys.my_surveys(c.db, c.actor)


@router.get("/surveys/enps")
def company_enps(c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    return surveys.company_enps(c.db, c.actor.company_id)


@router.get("/surveys/meta")
def surveys_meta(c: Ctx = Depends(ctx)):
    return {"kinds": surveys.KINDS, "question_types": surveys.QTYPES, "identity": surveys.IDENTITY,
            "recurrence": list(surveys.RECURRENCE), "max_questions": surveys.MAX_QUESTIONS,
            "min_responses": max(5, int(cfg.section(c.db, c.actor.company_id, "surveys").get("min_anonymous_responses") or 5))}


@router.post("/surveys", status_code=201)
def create_survey(req: SurveyIn, c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    s = surveys.create(c.db, c.actor, dump(req))
    c.db.commit()
    return surveys.detail(c.db, surveys.get(c.db, c.actor.company_id, s.uuid))


@router.get("/surveys/{uuid}")
def get_survey(uuid: UUID, c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    return surveys.detail(c.db, surveys.get(c.db, c.actor.company_id, uuid))


@router.put("/surveys/{uuid}")
def update_survey(uuid: UUID, req: SurveyIn, c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    s = surveys.update(c.db, c.actor, surveys.get(c.db, c.actor.company_id, uuid), dump(req))
    c.db.commit()
    return surveys.detail(c.db, surveys.get(c.db, c.actor.company_id, s.uuid))


@router.post("/surveys/{uuid}/publish")
def publish_survey(uuid: UUID, c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    s = surveys.get(c.db, c.actor.company_id, uuid)
    if s.status != "draft":
        raise BadRequest("This survey is already published.")
    surveys.publish(c.db, c.actor, s)
    c.db.commit()
    return surveys.detail(c.db, s)


@router.post("/surveys/{uuid}/close")
def close_survey(uuid: UUID, c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    s = surveys.get(c.db, c.actor.company_id, uuid)
    surveys.stop(c.db, c.actor, s)
    c.db.commit()
    return surveys.detail(c.db, s)


@router.post("/surveys/{uuid}/extend")
def extend_survey(uuid: UUID, req: ExtendIn, c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    s = surveys.get(c.db, c.actor.company_id, uuid)
    surveys.extend(c.db, c.actor, s, req.days)
    c.db.commit()
    return surveys.detail(c.db, s)


@router.post("/surveys/{uuid}/copy", status_code=201)
def copy_survey(uuid: UUID, c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    s = surveys.copy(c.db, c.actor, surveys.get(c.db, c.actor.company_id, uuid))
    c.db.commit()
    return surveys.detail(c.db, surveys.get(c.db, c.actor.company_id, s.uuid))


@router.post("/surveys/{uuid}/remind")
def remind_survey(uuid: UUID, c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    n = surveys.send_reminders(c.db, c.actor.company_id, surveys.get(c.db, c.actor.company_id, uuid), force=True)
    c.db.commit()
    return {"reminded": n}


@router.delete("/surveys/{uuid}")
def delete_survey(uuid: UUID, c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    surveys.delete_draft(c.db, surveys.get(c.db, c.actor.company_id, uuid))
    c.db.commit()
    return {"detail": "Deleted"}


@router.get("/surveys/{uuid}/take")
def take_survey(uuid: UUID, c: Ctx = Depends(ctx)):
    s = surveys.get(c.db, c.actor.company_id, uuid)
    out = surveys.for_taking(c.db, c.actor, s)
    if out["preview"] and not c.can("engage.surveys.manage"):
        raise HTTPException(status_code=404, detail="Survey not found")
    return out


@router.post("/surveys/{uuid}/responses", status_code=201)
def answer_survey(uuid: UUID, req: SurveyAnswerIn, c: Ctx = Depends(ctx)):
    s = surveys.get(c.db, c.actor.company_id, uuid)
    surveys.submit(c.db, c.actor, s, req.answers, req.show_name)
    c.db.commit()
    return {"detail": s.thank_you or "Thank you for your answers!"}


@router.get("/surveys/{uuid}/results")
def survey_results(uuid: UUID, run: UUID | None = None, c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    return surveys.results(c.db, surveys.get(c.db, c.actor.company_id, uuid), run)


@router.get("/surveys/{uuid}/participation")
def survey_participation(uuid: UUID, run: UUID | None = None, c: Ctx = Depends(ctx)):
    c.need("engage.surveys.manage")
    return surveys.participation(c.db, surveys.get(c.db, c.actor.company_id, uuid), run)


# ── Kiosks ──────────────────────────────────────────────────────────

@router.get("/kiosks")
def list_kiosks(c: Ctx = Depends(ctx)):
    c.need("engage.settings.manage")
    from modules.engagement.models import Kiosk
    rows = c.db.query(Kiosk).filter(Kiosk.company_id == c.actor.company_id).order_by(Kiosk.name).all()
    return {"items": [kiosk.as_dict(c.db, k) for k in rows], "slide_types": kiosk.SLIDE_TYPES}


@router.post("/kiosks", status_code=201)
def create_kiosk(req: KioskIn, c: Ctx = Depends(ctx)):
    c.need("engage.settings.manage")
    k = kiosk.create(c.db, c.actor.company_id, dump(req))
    c.db.commit()
    return kiosk.as_dict(c.db, k)


@router.put("/kiosks/{uuid}")
def update_kiosk(uuid: UUID, req: KioskIn, c: Ctx = Depends(ctx)):
    c.need("engage.settings.manage")
    k = kiosk.update(c.db, kiosk.get(c.db, c.actor.company_id, uuid), dump(req))
    c.db.commit()
    return kiosk.as_dict(c.db, k)


@router.post("/kiosks/{uuid}/new-link")
def new_kiosk_link(uuid: UUID, c: Ctx = Depends(ctx)):
    c.need("engage.settings.manage")
    k = kiosk.get(c.db, c.actor.company_id, uuid)
    kiosk.regenerate(c.db, k)
    c.db.commit()
    return kiosk.as_dict(c.db, k)


@public_router.get("/kiosk/{token}")
def kiosk_display(token: str, db: Session = Depends(get_db)):
    return kiosk.display(db, token)


# ── Home overview ───────────────────────────────────────────────────

@router.get("/home")
def home(c: Ctx = Depends(ctx)):
    """Buzz preview, today's celebrations, the latest announcement and what is waiting
    for the signed-in person, for the overview page."""
    cel = celebrations.overview(c.db, c.actor, 7)
    latest = ann.list_for_reader(c.db, c.actor, "current", 1, 1)["items"]
    preview = feed.list_feed(c.db, c.actor, filter_="all", limit=3)
    mine = surveys.my_surveys(c.db, c.actor)
    out = {
        "celebrations": {"today": cel["today"], "this_week": (cel["this_week"] + cel["upcoming"])[:6],
                         "new_joiners": cel["new_joiners"][:6]},
        "announcement": latest[0] if latest else None,
        "posts": (preview["pinned"] + preview["items"])[:3],
        "pending_surveys": mine["pending"][:3],
        "pending_acks": ann.pending_ack_count(c.db, c.actor),
    }
    c.db.commit()  # reading marks nothing, but keep the session clean
    return out


extra_routers = [public_router]
