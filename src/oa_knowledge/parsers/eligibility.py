"""Deterministic admission gate for local knowledge parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from oa_knowledge.parsers.format_router import detect_format, parser_attempts

SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".html", ".htm", ".txt", ".md", ".csv", ".json", ".xml",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff",
}
TECHNICAL_NAMES = {"metadata.json", "workflow.json", "manifest.json", "quality.json"}
FRAME_MARKERS = ("button", "mask", "toolbar", "page-frame", "frame_snapshot")


@dataclass(frozen=True)
class KnowledgeEligibilityDecision:
    eligible: bool
    reason_code: str
    detected_type: str
    routing_hint: str
    evidence: dict[str, object] = field(default_factory=dict)


def evaluate_eligibility(file_path: Path, *, duplicate_content: bool = False) -> KnowledgeEligibilityDecision:
    path = Path(file_path)
    suffix = path.suffix.lower()
    evidence: dict[str, object] = {"filename": path.name, "size_bytes": path.stat().st_size if path.exists() else 0}
    if not path.is_file() or evidence["size_bytes"] == 0:
        return KnowledgeEligibilityDecision(False, "EMPTY_FILE", suffix.lstrip(".") or "unknown", "reject", evidence)
    decision = detect_format(path)
    detected = decision.actual_file_type
    evidence["detected_by"] = decision.detection_source
    evidence["filename_normalized"] = decision.filename_normalized
    if path.name.lower() in TECHNICAL_NAMES:
        return KnowledgeEligibilityDecision(False, "OA_TECHNICAL_METADATA", detected, "source_evidence_only", evidence)
    if suffix in {".html", ".htm"} and any(marker in path.stem.lower() for marker in FRAME_MARKERS):
        return KnowledgeEligibilityDecision(False, "BUTTON_OR_MASK_FRAME", detected, "source_evidence_only", evidence)
    if decision.status_code != "parseable":
        reason = "ARCHIVE_CONTAINER_UNSUPPORTED" if decision.status_code == "archive_container_unsupported" else "UNSUPPORTED_FORMAT"
        return KnowledgeEligibilityDecision(False, reason, detected, "review", evidence)
    if duplicate_content:
        evidence["preserve_source_reference"] = True
        return KnowledgeEligibilityDecision(False, "DUPLICATE_CONTENT", detected, "reuse_content_object", evidence)
    attempts = parser_attempts(decision, mineru_enabled=True)
    route = attempts[0] if attempts else "markitdown"
    return KnowledgeEligibilityDecision(True, "KNOWLEDGE_ELIGIBLE", detected, route, evidence)
