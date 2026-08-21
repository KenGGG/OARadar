"""Safe, local SQLite state snapshots for the rebuild cutover."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from oa_knowledge.rebuild.validation import ValidationCheck


def _alembic_head() -> str:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "oa_knowledge" / "db" / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


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


def _is_rebuild_path(value: object, prefix: str) -> bool:
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    parts = value.split("/")
    return len(parts) > 1 and all(part not in {"", ".", ".."} for part in parts)


def _active_path_count(connection: sqlite3.Connection) -> int:
    invalid_files = sum(
        not _is_rebuild_path(row[0], "archive/")
        for row in connection.execute(
            "SELECT local_relpath FROM files WHERE local_relpath IS NOT NULL"
        )
    )
    invalid_exports = sum(
        not _is_rebuild_path(source_relpath, "archive/")
        or not _is_rebuild_path(markdown_relpath, "markdown/")
        or (assets_relpath is not None and not _is_rebuild_path(assets_relpath, "markdown/"))
        for source_relpath, markdown_relpath, assets_relpath in connection.execute(
            "SELECT source_relpath, markdown_relpath, assets_relpath FROM markdown_exports "
            "WHERE status IN ('queued', 'running', 'success')"
        )
    )
    return invalid_files + invalid_exports


def validate_database_copy(database_path: Path) -> list[ValidationCheck]:
    """Return redacted SQLite, foreign-key, and schema-head checks for a copy."""
    integrity: str | None = None
    foreign_keys: str | None = None
    revision: str | None = None
    active_paths: int | None = None
    try:
        with sqlite3.connect(f"file:{database_path.resolve().as_posix()}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = "ok" if not connection.execute("PRAGMA foreign_key_check").fetchall() else "failed"
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = row[0] if row is not None else None
            active_paths = _active_path_count(connection)
    except (OSError, sqlite3.DatabaseError):
        pass
    return [
        ValidationCheck("SQLITE_INTEGRITY", integrity == "ok", 1, int(integrity == "ok")),
        ValidationCheck("SQLITE_FOREIGN_KEYS", foreign_keys == "ok", 1, int(foreign_keys == "ok")),
        ValidationCheck("ALEMBIC_HEAD", revision == _alembic_head(), 1, int(revision == _alembic_head())),
        ValidationCheck("ACTIVE_RUNTIME_PATHS", active_paths == 0, 0, active_paths),
    ]


def _require_success_path(kind: str, target_relpath: object) -> str:
    prefix = {
        "original": "archive/",
        "parse": "parse/",
        "body_markdown": "markdown/",
        "attachment_markdown": "markdown/",
        "item_index": "markdown/",
    }.get(kind)
    if prefix is None or not _is_rebuild_path(target_relpath, prefix):
        raise RuntimeError("successful rebuild ledger path is invalid")
    return target_relpath


def _map_export_rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...],
    *,
    column: str,
    value: str | None,
    changed: set[int],
) -> None:
    for export_id, current in connection.execute(query, parameters):
        if current != value:
            connection.execute(f"UPDATE markdown_exports SET {column} = ? WHERE id = ?", (value, export_id))
            changed.add(export_id)


def apply_rebuilt_ledger(database_path: Path, run_id: int) -> dict[str, int]:
    """Apply one run's successful rebuilt paths to a copied runtime database only."""
    database_path = database_path.resolve()
    if not database_path.is_file() or database_path.is_symlink():
        raise ValueError("database copy must be a regular file")
    files_changed = parse_artifacts_changed = 0
    exports_changed: set[int] = set()
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        run = connection.execute(
            "SELECT 1 FROM pipeline_runs WHERE id = ? AND pipeline_type = 'data_rebuild'", (run_id,)
        ).fetchone()
        if run is None:
            raise ValueError("rebuild run was not found")
        try:
            outputs = list(connection.execute(
                "SELECT kind, source_file_id, oa_item_id, target_relpath FROM rebuild_outputs "
                "WHERE run_id = ? AND status = 'success' ORDER BY id",
                (run_id,),
            ))
            for kind, source_file_id, item_id, raw_target in outputs:
                target = _require_success_path(kind, raw_target)
                if kind == "original":
                    if source_file_id is None:
                        raise RuntimeError("successful original is missing its source file")
                    current = connection.execute(
                        "SELECT local_relpath FROM files WHERE id = ?", (source_file_id,)
                    ).fetchone()
                    if current is None:
                        raise RuntimeError("successful original source file is missing")
                    if current[0] != target:
                        connection.execute("UPDATE files SET local_relpath = ? WHERE id = ?", (target, source_file_id))
                        files_changed += 1
                    _map_export_rows(
                        connection,
                        "SELECT id, source_relpath FROM markdown_exports WHERE source_file_id = ?",
                        (source_file_id,), column="source_relpath", value=target, changed=exports_changed,
                    )
                elif kind == "parse":
                    if source_file_id is None:
                        raise RuntimeError("successful parse is missing its source file")
                    artifact_ids = [row[0] for row in connection.execute(
                        "SELECT parse_artifacts.id FROM parse_artifacts "
                        "JOIN parse_jobs ON parse_jobs.id = parse_artifacts.parse_job_id "
                        "WHERE parse_jobs.file_id = ? AND parse_artifacts.lifecycle_status = 'valid'",
                        (source_file_id,),
                    )]
                    for artifact_id in artifact_ids:
                        current = connection.execute(
                            "SELECT output_relpath FROM parse_artifacts WHERE id = ?", (artifact_id,)
                        ).fetchone()[0]
                        if current != target:
                            connection.execute(
                                "UPDATE parse_artifacts SET output_relpath = ? WHERE id = ?", (target, artifact_id)
                            )
                            parse_artifacts_changed += 1
                    connection.execute(
                        "UPDATE parse_jobs SET output_relpath = ? WHERE file_id = ? AND status = 'success'",
                        (target, source_file_id),
                    )
                elif kind in {"body_markdown", "attachment_markdown"}:
                    if source_file_id is None:
                        raise RuntimeError("successful markdown is missing its source file")
                    _map_export_rows(
                        connection,
                        "SELECT id, markdown_relpath FROM markdown_exports "
                        "WHERE source_file_id = ? AND document_kind = 'attachment'",
                        (source_file_id,), column="markdown_relpath", value=target, changed=exports_changed,
                    )
                    assets = f"{target.rsplit('/', 1)[0]}/assets/{source_file_id}"
                    _map_export_rows(
                        connection,
                        "SELECT id, assets_relpath FROM markdown_exports "
                        "WHERE source_file_id = ? AND document_kind = 'attachment'",
                        (source_file_id,), column="assets_relpath", value=assets, changed=exports_changed,
                    )
                elif kind == "item_index":
                    _map_export_rows(
                        connection,
                        "SELECT id, markdown_relpath FROM markdown_exports "
                        "WHERE oa_item_id = ? AND document_kind = 'item_index'",
                        (item_id,), column="markdown_relpath", value=target, changed=exports_changed,
                    )
            if _active_path_count(connection):
                raise RuntimeError("retired filesystem paths remain in active runtime rows")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return {
        "files": files_changed,
        "markdown_exports": len(exports_changed),
        "parse_artifacts": parse_artifacts_changed,
    }


def backup_live_database(source: Path, target: Path) -> None:
    """Snapshot an open SQLite database and atomically promote only a valid copy."""
    source = source.resolve()
    target = target.resolve()
    if source == target:
        raise ValueError("database copy target must differ from source")
    if not source.is_file() or source.is_symlink():
        raise ValueError("database copy source must be a regular file")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    os.close(descriptor)
    temporary.unlink()
    try:
        with (
            sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as source_connection,
            sqlite3.connect(temporary) as target_connection,
        ):
            source_connection.backup(target_connection)
        checks = validate_database_copy(temporary)
        if not all(check.ok for check in checks if check.code != "ACTIVE_RUNTIME_PATHS"):
            raise RuntimeError("database copy validation failed")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
