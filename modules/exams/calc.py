"""Pure result calculations (no database), so every rule is easy to test.

Numbers are Decimal throughout. Percentages are 0-100.

    band_for(76.5, bands)                      -> the grade band a percentage falls in
    combine([60, 72, 81], "best_of", 2)        -> 76.5   (average of the best two)
    weighted([(80, 20), (65, 80)])             -> 68     (weights are re-normalised when a part is missing)
    gpa([(9, 4), (7, 3)])                      -> credit-weighted grade point average (SGPA)
    cgpa([(8.2, 22), (8.8, 24)])               -> CGPA across terms, weighted by credits
    apply_grace(subjects, per_subject=5, …)    -> grace points per subject that lift fails to a pass
    decide(...)                                -> pass | fail | compartment | not_eligible
    competition_rank({id: value})              -> 1, 2, 2, 4 …
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Sequence

ZERO = Decimal(0)
HUNDRED = Decimal(100)

# Codes a teacher may type instead of marks.
CODES = {
    "AB": "Absent",     # counts as zero, fails the subject
    "EX": "Exempt",     # left out of totals
    "ML": "Medical",    # left out of totals
}
EXCLUDED_CODES = {"EX", "ML"}


def D(v) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def q(v, places: int = 2) -> Decimal | None:
    if v is None:
        return None
    return D(v).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def percent(obtained, maximum) -> Decimal | None:
    o, m = D(obtained), D(maximum)
    if o is None or not m:
        return None
    return o * HUNDRED / m


# ── Grade bands ──────────────────────────────────────────────────────

def band_for(pct, bands: Iterable[dict]) -> dict | None:
    """The band with the highest `min_percent` not above the percentage. Bands are dicts with
    at least `label` and `min_percent` (`grade_point`, `is_pass`, `description` optional)."""
    p = D(pct)
    if p is None:
        return None
    best = None
    for b in bands:
        lo = D(b["min_percent"])
        if lo is not None and p >= lo and (best is None or lo > D(best["min_percent"])):
            best = b
    return best


def band_by_label(label: str | None, bands: Iterable[dict]) -> dict | None:
    if not label:
        return None
    lab = label.strip().lower()
    return next((b for b in bands if str(b["label"]).strip().lower() == lab), None)


# ── Combining scores ─────────────────────────────────────────────────

def combine(values: Sequence, how: str = "average", n: int | None = None) -> Decimal | None:
    """Combine several percentages: average | best_of (average of the best n) | sum | max | min | latest."""
    vals = [D(v) for v in values if v is not None]
    if not vals:
        return None
    if how == "best_of":
        k = max(1, min(int(n or len(vals)), len(vals)))
        top = sorted(vals, reverse=True)[:k]
        return sum(top, ZERO) / len(top)
    if how == "sum":
        return sum(vals, ZERO)
    if how == "max":
        return max(vals)
    if how == "min":
        return min(vals)
    if how == "latest":
        return vals[-1]
    return sum(vals, ZERO) / len(vals)


def weighted(parts: Sequence[tuple]) -> Decimal | None:
    """Weighted average of (value, weight) pairs. Parts without a value are left out and the
    other weights re-normalised (a learner exempt from one exam is not penalised)."""
    use = [(D(v), D(w) or ZERO) for v, w in parts if v is not None]
    total_w = sum((w for _, w in use), ZERO)
    if not use or total_w <= 0:
        return None
    return sum((v * w for v, w in use), ZERO) / total_w


# ── GPA ──────────────────────────────────────────────────────────────

def gpa(rows: Sequence[tuple]) -> Decimal | None:
    """Credit-weighted grade point average of (grade_point, credits) pairs (credits default 1)."""
    use = [(D(gp), D(cr) if cr is not None else Decimal(1)) for gp, cr in rows if gp is not None]
    total = sum((c for _, c in use), ZERO)
    if not use or total <= 0:
        return None
    return sum((g * c for g, c in use), ZERO) / total


def cgpa(terms: Sequence[tuple]) -> Decimal | None:
    """CGPA from (sgpa, credits) of each term, weighted by credits."""
    return gpa(terms)


# ── Grace and pass rules ─────────────────────────────────────────────

def apply_grace(subjects: Sequence[dict], per_subject=None, max_subjects: int | None = None,
                total=None) -> list[Decimal]:
    """Grace points (in percentage points) per subject. Each subject dict has `percent` and
    `pass_percent`. Only failing subjects that the grace can lift to a pass get any; the
    smallest shortfalls are helped first, within the per-subject, subject-count and total caps."""
    out = [ZERO] * len(subjects)
    per = D(per_subject)
    if not per or per <= 0:
        return out
    budget = D(total) if total is not None else None
    short = []
    for i, s in enumerate(subjects):
        p, need = D(s.get("percent")), D(s.get("pass_percent"))
        if p is None or need is None or p >= need or s.get("absent"):
            continue
        gap = need - p
        if gap <= per:
            short.append((gap, i))
    short.sort()
    used_subjects = 0
    for gap, i in short:
        if max_subjects is not None and used_subjects >= max_subjects:
            break
        if budget is not None and gap > budget:
            continue
        out[i] = gap
        used_subjects += 1
        if budget is not None:
            budget -= gap
    return out


def decide(subject_passes: Sequence[bool], aggregate, *, pass_all_subjects: bool = True,
           min_aggregate=None, max_failed_subjects: int | None = None, attendance=None,
           min_attendance=None, complete: bool = True) -> str:
    """Overall result: pass | fail | compartment (failed a few subjects, may re-sit) |
    not_eligible (attendance below the minimum) | incomplete (marks missing)."""
    if not complete:
        return "incomplete"
    if min_attendance is not None and attendance is not None and D(attendance) < D(min_attendance):
        return "not_eligible"
    failed = sum(1 for p in subject_passes if not p)
    agg_ok = min_aggregate is None or (aggregate is not None and D(aggregate) >= D(min_aggregate))
    if not agg_ok:
        return "fail"
    if pass_all_subjects and failed:
        if max_failed_subjects is not None and failed <= max_failed_subjects:
            return "compartment"
        return "fail"
    return "pass"


def competition_rank(values: dict) -> dict:
    """Standard competition ranking (1, 2, 2, 4) of {key: value}, highest first. None is unranked."""
    ordered = sorted(((v, k) for k, v in values.items() if v is not None), key=lambda x: x[0], reverse=True)
    out, prev, rank = {}, None, 0
    for i, (v, k) in enumerate(ordered, 1):
        if v != prev:
            rank, prev = i, v
        out[k] = rank
    return out
