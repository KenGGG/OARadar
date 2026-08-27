from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine
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
from oa_knowledge.db.models import ArchivedFile, Base, OAItem, OAManifestItem
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
        ("external_number", "Synthetic external notice", "synthetic.internal", "SYN-AUTH-7", 1, "verified", False),
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
    after = {
        path.relative_to(settings.originals_root).as_posix(): path.read_bytes()
        for path in settings.originals_root.rglob("*")
        if path.is_file()
    }
    assert after == before


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
        output.write_text("faithful parsed body\n", encoding="utf-8")
        return ParseResult(
            output,
            engine,
            "synthetic-parser-v1",
            1.0,
            text_length=20,
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
    assert {path.read_text(encoding="utf-8") for path in attachments} == {
        attachments[0].read_text(encoding="utf-8")
    }


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
        "parse_failed",
        "unsupported_file_type",
    }
    assert any("later evidence" in path.read_text(encoding="utf-8") for path in result.output_root.rglob("*.md"))
