"""Face attendance: face check at the clock-in kiosk and (optionally) a selfie match for
web and phone clock-in.

- face_settings: one row per company with the face settings (on/off, match threshold,
  liveness level, snapshots, locations, consent wording).
- face_kiosk_settings: per kiosk: its location, face off / on / required, optional own
  threshold and liveness level.
- face_enrolments: one row per employee consent. Only face templates (numbers made from
  3 to 5 face samples, never the photos) are kept, encrypted with a key per company. On
  withdrawal the templates are deleted and the row stays as the consent record.
- face_events: every face check worth keeping: punches by face, PIN used instead of the
  face, faces not recognised, suspected photo or screen spoofing, selfie checks. Flagged
  ones wait for HR review. A small snapshot can be kept for a set number of days.

Rows are not audited field by field (templates and snapshots must never reach the audit
log); enrolment, withdrawal, review and settings changes are written with audit events.
"""
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


class FaceSettings(Base):
    __tablename__ = "face_settings"
    __audit__ = False  # changes are written with the face.settings_changed audit event
    __table_args__ = (UniqueConstraint("company_id", name="uq_face_settings_company"),
                      Index("ix_face_settings_company", "company_id"))

    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class FaceKioskSetting(Base):
    __tablename__ = "face_kiosk_settings"
    __table_args__ = (UniqueConstraint("kiosk_id", name="uq_face_kiosk_settings_kiosk"),
                      Index("ix_face_kiosk_settings_company", "company_id", "kiosk_id"))

    id = Column(Integer, primary_key=True)
    company_id = _company()
    kiosk_id = Column(Integer, ForeignKey("attendance_kiosks.id"), nullable=False)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    mode = Column(String(12), nullable=False, server_default=text("'off'"))  # off | on | required
    threshold = Column(Float, nullable=True)  # own match threshold (cosine distance), else the company's
    liveness = Column(String(12), nullable=True)  # own liveness level, else the company's
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class FaceEnrolment(Base):
    __tablename__ = "face_enrolments"
    __audit__ = False
    __table_args__ = (Index("ix_face_enrolments_company_employee", "company_id", "employee_id", "status"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    status = Column(String(12), nullable=False, server_default=text("'active'"))  # active | withdrawn | removed
    model = Column(String(40), nullable=False)  # which face model made the templates
    dims = Column(Integer, nullable=False)
    samples = Column(Integer, nullable=False)
    templates = Column(Text, nullable=True)  # encrypted JSON list of vectors; NULL once withdrawn
    quality = Column(Float, nullable=True)  # how alike the samples were (0 to 1)
    consent_version = Column(Integer, nullable=False)
    consent_text = Column(Text, nullable=False)
    consent_by = Column(String(10), nullable=False)  # self | hr
    consent_at = Column(DateTime(timezone=True), nullable=False)
    captured_by_name = Column(String(200), nullable=True)
    withdrawn_at = Column(DateTime(timezone=True), nullable=True)
    withdrawn_by_name = Column(String(200), nullable=True)
    withdraw_reason = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class FaceEvent(Base):
    __tablename__ = "face_events"
    __audit__ = False
    __table_args__ = (
        Index("ix_face_events_company_time", "company_id", "occurred_at"),
        Index("ix_face_events_company_review", "company_id", "flagged", "review_status"),
        Index("ix_face_events_company_employee", "company_id", "employee_id", "occurred_at"),
        Index("ix_face_events_client", "company_id", "client_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    kind = Column(String(20), nullable=False)  # face_punch | pin_fallback | no_match | spoof | selfie_match | selfie_fail
    source = Column(String(10), nullable=False)  # kiosk | web
    kiosk_id = Column(Integer, ForeignKey("attendance_kiosks.id"), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    punch_id = Column(Integer, ForeignKey("attendance_punches.id"), nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    score = Column(Float, nullable=True)  # similarity checked by the server (1 = identical)
    client_score = Column(Float, nullable=True)  # similarity the kiosk worked out
    threshold = Column(Float, nullable=True)  # the cosine distance limit in force
    attempts = Column(Integer, nullable=True)
    liveness = Column(JSONB, nullable=True)  # {"passed", "real", "live", "challenges"}
    snapshot = Column(Text, nullable=True)  # small JPEG data URL, deleted after the retention period
    offline = Column(Boolean, nullable=False, server_default=text("false"))
    client_id = Column(String(60), nullable=True)
    flagged = Column(Boolean, nullable=False, server_default=text("false"))
    flag_reasons = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    review_status = Column(String(12), nullable=True)  # open | accepted | rejected (flagged only)
    reviewed_by_name = Column(String(200), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_note = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
