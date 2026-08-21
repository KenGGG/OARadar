"""Synthetic safety tests for the explicit local rebuild cutover."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oa_knowledge.config import Settings
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.rebuild import cutover
from oa_knowledge.rebuild.cutover import (
    CutoverAuthorizationError,
    CutoverPlan,
    CutoverPreflightError,
    CutoverSmokeError,
    build_cutover_plan,
    execute_cutover,
    generate_authorization_token,
    verify_authorization_token,
)
from oa_knowledge.rebuild.state_copy import backup_live_database


def _ready_plan(tmp_path: Path) -> CutoverPlan:
    live = tmp_path / "data"
    rebuilt = tmp_path / "data_rebuilt"
    legacy = tmp_path / "data_legacy_20260822T120000Z"
    live.mkdir()
    rebuilt.mkdir()
    (live / "marker").write_text("legacy", encoding="utf-8")
    (rebuilt / "marker").write_text("rebuilt", encoding="utf-8")
    return CutoverPlan(
        live_root=live,
        rebuilt_root=rebuilt,
        legacy_root=legacy,
        units=cutover.KNOWN_USER_UNITS,
        validation_ok=True,
        database_backup_ok=True,
        external_backup_ok=True,
        git_clean=True,
        units_discovered=True,
        same_filesystem=True,
        legacy_available=True,
    )


def test_cutover_without_execute_changes_nothing(config_file: Path) -> None:
    """A normal CLI invocation may inspect but never makes a rename or service call."""
    settings = Settings.model_validate({
        "app": {"data_root": str(config_file.parent / "data")},
        "rebuild": {"target_root": "../data_rebuilt"},
    })
    settings.data_root.mkdir()
    (config_file.parent / "data_rebuilt").mkdir()

    plan = build_cutover_plan(settings, datetime(2026, 8, 22, 12, tzinfo=UTC))

    assert plan.live_root.exists()
    assert plan.rebuilt_root.exists()
    assert not plan.legacy_root.exists()


def test_execute_requires_all_preflight_gates_before_stopping_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _ready_plan(tmp_path)
    plan = CutoverPlan(**{**plan.__dict__, "database_backup_ok": False})
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(cutover, "_control_units", lambda action, units: calls.append((action, units)))

    with pytest.raises(CutoverPreflightError, match="DATABASE_BACKUP_MISSING"):
        execute_cutover(plan, authorized=True)

    assert calls == []
    assert (plan.live_root / "marker").read_text(encoding="utf-8") == "legacy"
    assert (plan.rebuilt_root / "marker").read_text(encoding="utf-8") == "rebuilt"


def test_execute_requires_a_current_external_backup_before_stopping_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _ready_plan(tmp_path)
    plan = CutoverPlan(**{**plan.__dict__, "external_backup_ok": False})
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(cutover, "_control_units", lambda action, units: calls.append((action, units)))

    with pytest.raises(CutoverPreflightError, match="EXTERNAL_BACKUP_MISSING"):
        execute_cutover(plan, authorized=True)

    assert calls == []
    assert (plan.live_root / "marker").read_text(encoding="utf-8") == "legacy"


def test_successful_cutover_uses_only_known_units_and_renames_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _ready_plan(tmp_path)
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(cutover, "_control_units", lambda action, units: calls.append((action, units)))
    monkeypatch.setattr(cutover, "_smoke", lambda _plan: True)

    result = execute_cutover(plan, authorized=True)

    assert result == {"status": "cutover_complete", "rollback": "not_required"}
    assert (plan.live_root / "marker").read_text(encoding="utf-8") == "rebuilt"
    assert (plan.legacy_root / "marker").read_text(encoding="utf-8") == "legacy"
    assert not plan.rebuilt_root.exists()
    assert calls == [
        ("stop", cutover.KNOWN_USER_UNITS),
        ("start", cutover.KNOWN_USER_UNITS),
    ]


def test_failed_smoke_restores_live_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-switch failure must restart the old directory without deleting either tree."""
    plan = _ready_plan(tmp_path)
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(cutover, "_control_units", lambda action, units: calls.append((action, units)))
    monkeypatch.setattr(cutover, "_smoke", lambda _plan: False)

    with pytest.raises(CutoverSmokeError):
        execute_cutover(plan, authorized=True)

    assert (plan.live_root / "marker").read_text(encoding="utf-8") == "legacy"
    assert (plan.rebuilt_root / "marker").read_text(encoding="utf-8") == "rebuilt"
    assert not plan.legacy_root.exists()
    assert calls == [
        ("stop", cutover.KNOWN_USER_UNITS),
        ("start", cutover.KNOWN_USER_UNITS),
        ("stop", cutover.KNOWN_USER_UNITS),
        ("start", cutover.KNOWN_USER_UNITS),
    ]


def test_second_rename_failure_restores_the_first_completed_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partially completed two-rename switch must still return the old tree."""
    plan = _ready_plan(tmp_path)
    calls: list[tuple[str, tuple[str, ...]]] = []
    actual_rename = cutover._rename

    def fail_promoting_rebuilt(source: Path, target: Path) -> None:
        if source == plan.rebuilt_root and target == plan.live_root:
            raise OSError("synthetic second rename failure")
        actual_rename(source, target)

    monkeypatch.setattr(cutover, "_control_units", lambda action, units: calls.append((action, units)))
    monkeypatch.setattr(cutover, "_rename", fail_promoting_rebuilt)

    with pytest.raises(cutover.CutoverError):
        execute_cutover(plan, authorized=True)

    assert (plan.live_root / "marker").read_text(encoding="utf-8") == "legacy"
    assert (plan.rebuilt_root / "marker").read_text(encoding="utf-8") == "rebuilt"
    assert not plan.legacy_root.exists()
    assert calls == [
        ("stop", cutover.KNOWN_USER_UNITS),
        ("stop", cutover.KNOWN_USER_UNITS),
        ("start", cutover.KNOWN_USER_UNITS),
    ]


@pytest.mark.parametrize("failure_number", (1, 2))
def test_directory_fsync_failure_after_rename_restores_every_mutated_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_number: int,
) -> None:
    """A rename has succeeded before its durability fsync can fail."""
    plan = _ready_plan(tmp_path)
    calls: list[tuple[str, tuple[str, ...]]] = []
    real_fsync = cutover._fsync_directory
    fsync_calls = 0

    def fail_once(directory: Path) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == failure_number:
            raise OSError("synthetic directory fsync failure")
        real_fsync(directory)

    monkeypatch.setattr(cutover, "_control_units", lambda action, units: calls.append((action, units)))
    monkeypatch.setattr(cutover, "_fsync_directory", fail_once)

    with pytest.raises(cutover.CutoverError):
        execute_cutover(plan, authorized=True)

    assert (plan.live_root / "marker").read_text(encoding="utf-8") == "legacy"
    assert (plan.rebuilt_root / "marker").read_text(encoding="utf-8") == "rebuilt"
    assert not plan.legacy_root.exists()
    assert calls == [
        ("stop", cutover.KNOWN_USER_UNITS),
        ("stop", cutover.KNOWN_USER_UNITS),
        ("start", cutover.KNOWN_USER_UNITS),
    ]


def test_persistent_rollback_fsync_failure_still_restores_live_before_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durability errors cannot prevent the remaining inverse rename or a safe restart."""
    plan = _ready_plan(tmp_path)
    control_calls: list[str] = []
    rename_calls: list[tuple[Path, Path]] = []
    unsafe_restarts: list[bool] = []
    real_rename = cutover._rename
    real_fsync = cutover._fsync_directory
    fsync_calls = 0

    def record_rename(source: Path, target: Path) -> None:
        rename_calls.append((source, target))
        real_rename(source, target)

    def fail_persistently_after_second_forward_sync(directory: Path) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls >= 2:
            raise OSError("synthetic persistent directory fsync failure")
        real_fsync(directory)

    def record_control(action: str, _units: tuple[str, ...]) -> None:
        control_calls.append(action)
        if action == "start":
            unsafe_restarts.append(not plan.live_root.exists())

    monkeypatch.setattr(cutover, "_rename", record_rename)
    monkeypatch.setattr(cutover, "_fsync_directory", fail_persistently_after_second_forward_sync)
    monkeypatch.setattr(cutover, "_control_units", record_control)

    with pytest.raises(cutover.CutoverRollbackError):
        execute_cutover(plan, authorized=True)

    assert rename_calls == [
        (plan.live_root, plan.legacy_root),
        (plan.rebuilt_root, plan.live_root),
        (plan.live_root, plan.rebuilt_root),
        (plan.legacy_root, plan.live_root),
    ]
    assert (plan.live_root / "marker").read_text(encoding="utf-8") == "legacy"
    assert (plan.rebuilt_root / "marker").read_text(encoding="utf-8") == "rebuilt"
    assert not plan.legacy_root.exists()
    assert control_calls == ["stop", "stop", "start"]
    assert unsafe_restarts == [False]


def test_authorization_token_is_path_bound_and_short_lived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _ready_plan(tmp_path)
    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    monkeypatch.setenv("OA_REBUILD_CUTOVER_AUTHORIZATION_KEY", "x" * 32)
    token = generate_authorization_token(plan, now=now)

    verify_authorization_token(plan, token, now=now + timedelta(minutes=9))
    moved = CutoverPlan(**{**plan.__dict__, "legacy_root": tmp_path / "different-legacy"})
    with pytest.raises(CutoverAuthorizationError):
        verify_authorization_token(moved, token, now=now + timedelta(minutes=1))
    with pytest.raises(CutoverAuthorizationError):
        verify_authorization_token(plan, token, now=now + timedelta(minutes=11))


def test_authorization_token_survives_a_second_dry_run_on_the_same_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator must be able to copy a fresh token into the next CLI call."""
    settings = Settings.model_validate({
        "app": {"data_root": str(tmp_path / "data")},
        "rebuild": {"target_root": "../data_rebuilt"},
    })
    settings.data_root.mkdir()
    (tmp_path / "data_rebuilt").mkdir()
    monkeypatch.setenv("OA_REBUILD_CUTOVER_AUTHORIZATION_KEY", "x" * 32)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

    shown_plan = build_cutover_plan(settings, now)
    execution_plan = build_cutover_plan(settings, now + timedelta(minutes=1))
    token = generate_authorization_token(shown_plan, now=now)

    verify_authorization_token(execution_plan, token, now=now + timedelta(minutes=1))


def test_build_plan_rejects_a_symlinked_live_root(tmp_path: Path) -> None:
    """A resolved Settings property must not hide a live-root symlink from cutover."""
    real = tmp_path / "real-data"
    real.mkdir()
    live_link = tmp_path / "data"
    live_link.symlink_to(real, target_is_directory=True)
    (tmp_path / "data_rebuilt").mkdir()
    settings = Settings.model_validate({
        "app": {"data_root": str(live_link)},
        "rebuild": {"target_root": "../data_rebuilt"},
    })

    with pytest.raises(ValueError, match="symlinks"):
        build_cutover_plan(settings, datetime(2026, 8, 22, 12, tzinfo=UTC))


def test_external_backup_requires_current_prepared_oaradar_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integrity alone must not let an unrelated or stale SQLite file authorize cutover."""
    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    live = tmp_path / "live" / "state" / "oa.db"
    rebuilt = tmp_path / "rebuilt"
    backup = tmp_path / "external" / "backup.db"
    live.parent.mkdir(parents=True)
    rebuilt.mkdir()
    upgrade_database(live)
    backup.parent.mkdir()
    backup_live_database(live, backup)
    monkeypatch.setenv("OA_REBUILD_CUTOVER_BACKUP_PATH", str(backup))

    assert cutover._external_backup_is_current(live.parents[1], rebuilt, now)

    raw_copy = tmp_path / "external" / "raw-copy.db"
    with sqlite3.connect(live) as source, sqlite3.connect(raw_copy) as target:
        source.backup(target)
    monkeypatch.setenv("OA_REBUILD_CUTOVER_BACKUP_PATH", str(raw_copy))
    assert not cutover._external_backup_is_current(live.parents[1], rebuilt, now)

    unrelated = tmp_path / "external" / "unrelated.db"
    sqlite3.connect(unrelated).close()
    monkeypatch.setenv("OA_REBUILD_CUTOVER_BACKUP_PATH", str(unrelated))
    assert not cutover._external_backup_is_current(live.parents[1], rebuilt, now)

    wrong_revision = tmp_path / "external" / "wrong-revision.db"
    with sqlite3.connect(backup) as source, sqlite3.connect(wrong_revision) as target:
        source.backup(target)
    with sqlite3.connect(wrong_revision) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'synthetic-wrong-head'")
        connection.commit()
    monkeypatch.setenv("OA_REBUILD_CUTOVER_BACKUP_PATH", str(wrong_revision))
    assert not cutover._external_backup_is_current(live.parents[1], rebuilt, now)

    other_live = tmp_path / "other-live" / "state" / "oa.db"
    other_live.parent.mkdir(parents=True)
    upgrade_database(other_live)
    with sqlite3.connect(other_live) as connection:
        connection.execute("CREATE TABLE synthetic_different_snapshot (value TEXT)")
        connection.commit()
    mismatched = tmp_path / "external" / "mismatched.db"
    backup_live_database(other_live, mismatched)
    monkeypatch.setenv("OA_REBUILD_CUTOVER_BACKUP_PATH", str(mismatched))
    assert not cutover._external_backup_is_current(live.parents[1], rebuilt, now)

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    in_repository = repository / "backup.db"
    backup_live_database(live, in_repository)
    monkeypatch.setenv("OA_REBUILD_CUTOVER_BACKUP_PATH", str(in_repository))
    assert not cutover._external_backup_is_current(live.parents[1], rebuilt, now)

    os.utime(backup, (now.timestamp() + 60, now.timestamp() + 60))
    monkeypatch.setenv("OA_REBUILD_CUTOVER_BACKUP_PATH", str(backup))
    assert not cutover._external_backup_is_current(live.parents[1], rebuilt, now)


def test_dirty_project_repository_blocks_external_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A data root outside Git cannot bypass changes in the deployed project."""
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    (project / "tracked.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "tracked.txt"], check=True)
    subprocess.run([
        "git", "-C", str(project), "-c", "user.name=synthetic",
        "-c", "user.email=synthetic@example.invalid", "commit", "--quiet", "-m", "base",
    ], check=True)
    (project / "tracked.txt").write_text("dirty", encoding="utf-8")
    monkeypatch.setattr(cutover, "_project_repository_root", lambda: project, raising=False)

    assert not cutover._git_worktree_is_clean(tmp_path / "external-data")
