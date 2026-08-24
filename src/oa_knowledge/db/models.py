from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func as sa_func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class OAItem(Base):
    __tablename__ = "oa_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    oa_item_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    logical_item_id: Mapped[int | None] = mapped_column(ForeignKey("logical_items.id", ondelete="SET NULL"))
    workitem_id_text: Mapped[str | None] = mapped_column(String)
    source_channel: Mapped[str] = mapped_column(String, nullable=False)
    process_id_text: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    sender: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    document_number: Mapped[str | None] = mapped_column(String)
    initiated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    oa_status: Mapped[str | None] = mapped_column(String)
    pipeline_status: Mapped[str] = mapped_column(String, nullable=False, default="discovered")
    archive_relpath: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    source_type: Mapped[str | None] = mapped_column(String(20))
    internal_category: Mapped[str | None] = mapped_column(String(80))
    external_issuer: Mapped[str | None] = mapped_column(Text)
    classification_version: Mapped[str | None] = mapped_column(String(20))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    files: Mapped[list[ArchivedFile]] = relationship(back_populates="item", cascade="all, delete-orphan")


class OAManifestItem(Base):
    """Canonical, deduplicated snapshot of every row in OA's Done list."""
    __tablename__ = "oa_manifest_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    oa_item_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    workitem_id_text: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    sender: Mapped[str | None] = mapped_column(Text)
    initiated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    list_page: Mapped[int] = mapped_column(Integer, nullable=False)
    # OA list order is page order plus the row position observed on that page.
    # It is deliberately independent from initiation/completion timestamps.
    list_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovery_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processing_status: Mapped[str] = mapped_column(String, nullable=False, default="discovered")
    no_attachment_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    matched_exclusion_keyword: Mapped[str | None] = mapped_column(Text)
    archive_relpath: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_stage: Mapped[str | None] = mapped_column(String)
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAManifestSync(Base):
    __tablename__ = "oa_manifest_syncs"
    id: Mapped[int] = mapped_column(primary_key=True)
    oa_total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    local_manifest_count: Mapped[int] = mapped_column(Integer, nullable=False)
    pages_scanned: Mapped[int] = mapped_column(Integer, nullable=False)
    source_total_pages: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArchivedFile(Base):
    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("oa_item_id", "attachment_key", "file_role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    oa_item_id: Mapped[int] = mapped_column(ForeignKey("oa_items.id", ondelete="CASCADE"), nullable=False)
    parent_file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"))
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    local_relpath: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    content_object_id: Mapped[int | None] = mapped_column(ForeignKey("content_objects.id", ondelete="SET NULL"))
    attachment_key: Mapped[str] = mapped_column(String, nullable=False)
    file_role: Mapped[str] = mapped_column(String, nullable=False)
    source_container_key: Mapped[str] = mapped_column(String, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    download_status: Mapped[str] = mapped_column(String, nullable=False, default="discovered")
    download_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_size: Mapped[int | None] = mapped_column(Integer)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parse_jobs: Mapped[list[ParseJob]] = relationship(back_populates="file")
    item: Mapped[OAItem] = relationship(back_populates="files")
    __table_args__ = (
        UniqueConstraint("oa_item_id", "attachment_key", "file_role"),
        CheckConstraint("depth >= 1 AND depth <= 10", name="ck_files_depth_1_10"),
    )


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary_json: Mapped[str | None] = mapped_column(Text)


class OperationJob(Base):
    __tablename__ = "operation_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','paused','completed','cancelled','auth_required','failed')",
            name="ck_operation_jobs_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    job_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    job_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    lease_owner: Mapped[str | None] = mapped_column(String)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String)
    events: Mapped[list[OperationEvent]] = relationship(back_populates="job", cascade="all, delete-orphan")


class OperationEvent(Base):
    __tablename__ = "operation_events"
    __table_args__ = (UniqueConstraint("job_id", "sequence"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("operation_jobs.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    job: Mapped[OperationJob] = relationship(back_populates="events")


class CollectionBatch(Base):
    __tablename__ = "collection_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    source_channel: Mapped[str] = mapped_column(String, nullable=False)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_field: Mapped[str] = mapped_column(String, nullable=False, default="completed_at")
    planned_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="planned")
    discovered_count: Mapped[int] = mapped_column(Integer, default=0)
    archived_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    source_total_count: Mapped[int | None] = mapped_column(Integer)
    source_total_pages: Mapped[int | None] = mapped_column(Integer)
    pages_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scanned_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    taxonomy_version: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text)
    plan_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items: Mapped[list[BatchItem]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class BatchItem(Base):
    __tablename__ = "batch_items"
    __table_args__ = (UniqueConstraint("batch_id", "oa_item_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("collection_batches.id", ondelete="CASCADE"), nullable=False)
    oa_item_key: Mapped[str] = mapped_column(String, nullable=False)
    workitem_id_text: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sender: Mapped[str | None] = mapped_column(Text)
    deadline_text: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    list_page: Mapped[int | None] = mapped_column(Integer)
    discovery_status: Mapped[str] = mapped_column(String, nullable=False, default="discovered")
    archive_status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    oa_item_id: Mapped[int | None] = mapped_column(ForeignKey("oa_items.id", ondelete="SET NULL"))
    detail_url: Mapped[str | None] = mapped_column(Text)
    archive_manifest_relpath: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    skip_reason: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str | None] = mapped_column(String)
    batch: Mapped[CollectionBatch] = relationship(back_populates="items")


class ParseJob(Base):
    __tablename__ = "parse_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    engine: Mapped[str] = mapped_column(String, nullable=False)
    engine_version: Mapped[str] = mapped_column(String, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    quality_score: Mapped[float | None] = mapped_column(Float)
    output_relpath: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    file: Mapped[ArchivedFile] = relationship(back_populates="parse_jobs")


class Classification(Base):
    __tablename__ = "classifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("oa_items.id", ondelete="CASCADE"), nullable=False)
    facet: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    rule_or_prompt_version: Mapped[str | None] = mapped_column(String)
    review_status: Mapped[str] = mapped_column(String, default="pending")


class Relation(Base):
    __tablename__ = "relations"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_item_id: Mapped[int] = mapped_column(ForeignKey("oa_items.id", ondelete="CASCADE"), nullable=False)
    target_item_id: Mapped[int] = mapped_column(ForeignKey("oa_items.id", ondelete="CASCADE"), nullable=False)
    relation_type: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(String, default="pending")


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_item_id: Mapped[int] = mapped_column(ForeignKey("oa_items.id", ondelete="CASCADE"), nullable=False)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    responsible_party: Mapped[str | None] = mapped_column(Text)
    deadline: Mapped[date | None] = mapped_column(Date)
    deadline_type: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    priority: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="candidate")
    source_kind: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_page: Mapped[int | None] = mapped_column(Integer)
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, default=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class ExclusionPolicy(Base):
    __tablename__ = "exclusion_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False, default="skip")
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="title")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False, default="v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ExclusionPolicyRevision(Base):
    __tablename__ = "exclusion_policy_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int | None] = mapped_column(ForeignKey("exclusion_policies.id", ondelete="SET NULL"))
    policy_name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    change_type: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(40), nullable=False, default="local_web")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ParseArtifact(Base):
    __tablename__ = "parse_artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    parse_job_id: Mapped[int] = mapped_column(ForeignKey("parse_jobs.id", ondelete="CASCADE"), nullable=False)
    content_object_id: Mapped[int | None] = mapped_column(ForeignKey("content_objects.id", ondelete="CASCADE"))
    engine: Mapped[str] = mapped_column(String(20), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(40), nullable=False)
    output_relpath: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    product_sha256: Mapped[str | None] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    page_map_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    quality_score: Mapped[float | None] = mapped_column(Float)
    quality_status: Mapped[str | None] = mapped_column(String(20))
    lifecycle_status: Mapped[str] = mapped_column(String(20), nullable=False, default="valid")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ContentObject(Base):
    __tablename__ = "content_objects"
    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    detected_type: Mapped[str | None] = mapped_column(String(40))
    active_parse_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("parse_artifacts.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MarkdownExport(Base):
    __tablename__ = "markdown_exports"
    __table_args__ = (
        UniqueConstraint("source_file_id", "schema_version", name="uq_markdown_export_source_schema"),
        UniqueConstraint("markdown_relpath", name="uq_markdown_export_relpath"),
        CheckConstraint("markdown_relpath NOT LIKE '/%' AND markdown_relpath <> '..' AND markdown_relpath NOT LIKE '../%' AND markdown_relpath NOT LIKE '%/../%' AND markdown_relpath NOT LIKE '%/..'", name="ck_markdown_export_relative"),
        CheckConstraint("document_kind IN ('attachment', 'item_index')", name="ck_markdown_export_document_kind"),
        Index(
            "uq_markdown_export_item_index_schema", "oa_item_id", "schema_version",
            unique=True, sqlite_where=text("document_kind = 'item_index'"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"))
    oa_item_id: Mapped[int | None] = mapped_column(ForeignKey("oa_items.id", ondelete="SET NULL"))
    document_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="attachment")
    content_object_id: Mapped[int | None] = mapped_column(ForeignKey("content_objects.id", ondelete="SET NULL"))
    parse_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("parse_artifacts.id", ondelete="SET NULL"))
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_relpath: Mapped[str] = mapped_column(Text, nullable=False)
    markdown_relpath: Mapped[str] = mapped_column(Text, nullable=False)
    markdown_sha256: Mapped[str | None] = mapped_column(String(64))
    assets_relpath: Mapped[str | None] = mapped_column(Text)
    parse_engine: Mapped[str] = mapped_column(String(40), nullable=False)
    parse_engine_version: Mapped[str] = mapped_column(String(80), nullable=False)
    parse_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    quality_score: Mapped[float | None] = mapped_column(Float)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    last_error: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MarkdownTask(Base):
    __tablename__ = "markdown_tasks"
    __table_args__ = (UniqueConstraint("source_file_id", "schema_version", name="uq_markdown_tasks_source_schema"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_engine: Mapped[str | None] = mapped_column(String(30))
    campaign: Mapped[str] = mapped_column(String(40), nullable=False, default="standard")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    elapsed_seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MarkdownQueueControl(Base):
    __tablename__ = "markdown_queue_control"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pdf_mineru_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MarkdownTaskEvent(Base):
    __tablename__ = "markdown_task_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("markdown_tasks.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlineAuditRun(Base):
    __tablename__ = "online_audit_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("operation_jobs.id", ondelete="SET NULL"), unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mismatch_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    access_failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pause_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_oa_item_key: Mapped[str | None] = mapped_column(String(180))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OnlineAuditItem(Base):
    __tablename__ = "online_audit_items"
    __table_args__ = (UniqueConstraint("run_id", "oa_item_key", name="uq_online_audit_run_item"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("online_audit_runs.id", ondelete="CASCADE"), nullable=False)
    manifest_item_id: Mapped[int | None] = mapped_column(ForeignKey("oa_manifest_items.id", ondelete="SET NULL"))
    oa_item_key: Mapped[str] = mapped_column(String(180), nullable=False)
    workitem_id_text: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    recognized_attachments: Mapped[int | None] = mapped_column(Integer)
    database_attachments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    downloaded_attachments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    markdown_attachments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    online_inventory_sha256: Mapped[str | None] = mapped_column(String(64))
    local_inventory_sha256: Mapped[str | None] = mapped_column(String(64))
    online_content_sha256: Mapped[str | None] = mapped_column(String(64))
    local_content_sha256: Mapped[str | None] = mapped_column(String(64))
    online_evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    local_evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    comparison_reason: Mapped[str | None] = mapped_column(String(80))
    depth_limit_reached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    elapsed_seconds: Mapped[float | None] = mapped_column(Float)


class OnlineAuditEvent(Base):
    __tablename__ = "online_audit_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_online_audit_event_sequence"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("online_audit_runs.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("online_audit_items.id", ondelete="SET NULL"))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    knowledge_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    content_object_id: Mapped[int] = mapped_column(ForeignKey("content_objects.id", ondelete="CASCADE"), nullable=False)
    active_parse_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("parse_artifacts.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    vault_relpath: Mapped[str | None] = mapped_column(Text)
    publish_status: Mapped[str] = mapped_column(String(20), nullable=False, default="candidate")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SourceReference(Base):
    __tablename__ = "source_references"
    __table_args__ = (UniqueConstraint("knowledge_document_id", "source_file_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    knowledge_document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    oa_item_id: Mapped[int] = mapped_column(ForeignKey("oa_items.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceAttachment(Base):
    __tablename__ = "source_attachments"
    __table_args__ = (UniqueConstraint("snapshot_id", "source_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("item_snapshots.id", ondelete="CASCADE"), nullable=False)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"))
    source_key: Mapped[str] = mapped_column(String(180), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    is_main_document: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_title: Mapped[str | None] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    download_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    content_object_id: Mapped[int | None] = mapped_column(ForeignKey("content_objects.id", ondelete="SET NULL"))
    error_code: Mapped[str | None] = mapped_column(String(80))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ArchivePackage(Base):
    __tablename__ = "archive_packages"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_attachment_id: Mapped[int] = mapped_column(ForeignKey("source_attachments.id", ondelete="CASCADE"), nullable=False)
    parent_package_id: Mapped[int | None] = mapped_column(ForeignKey("archive_packages.id", ondelete="SET NULL"))
    package_key: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_format: Mapped[str] = mapped_column(String(20), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    compressed_size_bytes: Mapped[int | None] = mapped_column(Integer)
    extracted_size_bytes: Mapped[int | None] = mapped_column(Integer)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tree_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    security_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ArchiveMember(Base):
    __tablename__ = "archive_members"
    __table_args__ = (UniqueConstraint("archive_package_id", "member_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    archive_package_id: Mapped[int] = mapped_column(ForeignKey("archive_packages.id", ondelete="CASCADE"), nullable=False)
    child_archive_package_id: Mapped[int | None] = mapped_column(ForeignKey("archive_packages.id", ondelete="SET NULL"))
    member_key: Mapped[str] = mapped_column(String(180), nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_path: Mapped[str | None] = mapped_column(Text)
    member_type: Mapped[str] = mapped_column(String(30), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    compressed_size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    content_object_id: Mapped[int | None] = mapped_column(ForeignKey("content_objects.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(String(80))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OAItemDocumentRelation(Base):
    __tablename__ = "oa_item_document_relations"
    __table_args__ = (UniqueConstraint("logical_item_id", "knowledge_document_id", "source_attachment_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    logical_item_id: Mapped[int] = mapped_column(ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False)
    knowledge_document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False)
    source_attachment_id: Mapped[int | None] = mapped_column(ForeignKey("source_attachments.id", ondelete="SET NULL"))
    archive_member_id: Mapped[int | None] = mapped_column(ForeignKey("archive_members.id", ondelete="SET NULL"))
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    is_main_document: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CuratedRun(Base):
    """A versioned curation attempt for one logical OA package."""

    __tablename__ = "curated_runs"
    __table_args__ = (UniqueConstraint("logical_item_id", "input_signature", name="uq_curated_run_input"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    logical_item_id: Mapped[int] = mapped_column(ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False)
    input_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    rules_version: Mapped[str] = mapped_column(String(40), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    config_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CuratedDecision(Base):
    """One of zero-to-many structured documents selected from a package."""

    __tablename__ = "curated_decisions"
    __table_args__ = (
        UniqueConstraint("curated_run_id", "ordinal", name="uq_curated_decision_ordinal"),
        UniqueConstraint("curated_run_id", "decision_hash", name="uq_curated_decision_hash"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    curated_run_id: Mapped[int] = mapped_column(ForeignKey("curated_runs.id", ondelete="CASCADE"), nullable=False)
    knowledge_document_id: Mapped[int | None] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="SET NULL"))
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    document_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    canonical_key: Mapped[str | None] = mapped_column(String(160))
    normalized_title: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_relpath: Mapped[str | None] = mapped_column(Text)
    review_reason: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CuratedDecisionSource(Base):
    """Validated, ordered membership edge from a decision to package evidence."""

    __tablename__ = "curated_decision_sources"
    __table_args__ = (
        UniqueConstraint("curated_decision_id", "source_key", name="uq_curated_decision_source_key"),
        UniqueConstraint("curated_decision_id", "ordinal", name="uq_curated_decision_source_ordinal"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    curated_decision_id: Mapped[int] = mapped_column(ForeignKey("curated_decisions.id", ondelete="CASCADE"), nullable=False)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"))
    source_attachment_id: Mapped[int | None] = mapped_column(ForeignKey("source_attachments.id", ondelete="SET NULL"))
    archive_member_id: Mapped[int | None] = mapped_column(ForeignKey("archive_members.id", ondelete="SET NULL"))
    parse_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("parse_artifacts.id", ondelete="SET NULL"))
    source_key: Mapped[str] = mapped_column(String(180), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LogicalItem(Base):
    __tablename__ = "logical_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    logical_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(30), nullable=False, default="discovered")
    current_pending_summary_id: Mapped[int | None] = mapped_column(Integer)
    current_done_summary_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ItemOccurrence(Base):
    __tablename__ = "item_occurrences"
    id: Mapped[int] = mapped_column(primary_key=True)
    logical_item_id: Mapped[int] = mapped_column(ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False)
    oa_item_id: Mapped[int | None] = mapped_column(ForeignKey("oa_items.id", ondelete="SET NULL"))
    occurrence_key: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    workitem_id_text: Mapped[str | None] = mapped_column(String)
    process_id_text: Mapped[str | None] = mapped_column(String)
    summary_id_text: Mapped[str | None] = mapped_column(String)
    affair_id_text: Mapped[str | None] = mapped_column(String)
    activity_id_text: Mapped[str | None] = mapped_column(String)
    case_id_text: Mapped[str | None] = mapped_column(String)
    form_record_id_text: Mapped[str | None] = mapped_column(String)
    object_id_text: Mapped[str | None] = mapped_column(String)
    template_id_text: Mapped[str | None] = mapped_column(String)
    identity_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    sender: Mapped[str | None] = mapped_column(Text)
    previous_approver: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    initiated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_text: Mapped[str | None] = mapped_column(Text)
    reminder_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processing_status: Mapped[str | None] = mapped_column(String(40))
    current_node: Mapped[str | None] = mapped_column(Text)
    importance: Mapped[str | None] = mapped_column(String(30))
    discovery_hash: Mapped[str | None] = mapped_column(String(64))
    occurrence_status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    raw_fields_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Pending-notification data cleanup ledger (plan-0807-1 §6).
    cleanup_status: Mapped[str | None] = mapped_column(String(30))
    cleanup_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_error_code: Mapped[str | None] = mapped_column(String(80))
    cleanup_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notify_fingerprint: Mapped[str | None] = mapped_column(String(64))
    allow_renotify: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Set when an on-demand OA re-sync confirmed the item is no longer present
    # in OA's pending list (so no title can be recovered). plan-0807-1 §sync.
    oa_gone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ItemSnapshot(Base):
    __tablename__ = "item_snapshots"
    __table_args__ = (UniqueConstraint("logical_item_id", "snapshot_kind", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    logical_item_id: Mapped[int] = mapped_column(ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False)
    occurrence_id: Mapped[int | None] = mapped_column(ForeignKey("item_occurrences.id", ondelete="SET NULL"))
    snapshot_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    is_canonical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SummaryJob(Base):
    __tablename__ = "summary_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    logical_item_id: Mapped[int] = mapped_column(ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("item_snapshots.id", ondelete="CASCADE"), nullable=False)
    summary_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    stage: Mapped[str] = mapped_column(String(30), nullable=False, default="item_summary")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SummaryVersion(Base):
    __tablename__ = "summary_versions"
    __table_args__ = (UniqueConstraint("logical_item_id", "summary_kind", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    logical_item_id: Mapped[int] = mapped_column(ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("item_snapshots.id", ondelete="CASCADE"), nullable=False)
    summary_job_id: Mapped[int | None] = mapped_column(ForeignKey("summary_jobs.id", ondelete="SET NULL"))
    summary_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="candidate")
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    structured_json: Mapped[str] = mapped_column(Text, nullable=False)
    provider_name: Mapped[str] = mapped_column(String(80), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    elapsed_seconds: Mapped[float | None] = mapped_column(Float)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    review_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    schema_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SummaryEvidence(Base):
    __tablename__ = "summary_evidence"
    __table_args__ = (UniqueConstraint("summary_version_id", "locator"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    summary_version_id: Mapped[int] = mapped_column(ForeignKey("summary_versions.id", ondelete="CASCADE"), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("item_snapshots.id", ondelete="CASCADE"), nullable=False)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"))
    parse_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("parse_artifacts.id", ondelete="SET NULL"))
    evidence_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    locator: Mapped[str] = mapped_column(String(180), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResourceLease(Base):
    __tablename__ = "resource_leases"
    id: Mapped[int] = mapped_column(primary_key=True)
    resource_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LlmRequestAudit(Base):
    __tablename__ = "llm_request_audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    summary_job_id: Mapped[int | None] = mapped_column(ForeignKey("summary_jobs.id", ondelete="SET NULL"))
    provider_name: Mapped[str] = mapped_column(String(80), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    content_classification: Mapped[str] = mapped_column(String(30), nullable=False)
    redaction_status: Mapped[str] = mapped_column(String(30), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    elapsed_seconds: Mapped[float | None] = mapped_column(Float)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    pipeline_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="running")
    total_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PipelineTask(Base):
    __tablename__ = "pipeline_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_runs.id", ondelete="SET NULL"))
    queue_name: Mapped[str] = mapped_column(String(40), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    logical_item_key: Mapped[str] = mapped_column(String(160), nullable=False)
    logical_item_id: Mapped[int | None] = mapped_column(ForeignKey("logical_items.id", ondelete="SET NULL"))
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(220), unique=True, nullable=False)
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    error_code: Mapped[str | None] = mapped_column(String(80))
    last_error: Mapped[str | None] = mapped_column(Text)
    recoverable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PipelineEvent(Base):
    __tablename__ = "pipeline_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("pipeline_tasks.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    logical_item_id: Mapped[int | None] = mapped_column(ForeignKey("logical_items.id", ondelete="SET NULL"))
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("item_snapshots.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    notification_type: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(220), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message_id: Mapped[str | None] = mapped_column(String(180))
    error_code: Mapped[str | None] = mapped_column(String(80))
    last_error: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReviewEntry(Base):
    __tablename__ = "review_queue"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("oa_items.id", ondelete="CASCADE"))
    file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    container_key: Mapped[str | None] = mapped_column(String)
    depth: Mapped[int | None] = mapped_column(Integer)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CleanupRun(Base):
    """一次可审计的数据清理计划及执行汇总。"""

    __tablename__ = "cleanup_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('planning','planned','quarantining','quarantined',"
            "'restoring','restored','purging','purged','failed')",
            name="ck_cleanup_run_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="planning")
    rules_version: Mapped[str] = mapped_column(String(40), nullable=False)
    categories_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    restored_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    restored_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    purged_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    purged_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CleanupItem(Base):
    """不含业务正文的单个清理候选及其隔离状态。"""

    __tablename__ = "cleanup_items"
    __table_args__ = (
        UniqueConstraint("cleanup_run_id", "relative_path", name="uq_cleanup_item_run_path"),
        CheckConstraint(
            "status IN ('planned','quarantined','restored','purged','skipped','failed')",
            name="ck_cleanup_item_status",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_cleanup_item_size"),
        CheckConstraint(
            "relative_path <> '' AND substr(relative_path, 1, 1) <> '/' "
            "AND relative_path <> '..' AND relative_path NOT LIKE '../%' "
            "AND relative_path NOT LIKE '%/../%' AND relative_path NOT LIKE '%/..' "
            "AND relative_path <> '.' AND relative_path NOT LIKE './%' "
            "AND relative_path NOT LIKE '%/./%' AND relative_path NOT LIKE '%/.'",
            name="ck_cleanup_item_relative_path",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    cleanup_run_id: Mapped[int] = mapped_column(
        ForeignKey("cleanup_runs.id", ondelete="CASCADE"), nullable=False,
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    preflight_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="planned")
    quarantine_relpath: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
