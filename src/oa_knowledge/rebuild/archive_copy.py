"""Verified atomic copies of ready original evidence into a clean rebuild tree."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import RebuildOutput
from oa_knowledge.done_archive import DONE_ARCHIVE_PREFIXES
from oa_knowledge.rebuild.inventory import InventoryRow
from oa_knowledge.rebuild.paths import resolve_rebuild_path
from oa_knowledge.storage_paths import resolve_data_path


def _file_matches(path: Path, *, size_bytes: int, sha256: str) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != size_bytes:
            return False
    except OSError:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == sha256


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _persist_output(session: Session, output: RebuildOutput) -> RebuildOutput:
    """Use the run/target uniqueness constraint as the idempotency boundary."""
    try:
        with session.begin_nested():
            session.add(output)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(RebuildOutput).where(
                RebuildOutput.run_id == output.run_id,
                RebuildOutput.target_relpath == output.target_relpath,
            )
        )
        if existing is None:
            raise
        return existing
    return output


def _record_failure(
    session: Session, row: InventoryRow, *, run_id: int, error_code: str
) -> RebuildOutput:
    return _persist_output(
        session,
        RebuildOutput(
            run_id=run_id,
            oa_item_id=row.item_id,
            source_file_id=row.file_id,
            kind="original",
            target_relpath=row.destination_relpath,
            sha256=None,
            status="failed",
            error_code=error_code,
        ),
    )


def _copy_to_temporary(source: Path, destination_dir: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=destination_dir, prefix=".rebuild-copy-", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            descriptor = -1
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        if descriptor != -1:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def copy_inventory_row(
    session: Session, settings: Settings, row: InventoryRow, *, run_id: int
) -> RebuildOutput:
    """Copy one ready original only after source and target verification.

    The database uniqueness constraint makes duplicate requests for one run and
    target resumable; source evidence is nevertheless checked on every call.
    """
    if row.status != "ready":
        raise ValueError("only ready inventory rows may be copied")
    try:
        source = resolve_data_path(
            settings.data_root, row.source_relpath, allowed_prefixes=DONE_ARCHIVE_PREFIXES
        )
    except ValueError:
        return _record_failure(session, row, run_id=run_id, error_code="SOURCE_UNSAFE")
    if not _file_matches(source, size_bytes=row.size_bytes, sha256=row.sha256):
        error = "SOURCE_MISSING" if not source.exists() else "SOURCE_SIZE_MISMATCH"
        if source.exists() and source.is_file() and source.stat().st_size == row.size_bytes:
            error = "SOURCE_HASH_MISMATCH"
        return _record_failure(session, row, run_id=run_id, error_code=error)

    target = resolve_rebuild_path(settings, row.destination_relpath)
    existing = session.scalar(
        select(RebuildOutput).where(
            RebuildOutput.run_id == run_id,
            RebuildOutput.target_relpath == row.destination_relpath,
        )
    )
    if existing is not None:
        return existing
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _file_matches(target, size_bytes=row.size_bytes, sha256=row.sha256):
            return _persist_output(
                session,
                RebuildOutput(
                    run_id=run_id, oa_item_id=row.item_id, source_file_id=row.file_id,
                    kind="original", target_relpath=row.destination_relpath, sha256=row.sha256,
                    status="success",
                ),
            )
        return _record_failure(session, row, run_id=run_id, error_code="TARGET_CONFLICT")

    temporary: Path | None = None
    try:
        temporary = _copy_to_temporary(source, target.parent)
        if not _file_matches(temporary, size_bytes=row.size_bytes, sha256=row.sha256):
            return _record_failure(session, row, run_id=run_id, error_code="COPY_VERIFICATION_FAILED")
        if not _file_matches(source, size_bytes=row.size_bytes, sha256=row.sha256):
            return _record_failure(session, row, run_id=run_id, error_code="SOURCE_CHANGED")
        # Recheck immediately before replacement: a different target is never overwritten.
        if target.exists():
            if _file_matches(target, size_bytes=row.size_bytes, sha256=row.sha256):
                return _persist_output(
                    session,
                    RebuildOutput(
                        run_id=run_id, oa_item_id=row.item_id, source_file_id=row.file_id,
                        kind="original", target_relpath=row.destination_relpath, sha256=row.sha256,
                        status="success",
                    ),
                )
            return _record_failure(session, row, run_id=run_id, error_code="TARGET_CONFLICT")
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(target.parent)
        if not _file_matches(target, size_bytes=row.size_bytes, sha256=row.sha256):
            target.unlink(missing_ok=True)
            return _record_failure(session, row, run_id=run_id, error_code="COPY_VERIFICATION_FAILED")
        return _persist_output(
            session,
            RebuildOutput(
                run_id=run_id, oa_item_id=row.item_id, source_file_id=row.file_id,
                kind="original", target_relpath=row.destination_relpath, sha256=row.sha256,
                status="success",
            ),
        )
    except OSError:
        return _record_failure(session, row, run_id=run_id, error_code="COPY_FAILED")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
