from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

import yaml


class PublicationError(ValueError):
    pass


IMAGE_LINK = re.compile(r"!\[[^]]*\]\(([^)]+)\)")
SAFE_INLINE_IMAGE = re.compile(r"^data:image/(?:png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=.,_-]*$", re.IGNORECASE)


def _validate(content: str, destination: Path, expected_sha256: str, assets_dir: Path | None) -> None:
    if not content.strip() or not content.startswith("---\n"):
        raise PublicationError("Markdown is empty or has no frontmatter")
    try:
        metadata = yaml.safe_load(content.removeprefix("---\n").split("\n---\n", 1)[0])
    except (yaml.YAMLError, IndexError) as exc:
        raise PublicationError("invalid YAML frontmatter") from exc
    if not isinstance(metadata, dict) or metadata.get("source_sha256") != expected_sha256:
        raise PublicationError("source_sha256 mismatch")
    for link in IMAGE_LINK.findall(content):
        if link.startswith("data:"):
            if not SAFE_INLINE_IMAGE.fullmatch(link):
                raise PublicationError("unsafe image URI")
            continue
        relative = PurePosixPath(link.split("#", 1)[0])
        if relative.is_absolute() or ".." in relative.parts:
            raise PublicationError("unsafe image link")
        candidate = destination.parent.joinpath(*relative.parts)
        if assets_dir and relative.parts and relative.parts[0] == destination.name.removesuffix(".md") + ".assets":
            candidate = assets_dir.joinpath(*relative.parts[1:])
        if not candidate.is_file():
            raise PublicationError(f"missing image asset: {link}")
    if metadata.get("parse_status") in {"failed", "unsupported"} and not metadata.get("last_error_code"):
        raise PublicationError("failure placeholder has no error code")


def publish_markdown(destination: Path, content: str, expected_sha256: str, assets_dir: Path | None = None) -> None:
    """Validate in staging, then replace only this source's Markdown/assets."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    _validate(content, destination, expected_sha256, assets_dir)
    stage_root = Path(tempfile.mkdtemp(prefix=".oaradar-md-", dir=destination.parent))
    staged_md = stage_root / destination.name
    staged_assets = stage_root / (destination.name.removesuffix(".md") + ".assets")
    try:
        staged_md.write_text(content, encoding="utf-8")
        with staged_md.open("rb") as handle:
            os.fsync(handle.fileno())
        if assets_dir and assets_dir.exists():
            shutil.copytree(assets_dir, staged_assets)
        final_assets = destination.with_name(destination.name.removesuffix(".md") + ".assets")
        backup_assets = stage_root / ".previous-assets"
        if final_assets.exists():
            final_assets.rename(backup_assets)
        try:
            if staged_assets.exists():
                staged_assets.rename(final_assets)
            os.replace(staged_md, destination)
        except Exception:
            if final_assets.exists():
                shutil.rmtree(final_assets)
            if backup_assets.exists():
                backup_assets.rename(final_assets)
            raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
