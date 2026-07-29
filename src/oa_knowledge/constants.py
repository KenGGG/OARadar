from enum import StrEnum


class PipelineStatus(StrEnum):
    DISCOVERED = "discovered"
    RAW_SAVED = "raw_saved"
    FILES_VERIFIED = "files_verified"
    PARSE_QUEUED = "parse_queued"
    PARSED = "parsed"
    CLASSIFIED = "classified"
    LINKED = "linked"
    TASKS_EXTRACTED = "tasks_extracted"
    PUBLISHED = "published"
    COMPLETED = "completed"
    COLLECT_FAILED = "collect_failed"
    DOWNLOAD_FAILED = "download_failed"
    PARSE_FAILED = "parse_failed"
    CLASSIFICATION_FAILED = "classification_failed"
    PUBLISH_FAILED = "publish_failed"
    NOTIFY_FAILED = "notify_failed"
    AUTH_REQUIRED = "auth_required"


class BatchStatus(StrEnum):
    PLANNED = "planned"
    DISCOVERING = "discovering"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FileRole(StrEnum):
    METADATA_SNAPSHOT = "metadata_snapshot"
    BODY_SNAPSHOT = "body_snapshot"
    WORKFLOW_SNAPSHOT = "workflow_snapshot"
    DIRECT_ATTACHMENT = "direct_attachment"
    OFFICIAL_BODY = "official_body"
    OFFICIAL_ATTACHMENT = "official_attachment"
    ASSOCIATED_DOCUMENT = "associated_document"
    OPINION_ATTACHMENT = "opinion_attachment"


class DownloadStatus(StrEnum):
    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    VERIFIED = "verified"
    DOWNLOAD_FAILED = "download_failed"
    REJECTED_ZERO_BYTE = "rejected_zero_byte"
    REJECTED_ERROR_PAGE = "rejected_error_page"


class ReviewKind(StrEnum):
    DEPTH_LIMIT_REACHED = "depth_limit_reached"
    INTEGRITY_ERROR = "integrity_error"
    CLASSIFICATION_LOW_CONFIDENCE = "classification_low_confidence"
    ATTACHMENT_SAMPLE_MISSING = "attachment_sample_missing"
    COLLECTION_ISSUE = "collection_issue"
