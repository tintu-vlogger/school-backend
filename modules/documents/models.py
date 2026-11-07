"""Company files, policies, letter templates, generated letters and e-signatures.

Files are kept in `stored_files` (bytes in Postgres, behind row-level security) and are
referenced from folders, policies, letters and other modules through
`modules.documents.service` (store_file / file_info / signed links).
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text,
                        UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import deferred, relationship

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _created():
    return Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated():
    return Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class DocumentSettings(Base):
    __tablename__ = "document_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_document_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_at = _updated()


class FileBlob(Base):
    """Raw bytes of an uploaded or generated file. The record that points to it is audited."""
    __tablename__ = "stored_files"
    __audit__ = False
    __table_args__ = (Index("ix_stored_files_company_created", "company_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    filename = Column(String(255), nullable=False)
    content_type = Column(String(120), nullable=False)
    size = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    data = deferred(Column(LargeBinary, nullable=False))
    purpose = Column(String(40), nullable=True)  # attachment, file, letter, signed, policy, image …
    created_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_at = _created()


class DocFolder(Base):
    """A folder in one of the file areas: company (everyone, by audience), department,
    hr (employee files, HR only) or personal (an employee's private locker)."""
    __tablename__ = "doc_folders"
    __table_args__ = (
        Index("ix_doc_folders_company_area", "company_id", "area"),
        Index("ix_doc_folders_company_owner", "company_id", "owner_employee_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    area = Column(String(20), nullable=False)
    name = Column(String(150), nullable=False)
    description = Column(String(300), nullable=True)
    parent_id = Column(Integer, ForeignKey("doc_folders.id"), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    owner_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    is_system = Column(Boolean, nullable=False, server_default=text("false"))
    sort_order = Column(Integer, nullable=False, server_default=text("0"))
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()


class DocFile(Base):
    __tablename__ = "doc_files"
    __table_args__ = (
        Index("ix_doc_files_company_folder", "company_id", "folder_id"),
        Index("ix_doc_files_company_employee", "company_id", "employee_id"),
        Index("ix_doc_files_company_expires", "company_id", "expires_on"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    folder_id = Column(Integer, ForeignKey("doc_folders.id"), nullable=False)
    title = Column(String(250), nullable=False)
    description = Column(String(500), nullable=True)
    stored_file_id = Column(Integer, ForeignKey("stored_files.id"), nullable=False)
    version = Column(Integer, nullable=False, server_default=text("1"))
    # The employee a file is about (employee files) or belongs to (personal locker)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    # Who may see a company or department file: {"all": true} or {"department_ids": [], "roles": [], "employee_ids": []}
    audience = Column(JSONB, nullable=False, server_default=text("'{\"all\": true}'::jsonb"))
    visible_to_employee = Column(Boolean, nullable=False, server_default=text("true"))  # employee files
    allow_download = Column(Boolean, nullable=False, server_default=text("true"))
    expires_on = Column(Date, nullable=True)
    expiry_reminded_on = Column(Date, nullable=True)
    source = Column(String(20), nullable=False, server_default=text("'upload'"))  # upload | letter | signed
    tags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    uploaded_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    uploaded_by_name = Column(String(200), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()

    folder = relationship("DocFolder")
    stored = relationship("FileBlob")


class DocFileVersion(Base):
    __tablename__ = "doc_file_versions"
    __table_args__ = (Index("ix_doc_file_versions_company_file", "company_id", "doc_file_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    doc_file_id = Column(Integer, ForeignKey("doc_files.id"), nullable=False)
    version = Column(Integer, nullable=False)
    stored_file_id = Column(Integer, ForeignKey("stored_files.id"), nullable=False)
    note = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()

    stored = relationship("FileBlob")


# ── Policies ────────────────────────────────────────────────────────

class Policy(Base):
    __tablename__ = "policies"
    __table_args__ = (Index("ix_policies_company_active", "company_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    title = Column(String(200), nullable=False)
    category_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    description = Column(Text, nullable=True)
    audience = Column(JSONB, nullable=False, server_default=text("'{\"all\": true}'::jsonb"))
    requires_ack = Column(Boolean, nullable=False, server_default=text("true"))
    ack_due_days = Column(Integer, nullable=False, server_default=text("14"))
    reminder_every_days = Column(Integer, nullable=False, server_default=text("3"))
    is_posh = Column(Boolean, nullable=False, server_default=text("false"))
    review_on = Column(Date, nullable=True)  # review/expiry date; HR is reminded before it
    review_reminded_on = Column(Date, nullable=True)
    owner_name = Column(String(200), nullable=True)
    current_version_id = Column(Integer, ForeignKey("policy_versions.id", use_alter=True,
                                                    name="fk_policies_current_version_id"), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()

    current_version = relationship("PolicyVersion", foreign_keys=[current_version_id], post_update=True)


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_policy_versions_policy_version"),
        Index("ix_policy_versions_company_policy", "company_id", "policy_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    policy_id = Column(Integer, ForeignKey("policies.id"), nullable=False)
    version = Column(Integer, nullable=False)
    stored_file_id = Column(Integer, ForeignKey("stored_files.id"), nullable=True)
    body_html = Column(Text, nullable=True)
    change_note = Column(String(300), nullable=True)
    effective_from = Column(Date, nullable=False)
    ack_due_on = Column(Date, nullable=True)
    published_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_by_name = Column(String(200), nullable=True)

    stored = relationship("FileBlob")


class PolicyAcknowledgement(Base):
    """One employee's acknowledgement of one policy version."""
    __tablename__ = "policy_acknowledgements"
    __table_args__ = (
        UniqueConstraint("version_id", "employee_id", name="uq_policy_acks_version_employee"),
        Index("ix_policy_acks_company_employee", "company_id", "employee_id", "acknowledged_at"),
        Index("ix_policy_acks_company_policy", "company_id", "policy_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    policy_id = Column(Integer, ForeignKey("policies.id"), nullable=False)
    version_id = Column(Integer, ForeignKey("policy_versions.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    due_on = Column(Date, nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    ip_address = Column(String(64), nullable=True)
    reminder_count = Column(Integer, nullable=False, server_default=text("0"))
    last_reminded_on = Column(Date, nullable=True)
    created_at = _created()
    updated_at = _updated()


# ── Letters ─────────────────────────────────────────────────────────

class Letterhead(Base):
    """A header or footer design (logo, address, signature images) used on PDFs."""
    __tablename__ = "letterheads"
    __table_args__ = (Index("ix_letterheads_company_kind", "company_id", "kind"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    kind = Column(String(10), nullable=False)  # header | footer
    name = Column(String(120), nullable=False)
    html = Column(Text, nullable=False, server_default=text("''"))
    height_mm = Column(Integer, nullable=False, server_default=text("30"))
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()


class LetterTemplate(Base):
    __tablename__ = "letter_templates"
    __table_args__ = (Index("ix_letter_templates_company_type", "company_id", "letter_type", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    letter_type = Column(String(40), nullable=False)
    description = Column(String(300), nullable=True)
    subject = Column(String(250), nullable=True)  # title of generated letters; may hold merge fields
    body_html = Column(Text, nullable=False, server_default=text("''"))
    header_id = Column(Integer, ForeignKey("letterheads.id"), nullable=True)
    footer_id = Column(Integer, ForeignKey("letterheads.id"), nullable=True)
    page_size = Column(String(10), nullable=False, server_default=text("'A4'"))
    orientation = Column(String(10), nullable=False, server_default=text("'portrait'"))
    margin_top = Column(Integer, nullable=False, server_default=text("20"))
    margin_right = Column(Integer, nullable=False, server_default=text("20"))
    margin_bottom = Column(Integer, nullable=False, server_default=text("20"))
    margin_left = Column(Integer, nullable=False, server_default=text("20"))
    font_family = Column(String(60), nullable=False, server_default=text("'Helvetica'"))
    font_size = Column(Integer, nullable=False, server_default=text("11"))
    # Values HR types at generation time: [{"key": "salary.ctc", "label": "Annual CTC", "type": "money"}]
    extra_fields = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    self_service = Column(Boolean, nullable=False, server_default=text("false"))
    esign_required = Column(Boolean, nullable=False, server_default=text("false"))
    # [{"kind": "employee" | "manager" | "specific", "employee_id": 3, "label": "Authorised signatory"}]
    signers = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    signing_order = Column(Boolean, nullable=False, server_default=text("true"))
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    version = Column(Integer, nullable=False, server_default=text("1"))
    created_at = _created()
    updated_at = _updated()

    header = relationship("Letterhead", foreign_keys=[header_id])
    footer = relationship("Letterhead", foreign_keys=[footer_id])


class Letter(Base):
    """A generated letter: the HTML as it was merged, and its PDF."""
    __tablename__ = "letters"
    __table_args__ = (
        Index("ix_letters_company_employee", "company_id", "employee_id"),
        Index("ix_letters_company_type", "company_id", "letter_type", text("created_at DESC")),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    template_id = Column(Integer, ForeignKey("letter_templates.id"), nullable=True)
    letter_type = Column(String(40), nullable=False)
    reference_no = Column(String(60), nullable=True)
    title = Column(String(250), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    html = Column(Text, nullable=False)
    values = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    header_id = Column(Integer, ForeignKey("letterheads.id"), nullable=True)
    footer_id = Column(Integer, ForeignKey("letterheads.id"), nullable=True)
    stored_file_id = Column(Integer, ForeignKey("stored_files.id"), nullable=True)
    doc_file_id = Column(Integer, ForeignKey("doc_files.id"), nullable=True)
    # issued | out_for_signature | signed | declined | cancelled
    status = Column(String(20), nullable=False, server_default=text("'issued'"))
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    generated_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    template = relationship("LetterTemplate")


class EsignRequest(Base):
    __tablename__ = "esign_requests"
    __table_args__ = (Index("ix_esign_requests_company_status", "company_id", "status"),
                      Index("ix_esign_requests_company_letter", "company_id", "letter_id"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    letter_id = Column(Integer, ForeignKey("letters.id"), nullable=False)
    title = Column(String(250), nullable=False)
    provider = Column(String(30), nullable=False, server_default=text("'in_app_otp'"))
    # out_for_signature | completed | declined | cancelled | expired
    status = Column(String(20), nullable=False, server_default=text("'out_for_signature'"))
    signing_order = Column(Boolean, nullable=False, server_default=text("true"))
    message = Column(String(500), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    signed_file_id = Column(Integer, ForeignKey("stored_files.id"), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    signers = relationship("EsignSigner", order_by="EsignSigner.position", back_populates="request")
    letter = relationship("Letter")


class EsignSigner(Base):
    __tablename__ = "esign_signers"
    __table_args__ = (Index("ix_esign_signers_company_employee", "company_id", "employee_id", "status"),
                      Index("ix_esign_signers_request", "request_id"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    request_id = Column(Integer, ForeignKey("esign_requests.id"), nullable=False)
    position = Column(Integer, nullable=False, server_default=text("1"))
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    name = Column(String(200), nullable=False)
    email = Column(String(255), nullable=False)
    label = Column(String(120), nullable=True)
    # waiting (earlier signer first) | pending | signed | declined | cancelled
    status = Column(String(20), nullable=False, server_default=text("'waiting'"))
    access_token = Column(String(64), nullable=False, unique=True)
    otp_hash = Column(String(128), nullable=True)
    otp_expires_at = Column(DateTime(timezone=True), nullable=True)
    otp_attempts = Column(Integer, nullable=False, server_default=text("0"))
    otp_verified_at = Column(DateTime(timezone=True), nullable=True)
    signature_image = Column(Text, nullable=True)
    signature_method = Column(String(20), nullable=True)  # drawn | typed | aadhaar
    signed_at = Column(DateTime(timezone=True), nullable=True)
    declined_at = Column(DateTime(timezone=True), nullable=True)
    decline_reason = Column(String(500), nullable=True)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(300), nullable=True)
    provider_ref = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    request = relationship("EsignRequest", back_populates="signers")
