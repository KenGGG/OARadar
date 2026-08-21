"""Explicit, bounded directory cutover for a verified local rebuild.

This module deliberately has no delete operation.  A cutover is exactly two
same-filesystem directory renames and, if anything after the first rename
fails, the inverse two renames.  The CLI is the only caller that turns a
path-bound, short-lived authorization token into ``authorized=True``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import PipelineRun
from oa_knowledge.rebuild.paths import resolve_rebuild_root
from oa_knowledge.rebuild.state_copy import (
    _PREPARED_COPY_APPLICATION_ID,
    _alembic_head,
    validate_database_copy,
)
from oa_knowledge.rebuild.validation import validate_rebuild, validation_passed

KNOWN_USER_UNITS = (
    "oaradar-web.service",
    "oaradar-worker.service",
    "oaradar-markdown-worker.service",
    "oaradar-hourly.timer",
    "oaradar-nightly.timer",
)
_AUTHORIZATION_KEY_ENV = "OA_REBUILD_CUTOVER_AUTHORIZATION_KEY"
_EXTERNAL_BACKUP_PATH_ENV = "OA_REBUILD_CUTOVER_BACKUP_PATH"
_TOKEN_MAX_AGE = timedelta(minutes=10)
_BACKUP_MAX_AGE = timedelta(hours=24)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class CutoverError(RuntimeError):
    """Base class whose error code is safe to expose from the local CLI."""

    error_code = "CUTOVER_FAILED"


class CutoverPreflightError(CutoverError):
    error_code = "CUTOVER_PREFLIGHT_FAILED"


class CutoverAuthorizationError(CutoverError):
    error_code = "CUTOVER_AUTHORIZATION_INVALID"


class CutoverSmokeError(CutoverError):
    error_code = "CUTOVER_SMOKE_FAILED"


class CutoverRollbackError(CutoverError):
    error_code = "CUTOVER_ROLLBACK_FAILED"


@dataclass(frozen=True)
class CutoverPlan:
    """The complete, bounded set of local resources a cutover may touch."""

    live_root: Path
    rebuilt_root: Path
    legacy_root: Path
    units: tuple[str, str, str, str, str]
    validation_ok: bool = False
    database_backup_ok: bool = False
    external_backup_ok: bool = False
    git_clean: bool = False
    units_discovered: bool = False
    same_filesystem: bool = False
    legacy_available: bool = False

    @property
    def preflight_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if self.units != KNOWN_USER_UNITS:
            errors.append("SYSTEMD_UNITS_INVALID")
        if not self.validation_ok:
            errors.append("VALIDATION_DIRTY")
        if not self.database_backup_ok:
            errors.append("DATABASE_BACKUP_MISSING")
        if not self.external_backup_ok:
            errors.append("EXTERNAL_BACKUP_MISSING")
        if not self.git_clean:
            errors.append("GIT_WORKTREE_DIRTY")
        if not self.units_discovered:
            errors.append("SYSTEMD_UNITS_UNAVAILABLE")
        if not self.same_filesystem:
            errors.append("CROSS_FILESYSTEM_MOVE")
        if not self.legacy_available:
            errors.append("LEGACY_TARGET_EXISTS")
        return tuple(errors)

    @property
    def ready(self) -> bool:
        return not self.preflight_errors


def _require_aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("cutover time must be timezone-aware")
    return now.astimezone(UTC)


def _absolute_without_symlinks(path: Path) -> Path:
    """Resolve a path only after rejecting every existing symlink component."""
    raw = path.expanduser()
    lexical = Path(os.path.abspath(raw))
    current = Path(lexical.anchor)
    for component in lexical.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError("cutover roots must not contain symlinks")
    return lexical.resolve(strict=False)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _directory_is_safe(path: Path) -> bool:
    try:
        _absolute_without_symlinks(path)
        return path.is_dir() and not path.is_symlink()
    except (OSError, ValueError):
        return False


def _same_filesystem(live: Path, rebuilt: Path, legacy: Path) -> bool:
    try:
        return (
            live.stat().st_dev == rebuilt.stat().st_dev
            and live.stat().st_dev == legacy.parent.stat().st_dev
        )
    except OSError:
        return False


def _project_repository_root() -> Path:
    """Return the deployed code repository whose uncommitted changes block cutover."""
    return _PROJECT_ROOT


def _git_worktree_is_clean(_data_root: Path) -> bool:
    """Require the deployed project repository, not the data directory, to be pristine."""
    try:
        root = subprocess.run(
            ["git", "-C", str(_project_repository_root()), "rev-parse", "--show-toplevel"],
            check=False, capture_output=True, text=True, timeout=5,
        )
        if root.returncode != 0:
            return False
        status = subprocess.run(
            ["git", "-C", root.stdout.strip(), "status", "--porcelain"],
            check=False, capture_output=True, text=True, timeout=10,
        )
        return status.returncode == 0 and not status.stdout.strip()
    except (OSError, StopIteration, subprocess.TimeoutExpired):
        return False


def _path_is_inside_git_worktree(path: Path) -> bool:
    """Reject external backup files inside any Git worktree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return result.returncode == 0


def _systemctl_show(unit: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=LoadState"],
            check=False, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "LoadState=loaded" in result.stdout


def _all_units_discovered() -> bool:
    return all(_systemctl_show(unit) for unit in KNOWN_USER_UNITS)


def _validation_is_clean(settings: Settings, database_path: Path) -> bool:
    """Use the newest rebuild run and its current evidence, read-only."""
    try:
        _absolute_without_symlinks(database_path)
    except ValueError:
        return False
    if not database_path.is_file() or database_path.is_symlink():
        return False
    engine = create_db_engine(database_path, read_only=True)
    try:
        with Session(engine) as session:
            run = session.scalar(
                select(PipelineRun)
                .where(PipelineRun.pipeline_type == "data_rebuild")
                .order_by(PipelineRun.id.desc())
                .limit(1)
            )
            return run is not None and validation_passed(
                validate_rebuild(session, settings, run.id)
            )
    except Exception:  # noqa: BLE001 - a failed read-only gate means no cutover.
        return False
    finally:
        engine.dispose()


def _rebuilt_database_is_ready(settings: Settings, rebuilt_root: Path) -> bool:
    database = rebuilt_root / settings.storage.sqlite_path
    try:
        _absolute_without_symlinks(database)
    except ValueError:
        return False
    return database.is_file() and not database.is_symlink() and all(
        check.ok for check in validate_database_copy(database)
    )


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _database_fingerprint(database_path: Path) -> str:
    """Fingerprint logical SQLite content, excluding the copy provenance PRAGMA."""
    digest = hashlib.sha256()
    with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as connection:
        for line in connection.iterdump():
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _external_backup_is_current(
    live_root: Path,
    rebuilt_root: Path,
    now: datetime,
    *,
    live_database: Path | None = None,
) -> bool:
    """Accept only a current, prepared OARadar snapshot outside data and Git trees."""
    configured = os.environ.get(_EXTERNAL_BACKUP_PATH_ENV, "")
    if not configured:
        return False
    try:
        current = _require_aware(now)
        backup = _absolute_without_symlinks(Path(configured))
        source = _absolute_without_symlinks(live_database or live_root / "state" / "oa.db")
        backup_time = datetime.fromtimestamp(backup.stat().st_mtime, tz=UTC)
        backup_age = current - backup_time
        if (
            not backup.is_file()
            or backup.is_symlink()
            or not source.is_file()
            or source.is_symlink()
            or _is_beneath(backup, live_root)
            or _is_beneath(backup, rebuilt_root)
            or _path_is_inside_git_worktree(backup)
            or backup_age < timedelta()
            or backup_age > _BACKUP_MAX_AGE
        ):
            return False
        with sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            application_id = connection.execute("PRAGMA application_id").fetchone()
        return (
            integrity == ("ok",)
            and not foreign_keys
            and revision == (_alembic_head(),)
            and application_id == (_PREPARED_COPY_APPLICATION_ID,)
            and hmac.compare_digest(_database_fingerprint(source), _database_fingerprint(backup))
        )
    except (OSError, sqlite3.DatabaseError, ValueError):
        return False


def build_cutover_plan(settings: Settings, now: datetime) -> CutoverPlan:
    """Collect cutover preflight state without mutating data or services."""
    # This name must stay stable long enough for an operator to copy the token
    # printed by a dry run into the following explicit ``--execute`` command.
    # A same-day target can only be used once because an existing legacy tree
    # is always rejected rather than replaced.
    stamp = _require_aware(now).strftime("%Y%m%d")
    # ``Settings.data_root`` is intentionally resolved for ordinary runtime
    # use.  A destructive rename must instead inspect the configured lexical
    # roots first, otherwise a symlink would be silently normalized away.
    live = _absolute_without_symlinks(settings.app.data_root)
    configured_target = settings.rebuild.target_root.expanduser()
    raw_rebuilt = (
        configured_target if configured_target.is_absolute() else live / configured_target
    )
    rebuilt = _absolute_without_symlinks(raw_rebuilt)
    if rebuilt != resolve_rebuild_root(settings):
        raise ValueError("cutover rebuild root is inconsistent")
    legacy = _absolute_without_symlinks(live.parent / f"{live.name}_legacy_{stamp}")
    live_safe = _directory_is_safe(live)
    rebuilt_safe = _directory_is_safe(rebuilt)
    legacy_available = not _lexists(legacy)
    return CutoverPlan(
        live_root=live,
        rebuilt_root=rebuilt,
        legacy_root=legacy,
        units=KNOWN_USER_UNITS,
        validation_ok=live_safe and rebuilt_safe and _validation_is_clean(
            settings, live / settings.storage.sqlite_path,
        ),
        database_backup_ok=rebuilt_safe and _rebuilt_database_is_ready(settings, rebuilt),
        external_backup_ok=live_safe and rebuilt_safe and _external_backup_is_current(
            live, rebuilt, _require_aware(now), live_database=live / settings.storage.sqlite_path,
        ),
        git_clean=_git_worktree_is_clean(live),
        units_discovered=_all_units_discovered(),
        same_filesystem=live_safe and rebuilt_safe and _same_filesystem(live, rebuilt, legacy),
        legacy_available=legacy_available,
    )


def _authorization_key() -> bytes:
    value = os.environ.get(_AUTHORIZATION_KEY_ENV, "")
    key = value.encode("utf-8")
    if len(key) < 32:
        raise CutoverAuthorizationError("cutover authorization key is unavailable")
    return key


def _token_path_digests(plan: CutoverPlan) -> dict[str, str]:
    """Bind a token to exact paths without printing those paths in its payload."""
    return {
        name: hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        for name, path in {
            "legacy_root": _absolute_without_symlinks(plan.legacy_root),
            "live_root": _absolute_without_symlinks(plan.live_root),
            "rebuilt_root": _absolute_without_symlinks(plan.rebuilt_root),
        }.items()
    }


def _encode_payload(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_payload(value: str) -> dict[str, object]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(decoded)
    except (UnicodeEncodeError, ValueError, json.JSONDecodeError):
        raise CutoverAuthorizationError("authorization token is malformed") from None
    if not isinstance(payload, dict):
        raise CutoverAuthorizationError("authorization token is malformed")
    return payload


def generate_authorization_token(plan: CutoverPlan, *, now: datetime) -> str:
    """Create a short-lived HMAC token bound to this exact rename triplet."""
    issued = _require_aware(now)
    payload: dict[str, object] = {
        "issued_at": issued.isoformat(),
        "nonce": secrets.token_urlsafe(16),
        "path_digests": _token_path_digests(plan),
        "version": 1,
    }
    encoded = _encode_payload(payload)
    signature = hmac.new(_authorization_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_authorization_token(plan: CutoverPlan, token: str, *, now: datetime) -> None:
    """Reject stale, forged, or differently-targeted authorization tokens."""
    issued_now = _require_aware(now)
    try:
        encoded, signature = token.split(".", 1)
    except ValueError:
        raise CutoverAuthorizationError("authorization token is malformed") from None
    expected = hmac.new(_authorization_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise CutoverAuthorizationError("authorization token signature is invalid")
    payload = _decode_payload(encoded)
    try:
        issued = datetime.fromisoformat(str(payload["issued_at"]))
        expected_path_digests = _token_path_digests(plan)
    except (KeyError, TypeError, ValueError):
        raise CutoverAuthorizationError("authorization token is malformed") from None
    if (
        payload.get("version") != 1
        or not isinstance(payload.get("nonce"), str)
        or not hmac.compare_digest(
            json.dumps(payload.get("path_digests"), sort_keys=True, separators=(",", ":")),
            json.dumps(expected_path_digests, sort_keys=True, separators=(",", ":")),
        )
        or issued.tzinfo is None
        or issued.utcoffset() is None
        or issued_now < issued.astimezone(UTC)
        or issued_now - issued.astimezone(UTC) > _TOKEN_MAX_AGE
    ):
        raise CutoverAuthorizationError("authorization token is stale or targets a different cutover")


def _control_units(action: str, units: tuple[str, str, str, str, str]) -> None:
    if units != KNOWN_USER_UNITS or action not in {"start", "stop"}:
        raise CutoverPreflightError("systemd action is outside the cutover allowlist")
    try:
        result = subprocess.run(
            ["systemctl", "--user", action, *units],
            check=False, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CutoverError("systemd control failed") from exc
    if result.returncode != 0:
        raise CutoverError("systemd control failed")


def _smoke(plan: CutoverPlan) -> bool:
    """Service-only smoke: no OA, Feishu, LLM, parser, or network calls."""
    try:
        for unit in plan.units:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", unit],
                check=False, capture_output=True, text=True, timeout=20,
            )
            if result.returncode != 0:
                return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename(source: Path, target: Path) -> None:
    os.rename(source, target)


def _sync_renamed_directories(source: Path, target: Path) -> None:
    _fsync_directory(source.parent)
    if target.parent != source.parent:
        _fsync_directory(target.parent)


def _runtime_paths_are_safe(plan: CutoverPlan) -> bool:
    try:
        live = _absolute_without_symlinks(plan.live_root)
        rebuilt = _absolute_without_symlinks(plan.rebuilt_root)
        legacy = _absolute_without_symlinks(plan.legacy_root)
    except ValueError:
        return False
    return (
        live == plan.live_root
        and rebuilt == plan.rebuilt_root
        and legacy == plan.legacy_root
        and _directory_is_safe(live)
        and _directory_is_safe(rebuilt)
        and not _lexists(legacy)
        and _same_filesystem(live, rebuilt, legacy)
    )


def _rollback(
    plan: CutoverPlan, *, live_to_legacy: bool, rebuilt_to_live: bool,
) -> list[BaseException]:
    """Undo every completed rename, even when durable-directory sync has failed."""
    errors: list[BaseException] = []
    for source, target, completed in (
        (plan.live_root, plan.rebuilt_root, rebuilt_to_live),
        (plan.legacy_root, plan.live_root, live_to_legacy),
    ):
        if not completed:
            continue
        try:
            _rename(source, target)
        except BaseException as exc:  # noqa: BLE001 - the next inverse rename remains required.
            errors.append(exc)
            continue
        try:
            _sync_renamed_directories(source, target)
        except BaseException as exc:  # noqa: BLE001 - preserve error after all renames are attempted.
            errors.append(exc)
    return errors


def execute_cutover(plan: CutoverPlan, *, authorized: bool) -> dict[str, str]:
    """Perform the two safe renames or restore the old tree on every failure."""
    if not authorized:
        raise CutoverAuthorizationError("explicit authorization is required")
    if plan.preflight_errors:
        raise CutoverPreflightError(",".join(plan.preflight_errors))
    if not _runtime_paths_are_safe(plan):
        raise CutoverPreflightError("CUTOVER_PATHS_CHANGED")

    live_to_legacy = rebuilt_to_live = False
    try:
        _control_units("stop", plan.units)
        _rename(plan.live_root, plan.legacy_root)
        live_to_legacy = True
        _sync_renamed_directories(plan.live_root, plan.legacy_root)
        _rename(plan.rebuilt_root, plan.live_root)
        rebuilt_to_live = True
        _sync_renamed_directories(plan.rebuilt_root, plan.live_root)
        _control_units("start", plan.units)
        if not _smoke(plan):
            raise CutoverSmokeError("post-cutover service smoke failed")
        return {"status": "cutover_complete", "rollback": "not_required"}
    except BaseException as exc:
        rollback_errors: list[BaseException] = []
        try:
            _control_units("stop", plan.units)
        except BaseException as stop_exc:  # noqa: BLE001 - preserve the original failure context.
            rollback_errors.append(stop_exc)
        rollback_errors.extend(
            _rollback(
                plan,
                live_to_legacy=live_to_legacy,
                rebuilt_to_live=rebuilt_to_live,
            )
        )
        if _directory_is_safe(plan.live_root):
            try:
                _control_units("start", plan.units)
            except BaseException as restart_exc:  # noqa: BLE001 - restart failure is a rollback failure.
                rollback_errors.append(restart_exc)
        else:
            rollback_errors.append(CutoverRollbackError("legacy live tree was not restored"))
        if rollback_errors:
            raise CutoverRollbackError("cutover rollback failed") from rollback_errors[0]
        if isinstance(exc, CutoverError):
            raise
        raise CutoverError("cutover failed") from exc
