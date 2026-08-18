"""Rule-first, context-bounded package classification."""

from __future__ import annotations

import json
import re

from oa_knowledge.curation.package import OAPackage
from oa_knowledge.curation.rules import SourceDisposition, classify_source
from oa_knowledge.curation.schemas import CurationDecision, ModelCurationResponse, SourceSemanticMap
from oa_knowledge.enrich.context_budget import chunk_text, estimate_tokens
from oa_knowledge.enrich.extractor import validate_json_response


PROMPT_VERSION = "curation-prompt-v2"

_FORMAL_NUMBER = re.compile(r"[\u4e00-\u9fffA-Za-z]{1,12}[〔\[【]\d{4}[〕\]】]\s*\d{1,6}\s*号")
_ISSUER_LINE = re.compile(
    r"^\s*(.{2,80}?(?:集团|公司|委员会|办公室|政府|厅|局|部))(?:\s*文件)?\s*$"
)


class CurationClassificationError(RuntimeError):
    pass


def _review(reason: str) -> CurationDecision:
    return CurationDecision(needs_review=True, reason_code=reason)


def _formal_metadata_from_sources(sources) -> tuple[str, str]:
    number = ""
    issuer = ""
    for source in sources:
        searchable = f"{source.title}\n{source.text[:4000]}"
        if not number and (match := _FORMAL_NUMBER.search(searchable)):
            number = match.group(0).strip()
        if not issuer:
            for line in source.text[:2000].splitlines():
                if match := _ISSUER_LINE.match(line):
                    issuer = match.group(1).strip()
                    break
        if issuer and number:
            break
    return issuer, number


def classify_package(package: OAPackage, client, *, max_input_tokens: int, confidence_threshold: float = 0.75) -> CurationDecision:
    if not package.completable:
        return _review("depth_limit_reached")
    routed = [(source, classify_source(source)) for source in package.ordered_sources]
    candidates = [(source, rule) for source, rule in routed if rule.disposition != SourceDisposition.NOISE]
    if not candidates:
        return CurationDecision(documents=[], needs_review=False, reason_code="rules_no_documents")
    if max_input_tokens < 256:
        return _review("context_budget_too_small")

    alias_for = {source.source_key: f"S{index}" for index, (source, _rule) in enumerate(candidates, 1)}
    actual_for = {alias: actual for actual, alias in alias_for.items()}
    candidate_keys = set(alias_for)
    candidate_by_key = {source.source_key: source for source, _rule in candidates}
    evidence_parts: list[str] = []
    per_source = max(128, max_input_tokens // max(1, len(candidates)))
    for source, rule in candidates:
        model_key = alias_for[source.source_key]
        prefix = f"[source_key={model_key} title={source.title} rule={rule.reason_code}]\n"
        remaining = max(64, per_source - estimate_tokens(prefix))
        pieces = chunk_text(source.text, max_tokens=remaining)
        if len(pieces) == 1:
            evidence_parts.append(prefix + pieces[0])
            continue
        map_schema = SourceSemanticMap.model_json_schema()
        map_system = (
            "你是本地OA来源分块分析器。只概括当前来源分块，保留文号、机构、标题和正文/附件关系信号；"
            "不得补充原文没有的事实。严格输出JSON。\n" + json.dumps(map_schema, ensure_ascii=False)
        )

        def map_parts(parts: list[str]) -> list[str] | None:
            mapped: list[str] = []
            for piece in parts:
                user = f"source_key={model_key}\n来源分块：\n{piece}"
                if estimate_tokens(user) > max_input_tokens:
                    return None
                response = client.chat(map_system, user, json_schema=map_schema)
                if response.get("error"):
                    raise CurationClassificationError(str(response.get("error")))
                try:
                    value = SourceSemanticMap.model_validate(validate_json_response(response.get("content")))
                except Exception:
                    return None
                # The source identity is supplied by the deterministic caller,
                # not inferred by the model. Bind it back even if the model
                # echoes a title or a malformed durable database key.
                mapped.append(value.model_copy(update={"source_key": model_key}).model_dump_json())
            return mapped

        mapped = map_parts(pieces)
        if mapped is None:
            return _review("map_schema_invalid")
        combined = "\n".join(mapped)
        while estimate_tokens(combined) > remaining:
            groups = chunk_text(combined, max_tokens=remaining)
            reduced = map_parts(groups)
            if reduced is None:
                return _review("map_reduce_failed")
            next_combined = "\n".join(reduced)
            if len(next_combined) >= len(combined):
                return _review("map_reduce_did_not_converge")
            combined = next_combined
        evidence_parts.append(prefix + combined)
    evidence = "\n\n".join(evidence_parts)
    if estimate_tokens(evidence) > max_input_tokens:
        evidence = chunk_text(evidence, max_tokens=max_input_tokens)[0]

    schema = ModelCurationResponse.model_json_schema()
    system = (
        "你是本机OA知识编目器。只能依据输入识别0到N份文件。严格输出JSON Schema；不得改写正文，"
        f"source_key只能从{','.join(actual_for)}中选择；不确定时documents返回空数组。\n" +
        json.dumps(schema, ensure_ascii=False)
    )
    result = client.chat(system, f"OA标题：{package.title}\n\n候选来源：\n{evidence}", json_schema=schema)
    if result.get("error"):
        raise CurationClassificationError(str(result.get("error")))
    parsed = validate_json_response(result.get("content"))
    try:
        response = ModelCurationResponse.model_validate(parsed)
    except Exception:
        return _review("schema_invalid")

    restored_documents = []
    for document in response.documents:
        model_keys = {source.source_key for source in document.sources} | set(document.evidence_source_keys)
        if any(key not in actual_for and key not in candidate_keys for key in model_keys):
            return _review("unknown_source_key")
        restored_sources = [
            source.model_copy(update={"source_key": actual_for.get(source.source_key, source.source_key)})
            for source in document.sources
        ]
        restored_evidence = [actual_for.get(key, key) for key in document.evidence_source_keys]
        document = document.model_copy(update={
            "sources": restored_sources,
            "evidence_source_keys": restored_evidence,
        })
        if document.confidence < confidence_threshold:
            return _review("low_confidence")
        if document.document_kind == "formal":
            relevant_sources = [
                candidate_by_key[source.source_key]
                for source in restored_sources
                if source.source_key in candidate_by_key
            ]
            source_issuer, source_number = _formal_metadata_from_sources(relevant_sources)
            document = document.model_copy(update={
                "issuer": document.issuer or source_issuer,
                "document_number": document.document_number or source_number,
            })
            if not document.issuer or not document.document_number:
                return _review("formal_metadata_incomplete")
        restored_documents.append(document)
    return CurationDecision(documents=restored_documents)
