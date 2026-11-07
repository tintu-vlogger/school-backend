"""Quizzes and tests: question bank, quiz assembly (fixed or random from the bank), learner
attempts with time limit and autosave, auto-marking, manual marking of long answers, results
and item analysis.

Marking rules (`mark_item`)
- single choice / true-false: full marks when the picked option is the right one.
- multiple choice: full marks only when exactly the right set of options is picked.
- short answer: full marks when the answer matches any accepted answer, ignoring case,
  extra spaces and a trailing full stop.
- numeric: full marks when |answer - value| <= tolerance.
- long answer: left for the teacher (the attempt shows "needs marking").
A wrong (not blank) answer loses the question's negative marks; an attempt never scores
below zero.
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.errors import BadRequest, NotFound
from core.timeutil import iso_utc, utc_now
from modules.academics.models import Section, Subject
from modules.lms import service as svc
from modules.lms.models import BankQuestion, Quiz, QuizAttempt, QuizQuestion, QuizSection
from modules.students import service as students
from modules.students.models import Student

KINDS = ("single", "multiple", "true_false", "short", "numeric", "long")
KIND_LABELS = {"single": "Single choice", "multiple": "Multiple choice", "true_false": "True / false",
               "short": "Short answer", "numeric": "Number", "long": "Long answer"}
TF_OPTIONS = [{"key": "true", "text": "True"}, {"key": "false", "text": "False"}]
ZERO = Decimal("0")


# ── Marking ──────────────────────────────────────────────────────────

def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().rstrip(".").casefold()


def _num(v) -> Decimal | None:
    try:
        return Decimal(str(v).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def is_blank(given) -> bool:
    return given is None or given == "" or given == [] or (isinstance(given, str) and not given.strip())


def mark_item(kind: str, answer, given, marks: Decimal, negative: Decimal = ZERO) -> tuple[Decimal | None, bool | None]:
    """(score, correct) for one answer; (None, None) when it needs a teacher."""
    marks, negative = Decimal(marks), Decimal(negative or 0)
    if kind == "long":
        return (ZERO, None) if is_blank(given) else (None, None)
    if is_blank(given):
        return ZERO, False
    correct = False
    if kind in ("single", "true_false"):
        key = (answer or [None])[0]
        correct = str(given) == str(key)
    elif kind == "multiple":
        picked = set(map(str, given if isinstance(given, list) else [given]))
        correct = picked == set(map(str, answer or []))
    elif kind == "short":
        correct = _norm(given) in {_norm(a) for a in (answer or [])}
    elif kind == "numeric":
        val, target = _num(given), _num((answer or {}).get("value"))
        tol = _num((answer or {}).get("tolerance") or 0) or ZERO
        correct = val is not None and target is not None and abs(val - target) <= abs(tol)
    return (marks if correct else (-negative if negative else ZERO)), correct


def validate_question(data: dict) -> dict:
    kind = data.get("kind")
    if kind not in KINDS:
        raise BadRequest("Pick a question type.")
    opts = data.get("options") or []
    ans = data.get("answer")
    if kind == "true_false":
        data["options"] = TF_OPTIONS
        val = ans[0] if isinstance(ans, list) and ans else ans
        if str(val).lower() not in ("true", "false"):
            raise BadRequest("Say whether the statement is true or false.")
        data["answer"] = [str(val).lower()]
    elif kind in ("single", "multiple"):
        clean = []
        for i, o in enumerate(opts):
            t = (o.get("text") if isinstance(o, dict) else str(o) or "").strip()
            if t:
                clean.append({"key": (o.get("key") if isinstance(o, dict) and o.get("key") else chr(97 + i))[:8], "text": t})
        if len(clean) < 2:
            raise BadRequest("Add at least two options.")
        keys = {o["key"] for o in clean}
        if len(keys) != len(clean):
            raise BadRequest("Each option needs its own key.")
        picked = [str(a) for a in (ans if isinstance(ans, list) else [ans]) if a not in (None, "")]
        if not picked or set(picked) - keys:
            raise BadRequest("Mark the right option." if kind == "single" else "Mark the right options.")
        if kind == "single" and len(picked) != 1:
            raise BadRequest("A single-choice question has exactly one right option.")
        data["options"], data["answer"] = clean, picked
    elif kind == "short":
        accepted = [str(a).strip() for a in (ans if isinstance(ans, list) else [ans]) if str(a or "").strip()]
        if not accepted:
            raise BadRequest("Add at least one accepted answer.")
        data["options"], data["answer"] = [], accepted
    elif kind == "numeric":
        if not isinstance(ans, dict) or _num(ans.get("value")) is None:
            raise BadRequest("Enter the right number.")
        tol = _num(ans.get("tolerance") or 0)
        if tol is None or tol < 0:
            raise BadRequest("The tolerance must be zero or more.")
        data["options"], data["answer"] = [], {"value": str(_num(ans["value"])), "tolerance": str(tol)}
    else:
        data["options"], data["answer"] = [], None
    if data.get("difficulty") not in (None, "easy", "medium", "hard"):
        raise BadRequest("Pick easy, medium or hard.")
    data["tags"] = sorted({str(t).strip().lower()[:30] for t in (data.get("tags") or []) if str(t).strip()})
    return data


# ── Question bank ────────────────────────────────────────────────────

def question_query(db: Session, user: dict):
    q = db.query(BankQuestion).options(joinedload(BankQuestion.subject)).filter(BankQuestion.company_id == user["company_id"])
    if not svc.is_manager(db, user):
        subjects = {a["subject_id"] for a in svc.allocations(db, user)}
        q = q.filter(or_(BankQuestion.subject_id.in_(subjects or [-1]),
                         BankQuestion.created_by_employee_id == user.get("employee_id")))
    return q


def question_out(q: BankQuestion, with_answer: bool = True) -> dict:
    d = {"id": q.id, "uuid": str(q.uuid), "subject_id": q.subject_id, "subject_name": q.subject.name if q.subject else None,
         "program_id": q.program_id, "topic_id": q.topic_id, "topic": q.topic, "kind": q.kind,
         "kind_label": KIND_LABELS.get(q.kind), "prompt": q.prompt, "options": q.options or [],
         "marks": svc.dec(q.marks), "negative_marks": svc.dec(q.negative_marks), "difficulty": q.difficulty,
         "tags": q.tags or [], "active": q.active, "created_at": iso_utc(q.created_at)}
    if with_answer:
        d["answer"], d["explanation"] = q.answer, q.explanation
    return d


def save_question(db: Session, user: dict, data: dict, row: BankQuestion | None = None) -> BankQuestion:
    cid = user["company_id"]
    merged = {"kind": row.kind if row else None, "options": row.options if row else [], "answer": row.answer if row else None,
              "tags": row.tags if row else [], **data}
    if set(data) & {"kind", "options", "answer", "tags", "difficulty"}:
        merged = validate_question(merged)
        for k in ("kind", "options", "answer", "tags"):
            data[k] = merged[k]
    subject_id = data.get("subject_id", row.subject_id if row else None)
    if not db.query(Subject.id).filter(Subject.company_id == cid, Subject.id == subject_id).first():
        raise BadRequest("Pick a subject.")
    if not svc.is_manager(db, user):
        if svc.TEACH not in svc.perms(db, user):
            raise BadRequest("You don't have permission to write questions.")
        if subject_id not in {a["subject_id"] for a in svc.allocations(db, user)}:
            raise BadRequest("You can only add questions for the subjects you teach.")
    if row is None:
        row = BankQuestion(company_id=cid, created_by_employee_id=user.get("employee_id"))
        db.add(row)
    for k, v in data.items():
        setattr(row, k, v)
    db.flush()
    return row


# ── Quizzes ──────────────────────────────────────────────────────────

def quiz_query(db: Session, user: dict):
    q = db.query(Quiz).options(joinedload(Quiz.subject)).filter(Quiz.company_id == user["company_id"],
                                                                Quiz.archived_at.is_(None))
    scope = svc.staff_section_scope(db, user)
    if scope is not None:
        sub = db.query(QuizSection.quiz_id).filter(QuizSection.section_id.in_(scope or [-1]))
        q = q.filter(or_(Quiz.id.in_(sub), Quiz.created_by_employee_id == user.get("employee_id")))
    return q


def get_quiz(db: Session, user: dict, uuid) -> Quiz:
    row = quiz_query(db, user).filter(Quiz.uuid == uuid).first()
    if row is None:
        raise NotFound("Quiz")
    return row


def quiz_window(qz: Quiz, now: datetime | None = None) -> str:
    """draft | scheduled (not open yet) | open | closed"""
    now = now or utc_now()
    if qz.status == "draft":
        return "draft"
    if qz.status == "closed" or (qz.closes_at and now > svc.as_utc(qz.closes_at)):
        return "closed"
    if qz.opens_at and now < svc.as_utc(qz.opens_at):
        return "scheduled"
    return "open"


def total_marks(qz: Quiz) -> Decimal:
    if qz.mode == "random":
        return ZERO  # depends on the questions drawn; shown per attempt
    return sum((Decimal(qq.marks if qq.marks is not None else qq.question.marks) for qq in qz.questions), ZERO)


def question_count(qz: Quiz) -> int:
    if qz.mode == "random":
        return sum(int(r.get("count") or 0) for r in (qz.random_rules or []))
    return len(qz.questions)


def quiz_out(db: Session, qz: Quiz, labels: dict | None = None, stats: dict | None = None) -> dict:
    secs = [l.section_id for l in qz.sections]
    labels = labels if labels is not None else svc.section_labels(db, qz.company_id, secs)
    return {
        "id": qz.id, "uuid": str(qz.uuid), "title": qz.title, "instructions": qz.instructions, "kind": qz.kind,
        "subject_id": qz.subject_id, "subject_name": qz.subject.name if qz.subject else None, "program_id": qz.program_id,
        "mode": qz.mode, "random_rules": qz.random_rules or [], "time_limit_min": qz.time_limit_min,
        "attempts_allowed": qz.attempts_allowed, "shuffle_questions": qz.shuffle_questions,
        "shuffle_options": qz.shuffle_options, "opens_at": iso_utc(qz.opens_at), "closes_at": iso_utc(qz.closes_at),
        "show_answers": qz.show_answers, "pass_percent": svc.dec(qz.pass_percent), "status": qz.status,
        "window": quiz_window(qz), "section_ids": secs, "sections": [{"id": s, "label": labels.get(s)} for s in secs],
        "question_count": question_count(qz), "total_marks": svc.dec(total_marks(qz)),
        "created_by_employee_id": qz.created_by_employee_id, "created_by_name": qz.created_by_name,
        "created_at": iso_utc(qz.created_at), "stats": (stats or {}).get(qz.id),
    }


def save_quiz(db: Session, user: dict, data: dict, row: Quiz | None = None) -> Quiz:
    cid = user["company_id"]
    section_ids = data.pop("section_ids", None)
    question_items = data.pop("questions", None)
    target = section_ids if section_ids is not None else [l.section_id for l in row.sections] if row else []
    svc.require_teaching(db, user, target, data.get("subject_id", row.subject_id if row else None))
    opens = data.get("opens_at", row.opens_at if row else None)
    closes = data.get("closes_at", row.closes_at if row else None)
    if opens and closes and svc.as_utc(closes) <= svc.as_utc(opens):
        raise BadRequest("The quiz must close after it opens.")
    if data.get("attempts_allowed") is not None and not 1 <= int(data["attempts_allowed"]) <= 20:
        raise BadRequest("Allow between 1 and 20 attempts.")
    if data.get("show_answers") not in (None, "after_submit", "after_close", "never"):
        raise BadRequest("Pick when learners see the answers.")
    if "random_rules" in data:
        rules = []
        for r in data["random_rules"] or []:
            n = int(r.get("count") or 0)
            if n > 0:
                rules.append({"count": min(n, 100), "difficulty": r.get("difficulty") or None,
                              "topic": (r.get("topic") or "").strip() or None, "tag": (r.get("tag") or "").strip().lower() or None})
        data["random_rules"] = rules
    if row is None:
        row = Quiz(company_id=cid, created_by_employee_id=user.get("employee_id"), created_by_name=svc.actor_name())
        db.add(row)
    was_published = row.status == "published" if row.id else False
    if row.id and db.query(QuizAttempt.id).filter(QuizAttempt.quiz_id == row.id).first():
        locked = {"mode", "random_rules"} & set(data)
        if locked or question_items is not None:
            raise BadRequest("Learners have started this quiz, so its questions can't change.")
    for k, v in data.items():
        setattr(row, k, v)
    if section_ids is not None:
        secs = db.query(Section).filter(Section.company_id == cid, Section.id.in_(section_ids)).all()
        programs = {s.program_id for s in secs}
        row.program_id = programs.pop() if len(programs) == 1 else None
        keep = {l.section_id: l for l in row.sections}
        row.sections = [keep.get(s) or QuizSection(company_id=cid, section_id=s) for s in dict.fromkeys(section_ids)]
    db.flush()
    if question_items is not None:
        set_questions(db, user, row, question_items)
    if row.status == "published":
        if row.mode == "fixed" and not row.questions:
            raise BadRequest("Add questions before publishing.")
        if row.mode == "random":
            check_random_pool(db, row)
        if not was_published:
            _notify_published(db, row)
    return row


def set_questions(db: Session, user: dict, qz: Quiz, items: list[dict]) -> None:
    ids = [int(i["question_id"]) for i in items]
    found = {q.id: q for q in db.query(BankQuestion).filter(BankQuestion.company_id == qz.company_id, BankQuestion.id.in_(ids or [-1])).all()}
    if set(ids) - set(found):
        raise BadRequest("A question was not found.")
    if any(found[i].subject_id != qz.subject_id for i in ids):
        raise BadRequest("All questions must be from the quiz's subject.")
    keep = {qq.question_id: qq for qq in qz.questions}
    new = []
    for n, it in enumerate(items, 1):
        qid = int(it["question_id"])
        qq = keep.get(qid) or QuizQuestion(company_id=qz.company_id, question_id=qid)
        qq.sort_order = n
        qq.marks = Decimal(str(it["marks"])) if it.get("marks") not in (None, "") else None
        new.append(qq)
    qz.questions = new
    db.flush()


def _pool(db: Session, qz: Quiz, rule: dict):
    q = db.query(BankQuestion).filter(BankQuestion.company_id == qz.company_id, BankQuestion.subject_id == qz.subject_id,
                                  BankQuestion.active.is_(True))
    if rule.get("difficulty"):
        q = q.filter(BankQuestion.difficulty == rule["difficulty"])
    if rule.get("topic"):
        q = q.filter(BankQuestion.topic.ilike(rule["topic"]))
    if rule.get("tag"):
        q = q.filter(BankQuestion.tags.contains([rule["tag"]]))
    return q


def check_random_pool(db: Session, qz: Quiz) -> None:
    if not qz.random_rules:
        raise BadRequest("Say how many questions to draw from the bank.")
    for r in qz.random_rules:
        have = _pool(db, qz, r).count()
        if have < r["count"]:
            raise BadRequest(f"The bank has only {have} matching questions for a rule that needs {r['count']}.")


def _notify_published(db: Session, qz: Quiz) -> None:
    from modules.notifications.service import notify_portal
    sids = []
    for l in qz.sections:
        sids += [s.id for s, _ in students.section_students(db, qz.company_id, l.section_id)]
    accounts = students.portal_account_ids_for_students(db, qz.company_id, sids, parents=True, students=True)
    tz = svc.company_tz(db, qz.company_id)
    when = f" Closes {svc.as_utc(qz.closes_at).astimezone(tz).strftime('%d %b, %I:%M %p')}." if qz.closes_at else ""
    notify_portal(db, qz.company_id, accounts, kind="lms.quiz", title=f"New {qz.kind}: {qz.title}",
                  body=f"{qz.subject.name if qz.subject else ''}.{when}", link="/dashboard/my/quizzes")


# ── Attempts ─────────────────────────────────────────────────────────

def _draw_paper(db: Session, qz: Quiz, rng: random.Random) -> list[dict]:
    paper: list[dict] = []
    if qz.mode == "random":
        taken: set[int] = set()
        for r in qz.random_rules or []:
            pool = [q for q in _pool(db, qz, r).all() if q.id not in taken]
            pick = rng.sample(pool, min(r["count"], len(pool)))
            for q in pick:
                taken.add(q.id)
                paper.append({"question_id": q.id, "marks": str(q.marks)})
        questions = {q.id: q for q in db.query(BankQuestion).filter(BankQuestion.id.in_(taken or [-1])).all()}
    else:
        questions = {qq.question_id: qq.question for qq in qz.questions}
        paper = [{"question_id": qq.question_id, "marks": str(qq.marks if qq.marks is not None else qq.question.marks)}
                 for qq in qz.questions]
    if qz.shuffle_questions:
        rng.shuffle(paper)
    for item in paper:
        q = questions[item["question_id"]]
        keys = [o["key"] for o in (q.options or [])]
        if qz.shuffle_options and q.kind in ("single", "multiple"):
            rng.shuffle(keys)
        item["options"] = keys
    return paper


def attempts_of(db: Session, qz: Quiz, student_id: int) -> list[QuizAttempt]:
    return (db.query(QuizAttempt).filter(QuizAttempt.quiz_id == qz.id, QuizAttempt.student_id == student_id)
            .order_by(QuizAttempt.attempt_no).all())


def start_attempt(db: Session, qz: Quiz, learner: svc.Learner, now: datetime | None = None,
                  rng: random.Random | None = None) -> QuizAttempt:
    """Resume the open attempt, or start a new one when attempts remain and the quiz is open."""
    now = now or utc_now()
    if learner.section_id not in {l.section_id for l in qz.sections}:
        raise NotFound("Quiz")
    window = quiz_window(qz, now)
    existing = attempts_of(db, qz, learner.student.id)
    for a in existing:
        if a.status == "in_progress":
            if a.deadline_at and now > svc.as_utc(a.deadline_at):
                finish(db, a, now)
            else:
                return a
    if window == "scheduled":
        raise BadRequest("This quiz hasn't opened yet.")
    if window != "open":
        raise BadRequest("This quiz is closed.")
    if len(existing) >= (qz.attempts_allowed or 1):
        raise BadRequest("You have used all your attempts.")
    paper = _draw_paper(db, qz, rng or random.Random())
    if not paper:
        raise BadRequest("This quiz has no questions yet.")
    deadline = None
    if qz.time_limit_min:
        deadline = now + timedelta(minutes=qz.time_limit_min)
    if qz.closes_at and (deadline is None or deadline > svc.as_utc(qz.closes_at)):
        deadline = svc.as_utc(qz.closes_at)
    a = QuizAttempt(company_id=qz.company_id, quiz_id=qz.id, student_id=learner.student.id,
                    attempt_no=len(existing) + 1, started_at=now, deadline_at=deadline, paper=paper, answers={},
                    item_scores={}, status="in_progress",
                    max_score=sum((Decimal(p["marks"]) for p in paper), ZERO))
    db.add(a)
    db.flush()
    return a


def save_answers(db: Session, a: QuizAttempt, answers: dict, now: datetime | None = None) -> QuizAttempt:
    now = now or utc_now()
    if a.status != "in_progress":
        raise BadRequest("This attempt has been submitted.")
    if a.deadline_at and now > svc.as_utc(a.deadline_at) + timedelta(seconds=30):
        finish(db, a, now)
        raise BadRequest("Time is up. Your saved answers were submitted.")
    allowed = {str(p["question_id"]) for p in a.paper}
    merged = dict(a.answers or {})
    for k, v in (answers or {}).items():
        if str(k) in allowed:
            merged[str(k)] = v
    a.answers = merged
    db.flush()
    return a


def _questions(db: Session, a: QuizAttempt) -> dict[int, BankQuestion]:
    ids = [p["question_id"] for p in a.paper]
    return {q.id: q for q in db.query(BankQuestion).filter(BankQuestion.id.in_(ids or [-1])).all()}


def finish(db: Session, a: QuizAttempt, now: datetime | None = None) -> QuizAttempt:
    """Submit and auto-mark (idempotent for attempts already submitted)."""
    if a.status != "in_progress":
        return a
    now = now or utc_now()
    a.submitted_at = now if not a.deadline_at or now <= svc.as_utc(a.deadline_at) else svc.as_utc(a.deadline_at)
    auto_mark(db, a)
    return a


def auto_mark(db: Session, a: QuizAttempt) -> None:
    qs = _questions(db, a)
    scores = dict(a.item_scores or {})
    for p in a.paper:
        q = qs.get(p["question_id"])
        key = str(p["question_id"])
        if q is None:
            continue
        prev = scores.get(key) or {}
        if q.kind == "long" and prev.get("auto") is False:  # already marked by the teacher
            continue
        score, correct = mark_item(q.kind, q.answer, (a.answers or {}).get(key), Decimal(p["marks"]), q.negative_marks)
        scores[key] = {"score": None if score is None else str(score), "correct": correct, "auto": True,
                       "feedback": prev.get("feedback")}
    a.item_scores = scores
    _total(a)


def _total(a: QuizAttempt) -> None:
    pending = any(v.get("score") is None for v in (a.item_scores or {}).values())
    total = sum((Decimal(v["score"]) for v in (a.item_scores or {}).values() if v.get("score") is not None), ZERO)
    a.score = max(total, ZERO)
    a.needs_marking = pending
    a.status = "submitted" if pending else "marked"


def mark_manually(db: Session, user: dict, a: QuizAttempt, items: dict) -> QuizAttempt:
    if a.status == "in_progress":
        raise BadRequest("The learner is still working on this attempt.")
    marks = {str(p["question_id"]): Decimal(p["marks"]) for p in a.paper}
    scores = dict(a.item_scores or {})
    for qid, v in (items or {}).items():
        qid = str(qid)
        if qid not in marks:
            continue
        s = _num(v.get("score")) if v.get("score") not in (None, "") else None
        if s is None:
            continue
        if s < 0 or s > marks[qid]:
            raise BadRequest(f"A score must be between 0 and {marks[qid]}.")
        scores[qid] = {"score": str(s), "correct": s >= marks[qid], "auto": False,
                       "feedback": (v.get("feedback") or "").strip() or None}
    a.item_scores = scores
    a.marked_by_employee_id = user.get("employee_id")
    _total(a)
    db.flush()
    if a.status == "marked":
        from modules.notifications.service import notify_portal
        qz = a.quiz
        notify_portal(db, a.company_id, students.portal_account_ids_for_students(db, a.company_id, [a.student_id]),
                      kind="lms.quiz_marked", title=f"Quiz marked: {qz.title}",
                      body=f"Score {svc.dec(a.score)} / {svc.dec(a.max_score)}.", link="/dashboard/my/quizzes")
    return a


def answers_visible(qz: Quiz, a: QuizAttempt, now: datetime | None = None) -> bool:
    if a.status == "in_progress":
        return False
    if qz.show_answers == "after_submit":
        return True
    if qz.show_answers == "after_close":
        return quiz_window(qz, now) == "closed"
    return False


def attempt_out(db: Session, a: QuizAttempt, *, for_learner: bool, show_answers: bool = False) -> dict:
    qs = _questions(db, a)
    items = []
    for n, p in enumerate(a.paper, 1):
        q = qs.get(p["question_id"])
        if q is None:
            continue
        by_key = {o["key"]: o for o in (q.options or [])}
        opts = [by_key[k] for k in p.get("options") or [] if k in by_key] or (q.options or [])
        sc = (a.item_scores or {}).get(str(q.id)) or {}
        item = {"no": n, "question_id": q.id, "kind": q.kind, "prompt": q.prompt, "options": opts, "marks": p["marks"],
                "response": (a.answers or {}).get(str(q.id))}
        if not for_learner or a.status != "in_progress":
            item.update({"score": sc.get("score") if (not for_learner or a.status == "marked" or show_answers) else None,
                         "correct": sc.get("correct") if (not for_learner or show_answers) else None,
                         "feedback": sc.get("feedback"), "auto": sc.get("auto", True)})
        if not for_learner or show_answers:
            item["answer"], item["explanation"] = q.answer, q.explanation
        items.append(item)
    show_score = not for_learner or a.status == "marked"
    return {"uuid": str(a.uuid), "attempt_no": a.attempt_no, "status": a.status, "started_at": iso_utc(a.started_at),
            "deadline_at": iso_utc(a.deadline_at), "submitted_at": iso_utc(a.submitted_at),
            "score": svc.dec(a.score) if show_score else None, "max_score": svc.dec(a.max_score),
            "needs_marking": a.needs_marking, "items": items, "server_now": iso_utc(utc_now()),
            "student_id": a.student_id, "show_answers": show_answers}


def quiz_stats(db: Session, quizzes: list[Quiz]) -> dict[int, dict]:
    ids = [q.id for q in quizzes]
    if not ids:
        return {}
    out = {i: {"attempted": 0, "to_mark": 0, "average": None} for i in ids}
    for qid, n, to_mark, avg in (db.query(QuizAttempt.quiz_id, func.count(func.distinct(QuizAttempt.student_id)),
                                          func.count(QuizAttempt.id).filter(QuizAttempt.needs_marking.is_(True)),
                                          func.avg(QuizAttempt.score * 100 / func.nullif(QuizAttempt.max_score, 0))
                                          .filter(QuizAttempt.status == "marked"))
                                 .filter(QuizAttempt.quiz_id.in_(ids), QuizAttempt.status != "in_progress")
                                 .group_by(QuizAttempt.quiz_id).all()):
        out[qid] = {"attempted": n, "to_mark": to_mark, "average": None if avg is None else round(float(avg), 1)}
    return out


def results(db: Session, qz: Quiz) -> dict:
    """Best attempt per learner, plus item analysis over every submitted attempt."""
    roster = []
    for l in qz.sections:
        roster += students.section_students(db, qz.company_id, l.section_id)
    attempts = db.query(QuizAttempt).filter(QuizAttempt.quiz_id == qz.id).order_by(QuizAttempt.attempt_no).all()
    by_student: dict[int, list[QuizAttempt]] = {}
    for a in attempts:
        by_student.setdefault(a.student_id, []).append(a)
    labels = svc.section_labels(db, qz.company_id, [l.section_id for l in qz.sections])
    rows = []
    for s, e in roster:
        mine = [a for a in by_student.get(s.id, []) if a.status != "in_progress"]
        best = max(mine, key=lambda a: (a.score or ZERO)) if mine else None
        open_ = next((a for a in by_student.get(s.id, []) if a.status == "in_progress"), None)
        pct = float(best.score * 100 / best.max_score) if best and best.max_score else None
        rows.append({"student_id": s.id, "student_uuid": str(s.uuid), "name": s.full_name, "roll_no": e.roll_no,
                     "section_label": labels.get(e.section_id), "attempts": len(mine),
                     "in_progress": open_ is not None, "best_attempt_uuid": str(best.uuid) if best else None,
                     "score": svc.dec(best.score) if best else None, "max_score": svc.dec(best.max_score) if best else None,
                     "percent": round(pct, 1) if pct is not None else None,
                     "passed": (pct >= float(qz.pass_percent)) if pct is not None and qz.pass_percent is not None else None,
                     "needs_marking": bool(best and best.needs_marking),
                     "submitted_at": iso_utc(best.submitted_at) if best else None})
    done = [a for a in attempts if a.status != "in_progress"]
    qids: list[int] = []
    for a in done:
        for p in a.paper:
            if p["question_id"] not in qids:
                qids.append(p["question_id"])
    qs = {q.id: q for q in db.query(BankQuestion).filter(BankQuestion.id.in_(qids or [-1])).all()}
    analysis = []
    for qid in qids:
        q = qs.get(qid)
        if q is None:
            continue
        seen, correct, total_score, total_marks_, blank = 0, 0, ZERO, ZERO, 0
        dist: dict[str, int] = {o["key"]: 0 for o in (q.options or [])}
        for a in done:
            p = next((p for p in a.paper if p["question_id"] == qid), None)
            if p is None:
                continue
            seen += 1
            sc = (a.item_scores or {}).get(str(qid)) or {}
            if sc.get("correct"):
                correct += 1
            if sc.get("score") is not None:
                total_score += Decimal(sc["score"])
                total_marks_ += Decimal(p["marks"])
            given = (a.answers or {}).get(str(qid))
            if is_blank(given):
                blank += 1
            for k in (given if isinstance(given, list) else [given]):
                if k is not None and str(k) in dist:
                    dist[str(k)] += 1
        analysis.append({"question_id": qid, "prompt": q.prompt, "kind": q.kind, "difficulty": q.difficulty,
                         "seen": seen, "correct": correct, "blank": blank,
                         "correct_percent": round(correct * 100 / seen, 1) if seen and q.kind != "long" else None,
                         "average_percent": round(float(total_score * 100 / total_marks_), 1) if total_marks_ else None,
                         "options": [{"key": o["key"], "text": o["text"], "count": dist.get(o["key"], 0),
                                      "is_answer": o["key"] in (q.answer or []) if isinstance(q.answer, list) else False}
                                     for o in (q.options or [])]})
    scored = [r["percent"] for r in rows if r["percent"] is not None]
    return {"rows": rows, "items": analysis,
            "summary": {"learners": len(rows), "attempted": len(scored) + sum(1 for r in rows if r["attempts"] and r["percent"] is None),
                        "average": round(sum(scored) / len(scored), 1) if scored else None,
                        "highest": max(scored) if scored else None, "lowest": min(scored) if scored else None,
                        "to_mark": sum(1 for a in done if a.needs_marking)}}


def portal_quizzes(db: Session, learner: svc.Learner) -> list[tuple[Quiz, list[QuizAttempt]]]:
    if not learner.section_id:
        return []
    rows = (db.query(Quiz).options(joinedload(Quiz.subject)).join(QuizSection, QuizSection.quiz_id == Quiz.id)
            .filter(Quiz.company_id == learner.student.company_id, QuizSection.section_id == learner.section_id,
                    Quiz.archived_at.is_(None), Quiz.status.in_(("published", "closed")))
            .order_by(Quiz.opens_at.desc().nullslast(), Quiz.id.desc()).limit(200).all())
    atts = db.query(QuizAttempt).filter(QuizAttempt.student_id == learner.student.id,
                                        QuizAttempt.quiz_id.in_([q.id for q in rows] or [-1])).all()
    now = utc_now()
    for a in atts:  # close attempts whose time ran out
        if a.status == "in_progress" and a.deadline_at and now > svc.as_utc(a.deadline_at):
            finish(db, a, now)
    by: dict[int, list[QuizAttempt]] = {}
    for a in sorted(atts, key=lambda a: a.attempt_no):
        by.setdefault(a.quiz_id, []).append(a)
    return [(q, by.get(q.id, [])) for q in rows]


def portal_quiz_out(db: Session, qz: Quiz, atts: list[QuizAttempt]) -> dict:
    d = quiz_out(db, qz, labels={})
    for k in ("created_by_employee_id", "random_rules", "stats"):
        d.pop(k, None)
    done = [a for a in atts if a.status != "in_progress"]
    best = max(done, key=lambda a: (a.score or ZERO)) if done else None
    d.update({"attempts_used": len(atts), "in_progress_uuid": next((str(a.uuid) for a in atts if a.status == "in_progress"), None),
              "attempts": [{"uuid": str(a.uuid), "attempt_no": a.attempt_no, "status": a.status,
                            "score": svc.dec(a.score) if a.status == "marked" else None,
                            "max_score": svc.dec(a.max_score), "submitted_at": iso_utc(a.submitted_at)} for a in done],
              "best_score": svc.dec(best.score) if best and best.status == "marked" else None,
              "best_max": svc.dec(best.max_score) if best else None,
              "can_start": quiz_window(qz) == "open" and (len(atts) < (qz.attempts_allowed or 1) or
                                                            any(a.status == "in_progress" for a in atts))})
    return d


def close_expired(db: Session) -> int:
    now = utc_now()
    rows = db.query(QuizAttempt).filter(QuizAttempt.status == "in_progress", QuizAttempt.deadline_at.isnot(None),
                                        QuizAttempt.deadline_at < now - timedelta(minutes=1)).limit(500).all()
    for a in rows:
        db.info["company_id"] = a.company_id
        finish(db, a, now)
    db.info.pop("company_id", None)
    db.flush()
    return len(rows)


def student_of_attempt(db: Session, a: QuizAttempt) -> Student:
    return db.query(Student).filter(Student.id == a.student_id).first()


def audit_publish(db: Session, qz: Quiz) -> None:
    audit.event(db, "lms.quiz_published", "lms_quizzes", qz.id, {"sections": [l.section_id for l in qz.sections]},
                entity_label=qz.title)
