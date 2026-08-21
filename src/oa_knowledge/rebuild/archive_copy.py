"""Verified no-clobber copies of ready evidence into a clean rebuild tree."""

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


def _copy_to_temporary(source: Path, destination_dir: Path) -> Path:
    descriptor, name = tempfile.mkstemp(dir=destination_dir, prefix=".rebuild-copy-", suffix=".tmp")
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


def _find_output(session: Session, row: InventoryRow, run_id: int) -> RebuildOutput | None:
    return session.scalar(select(RebuildOutput).where(
        RebuildOutput.run_id == run_id, RebuildOutput.target_relpath == row.destination_relpath,
    ))


def _ensure_pending(session: Session, row: InventoryRow, *, run_id: int) -> RebuildOutput:
    """Commit an intent before final publication; recover a concurrent insert."""
    output = _find_output(session, row, run_id)
    if output is None:
        output = RebuildOutput(
            run_id=run_id, oa_item_id=row.item_id, source_file_id=row.file_id,
            kind="original", target_relpath=row.destination_relpath, sha256=row.sha256,
            status="pending", error_code=None,
        )
        session.add(output)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            output = _find_output(session, row, run_id)
            if output is None:
                raise
    if output.status != "pending":
        output.status, output.error_code, output.sha256 = "pending", None, row.sha256
        session.commit()
    return output


def _set_status(session: Session, output: RebuildOutput, *, status: str, error_code: str | None) -> RebuildOutput:
    output.status, output.error_code = status, error_code
    session.commit()
    return output


def _record_failure(session: Session, row: InventoryRow, *, run_id: int, error_code: str) -> RebuildOutput:
    return _set_status(
        session, _ensure_pending(session, row, run_id=run_id), status="failed", error_code=error_code,
    )


def _unlink_if_owned(target: Path, published_inode: tuple[int, int]) -> None:
    """Compensate only a final name still pointing at our published inode."""
    try:
        stat_result = target.stat()
    except OSError:
        return
    if (stat_result.st_dev, stat_result.st_ino) == published_inode:
        target.unlink(missing_ok=True)
        _fsync_directory(target.parent)


def _publish_no_clobber(temporary: Path, target: Path) -> tuple[bool, tuple[int, int] | None]:
    """Atomically create the final name from target-local temporary bytes."""
    stat_result = temporary.stat()
    try:
        os.link(temporary, target)
    except FileExistsError:
        return False, None
    _fsync_directory(target.parent)
    return True, (stat_result.st_dev, stat_result.st_ino)


def copy_inventory_row(session: Session, settings: Settings, row: InventoryRow, *, run_id: int) -> RebuildOutput:
    """Durably ledger and atomically publish one verified ready original.

    The function owns commits for its ledger transition, so a caller rollback
    after a returned success cannot remove that success record.
    """
    if row.status != "ready":
        raise ValueError("only ready inventory rows may be copied")
    try:
        source = resolve_data_path(settings.data_root, row.source_relpath, allowed_prefixes=DONE_ARCHIVE_PREFIXES)
    except ValueError:
        return _record_failure(session, row, run_id=run_id, error_code="SOURCE_UNSAFE")
    if not _file_matches(source, size_bytes=row.size_bytes, sha256=row.sha256):
        error = "SOURCE_MISSING" if not source.exists() else "SOURCE_SIZE_MISMATCH"
        if source.exists() and source.is_file() and source.stat().st_size == row.size_bytes:
            error = "SOURCE_HASH_MISMATCH"
        return _record_failure(session, row, run_id=run_id, error_code=error)

    target = resolve_rebuild_path(settings, row.destination_relpath)
    output = _find_output(session, row, run_id)
    if output is not None and output.status == "success":
        if _file_matches(target, size_bytes=row.size_bytes, sha256=row.sha256):
            return output
        if target.exists():
            return _set_status(session, output, status="failed", error_code="TARGET_CONFLICT")

    output = _ensure_pending(session, row, run_id=run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _file_matches(target, size_bytes=row.size_bytes, sha256=row.sha256):
            return _set_status(session, output, status="success", error_code=None)
        return _set_status(session, output, status="failed", error_code="TARGET_CONFLICT")

    temporary: Path | None = None
    published_inode: tuple[int, int] | None = None
    try:
        temporary = _copy_to_temporary(source, target.parent)
        if not _file_matches(temporary, size_bytes=row.size_bytes, sha256=row.sha256):
            return _set_status(session, output, status="failed", error_code="COPY_VERIFICATION_FAILED")
        if not _file_matches(source, size_bytes=row.size_bytes, sha256=row.sha256):
            return _set_status(session, output, status="failed", error_code="SOURCE_CHANGED")
        published, published_inode = _publish_no_clobber(temporary, target)
        if not published:
            if _file_matches(target, size_bytes=row.size_bytes, sha256=row.sha256):
                return _set_status(session, output, status="success", error_code=None)
            return _set_status(session, output, status="failed", error_code="TARGET_CONFLICT")
        temporary.unlink()
        temporary = None
        if not _file_matches(target, size_bytes=row.size_bytes, sha256=row.sha256):
            _unlink_if_owned(target, published_inode)
            return _set_status(session, output, status="failed", error_code="COPY_VERIFICATION_FAILED")
        try:
            return _set_status(session, output, status="success", error_code=None)
        except Exception:
            _unlink_if_owned(target, published_inode)
            raise
    except OSError:
        return _set_status(session, output, status="failed", error_code="COPY_FAILED")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
