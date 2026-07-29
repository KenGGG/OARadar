"""Structured extraction from document content using LLM or rules."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


# --- Pydantic schemas for LLM output validation ---


class ExtractedTask(BaseModel):
    """A structured task extracted from an OA document."""
    title: str
    action: str
    responsible_party: str | None = None
    deadline: date | None = None
    deadline_type: str = Field(default="unknown", pattern="^(explicit|calculated|unknown)$")
    evidence_text: str
    evidence_page: int | None = None
    confidence: float = Field(ge=0, le=1)
    source_kind: str = "llm_inferred"
    needs_confirmation: bool = True

    @field_validator("evidence_text")
    @classmethod
    def evidence_not_empty(cls, v: str) -> str:
        if not v or len(v.strip()) < 3:
            raise ValueError("evidence_text must be a meaningful excerpt")
        return v


class ExtractionResult(BaseModel):
    """Structured extraction result from a document."""
    tasks: list[ExtractedTask] = Field(default_factory=list)
    record_type: str = "uncategorized"
    authority_level: str = "unknown"
    business_domains: list[str] = Field(default_factory=list)
    action_type: str = "none"
    document_numbers: list[str] = Field(default_factory=list)
    dates: list[date] = Field(default_factory=list)
    workflow_opinions: list[str] = Field(default_factory=list)
    prompt_version: str = "extractor-v1"
    model_used: str | None = None
    extraction_method: str = "llm"  # "llm" or "rules"


# --- Rule-based extraction fallback ---


@dataclass
class RuleMatch:
    pattern: str
    value: str
    confidence: float


def extract_deadline_rules(text: str) -> list[RuleMatch]:
    """Extract deadline hints from text using regex rules."""
    patterns: list[tuple[str, str, float]] = [
        (r"(\d{4}[年\-]\d{1,2}[月\-]\d{1,2}[日]?)", "explicit_date", 0.9),
        (r"(\d+)\s*个工作日", "business_days", 0.7),
        (r"(\d+)\s*日内?", "days", 0.6),
        (r"(\d{4}[年\-]\d{1,2}[月])", "month", 0.5),
    ]
    results = []
    for pattern, label, conf in patterns:
        for match in re.finditer(pattern, text):
            results.append(RuleMatch(pattern=pattern, value=match.group(), confidence=conf))
    return results


def extract_task_candidates(text: str, title: str = "") -> list[ExtractedTask]:
    """Extract task candidates from text using deterministic rules.

    This is a fallback when LLM is unavailable.
    """
    tasks: list[ExtractedTask] = []
    combined = f"{title}\n{text}"[:2000]

    # Look for action-oriented patterns
    action_patterns = [
        r"请\s*(?:各|有关|相关)?\s*([^\n,，。；;]+?)\s*(?:于|在|于)?([^\n,，。；;]+?)\s*(?:完成|报送|提交|落实|开展|组织|配合|参加|召开|做好)",
        r"([^\n,，。；;]+?)\s*(?:须|应|要|必须|不得|禁止)\s*([^\n,，。；;]+)",
        r"责任单位[：:]\s*([^\n,，。；;\r]+)",
        r"截止时间[：:]\s*([^\n,，。；;\r]+)",
        r"报送时间[：:]\s*([^\n,，。；;\r]+)",
    ]

    for pattern in action_patterns:
        for match in re.finditer(pattern, combined):
            groups = match.groups()
            if len(groups) >= 2:
                responsible = groups[0].strip()
                action = groups[1].strip()
                evidence = match.group(0).strip()[:200]
                tasks.append(ExtractedTask(
                    title=f"{responsible}: {action}",
                    action=action,
                    responsible_party=responsible,
                    deadline_type="unknown",
                    evidence_text=evidence,
                    confidence=0.6,
                    source_kind="rule_extracted",
                    needs_confirmation=True,
                ))
            elif len(groups) == 1:
                tasks.append(ExtractedTask(
                    title=groups[0].strip()[:100],
                    action=groups[0].strip()[:100],
                    evidence_text=match.group(0).strip()[:200],
                    confidence=0.4,
                    source_kind="rule_extracted",
                    needs_confirmation=True,
                ))

    return tasks


def validate_json_response(text: str | None) -> dict | None:
    """Attempt to parse and validate a JSON response, with repair attempts."""
    if not text:
        return None

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON from markdown code blocks
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to repair common JSON issues
    repaired = text.strip()
    repaired = re.sub(r",\s*}", "}", repaired)
    repaired = re.sub(r",\s*]", "]", repaired)
    repaired = repaired.replace("'", '"')
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None
