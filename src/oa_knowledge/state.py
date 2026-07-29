from oa_knowledge.constants import BatchStatus, PipelineStatus


PIPELINE_TRANSITIONS = {
    PipelineStatus.DISCOVERED: {PipelineStatus.RAW_SAVED, PipelineStatus.COLLECT_FAILED, PipelineStatus.AUTH_REQUIRED},
    PipelineStatus.RAW_SAVED: {PipelineStatus.FILES_VERIFIED, PipelineStatus.DOWNLOAD_FAILED},
    PipelineStatus.FILES_VERIFIED: {PipelineStatus.PARSE_QUEUED, PipelineStatus.COMPLETED},
    PipelineStatus.PARSE_QUEUED: {PipelineStatus.PARSED, PipelineStatus.PARSE_FAILED},
    PipelineStatus.PARSED: {PipelineStatus.CLASSIFIED, PipelineStatus.CLASSIFICATION_FAILED},
    PipelineStatus.CLASSIFIED: {PipelineStatus.LINKED},
    PipelineStatus.LINKED: {PipelineStatus.TASKS_EXTRACTED},
    PipelineStatus.TASKS_EXTRACTED: {PipelineStatus.PUBLISHED, PipelineStatus.PUBLISH_FAILED},
    PipelineStatus.PUBLISHED: {PipelineStatus.COMPLETED, PipelineStatus.NOTIFY_FAILED},
}

BATCH_TRANSITIONS = {
    BatchStatus.PLANNED: {BatchStatus.DISCOVERING, BatchStatus.CANCELLED},
    BatchStatus.DISCOVERING: {BatchStatus.READY, BatchStatus.PAUSED, BatchStatus.FAILED},
    BatchStatus.READY: {BatchStatus.RUNNING, BatchStatus.CANCELLED},
    BatchStatus.RUNNING: {BatchStatus.PAUSED, BatchStatus.VALIDATING, BatchStatus.FAILED},
    BatchStatus.PAUSED: {BatchStatus.DISCOVERING, BatchStatus.RUNNING, BatchStatus.CANCELLED},
    BatchStatus.VALIDATING: {BatchStatus.COMPLETED, BatchStatus.FAILED, BatchStatus.PAUSED},
    BatchStatus.FAILED: {BatchStatus.RUNNING, BatchStatus.CANCELLED},
}


def validate_transition(current, target, transitions) -> None:
    if target not in transitions.get(current, set()):
        raise ValueError(f"invalid state transition: {current} -> {target}")


def reconcile_batch(discovered: int, archived: int, skipped: int, unresolved: int, reviewed: int = 0) -> bool:
    if min(discovered, archived, skipped, unresolved, reviewed) < 0:
        raise ValueError("batch counts cannot be negative")
    return discovered == archived + skipped + reviewed + unresolved and unresolved == 0
