"""Rule-based classification for OA documents."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Classification:
    """A single classification result with evidence and confidence."""
    facet: str
    value: str
    source: str  # "rule" or "llm"
    confidence: float
    evidence: str = ""
    rule_or_prompt_version: str = "v1"


# Patterns for record_type detection
_RECORD_TYPE_RULES: list[tuple[str, str, str]] = [
    # (keyword_pattern, record_type_value, description)
    (r"红头文件|红头", "official_document", "red-header official document"),
    (r"制度|规定|办法|细则|规程", "policy", "institutional policy/regulation"),
    (r"通知", "notice", "notification"),
    (r"决定", "decision", "decision"),
    (r"通报", "bulletin", "circulated bulletin"),
    (r"请示", "inquiry", "request for instructions"),
    (r"批复|回复", "reply", "official reply"),
    (r"报告", "report", "report"),
    (r"函", "letter", "official letter"),
    (r"纪要|会议纪要", "minutes", "meeting minutes"),
    (r"方案|计划", "plan", "plan/scheme"),
    (r"意见", "opinion", "opinion/guidance"),
    (r"公告", "announcement", "public announcement"),
    (r"任命|聘任", "appointment", "personnel appointment"),
    (r"预算|决算", "budget", "budget document"),
    (r"合同|协议", "contract", "contract/agreement"),
]

# Authority level patterns
_AUTHORITY_RULES: list[tuple[str, str]] = [
    (r"集团\s*(公司|有限)", "company_group"),
    (r"总公司", "company_headquarters"),
    (r"财务部|财务管理部", "company_department"),
    (r"人力资.*部|办公室|综合部", "company_department"),
    (r"管委会|开发区", "government_bureau"),
    (r"国务院|省政府|市政府", "government_level"),
]

# Validity status patterns
_VALIDITY_RULES: list[tuple[str, str]] = [
    (r"废止|取消|失效|终止", "repealed"),
    (r"修订|修改|补充|替换", "amended"),
    (r"试行|暂行|试验", "trial"),
    (r"有效|现行|生效|实施", "effective"),
]

# Confidentiality patterns
_CONFIDENTIALITY_RULES: list[tuple[str, str]] = [
    (r"绝密", "top_secret"),
    (r"机密", "confidential"),
    (r"秘密", "secret"),
    (r"内部|内参", "internal"),
    (r"公开", "public"),
]


def _match_rules(text: str, rules: list[tuple[str, str]], default: str = "uncategorized") -> tuple[str, str]:
    """Match text against a list of (pattern, value) rules. Returns (value, evidence)."""
    for rule_entry in rules:
        if len(rule_entry) < 2:
            continue
        pattern = rule_entry[0]
        value = rule_entry[1]
        if re.search(pattern, text):
            return value, f"matched_rule:{pattern}"
    return default, "no_match"


def classify_item(
    title: str,
    content: str = "",
    issuer: str | None = None,
    document_number: str | None = None,
) -> list[Classification]:
    """Apply rule-based classification to an OA item.

    Returns a list of Classification objects for each facet.
    """
    combined = f"{title} {content}".strip()
    classifications: list[Classification] = []

    # Record type
    rt_value, rt_evidence = _match_rules(combined, _RECORD_TYPE_RULES)
    classifications.append(Classification(
        facet="record_type",
        value=rt_value,
        source="rule",
        confidence=0.9 if rt_value != "uncategorized" else 0.3,
        evidence=rt_evidence,
    ))

    # Authority level
    issuer_text = issuer or ""
    al_value, al_evidence = _match_rules(issuer_text, _AUTHORITY_RULES, "unknown")
    classifications.append(Classification(
        facet="authority_level",
        value=al_value,
        source="rule",
        confidence=0.85 if al_value != "unknown" else 0.2,
        evidence=al_evidence,
    ))

    # Validity status
    vs_value, vs_evidence = _match_rules(combined, _VALIDITY_RULES, "effective")
    classifications.append(Classification(
        facet="validity_status",
        value=vs_value,
        source="rule",
        confidence=0.8 if vs_value != "effective" else 0.5,
        evidence=vs_evidence,
    ))

    # Confidentiality
    conf_value, conf_evidence = _match_rules(combined, _CONFIDENTIALITY_RULES, "internal")
    classifications.append(Classification(
        facet="confidentiality",
        value=conf_value,
        source="rule",
        confidence=0.85 if conf_value != "internal" else 0.3,
        evidence=conf_evidence,
    ))

    return classifications
