"""Attendance: rules, settings, punches, daily records, requests, locks and devices.

- attendance_rules: versioned policies (late/early grace, penalties, minimum hours,
  rounding, overtime). Editing creates a new version from a date; old days keep theirs.
- time_settings: other versioned settings by key (roster rest rules, geo-fence,
  regularisation, WFH, permission hours, on duty, overtime limits, timesheets).
- attendance_punches: every clock-in/out from web, devices, files and corrections.
- attendance_days: the computed result for one employee and one date.
- attendance_requests: regularisation, on duty, WFH, permission hours and overtime
  pre-approval; each goes through the workflow engine.
- attendance_locks / attendance_adjustments: a locked pay period never changes; later
  corrections become adjustments paid in the next open period.
- attendance_devices, device_enrolments, device_sync_logs: biometric/RFID/face devices.
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text,
                        Time, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


def _id():
    return Column(Integer, primary_key=True)


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


class AttendanceRule(Base):
    __tablename__ = "attendance_rules"
    __table_args__ = (
        Index("ix_attendance_rules_company_lineage", "company_id", "lineage_id", "effective_from"),
    )

    id = _id()
    uuid = _uuid()
    company_id = _company()
    lineage_id = Column(UUID(as_uuid=True), nullable=False)
    version = Column(Integer, nullable=False, server_default=text("1"))
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    is_current = Column(Boolean, nullable=False, server_default=text("true"))
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    priority = Column(Integer, nullable=False, server_default=text("100"))  # lower wins
    # {"employee_ids": [...], "department_ids": [...], "designation_ids": [...], "shift_ids": [...]}
    applies_to = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    config = Column(JSONB, nullable=False)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    change_note = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TimeSetting(Base):
    __tablename__ = "time_settings"
    __table_args__ = (
        Index("ix_time_settings_company_key", "company_id", "key", "effective_from"),
        UniqueConstraint("company_id", "key", "effective_from", name="uq_time_settings_company_key_from"),
    )

    id = _id()
    uuid = _uuid()
    company_id = _company()
    key = Column(String(40), nullable=False)
    version = Column(Integer, nullable=False, server_default=text("1"))
    effective_from = Column(Date, nullable=False)
    config = Column(JSONB, nullable=False)
    change_note = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Geofence(Base):
    """An office or site where people may clock in from the web or phone."""
    __tablename__ = "attendance_geofences"
    __table_args__ = (Index("ix_attendance_geofences_company", "company_id", "active"),)

    id = _id()
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    address = Column(String(300), nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    radius_m = Column(Integer, nullable=False, server_default=text("200"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Device(Base):
    __tablename__ = "attendance_devices"
    __table_args__ = (
        Index("ix_attendance_devices_company", "company_id", "active"),
        Index("ix_attendance_devices_key_prefix", "api_key_prefix"),
        # an ADMS device is recognised by its serial number alone, so it is unique everywhere
        Index("uq_attendance_devices_adms_serial", "serial_number", unique=True,
              postgresql_where=text("connection = 'adms' AND serial_number IS NOT NULL")),
    )

    id = _id()
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    vendor = Column(String(80), nullable=True)  # eSSL, ZKTeco, Realtime, Matrix, Suprema, other
    model = Column(String(80), nullable=True)
    serial_number = Column(String(80), nullable=True)
    kind = Column(String(20), nullable=False, server_default=text("'biometric'"))  # biometric, rfid, face, other
    location_name = Column(String(150), nullable=True)
    timezone = Column(String(64), nullable=True)  # for device clocks without a zone
    # push (device or middleware posts to us), file (CSV/Excel upload), ftp (we pull files),
    # api, database, zk (we pull), adms, webhook (device posts to us), email (mailbox)
    connection = Column(String(20), nullable=False, server_default=text("'push'"))
    api_key_prefix = Column(String(16), nullable=True)
    api_key_hash = Column(String(128), nullable=True)
    # {"protocol": "sftp", "host": "...", "port": 22, "username": "...", "password": "...", "path": "/exports",
    #  "file_pattern": "*.csv", "every_minutes": 30, "delete_after": false}
    ftp_config = Column(JSONB, nullable=True)
    # {"employee": "EmpCode", "timestamp": "PunchTime", "date": null, "time": null, "direction": "InOut",
    #  "date_format": "%d-%m-%Y %H:%M", "has_header": true, "match_by": "device_user_id"}
    column_mapping = Column(JSONB, nullable=True)
    # settings of the api / database / zk / email / webhook / adms connections (secrets
    # encrypted inside, see device_common) and where the reading runs: our server or the
    # office connector
    source_config = Column(JSONB, nullable=True)
    runs_on = Column(String(20), nullable=False, server_default=text("'server'"))
    # where reading got to (last punch time / id), ADMS stamps and queued commands, last error
    sync_state = Column(JSONB, nullable=True)
    connector_seen_at = Column(DateTime(timezone=True), nullable=True)
    connector_info = Column(JSONB, nullable=True)  # version, host, queue size, last error
    active = Column(Boolean, nullable=False, server_default=text("true"))
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    next_pull_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class DeviceEnrolment(Base):
    """Which employee a device's user id belongs to. device_id NULL = the same id on every device."""
    __tablename__ = "device_enrolments"
    __table_args__ = (
        Index("ix_device_enrolments_company_device", "company_id", "device_id", "device_user_id"),
        Index("uq_device_enrolments_device_user", "company_id", "device_id", "device_user_id", unique=True,
              postgresql_where=text("device_id IS NOT NULL")),
        Index("uq_device_enrolments_any_device_user", "company_id", "device_user_id", unique=True,
              postgresql_where=text("device_id IS NULL")),
    )

    id = _id()
    uuid = _uuid()
    company_id = _company()
    device_id = Column(Integer, ForeignKey("attendance_devices.id"), nullable=True)
    device_user_id = Column(String(60), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    card_number = Column(String(60), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class DeviceSyncLog(Base):
    __tablename__ = "device_sync_logs"
    __audit__ = False  # an operational log, not a business record
    __table_args__ = (Index("ix_device_sync_logs_company_created", "company_id", text("created_at DESC")),)

    id = _id()
    uuid = _uuid()
    company_id = _company()
    device_id = Column(Integer, ForeignKey("attendance_devices.id"), nullable=True)
    kind = Column(String(20), nullable=False)  # push, file, ftp
    status = Column(String(20), nullable=False)  # ok, partial, failed
    file_name = Column(String(255), nullable=True)
    received = Column(Integer, nullable=False, server_default=text("0"))
    inserted = Column(Integer, nullable=False, server_default=text("0"))
    duplicates = Column(Integer, nullable=False, server_default=text("0"))
    unmatched = Column(Integer, nullable=False, server_default=text("0"))
    errors = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    message = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Punch(Base):
    __tablename__ = "attendance_punches"
    __table_args__ = (
        Index("ix_attendance_punches_company_employee_time", "company_id", "employee_id", "punched_at"),
        # the same person punching at the same second is one punch, whatever the source
        UniqueConstraint("employee_id", "punched_at", name="uq_attendance_punches_employee_time"),
    )

    id = _id()
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    punched_at = Column(DateTime(timezone=True), nullable=False)
    direction = Column(String(10), nullable=True)  # in, out, or NULL (worked out from order)
    source = Column(String(20), nullable=False)  # web, mobile, device, file, ftp, regularisation, admin
    device_id = Column(Integer, ForeignKey("attendance_devices.id"), nullable=True)
    device_user_id = Column(String(60), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    accuracy_m = Column(Float, nullable=True)
    geofence_id = Column(Integer, ForeignKey("attendance_geofences.id"), nullable=True)
    distance_m = Column(Integer, nullable=True)
    selfie = Column(Text, nullable=True)  # data URL, kept for the retention period
    ip_address = Column(String(64), nullable=True)
    request_id = Column(Integer, ForeignKey("attendance_requests.id"), nullable=True)
    ignored = Column(Boolean, nullable=False, server_default=text("false"))  # replaced by a correction
    note = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AttendanceDay(Base):
    __tablename__ = "attendance_days"
    __table_args__ = (
        UniqueConstraint("employee_id", "date", name="uq_attendance_days_employee_date"),
        Index("ix_attendance_days_company_date", "company_id", "date"),
        Index("ix_attendance_days_company_employee_date", "company_id", "employee_id", "date"),
    )

    id = _id()
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    date = Column(Date, nullable=False)
    # present, absent, half_day, on_leave, holiday, weekly_off, wfh, on_duty, missing_punch
    status = Column(String(20), nullable=False)
    day_type = Column(String(20), nullable=False, server_default=text("'working'"))  # working, weekly_off, holiday
    shift_id = Column(Integer, ForeignKey("shifts.id"), nullable=True)
    shift_start = Column(DateTime(timezone=True), nullable=True)
    shift_end = Column(DateTime(timezone=True), nullable=True)
    first_in = Column(DateTime(timezone=True), nullable=True)
    last_out = Column(DateTime(timezone=True), nullable=True)
    punch_count = Column(Integer, nullable=False, server_default=text("0"))
    worked_minutes = Column(Integer, nullable=False, server_default=text("0"))
    break_minutes = Column(Integer, nullable=False, server_default=text("0"))
    late_minutes = Column(Integer, nullable=False, server_default=text("0"))
    early_minutes = Column(Integer, nullable=False, server_default=text("0"))
    overtime_minutes = Column(Integer, nullable=False, server_default=text("0"))
    unapproved_overtime_minutes = Column(Integer, nullable=False, server_default=text("0"))
    night_minutes = Column(Integer, nullable=False, server_default=text("0"))
    permission_minutes = Column(Integer, nullable=False, server_default=text("0"))
    is_late = Column(Boolean, nullable=False, server_default=text("false"))
    is_early_exit = Column(Boolean, nullable=False, server_default=text("false"))
    is_night_shift = Column(Boolean, nullable=False, server_default=text("false"))
    shift_allowance = Column(Boolean, nullable=False, server_default=text("false"))
    payable_days = Column(Numeric(4, 2), nullable=False, server_default=text("0"))
    lop_days = Column(Numeric(4, 2), nullable=False, server_default=text("0"))
    penalty_lop_days = Column(Numeric(4, 2), nullable=False, server_default=text("0"))
    # pay lost on reduced-pay leave (e.g. 0.5 for a day of half-pay sick leave); included in lop_days
    pay_cut_days = Column(Numeric(4, 2), nullable=False, server_default=text("0"))
    leave_info = Column(JSONB, nullable=True)  # {"unit": "full" | "first_half" | "second_half", "paid": true, "name": "CL"}
    holiday_name = Column(String(150), nullable=True)
    notes = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # short reasons shown on screen
    rule_id = Column(Integer, ForeignKey("attendance_rules.id"), nullable=True)
    override_status = Column(String(20), nullable=True)  # set by HR by hand
    override_note = Column(String(300), nullable=True)
    locked = Column(Boolean, nullable=False, server_default=text("false"))
    computed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    shift = relationship("Shift")


class AttendanceRequest(Base):
    """kind: regularisation, on_duty, wfh, permission, overtime."""
    __tablename__ = "attendance_requests"
    __table_args__ = (
        Index("ix_attendance_requests_company_employee", "company_id", "employee_id", "kind", "date"),
        Index("ix_attendance_requests_company_status", "company_id", "status"),
    )

    id = _id()
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    kind = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)
    from_time = Column(Time, nullable=True)
    to_time = Column(Time, nullable=True)
    minutes = Column(Integer, nullable=True)
    duration = Column(String(20), nullable=True)  # full_day, first_half, second_half, hours
    category_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    reason = Column(Text, nullable=True)
    # regularisation: {"in": "09:30", "out": "18:30", "in_next_day": false, "out_next_day": false}
    # overtime: {"consent": true, "consent_text": "..."}
    details = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending, approved, rejected, cancelled
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AttendanceLock(Base):
    __tablename__ = "attendance_locks"
    __table_args__ = (Index("ix_attendance_locks_company_period", "company_id", "period_start", "period_end"),)

    id = _id()
    uuid = _uuid()
    company_id = _company()
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'locked'"))  # locked, unlocked
    locked_at = Column(DateTime(timezone=True), nullable=True)
    locked_by_name = Column(String(200), nullable=True)
    unlocked_at = Column(DateTime(timezone=True), nullable=True)
    unlocked_by_name = Column(String(200), nullable=True)
    note = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AttendanceAdjustment(Base):
    """A change to a day in a locked period, carried into the next open period's payroll."""
    __tablename__ = "attendance_adjustments"
    __table_args__ = (Index("ix_attendance_adjustments_company_period", "company_id", "apply_from", "employee_id"),)

    id = _id()
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    date = Column(Date, nullable=False)  # the locked day that changed
    apply_from = Column(Date, nullable=False)  # first day of the period that pays it
    payable_days_delta = Column(Numeric(4, 2), nullable=False, server_default=text("0"))
    lop_days_delta = Column(Numeric(4, 2), nullable=False, server_default=text("0"))
    overtime_minutes_delta = Column(Integer, nullable=False, server_default=text("0"))
    old_status = Column(String(20), nullable=True)
    new_status = Column(String(20), nullable=True)
    reason = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    employee = relationship("Employee")


class LeaveLink(Base):
    """What attendance did to leave on one day: an absence turned into leave (or LOP), a
    late-mark penalty taken from a leave balance, or comp off credited for work on a day
    off. Kept so each can be undone when the attendance is corrected."""
    __tablename__ = "attendance_leave_links"
    __table_args__ = (
        Index("ix_attendance_leave_links_company_employee_date", "company_id", "employee_id", "date"),
        Index("ix_attendance_leave_links_company_status", "company_id", "kind", "status"),
    )

    id = _id()
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    date = Column(Date, nullable=False)
    kind = Column(String(20), nullable=False)  # absence | late_penalty | comp_off
    # pending (waiting for approval) | applied (leave taken / comp off credited) | lop (kept as loss of pay)
    # | rejected | reversed
    status = Column(String(20), nullable=False)
    days = Column(Numeric(4, 2), nullable=False, server_default=text("0"))
    unit = Column(String(12), nullable=False, server_default=text("'day'"))  # day | half_day
    leave_type_id = Column(Integer, ForeignKey("leave_types.id"), nullable=True)
    leave_request_id = Column(Integer, ForeignKey("leave_requests.id"), nullable=True)
    comp_off_id = Column(Integer, ForeignKey("comp_off_requests.id"), nullable=True)
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    note = Column(String(300), nullable=True)
    reversed_at = Column(DateTime(timezone=True), nullable=True)
    reverse_reason = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class LopEntry(Base):
    """Loss-of-pay changes for a pay period: manual adjustments before payroll, days carried
    in from an earlier period over the cap, and the final figure written when the period is
    locked. Voided (never deleted) when a lock is undone."""
    __tablename__ = "lop_period_entries"
    __table_args__ = (Index("ix_lop_period_entries_company_period", "company_id", "period_start", "employee_id"),)

    id = _id()
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    kind = Column(String(20), nullable=False)  # manual | carry_in | final
    days = Column(Numeric(6, 2), nullable=False)
    note = Column(String(300), nullable=True)
    meta = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    void = Column(Boolean, nullable=False, server_default=text("false"))
    created_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Kiosk(Base):
    """A shared tablet where people clock in with their employee code and PIN. Opened at a
    secret link; the token is stored hashed and shown once."""
    __tablename__ = "attendance_kiosks"
    __table_args__ = (Index("ix_attendance_kiosks_company", "company_id", "active"),
                      Index("ix_attendance_kiosks_prefix", "token_prefix"))

    id = _id()
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    location_note = Column(String(200), nullable=True)
    token_prefix = Column(String(16), nullable=True)
    token_hash = Column(String(64), nullable=True)
    photo = Column(String(12), nullable=False, server_default=text("'off'"))  # off | optional | required
    allowed_ips = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    punches_count = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class KioskPin(Base):
    """An employee's kiosk PIN (hashed). Wrong tries lock it for a while. Changes are logged
    with audit events (without the hash), so row auditing is off."""
    __tablename__ = "kiosk_pins"
    __audit__ = False
    __table_args__ = (UniqueConstraint("employee_id", name="uq_kiosk_pins_employee"),
                      Index("ix_kiosk_pins_company_employee", "company_id", "employee_id"))

    id = _id()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    pin_hash = Column(String(200), nullable=False)
    failed_count = Column(Integer, nullable=False, server_default=text("0"))
    locked_until = Column(DateTime(timezone=True), nullable=True)
    set_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
