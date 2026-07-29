"""Document parser module — converts raw files to Markdown."""

from oa_knowledge.parsers.markitdown_parser import parse_with_markitdown
from oa_knowledge.parsers.mineru_parser import parse_with_mineru, mineru_available
from oa_knowledge.parsers.eligibility import KnowledgeEligibilityDecision, evaluate_eligibility
from oa_knowledge.parsers.quality import assess_quality
from oa_knowledge.parsers.router import parse_file, preflight

__all__ = [
    "parse_file",
    "preflight",
    "parse_with_markitdown",
    "parse_with_mineru",
    "mineru_available",
    "KnowledgeEligibilityDecision",
    "evaluate_eligibility",
    "assess_quality",
]
