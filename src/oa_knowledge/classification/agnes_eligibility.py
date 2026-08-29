"""Local-only safety gate for the narrowly permitted Agnes public channel.

The gate is deliberately conservative: it uses only already-local metadata
and parsed text, never asks a model, and never infers publication status from
an initiator. Any uncertainty stays on the local model path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

AgnesEligibilityStatus = Literal["allowed", "local_only"]

_PUBLIC_AUTHORITY = re.compile(
    r"(?:人民政府|人民政府办公厅|人民政府办公室|管理委员会|管委会|国有资产监督管理委员会|国资委|"
    r"(?:工业和信息化|财政|商务|发展和改革|市场监督管理|人力资源和社会保障|住房和城乡建设|生态环境|"
    r"应急管理|司法|税务|审计|统计)局|(?:省|市|区|县).{0,12}(?:委员会|厅|局|办))"
)
_FORMAL_NUMBER = re.compile(r"[\u4e00-\u9fffA-Za-z]+[〔\[【(（]\d{4}[〕\]】)）]\s*\d+号")
_SENSITIVE = re.compile(
    r"(?:融资租赁|租赁项目|客户|尽调|财务|资金|银行|授信|融资|合同|法律|诉讼|风险报告|"
    r"人事|薪酬|通讯录|预算|考核|经营数据|项目风险|个人信息)"
)


@dataclass(frozen=True, slots=True)
class AgnesEligibilityInput:
    title: str
    content_origin: str | None
    flow_type: str | None
    document_number: str | None
    canonical_issuer: str | None
    workflow: str | None
    attachment_names: tuple[str, ...] = ()
    parsed_text: str = ""


@dataclass(frozen=True, slots=True)
class AgnesEligibility:
    status: AgnesEligibilityStatus
    reason: str


def determine_agnes_eligibility(value: AgnesEligibilityInput) -> AgnesEligibility:
    """Decide egress eligibility entirely from locally available evidence."""
    if value.content_origin != "external":
        return AgnesEligibility("local_only", "non_external_content")

    text = "\n".join(
        part.strip()
        for part in (
            value.title,
            value.document_number or "",
            value.canonical_issuer or "",
            value.workflow or "",
            *value.attachment_names,
            value.parsed_text,
        )
        if part and part.strip()
    )
    if _SENSITIVE.search(text):
        return AgnesEligibility("local_only", "sensitive_signal")

    has_formal_number = bool(
        (value.document_number and _FORMAL_NUMBER.search(value.document_number))
        or _FORMAL_NUMBER.search(text)
    )
    has_public_authority = bool(_PUBLIC_AUTHORITY.search(text))
    is_external_inbound = value.flow_type == "external_inbound"
    is_forwarded = "传阅" in (value.title + (value.workflow or ""))
    if has_formal_number and has_public_authority and (is_external_inbound or is_forwarded):
        return AgnesEligibility("allowed", "external_public_formal_document")
    return AgnesEligibility("local_only", "external_public_not_proven")
