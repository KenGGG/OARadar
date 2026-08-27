"""Focused internal OA business classification from local attachment text."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

BusinessCategory = Literal[
    "01_公司治理与决策",
    "02_业务项目与投放租后",
    "03_风险合规审计法务",
    "04_财务资金与融资",
    "05_经营计划与绩效考核",
    "06_人力资源",
    "07_党建纪检与工会",
    "08_行政采购与信息化",
    "09_对外报送与监管反馈",
    "99_其他内部",
]

_CATEGORY_PATTERNS: tuple[tuple[BusinessCategory, tuple[str, ...]], ...] = (
    ("01_公司治理与决策", (r"董事会", r"股东(?:会|大会)", r"监事会", r"公司治理", r"公司章程")),
    ("02_业务项目与投放租后", (r"融资租赁", r"租赁项目", r"项目(?:立项|评审|投放|合同)", r"投放条件", r"租后管理")),
    ("03_风险合规审计法务", (r"风险(?:专题|管理|审查)?", r"合规", r"审计", r"法务", r"诉讼")),
    ("04_财务资金与融资", (r"银行授信", r"授信(?:额度|材料|申请)", r"融资材料", r"资金管理", r"财务", r"银行账户")),
    ("05_经营计划与绩效考核", (r"经营计划", r"绩效(?:考核|评价)", r"年度考核", r"经营分析")),
    ("06_人力资源", (r"人力资源", r"人员任免", r"招聘", r"薪酬", r"员工培训")),
    ("07_党建纪检与工会", (r"党建", r"党支部", r"党委", r"纪检", r"纪委", r"工会")),
    ("08_行政采购与信息化", (r"行政管理", r"办公用品", r"采购", r"信息化", r"信息系统", r"软件服务")),
    ("09_对外报送与监管反馈", (r"监管(?:报送|反馈)", r"对外报送", r"统计报送", r"监管机构")),
)

_DOCUMENT_TYPES: tuple[tuple[str, str], ...] = (
    (r"立项申请书?", "立项申请书"),
    (r"会议纪要", "会议纪要"),
    (r"(?:印鉴使用申请|用印申请|合同用印|用印)", "印鉴使用申请"),
)


@dataclass(frozen=True, slots=True)
class InternalClassification:
    business_category: BusinessCategory
    document_type: str | None
    confidence: float
    decision_source: Literal["content_rule", "local_qwen"]
    evidence: str


def extract_document_type(title: str) -> str | None:
    for pattern, document_type in _DOCUMENT_TYPES:
        if re.search(pattern, title):
            return document_type
    return None


def classify_by_content(
    title: str, bodies: tuple[str, ...]
) -> InternalClassification | None:
    text = "\n".join((title, *bodies))
    matches: list[tuple[int, BusinessCategory, str]] = []
    for category, patterns in _CATEGORY_PATTERNS:
        hits = [match.group(0) for pattern in patterns if (match := re.search(pattern, text))]
        if hits:
            matches.append((len(hits), category, hits[0]))
    if not matches:
        return None
    matches.sort(key=lambda row: (-row[0], row[1]))
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        return None
    score, category, evidence = matches[0]
    return InternalClassification(
        business_category=category,
        document_type=extract_document_type(title),
        confidence=min(0.96, 0.84 + score * 0.04),
        decision_source="content_rule",
        evidence=evidence,
    )


class _QwenPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    business_category: BusinessCategory
    document_type: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence_quote: str = Field(min_length=8, max_length=500)
    outside_existing_categories: bool
    reason: str = Field(min_length=8, max_length=500)


class _ChatClient(Protocol):
    def chat(
        self, system_prompt: str, user_prompt: str, *, json_schema: dict
    ) -> dict: ...


class LocalQwenInternalClassifier:
    """One bounded, localhost-backed structured classification request."""

    def __init__(self, client: _ChatClient, *, confidence_threshold: float = 0.75):
        self._client = client
        self._threshold = confidence_threshold

    def classify(
        self,
        title: str,
        bodies: tuple[str, ...],
        document_type: str | None,
    ) -> InternalClassification | None:
        content = "\n\n".join(body.strip() for body in bodies if body.strip())[:12_000]
        if not content:
            return None
        categories = "\n".join(category for category, _ in _CATEGORY_PATTERNS)
        response = self._client.chat(
            (
                "你只对本机 OA 内部事项进行业务分类。document_type 与 business_category 必须分开。"
                "只有内容明确不属于 01—09 时才可选择 99，并将 outside_existing_categories 设为 true。"
                "不得猜测，返回符合 JSON schema 的单个对象。"
            ),
            (
                f"可选业务分类：\n{categories}\n99_其他内部\n\n"
                f"标题：{title[:500]}\n已识别文种：{document_type or '未知'}\n"
                f"附件内容：\n{content}"
            ),
            json_schema=_QwenPayload.model_json_schema(),
        )
        if response.get("error") or not response.get("content"):
            return None
        try:
            payload = _QwenPayload.model_validate_json(response["content"])
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if payload.confidence < self._threshold:
            return None
        if (
            payload.business_category == "99_其他内部"
            and not payload.outside_existing_categories
        ):
            return None
        return InternalClassification(
            business_category=payload.business_category,
            document_type=payload.document_type or document_type,
            confidence=payload.confidence,
            decision_source="local_qwen",
            evidence=payload.evidence_quote,
        )
