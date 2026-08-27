from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from oa_knowledge.classification.private_config import (
    PrivateConfigError,
    load_private_classification_config,
)
from oa_knowledge.config import load_settings


REQUIRED_FILES = {
    "initiator_profiles.yaml": """
initiators:
  synth.person.internal:
    role: internal
    aliases: [Synthetic Internal Person]
  synth.person.external:
    role: external
    aliases: [Synthetic External Person]
  synth.person.mixed:
    role: mixed
    aliases: [Synthetic Mixed Person]
  synth.person.system:
    role: system
    aliases: [Synthetic Workflow Account]
  synth.person.unknown:
    role: unknown
    aliases: [Synthetic Unresolved Person]
""",
    "document_number_issuers.yaml": """
rules:
  - pattern: '^SYN-AUTH-[0-9]{4}-[0-9]+$'
    canonical_issuer: Synthetic Records Authority
    document_type: notice
""",
    "issuer_aliases.yaml": """
aliases:
  SRA: Synthetic Records Authority
  Synthetic Records Authority: Synthetic Records Authority
""",
    "title_templates.yaml": """
templates:
  - pattern: '^Synthetic internal approval:'
    content_origin: internal
    flow_type: approval
    business_category: 08_synthetic_administration
  - pattern: '^Synthetic external circulation:'
    content_origin: external
    flow_type: circulation
    canonical_issuer: Synthetic Records Authority
""",
}


def _write_private_config(
    root: Path,
    *,
    replacements: dict[str, str] | None = None,
    modes: dict[str, int] | None = None,
) -> Path:
    root.mkdir()
    contents = REQUIRED_FILES | (replacements or {})
    for filename, text in contents.items():
        path = root / filename
        path.write_text(text.lstrip(), encoding="utf-8")
        path.chmod((modes or {}).get(filename, 0o600))
    return root


def test_loads_all_four_strict_schemas_and_preserves_unknown_role(tmp_path: Path) -> None:
    root = _write_private_config(tmp_path / "classification")

    loaded = load_private_classification_config(root)

    assert loaded.config.initiators["synth.person.unknown"].role == "unknown"
    assert set(loaded.config.initiators) == {
        "synth.person.internal",
        "synth.person.external",
        "synth.person.mixed",
        "synth.person.system",
        "synth.person.unknown",
    }
    assert loaded.config.document_number_issuers[0].canonical_issuer == "Synthetic Records Authority"
    assert loaded.config.issuer_aliases["SRA"] == "Synthetic Records Authority"
    assert loaded.config.title_templates[0].content_origin == "internal"
    assert len(loaded.config_sha256) == 64
    assert set(loaded.config_sha256) <= set("0123456789abcdef")


def test_hash_is_deterministic_across_mapping_order_and_root(tmp_path: Path) -> None:
    first = _write_private_config(tmp_path / "first")
    second = _write_private_config(
        tmp_path / "second",
        replacements={
            "issuer_aliases.yaml": """
aliases:
  Synthetic Records Authority: Synthetic Records Authority
  SRA: Synthetic Records Authority
""",
        },
    )

    first_loaded = load_private_classification_config(first)
    second_loaded = load_private_classification_config(second)

    assert first_loaded.config == second_loaded.config
    assert first_loaded.config_sha256 == second_loaded.config_sha256


def test_hash_ignores_unordered_rule_and_initiator_alias_declaration_order(
    tmp_path: Path,
) -> None:
    initiators = """
initiators:
  synth.person.internal:
    role: internal
    aliases: [Synthetic Internal Alpha, Synthetic Internal Beta]
  synth.person.unknown:
    role: unknown
    aliases: [Synthetic Unknown Alpha, Synthetic Unknown Beta]
"""
    document_rules = """
rules:
  - pattern: '^SYN-A-[0-9]+$'
    canonical_issuer: Synthetic Authority A
    document_type: notice
  - pattern: '^SYN-B-[0-9]+$'
    canonical_issuer: Synthetic Authority B
    document_type: circular
"""
    title_rules = """
templates:
  - pattern: '^Synthetic approval:'
    content_origin: internal
    flow_type: approval
    business_category: 08_synthetic_administration
  - pattern: '^Synthetic circulation:'
    content_origin: external
    flow_type: circulation
    canonical_issuer: Synthetic Authority A
"""
    first = _write_private_config(
        tmp_path / "first",
        replacements={
            "initiator_profiles.yaml": initiators,
            "document_number_issuers.yaml": document_rules,
            "title_templates.yaml": title_rules,
        },
    )
    reordered = _write_private_config(
        tmp_path / "reordered",
        replacements={
            "initiator_profiles.yaml": """
initiators:
  synth.person.unknown:
    role: unknown
    aliases: [Synthetic Unknown Beta, Synthetic Unknown Alpha]
  synth.person.internal:
    role: internal
    aliases: [Synthetic Internal Beta, Synthetic Internal Alpha]
""",
            "document_number_issuers.yaml": """
rules:
  - pattern: '^SYN-B-[0-9]+$'
    canonical_issuer: Synthetic Authority B
    document_type: circular
  - pattern: '^SYN-A-[0-9]+$'
    canonical_issuer: Synthetic Authority A
    document_type: notice
""",
            "title_templates.yaml": """
templates:
  - pattern: '^Synthetic circulation:'
    content_origin: external
    flow_type: circulation
    canonical_issuer: Synthetic Authority A
  - pattern: '^Synthetic approval:'
    content_origin: internal
    flow_type: approval
    business_category: 08_synthetic_administration
""",
        },
    )
    semantically_changed = _write_private_config(
        tmp_path / "changed",
        replacements={
            "initiator_profiles.yaml": initiators.replace("role: unknown", "role: mixed"),
            "document_number_issuers.yaml": document_rules,
            "title_templates.yaml": title_rules,
        },
    )

    first_hash = load_private_classification_config(first).config_sha256
    reordered_hash = load_private_classification_config(reordered).config_sha256
    changed_hash = load_private_classification_config(semantically_changed).config_sha256

    assert reordered_hash == first_hash
    assert changed_hash != first_hash


@pytest.mark.parametrize(
    ("filename", "replacement"),
    [
        (
            "initiator_profiles.yaml",
            "initiators:\n  synth.person.internal:\n    role: internal\n    unexpected: forbidden\n",
        ),
        (
            "document_number_issuers.yaml",
            "rules:\n  - pattern: '^SYN-'\n    canonical_issuer: Synthetic Records Authority\n    unexpected: forbidden\n",
        ),
        (
            "title_templates.yaml",
            "templates:\n  - pattern: '^Synthetic'\n    content_origin: internal\n    unexpected: forbidden\n",
        ),
        (
            "issuer_aliases.yaml",
            "aliases:\n  SRA: Synthetic Records Authority\nunexpected: forbidden\n",
        ),
    ],
)
def test_rejects_unknown_fields_in_each_file_schema(
    tmp_path: Path, filename: str, replacement: str
) -> None:
    root = _write_private_config(tmp_path / "classification", replacements={filename: replacement})

    with pytest.raises(PrivateConfigError, match=filename):
        load_private_classification_config(root)


def test_requires_all_four_files(tmp_path: Path) -> None:
    root = _write_private_config(tmp_path / "classification")
    (root / "title_templates.yaml").unlink()

    with pytest.raises(PrivateConfigError, match="title_templates.yaml"):
        load_private_classification_config(root)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_rejects_permissions_broader_than_owner_read_write(tmp_path: Path) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        modes={"issuer_aliases.yaml": 0o640},
    )

    with pytest.raises(PrivateConfigError, match=r"issuer_aliases.yaml.*0600"):
        load_private_classification_config(root)


def test_rejects_symlink_that_escapes_the_private_root(tmp_path: Path) -> None:
    root = _write_private_config(tmp_path / "classification")
    outside = tmp_path / "outside.yaml"
    outside.write_text(REQUIRED_FILES["issuer_aliases.yaml"].lstrip(), encoding="utf-8")
    outside.chmod(0o600)
    (root / "issuer_aliases.yaml").unlink()
    (root / "issuer_aliases.yaml").symlink_to(outside)

    with pytest.raises(PrivateConfigError, match=r"issuer_aliases.yaml.*symlink"):
        load_private_classification_config(root)


def test_rejects_non_regular_required_file(tmp_path: Path) -> None:
    root = _write_private_config(tmp_path / "classification")
    (root / "issuer_aliases.yaml").unlink()
    (root / "issuer_aliases.yaml").mkdir()

    with pytest.raises(PrivateConfigError, match=r"issuer_aliases.yaml.*regular"):
        load_private_classification_config(root)


def test_rejects_duplicate_yaml_alias_key_instead_of_silently_overwriting(tmp_path: Path) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        replacements={
            "issuer_aliases.yaml": """
aliases:
  SRA: Synthetic Records Authority
  SRA: Synthetic Alternate Authority
""",
        },
    )

    with pytest.raises(PrivateConfigError, match=r"issuer_aliases.yaml.*duplicate"):
        load_private_classification_config(root)


def test_rejects_normalized_alias_conflict_between_canonical_issuers(tmp_path: Path) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        replacements={
            "issuer_aliases.yaml": """
aliases:
  SRA: Synthetic Records Authority
  ' SRA ': Synthetic Alternate Authority
""",
        },
    )

    with pytest.raises(PrivateConfigError, match="issuer_aliases.yaml"):
        load_private_classification_config(root)


@pytest.mark.parametrize(
    "aliases",
    [
        """
aliases:
  SAA: Synthetic Authority A
  Synthetic Authority A: Synthetic Authority B
  Synthetic Authority B: Synthetic Authority B
""",
        """
aliases:
  Synthetic Authority A: Synthetic Authority B
  Synthetic Authority B: Synthetic Authority A
""",
        """
aliases:
  SAA: Synthetic Authority A
  Synthetic Authority A: Synthetic Alternate Authority
""",
    ],
    ids=["alias-chain", "alias-cycle", "non-terminal-canonical-target"],
)
def test_rejects_non_terminal_canonical_issuer_aliases(
    tmp_path: Path, aliases: str
) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        replacements={"issuer_aliases.yaml": aliases},
    )

    with pytest.raises(PrivateConfigError, match="issuer_aliases.yaml"):
        load_private_classification_config(root)


@pytest.mark.parametrize(
    "aliases",
    [
        """
aliases:
  SAA: Synthetic Authority A
  Synthetic Authority A: SYNTHETIC AUTHORITY A
""",
        """
aliases:
  SAA: Synthetic Authority A
  SAA Upper: SYNTHETIC AUTHORITY A
""",
    ],
    ids=["case-drifting-pseudo-self-map", "non-unique-canonical-output"],
)
def test_rejects_case_drift_between_canonical_output_strings(
    tmp_path: Path, aliases: str
) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        replacements={"issuer_aliases.yaml": aliases},
    )

    with pytest.raises(PrivateConfigError, match="issuer_aliases.yaml"):
        load_private_classification_config(root)


@pytest.mark.parametrize(
    ("filename", "rule_config"),
    [
        (
            "document_number_issuers.yaml",
            """
rules:
  - pattern: '^SYN-REDIRECT-[0-9]+$'
    canonical_issuer: Synthetic Authority A
""",
        ),
        (
            "title_templates.yaml",
            """
templates:
  - pattern: '^Synthetic redirected circulation:'
    content_origin: external
    flow_type: circulation
    canonical_issuer: Synthetic Authority A
""",
        ),
    ],
    ids=["document-number-rule", "title-template"],
)
def test_rejects_rule_canonical_issuer_that_aliases_to_another_output(
    tmp_path: Path, filename: str, rule_config: str
) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        replacements={
            filename: rule_config,
            "issuer_aliases.yaml": """
aliases:
  Synthetic Authority A: Synthetic Authority B
  Synthetic Authority B: Synthetic Authority B
""",
        },
    )

    with pytest.raises(PrivateConfigError, match="conflicting aliases"):
        load_private_classification_config(root)


def test_accepts_exact_canonical_output_from_case_insensitive_alias_key_across_rules(
    tmp_path: Path,
) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        replacements={
            "document_number_issuers.yaml": """
rules:
  - pattern: '^SYN-EXACT-[0-9]+$'
    canonical_issuer: Synthetic Authority A
""",
            "title_templates.yaml": """
templates:
  - pattern: '^Synthetic exact circulation:'
    content_origin: external
    flow_type: circulation
    canonical_issuer: Synthetic Authority A
""",
            "issuer_aliases.yaml": """
aliases:
  SAA: Synthetic Authority A
  synthetic authority a: Synthetic Authority A
""",
        },
    )

    loaded = load_private_classification_config(root)

    assert loaded.config.issuer_aliases == {
        "SAA": "Synthetic Authority A",
        "synthetic authority a": "Synthetic Authority A",
    }
    assert loaded.config.document_number_issuers[0].canonical_issuer == (
        "Synthetic Authority A"
    )
    assert loaded.config.title_templates[0].canonical_issuer == "Synthetic Authority A"


@pytest.mark.parametrize(
    "replacements",
    [
        {
            "issuer_aliases.yaml": """
aliases:
  SAA: Synthetic Authority A
""",
            "document_number_issuers.yaml": """
rules:
  - pattern: '^SYN-ALIAS-DOC-[0-9]+$'
    canonical_issuer: SYNTHETIC AUTHORITY A
""",
        },
        {
            "document_number_issuers.yaml": """
rules:
  - pattern: '^SYN-DOC-TITLE-[0-9]+$'
    canonical_issuer: Synthetic Authority A
""",
            "title_templates.yaml": """
templates:
  - pattern: '^Synthetic doc title conflict:'
    content_origin: external
    flow_type: circulation
    canonical_issuer: SYNTHETIC AUTHORITY A
""",
        },
        {
            "document_number_issuers.yaml": """
rules:
  - pattern: '^SYN-DOC-ONE-[0-9]+$'
    canonical_issuer: Synthetic Authority A
  - pattern: '^SYN-DOC-TWO-[0-9]+$'
    canonical_issuer: SYNTHETIC AUTHORITY A
""",
        },
        {
            "title_templates.yaml": """
templates:
  - pattern: '^Synthetic title one:'
    content_origin: external
    flow_type: circulation
    canonical_issuer: Synthetic Authority A
  - pattern: '^Synthetic title two:'
    content_origin: external
    flow_type: distribution
    canonical_issuer: SYNTHETIC AUTHORITY A
""",
        },
        {
            "issuer_aliases.yaml": """
aliases:
  SAA: Synthetic Authority A
""",
            "title_templates.yaml": """
templates:
  - pattern: '^Synthetic alias title conflict:'
    content_origin: external
    flow_type: circulation
    canonical_issuer: SYNTHETIC AUTHORITY A
""",
        },
    ],
    ids=[
        "alias-vs-document",
        "document-vs-title",
        "document-vs-document",
        "title-vs-title",
        "alias-vs-title",
    ],
)
def test_rejects_cross_source_case_drift_for_one_canonical_identity(
    tmp_path: Path, replacements: dict[str, str]
) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        replacements=replacements,
    )

    with pytest.raises(PrivateConfigError, match="conflicting aliases"):
        load_private_classification_config(root)


def test_accepts_one_exact_canonical_spelling_reused_by_all_sources(tmp_path: Path) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        replacements={
            "issuer_aliases.yaml": """
aliases:
  SAA: Synthetic Authority A
""",
            "document_number_issuers.yaml": """
rules:
  - pattern: '^SYN-EXACT-ONE-[0-9]+$'
    canonical_issuer: Synthetic Authority A
  - pattern: '^SYN-EXACT-TWO-[0-9]+$'
    canonical_issuer: Synthetic Authority A
""",
            "title_templates.yaml": """
templates:
  - pattern: '^Synthetic exact one:'
    content_origin: external
    flow_type: circulation
    canonical_issuer: Synthetic Authority A
  - pattern: '^Synthetic exact two:'
    content_origin: external
    flow_type: distribution
    canonical_issuer: Synthetic Authority A
""",
        },
    )

    loaded = load_private_classification_config(root)

    assert {
        rule.canonical_issuer for rule in loaded.config.document_number_issuers
    } == {"Synthetic Authority A"}
    assert {
        template.canonical_issuer for template in loaded.config.title_templates
    } == {"Synthetic Authority A"}


@pytest.mark.parametrize(
    ("filename", "rules"),
    [
        (
            "document_number_issuers.yaml",
            """
rules:
  - pattern: '^SYN-DUP-[0-9]+$'
    canonical_issuer: Synthetic Authority A
  - pattern: '^SYN-DUP-[0-9]+$'
    canonical_issuer: Synthetic Authority B
""",
        ),
        (
            "title_templates.yaml",
            """
templates:
  - pattern: '^Synthetic duplicate:'
    content_origin: internal
    flow_type: approval
  - pattern: '^Synthetic duplicate:'
    content_origin: external
    flow_type: circulation
""",
        ),
    ],
)
def test_rejects_rule_patterns_with_order_ambiguous_outcomes(
    tmp_path: Path, filename: str, rules: str
) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        replacements={filename: rules},
    )

    with pytest.raises(PrivateConfigError, match=filename):
        load_private_classification_config(root)


@pytest.mark.parametrize(
    "profile",
    [
        "aliases: [Synthetic Unresolved Person]",
        "role: undecided\n    aliases: [Synthetic Unresolved Person]",
    ],
)
def test_rejects_missing_or_invalid_initiator_role(tmp_path: Path, profile: str) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        replacements={
            "initiator_profiles.yaml": (
                "initiators:\n  synth.person.unknown:\n    " + profile + "\n"
            ),
        },
    )

    with pytest.raises(PrivateConfigError, match=r"initiator_profiles.yaml.*role"):
        load_private_classification_config(root)


def test_classification_private_directory_is_the_only_settings_entry(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "classification"
    monkeypatch.setenv("OA_CLASSIFICATION_PRIVATE_DIR", str(root))
    monkeypatch.setenv("OA_CLASSIFICATION_INITIATOR_NAME", "must-not-be-loaded")

    settings = load_settings()

    assert settings.classification_private_dir == root.resolve()
    assert "must-not-be-loaded" not in settings.model_dump_json()


def test_settings_preserves_root_symlink_for_loader_rejection(monkeypatch, tmp_path: Path) -> None:
    target = _write_private_config(tmp_path / "classification-target")
    configured = tmp_path / "classification-link"
    configured.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("OA_CLASSIFICATION_PRIVATE_DIR", str(configured))

    settings = load_settings()

    assert settings.classification_private_dir == configured.absolute()
    assert settings.classification_private_dir.is_symlink()
    with pytest.raises(PrivateConfigError, match="root must not be a symlink"):
        load_private_classification_config(settings.classification_private_dir)


@pytest.mark.parametrize(
    ("filename", "replacement", "private_fragments"),
    [
        (
            "initiator_profiles.yaml",
            """
initiators:
  TOP-SECRET-SYNTHETIC-ID:
    role: TOP-SECRET-SYNTHETIC-ROLE
    aliases: [TOP-SECRET-SYNTHETIC-ALIAS]
""",
            (
                "TOP-SECRET-SYNTHETIC-ID",
                "TOP-SECRET-SYNTHETIC-ROLE",
                "TOP-SECRET-SYNTHETIC-ALIAS",
            ),
        ),
        (
            "issuer_aliases.yaml",
            """
aliases:
  TOP-SECRET-SYNTHETIC-KEY: [TOP-SECRET-SYNTHETIC-YAML
""",
            ("TOP-SECRET-SYNTHETIC-KEY", "TOP-SECRET-SYNTHETIC-YAML"),
        ),
    ],
    ids=["schema-validation", "malformed-yaml"],
)
def test_private_validation_errors_have_no_input_bearing_exception_chain(
    tmp_path: Path,
    filename: str,
    replacement: str,
    private_fragments: tuple[str, ...],
) -> None:
    root = _write_private_config(
        tmp_path / "classification",
        replacements={filename: replacement},
    )

    with pytest.raises(PrivateConfigError) as captured:
        load_private_classification_config(root)

    error = captured.value
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = str(error)
    assert filename in rendered
    for fragment in private_fragments:
        assert fragment not in rendered


def test_public_examples_are_synthetic_and_cover_all_initiator_roles() -> None:
    examples_root = Path(__file__).parents[1] / "examples" / "classification"
    initiators = yaml.safe_load(
        (examples_root / "initiator_profiles.example.yaml").read_text(encoding="utf-8")
    )["initiators"]

    assert {profile["role"] for profile in initiators.values()} == {
        "internal",
        "external",
        "mixed",
        "system",
        "unknown",
    }
    assert all(identifier.startswith("synth.") for identifier in initiators)


def test_model_schemas_are_strict_even_when_used_directly() -> None:
    from oa_knowledge.classification.schemas import InitiatorProfile

    with pytest.raises(ValidationError):
        InitiatorProfile.model_validate({"role": "internal", "unexpected": "forbidden"})
