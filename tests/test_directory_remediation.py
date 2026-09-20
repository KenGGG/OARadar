import importlib.util
from pathlib import Path

from oa_knowledge.classification.schemas import PrivateClassificationConfig

_SPEC = importlib.util.spec_from_file_location(
    "directory_remediation", Path(__file__).parents[1] / "scripts" / "directory_remediation.py"
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_category_warning = _MODULE._category_warning
_issuer_status = _MODULE._issuer_status
_source_resolution = _MODULE._source_resolution


def _config() -> PrivateClassificationConfig:
    return PrivateClassificationConfig.model_validate({
        "initiators": {"internal": {"role": "internal", "aliases": ["甲"]}},
        "issuer_aliases": {"甲局": "甲市财政局"},
        "document_number_issuers": [{"pattern": "甲发", "canonical_issuer": "甲市财政局"}],
        "title_templates": [{"pattern": "^不会匹配$", "content_origin": "internal", "flow_type": "approval"}],
    })


def test_migration_gate_rejects_display_wrapper_issuer() -> None:
    assert _issuer_status("(盖章)甲市财政局", _config()) == ("invalid", None)


def test_migration_gate_keeps_complete_direct_issuer() -> None:
    assert _issuer_status("甲市财政局", _config()) == ("valid", "甲市财政局")


def test_migration_gate_canonicalizes_an_approved_alias_but_rejects_generic_name() -> None:
    config = _config()
    config.issuer_aliases["财政局"] = "甲市财政局"
    assert _issuer_status("财政局", config) == ("valid", "甲市财政局")
    assert _issuer_status("人民政府办公室", _config()) == ("invalid", None)


def test_migration_gate_rejects_fragment_and_internal_style_issuer() -> None:
    assert _issuer_status("产监督管理局", _config()) == ("invalid", None)
    assert _issuer_status("风险控制中心", _config()) == ("invalid", None)


def test_category_sanity_warns_without_reclassifying() -> None:
    assert _category_warning("某项目租后检查", "04_财务资金与融资") == "title_signals_02_业务项目与投放租后"
    assert _category_warning("某项目租后检查", "02_业务项目与投放租后") == ""


def test_source_resolution_detects_equivalent_and_conflicting_packages(tmp_path: Path) -> None:
    first = tmp_path / "first"; second = tmp_path / "second"; first.mkdir(); second.mkdir()
    for folder in (first, second):
        (folder / "_index.md").write_text('---\noa_item_key: "done:test"\n---\n', encoding="utf-8")
        (folder / "a.md").write_text("---\nsource_file_id: 1\nsource_sha256: " + "a" * 64 + "\n---\n", encoding="utf-8")
    resolution, _, _ = _source_resolution([first, second], "done:test")
    assert resolution == "duplicate_equivalent"
    (second / "a.md").write_text("different", encoding="utf-8")
    resolution, _, _ = _source_resolution([first, second], "done:test")
    assert resolution == "conflict"
