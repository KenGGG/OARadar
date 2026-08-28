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
    ("06_人力资源", (r"人力资源", r"人员任免", r"招聘", r"薪酬", r"员工培训", r"岗位", r"人事", r"员工")),
    ("07_党建纪检与工会", (r"党建", r"党支部", r"党委", r"纪检", r"纪委", r"工会")),
    ("08_行政采购与信息化", (r"行政管理", r"办公用品", r"采购", r"信息化", r"信息系统", r"软件服务", r"营业执照(?:原件)?(?:外借|借用)?")),
    ("09_对外报送与监管反馈", (r"监管(?:报送|反馈)", r"对外报送", r"统计报送", r"监管机构")),
)

_TITLE_SUBJECT_RULES: tuple[tuple[BusinessCategory, tuple[str, ...]], ...] = (
    ("01_公司治理与决策", (r"董事会", r"股东(?:会|大会)", r"监事会", r"公司治理", r"公司章程")),
    ("09_对外报送与监管反馈", (r"(?:报|报送|反馈|回复).*(?:金服集团|集团|监管|政府|主管部门)", r"(?:金服集团|监管|政府|主管部门).*(?:报送|反馈|回复)", r"(?:监管|对外|统计)报送")),
    ("04_财务资金与融资", (r"银行授信", r"授信(?:额度|材料|申请)", r"融资材料", r"资金划转", r"银行账户", r"(?:电子银行|网银)", r"银行.*(?:协议|保密)")),
    ("05_经营计划与绩效考核", (r"(?:重点)?督办(?:项目|事项)?", r"重点任务跟踪")),
    ("02_业务项目与投放租后", (
        r"租后(?:检查|报告|管理)", r"资产检查", r"资产分类", r"项目(?:立项|预审|评审|投放|合同)",
        r"(?:直租|直接租赁)", r"售后回租", r"联合承租", r"业务方案变更", r"融资租赁", r"租赁项目",
    )),
    ("03_风险合规审计法务", (r"风险(?:专题|管理|审查)?", r"合规", r"审计", r"法务", r"诉讼")),
    ("06_人力资源", (r"人力资源", r"人员任免", r"招聘", r"薪酬", r"员工培训", r"岗位", r"人事", r"员工")),
    ("07_党建纪检与工会", (r"党建", r"党支部", r"党委", r"纪检", r"纪委", r"工会")),
    ("08_行政采购与信息化", (r"行政管理", r"办公用品", r"采购", r"信息化", r"信息系统", r"软件服务", r"营业执照(?:原件)?(?:外借|借用)?")),
    ("05_经营计划与绩效考核", (r"经营计划", r"绩效(?:考核|评价)", r"年度考核", r"经营分析", r"工作简报")),
)

_BUSINESS_BRIEF = re.compile(r"业务[一二三四五六七八九十\d]+部.*工作简报")
_BUSINESS_MEETING = re.compile(r"业务(?:条线|[一二三四五六七八九十\d]+部)?.*工作会议纪要")

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
    body = "\n".join(bodies)
    if _BUSINESS_MEETING.search(title) and not re.search(r"(?:项目|租赁).*(?:评审|专题|方案)|(?:项目|租赁)(?:评审|专题|方案)", title):
        return _content_rule("05_经营计划与绩效考核", "业务工作会议纪要", title)
    if _BUSINESS_BRIEF.search(title):
        body_match = _subject_match(body, exclude={"05_经营计划与绩效考核"})
        if body_match is None:
            return _content_rule("05_经营计划与绩效考核", "工作简报", title)
        category, evidence = body_match
        return _content_rule(category, evidence, title)
    title_match = _subject_match(title)
    if title_match is not None:
        category, evidence = title_match
        return _content_rule(category, evidence, title)
    body_match = _subject_match(body)
    if body_match is None:
        return None
    category, evidence = body_match
    return _content_rule(category, evidence, title)


def _subject_match(
    text: str, *, exclude: set[BusinessCategory] | None = None
) -> tuple[BusinessCategory, str] | None:
    for category, patterns in _TITLE_SUBJECT_RULES:
        if exclude and category in exclude:
            continue
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return category, match.group(0)
    return None


def _content_rule(
    category: BusinessCategory, evidence: str, title: str
) -> InternalClassification:
    return InternalClassification(
        business_category=category,
        document_type=extract_document_type(title),
        confidence=0.92,
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
        self.last_rejection_code: str | None = None

    def classify(
        self,
        title: str,
        bodies: tuple[str, ...],
        document_type: str | None,
    ) -> InternalClassification | None:
        self.last_rejection_code = None
        content = "\n\n".join(body.strip() for body in bodies if body.strip())[:12_000]
        if not content:
            self.last_rejection_code = "empty_content"
            return None
        categories = "\n".join(category for category, _ in _CATEGORY_PATTERNS)
        response = self._client.chat(
            (
                "你是严格 JSON 分类器。只输出一个 JSON 对象，禁止输出其他键、解释、Markdown、<think>。"
                "必须包含且只包含以下键：business_category, document_type, confidence, evidence_quote, "
                "outside_existing_categories, reason。document_type 与 business_category 必须分开。"
                "business_category 必须逐字等于可选分类之一，不得使用简称如“02”。"
                "只有内容明确不属于 01—09 时才可选择 99，并将 outside_existing_categories 设为 true。"
                "合法示例：{\"business_category\":\"02_业务项目与投放租后\",\"document_type\":\"立项申请书\","
                "\"confidence\":0.91,\"evidence_quote\":\"融资租赁项目立项评审及投放条件\","
                "\"outside_existing_categories\":false,\"reason\":\"项目立项评审属于业务项目与投放租后。\"}"
            ),
            (
                f"可选业务分类：\n{categories}\n99_其他内部\n\n"
                f"标题：{title[:500]}\n已识别文种：{document_type or '未知'}\n"
                f"附件内容：\n{content}"
            ),
            json_schema=_QwenPayload.model_json_schema(),
        )
        if response.get("error"):
            self.last_rejection_code = str(response.get("reason_code") or "model_error")
            return None
        raw_content = response.get("content")
        if not isinstance(raw_content, str) or not raw_content.strip():
            self.last_rejection_code = "empty_response"
            return None
        payload_text = _extract_qwen_json(raw_content)
        if payload_text is None:
            self.last_rejection_code = "json_missing"
            return None
        try:
            payload = _QwenPayload.model_validate_json(payload_text)
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
            self.last_rejection_code = "schema_invalid"
            return None
        if payload.confidence < self._threshold:
            self.last_rejection_code = "confidence_below_threshold"
            return None
        if (
            payload.business_category == "99_其他内部"
            and not payload.outside_existing_categories
        ):
            self.last_rejection_code = "other_without_explicit_evidence"
            return None
        return InternalClassification(
            business_category=payload.business_category,
            document_type=payload.document_type or document_type,
            confidence=payload.confidence,
            decision_source="local_qwen",
            evidence=payload.evidence_quote,
        )


class LocalQwenIssuerExtractor:
    """Bounded local-model fallback that extracts only a sending organization."""

    def __init__(self, client: _ChatClient, *, confidence_threshold: float = 0.75):
        self._client = client
        self._threshold = confidence_threshold
        self.last_rejection_code: str | None = None

    def extract(self, title: str, bodies: tuple[str, ...]) -> tuple[str, str] | None:
        self.last_rejection_code = None
        content = "\n\n".join(bodies)[:12_000]
        response = self._client.chat(
            "只提取真实发文单位。只输出 JSON，禁止判断内外部、业务分类或任何其他内容。",
            f"标题：{title[:500]}\n正文：{content}",
            json_schema={"type":"object","additionalProperties":False,"required":["issuer","confidence","evidence_quote"],"properties":{"issuer":{"type":"string","minLength":2},"confidence":{"type":"number"},"evidence_quote":{"type":"string","minLength":2}}},
        )
        raw = response.get("content") if not response.get("error") else None
        text = _extract_qwen_json(raw) if isinstance(raw, str) else None
        if text is None:
            self.last_rejection_code = "json_missing"; return None
        try:
            value = json.loads(text); issuer = value["issuer"].strip(); confidence = float(value["confidence"]); evidence = value["evidence_quote"].strip()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.last_rejection_code = "schema_invalid"; return None
        if not issuer or confidence < self._threshold or not evidence:
            self.last_rejection_code = "confidence_below_threshold"; return None
        return issuer, evidence


def _extract_qwen_json(raw_content: str) -> str | None:
    """Accept one schema object, discarding only a leading Qwen reasoning block."""
    text = raw_content.strip()
    text = re.sub(r"^<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
        if match is None:
            return None
        text = match.group(1)
    try:
        _payload, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None
    if text[end:].strip():
        return None
    return text[:end]
