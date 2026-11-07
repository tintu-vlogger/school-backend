"""An employee's usual working week: which days they work and their shift timings.

Stored in employee_settings.config["timing"] as
    {"Monday": {"working": true, "slots": [{"start": "09:00", "end": "18:00"}]}, ...}
Older data may hold each day as a list of [start, end] pairs; both are accepted."""

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _day(value) -> dict:
    if isinstance(value, list):  # [["09:00", "18:00"], ...]
        slots = [{"start": a, "end": b} for a, b in value if a and b]
        return {"working": bool(slots), "slots": slots}
    if isinstance(value, dict):
        slots = [
            {"start": s.get("start"), "end": s.get("end")}
            for s in (value.get("slots") or [])
            if isinstance(s, dict) and s.get("start") and s.get("end")
        ]
        return {"working": bool(value.get("working", bool(slots))), "slots": slots}
    return {"working": False, "slots": []}


def normalize_work_arrangement(incoming: dict | None, existing: dict | None = None) -> dict:
    """Merge an update into the stored settings and return a clean copy."""
    config = dict(existing or {})
    incoming = dict(incoming or {})
    timing = incoming.pop("timing", config.get("timing") or {})
    config["timing"] = {day: _day((timing or {}).get(day)) for day in DAYS}
    config.update(incoming)  # other settings, e.g. "background"
    return config


DEFAULT_WORK_ARRANGEMENT = normalize_work_arrangement({
    "timing": {
        **{d: [["09:30", "18:30"]] for d in DAYS[:5]},
        "Saturday": [],
        "Sunday": [],
    },
})
