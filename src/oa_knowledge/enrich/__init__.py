"""Enrichment package — classification and structured extraction."""

from oa_knowledge.enrich.extractor import (
    ExtractionResult,
    ExtractedTask,
    extract_task_candidates,
    validate_json_response,
)
from oa_knowledge.enrich.llm_client import LlmClient
from oa_knowledge.enrich.rules import Classification, classify_item

__all__ = [
    "Classification",
    "classify_item",
    "LlmClient",
    "ExtractionResult",
    "ExtractedTask",
    "extract_task_candidates",
    "validate_json_response",
]
