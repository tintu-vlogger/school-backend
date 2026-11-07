"""Tasks: assigned work with a due date, priority, checklist, comments and attachments.
Other modules create tasks through `modules.tasks.service.create_task(...)`."""
from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_company_assignee_status", "company_id", "assignee_employee_id", "status"),
        Index("ix_tasks_company_creator", "company_id", "created_by_employee_id"),
        Index("ix_tasks_company_due", "company_id", "due_date"),
        Index("ix_tasks_company_source", "company_id", "source_module", "source_ref"),
        Index("ix_tasks_company_series", "company_id", "series_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    assignee_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)  # NULL = owner or system
    created_by_name = Column(String(200), nullable=True)
    priority = Column(String(10), nullable=False, server_default=text("'medium'"))  # low | medium | high | urgent
    status = Column(String(20), nullable=False, server_default=text("'todo'"))  # todo | in_progress | done | cancelled
    start_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    position = Column(Float, nullable=False, server_default=text("0"))
    labels = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    attachments = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # Created by another module: e.g. ("onboarding", "<checklist uuid>")
    source_module = Column(String(40), nullable=True)
    source_ref = Column(String(80), nullable=True)
    # Repeat: {"freq": "daily" | "weekly" | "monthly", "interval": 1, "until": "2027-03-31"}
    recurrence = Column(JSONB, nullable=True)
    series_id = Column(UUID(as_uuid=True), nullable=True)
    remind_days_before = Column(Integer, nullable=False, server_default=text("1"))
    reminded_on = Column(Date, nullable=True)
    overdue_notified = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    checklist = relationship("TaskChecklistItem", back_populates="task", order_by="TaskChecklistItem.position",
                             cascade="all, delete-orphan")
    comments = relationship("TaskComment", back_populates="task", order_by="TaskComment.id")


class TaskChecklistItem(Base):
    __tablename__ = "task_checklist_items"
    __table_args__ = (Index("ix_task_checklist_company_task", "company_id", "task_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    title = Column(String(300), nullable=False)
    done = Column(Boolean, nullable=False, server_default=text("false"))
    done_at = Column(DateTime(timezone=True), nullable=True)
    done_by_name = Column(String(200), nullable=True)
    position = Column(Integer, nullable=False, server_default=text("0"))

    task = relationship("Task", back_populates="checklist")


class TaskComment(Base):
    __tablename__ = "task_comments"
    __table_args__ = (Index("ix_task_comments_company_task", "company_id", "task_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    body = Column(Text, nullable=False)
    author_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    author_name = Column(String(200), nullable=True)
    attachments = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    task = relationship("Task", back_populates="comments")
