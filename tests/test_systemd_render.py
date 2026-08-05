"""Tests for the systemd unit renderer (plan-0805-02 §5)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from oa_knowledge.ops.systemd_render import (
    REPO_ROOT,
    UNIT_FILES,
    detect_context,
    render_units,
    write_units,
)


def _ctx(tmp_path: Path):
    uv = shutil.which("uv") or (Path.home() / ".local" / "bin" / "uv")
    project = tmp_path / "OARadar"
    project.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text("app:\n  data_root: /data/oaradar\n", encoding="utf-8")
    return detect_context(project, config, "Asia/Shanghai", uv_bin=Path(uv))


def test_render_replaces_all_placeholders_with_absolute_paths(tmp_path: Path) -> None:
    # Render using the committed templates under scripts/systemd/templates.
    ctx = _ctx(tmp_path)
    rendered = render_units(ctx)
    assert set(rendered) == set(UNIT_FILES)
    for name, content in rendered.items():
        assert "{{" not in content, f"unsubstituted placeholder in {name}"
        assert "%h/OARadar" not in content
        assert "%h/.local/bin/uv" not in content


def test_render_uses_configured_timezone_and_schedule(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    rendered = render_units(ctx)
    hourly_timer = rendered["oaradar-hourly.timer"]
    nightly_timer = rendered["oaradar-nightly.timer"]
    assert "TZ=Asia/Shanghai" in hourly_timer
    assert "Mon..Fri *-*-* 09..17:05:00" in hourly_timer
    assert "TZ=Asia/Shanghai" in nightly_timer
    assert "Mon..Fri *-*-* 23:30:00" in nightly_timer


def test_render_includes_security_options_and_flock(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    rendered = render_units(ctx)
    for svc in ("oaradar-worker.service", "oaradar-markdown-worker.service"):
        assert "NoNewPrivileges=true" in rendered[svc]
        assert "PrivateTmp=true" in rendered[svc]
        assert "UMask=0077" in rendered[svc]
        assert "Restart=always" in rendered[svc]
    # Both scheduled oneshots are guarded by flock.
    assert "flock -n %t/oaradar-hourly.lock" in rendered["oaradar-hourly.service"]
    assert "flock -n %t/oaradar-nightly.lock" in rendered["oaradar-nightly.service"]


def test_render_points_at_schedule_nightly(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    rendered = render_units(ctx)
    # The nightly unit must drive the durable orchestration, not the old
    # bare manifest sync.
    assert "oa schedule nightly" in rendered["oaradar-nightly.service"]
    assert "oa schedule hourly" not in rendered["oaradar-nightly.service"]


def test_write_units_creates_files(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.systemd_dir = tmp_path / "units"
    written = write_units(ctx, ctx.systemd_dir)
    assert len(written) == len(UNIT_FILES)
    for path in written:
        assert path.exists()
        assert "{{" not in path.read_text(encoding="utf-8")


def test_template_files_exist() -> None:
    for name in UNIT_FILES:
        assert (REPO_ROOT / "scripts" / "systemd" / "templates" / f"{name}.in").exists()
