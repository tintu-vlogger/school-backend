"""Permissions owned by the students module."""
from modules.roles.models import register_permissions

register_permissions(
    {
        "students.view": "See students, guardians and class lists",
        "students.manage": "Add and edit students and guardians, enrol and change status",
        "students.sensitive.view": "See full national ID numbers of students",
        "students.promote": "Promote, detain and graduate students at year end",
        "students.portal.manage": "Create parent and student sign-ins and reset their passwords",
        "portal.parent": "Parent portal (children's information)",
        "portal.student": "Student portal (own information)",
    },
    {
        "principal": ["students.view", "students.manage", "students.promote", "students.portal.manage",
                      "students.sensitive.view"],
        "hr": ["students.view", "students.manage", "students.portal.manage"],
        "teacher": ["students.view"],
        "accountant": ["students.view"],
        "librarian": ["students.view"],
        "parent": ["portal.parent"],
        "student": ["portal.student"],
    },
)
