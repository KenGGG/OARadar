"""Retire generated derivatives after a verified replacement, keeping recovery ZIPs."""
from pathlib import Path
import json
import shutil
import zipfile
from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.archive.naming import validate_relative_path


def retire_rendered_output(settings, old_relpath, old_sha256, new_relpath, new_sha256):
    if not old_relpath or old_relpath == new_relpath:
        return False
    old = settings.workspace_root.joinpath(*validate_relative_path(old_relpath).parts)
    new = settings.workspace_root.joinpath(*validate_relative_path(new_relpath).parts)
    old.resolve().relative_to(settings.markdown_root.resolve())
    new.resolve().relative_to(settings.markdown_root.resolve())
    if not old.is_file() or sha256_file(old) != old_sha256:
        return False  # Preserve unverified/manual changes for review.
    if not new.is_file() or sha256_file(new) != new_sha256:
        raise ValueError("replacement Markdown is not verified")
    backup = settings.runtime_root / "markdown-relocation-backups"
    backup.mkdir(parents=True, exist_ok=True)
    archive = backup / (old_sha256 + ".zip")
    assets = old.with_name(old.stem + ".assets")
    assets.resolve().relative_to(settings.markdown_root.resolve())
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.write(old, "document.md")
        output.writestr("location.json", json.dumps({"old_relpath":old_relpath,"new_relpath":new_relpath,
            "old_sha256":old_sha256,"new_sha256":new_sha256},ensure_ascii=False))
        if assets.is_dir():
            for path in assets.rglob("*"):
                path.resolve().relative_to(assets.resolve())
                if path.is_file():output.write(path,"assets/"+path.relative_to(assets).as_posix())
    with zipfile.ZipFile(archive) as saved:
        import hashlib
        if saved.testzip() is not None or hashlib.sha256(saved.read("document.md")).hexdigest() != old_sha256:
            raise ValueError("Markdown relocation backup failed verification")
    old.unlink()
    if assets.is_dir():shutil.rmtree(assets)
    parent=old.parent
    while parent != settings.markdown_root and parent.is_dir() and not any(parent.iterdir()):
        parent.resolve().relative_to(settings.markdown_root.resolve())
        parent.rmdir();parent=parent.parent
    return True
