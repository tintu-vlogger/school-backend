"""Periodic job of face attendance: delete old snapshots and the face data of people who left."""
try:
    from core import periodic
    from modules.face_attendance import service

    periodic.register("face_attendance.retention", service.purge)
except ImportError:
    pass
