"""Deterministic admission gate for local knowledge parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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
    with path.open("rb") as stream:
        signature = stream.read(8)
    if signature.startswith(b"%PDF-"):
        suffix = ".pdf"
        evidence["detected_by"] = "file_signature"
    detected = suffix.lstrip(".") or "unknown"
    if path.name.lower() in TECHNICAL_NAMES:
        return KnowledgeEligibilityDecision(False, "OA_TECHNICAL_METADATA", detected, "source_evidence_only", evidence)
    if suffix in {".html", ".htm"} and any(marker in path.stem.lower() for marker in FRAME_MARKERS):
        return KnowledgeEligibilityDecision(False, "BUTTON_OR_MASK_FRAME", detected, "source_evidence_only", evidence)
    if suffix not in SUPPORTED_EXTENSIONS:
        return KnowledgeEligibilityDecision(False, "UNSUPPORTED_FORMAT", detected, "review", evidence)
    if duplicate_content:
        evidence["preserve_source_reference"] = True
        return KnowledgeEligibilityDecision(False, "DUPLICATE_CONTENT", detected, "reuse_content_object", evidence)
    route = "mineru" if suffix in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"} else "markitdown"
    return KnowledgeEligibilityDecision(True, "KNOWLEDGE_ELIGIBLE", detected, route, evidence)
