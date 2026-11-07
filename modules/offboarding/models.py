"""Exits: one record per leaving with its flow, notice, handover, departmental clearances,
exit interview, settlement handoff, letters, disabling and alumni access."""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _json(default="'{}'::jsonb"):
    return Column(JSONB, nullable=False, server_default=text(default))


def _money():
    return Column(Numeric(14, 2), nullable=True)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


class OffboardingSettings(Base):
    __tablename__ = "offboarding_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_offboarding_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = _json()
    created_at, updated_at = _stamps()


class ExitFlow(Base):
    """What happens for one kind of exit for an audience: clearances with owners and due
    dates, letters, the exit interview and who may raise it."""
    __tablename__ = "exit_flows"
    __table_args__ = (Index("ix_exit_flows_company_type", "company_id", "exit_type", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    exit_type = Column(String(20), nullable=False)
    audience = _json()
    priority = Column(Integer, nullable=False, server_default=text("100"))
    # [{"key": "it", "label": "IT clearance", "department": "it|admin|finance|manager|assets|hr|other",
    #   "owner_type": "role|employee|reporting_manager", "owner_value": "hr" | 12, "due_days": -1, "items": ["..."]}]
    clearances = _json("'[]'::jsonb")
    letters = _json("'[]'::jsonb")          # ["relieving", "experience"] or ["termination"]
    exit_interview = Column(Boolean, nullable=False, server_default=text("true"))
    raised_by = _json("'[\"hr\"]'::jsonb")  # who may raise: employee | manager | hr
    needs_approval = Column(Boolean, nullable=False, server_default=text("true"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class Exit(Base):
    __tablename__ = "exits"
    __table_args__ = (
        Index("ix_exits_company_status", "company_id", "status"),
        Index("ix_exits_company_employee", "company_id", "employee_id"),
        Index("ix_exits_company_lwd", "company_id", "last_working_day"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(40), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    flow_id = Column(Integer, ForeignKey("exit_flows.id"), nullable=True)
    # resignation | termination | retrenchment | layoff | contract_end | retirement | death | absconding
    exit_type = Column(String(20), nullable=False)
    reason_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    reason_text = Column(Text, nullable=True)
    raised_by = Column(String(20), nullable=False, server_default=text("'hr'"))  # employee | manager | hr | system
    raised_by_name = Column(String(200), nullable=True)
    # requested | in_progress | completed | withdrawn | rejected | cancelled
    status = Column(String(20), nullable=False, server_default=text("'requested'"))
    resignation_date = Column(Date, nullable=False)      # date the notice was given (or the event date)
    proposed_last_day = Column(Date, nullable=True)
    last_working_day = Column(Date, nullable=False)
    notice_required_days = Column(Integer, nullable=False, server_default=text("0"))
    notice_served_days = Column(Integer, nullable=False, server_default=text("0"))
    notice_shortfall_days = Column(Integer, nullable=False, server_default=text("0"))
    notice_action = Column(String(20), nullable=False, server_default=text("'serve'"))  # serve | buyout | waive | recover
    notice_amount = _money()          # buy-out paid by the employee or recovery deducted
    notice_pay_in_lieu = _money()     # paid by the employer when the company cuts notice short
    compensation = _json()            # retrenchment / lay-off inputs and amounts
    replacement_needed = Column(Boolean, nullable=False, server_default=text("false"))
    rehire_eligible = Column(Boolean, nullable=True)
    rehire_note = Column(String(300), nullable=True)
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    withdrawn_at = Column(DateTime(timezone=True), nullable=True)
    withdraw_reason = Column(String(300), nullable=True)
    # Handover
    handover_to_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    approvals_delegated_to_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    approvals_delegated_at = Column(DateTime(timezone=True), nullable=True)
    handover_notes = Column(Text, nullable=True)
    # Settlement (handed to payroll)
    settlement_due_date = Column(Date, nullable=True)
    settlement_status = Column(String(20), nullable=False, server_default=text("'not_started'"))  # not_started | handed_over | approved | paid
    settlement_ref = Column(String(80), nullable=True)
    settlement_handed_at = Column(DateTime(timezone=True), nullable=True)
    settlement_paid_on = Column(Date, nullable=True)
    settlement_summary = _json()
    # Letters and closure
    letters = _json("'[]'::jsonb")    # [{"type", "letter_uuid", "title", "at"}]
    login_disabled_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    pf_exit_updated = Column(Boolean, nullable=False, server_default=text("false"))
    alumni_until = Column(Date, nullable=True)
    alumni_token = Column(String(64), nullable=True, unique=True)
    alumni_last_seen_at = Column(DateTime(timezone=True), nullable=True)
    history = _json("'[]'::jsonb")
    created_at, updated_at = _stamps()

    flow = relationship("ExitFlow")
    clearances = relationship("ExitClearance", back_populates="exit", order_by="ExitClearance.id")
    handovers = relationship("ExitHandover", back_populates="exit", order_by="ExitHandover.id")


class ExitClearance(Base):
    __tablename__ = "exit_clearances"
    __table_args__ = (Index("ix_exit_clearances_company_status", "company_id", "status", "due_date"),
                      Index("ix_exit_clearances_company_exit", "company_id", "exit_id"),
                      Index("ix_exit_clearances_company_owner", "company_id", "owner_employee_id"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    exit_id = Column(Integer, ForeignKey("exits.id"), nullable=False)
    key = Column(String(40), nullable=False)
    label = Column(String(150), nullable=False)
    department = Column(String(20), nullable=False, server_default=text("'other'"))
    owner_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    owner_role = Column(String(20), nullable=True)
    due_days = Column(Integer, nullable=False, server_default=text("0"))  # relative to the last working day
    due_date = Column(Date, nullable=False)
    # pending | cleared | not_applicable
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    items = _json("'[]'::jsonb")       # [{"label", "done"}]
    remarks = Column(Text, nullable=True)
    recovery_amount = _money()
    cleared_at = Column(DateTime(timezone=True), nullable=True)
    cleared_by = Column(String(200), nullable=True)
    reminded_on = Column(Date, nullable=True)
    created_at, updated_at = _stamps()

    exit = relationship("Exit", back_populates="clearances")


class ExitHandover(Base):
    __tablename__ = "exit_handovers"
    __table_args__ = (Index("ix_exit_handovers_company_exit", "company_id", "exit_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    exit_id = Column(Integer, ForeignKey("exits.id"), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'task'"))  # task | document | approvals | knowledge | other
    title = Column(String(250), nullable=False)
    details = Column(Text, nullable=True)
    to_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    ref = Column(String(80), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending | done
    done_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    exit = relationship("Exit", back_populates="handovers")


class ExitInterview(Base):
    __tablename__ = "exit_interviews"
    __table_args__ = (Index("ix_exit_interviews_company_exit", "company_id", "exit_id"),
                      UniqueConstraint("exit_id", name="uq_exit_interviews_exit"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    exit_id = Column(Integer, ForeignKey("exits.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    interviewer_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    scheduled_on = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending | submitted | skipped
    primary_reason_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    ratings = _json()                 # {"manager": 4, "growth": 2, "pay": 3, "culture": 4, "worklife": 3}
    would_rejoin = Column(String(10), nullable=True)   # yes | no | maybe
    would_recommend = Column(Integer, nullable=True)   # 0-10
    answers = _json()                 # {question key: text}
    checklist = _json()               # {"equipment": true, ...}
    interviewer_notes = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()
