"""Tests for the systemd unit renderer (plan-0805-02 §5)."""

from __future__ import annotations

import shutil
import subprocess
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
    # Timezone is appended to the calendar spec (systemd's supported form;
    # the `TZ=` prefix is not parsed by all builds).
    assert "Asia/Shanghai" in hourly_timer
    assert "Mon..Fri *-*-* 09..17:05:00 Asia/Shanghai" in hourly_timer
    assert "Asia/Shanghai" in nightly_timer
    assert "Mon..Fri *-*-* 23:30:00 Asia/Shanghai" in nightly_timer
    assert "TZ=Asia/Shanghai" not in hourly_timer


def test_render_includes_security_options_and_flock(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    rendered = render_units(ctx)
    for svc in ("oaradar-worker.service", "oaradar-markdown-worker.service"):
        assert "NoNewPrivileges=true" in rendered[svc]
        assert "UMask=0077" in rendered[svc]
        assert "Restart=always" in rendered[svc]
    # Chromium persistent profiles need the host user /tmp namespace for
    # navigation and singleton IPC. Non-browser workers retain isolation.
    assert "PrivateTmp=false" in rendered["oaradar-worker.service"]
    assert "PrivateTmp=true" in rendered["oaradar-markdown-worker.service"]
    # Both scheduled oneshots are guarded by flock.
    assert "flock -n %t/oaradar-hourly.lock" in rendered["oaradar-hourly.service"]
    assert "flock -n %t/oaradar-nightly.lock" in rendered["oaradar-nightly.service"]


def test_render_points_at_schedule_nightly(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    rendered = render_units(ctx)
    # The nightly unit must drive the durable orchestration, not the old
    # bare manifest sync.
    assert "oa schedule enqueue nightly" in rendered["oaradar-nightly.service"]
    assert "oa schedule enqueue hourly" not in rendered["oaradar-nightly.service"]
    hourly_script = (REPO_ROOT / "scripts" / "hourly-sync.sh").read_text(encoding="utf-8")
    assert "oa schedule enqueue hourly" in hourly_script


def test_write_units_creates_files(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.systemd_dir = tmp_path / "units"
    written = write_units(ctx, ctx.systemd_dir)
    assert len(written) == len(UNIT_FILES)
    for path in written:
        assert path.exists()
        assert "{{" not in path.read_text(encoding="utf-8")


def test_render_web_service_points_at_oa_web(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    rendered = render_units(ctx)
    web = rendered["oaradar-web.service"]
    assert "ExecStart=" in web
    assert "oa web --config" in web
    assert "{{" not in web
    # The Web console is loopback-only and self-restarting.
    assert "Restart=always" in web
    assert "WantedBy=default.target" in web


def test_render_web_service_has_security_options(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    rendered = render_units(ctx)
    web = rendered["oaradar-web.service"]
    assert "NoNewPrivileges=true" in web
    assert "PrivateTmp=true" in web
    assert "UMask=0077" in web
    assert "Type=simple" in web


def test_render_passes_systemd_analyze_verify(tmp_path: Path) -> None:
    # Plan §9 "test six": render every unit into a temp dir and run
    # `systemd-analyze verify`. Skip gracefully where systemd-analyze is
    # unavailable (e.g. CI without a real systemd), but never depend on the
    # deploying machine's live user units.
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not available")
    ctx = _ctx(tmp_path)
    ctx.systemd_dir = tmp_path / "units"
    write_units(ctx, ctx.systemd_dir)
    for name in UNIT_FILES:
        proc = subprocess.run(
            ["systemd-analyze", "verify", str(ctx.systemd_dir / name)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            f"systemd-analyze verify failed for {name}:\n{proc.stdout}\n{proc.stderr}"
        )


def test_template_files_exist() -> None:
    for name in UNIT_FILES:
        assert (REPO_ROOT / "scripts" / "systemd" / "templates" / f"{name}.in").exists()
