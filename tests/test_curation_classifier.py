import json

from oa_knowledge.curation.classifier import classify_package
from oa_knowledge.curation.package import OAPackage, PackageSource
from oa_knowledge.enrich.context_budget import estimate_tokens


class FakeClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str, **_kwargs) -> dict:
        self.calls.append((system, user))
        return {"content": json.dumps(self.payload, ensure_ascii=False), "error": None}


def package() -> OAPackage:
    return OAPackage(package_key="oa:synthetic", title="转发通知", completed_at="2026-08-15", sources=(
        PackageSource(source_key="file:1", title="示例发〔2026〕1号.md", markdown_relpath="parse/1.md", content_sha256="a" * 64, markdown_sha256="b" * 64, text="示例集团文件\n示例发〔2026〕1号\n关于测试工作的通知"),
    ))


def test_classifier_accepts_zero_to_many_strict_decisions() -> None:
    client = FakeClient({"documents": [{
        "document_kind": "formal", "normalized_title": "关于测试工作的通知", "issuer": "示例集团",
        "document_number": "示例发〔2026〕1号", "publication_date": "2026-08-01", "topic": "",
        "customer": "", "project": "", "stage": "", "confidence": 0.95,
        "sources": [{"source_key": "file:1", "role": "body"}], "evidence_source_keys": ["file:1"],
    }]})
    result = classify_package(package(), client, max_input_tokens=2000)
    assert len(result.documents) == 1
    assert result.documents[0].document_kind == "formal"


def test_classifier_uses_short_model_aliases_and_restores_durable_source_keys() -> None:
    client = FakeClient({"documents": [{
        "document_kind": "formal", "normalized_title": "关于测试工作的通知", "issuer": "示例集团",
        "document_number": "示例发〔2026〕1号", "publication_date": "2026-08-01", "topic": "",
        "customer": "", "project": "", "stage": "", "confidence": 0.95,
        "sources": [{"source_key": "S1", "role": "body"}], "evidence_source_keys": ["S1"],
    }]})

    result = classify_package(package(), client, max_input_tokens=2000)

    assert result.documents[0].sources[0].source_key == "file:1"
    assert result.documents[0].evidence_source_keys == ["file:1"]
    assert "source_key=S1" in client.calls[0][1]


def test_classifier_rejects_model_source_outside_package() -> None:
    client = FakeClient({"documents": [{
        "document_kind": "formal", "normalized_title": "Synthetic", "issuer": "", "document_number": "",
        "publication_date": "", "topic": "", "customer": "", "project": "", "stage": "",
        "confidence": 0.9, "sources": [{"source_key": "file:999", "role": "body"}],
        "evidence_source_keys": ["file:999"],
    }]})
    result = classify_package(package(), client, max_input_tokens=2000)
    assert result.needs_review
    assert result.reason_code == "unknown_source_key"


def test_classifier_degrades_low_confidence_to_review() -> None:
    client = FakeClient({"documents": [{
        "document_kind": "internal", "normalized_title": "Synthetic", "issuer": "", "document_number": "",
        "publication_date": "", "topic": "测试", "customer": "", "project": "", "stage": "",
        "confidence": 0.4, "sources": [{"source_key": "file:1", "role": "body"}],
        "evidence_source_keys": ["file:1"],
    }]})
    result = classify_package(package(), client, max_input_tokens=2000)
    assert result.needs_review
    assert result.reason_code == "low_confidence"


def test_classifier_parks_invalid_final_schema_for_review_instead_of_retry_loop() -> None:
    client = FakeClient({"unexpected": "shape"})

    result = classify_package(package(), client, max_input_tokens=2000)

    assert result.needs_review
    assert result.reason_code == "schema_invalid"


def test_classifier_fills_formal_metadata_only_from_source_evidence() -> None:
    client = FakeClient({"documents": [{
        "document_kind": "formal", "normalized_title": "关于测试工作的通知", "issuer": "",
        "document_number": "", "publication_date": "2026-08-01", "topic": "",
        "customer": "", "project": "", "stage": "", "confidence": 0.95,
        "sources": [{"source_key": "S1", "role": "body"}], "evidence_source_keys": ["S1"],
    }]})

    result = classify_package(package(), client, max_input_tokens=2000)

    assert not result.needs_review
    assert result.documents[0].issuer == "示例集团"
    assert result.documents[0].document_number == "示例发〔2026〕1号"


class LongClient(FakeClient):
    def chat(self, system: str, user: str, **_kwargs) -> dict:
        self.calls.append((system, user))
        if "来源分块" in system:
            return {"error": None, "content": json.dumps({
                "source_key": "file:1", "summary": "合成正式文件片段", "document_signals": ["文号"],
            }, ensure_ascii=False)}
        return super().chat(system, user, **_kwargs)


def test_classifier_maps_long_source_before_final_decision() -> None:
    base = package()
    long_package = OAPackage(
        package_key=base.package_key, title=base.title, completed_at=base.completed_at,
        sources=(PackageSource(
            source_key="file:1", title="示例发〔2026〕1号.md", markdown_relpath="parse/1.md",
            content_sha256="a" * 64, markdown_sha256="b" * 64,
            text="示例集团文件\n示例发〔2026〕1号\n" + "甲" * 10_000,
        ),),
    )
    client = LongClient({"documents": [{
        "document_kind": "formal", "normalized_title": "关于测试工作的通知", "issuer": "示例集团",
        "document_number": "示例发〔2026〕1号", "publication_date": "2026-08-01", "topic": "",
        "customer": "", "project": "", "stage": "", "confidence": 0.95,
        "sources": [{"source_key": "file:1", "role": "body"}], "evidence_source_keys": ["file:1"],
    }]})

    result = classify_package(long_package, client, max_input_tokens=900)

    assert not result.needs_review
    assert len(client.calls) > 2
    assert all(estimate_tokens(user) <= 900 for _system, user in client.calls)
