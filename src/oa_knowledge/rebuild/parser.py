"""Parse verified rebuilt originals without touching the live archive tree."""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, RebuildOutput
from oa_knowledge.parsers.router import is_supported_file, parse_file
from oa_knowledge.rebuild.paths import resolve_rebuild_path


@dataclass(frozen=True)
class RebuildParseResult:
    source_file_id: int
    status: str
    engine: str
    output_relpath: str | None
    source_sha256: str
    product_sha256: str | None
    error_code: str | None


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


def _tree_sha256(directory: Path) -> str | None:
    """Hash every regular parser product deterministically, including assets."""
    if not directory.is_dir():
        return None
    digest = hashlib.sha256()
    found = False
    for path in sorted(directory.rglob("*"), key=lambda value: value.as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        found = True
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        content = hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(content)
    return digest.hexdigest() if found else None


def _fsync_tree(directory: Path) -> None:
    for path in directory.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            except OSError:
                pass
    for path in sorted((directory, *directory.rglob("*")), key=lambda value: len(value.parts), reverse=True):
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            continue
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)


def _promote_no_clobber(staging: Path, target: Path) -> bool:
    """Atomically rename a target-local directory, refusing an existing name."""
    rename_noreplace = 1
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.renameat2(-100, os.fsencode(staging), -100, os.fsencode(target), rename_noreplace)
    except (AttributeError, OSError):
        raise OSError("atomic no-clobber directory promotion is unavailable")
    if result == 0:
        _fsync_tree(target.parent)
        return True
    error = ctypes.get_errno()
    if error == getattr(os, "EEXIST", 17):
        return False
    raise OSError(error, os.strerror(error), target)


def _source_output(session: Session, settings: Settings, run_id: int, source_file_id: int):
    """Return the newest copied original which remains valid in the rebuild tree."""
    rows = session.execute(
        select(RebuildOutput, ArchivedFile)
        .join(ArchivedFile, RebuildOutput.source_file_id == ArchivedFile.id)
        .where(
            RebuildOutput.run_id == run_id,
            RebuildOutput.source_file_id == source_file_id,
            RebuildOutput.kind == "original",
            RebuildOutput.status == "success",
        )
        .order_by(RebuildOutput.id.desc())
    ).all()
    for output, source in rows:
        if (
            source.size_bytes is not None
            and source.sha256
            and output.sha256 == source.sha256
            and _file_matches(
                resolve_rebuild_path(settings, output.target_relpath),
                size_bytes=source.size_bytes,
                sha256=source.sha256,
            )
        ):
            return output, source
    return None


def _result(source_file_id: int, status: str, engine: str, *, relpath: str | None,
            source_sha256: str, product_sha256: str | None, error_code: str | None) -> RebuildParseResult:
    return RebuildParseResult(source_file_id, status, engine, relpath, source_sha256, product_sha256, error_code)


def _record_output(session: Session, *, run_id: int, source: ArchivedFile, relpath: str,
                   sha256: str | None, status: str, error_code: str | None) -> RebuildOutput:
    output = session.scalar(select(RebuildOutput).where(
        RebuildOutput.run_id == run_id, RebuildOutput.target_relpath == relpath,
    ))
    if output is None:
        output = RebuildOutput(
            run_id=run_id, oa_item_id=source.oa_item_id, source_file_id=source.id,
            kind="parse", target_relpath=relpath, sha256=sha256,
            status=status, error_code=error_code,
        )
        session.add(output)
    else:
        output.sha256, output.status, output.error_code = sha256, status, error_code
    session.commit()
    return output


def _remove_prior_successes(session: Session, settings: Settings, *, run_id: int,
                            source_file_id: int, keep_relpath: str) -> None:
    """Keep one current-run successful parse ledger result for a source."""
    previous = list(session.scalars(select(RebuildOutput).where(
        RebuildOutput.run_id == run_id,
        RebuildOutput.source_file_id == source_file_id,
        RebuildOutput.kind == "parse",
        RebuildOutput.status == "success",
        RebuildOutput.target_relpath != keep_relpath,
    )))
    prefix = f"parse/{run_id}/{source_file_id}/"
    for output in previous:
        if output.target_relpath.startswith(prefix):
            target = resolve_rebuild_path(settings, output.target_relpath)
            if target.is_dir():
                shutil.rmtree(target)
        session.delete(output)
    if previous:
        session.commit()


def parse_rebuilt_source(
    session: Session, settings: Settings, run_id: int, source_file_id: int,
) -> RebuildParseResult:
    """Parse exactly one copied, hash-verified original into ``parse/``.

    Ledger writes use an independent session so a caller rollback cannot erase
    a published parse product.  The live ``ArchivedFile.local_relpath`` is
    deliberately never resolved or read.
    """
    with Session(bind=session.get_bind(), expire_on_commit=False) as ledger:
        resolved = _source_output(ledger, settings, run_id, source_file_id)
        if resolved is None:
            source = ledger.get(ArchivedFile, source_file_id)
            if source is not None:
                _record_output(
                    ledger, run_id=run_id, source=source,
                    relpath=f"parse/{run_id}/{source_file_id}/unavailable",
                    sha256=None, status="failed", error_code="REBUILT_ORIGINAL_UNAVAILABLE",
                )
            return _result(source_file_id, "failed", "none", relpath=None,
                           source_sha256=source.sha256 if source and source.sha256 else "",
                           product_sha256=None, error_code="REBUILT_ORIGINAL_UNAVAILABLE")
        original, source = resolved
        source_path = resolve_rebuild_path(settings, original.target_relpath)
        source_sha = source.sha256 or ""
        base_relpath = f"parse/{run_id}/{source_file_id}/{source_sha}"

        if not is_supported_file(source_path, settings):
            relpath = f"{base_relpath}/unsupported"
            existing = ledger.scalar(select(RebuildOutput).where(
                RebuildOutput.run_id == run_id, RebuildOutput.target_relpath == relpath,
            ))
            if existing is None or existing.status != "success":
                _record_output(ledger, run_id=run_id, source=source, relpath=relpath,
                               sha256=None, status="success", error_code=None)
            _remove_prior_successes(ledger, settings, run_id=run_id,
                                    source_file_id=source_file_id, keep_relpath=relpath)
            return _result(source_file_id, "unsupported", "none", relpath=None,
                           source_sha256=source_sha, product_sha256=None,
                           error_code="UNSUPPORTED_FILE_TYPE")

        existing = list(ledger.scalars(select(RebuildOutput).where(
            RebuildOutput.run_id == run_id, RebuildOutput.source_file_id == source_file_id,
            RebuildOutput.kind == "parse", RebuildOutput.status == "success",
            RebuildOutput.target_relpath.startswith(f"{base_relpath}/"),
        )))
        for output in existing:
            target = resolve_rebuild_path(settings, output.target_relpath)
            product_sha = _tree_sha256(target)
            if product_sha is not None and product_sha == output.sha256:
                engine = Path(output.target_relpath).name
                return _result(source_file_id, "success", engine, relpath=output.target_relpath,
                               source_sha256=source_sha, product_sha256=product_sha, error_code=None)

        parse_root = resolve_rebuild_path(settings, "parse")
        parse_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".rebuild-parse-", dir=parse_root))
        try:
            parsed = parse_file(source_path, settings, output_dir=staging)
            product = parsed.output_path.resolve()
            if not product.is_file() or staging not in (product, *product.parents):
                raise ValueError("parser output escaped rebuild staging")
            product_sha = _tree_sha256(staging)
            if product_sha is None:
                raise ValueError("parser produced no regular files")
            _fsync_tree(staging)
            relpath = f"{base_relpath}/{parsed.engine}"
            target = resolve_rebuild_path(settings, relpath)
            target.parent.mkdir(parents=True, exist_ok=True)

            # Phase 2 may reconcile a copied original while parsing.  Observe
            # it again but never mutate that source ledger row from here.
            current = _source_output(ledger, settings, run_id, source_file_id)
            if current is None or current[0].id != original.id:
                _record_output(ledger, run_id=run_id, source=source,
                               relpath=f"{base_relpath}/failed", sha256=None,
                               status="failed", error_code="REBUILT_ORIGINAL_CHANGED")
                return _result(source_file_id, "failed", parsed.engine, relpath=None,
                               source_sha256=source_sha, product_sha256=None,
                               error_code="REBUILT_ORIGINAL_CHANGED")

            try:
                _record_output(ledger, run_id=run_id, source=source, relpath=relpath,
                               sha256=product_sha, status="pending", error_code=None)
            except IntegrityError:
                ledger.rollback()
            if not _promote_no_clobber(staging, target):
                if _tree_sha256(target) != product_sha:
                    _record_output(ledger, run_id=run_id, source=source, relpath=relpath,
                                   sha256=product_sha, status="failed", error_code="TARGET_CONFLICT")
                    return _result(source_file_id, "failed", parsed.engine, relpath=None,
                                   source_sha256=source_sha, product_sha256=None,
                                   error_code="TARGET_CONFLICT")
            else:
                staging = None  # promoted; final cleanup must not remove it
            _record_output(ledger, run_id=run_id, source=source, relpath=relpath,
                           sha256=product_sha, status="success", error_code=None)
            _remove_prior_successes(ledger, settings, run_id=run_id,
                                    source_file_id=source_file_id, keep_relpath=relpath)
            return _result(source_file_id, "success", parsed.engine, relpath=relpath,
                           source_sha256=source_sha, product_sha256=product_sha, error_code=None)
        except Exception as exc:  # noqa: BLE001 - every engine failure is source-local
            # A parser failure is isolated to this source.  No source or other
            # parse output is downgraded as part of failure handling.
            relpath = f"{base_relpath}/failed"
            _record_output(ledger, run_id=run_id, source=source, relpath=relpath,
                           sha256=None, status="failed", error_code=type(exc).__name__.upper())
            return _result(source_file_id, "failed", "none", relpath=None,
                           source_sha256=source_sha, product_sha256=None,
                           error_code=type(exc).__name__.upper())
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
