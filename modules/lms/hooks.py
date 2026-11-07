"""Permissions, portal paths and background jobs of the teaching and learning module."""
from core import periodic
from core.security import register_portal_prefix
from modules.roles.models import register_permissions

register_permissions(
    {
        "lms.view": "See homework, study materials, online classes, quizzes, syllabus and lesson plans (own sections)",
        "lms.teach": "Set homework, share materials, schedule online classes, build quizzes and plan lessons for the sections and subjects you teach",
        "lms.manage": "Manage teaching and learning for every section: all homework, classes, quizzes, syllabus and coverage",
        "lms.settings.manage": "Change teaching and learning settings (video provider, reminders, defaults)",
    },
    {
        "principal": ["lms.view", "lms.teach", "lms.manage", "lms.settings.manage"],
        "manager": ["lms.view", "lms.teach"],
        "teacher": ["lms.view", "lms.teach"],
        "hr": ["lms.view"],
    },
)

register_portal_prefix("/lms/portal")


def _job(db) -> int:
    from modules.lms import live, quiz
    return live.send_reminders(db) + quiz.close_expired(db)


periodic.register("lms.reminders", _job)
