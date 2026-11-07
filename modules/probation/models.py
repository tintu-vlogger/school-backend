"""Probation: policies chosen by rule, one probation per person, multi-level reviews."""
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _json(default="'{}'::jsonb"):
    return Column(JSONB, nullable=False, server_default=text(default))


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


class ProbationPolicy(Base):
    """How long probation lasts for an audience, when reviews start, who reviews and how
    it may be extended. Editing keeps running probations on the terms they started with
    (their dates are already set)."""
    __tablename__ = "probation_policies"
    __table_args__ = (Index("ix_probation_policies_company", "company_id", "active", "priority"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    description = Column(String(300), nullable=True)
    audience = _json()
    priority = Column(Integer, nullable=False, server_default=text("100"))
    duration_value = Column(Integer, nullable=False, server_default=text("6"))
    duration_unit = Column(String(10), nullable=False, server_default=text("'months'"))  # days | months
    review_before_days = Column(Integer, nullable=False, server_default=text("15"))
    allow_extension = Column(Boolean, nullable=False, server_default=text("true"))
    extension_value = Column(Integer, nullable=False, server_default=text("3"))
    extension_unit = Column(String(10), nullable=False, server_default=text("'months'"))
    max_extensions = Column(Integer, nullable=False, server_default=text("1"))
    max_total_months = Column(Integer, nullable=True)  # cap from standing orders / state rules
    # [{"level": 1, "type": "reporting_manager|skip_level_manager|department_head|role|employee", "value": "hr" | 12}]
    reviewers = _json("'[]'::jsonb")
    feedback_from = _json("'[]'::jsonb")    # same shape; optional input before the decision
    auto_confirm = Column(Boolean, nullable=False, server_default=text("false"))  # confirm on end date when all reviews say confirm
    confirmation_letter = Column(Boolean, nullable=False, server_default=text("true"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class Probation(Base):
    __tablename__ = "probations"
    __table_args__ = (
        Index("ix_probations_company_status", "company_id", "status", "end_date"),
        Index("ix_probations_company_employee", "company_id", "employee_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    policy_id = Column(Integer, ForeignKey("probation_policies.id"), nullable=True)
    start_date = Column(Date, nullable=False)
    original_end_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    # active | in_review | on_hold | extended | confirmed | terminated | cancelled
    status = Column(String(20), nullable=False, server_default=text("'active'"))
    extensions = Column(Integer, nullable=False, server_default=text("0"))
    review_started_at = Column(DateTime(timezone=True), nullable=True)
    current_level = Column(Integer, nullable=False, server_default=text("1"))
    outcome = Column(String(20), nullable=True)   # confirm | extend | terminate | hold
    outcome_reason = Column(Text, nullable=True)
    hold_reason = Column(String(500), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decided_by = Column(String(200), nullable=True)
    confirmation_date = Column(Date, nullable=True)
    letter_id = Column(Integer, nullable=True)
    exit_id = Column(Integer, nullable=True)       # exits.id when the outcome is termination
    last_reminded_on = Column(Date, nullable=True)
    history = _json("'[]'::jsonb")
    created_at, updated_at = _stamps()

    policy = relationship("ProbationPolicy")
    reviews = relationship("ProbationReview", back_populates="probation", order_by="ProbationReview.level")


class ProbationReview(Base):
    __tablename__ = "probation_reviews"
    __table_args__ = (Index("ix_probation_reviews_company_reviewer", "company_id", "reviewer_employee_id", "status"),
                      Index("ix_probation_reviews_company_probation", "company_id", "probation_id"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    probation_id = Column(Integer, ForeignKey("probations.id"), nullable=False)
    round = Column(Integer, nullable=False, server_default=text("1"))  # 1 = first review, 2 = after first extension …
    level = Column(Integer, nullable=False, server_default=text("1"))
    kind = Column(String(20), nullable=False, server_default=text("'review'"))  # review | feedback
    reviewer_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    reviewer_name = Column(String(200), nullable=True)
    # waiting (earlier level first) | pending | submitted | skipped
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    rating = Column(Integer, nullable=True)  # 1-5
    recommendation = Column(String(20), nullable=True)  # confirm | extend | terminate | hold
    strengths = Column(Text, nullable=True)
    improvements = Column(Text, nullable=True)
    comments = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    reminded_on = Column(Date, nullable=True)
    created_at, updated_at = _stamps()

    probation = relationship("Probation", back_populates="reviews")
