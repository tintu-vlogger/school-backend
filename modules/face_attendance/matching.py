"""Face template maths, kept free of the database so it is easy to test.

A face template is a list of numbers (an "embedding") made in the browser by the face
model from one face sample. Two templates of the same person point in nearly the same
direction, so we compare them with cosine similarity:

    similarity = cos(angle) between the two vectors   (1 = identical, 0 = unrelated)
    distance   = 1 - similarity

A probe matches a person when the distance to their closest enrolled sample is at most
the threshold, and (when several people are compared) the best person is ahead of the
next one by at least the margin, so two similar-looking people are never confused.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

MIN_DIMS = 64
MAX_DIMS = 2048
MIN_SAMPLES = 3
MAX_SAMPLES = 5


class TemplateError(ValueError):
    pass


def clean_vector(v, dims: int | None = None) -> list[float]:
    """Check one template and return it scaled to length 1."""
    if not isinstance(v, (list, tuple)):
        raise TemplateError("A face template must be a list of numbers.")
    if not (MIN_DIMS <= len(v) <= MAX_DIMS):
        raise TemplateError("The face template has the wrong size.")
    if dims is not None and len(v) != dims:
        raise TemplateError("The face template was made by a different face model.")
    out: list[float] = []
    for x in v:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise TemplateError("The face template has values that are not numbers.")
        out.append(float(x))
    norm = math.sqrt(sum(x * x for x in out))
    if norm < 1e-9:
        raise TemplateError("The face template is empty.")
    return [x / norm for x in out]


def similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two templates (any length; they are normalised here)."""
    if len(a) != len(b):
        raise TemplateError("Face templates of different sizes cannot be compared.")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def distance(a: list[float], b: list[float]) -> float:
    return 1.0 - similarity(a, b)


def best_similarity(probe: list[float], samples: list[list[float]]) -> float:
    """How alike the probe is to the closest of one person's samples."""
    return max((similarity(probe, s) for s in samples), default=0.0)


def is_match(score: float, threshold: float) -> bool:
    """score is a similarity, threshold a cosine distance limit."""
    return (1.0 - score) <= threshold + 1e-9


@dataclass
class MatchResult:
    matched: bool
    key: object | None  # who matched (or the closest person when not matched)
    score: float  # similarity to `key`
    second_score: float | None
    reason: str  # matched | below_threshold | ambiguous | empty


def identify(probe: list[float], gallery: dict, threshold: float, margin: float = 0.0) -> MatchResult:
    """1:N search: which enrolled person (gallery key -> list of samples) is this?"""
    scored = sorted(((best_similarity(probe, samples), key) for key, samples in gallery.items() if samples),
                    key=lambda t: t[0], reverse=True)
    if not scored:
        return MatchResult(False, None, 0.0, None, "empty")
    top_score, top_key = scored[0]
    second = scored[1][0] if len(scored) > 1 else None
    if not is_match(top_score, threshold):
        return MatchResult(False, top_key, top_score, second, "below_threshold")
    if second is not None and is_match(second, threshold) and top_score - second < margin:
        return MatchResult(False, top_key, top_score, second, "ambiguous")
    return MatchResult(True, top_key, top_score, second, "matched")


def verify(probe: list[float], samples: list[list[float]], threshold: float) -> tuple[bool, float]:
    """1:1 check: is this the person with these samples? Returns (matched, similarity)."""
    score = best_similarity(probe, samples)
    return is_match(score, threshold), score


def sample_quality(samples: list[list[float]]) -> float:
    """Lowest similarity between any two samples of one enrolment (1 = all alike)."""
    if len(samples) < 2:
        return 1.0
    return min(similarity(samples[i], samples[j]) for i in range(len(samples)) for j in range(i + 1, len(samples)))


def check_samples(raw: list, threshold: float) -> tuple[list[list[float]], float]:
    """Validate 3 to 5 enrolment samples: same size, and all of the same face."""
    if not isinstance(raw, list) or not (MIN_SAMPLES <= len(raw) <= MAX_SAMPLES):
        raise TemplateError(f"Take {MIN_SAMPLES} to {MAX_SAMPLES} face samples.")
    first = clean_vector(raw[0])
    samples = [first] + [clean_vector(v, len(first)) for v in raw[1:]]
    quality = sample_quality(samples)
    # samples of one sitting should be closer to each other than a normal match needs
    if not is_match(quality, threshold * 1.25):
        raise TemplateError("The samples don't look like the same face. Keep one face in front of the camera "
                            "and try again in good light.")
    return samples, quality
