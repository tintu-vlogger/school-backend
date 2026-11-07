"""Salary bank file permission. HR admin gets it; HR gets it with payroll.run."""
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

ALL_PERMISSIONS.setdefault("payroll.bank_files", "Make and download salary bank transfer files")
for _role in ("admin", "hr"):
    _lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    if "payroll.bank_files" not in _lst:
        _lst.append("payroll.bank_files")
