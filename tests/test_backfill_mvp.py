from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from oa_knowledge.backfill_mvp import SampleItem, select_representative_items
from oa_knowledge.classification.schemas import PrivateClassificationConfig
from oa_knowledge.db.models import ArchivedFile, Base, OAItem, OAManifestItem


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

