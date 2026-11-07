"""Travel policy: defaults, city tiers and the checks a trip goes through.

A policy version holds `rules`:

    city_tiers         {"X": [cities], "Y": [cities]}; every other Indian city is tier Z
    bands              grade bands, first match wins (empty grade_codes = everyone):
                       {name, grade_codes, modes: {mode: [classes allowed]},
                        hotel: {X, Y, Z, international} nightly limit,
                        per_diem: {X, Y, Z, international} daily allowance,
                        lead_days: {domestic, international}}
    advance_max_percent  highest advance as a share of the estimated cost
    claim_categories   expense category codes used when a trip is settled
                       {ticket, hotel, per_diem}

Tiers follow the X / Y / Z city classification used for house rent allowance (7th Pay
Commission, 2015). Companies edit the lists on the policy screen.

Everything here is pure (no database) so the rules are easy to test.
"""
from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal

MODES = {
    "flight": ("Flight", {"economy": "Economy", "premium_economy": "Premium economy", "business": "Business"}),
    "train": ("Train", {"SL": "Sleeper (SL)", "3E": "AC 3 economy (3E)", "3A": "AC 3 tier (3A)", "2A": "AC 2 tier (2A)",
                        "1A": "AC first class (1A)", "CC": "AC chair car (CC)", "EC": "Executive chair car (EC)"}),
    "bus": ("Bus", {"non_ac": "Non-AC", "ac_seater": "AC seater", "ac_sleeper": "AC sleeper"}),
    "cab": ("Cab", {"hatchback": "Hatchback", "sedan": "Sedan", "suv": "SUV"}),
    "own_vehicle": ("Own vehicle", {"car": "Car", "two_wheeler": "Two-wheeler"}),
}
TIERS = ("X", "Y", "Z", "international")
TIER_LABELS = {"X": "Tier X (metro)", "Y": "Tier Y", "Z": "Tier Z", "international": "International"}

X_CITIES = ["Ahmedabad", "Bengaluru", "Chennai", "Delhi", "Hyderabad", "Kolkata", "Mumbai", "Pune"]
Y_CITIES = [
    "Agra", "Ajmer", "Aligarh", "Amravati", "Amritsar", "Asansol", "Aurangabad", "Bareilly", "Belagavi", "Bhavnagar",
    "Bhiwandi", "Bhopal", "Bhubaneswar", "Bikaner", "Bokaro", "Chandigarh", "Coimbatore", "Cuttack", "Dehradun",
    "Dhanbad", "Bhilai", "Durgapur", "Erode", "Faridabad", "Firozabad", "Ghaziabad", "Gorakhpur", "Kalaburagi",
    "Guntur", "Gurugram", "Guwahati", "Gwalior", "Hubballi", "Indore", "Jabalpur", "Jaipur", "Jalandhar", "Jammu",
    "Jamnagar", "Jamshedpur", "Jhansi", "Jodhpur", "Kakinada", "Kannur", "Kanpur", "Kochi", "Kolhapur", "Kollam",
    "Kota", "Kozhikode", "Kurnool", "Lucknow", "Ludhiana", "Madurai", "Malappuram", "Mangaluru", "Mathura",
    "Meerut", "Moradabad", "Mysuru", "Nagpur", "Nanded", "Nashik", "Nellore", "Noida", "Palakkad", "Patna",
    "Puducherry", "Prayagraj", "Raipur", "Rajkot", "Rajahmundry", "Ranchi", "Rourkela", "Salem", "Sangli",
    "Siliguri", "Solapur", "Srinagar", "Surat", "Thiruvananthapuram", "Thrissur", "Tiruchirappalli",
    "Tirunelveli", "Tiruppur", "Ujjain", "Vadodara", "Varanasi", "Vasai-Virar", "Vijayawada", "Visakhapatnam",
    "Vellore", "Warangal",
]
# Older or other spellings people type
ALIASES = {
    "bangalore": "bengaluru", "bombay": "mumbai", "madras": "chennai", "calcutta": "kolkata", "new delhi": "delhi",
    "gurgaon": "gurugram", "trivandrum": "thiruvananthapuram", "cochin": "kochi", "ernakulam": "kochi",
    "mysore": "mysuru", "mangalore": "mangaluru", "allahabad": "prayagraj", "pondicherry": "puducherry",
    "calicut": "kozhikode", "trichy": "tiruchirappalli", "vizag": "visakhapatnam", "baroda": "vadodara",
    "belgaum": "belagavi", "hubli": "hubballi", "gulbarga": "kalaburagi", "navi mumbai": "mumbai", "thane": "mumbai",
    "secunderabad": "hyderabad", "greater noida": "noida", "benaras": "varanasi", "banaras": "varanasi",
}

DEFAULT_RULES: dict = {
    "city_tiers": {"X": X_CITIES, "Y": Y_CITIES},
    "bands": [
        {"name": "Leadership", "grade_codes": ["G6", "G7"],
         "modes": {"flight": ["economy", "premium_economy", "business"], "train": ["1A", "2A", "EC", "CC"],
                   "bus": ["ac_sleeper", "ac_seater"], "cab": ["sedan", "suv"], "own_vehicle": ["car"]},
         "hotel": {"X": 12000, "Y": 9000, "Z": 7000, "international": 20000},
         "per_diem": {"X": 2500, "Y": 2000, "Z": 1500, "international": 6000},
         "lead_days": {"domestic": 3, "international": 14}},
        {"name": "Managers", "grade_codes": ["G4", "G5"],
         "modes": {"flight": ["economy", "premium_economy"], "train": ["2A", "3A", "CC", "EC"],
                   "bus": ["ac_sleeper", "ac_seater"], "cab": ["hatchback", "sedan"], "own_vehicle": ["car", "two_wheeler"]},
         "hotel": {"X": 7000, "Y": 5500, "Z": 4000, "international": 14000},
         "per_diem": {"X": 1500, "Y": 1200, "Z": 1000, "international": 4500},
         "lead_days": {"domestic": 7, "international": 21}},
        {"name": "Everyone else", "grade_codes": [],
         "modes": {"flight": ["economy"], "train": ["3A", "3E", "CC", "SL"], "bus": ["ac_sleeper", "ac_seater", "non_ac"],
                   "cab": ["hatchback"], "own_vehicle": ["car", "two_wheeler"]},
         "hotel": {"X": 4500, "Y": 3500, "Z": 2500, "international": 10000},
         "per_diem": {"X": 1000, "Y": 800, "Z": 600, "international": 3500},
         "lead_days": {"domestic": 7, "international": 21}},
    ],
    "advance_max_percent": 80,
    "claim_categories": {"ticket": "TRAVEL", "hotel": "HOTEL", "per_diem": "PERDIEM"},
}


def default_rules() -> dict:
    return copy.deepcopy(DEFAULT_RULES)


def d(v) -> Decimal:
    if v is None or v == "":
        return Decimal("0")
    return Decimal(str(v))


def _norm(city: str | None) -> str:
    c = " ".join((city or "").lower().replace(",", " ").split())
    return ALIASES.get(c, c)


def city_tier(rules: dict, city: str | None, international: bool = False) -> str:
    if international:
        return "international"
    name = _norm(city)
    tiers = rules.get("city_tiers") or {}
    for t in ("X", "Y"):
        if name in {_norm(x) for x in tiers.get(t) or []}:
            return t
    return "Z"


def band_for(rules: dict, grade_code: str | None) -> dict:
    bands = rules.get("bands") or []
    for b in bands:
        codes = b.get("grade_codes") or []
        if not codes or (grade_code and grade_code in codes):
            return b
    return bands[-1] if bands else {}


def destinations(trip: dict) -> list[str]:
    """Places the traveller stays in: hotel cities, else where the legs go (not the way home)."""
    stays = [s.get("city") for s in trip.get("stays") or [] if s.get("city")]
    if stays:
        return stays
    legs = trip.get("legs") or []
    places = [l.get("to_place") for l in legs if l.get("to_place")]
    origin = legs[0].get("from_place") if legs else None
    return [p for p in places if _norm(p) != _norm(origin)] or places


def per_diem(rules: dict, grade_code: str | None, trip: dict) -> tuple[int, Decimal]:
    """(days, daily rate). Days run from the start to the end date inclusive; the rate is
    the highest of the tiers the trip goes to."""
    start, end = trip.get("start_date"), trip.get("end_date")
    if not start or not end or end < start:
        return 0, Decimal("0")
    intl = trip.get("travel_type") == "international"
    band = band_for(rules, grade_code)
    rates = band.get("per_diem") or {}
    tiers = {city_tier(rules, c, intl) for c in destinations(trip)} or {"Z" if not intl else "international"}
    rate = max((d(rates.get(t)) for t in tiers), default=Decimal("0"))
    return (end - start).days + 1, rate


def nights(stay: dict) -> int:
    a, b = stay.get("check_in"), stay.get("check_out")
    if not a or not b:
        return 0
    return max((b - a).days, 1)


def estimate(rules: dict, grade_code: str | None, trip: dict) -> dict:
    days, rate = per_diem(rules, grade_code, trip)
    legs = sum((d(l.get("estimated_cost")) for l in trip.get("legs") or []), Decimal("0"))
    stays = sum((d(s.get("nightly_cost")) * nights(s) for s in trip.get("stays") or []), Decimal("0"))
    pd = rate * days
    return {"legs": legs, "stays": stays, "per_diem_days": days, "per_diem_rate": rate, "per_diem": pd,
            "total": legs + stays + pd}


def check(rules: dict, grade_code: str | None, trip: dict, today: date) -> list[dict]:
    """Every way the trip goes outside the policy, as {code, message, where}. An empty list
    means the trip is within policy."""
    flags: list[dict] = []
    intl = trip.get("travel_type") == "international"
    band = band_for(rules, grade_code)
    allowed = band.get("modes") or {}
    for i, leg in enumerate(trip.get("legs") or [], start=1):
        mode, cls = leg.get("mode"), leg.get("travel_class")
        where = f"Journey {i}"
        mode_label = MODES.get(mode, (mode, {}))[0]
        if mode not in allowed:
            flags.append({"code": "mode", "where": where, "message": f"{mode_label} is not allowed for your grade"})
            continue
        classes = allowed.get(mode) or []
        if cls and classes and cls not in classes:
            label = MODES.get(mode, ("", {}))[1].get(cls, cls)
            flags.append({"code": "class", "where": where, "message": f"{mode_label} {label} is above your grade"})
        start, end = trip.get("start_date"), trip.get("end_date")
        dd = leg.get("depart_date")
        if dd and start and end and not (start <= dd <= end):
            flags.append({"code": "dates", "where": where, "message": "Journey date is outside the trip dates"})
    hotel = band.get("hotel") or {}
    for i, s in enumerate(trip.get("stays") or [], start=1):
        tier = city_tier(rules, s.get("city"), intl)
        cap = hotel.get(tier)
        if cap is not None and d(s.get("nightly_cost")) > d(cap):
            flags.append({"code": "hotel", "where": f"Stay {i}",
                          "message": f"Hotel in {s.get('city')} is above the {TIER_LABELS[tier].lower()} limit of ₹{d(cap):,.0f} a night"})
    lead = (band.get("lead_days") or {}).get("international" if intl else "domestic")
    start = trip.get("start_date")
    if lead and start and (start - today).days < int(lead):
        flags.append({"code": "lead_time", "where": "Trip",
                      "message": f"Asked {max((start - today).days, 0)} day(s) ahead; the policy asks for {lead}"})
    pct = rules.get("advance_max_percent")
    total = d(trip.get("estimated_total"))
    adv = d(trip.get("advance_amount"))
    if adv > 0 and pct is not None and total > 0 and adv > total * d(pct) / 100:
        flags.append({"code": "advance", "where": "Advance",
                      "message": f"Advance is more than {pct}% of the estimated cost"})
    return flags


def clean_rules(rules: dict) -> dict:
    """Validate a policy edited on screen. Raises ValueError with a plain message."""
    out = default_rules()
    out.update({k: v for k, v in (rules or {}).items() if k in out})
    if not isinstance(out["bands"], list) or not out["bands"]:
        raise ValueError("Add at least one grade band.")
    for b in out["bands"]:
        if not (b.get("name") or "").strip():
            raise ValueError("Give every grade band a name.")
        modes = b.get("modes") or {}
        for m, classes in modes.items():
            if m not in MODES:
                raise ValueError(f"Unknown travel mode {m}.")
            bad = [c for c in classes or [] if c not in MODES[m][1]]
            if bad:
                raise ValueError(f"Unknown class {bad[0]} for {MODES[m][0].lower()}.")
        for key in ("hotel", "per_diem"):
            for t, v in (b.get(key) or {}).items():
                if t not in TIERS or (v is not None and d(v) < 0):
                    raise ValueError("Limits must be zero or more for tiers X, Y, Z and international.")
        b["grade_codes"] = [str(x) for x in b.get("grade_codes") or []]
    if not any(not (b.get("grade_codes") or []) for b in out["bands"]):
        raise ValueError("Keep one band for everyone else (with no grades picked).")
    pct = out.get("advance_max_percent")
    if pct is not None and not (0 <= float(pct) <= 100):
        raise ValueError("The advance limit must be between 0 and 100 percent.")
    return out


def options() -> dict:
    return {"modes": [{"value": k, "label": v[0], "classes": [{"value": c, "label": l} for c, l in v[1].items()]}
                      for k, v in MODES.items()],
            "tiers": [{"value": t, "label": TIER_LABELS[t]} for t in TIERS]}
