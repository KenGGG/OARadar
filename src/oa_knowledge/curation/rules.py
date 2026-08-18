"""High-precision rules that reduce noise before local model calls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from oa_knowledge.curation.package import PackageSource


RULES_VERSION = "curation-rules-v2"


class SourceDisposition(StrEnum):
    NOISE = "noise"
    CANDIDATE = "candidate"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RuleResult:
    disposition: SourceDisposition
    reason_code: str
    confidence: float


_WORKFLOW_NAMES = re.compile(r"(?:传阅|审批|流程|处理|签批|呈批|阅办|收文)(?:单|记录|意见|信息)?", re.I)
_WORKFLOW_TEXT = re.compile(r"(?:流程节点|处理人|已阅|发起人|部门负责人|办结时间|审批意见)")
_FORMAL_NUMBER = re.compile(r"[\u4e00-\u9fffA-Za-z]{1,12}[〔\[【]\d{4}[〕\]】]\s*\d{1,6}\s*号")
_FORMAL_HEADING = re.compile(r"(?:集团|公司|委员会|办公室|政府|厅|局|部)\s*(?:文件|通知|决定|办法|意见)")


def classify_source(source: PackageSource) -> RuleResult:
    title = source.title.strip()
    text = source.text[:4000]
    if not text.strip():
        return RuleResult(SourceDisposition.NOISE, "empty_markdown", 1.0)
    if _WORKFLOW_NAMES.search(title) and _WORKFLOW_TEXT.search(text) and len(text) < 3000:
        return RuleResult(SourceDisposition.NOISE, "workflow_shell", 0.98)
    if _FORMAL_NUMBER.search(title + "\n" + text) or _FORMAL_HEADING.search(text[:1000]):
        return RuleResult(SourceDisposition.CANDIDATE, "formal_document_signal", 0.95)
    return RuleResult(SourceDisposition.AMBIGUOUS, "semantic_review_required", 0.5)
