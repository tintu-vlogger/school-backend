"""Assessment scoring and course progress: pure functions, easy to test.

Marking: a right answer earns the question's marks; a wrong answer loses the negative
marks (none, a fixed number, or a % of the question's marks); an unanswered question
scores 0. Multiple-choice questions need exactly the right set of options (no partial
marks). Short answers match any accepted answer, ignoring case and extra spaces. The
total never goes below 0. Passed means the percentage reaches the pass mark.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def _d(x) -> Decimal:
    return Decimal(str(x or 0))


def q2(x) -> Decimal:
    return _d(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def penalty(marks, mode: str, value) -> Decimal:
    if mode == "fixed":
        return _d(value)
    if mode == "percent":
        return _d(marks) * _d(value) / 100
    return Decimal(0)


def _norm(s: str) -> str:
    return " ".join(str(s or "").strip().lower().split())


def is_answered(question: dict, answer) -> bool:
    if question["kind"] == "short":
        return bool(_norm(answer if isinstance(answer, str) else ""))
    if isinstance(answer, str):
        return bool(answer)
    return bool(answer)


def is_correct(question: dict, answer) -> bool:
    kind = question["kind"]
    if kind == "short":
        accepted = {_norm(a) for a in question.get("accepted_answers") or [] if _norm(a)}
        return _norm(answer if isinstance(answer, str) else "") in accepted
    picked = {answer} if isinstance(answer, str) else set(answer or [])
    right = {o["id"] for o in question.get("options") or [] if o.get("correct")}
    if kind in ("single", "true_false"):
        return len(picked) == 1 and picked <= right
    return picked == right


def score_question(question: dict, answer, mode: str = "none", value=0) -> dict:
    marks = _d(question.get("marks") or 1)
    if not is_answered(question, answer):
        return {"id": question["id"], "answered": False, "correct": False, "marks": 0.0, "max": float(marks)}
    if is_correct(question, answer):
        got = marks
        ok = True
    else:
        got = -penalty(marks, mode, value)
        ok = False
    return {"id": question["id"], "answered": True, "correct": ok, "marks": float(q2(got)), "max": float(marks)}


def score_attempt(questions: list[dict], answers: dict, mode: str = "none", value=0, pass_pct=60) -> dict:
    results = [score_question(q, (answers or {}).get(str(q["id"])), mode, value) for q in questions]
    total = sum((_d(r["marks"]) for r in results), Decimal(0))
    total = max(total, Decimal(0))
    max_score = sum((_d(r["max"]) for r in results), Decimal(0))
    pct = (total / max_score * 100) if max_score > 0 else Decimal(0)
    pct = q2(pct)
    return {"score": float(q2(total)), "max_score": float(q2(max_score)), "pct": float(pct),
            "passed": pct >= _d(pass_pct), "results": results,
            "correct": sum(1 for r in results if r["correct"]), "wrong": sum(1 for r in results if r["answered"] and not r["correct"]),
            "unanswered": sum(1 for r in results if not r["answered"])}


def final_score(attempts: list[dict], rule: str = "highest") -> dict | None:
    """The attempt that counts: the highest or the latest submitted one."""
    done = [a for a in attempts if a.get("pct") is not None]
    if not done:
        return None
    if rule == "latest":
        return max(done, key=lambda a: a["number"])
    return max(done, key=lambda a: (a["pct"], a["number"]))


def course_progress(lesson_ids: list[int], done_ids: list[int]) -> float:
    if not lesson_ids:
        return 0.0
    done = len(set(lesson_ids) & set(done_ids))
    return float(q2(Decimal(done) / Decimal(len(lesson_ids)) * 100))


def unlocked(lesson_ids: list[int], done_ids: list[int], sequential: bool) -> set[int]:
    """Lessons a learner may open: all of them, or (in order) every done lesson and the next one."""
    if not sequential:
        return set(lesson_ids)
    out = set()
    for lid in lesson_ids:
        out.add(lid)
        if lid not in done_ids:
            break
    return out
