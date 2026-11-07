"""Permissions and portal access for the exams module."""
from core.security import register_portal_prefix
from modules.roles.models import register_permissions

register_permissions(
    {
        "exams.view": "See exams, the exam timetable, results and analysis",
        "exams.manage": "Set up exams, papers, seating, grading schemes, result rules and report card templates",
        "exams.marks": "Enter marks for the subjects and sections the person teaches",
        "exams.marks.all": "Enter or correct marks for any section (exam cell)",
        "exams.moderate": "Moderate, add grace marks, send back, lock and unlock marks",
        "exams.publish": "Publish results to parents and students",
        "exams.portal": "Parents and students: see published results and the exam timetable",
    },
    {
        "principal": ["exams.view", "exams.manage", "exams.marks", "exams.marks.all", "exams.moderate", "exams.publish"],
        "manager": ["exams.view", "exams.marks"],
        "teacher": ["exams.view", "exams.marks"],
        "hr": ["exams.view"],
        "parent": ["exams.portal"],
        "student": ["exams.portal"],
    },
)

register_portal_prefix("/exams/portal")
