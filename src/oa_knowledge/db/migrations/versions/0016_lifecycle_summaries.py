"""Add logical lifecycle, versioned summaries, request audit, and resource leases."""

from alembic import op
import sqlalchemy as sa


revision = "0016_lifecycle_summaries"
down_revision = "0015_parse_artifact_timestamp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "logical_items" not in tables:
        op.create_table(
            "logical_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("logical_key", sa.String(120), nullable=False, unique=True),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("lifecycle_status", sa.String(30), nullable=False, server_default="discovered"),
            sa.Column("current_pending_summary_id", sa.Integer()),
            sa.Column("current_done_summary_id", sa.Integer()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        )

    oa_columns = {column["name"] for column in sa.inspect(bind).get_columns("oa_items")}
    if "logical_item_id" not in oa_columns:
        op.add_column("oa_items", sa.Column("logical_item_id", sa.Integer(), nullable=True))
        op.create_index("ix_oa_items_logical_item_id", "oa_items", ["logical_item_id"])

    tables = set(sa.inspect(bind).get_table_names())
    if "item_occurrences" not in tables:
        op.create_table(
            "item_occurrences",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("logical_item_id", sa.Integer(), sa.ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False),
            sa.Column("oa_item_id", sa.Integer(), sa.ForeignKey("oa_items.id", ondelete="SET NULL")),
            sa.Column("occurrence_key", sa.String(180), nullable=False, unique=True),
            sa.Column("channel", sa.String(30), nullable=False),
            sa.Column("workitem_id_text", sa.String()),
            sa.Column("process_id_text", sa.String()),
            sa.Column("summary_id_text", sa.String()),
            sa.Column("affair_id_text", sa.String()),
            sa.Column("form_record_id_text", sa.String()),
            sa.Column("object_id_text", sa.String()),
            sa.Column("detail_url", sa.Text()),
            sa.Column("title", sa.Text()),
            sa.Column("sender", sa.Text()),
            sa.Column("occurrence_status", sa.String(30), nullable=False, server_default="active"),
            sa.Column("raw_fields_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        )
        op.create_index("ix_occurrences_process_id", "item_occurrences", ["process_id_text"])
        op.create_index("ix_occurrences_summary_id", "item_occurrences", ["summary_id_text"])

    tables = set(sa.inspect(bind).get_table_names())
    if "item_snapshots" not in tables:
        op.create_table(
            "item_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("logical_item_id", sa.Integer(), sa.ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False),
            sa.Column("occurrence_id", sa.Integer(), sa.ForeignKey("item_occurrences.id", ondelete="SET NULL")),
            sa.Column("snapshot_kind", sa.String(30), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("is_canonical", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("logical_item_id", "snapshot_kind", "version", name="uq_item_snapshot_version"),
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "summary_jobs" not in tables:
        op.create_table(
            "summary_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("logical_item_id", sa.Integer(), sa.ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False),
            sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("item_snapshots.id", ondelete="CASCADE"), nullable=False),
            sa.Column("summary_kind", sa.String(30), nullable=False),
            sa.Column("stage", sa.String(30), nullable=False, server_default="item_summary"),
            sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
            sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("last_error_code", sa.String(80)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "summary_versions" not in tables:
        op.create_table(
            "summary_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("logical_item_id", sa.Integer(), sa.ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False),
            sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("item_snapshots.id", ondelete="CASCADE"), nullable=False),
            sa.Column("summary_job_id", sa.Integer(), sa.ForeignKey("summary_jobs.id", ondelete="SET NULL")),
            sa.Column("summary_kind", sa.String(30), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="candidate"),
            sa.Column("input_hash", sa.String(64), nullable=False),
            sa.Column("structured_json", sa.Text(), nullable=False),
            sa.Column("provider_name", sa.String(80), nullable=False),
            sa.Column("model_name", sa.String(120), nullable=False),
            sa.Column("prompt_version", sa.String(80), nullable=False),
            sa.Column("elapsed_seconds", sa.Float()),
            sa.Column("prompt_tokens", sa.Integer()),
            sa.Column("completion_tokens", sa.Integer()),
            sa.Column("confidence", sa.Float()),
            sa.Column("review_status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("schema_valid", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("logical_item_id", "summary_kind", "version", name="uq_summary_version"),
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "summary_evidence" not in tables:
        op.create_table(
            "summary_evidence",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("summary_version_id", sa.Integer(), sa.ForeignKey("summary_versions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("item_snapshots.id", ondelete="CASCADE"), nullable=False),
            sa.Column("file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="SET NULL")),
            sa.Column("parse_artifact_id", sa.Integer(), sa.ForeignKey("parse_artifacts.id", ondelete="SET NULL")),
            sa.Column("evidence_kind", sa.String(30), nullable=False),
            sa.Column("locator", sa.String(180), nullable=False),
            sa.Column("evidence_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("summary_version_id", "locator", name="uq_summary_evidence_locator"),
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "resource_leases" not in tables:
        op.create_table(
            "resource_leases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("resource_key", sa.String(80), nullable=False, unique=True),
            sa.Column("resource_kind", sa.String(30), nullable=False),
            sa.Column("owner_id", sa.String(120), nullable=False),
            sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "llm_request_audits" not in tables:
        op.create_table(
            "llm_request_audits",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("summary_job_id", sa.Integer(), sa.ForeignKey("summary_jobs.id", ondelete="SET NULL")),
            sa.Column("provider_name", sa.String(80), nullable=False),
            sa.Column("model_name", sa.String(120), nullable=False),
            sa.Column("provider_mode", sa.String(30), nullable=False),
            sa.Column("content_classification", sa.String(30), nullable=False),
            sa.Column("redaction_status", sa.String(30), nullable=False),
            sa.Column("input_hash", sa.String(64), nullable=False),
            sa.Column("decision", sa.String(20), nullable=False),
            sa.Column("reason_code", sa.String(80), nullable=False),
            sa.Column("elapsed_seconds", sa.Float()),
            sa.Column("prompt_tokens", sa.Integer()),
            sa.Column("completion_tokens", sa.Integer()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        )


def downgrade() -> None:
    op.drop_table("llm_request_audits")
    op.drop_table("resource_leases")
    op.drop_table("summary_evidence")
    op.drop_table("summary_versions")
    op.drop_table("summary_jobs")
    op.drop_table("item_snapshots")
    op.drop_index("ix_occurrences_summary_id", table_name="item_occurrences")
    op.drop_index("ix_occurrences_process_id", table_name="item_occurrences")
    op.drop_table("item_occurrences")
    op.drop_index("ix_oa_items_logical_item_id", table_name="oa_items")
    op.drop_column("oa_items", "logical_item_id")
    op.drop_table("logical_items")
