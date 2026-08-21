"""Synthetic safety tests for the explicit local rebuild cutover."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oa_knowledge.config import Settings
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
