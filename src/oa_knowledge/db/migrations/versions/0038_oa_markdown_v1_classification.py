"""Add OA Markdown V1 classification decisions and versioned parse identity."""

from alembic import op
import sqlalchemy as sa


revision = "0038_oa_markdown_v1_classification"
down_revision = "0037_no_attachment_evidence"
branch_labels = None
depends_on = None


CLASSIFICATION_STATUSES = "'classified', 'needs_review', 'excluded'"
INTEGRITY_STATUSES = (
    "'ok', 'no_attachment_confirmed', 'missing', 'size_mismatch', "
    "'sha256_mismatch', 'download_failed', 'not_checked'"
)
BUSINESS_CATEGORIES = (
    "'01_公司治理与决策', '02_业务项目与投放租后', '03_风险合规审计法务', "
    "'04_财务资金与融资', '05_经营计划与绩效考核', '06_人力资源', "
    "'07_党建纪检与工会', '08_行政采购与信息化', '09_对外报送与监管反馈', "
    "'99_其他内部'"
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _parse_columns() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("parse_artifacts")
    }


def _parse_indexes() -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("parse_artifacts")
    }


def upgrade() -> None:
    if "profile_version" not in _parse_columns():
        with op.batch_alter_table("parse_artifacts") as batch:
            batch.add_column(sa.Column(
                "profile_version",
                sa.String(80),
                nullable=False,
                server_default="legacy",
            ))
        op.execute(sa.text(
            """
            UPDATE parse_artifacts
            SET profile_version = 'legacy-duplicate-' || id
            WHERE content_object_id IS NOT NULL
              AND id NOT IN (
                  SELECT MIN(id)
                  FROM parse_artifacts
                  WHERE content_object_id IS NOT NULL
                  GROUP BY content_object_id, engine, engine_version, config_hash
              )
            """
        ))
    if "uq_parse_artifact_reuse_identity" not in _parse_indexes():
        op.create_index(
            "uq_parse_artifact_reuse_identity",
            "parse_artifacts",
            ["content_object_id", "engine", "engine_version", "profile_version", "config_hash"],
            unique=True,
        )

    tables = _tables()
    if "classification_runs" not in tables:
        op.create_table(
            "classification_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.String(80), nullable=False),
            sa.Column("run_kind", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("input_signature", sa.String(64), nullable=False),
            sa.Column("manifest_sha256", sa.String(64), nullable=False),
            sa.Column("exclusion_policy_sha256", sa.String(64), nullable=False),
            sa.Column("rule_version", sa.String(40), nullable=False),
            sa.Column("schema_version", sa.String(40), nullable=False),
            sa.Column("prompt_version", sa.String(40), nullable=False),
            sa.Column("model_name", sa.String(120), nullable=False),
            sa.Column("private_config_sha256", sa.String(64), nullable=False),
            sa.Column("target_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("excluded_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("run_id", name="uq_classification_run_id"),
            sa.CheckConstraint("run_kind IN ('full', 'incremental')", name="ck_classification_run_kind"),
            sa.CheckConstraint("status IN ('created', 'running', 'completed', 'failed')", name="ck_classification_run_status"),
            sa.CheckConstraint("target_count >= 0 AND excluded_count >= 0", name="ck_classification_run_counts"),
        )

    tables = _tables()
    if "classification_run_items" not in tables:
        op.create_table(
            "classification_run_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("classification_run_id", sa.Integer(), sa.ForeignKey("classification_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("oa_item_key", sa.Text(), nullable=False),
            sa.Column("inclusion_reason", sa.String(40), nullable=False),
            sa.Column("stage", sa.String(20), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error_code", sa.String(80)),
            sa.Column("last_error_detail", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("classification_run_id", "oa_item_key", name="uq_classification_run_item"),
            sa.CheckConstraint(
                "stage IN ('queued', 'metadata', 'parse', 'content', 'qwen', 'decided', 'failed')",
                name="ck_classification_run_item_stage",
            ),
            sa.CheckConstraint("attempts >= 0", name="ck_classification_run_item_attempts"),
        )

    tables = _tables()
    if "classification_decisions" not in tables:
        op.create_table(
            "classification_decisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("classification_run_id", sa.Integer(), sa.ForeignKey("classification_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("oa_item_key", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("decision_input_sha256", sa.String(64), nullable=False),
            sa.Column("decision_source", sa.String(20), nullable=False),
            sa.Column("classification_status", sa.String(20), nullable=False),
            sa.Column("content_integrity_status", sa.String(30), nullable=False),
            sa.Column("content_origin", sa.String(20)),
            sa.Column("flow_type", sa.String(40)),
            sa.Column("initiator", sa.Text()),
            sa.Column("initiator_type", sa.String(20), nullable=False),
            sa.Column("relay_from", sa.Text()),
            sa.Column("transfer_chain_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("issuer", sa.Text()),
            sa.Column("canonical_issuer", sa.Text()),
            sa.Column("business_category", sa.String(80)),
            sa.Column("document_number", sa.Text()),
            sa.Column("document_type", sa.String(80)),
            sa.Column("normalized_title", sa.Text(), nullable=False),
            sa.Column("classification_confidence", sa.Float(), nullable=False),
            sa.Column("classification_reason_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("rule_version", sa.String(40), nullable=False),
            sa.Column("private_config_sha256", sa.String(64), nullable=False),
            sa.Column("manual_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("actor", sa.String(120)),
            sa.Column("supersedes_decision_id", sa.Integer(), sa.ForeignKey("classification_decisions.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("oa_item_key", "version", name="uq_classification_decision_version"),
            sa.CheckConstraint("version >= 1", name="ck_classification_decision_version"),
            sa.CheckConstraint(f"classification_status IN ({CLASSIFICATION_STATUSES})", name="ck_classification_decision_status"),
            sa.CheckConstraint(f"content_integrity_status IN ({INTEGRITY_STATUSES})", name="ck_classification_integrity_status"),
            sa.CheckConstraint("content_origin IS NULL OR content_origin IN ('internal', 'external')", name="ck_classification_content_origin"),
            sa.CheckConstraint("decision_source IN ('metadata_rule', 'content_rule', 'local_qwen', 'manual')", name="ck_classification_decision_source"),
            sa.CheckConstraint("initiator_type IN ('internal', 'external', 'mixed', 'system', 'unknown')", name="ck_classification_initiator_type"),
            sa.CheckConstraint("classification_confidence >= 0 AND classification_confidence <= 1", name="ck_classification_confidence"),
            sa.CheckConstraint(
                "business_category IS NULL OR "
                "(content_origin IS NOT NULL AND content_origin = 'internal')",
                name="ck_classification_category_requires_internal",
            ),
            sa.CheckConstraint(
                "content_origin <> 'external' OR "
                "(canonical_issuer IS NOT NULL AND trim(canonical_issuer) <> '')",
                name="ck_classification_external_issuer_required",
            ),
            sa.CheckConstraint(
                "canonical_issuer IS NULL OR content_origin = 'external'",
                name="ck_classification_issuer_requires_external",
            ),
            sa.CheckConstraint(
                "manual_locked = 0 OR "
                "(decision_source = 'manual' AND actor IS NOT NULL AND trim(actor) <> '')",
                name="ck_classification_manual_lock_provenance",
            ),
            sa.CheckConstraint(
                "decision_source <> 'manual' OR (actor IS NOT NULL AND trim(actor) <> '')",
                name="ck_classification_manual_actor_required",
            ),
            sa.CheckConstraint(
                "classification_status <> 'classified' OR "
                f"(content_origin = 'internal' AND business_category IN ({BUSINESS_CATEGORIES})) OR "
                "(content_origin = 'external' AND canonical_issuer IS NOT NULL AND trim(canonical_issuer) <> '')",
                name="ck_classification_publish_fields",
            ),
            sa.CheckConstraint(
                "classification_status <> 'excluded' OR "
                "(content_origin IS NULL AND business_category IS NULL AND canonical_issuer IS NULL)",
                name="ck_classification_excluded_fields",
            ),
        )
        op.create_index(
            "uq_classification_current_decision",
            "classification_decisions",
            ["oa_item_key"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )
        op.create_index(
            "ix_classification_decision_run_status",
            "classification_decisions",
            ["classification_run_id", "classification_status"],
        )

    tables = _tables()
    if "classification_evidence" not in tables:
        op.create_table(
            "classification_evidence",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("classification_decision_id", sa.Integer(), sa.ForeignKey("classification_decisions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("evidence_type", sa.String(80), nullable=False),
            sa.Column("evidence_scope", sa.String(20), nullable=False),
            sa.Column("value_json", sa.Text(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("source_file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="SET NULL")),
            sa.Column("parse_artifact_id", sa.Integer(), sa.ForeignKey("parse_artifacts.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("classification_decision_id", "sequence", name="uq_classification_evidence_sequence"),
            sa.CheckConstraint("sequence >= 1", name="ck_classification_evidence_sequence"),
            sa.CheckConstraint("evidence_scope IN ('package', 'attachment')", name="ck_classification_evidence_scope"),
            sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_classification_evidence_confidence"),
        )


def downgrade() -> None:
    tables = _tables()
    for table in (
        "classification_evidence",
        "classification_decisions",
        "classification_run_items",
        "classification_runs",
    ):
        if table in tables:
            op.drop_table(table)
    if "uq_parse_artifact_reuse_identity" in _parse_indexes():
        op.drop_index("uq_parse_artifact_reuse_identity", table_name="parse_artifacts")
    if "profile_version" in _parse_columns():
        with op.batch_alter_table("parse_artifacts") as batch:
            batch.drop_column("profile_version")
