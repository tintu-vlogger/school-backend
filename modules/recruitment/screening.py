"""Resume screening: a score from 0 to 100 against the job, with a plain explanation.

Scorers are pluggable. The built-in "rules" scorer compares skills, experience, nice-to-have
skills and location. Another scorer (for example an AI model) registers itself with
`register("ai", fn)` and is chosen in the hiring settings; it receives the same inputs
and must return the same shape, so the screens and the pipeline do not change.

    result = score(job_dict, candidate_dict, weights)
    -> {"score": 72, "scorer": "rules", "parts": [{"key", "label", "points", "max", "detail"}], "explanation": "..."}
"""
from __future__ import annotations

import re
from typing import Callable

_SCORERS: dict[str, Callable[[dict, dict, dict], dict]] = {}


def register(name: str, fn: Callable[[dict, dict, dict], dict]) -> None:
    _SCORERS[name] = fn


def available() -> list[str]:
    return sorted(_SCORERS)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#.]", "", (s or "").lower())


def _has_skill(skill: str, cand_skills: set[str], text: str) -> bool:
    n = _norm(skill)
    if not n:
        return False
    if n in cand_skills:
        return True
    # whole-word match in the resume text (so "Java" does not match "JavaScript")
    pattern = r"(?<![a-z0-9+#])" + re.escape(skill.lower().strip()) + r"(?![a-z0-9+#])"
    return re.search(pattern, text) is not None


def rules_scorer(job: dict, cand: dict, weights: dict) -> dict:
    w = {"skills": 50, "experience": 30, "nice_skills": 10, "location": 10, **(weights or {})}
    text = (cand.get("resume_text") or "").lower() + " " + " ".join(cand.get("skills") or []).lower()
    cand_skills = {_norm(s) for s in (cand.get("skills") or [])}
    parts = []

    must = [s for s in (job.get("skills") or []) if str(s).strip()]
    if must:
        hit = [s for s in must if _has_skill(s, cand_skills, text)]
        miss = [s for s in must if s not in hit]
        pts = w["skills"] * len(hit) / len(must)
        detail = f"Has {len(hit)} of {len(must)} required skills"
        if hit:
            detail += f" ({', '.join(hit[:6])})"
        if miss:
            detail += f"; missing {', '.join(miss[:6])}"
        parts.append({"key": "skills", "label": "Required skills", "points": round(pts, 1), "max": w["skills"],
                      "detail": detail, "matched": hit, "missing": miss})
    else:
        parts.append({"key": "skills", "label": "Required skills", "points": w["skills"], "max": w["skills"],
                      "detail": "The job lists no required skills", "matched": [], "missing": []})

    exp = cand.get("total_experience")
    lo, hi = job.get("min_experience"), job.get("max_experience")
    if exp is None:
        pts, detail = w["experience"] * 0.4, "Experience not known"
    elif lo is None and hi is None:
        pts, detail = w["experience"], f"{exp:g} years; the job sets no range"
    elif lo is not None and exp < lo:
        gap = lo - exp
        pts = max(0.0, w["experience"] * (1 - gap / max(lo, 1)))
        detail = f"{exp:g} years, {gap:g} below the {lo:g}-year minimum"
    elif hi is not None and exp > hi + 3:
        pts, detail = w["experience"] * 0.6, f"{exp:g} years, well above the {hi:g}-year maximum"
    else:
        pts, detail = w["experience"], f"{exp:g} years, within the range"
    parts.append({"key": "experience", "label": "Experience", "points": round(pts, 1), "max": w["experience"],
                  "detail": detail})

    nice = [s for s in (job.get("nice_skills") or []) if str(s).strip()]
    if nice:
        hit = [s for s in nice if _has_skill(s, cand_skills, text)]
        pts = w["nice_skills"] * len(hit) / len(nice)
        parts.append({"key": "nice_skills", "label": "Good-to-have skills", "points": round(pts, 1),
                      "max": w["nice_skills"], "detail": f"Has {len(hit)} of {len(nice)}"
                      + (f" ({', '.join(hit[:5])})" if hit else "")})
    else:
        parts.append({"key": "nice_skills", "label": "Good-to-have skills", "points": w["nice_skills"],
                      "max": w["nice_skills"], "detail": "None listed"})

    loc_job = (job.get("location") or "").lower()
    loc_c = (cand.get("location") or "").lower()
    if job.get("work_mode") == "remote" or not loc_job:
        pts, detail = w["location"], "Location does not matter for this job"
    elif loc_c and (loc_job in loc_c or loc_c in loc_job):
        pts, detail = w["location"], f"Based in {cand.get('location')}"
    elif not loc_c:
        pts, detail = w["location"] * 0.5, "Location not known"
    else:
        pts, detail = w["location"] * 0.3, f"Based in {cand.get('location')}, the job is in {job.get('location')}"
    parts.append({"key": "location", "label": "Location", "points": round(pts, 1), "max": w["location"],
                  "detail": detail})

    total_max = sum(p["max"] for p in parts) or 1
    score = round(100 * sum(p["points"] for p in parts) / total_max)
    band = "strong match" if score >= 75 else "possible match" if score >= 50 else "weak match"
    explanation = f"{score}/100, a {band}. " + ". ".join(p["detail"] for p in parts) + "."
    return {"score": int(score), "scorer": "rules", "parts": parts, "explanation": explanation}


register("rules", rules_scorer)


def score(job: dict, cand: dict, weights: dict | None = None, scorer: str = "rules") -> dict:
    fn = _SCORERS.get(scorer) or _SCORERS["rules"]
    try:
        out = fn(job, cand, weights or {})
    except Exception:  # a plug-in scorer that fails never blocks hiring
        out = rules_scorer(job, cand, weights or {})
    out["score"] = max(0, min(100, int(out.get("score") or 0)))
    return out
