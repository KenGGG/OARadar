import hashlib
import json
from pathlib import Path

import pytest

from oa_knowledge.curation.classifier import classify_package
from oa_knowledge.curation.package import OAPackage, PackageSource


CASES = json.loads((Path(__file__).parent / "fixtures/curation/golden_cases.json").read_text(encoding="utf-8"))


class GoldenClient:
    def __init__(self, payload):
        self.payload = payload

    def chat(self, *_args, **_kwargs):
        return {"error": None, "content": json.dumps(self.payload, ensure_ascii=False)}


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_synthetic_curation_goldens(case) -> None:
    sources = tuple(
        PackageSource(
            source_key=row["key"], title=row["title"], markdown_relpath=f"parse/{index}.md",
            content_sha256=hashlib.sha256(f"source-{index}".encode()).hexdigest(),
            markdown_sha256=hashlib.sha256(row["text"].encode()).hexdigest(), text=row["text"], ordinal=index,
        )
        for index, row in enumerate(case["sources"], 1)
    )
    package = OAPackage(package_key=f"synthetic:{case['name']}", title=case["title"], completed_at="2026-08-15", sources=sources)

    result = classify_package(package, GoldenClient(case["model"]), max_input_tokens=4000)

    assert len(result.documents) == case["expected_documents"]
    assert result.reason_code == case["expected_reason"]
