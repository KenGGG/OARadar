from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from oa_knowledge.backfill_mvp import (
    BackfillMVPRequest,
    BackfillMVPService,
    SampleItem,
    select_representative_items,
)
from oa_knowledge.classification.schemas import PrivateClassificationConfig
from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    ArchivedFile,
    Base,
    ClassificationEvidence,
    OAItem,
    OAManifestItem,
)
from oa_knowledge.parsers.router import ParseResult


def _config() -> PrivateClassificationConfig:
    return PrivateClassificationConfig.model_validate(
        {
            "initiators": {
                "synthetic.internal": {"role": "internal", "aliases": []},
                "synthetic.mixed": {"role": "mixed", "aliases": []},
                "synthetic.unknown": {"role": "unknown", "aliases": []},
            },
            "document_number_issuers": [
                {
                    "pattern": r"^SYN-AUTH-[0-9]+$",
                    "canonical_issuer": "Synthetic Authority",
                }
            ],
            "issuer_aliases": {"Synthetic Authority": "Synthetic Authority"},
            "title_templates": [
                {
                    "pattern": r"^Synthetic internal approval",
                    "content_origin": "internal",
                    "flow_type": "approval",
                    "business_category": "08_行政采购与信息化",
                }
            ],
        }
    )


def _engine(reverse: bool = False):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    rows: list[tuple[OAManifestItem, OAItem | None, int, str]] = []
    rows.append(
        (
            OAManifestItem(
                oa_item_key="done:excluded",
                title="Synthetic excluded leave",
                sender="synthetic.internal",
                processing_status="skipped",
                matched_exclusion_keyword="Synthetic leave",
            ),
            None,
            0,
            "verified",
        )
    )
    for index in range(80):
        key = f"done:ordinary-{index:03d}"
        rows.append(
            (
                OAManifestItem(
                    oa_item_key=key,
                    title=f"Synthetic ordinary matter {index}",
                    sender="synthetic.internal",
                    processing_status="downloaded",
                ),
                OAItem(
                    oa_item_key=key,
                    source_channel="done",
                    title=f"Synthetic ordinary matter {index}",
                    sender="synthetic.internal",
                    pipeline_status="files_verified",
                ),
                1,
                "verified",
            )
        )
    special_specs = (
        ("internal_template", "Synthetic internal approval equipment", "synthetic.internal", None, 1, "verified", False),
        ("external_number", "SYN-AUTH-7", "synthetic.internal", None, 1, "verified", False),
        ("transfer", "【文件传阅】Synthetic notice", "synthetic.internal", None, 1, "verified", False),
        ("no_number", "Synthetic neutral no number", "synthetic.unknown", None, 1, "verified", False),
        ("multi_attachment", "Synthetic many files", "synthetic.internal", None, 4, "verified", False),
        ("no_attachment", "Synthetic confirmed empty", "synthetic.internal", None, 0, "verified", True),
        ("mixed", "Synthetic mixed sender", "synthetic.mixed", None, 1, "verified", False),
        ("abnormal", "Synthetic changed file", "synthetic.internal", None, 1, "rejected_zero_byte", False),
    )
    for repeat in range(5):
        for bucket, title, sender, number, file_count, status, no_attachment in special_specs:
            key = f"done:{bucket}-{repeat}"
            manifest_status = "no_attachment" if no_attachment else "downloaded"
            rows.append(
                (
                    OAManifestItem(
                        oa_item_key=key,
                        title=title,
                        sender=sender,
                        processing_status=manifest_status,
                        no_attachment_confirmed=no_attachment,
                    ),
                    OAItem(
                        oa_item_key=key,
                        source_channel="done",
                        title=title,
                        sender=sender,
                        document_number=number,
                        pipeline_status="files_verified",
                    ),
                    file_count,
                    status,
                )
            )
    if reverse:
        rows.reverse()
    with Session(engine) as session:
        for ordinal, (manifest, item, file_count, status) in enumerate(rows, 1):
            manifest.list_page = 1
            manifest.list_ordinal = ordinal
            session.add(manifest)
            if item is None:
                continue
            session.add(item)
            session.flush()
            for file_ordinal in range(file_count):
                session.add(
                    ArchivedFile(
                        oa_item_id=item.id,
                        original_name=f"synthetic-{file_ordinal}.pdf",
                        local_relpath=f"originals/{item.oa_item_key}/{file_ordinal}.pdf",
                        size_bytes=10,
                        sha256=f"{item.id:064x}"[-64:],
                        attachment_key=f"attachment-{file_ordinal}",
                        file_role="direct_attachment",
                        source_container_key="root",
                        depth=1,
                        download_status=status,
                    )
                )
        session.commit()
    return engine


def test_selects_representative_targets_without_building_a_sampling_framework() -> None:
    engine = _engine()
    try:
        with Session(engine) as session:
            selected = select_representative_items(session, _config(), 100)
    finally:
        engine.dispose()

    assert len(selected) == 100
    assert all(isinstance(row, SampleItem) for row in selected)
    assert len({row.oa_item_key for row in selected}) == 100
    assert "done:excluded" not in {row.oa_item_key for row in selected}
    buckets = {row.bucket for row in selected}
    assert {
        "ordinary",
        "internal_template",
        "external_document_number",
        "file_transfer",
        "no_document_number",
        "multiple_attachments",
        "no_attachment",
        "mixed_initiator",
        "attachment_abnormal",
    } <= buckets
    assert sum(row.bucket == "ordinary" for row in selected) >= 60


def test_selection_is_stable_when_database_insertion_order_changes() -> None:
    first_engine = _engine()
    second_engine = _engine(reverse=True)
    try:
        with Session(first_engine) as first, Session(second_engine) as second:
            first_result = select_representative_items(first, _config(), 100)
            second_result = select_representative_items(second, _config(), 100)
    finally:
        first_engine.dispose()
        second_engine.dispose()

    assert first_result == second_result


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "app": {"data_root": str(tmp_path / "data")},
            "runtime": {
                "state_root": str(tmp_path / "state"),
                "cache_root": str(tmp_path / "cache"),
            },
            "mineru": {"enabled": True},
        }
    )


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _add_item(
    factory: sessionmaker[Session],
    settings: Settings,
    *,
    key: str,
    title: str,
    sender: str,
    payloads: tuple[tuple[str, bytes, str], ...] = (),
    no_attachment: bool = False,
) -> None:
    with factory.begin() as session:
        manifest = OAManifestItem(
            oa_item_key=key,
            title=title,
            sender=sender,
            list_page=1,
            list_ordinal=1,
            processing_status="no_attachment" if no_attachment else "downloaded",
            no_attachment_confirmed=no_attachment,
        )
        item = OAItem(
            oa_item_key=key,
            source_channel="done",
            title=title,
            sender=sender,
            pipeline_status="files_verified",
        )
        session.add_all([manifest, item])
        session.flush()
        for ordinal, (filename, payload, status) in enumerate(payloads, 1):
            relpath = f"originals/{key}/{filename}"
            source = settings.data_root / relpath
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(payload)
            session.add(
                ArchivedFile(
                    oa_item_id=item.id,
                    original_name=filename,
                    local_relpath=relpath,
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    attachment_key=f"attachment-{ordinal}",
                    file_role="direct_attachment",
                    source_container_key="root",
                    depth=1,
                    download_status=status,
                )
            )


def test_vertical_slice_builds_classified_and_review_packages_without_touching_originals(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    factory = _factory()
    _add_item(
        factory,
        settings,
        key="done:internal",
        title="Synthetic internal approval equipment",
        sender="synthetic.internal",
        payloads=(("evidence.txt", b"faithful synthetic evidence\n", "verified"),),
    )
    _add_item(
        factory,
        settings,
        key="done:review",
        title="Synthetic unresolved matter",
        sender="synthetic.unknown",
        no_attachment=True,
    )
    before = {
        path.relative_to(settings.originals_root).as_posix(): path.read_bytes()
        for path in settings.originals_root.rglob("*")
        if path.is_file()
    }

    result = BackfillMVPService(
        settings,
        factory,
        _config(),
        private_config_sha256="a" * 64,
    ).run(
        BackfillMVPRequest(
            run_id="synthetic-vertical",
            target_keys=("done:internal", "done:review"),
        )
    )

    assert result.processed == 2
    assert result.packages == 2
    assert result.classified == 1
    assert result.needs_review == 1
    assert result.attachments_attempted == 1
    assert result.attachments_converted == 1
    indexes = list(result.output_root.rglob("_index.md"))
    attachments = [
        path
        for path in result.output_root.rglob("*.md")
        if path.name != "_index.md"
    ]
    assert len(indexes) == 2
    assert len(attachments) == 1
    assert "faithful synthetic evidence" in attachments[0].read_text(encoding="utf-8")
    assert any("needs_review" in path.parts for path in indexes)
    for name in ("sample.csv", "classification.csv", "exceptions.csv"):
        report = result.output_root / name
        assert report.read_bytes().startswith(b"\xef\xbb\xbf")
    manifest = json.loads(
        (result.output_root / "build_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == "backfill-mvp-v3.2"
    assert manifest["counts"]["selected"] == 2
    assert manifest["counts"]["packages"] == 2
    assert manifest["counts"]["classified"] == 1
    assert manifest["counts"]["needs_review"] == 1
    assert manifest["counts"]["attachments_attempted"] == 1
    assert manifest["counts"]["attachments_converted"] == 1
    assert manifest["reconciliation"]["ok"] is True
    assert manifest["reconciliation"]["selected_equation"] is True
    assert manifest["reconciliation"]["attachment_equation"] is True
    assert set(manifest["parser_environment"]) == {"antiword", "wv"}
    for file_row in manifest["files"]:
        path = result.output_root / file_row["path"]
        assert path.is_file()
        assert path.stat().st_size == file_row["size"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == file_row["sha256"]
    after = {
        path.relative_to(settings.originals_root).as_posix(): path.read_bytes()
        for path in settings.originals_root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_identical_completed_run_is_verified_and_reused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    factory = _factory()
    _add_item(
        factory,
        settings,
        key="done:resume",
        title="Synthetic internal approval resume",
        sender="synthetic.internal",
        no_attachment=True,
    )
    service = BackfillMVPService(
        settings,
        factory,
        _config(),
        private_config_sha256="a" * 64,
    )
    request = BackfillMVPRequest(
        run_id="synthetic-resume", target_keys=("done:resume",)
    )

    first = service.run(request)
    first_manifest = (first.output_root / "build_manifest.json").read_bytes()
    second = service.run(request)

    assert second == first
    assert (second.output_root / "build_manifest.json").read_bytes() == first_manifest


def test_duplicate_attachment_content_is_parsed_once_and_materialized_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    factory = _factory()
    payload = b"synthetic binary payload"
    for suffix in ("one", "two"):
        _add_item(
            factory,
            settings,
            key=f"done:{suffix}",
            title=f"Synthetic internal approval {suffix}",
            sender="synthetic.internal",
            payloads=((f"{suffix}.pdf", payload, "verified"),),
        )
    calls: list[str] = []

    def fake_parse(
        source: Path,
        settings: Settings,
        *,
        engine: str,
        output_dir: Path,
        profile_version: str,
    ) -> ParseResult:
        calls.append(source.name)
        output = output_dir / "parsed.md"
        output.write_text(
            "Faithful synthetic parsed body with enough effective characters for quality validation.\n",
            encoding="utf-8",
        )
        return ParseResult(
            output,
            engine,
            "synthetic-parser-v1",
            1.0,
            text_length=82,
            profile_version=profile_version,
        )

    monkeypatch.setattr(
        "oa_knowledge.classification.parse_cache.parse_file", fake_parse
    )
    monkeypatch.setattr(
        "oa_knowledge.backfill_mvp.resolve_parser_version",
        lambda engine, settings: "synthetic-parser-v1",
    )

    result = BackfillMVPService(
        settings,
        factory,
        _config(),
        private_config_sha256="a" * 64,
    ).run(
        BackfillMVPRequest(
            run_id="synthetic-dedup",
            target_keys=("done:one", "done:two"),
        )
    )

    assert calls == ["one.pdf"]
    assert result.attachments_converted == 2
    attachments = [
        path
        for path in result.output_root.rglob("*.md")
        if path.name != "_index.md"
    ]
    assert len(attachments) == 2
    rendered = [path.read_text(encoding="utf-8") for path in attachments]
    assert all("Faithful synthetic parsed body" in text for text in rendered)
    assert {yaml.safe_load(text.split("---", 2)[1])["source_sha256"] for text in rendered} == {
        hashlib.sha256(payload).hexdigest()
    }


def test_same_oa_truncated_alias_materializes_one_canonical_attachment(
    tmp_path: Path,
) -> None:
    """A CAP4/legacy-panel alias must not become a second Package attachment."""
    settings = _settings(tmp_path)
    factory = _factory()
    payload = b"faithful synthetic alias content with enough readable characters"
    _add_item(
        factory,
        settings,
        key="done:alias",
        title="Synthetic internal approval equipment",
        sender="synthetic.internal",
        payloads=(
            ("完整附件.txt", payload, "verified"),
            ("完整附....txt", payload, "verified"),
        ),
    )
    with factory.begin() as session:
        rows = list(session.scalars(select(ArchivedFile).order_by(ArchivedFile.id)))
        rows[0].file_role = "official_attachment"
        rows[0].source_container_key = "collaboration:alias"
        rows[1].file_role = "direct_attachment"
        rows[1].source_container_key = "collaboration:alias"

    result = BackfillMVPService(
        settings, factory, _config(), private_config_sha256="a" * 64
    ).run(BackfillMVPRequest(run_id="synthetic-alias", target_keys=("done:alias",)))

    attachments = [
        path for path in result.output_root.rglob("*.md") if path.name != "_index.md"
    ]
    index = next(result.output_root.rglob("_index.md")).read_text(encoding="utf-8")
    manifest = json.loads(
        (result.output_root / "build_manifest.json").read_text(encoding="utf-8")
    )
    assert result.attachments_attempted == 1
    assert result.attachments_converted == 1
    assert len(attachments) == 1
    assert "完整附件" in attachments[0].name
    assert index.count("](<附件") == 1
    assert manifest["counts"]["attachments_duplicate_aliases"] == 1


def test_attachment_failures_are_reported_without_stopping_later_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    factory = _factory()
    _add_item(
        factory,
        settings,
        key="done:parse-failure",
        title="Synthetic internal approval parse failure",
        sender="synthetic.internal",
        payloads=(("broken.pdf", b"synthetic broken", "verified"),),
    )
    _add_item(
        factory,
        settings,
        key="done:unsupported",
        title="Synthetic internal approval unsupported",
        sender="synthetic.internal",
        payloads=(("archive.bin", b"synthetic unsupported", "verified"),),
    )
    _add_item(
        factory,
        settings,
        key="done:later",
        title="Synthetic internal approval later",
        sender="synthetic.internal",
        payloads=(("later.txt", b"later evidence\n", "verified"),),
    )

    def fail_parse(*args, **kwargs):
        raise RuntimeError("synthetic parser failure")

    monkeypatch.setattr(
        "oa_knowledge.classification.parse_cache.parse_file", fail_parse
    )
    monkeypatch.setattr(
        "oa_knowledge.backfill_mvp.resolve_parser_version",
        lambda engine, settings: "synthetic-parser-v1",
    )

    result = BackfillMVPService(
        settings,
        factory,
        _config(),
        private_config_sha256="a" * 64,
    ).run(
        BackfillMVPRequest(
            run_id="synthetic-continuation",
            target_keys=(
                "done:parse-failure",
                "done:unsupported",
                "done:later",
            ),
        )
    )

    assert result.packages == 3
    assert result.attachments_attempted == 3
    assert result.attachments_converted == 1
    assert result.attachments_failed == 1
    assert result.attachments_skipped == 1
    assert {row.code for row in result.exceptions} == {
        "parse_quality_failed",
        "unsupported_file_type",
    }
    assert any("later evidence" in path.read_text(encoding="utf-8") for path in result.output_root.rglob("*.md"))


@pytest.mark.parametrize("suffix", (".pdf", ".png", ".jpg", ".tiff"))
def test_visual_documents_use_mineru_then_at_most_one_markitdown_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    """Routing visual files straight to MarkItDown recreates empty OCR output."""
    settings = _settings(tmp_path)
    factory = _factory()
    _add_item(
        factory,
        settings,
        key="done:visual",
        title="Synthetic internal approval visual",
        sender="synthetic.internal",
        payloads=((f"visual{suffix}", b"synthetic visual", "verified"),),
    )
    calls: list[str] = []

    def fake_parse(
        source: Path,
        settings: Settings,
        *,
        engine: str,
        output_dir: Path,
        profile_version: str,
    ) -> ParseResult:
        calls.append(engine)
        output = output_dir / f"{engine}.md"
        body = "" if engine == "mineru" else "Readable synthetic fallback body with enough effective characters."
        output.write_text(body, encoding="utf-8")
        return ParseResult(
            output,
            engine,
            "synthetic-parser-v2",
            1.0,
            text_length=len(body),
            profile_version=profile_version,
        )

    monkeypatch.setattr("oa_knowledge.classification.parse_cache.parse_file", fake_parse)
    monkeypatch.setattr(
        "oa_knowledge.backfill_mvp.resolve_parser_version",
        lambda engine, settings: "synthetic-parser-v2",
    )

    result = BackfillMVPService(
        settings, factory, _config(), private_config_sha256="a" * 64
    ).run(
        BackfillMVPRequest(run_id=f"synthetic-visual-{suffix[1:]}", target_keys=("done:visual",))
    )

    assert calls == ["mineru", "markitdown"]
    assert result.attachments_converted == 1
    attachment = next(
        path for path in result.output_root.rglob("*.md") if path.name != "_index.md"
    )
    frontmatter = yaml.safe_load(attachment.read_text(encoding="utf-8").split("---", 2)[1])
    assert frontmatter["conversion_profile"] == "backfill-mvp-v3.2"
    assert frontmatter["parse_engine"] == "markitdown"
    assert frontmatter["fallback_reasons"] == ["empty_body"]


def test_fragmented_parse_output_triggers_one_fallback_and_records_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A high parser score must not hide a document made of hundreds of fragments."""
    settings = _settings(tmp_path)
    factory = _factory()
    _add_item(
        factory,
        settings,
        key="done:fragmented",
        title="Synthetic internal approval fragmented",
        sender="synthetic.internal",
        payloads=(("fragmented.pdf", b"synthetic fragmented", "verified"),),
    )
    calls: list[str] = []

    def fake_parse(source, settings, *, engine, output_dir, profile_version):
        calls.append(engine)
        output = output_dir / f"{engine}.md"
        body = "\n".join(f"fragment {index}" for index in range(40)) if engine == "mineru" else (
            "This synthetic fallback paragraph is deliberately long enough to be read "
            "as a coherent paragraph instead of a collection of broken lines."
        )
        output.write_text(body, encoding="utf-8")
        return ParseResult(
            output,
            engine,
            "synthetic-parser-v2",
            1.0,
            text_length=len(body),
            profile_version=profile_version,
        )

    monkeypatch.setattr("oa_knowledge.classification.parse_cache.parse_file", fake_parse)
    monkeypatch.setattr(
        "oa_knowledge.backfill_mvp.resolve_parser_version",
        lambda engine, settings: "synthetic-parser-v2",
    )

    result = BackfillMVPService(
        settings, factory, _config(), private_config_sha256="a" * 64
    ).run(BackfillMVPRequest(run_id="synthetic-fragmented", target_keys=("done:fragmented",)))

    assert calls == ["mineru", "markitdown"]
    attachment = next(
        path for path in result.output_root.rglob("*.md") if path.name != "_index.md"
    )
    frontmatter = yaml.safe_load(attachment.read_text(encoding="utf-8").split("---", 2)[1])
    assert frontmatter["fallback_reasons"] == ["fragmented_lines"]


def test_two_low_quality_attempts_fail_instead_of_looping_or_publishing_empty_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the two-attempt cap could loop; accepting attempt two publishes junk."""
    settings = _settings(tmp_path)
    factory = _factory()
    _add_item(
        factory,
        settings,
        key="done:bad-quality",
        title="Synthetic internal approval bad quality",
        sender="synthetic.internal",
        payloads=(("bad.pdf", b"synthetic bad quality", "verified"),),
    )
    calls: list[str] = []

    def fake_parse(source, settings, *, engine, output_dir, profile_version):
        calls.append(engine)
        output = output_dir / f"{engine}.md"
        output.write_text("x", encoding="utf-8")
        return ParseResult(
            output,
            engine,
            "synthetic-parser-v2",
            0.2,
            text_length=1,
            profile_version=profile_version,
        )

    monkeypatch.setattr("oa_knowledge.classification.parse_cache.parse_file", fake_parse)
    monkeypatch.setattr(
        "oa_knowledge.backfill_mvp.resolve_parser_version",
        lambda engine, settings: "synthetic-parser-v2",
    )

    result = BackfillMVPService(
        settings, factory, _config(), private_config_sha256="a" * 64
    ).run(BackfillMVPRequest(run_id="synthetic-bad-quality", target_keys=("done:bad-quality",)))

    assert calls == ["mineru", "markitdown"]
    assert result.attachments_converted == 0
    assert result.attachments_failed == 1
    assert [row.code for row in result.exceptions] == ["parse_quality_failed"]
    assert not [path for path in result.output_root.rglob("*.md") if path.name != "_index.md"]


def test_office_documents_do_not_retry_with_an_unsupported_mineru_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blindly applying the visual fallback to Office files invokes an unsupported route."""
    settings = _settings(tmp_path)
    factory = _factory()
    _add_item(
        factory,
        settings,
        key="done:office",
        title="Synthetic internal approval office",
        sender="synthetic.internal",
        payloads=(("office.docx", b"synthetic office", "verified"),),
    )
    calls: list[str] = []

    def fake_parse(source, settings, *, engine, output_dir, profile_version):
        calls.append(engine)
        output = output_dir / "empty.md"
        output.write_text("", encoding="utf-8")
        return ParseResult(
            output,
            engine,
            "synthetic-parser-v2",
            0.1,
            text_length=0,
            profile_version=profile_version,
        )

    monkeypatch.setattr("oa_knowledge.classification.parse_cache.parse_file", fake_parse)
    monkeypatch.setattr(
        "oa_knowledge.backfill_mvp.resolve_parser_version",
        lambda engine, settings: "synthetic-parser-v2",
    )

    result = BackfillMVPService(
        settings, factory, _config(), private_config_sha256="a" * 64
    ).run(BackfillMVPRequest(run_id="synthetic-office", target_keys=("done:office",)))

    assert calls == ["markitdown"]
    assert result.attachments_failed == 1
    assert [row.code for row in result.exceptions] == ["parse_quality_failed"]


def test_legacy_doc_uses_markitdown_then_wv_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WPS binary DOC must get one compatible fallback instead of being skipped."""
    settings = _settings(tmp_path)
    factory = _factory()
    _add_item(
        factory,
        settings,
        key="done:legacy-doc",
        title="Synthetic internal approval legacy DOC",
        sender="synthetic.internal",
        payloads=(("legacy.doc", b"synthetic legacy doc", "verified"),),
    )
    calls: list[str] = []

    def fake_parse(source, settings, *, engine, output_dir, profile_version):
        calls.append(engine)
        output = output_dir / f"{engine}.md"
        body = "" if engine == "markitdown" else (
            "Readable synthetic legacy document body with enough characters to "
            "pass the candidate quality gate."
        )
        output.write_text(body, encoding="utf-8")
        return ParseResult(
            output, engine, f"{engine}-test-v1", 1.0,
            text_length=len(body), profile_version=profile_version,
        )

    monkeypatch.setattr("oa_knowledge.classification.parse_cache.parse_file", fake_parse)
    monkeypatch.setattr(
        "oa_knowledge.backfill_mvp.resolve_parser_version",
        lambda engine, settings: f"{engine}-test-v1",
    )

    result = BackfillMVPService(
        settings, factory, _config(), private_config_sha256="a" * 64
    ).run(BackfillMVPRequest(run_id="synthetic-legacy-doc", target_keys=("done:legacy-doc",)))

    attachment = next(
        path for path in result.output_root.rglob("*.md") if path.name != "_index.md"
    )
    metadata = yaml.safe_load(attachment.read_text(encoding="utf-8").split("---", 2)[1])
    assert calls == ["markitdown", "wv"]
    assert result.attachments_converted == 1
    assert metadata["parse_engine"] == "wv"
    assert metadata["parse_engine_version"] == "wv-test-v1"
    assert metadata["fallback_reasons"] == ["empty_body"]


def test_legacy_doc_falls_back_when_markitdown_rejects_the_binary_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parser exception is a fallback reason, not a batch-stopping exception."""
    from markitdown._exceptions import UnsupportedFormatException

    settings = _settings(tmp_path)
    factory = _factory()
    _add_item(
        factory, settings, key="done:legacy-doc-error",
        title="Synthetic internal approval legacy DOC", sender="synthetic.internal",
        payloads=(("legacy.doc", b"synthetic legacy doc", "verified"),),
    )
    calls: list[str] = []

    def fake_parse(source, settings, *, engine, output_dir, profile_version):
        calls.append(engine)
        if engine == "markitdown":
            raise UnsupportedFormatException("synthetic unsupported legacy DOC")
        output = output_dir / "wv.md"
        output.write_text("Readable synthetic legacy fallback body with enough characters.", encoding="utf-8")
        return ParseResult(output, "wv", "wv-test-v1", 1.0, text_length=60, profile_version=profile_version)

    monkeypatch.setattr("oa_knowledge.classification.parse_cache.parse_file", fake_parse)
    monkeypatch.setattr(
        "oa_knowledge.backfill_mvp.resolve_parser_version",
        lambda engine, settings: f"{engine}-test-v1",
    )

    result = BackfillMVPService(settings, factory, _config(), private_config_sha256="a" * 64).run(
        BackfillMVPRequest(run_id="synthetic-legacy-doc-error", target_keys=("done:legacy-doc-error",))
    )

    assert calls == ["markitdown", "wv"]
    assert result.attachments_converted == 1


def test_v2_classifies_an_unresolved_internal_item_from_attachment_content(
    tmp_path: Path,
) -> None:
    """Leaving the metadata-only service in place sends every unknown internal item to review."""
    settings = _settings(tmp_path)
    factory = _factory()
    _add_item(
        factory,
        settings,
        key="done:content-rule",
        title="Synthetic 立项申请书",
        sender="synthetic.internal",
        payloads=(("evidence.txt", "融资租赁项目评审及投放条件".encode(), "verified"),),
    )

    result = BackfillMVPService(
        settings, factory, _config(), private_config_sha256="a" * 64
    ).run(
        BackfillMVPRequest(
            run_id="synthetic-content-rule-v2", target_keys=("done:content-rule",)
        )
    )

    assert result.classified == 1
    with (result.output_root / "classification.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        row = next(__import__("csv").DictReader(stream))
    assert row["business_category"] == "02_业务项目与投放租后"
    assert row["document_type"] == "立项申请书"
    assert row["decision_source"] == "content_rule"


def test_v2_calls_local_qwen_only_after_title_and_content_rules_are_unresolved(
    tmp_path: Path,
) -> None:
    """Defaulting before the last stage would prevent the required local-Qwen fallback."""
    settings = _settings(tmp_path)
    factory = _factory()
    _add_item(
        factory,
        settings,
        key="done:qwen",
        title="Synthetic 会议纪要",
        sender="synthetic.internal",
        payloads=(("evidence.txt", b"Synthetic ambiguous internal evidence", "verified"),),
    )
    class FakeClient:
        calls = 0

        def chat(self, system_prompt: str, user_prompt: str, *, json_schema: dict):
            self.calls += 1
            return {
                "content": json.dumps(
                    {
                        "business_category": "99_其他内部",
                        "document_type": "会议纪要",
                        "confidence": 0.9,
                        "evidence_quote": "Synthetic evidence outside the existing categories.",
                        "outside_existing_categories": True,
                        "reason": "Synthetic evidence supports the final fallback category.",
                    },
                    ensure_ascii=False,
                ),
                "model": "synthetic-qwen",
                "usage": None,
                "error": None,
                "elapsed_seconds": 0.01,
            }

    client = FakeClient()

    result = BackfillMVPService(
        settings,
        factory,
        _config(),
        private_config_sha256="a" * 64,
        qwen_client=client,
    ).run(
        BackfillMVPRequest(run_id="synthetic-qwen-v2", target_keys=("done:qwen",))
    )

    assert client.calls == 1
    assert result.classified == 1
    with (result.output_root / "classification.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        row = next(__import__("csv").DictReader(stream))
    assert row["business_category"] == "99_其他内部"
    assert row["document_type"] == "会议纪要"
    assert row["decision_source"] == "local_qwen"
    manifest = json.loads(
        (result.output_root / "build_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["qwen_outcomes"] == {"accepted": 1}
    with factory() as session:
        qwen_evidence = session.query(ClassificationEvidence).filter_by(
            evidence_type="local_qwen"
        ).one()
    evidence_value = json.loads(qwen_evidence.value_json)
    assert evidence_value["evidence_excerpt"] == (
        "Synthetic evidence outside the existing categories."
    )
