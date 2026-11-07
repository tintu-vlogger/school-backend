"""Pure calculations for performance: OKR progress and roll-up, check-in plans, review
scores and weightage, rating distribution for calibration, the nine-box grid and the
increment guideline matrix. No database access, so they are easy to test."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal


def _f(x) -> float:
    return float(x) if x is not None else 0.0


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def r2(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ── OKRs ────────────────────────────────────────────────────────────

def kr_progress(kind: str, start=0, target=100, current=0, milestones: list | None = None) -> float:
    """Progress of one key result, 0..100.
    number / percent: share of the way from start to target (works for targets that go
    down, e.g. cut attrition from 18 to 12); milestone: weighted share of milestones done;
    boolean: 0 or 100."""
    if kind == "boolean":
        return 100.0 if _f(current) >= 1 else 0.0
    if kind == "milestone":
        ms = milestones or []
        total = sum(_f(m.get("weight", 1)) for m in ms)
        if total <= 0:
            return 0.0
        done = sum(_f(m.get("weight", 1)) for m in ms if m.get("done"))
        return r2(clamp(done / total * 100))
    s, t, c = _f(start), _f(target), _f(current)
    if t == s:
        return 100.0 if c == t else 0.0
    return r2(clamp((c - s) / (t - s) * 100))


def weighted_progress(items: list[tuple[float, float]]) -> float:
    """Weighted average of (progress, weight) pairs. Items with no weight are ignored;
    when every weight is zero the plain average is used."""
    if not items:
        return 0.0
    total = sum(max(_f(w), 0) for _, w in items)
    if total <= 0:
        return r2(sum(_f(p) for p, _ in items) / len(items))
    return r2(sum(_f(p) * max(_f(w), 0) for p, w in items) / total)


def objective_progress(key_results: list[tuple[float, float]], children: list[tuple[float, float]]) -> float:
    """An objective's progress: its key results and its aligned child objectives (each
    child counts as one more key result with its own weight)."""
    return weighted_progress(list(key_results) + list(children))


def expected_progress(start: date, end: date, today: date) -> float:
    """Where progress should be today if it moved in a straight line over the period."""
    if end <= start:
        return 100.0
    if today <= start:
        return 0.0
    if today >= end:
        return 100.0
    return r2((today - start).days / (end - start).days * 100)


DEFAULT_STATUSES = [
    {"key": "on_track", "label": "On track", "color": "emerald", "auto_min": 90},
    {"key": "at_risk", "label": "At risk", "color": "amber", "auto_min": 60},
    {"key": "behind", "label": "Behind", "color": "rose", "auto_min": 0},
    {"key": "done", "label": "Done", "color": "indigo", "auto_min": None},
    {"key": "paused", "label": "Paused", "color": "slate", "auto_min": None},
]


def auto_status(progress: float, expected: float, statuses: list[dict] | None = None) -> str:
    """Status from progress against where it should be: progress / expected as a %
    is compared with each status's `auto_min` (highest first). 100% progress is done."""
    statuses = statuses or DEFAULT_STATUSES
    keys = {s["key"] for s in statuses}
    if progress >= 100 and "done" in keys:
        return "done"
    ranked = sorted([s for s in statuses if s.get("auto_min") is not None], key=lambda s: -_f(s["auto_min"]))
    if not ranked:
        return statuses[0]["key"]
    ratio = 100.0 if expected <= 0 else progress / expected * 100
    for s in ranked:
        if ratio >= _f(s["auto_min"]):
            return s["key"]
    return ranked[-1]["key"]


FREQUENCY_DAYS = {"weekly": 7, "fortnightly": 14, "monthly": None, "none": None}


def planned_checkins(start: date, end: date, frequency: str) -> list[date]:
    """Planned check-in dates from the start to the end of a key result."""
    if frequency == "none" or not start or not end or end < start:
        return []
    out: list[date] = []
    if frequency == "monthly":
        d = start
        while True:
            y, m = (d.year + (d.month // 12), d.month % 12 + 1)
            day = min(start.day, 28)
            d = date(y, m, day)
            if d > end:
                break
            out.append(d)
    else:
        step = FREQUENCY_DAYS.get(frequency) or 7
        d = start + timedelta(days=step)
        while d <= end:
            out.append(d)
            d += timedelta(days=step)
    if not out or out[-1] != end:
        out.append(end)
    return out


def checkin_flags(planned: list[date], done: list[date], today: date, grace_days: int = 2) -> list[dict]:
    """For each planned date: 'on_time' (a check-in within the window before it, up to the
    grace days after), 'late' (the first check-in after the grace days but before the next
    planned date), 'missed' (none at all and the date has passed) or 'upcoming'."""
    done = sorted(done)
    used: set[int] = set()
    out = []
    prev = None
    for i, p in enumerate(planned):
        window_start = prev + timedelta(days=1) if prev else date.min
        nxt = planned[i + 1] if i + 1 < len(planned) else None
        due = p + timedelta(days=grace_days)
        on_time = [j for j, d in enumerate(done) if j not in used and window_start <= d <= due]
        late = [j for j, d in enumerate(done) if j not in used and due < d and (nxt is None or d <= nxt)]
        if on_time:
            state, j = "on_time", on_time[-1]
            used.update(on_time)
        elif late:
            state, j = "late", late[0]
            used.add(j)
        elif due < today:
            state, j = "missed", None
        else:
            state, j = "upcoming", None
        out.append({"planned": p.isoformat(), "state": state, "checked_in": done[j].isoformat() if j is not None else None})
        prev = p
    return out


# ── Reviews ─────────────────────────────────────────────────────────

def section_score(items: list[tuple[float | None, float]]) -> float | None:
    """Weighted average of (rating, weight); unrated items (None, e.g. N/A) are skipped."""
    rated = [(r, w) for r, w in items if r is not None]
    if not rated:
        return None
    return weighted_progress([(float(r), float(w)) for r, w in rated])


def form_score(sections: list[tuple[float | None, float]]) -> float | None:
    """A reviewer's overall score: section scores weighted by section weight; sections
    with nothing rated drop out and the rest are re-weighted."""
    return section_score(sections)


def combined_score(type_scores: dict[str, float | None], weightage: dict[str, float]) -> float | None:
    """Final score from each reviewer type's score (self, manager, peer, skip) and the
    cycle weightage. Types without a submitted score drop out and the rest re-weighted."""
    pairs = [(s, _f(weightage.get(t, 0))) for t, s in type_scores.items() if s is not None and _f(weightage.get(t, 0)) > 0]
    if not pairs:
        # fall back to the manager's score, then any score, when weights are all zero
        for t in ("manager", "skip", "peer", "self"):
            if type_scores.get(t) is not None:
                return round(float(type_scores[t]), 3)
        return None
    total = sum(w for _, w in pairs)
    return round(sum(float(s) * w for s, w in pairs) / total, 3)


def round_to_scale(score: float | None, values: list[float]) -> float | None:
    """Nearest rating on the scale (half rounds up)."""
    if score is None or not values:
        return None
    vals = sorted(float(v) for v in values)
    best = vals[0]
    for v in vals:
        if abs(score - v) < abs(score - best) - 1e-9 or (abs(abs(score - v) - abs(score - best)) < 1e-9 and v > best):
            best = v
    return best


def progress_to_rating(progress: float, values: list[float]) -> float | None:
    """Suggested goal rating from goal progress (0% = lowest point, 100% = highest)."""
    if not values:
        return None
    vals = sorted(float(v) for v in values)
    lo, hi = vals[0], vals[-1]
    return round_to_scale(lo + (hi - lo) * clamp(progress) / 100, vals)


def distribution(ratings: list[float | None], targets: dict, values: list[float]) -> list[dict]:
    """Calibration view: for every rating on the scale, how many people have it now, the
    target share and the head-count that share means (largest remainder, so targets add
    up to everyone), and the gap."""
    vals = sorted(float(v) for v in values)
    rated = [float(r) for r in ratings if r is not None]
    n = len(rated)
    tgt = {float(k): _f(v) for k, v in (targets or {}).items()}
    raw = {v: tgt.get(v, 0.0) * n / 100 for v in vals}
    counts = {v: int(raw[v]) for v in vals}
    if sum(tgt.values()) > 0:
        left = n - sum(counts.values())
        for v in sorted(vals, key=lambda v: (-(raw[v] - int(raw[v])), -v))[:max(left, 0)]:
            counts[v] += 1
    out = []
    for v in vals:
        actual = sum(1 for r in rated if r == v)
        out.append({
            "value": v, "count": actual, "actual_pct": r2(actual / n * 100) if n else 0.0,
            "target_pct": tgt.get(v, 0.0), "target_count": counts[v] if sum(tgt.values()) > 0 else None,
            "gap": (actual - counts[v]) if sum(tgt.values()) > 0 else None,
        })
    return out


def performance_level(rating: float | None, values: list[float]) -> int | None:
    """1 low, 2 medium, 3 high: the scale split into thirds."""
    if rating is None or not values:
        return None
    lo, hi = min(values), max(values)
    if hi == lo:
        return 2
    pos = (float(rating) - lo) / (hi - lo)
    return 1 if pos < 1 / 3 else (2 if pos < 2 / 3 else 3)


def nine_box(rating: float | None, potential: int | None, values: list[float]) -> int | None:
    """Box number 1..9: rows by potential (3 at the top), columns by performance.
    Box = (potential - 1) * 3 + performance level."""
    perf = performance_level(rating, values)
    if perf is None or potential not in (1, 2, 3):
        return None
    return (potential - 1) * 3 + perf


# ── Increments ──────────────────────────────────────────────────────

def compa_ratio(current_ctc, midpoint) -> float | None:
    if not midpoint or _f(midpoint) <= 0:
        return None
    return round(_f(current_ctc) / _f(midpoint), 3)


def guideline_pct(rating: float | None, compa: float | None, guidelines: dict) -> float | None:
    """Look up the suggested increment % in the matrix: the row for the rating (nearest
    lower rating row) and the compa-ratio band that contains the ratio (first band when the
    ratio is unknown)."""
    rows = sorted(guidelines.get("rows") or [], key=lambda r: _f(r.get("rating")))
    bands = guidelines.get("bands") or []
    if rating is None or not rows or not bands:
        return None
    row = None
    for r in rows:
        if _f(r.get("rating")) <= float(rating) + 1e-9:
            row = r
    if row is None:
        row = rows[0]
    idx = 0
    if compa is not None:
        idx = len(bands) - 1
        for i, b in enumerate(bands):
            lo = _f(b.get("min")) if b.get("min") is not None else float("-inf")
            hi = _f(b.get("max")) if b.get("max") is not None else float("inf")
            if lo <= compa < hi:
                idx = i
                break
    pcts = row.get("pcts") or []
    return _f(pcts[idx]) if idx < len(pcts) else None


def money(x) -> Decimal:
    return Decimal(str(x or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def increment(current_ctc, pct) -> tuple[Decimal, Decimal]:
    """(increment amount, new CTC), to 2 decimal places."""
    amount = money(Decimal(str(current_ctc or 0)) * Decimal(str(pct or 0)) / 100)
    return amount, money(Decimal(str(current_ctc or 0)) + amount)
