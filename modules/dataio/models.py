"""Import and export jobs. An import keeps the uploaded file, the column mapping, the
row-level errors and its progress; records it creates or changes are audited by the
module services it calls. The job row itself is not audited (progress updates would
flood the log); its start and finish are written as audit events."""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, LargeBinary, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from core.db import Base


class DataJob(Base):
    __tablename__ = "data_jobs"
    __audit__ = False
    __table_args__ = (Index("ix_data_jobs_company_created", "company_id", "kind", text("created_at DESC")),)

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    kind = Column(String(10), nullable=False, server_default=text("'import'"))     # import | export
    entity = Column(String(40), nullable=False)
    mode = Column(String(10), nullable=False, server_default=text("'add'"))        # add | update | upsert
    match_key = Column(String(40), nullable=True)
    file_name = Column(String(200), nullable=True)
    file_content = Column(LargeBinary, nullable=True)
    headers = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    mapping = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))   # {target column: file header}
    options = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # uploaded | validated | queued | running | done | failed
    status = Column(String(12), nullable=False, server_default=text("'uploaded'"))
    total = Column(Integer, nullable=False, server_default=text("0"))
    processed = Column(Integer, nullable=False, server_default=text("0"))
    added = Column(Integer, nullable=False, server_default=text("0"))
    updated = Column(Integer, nullable=False, server_default=text("0"))
    skipped = Column(Integer, nullable=False, server_default=text("0"))
    failed = Column(Integer, nullable=False, server_default=text("0"))
    errors = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))     # [{row, column, message}]
    preview = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    message = Column(String(300), nullable=True)
    export_log_id = Column(Integer, ForeignKey("export_logs.id"), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
