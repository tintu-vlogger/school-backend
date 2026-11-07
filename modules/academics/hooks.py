"""Permissions owned by the academics module."""
from modules.roles.models import register_permissions

register_permissions(
    {
        "academics.view": "See the academic structure: sessions, classes, sections, subjects and rooms",
        "academics.manage": "Set up sessions, terms, classes or programmes, sections, subjects and teacher allocation",
        "academics.profile.manage": "Change the institution profile: type, board, words used and modules switched on",
    },
    {
        "principal": ["academics.view", "academics.manage"],
        "hr": ["academics.view"],
        "manager": ["academics.view"],
        "teacher": ["academics.view"],
        "accountant": ["academics.view"],
        "librarian": ["academics.view"],
        "employee": ["academics.view"],
    },
)
