"""Shared OA-package semantic classification with safe public routing.

This module contains no database mutation.  Callers decide whether to adopt a
validated result as a new versioned classification decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .agnes_eligibility import AgnesEligibility
from .internal_classification import _extract_qwen_json

_CATEGORIES = (
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
)


class _EvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=4, max_length=500)


class SemanticOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    classification_status: Literal["classified", "needs_review"]
    content_origin: Literal["internal", "external"] | None
    flow_type: Literal[
        "direct", "internal_relay", "external_inbound", "outbound_submission"
    ] | None
    canonical_issuer: str | None = Field(default=None, max_length=300)
    business_category: str | None = None
    document_type: str | None = Field(default=None, max_length=80)
    confidence: float = Field(ge=0, le=1)
    review_current: Literal["keep", "replace", "needs_review", "classify"]
    evidence: list[_EvidencePayload] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=4, max_length=800)

    @model_validator(mode="after")
    def valid_publishable_fields(self) -> SemanticOutcome:
        if self.business_category is not None and self.business_category not in _CATEGORIES:
            raise ValueError("business_category is not an approved category")
        if self.classification_status == "classified":
            if self.content_origin == "internal" and self.business_category is None:
                raise ValueError("classified internal result requires business_category")
            if self.content_origin == "external" and not (self.canonical_issuer or "").strip():
                raise ValueError("classified external result requires canonical_issuer")
            if self.content_origin is None:
                raise ValueError("classified result requires content_origin")
        if self.content_origin != "external" and self.canonical_issuer is not None:
            raise ValueError("canonical_issuer requires external content_origin")
        if self.content_origin != "internal" and self.business_category is not None:
            raise ValueError("business_category requires internal content_origin")
        return self


class _ChatClient(Protocol):
    def chat(self, system_prompt: str, user_prompt: str, *, json_schema: dict) -> dict: ...


@dataclass(frozen=True, slots=True)
class SemanticPackage:
    oa_item_key: str
    title: str
    document_number: str | None
    attachment_names: tuple[str, ...]
    parsed_attachments: tuple[tuple[str, str], ...]
    parse_artifact_hashes: tuple[str, ...]
    current_classification: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class SemanticClassificationResult:
    provider: Literal["agnes", "local_qwen"]
    input_sha256: str
    eligibility_reason: str
    cache_hit: bool
    outcome: SemanticOutcome | None
    rejection_code: str | None
    model: str | None


class JsonSemanticCache:
    """Private, durable output cache; it never stores credentials or prompt text."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def read(self, input_sha256: str) -> dict[str, object] | None:
        path = self._path(input_sha256)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def write(self, input_sha256: str, value: dict[str, object]) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        destination = self._path(input_sha256)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._root, prefix=".tmp-", delete=False
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)

    def _path(self, input_sha256: str) -> Path:
        if len(input_sha256) != 64 or any(char not in "0123456789abcdef" for char in input_sha256):
            raise ValueError("semantic cache key must be a lowercase SHA256")
        return self._root / f"{input_sha256}.json"


class SemanticClassifier:
    """Route one OA package to Agnes only after the local safety gate."""

    def __init__(
        self,
        agnes_client: _ChatClient,
        local_client: _ChatClient,
        cache: JsonSemanticCache,
        *,
        prompt_version: str,
        agnes_model: str = "agnes-2.0-flash",
        local_model: str = "qwen3.5:9b",
    ) -> None:
        self._agnes = agnes_client
        self._local = local_client
        self._cache = cache
        self._prompt_version = prompt_version
        self._agnes_model = agnes_model
        self._local_model = local_model

    def classify(
        self, package: SemanticPackage, eligibility: AgnesEligibility
    ) -> SemanticClassificationResult:
        provider: Literal["agnes", "local_qwen"] = (
            "agnes" if eligibility.status == "allowed" else "local_qwen"
        )
        model = self._agnes_model if provider == "agnes" else self._local_model
        input_sha256 = self._input_sha(package, provider, model)
        cached = self._cache.read(input_sha256)
        if cached is not None:
            outcome = self._cached_outcome(cached)
            if outcome is not None:
                return SemanticClassificationResult(
                    provider, input_sha256, eligibility.reason, True, outcome, None,
                    str(cached.get("model") or model),
                )
        public = provider == "agnes"
        client = self._agnes if public else self._local
        response: dict = {}
        outcome: SemanticOutcome | None = None
        # A schema-invalid response is safe to retry once with exactly the
        # same payload.  Agnes receives only its locally approved public
        # payload; Qwen remains local-only.  Transport failures are handled by
        # the provider clients' bounded retry policies instead.
        for attempt in range(2):
            response = client.chat(
                _system_prompt(),
                _user_prompt(package, public=public),
                json_schema=SemanticOutcome.model_json_schema(),
            )
            if response.get("error"):
                return SemanticClassificationResult(
                    provider, input_sha256, eligibility.reason, False, None,
                    "model_request_failed", str(response.get("model") or model),
                )
            raw = response.get("content")
            payload = _extract_qwen_json(raw) if isinstance(raw, str) else None
            if payload is not None:
                try:
                    outcome = SemanticOutcome.model_validate_json(payload)
                except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
                    outcome = None
            if outcome is not None:
                break
            if attempt == 1:
                return SemanticClassificationResult(
                    provider, input_sha256, eligibility.reason, False, None,
                    "schema_invalid", str(response.get("model") or model),
                )
        if outcome is None:
            raise AssertionError("semantic retry loop must return or validate an outcome")
        self._cache.write(
            input_sha256,
            {
                "provider": provider,
                "model": str(response.get("model") or model),
                "prompt_version": self._prompt_version,
                "input_sha256": input_sha256,
                "eligibility_reason": eligibility.reason,
                "outcome": outcome.model_dump(mode="json"),
            },
        )
        return SemanticClassificationResult(
            provider, input_sha256, eligibility.reason, False, outcome, None,
            str(response.get("model") or model),
        )

    def _input_sha(
        self, package: SemanticPackage, provider: str, model: str
    ) -> str:
        value = {
            "oa_item_key": package.oa_item_key,
            "parse_artifact_hashes": package.parse_artifact_hashes,
            "prompt_version": self._prompt_version,
            "provider": provider,
            "model": model,
        }
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _cached_outcome(value: dict[str, object]) -> SemanticOutcome | None:
        candidate = value.get("outcome")
        try:
            return SemanticOutcome.model_validate(candidate)
        except ValidationError:
            return None


def _system_prompt() -> str:
    categories = "、".join(_CATEGORIES)
    return (
        "你是 OARadar 的严格 OA 语义分类器。标题只是辅助证据，正文、发文机关、文号和落款优先。"
        "发起人不等于作者；引用机构不等于当前文件 issuer；不确定时返回 needs_review。"
        "公司自行形成的对外报送材料是 internal + outbound_submission。"
        f"内部业务分类只能是：{categories}。"
        "只输出一个 JSON 对象，禁止思维链、Markdown、<think> 或额外键。"
        "必须且只能包含这些键：classification_status, content_origin, flow_type, "
        "canonical_issuer, business_category, document_type, confidence, review_current, evidence, reason。"
        "classification_status 只能是 classified 或 needs_review；content_origin 只能是 internal、external 或 null；"
        "flow_type 只能是 direct、internal_relay、external_inbound、outbound_submission 或 null。"
        "review_current 必须是字符串 keep、replace、needs_review 或 classify，绝不是 true/false。"
        "classified + internal 必须有 business_category 且 canonical_issuer 为 null；"
        "classified + external 必须有 canonical_issuer 且 business_category 为 null。"
        "needs_review 不猜测：可将无法确认字段设为 null。"
        "evidence 必须是数组，元素仅含 source、type、summary。"
    )


def _user_prompt(package: SemanticPackage, *, public: bool) -> str:
    attachment_blocks = "\n\n".join(
        f"===== {name} =====\n{_select_text(body)}"
        for name, body in package.parsed_attachments
    ) or "（没有可用正文）"
    rows = [
        "请判断以下 OA Package。",
        f"OA 标题：{package.title[:1000]}",
        f"正式文号：{package.document_number or '未知'}",
        "附件名称：" + "；".join(name[:300] for name in package.attachment_names),
    ]
    if not public and package.current_classification is not None:
        rows.append("当前分类：" + json.dumps(package.current_classification, ensure_ascii=False))
    rows.extend(("Parsed content：", attachment_blocks))
    return "\n\n".join(rows)


def _select_text(value: str, *, maximum: int = 24_000) -> str:
    """Keep deterministic start/end context and preserve attachment boundaries."""
    text = value.strip()
    if len(text) <= maximum:
        return text
    head = text[: maximum * 2 // 3]
    tail = text[-maximum // 3 :]
    return f"{head}\n\n[...正文中段省略... ]\n\n{tail}"
