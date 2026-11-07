"""Built-in question matcher used when no AI model is set up. It recognises common HR
questions and answers them with the same safe tools the model would use."""
from __future__ import annotations

import re

GROUP_WORDS = {
    "department": "department", "departments": "department", "dept": "department", "team": "department",
    "location": "location", "locations": "location", "office": "location", "offices": "location", "city": "location",
    "branch": "location", "designation": "designation", "designations": "designation", "role": "designation",
    "title": "designation", "gender": "gender", "male": "gender", "female": "gender", "women": "gender",
    "grade": "grade", "band": "grade", "entity": "legal_entity", "company": "legal_entity",
    "type": "employment_type", "employment type": "employment_type", "contract": "employment_type",
    "age": "age_group", "service": "service", "tenure": "service", "state": "state", "stage": "stage",
}
PERIODS = [("last month", "last_month"), ("this month", "this_month"), ("this quarter", "this_quarter"),
           ("this financial year", "this_fy"), ("this fy", "this_fy"), ("this year", "this_year"),
           ("last year", "this_year")]
REPORT_WORDS = {"birthday": "birthdays", "birthdays": "birthdays", "probation": "probation_due",
                "attrition": "attrition", "turnover": "attrition", "overtime": "overtime",
                "documents expir": "documents_expiring", "expiring document": "documents_expiring",
                "missing pan": "missing_statutory", "missing uan": "missing_statutory",
                "missing bank": "missing_statutory", "payroll register": "payroll_register",
                "statutory summary": "statutory_summary", "muster": "muster_roll", "late": "late_early",
                "help desk": "helpdesk_sla", "helpdesk": "helpdesk_sla", "training": "training_completion",
                "survey": "survey_results", "hiring funnel": "hiring_funnel", "funnel": "hiring_funnel",
                "gender mix": "gender_mix", "diversity": "diversity_department", "lop": "lop",
                "loss of pay": "lop", "ctc": "ctc", "payroll cost": "payroll_cost"}

EXAMPLES = [
    "How many people do we have by department?",
    "Who is on leave today?",
    "What is my leave balance?",
    "Who joined this month?",
    "Show the attendance summary for last month",
    "What is waiting for my approval?",
    "Find Priya",
    "Run the birthdays report",
]


def _period(t: str) -> str:
    for word, key in PERIODS:
        if word in t:
            return key
    m = re.search(r"last (\d+) days", t)
    if m:
        return f"last_n_days:{m.group(1)}"
    return "this_month"


def match(text: str) -> tuple[str, dict] | None:
    t = " " + re.sub(r"\s+", " ", (text or "").lower()).strip(" ?.!") + " "
    if not t.strip():
        return None
    if re.search(r"(approv|waiting for me|waiting on me|pending for me|my inbox|to approve)", t):
        return "pending_approvals", {}
    if re.search(r"(on leave|is off|are off|away|absent today|out of office)", t) and "balance" not in t:
        day = "tomorrow" if "tomorrow" in t else "today"
        m = re.search(r"(\d{4}-\d{2}-\d{2})", t)
        return "who_is_on_leave", {"date": m.group(1) if m else day}
    if "balance" in t or re.search(r"how many (leaves|leave days)", t):
        m = re.search(r"(?:balance (?:of|for)|for) ([a-z][a-z .']+?)(?: \?|$| this| in )", t)
        who = "me" if re.search(r"\b(my|mine|i have|do i)\b", t) or not m else m.group(1).strip()
        lt = None
        for word in ("earned", "casual", "sick", "privilege", "comp off", "optional"):
            if word in t:
                lt = word
        args = {"employee": who}
        if lt:
            args["leave_type"] = lt
        return "leave_balances", args
    if re.search(r"(joined|joiners|new hires|new joiners|onboarded)", t):
        p = _period(t)
        args = {"kind": "joiners", "period": p}
        if p.startswith("last_n_days:"):
            args = {"kind": "joiners", "period": "last_n_days", "days": int(p.split(":")[1])}
        return "joiners_leavers", args
    if re.search(r"(who left|leavers|resigned|exited|exits)", t):
        p = _period(t)
        args = {"kind": "leavers", "period": p}
        if p.startswith("last_n_days:"):
            args = {"kind": "leavers", "period": "last_n_days", "days": int(p.split(":")[1])}
        return "joiners_leavers", args
    if "attendance" in t or "present" in t or "late marks" in t:
        args = {"period": "last_month" if "last month" in t else "this_month"}
        if re.search(r"\b(my|mine|me)\b", t):
            args["employee"] = "me"
        return "attendance_summary", args
    if re.search(r"(how many|headcount|head count|number of|count of|total) .*(people|employees|staff|headcount|"
                 r"workforce|members|us)|headcount|head count", t):
        args: dict = {}
        m = re.search(r"\bby ([a-z ]+?) ?$", t.strip()) or re.search(r"\bper ([a-z ]+?) ?$", t.strip())
        if m:
            key = m.group(1).strip()
            args["group_by"] = GROUP_WORDS.get(key) or GROUP_WORDS.get(key.split(" ")[0])
        else:
            for w, g in GROUP_WORDS.items():
                if re.search(rf"\b(each|every|per|by) {w}\b", t):
                    args["group_by"] = g
                    break
        m = re.search(r"\bin (?:the )?([a-z][a-z &]+?)(?: department| team)? ?$", t.strip())
        if m and not args.get("group_by"):
            args["department"] = m.group(1).strip()
        if args.get("group_by") is None:
            args.pop("group_by", None)
        return "headcount", args
    for word, key in REPORT_WORDS.items():
        if word in t:
            return "run_report", {"report": key}
    m = re.search(r"\b(?:run|show|open) (?:the |me the |my )?(.+?) report\b", t)
    if m:
        return "run_report", {"report": m.group(1).strip()}
    if re.search(r"(list|which|what) reports", t):
        return "list_reports", {}
    m = re.search(r"\b(?:find|search for|search|look up|lookup|who is|who's|details of|contact of|email of)"
                  r" ([a-z0-9@._ -]{2,60})", t)
    if m:
        q = re.sub(r"\b(the|our|employee|person)\b", "", m.group(1)).strip()
        if q and q not in ("on leave", "away"):
            return "search_employees", {"query": q}
    return None
