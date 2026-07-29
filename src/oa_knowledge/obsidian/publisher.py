"""Publish pipeline — publishes classified items to Obsidian vault."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.constants import PipelineStatus
from oa_knowledge.db.models import OAItem
from oa_knowledge.obsidian.lint import lint_note
from oa_knowledge.obsidian.source_note import build_frontmatter, build_source_note


class PublishPipeline:
    """Publishes classified OA items as Obsidian source notes."""

    def __init__(self, settings: Settings, vault_root: Path | None = None) -> None:
        self.settings = settings
        self.vault_root = vault_root or (settings.data_root / "vault")

    def publish_classified_items(self, limit: int = 50) -> dict:
        """Publish all classified but unpublished items to the Obsidian vault.

        Returns summary with published, skipped, failed counts.
        """
        engine = __import__("oa_knowledge.db.engine", fromlist=["create_db_engine"]).create_db_engine(
            self.settings.database_path
        )

        summary = {"published": 0, "skipped": 0, "failed": 0, "errors": []}

        with Session(engine) as session:
            item_ids = (
                session.execute(
                    select(OAItem.id).where(
                        OAItem.pipeline_status == PipelineStatus.CLASSIFIED
                    )
                    .order_by(OAItem.id)
                    .limit(limit)
                )
                .scalars()
                .all()
            )

        for item_id in item_ids:
            try:
                with Session(engine) as session:
                    item = session.get(OAItem, item_id)
                    if item is None or item.pipeline_status != PipelineStatus.CLASSIFIED:
                        summary["skipped"] += 1
                        continue
                    self._publish_single(item, session)
                    session.commit()
                    summary["published"] += 1
            except Exception as exc:
                summary["failed"] += 1
                summary["errors"].append(f"item={item_id}: {exc}")

        return summary

    def _publish_single(self, item: OAItem, db_session: Session) -> Path:
        """Publish a single item to the vault."""
        # Staging directory for atomic publish
        staging_id = uuid4().hex[:12]
        staging_dir = self.vault_root / ".staging" / staging_id
        staging_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Determine date-based path
            received = item.received_at or datetime.now(timezone.utc)
            year_month = received.strftime("%Y/%m")
            item_dir = staging_dir / "raw" / "sources" / "oa" / year_month / item.oa_item_key

            # Build frontmatter
            classifications = []  # Would come from DB in production
            frontmatter = build_frontmatter(
                item_id=item.id,
                title=item.title,
                classifications=classifications,
                source_channel=item.source_channel,
                issuer=item.sender or "",
                document_number=item.document_number or "",
                received_at=str(item.received_at) if item.received_at else "",
            )

            # Build source note body
            body = build_source_note(
                title=item.title,
                source_channel=item.source_channel,
                issuer=item.sender or "",
                document_number=item.document_number or "",
                received_at=str(item.received_at) if item.received_at else "",
            )

            # Write source.md
            source_path = item_dir / "source.md"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(f"{frontmatter}\n\n{body}", encoding="utf-8")

            lint_result = lint_note(source_path, staging_dir)
            if not lint_result.valid:
                raise ValueError(f"Obsidian lint failed: {'; '.join(lint_result.errors)}")

            # Write manifest
            manifest = {
                "item_id": item.id,
                "oa_item_key": item.oa_item_key,
                "published_at": datetime.now(timezone.utc).isoformat(),
                "vault_path": str(source_path.relative_to(staging_dir)),
                "version": staging_id,
            }
            (item_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            # Atomic move from staging to vault
            dest_path = self.vault_root / source_path.relative_to(staging_dir)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), str(dest_path))
            staged_manifest = item_dir / "manifest.json"
            if staged_manifest.exists():
                shutil.move(
                    str(staged_manifest),
                    str(dest_path.parent / "manifest.json"),
                )

            # Clean up staging
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

            # Update pipeline status
            item.pipeline_status = PipelineStatus.PUBLISHED

            return dest_path

        except Exception:
            # Clean up staging on failure
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            raise
