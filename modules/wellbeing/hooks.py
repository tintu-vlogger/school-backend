"""Wellbeing permissions, number series for gate and visitor passes, and portal paths."""
from core.numbering import register_series
from core.security import register_portal_prefix
from modules.roles.models import register_permissions

register_permissions(
    {
        "wellbeing.view": "See health records, infirmary visits, discipline, merit points and the visitor log",
        "wellbeing.record": "Record infirmary visits, incidents, merit points, visitors and gate passes",
        "wellbeing.manage": "Run wellbeing: health records, check-ups, confidential incidents and settings",
        "wellbeing.counselling": "Read and write counselling notes (restricted)",
    },
    {
        "principal": ["wellbeing.view", "wellbeing.record", "wellbeing.manage"],
        "teacher": ["wellbeing.view", "wellbeing.record"],
        "hr": ["wellbeing.view", "wellbeing.record"],
        "manager": ["wellbeing.view", "wellbeing.record"],
    },
)

register_series("gate_pass", "Gate passes", "GP", "{PREFIX}{YY}{MM}-{SEQ:4}", "monthly", True,
                "Early pickup passes issued at the gate.")
register_series("visitor_pass", "Visitor passes", "VP", "{PREFIX}{YY}{MM}-{SEQ:4}", "monthly", True,
                "Passes for visitors signed in at the gate.")

register_portal_prefix("/wellbeing/portal")
