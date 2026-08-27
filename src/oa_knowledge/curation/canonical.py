"""Canonical identities and stable Curated output paths."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
import re
import unicodedata

from oa_knowledge.curation.schemas import DocumentDecision


_UNSAFE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")
_SPACE = re.compile(r"\s+")


def sanitize_component(value: str, *, collision_key: str = "", max_length: int = 80) -> str:
    cleaned = unicodedata.normalize("NFKC", value or "").strip()
    cleaned = _UNSAFE.sub("_", cleaned)
    cleaned = _SPACE.sub(" ", cleaned).strip(" ._") or "未识别"
    if len(cleaned) > max_length:
        suffix = (collision_key or hashlib.sha256(cleaned.encode()).hexdigest())[:8]
        cleaned = cleaned[: max_length - 9].rstrip() + "_" + suffix
    return cleaned


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", unicodedata.normalize("NFKC", value).lower())


def canonical_key(decision: DocumentDecision, source_hashes: list[str]) -> str:
    if decision.document_kind == "formal" and decision.issuer and decision.document_number:
        identity = f"{_normalized(decision.issuer)}:{_normalized(decision.document_number)}"
        return "formal:" + hashlib.sha256(identity.encode()).hexdigest()
    identity = "\n".join(sorted(set(source_hashes)))
    return "content:" + hashlib.sha256(identity.encode()).hexdigest()


def publication_relpath(decision: DocumentDecision, *, fallback_date: str, collision_key: str) -> PurePosixPath:
    date = decision.publication_date or fallback_date or "0000-00-00"
    year = date[:4] if len(date) >= 4 and date[:4].isdigit() else "未知年份"
    month = date[:7] if len(date) >= 7 and date[:4].isdigit() else "未知月份"
    title = sanitize_component(decision.normalized_title, collision_key=collision_key)
    root = PurePosixPath("curated", "oa")
    if decision.document_kind == "formal":
        issuer = sanitize_component(decision.issuer, collision_key=collision_key)
        number = sanitize_component(decision.document_number, collision_key=collision_key)
        return root / "正式文件" / issuer / year / sanitize_component(f"{number}_{title}", collision_key=collision_key)
    if decision.document_kind == "internal":
        topic = sanitize_component(decision.topic or "待分类", collision_key=collision_key)
        return root / "公司内部" / topic / month / title
    customer = sanitize_component(decision.customer or "待识别客户", collision_key=collision_key)
    project = sanitize_component(decision.project or "待识别项目", collision_key=collision_key)
    stage = sanitize_component(decision.stage or "待识别阶段", collision_key=collision_key)
    return root / "项目资料" / customer / project / stage / title
