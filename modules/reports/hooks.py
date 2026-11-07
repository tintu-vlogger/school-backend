"""Permissions for reports, dashboards, data import/export, compliance and integrations, and
the periodic job that emails scheduled reports."""
from core import periodic
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

INSIGHTS_PERMISSIONS = {
    "insights.reports.manage": "Share reports with roles and people, and email them on a schedule",
    "insights.reports.all_data": "See everyone's rows in reports (pay data still needs payroll access)",
    "insights.dashboards.manage": "Build dashboards and share them",
    "insights.data.import": "Import data from Excel or CSV",
    "insights.data.export": "Export data in the import format",
    "insights.compliance.view": "See the compliance dashboard",
    "insights.mcp.connect": "Connect outside AI assistants with a personal access token",
    "settings.integrations.manage": "Set up integrations: AI model, email, accounting, sign-in and others",
}
for _k, _v in INSIGHTS_PERMISSIONS.items():
    ALL_PERMISSIONS.setdefault(_k, _v)
_GRANTS = {
    "admin": list(INSIGHTS_PERMISSIONS),
    "hr": ["insights.reports.manage", "insights.reports.all_data", "insights.dashboards.manage",
           "insights.data.import", "insights.data.export", "insights.compliance.view", "insights.mcp.connect"],
    "manager": ["insights.mcp.connect"],
}
for _role, _perms in _GRANTS.items():
    lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    for _p in _perms:
        if _p not in lst:
            lst.append(_p)


def _send_due(db) -> int:
    from modules.reports.service import run_due
    return run_due(db)


periodic.register("reports.schedules", _send_due)
