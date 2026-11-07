"""Engagement: Buzz feed, celebrations, kudos, announcements, surveys, kiosks; and the
in-app notification centre columns on notifications

Revision ID: en1g2a3g4e5m
Revises: w8x9y0z1a2b3
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "en1g2a3g4e5m"
down_revision = "w8x9y0z1a2b3"
branch_labels = None
depends_on = None

TABLES = ("engagement_settings", "engagement_profiles", "engagement_files", "kudos_values", "kudos", "kudos_recipients",
          "announcements", "announcement_receipts", "surveys", "survey_questions", "survey_runs", "survey_participations",
          "survey_responses", "survey_answers", "buzz_posts", "buzz_poll_options", "buzz_poll_votes", "buzz_comments",
          "buzz_reactions", "buzz_reports", "celebration_events", "kiosks")


def upgrade():
    op.create_table(
        "engagement_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_engagement_settings_company_id"), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint("company_id", name="uq_engagement_settings_company"),
    )
    op.create_table(
        "engagement_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_engagement_profiles_company_id"), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_engagement_profiles_employee_id"), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("joining_date", sa.Date(), nullable=True),
        sa.Column("wedding_date", sa.Date(), nullable=True),
        sa.Column("hide_birthday", sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column("hide_work_anniversary", sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column("hide_wedding_anniversary", sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column("celebration_emails", sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint("company_id", "employee_id", name="uq_engagement_profiles_employee"),
    )
    op.create_table(
        "engagement_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_engagement_files_company_id"), nullable=False),
        sa.Column("uploaded_by_employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_engagement_files_uploaded_by"), nullable=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("mime", sa.String(120), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("thumb", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index("ix_engagement_files_company_created", "engagement_files", ["company_id", "created_at"])
    op.create_table(
        "kudos_values",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_kudos_values_company_id"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.String(300), nullable=True),
        sa.Column("emoji", sa.String(16), nullable=False, server_default=sa.text("'👏'")),
        sa.Column("color", sa.String(20), nullable=False, server_default=sa.text("'amber'")),
        sa.Column("points", sa.Integer(), nullable=False, server_default=sa.text('10')),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index("ix_kudos_values_company_position", "kudos_values", ["company_id", "position"])
    op.create_table(
        "kudos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_kudos_company_id"), nullable=False),
        sa.Column("giver_employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_kudos_giver_employee_id"), nullable=True),
        sa.Column("value_id", sa.Integer(), sa.ForeignKey("kudos_values.id", name="fk_kudos_value_id"), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index("ix_kudos_company_created", "kudos", ["company_id", sa.text("created_at DESC")])
    op.create_table(
        "kudos_recipients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_kudos_recipients_company_id"), nullable=False),
        sa.Column("kudos_id", sa.Integer(), sa.ForeignKey("kudos.id", name="fk_kudos_recipients_kudos_id", ondelete="CASCADE"), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_kudos_recipients_employee_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint("kudos_id", "employee_id", name="uq_kudos_recipients_once"),
    )
    op.create_index("ix_kudos_recipients_company_employee", "kudos_recipients", ["company_id", "employee_id"])
    op.create_table(
        "announcements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_announcements_company_id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default=sa.text("'announcement'")),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.String(400), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("cover", postgresql.JSONB(), nullable=True),
        sa.Column("audience", postgresql.JSONB(), nullable=False, server_default=sa.text('\'{"all": true}\'::jsonb')),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requires_ack", sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column("ack_due_on", sa.Date(), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column("show_on_kiosk", sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column("allow_comments", sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column("send_email", sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column("author_employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_announcements_author_employee_id"), nullable=True),
        sa.Column("author_name", sa.String(200), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index("ix_announcements_company_status", "announcements", ["company_id", "status"])
    op.create_index("ix_announcements_company_publish", "announcements", ["company_id", "publish_at"])
    op.create_table(
        "announcement_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_announcement_receipts_company_id"), nullable=False),
        sa.Column("announcement_id", sa.Integer(), sa.ForeignKey("announcements.id", name="fk_announcement_receipts_announcement_id"), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_announcement_receipts_employee_id"), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint("announcement_id", "employee_id", name="uq_announcement_receipts_once"),
    )
    op.create_index("ix_announcement_receipts_company_ann", "announcement_receipts", ["company_id", "announcement_id"])
    op.create_table(
        "surveys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_surveys_company_id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default=sa.text("'survey'")),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("thank_you", sa.String(500), nullable=True),
        sa.Column("cover", postgresql.JSONB(), nullable=True),
        sa.Column("identity", sa.String(20), nullable=False, server_default=sa.text("'anonymous'")),
        sa.Column("min_responses", sa.Integer(), nullable=False, server_default=sa.text('5')),
        sa.Column("audience", postgresql.JSONB(), nullable=False, server_default=sa.text('\'{"all": true}\'::jsonb')),
        sa.Column("mandatory", sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column("reminder_every_days", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_days", sa.Integer(), nullable=False, server_default=sa.text('7')),
        sa.Column("recurrence", sa.String(20), nullable=False, server_default=sa.text("'none'")),
        sa.Column("recurrence_count", sa.Integer(), nullable=True),
        sa.Column("post_to_buzz", sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column("created_by_employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_surveys_created_by_employee_id"), nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index("ix_surveys_company_status", "surveys", ["company_id", "status"])
    op.create_table(
        "survey_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_survey_questions_company_id"), nullable=False),
        sa.Column("survey_id", sa.Integer(), sa.ForeignKey("surveys.id", name="fk_survey_questions_survey_id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column("qtype", sa.String(20), nullable=False),
        sa.Column("help_text", sa.String(300), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column("options", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("settings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("text", sa.String(500), nullable=False),
    )
    op.create_index("ix_survey_questions_company_survey", "survey_questions", ["company_id", "survey_id", "position"])
    op.create_table(
        "survey_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_survey_runs_company_id"), nullable=False),
        sa.Column("survey_id", sa.Integer(), sa.ForeignKey("surveys.id", name="fk_survey_runs_survey_id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'scheduled'")),
        sa.Column("invited_count", sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint("survey_id", "number", name="uq_survey_runs_number"),
    )
    op.create_index("ix_survey_runs_company_status", "survey_runs", ["company_id", "status"])
    op.create_table(
        "survey_participations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_survey_participations_company_id"), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("survey_runs.id", name="fk_survey_participations_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_survey_participations_employee_id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("submitted_on", sa.Date(), nullable=True),
        sa.Column("reminders_sent", sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint("run_id", "employee_id", name="uq_survey_participations_once"),
    )
    op.create_index("ix_survey_participations_company_employee", "survey_participations", ["company_id", "employee_id", "status"])
    op.create_table(
        "survey_responses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_survey_responses_company_id"), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("survey_runs.id", name="fk_survey_responses_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_survey_responses_employee_id"), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("submitted_on", sa.Date(), nullable=False),
    )
    op.create_index("ix_survey_responses_company_run", "survey_responses", ["company_id", "run_id"])
    op.create_table(
        "survey_answers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_survey_answers_company_id"), nullable=False),
        sa.Column("response_id", sa.Integer(), sa.ForeignKey("survey_responses.id", name="fk_survey_answers_response_id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.Integer(), sa.ForeignKey("survey_questions.id", name="fk_survey_answers_question_id", ondelete="CASCADE"), nullable=False),
        sa.Column("value_number", sa.Numeric(6, 2), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_choices", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_survey_answers_company_question", "survey_answers", ["company_id", "question_id"])
    op.create_table(
        "buzz_posts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_buzz_posts_company_id"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False, server_default=sa.text("'post'")),
        sa.Column("author_employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_buzz_posts_author_employee_id"), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("mentions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("hashtags", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("attachments", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("link_preview", postgresql.JSONB(), nullable=True),
        sa.Column("poll", postgresql.JSONB(), nullable=True),
        sa.Column("audience", postgresql.JSONB(), nullable=False, server_default=sa.text('\'{"all": true}\'::jsonb')),
        sa.Column("theme", sa.String(40), nullable=True),
        sa.Column("subject_employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_buzz_posts_subject_employee_id"), nullable=True),
        sa.Column("celebration_date", sa.Date(), nullable=True),
        sa.Column("years", sa.Integer(), nullable=True),
        sa.Column("kudos_id", sa.Integer(), sa.ForeignKey("kudos.id", name="fk_buzz_posts_kudos_id"), nullable=True),
        sa.Column("announcement_id", sa.Integer(), sa.ForeignKey("announcements.id", name="fk_buzz_posts_announcement_id"), nullable=True),
        sa.Column("survey_id", sa.Integer(), sa.ForeignKey("surveys.id", name="fk_buzz_posts_survey_id"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'published'")),
        sa.Column("allow_comments", sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pinned_by_name", sa.String(200), nullable=True),
        sa.Column("hidden_reason", sa.String(500), nullable=True),
        sa.Column("hidden_by_name", sa.String(200), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workflow_instance_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index("uq_buzz_posts_celebration", "buzz_posts", ["company_id", "kind", "subject_employee_id", "celebration_date"], unique=True, postgresql_where=sa.text("celebration_date IS NOT NULL"))
    op.create_index("ix_buzz_posts_company_subject", "buzz_posts", ["company_id", "subject_employee_id"])
    op.create_index("ix_buzz_posts_company_kind", "buzz_posts", ["company_id", "kind"])
    op.create_index("ix_buzz_posts_company_pinned", "buzz_posts", ["company_id", "pinned_at"])
    op.create_index("ix_buzz_posts_company_created", "buzz_posts", ["company_id", sa.text("created_at DESC")])
    op.create_table(
        "buzz_poll_options",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_buzz_poll_options_company_id"), nullable=False),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("buzz_posts.id", name="fk_buzz_poll_options_post_id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text('0')),
    )
    op.create_index("ix_buzz_poll_options_company_post", "buzz_poll_options", ["company_id", "post_id"])
    op.create_table(
        "buzz_poll_votes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_buzz_poll_votes_company_id"), nullable=False),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("buzz_posts.id", name="fk_buzz_poll_votes_post_id", ondelete="CASCADE"), nullable=False),
        sa.Column("option_id", sa.Integer(), sa.ForeignKey("buzz_poll_options.id", name="fk_buzz_poll_votes_option_id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_key", sa.String(20), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_buzz_poll_votes_employee_id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint("option_id", "actor_key", name="uq_buzz_poll_votes_option_actor"),
    )
    op.create_index("ix_buzz_poll_votes_company_post", "buzz_poll_votes", ["company_id", "post_id"])
    op.create_table(
        "buzz_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_buzz_comments_company_id"), nullable=False),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("buzz_posts.id", name="fk_buzz_comments_post_id"), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("buzz_comments.id", name="fk_buzz_comments_parent_id"), nullable=True),
        sa.Column("author_employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_buzz_comments_author_employee_id"), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("mentions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'published'")),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index("ix_buzz_comments_company_post", "buzz_comments", ["company_id", "post_id", "created_at"])
    op.create_table(
        "buzz_reactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_buzz_reactions_company_id"), nullable=False),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("buzz_posts.id", name="fk_buzz_reactions_post_id"), nullable=False),
        sa.Column("comment_id", sa.Integer(), sa.ForeignKey("buzz_comments.id", name="fk_buzz_reactions_comment_id"), nullable=True),
        sa.Column("actor_key", sa.String(20), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_buzz_reactions_employee_id"), nullable=True),
        sa.Column("emoji", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index("uq_buzz_reactions_post_actor", "buzz_reactions", ["post_id", "actor_key"], unique=True, postgresql_where=sa.text("comment_id IS NULL"))
    op.create_index("uq_buzz_reactions_comment_actor", "buzz_reactions", ["comment_id", "actor_key"], unique=True, postgresql_where=sa.text("comment_id IS NOT NULL"))
    op.create_index("ix_buzz_reactions_company_post", "buzz_reactions", ["company_id", "post_id"])
    op.create_table(
        "buzz_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_buzz_reports_company_id"), nullable=False),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("buzz_posts.id", name="fk_buzz_reports_post_id"), nullable=False),
        sa.Column("comment_id", sa.Integer(), sa.ForeignKey("buzz_comments.id", name="fk_buzz_reports_comment_id"), nullable=True),
        sa.Column("reporter_key", sa.String(20), nullable=False),
        sa.Column("reporter_employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_buzz_reports_reporter_employee_id"), nullable=True),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("note", sa.String(1000), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'open'")),
        sa.Column("resolved_by_name", sa.String(200), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index("ix_buzz_reports_company_status", "buzz_reports", ["company_id", "status"])
    op.create_table(
        "celebration_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_celebration_events_company_id"), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", name="fk_celebration_events_employee_id"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("years", sa.Integer(), nullable=True),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("buzz_posts.id", name="fk_celebration_events_post_id"), nullable=True),
        sa.Column("email_status", sa.String(20), nullable=True),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint("company_id", "employee_id", "kind", "event_date", name="uq_celebration_events_once"),
    )
    op.create_index("ix_celebration_events_company_date", "celebration_events", ["company_id", "event_date"])
    op.create_table(
        "kiosks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", name="fk_kiosks_company_id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("slides", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("seconds_per_slide", sa.Integer(), nullable=False, server_default=sa.text('10')),
        sa.Column("audience", postgresql.JSONB(), nullable=False, server_default=sa.text('\'{"all": true}\'::jsonb')),
        sa.Column("theme", sa.String(20), nullable=False, server_default=sa.text("'dark'")),
        sa.Column("welcome_text", sa.String(200), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index("ix_kiosks_company", "kiosks", ["company_id"])
    op.create_index("uq_kiosks_token", "kiosks", ["token"], unique=True)

    for table in TABLES:
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)

    # notification centre
    op.add_column("notifications", sa.Column("recipient", sa.String(10), nullable=True))
    op.add_column("notifications", sa.Column("title", sa.String(200), nullable=True))
    op.add_column("notifications", sa.Column("link", sa.String(500), nullable=True))
    op.add_column("notifications", sa.Column("actor_name", sa.String(200), nullable=True))
    op.add_column("notifications", sa.Column("actor_employee_id", sa.Integer(), nullable=True))
    op.add_column("notifications", sa.Column("group_key", sa.String(120), nullable=True))
    op.add_column("notifications", sa.Column("data", postgresql.JSONB(), nullable=True))
    op.add_column("notifications", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_notifications_company_recipient_unread", "notifications", ["company_id", "employee_id", "read_at"])


def downgrade():
    op.drop_index("ix_notifications_company_recipient_unread", table_name="notifications")
    for col in ("read_at", "data", "group_key", "actor_employee_id", "actor_name", "link", "title", "recipient"):
        op.drop_column("notifications", col)
    for table in reversed(TABLES):
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
        op.drop_table(table)
