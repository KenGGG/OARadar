"""Minimal vertical-slice helpers for a real OA candidate backfill."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from oa_knowledge.classification.schemas import PrivateClassificationConfig
from oa_knowledge.db.models import ArchivedFile, OAItem, OAManifestItem


@dataclass(frozen=True, slots=True)
class SampleItem:
    oa_item_key: str
    bucket: str
    reason: str


_SPECIAL_PRIORITY = (
    "attachment_abnormal",
    "no_attachment",
    "multiple_attachments",
    "mixed_initiator",
    "file_transfer",
    "external_document_number",
    "internal_template",
    "no_document_number",
)


def _evenly_spaced(rows: list[SampleItem], count: int) -> list[SampleItem]:
    if count <= 0:
        return []
    if count >= len(rows):
        return rows
    return [rows[(index * len(rows)) // count] for index in range(count)]


def select_representative_items(
    session: Session,
    config: PrivateClassificationConfig,
    sample_size: int,
) -> tuple[SampleItem, ...]:
    """Select a stable ordinary-heavy sample using existing OA fields only."""
    if sample_size < 1:
        raise ValueError("sample_size must be positive")

    manifests = list(
        session.scalars(
            select(OAManifestItem)
            .where(
                OAManifestItem.processing_status != "skipped",
                func.coalesce(OAManifestItem.matched_exclusion_keyword, "") == "",
            )
            .order_by(OAManifestItem.oa_item_key)
        )
    )
    if sample_size > len(manifests):
        raise ValueError("sample_size exceeds available target OA items")

    keys = tuple(row.oa_item_key for row in manifests)
    items = {
        row.oa_item_key: row
        for row in session.scalars(
            select(OAItem).where(
                OAItem.source_channel == "done", OAItem.oa_item_key.in_(keys)
            )
        )
    }
    item_ids = tuple(row.id for row in items.values())
    file_stats = {
        item_id: (count, abnormal)
        for item_id, count, abnormal in session.execute(
            select(
                ArchivedFile.oa_item_id,
                func.count(ArchivedFile.id),
                func.sum(
                    case((ArchivedFile.download_status != "verified", 1), else_=0)
                ),
            )
            .where(ArchivedFile.oa_item_id.in_(item_ids))
            .group_by(ArchivedFile.oa_item_id)
        )
    } if item_ids else {}
    initiator_roles = {
        identifier.casefold(): profile.role
        for identifier, profile in config.initiators.items()
    }
    for profile in config.initiators.values():
        for alias in profile.aliases:
            initiator_roles[alias.casefold()] = profile.role

    by_bucket: dict[str, list[SampleItem]] = {
        bucket: [] for bucket in _SPECIAL_PRIORITY
    }
    ordinary: list[SampleItem] = []
    for manifest in manifests:
        item = items.get(manifest.oa_item_key)
        document_number = item.document_number if item else None
        initiator = (manifest.sender or (item.sender if item else "") or "").strip()
        role = initiator_roles.get(initiator.casefold(), "unknown")
        file_count, abnormal_count = file_stats.get(item.id, (0, 0)) if item else (0, 0)
        title = manifest.title or ""

        bucket = "ordinary"
        reason = "stable ordinary target"
        if abnormal_count:
            bucket, reason = "attachment_abnormal", "attachment status is not verified"
        elif manifest.no_attachment_confirmed:
            bucket, reason = "no_attachment", "manifest confirms no attachment"
        elif file_count >= 3:
            bucket, reason = "multiple_attachments", f"attachment_count={file_count}"
        elif role == "mixed":
            bucket, reason = "mixed_initiator", "configured initiator role=mixed"
        elif re.search(r"(?:文件传阅|传阅件|传阅-|【传阅】)", title):
            bucket, reason = "file_transfer", "title contains a transfer marker"
        elif document_number and any(
            re.search(rule.pattern, document_number)
            for rule in config.document_number_issuers
        ):
            bucket, reason = "external_document_number", "document number matches issuer rule"
        elif any(re.search(rule.pattern, title) for rule in config.title_templates):
            bucket, reason = "internal_template", "title matches configured template"
        elif not document_number and role == "unknown":
            bucket, reason = "no_document_number", "unknown initiator and no document number"

        sample = SampleItem(manifest.oa_item_key, bucket, reason)
        if bucket == "ordinary":
            ordinary.append(sample)
        else:
            by_bucket[bucket].append(sample)

    special_limit = min(35, sample_size * 35 // 100)
    special: list[SampleItem] = []
    round_index = 0
    while len(special) < special_limit:
        added = False
        for bucket in _SPECIAL_PRIORITY:
            rows = by_bucket[bucket]
            if round_index < len(rows) and len(special) < special_limit:
                special.append(rows[round_index])
                added = True
        if not added:
            break
        round_index += 1

    ordinary_needed = sample_size - len(special)
    selected = [*_evenly_spaced(ordinary, ordinary_needed), *special]
    if len(selected) < sample_size:
        chosen = {row.oa_item_key for row in selected}
        remaining = [
            row
            for bucket in _SPECIAL_PRIORITY
            for row in by_bucket[bucket]
            if row.oa_item_key not in chosen
        ]
        selected.extend(remaining[: sample_size - len(selected)])
    if len(selected) != sample_size:
        raise ValueError("not enough target OA items for requested sample")
    return tuple(sorted(selected, key=lambda row: row.oa_item_key))
