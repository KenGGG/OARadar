"""Render OARadar systemd unit files from templates (plan-0805-02 §5).

The committed templates under ``scripts/systemd/templates`` still contain
``%h``/``%h/.local/bin/uv`` placeholders that must be rewritten with absolute
paths for the deploying machine. ``render_units`` performs that substitution
so the installer never ships machine-specific paths, and so the result is
unit-testable.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIR = REPO_ROOT / "scripts" / "systemd" / "templates"

UNIT_FILES = (
    "oaradar-worker.service",
    "oaradar-markdown-worker.service",
    "oaradar-hourly.service",
    "oaradar-hourly.timer",
    "oaradar-nightly.service",
    "oaradar-nightly.timer",
)

PLACEHOLDERS = ("{{PROJECT_ROOT}}", "{{UV_BIN}}", "{{CONFIG_PATH}}", "{{ENV_FILE}}", "{{TIMEZONE}}")


@dataclass
class SystemdContext:
    project_root: Path
    uv_bin: Path
    config_path: Path
    env_file: Path
    timezone: str
    systemd_dir: Path


def _detect_uv() -> Path:
    found = shutil.which("uv")
    if found:
        return Path(found).resolve()
    candidate = Path.home() / ".local" / "bin" / "uv"
    if candidate.exists():
        return candidate.resolve()
    raise FileNotFoundError("uv not found on PATH or ~/.local/bin/uv")


def detect_context(
    project_root: Path,
    config: Path,
    timezone: str,
    *,
    env_file: Path | None = None,
    uv_bin: Path | None = None,
) -> SystemdContext:
    project_root = Path(project_root).resolve()
    config_path = Path(config).resolve()
    uv = Path(uv_bin).resolve() if uv_bin else _detect_uv()
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    ef = Path(env_file).resolve() if env_file else (Path.home() / ".config" / "oaradar" / "env")
    return SystemdContext(project_root, uv, config_path, ef, timezone, systemd_dir)


def _substitutions(ctx: SystemdContext) -> dict[str, str]:
    return {
        "{{PROJECT_ROOT}}": str(ctx.project_root),
        "{{UV_BIN}}": str(ctx.uv_bin),
        "{{CONFIG_PATH}}": str(ctx.config_path),
        "{{ENV_FILE}}": str(ctx.env_file),
        "{{TIMEZONE}}": ctx.timezone,
    }


def render_units(ctx: SystemdContext, template_dir: Path | None = None) -> dict[str, str]:
    """Return ``{unit_name: rendered_content}`` with all placeholders resolved."""
    template_dir = Path(template_dir) if template_dir else TEMPLATE_DIR
    subs = _substitutions(ctx)
    rendered: dict[str, str] = {}
    for name in UNIT_FILES:
        text = (template_dir / f"{name}.in").read_text(encoding="utf-8")
        for placeholder, value in subs.items():
            text = text.replace(placeholder, value)
        if "{{" in text:
            raise ValueError(f"unsubstituted placeholder remaining in {name}")
        rendered[name] = text
    return rendered


def write_units(ctx: SystemdContext, output_dir: Path | None = None) -> list[Path]:
    output_dir = Path(output_dir) if output_dir else ctx.systemd_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, content in render_units(ctx).items():
        path = output_dir / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def next_trigger(on_calendar: str) -> str | None:
    """Best-effort next firing time via ``systemd-analyze calendar``."""
    try:
        proc = subprocess.run(
            ["systemd-analyze", "calendar", "--iterations", "1", on_calendar],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render OARadar systemd units")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--uv-bin", default=None)
    parser.add_argument("--output-dir", default=None, help="Override the systemd user dir")
    args = parser.parse_args(argv)

    ctx = detect_context(
        Path(args.project_root), Path(args.config), args.timezone,
        env_file=Path(args.env_file) if args.env_file else None,
        uv_bin=Path(args.uv_bin) if args.uv_bin else None,
    )
    written = write_units(ctx, args.output_dir)
    print(f"Rendered {len(written)} unit files into {args.output_dir or ctx.systemd_dir}")
    for path in written:
        print(f"  {path.name}")
    print("Next triggers (Asia/Shanghai):")
    for cal in (
        f"TZ={ctx.timezone} Mon..Fri *-*-* 09..17:05:00",
        f"TZ={ctx.timezone} Mon..Fri *-*-* 23:30:00",
    ):
        print(f"  {cal} -> {next_trigger(cal) or '(systemd-analyze unavailable)'}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
