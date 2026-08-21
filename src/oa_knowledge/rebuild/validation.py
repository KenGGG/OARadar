"""Read-only, redacted acceptance gates for a single rebuild run."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, OAItem, RebuildOutput
from oa_knowledge.rebuild.body_source import body_markdown_filename
from oa_knowledge.rebuild.markdown import (
    _attachment_markdown_filename,
    _publication_sha,
)
from oa_knowledge.rebuild.parser import _tree_sha256
from oa_knowledge.rebuild.paths import (
    markdown_item_relpath,
    resolve_rebuild_path,
    resolve_rebuild_root,
)
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES


@dataclass(frozen=True)
class ValidationCheck:
    """One redacted acceptance result; values are aggregate counts only."""

    code: str
    ok: bool
    expected: int | None
    actual: int | None


_LINK = re.compile(r"!?\[[^]]*\]\((?P<target>[^)]+)\)")
_ROOT_DIRS = frozenset({"archive", "parse", "markdown", "state", "runtime"})
_DERIVED_KINDS = frozenset({"body_markdown", "attachment_markdown", "item_index"})


def _check(code: str, expected: int | None, actual: int | None) -> ValidationCheck:
    return ValidationCheck(code, expected == actual, expected, actual)


def _sha256_file(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _regular_files(root: Path) -> tuple[list[Path], bool]:
    """List only regular non-symlink files, while detecting unsafe entries."""
    if not root.exists():
        return [], True
    if not root.is_dir() or root.is_symlink():
        return [], False
    result: list[Path] = []
    valid = True

    def walk(directory: Path) -> None:
        nonlocal valid
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            valid = False
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    valid = False
                elif entry.is_dir(follow_symlinks=False):
                    walk(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    result.append(Path(entry.path))
                else:
                    valid = False
            except OSError:
                valid = False

    walk(root)
    return result, valid


def _safe_target(settings: Settings, relpath: str) -> Path | None:
    try:
        return resolve_rebuild_path(settings, relpath)
    except ValueError:
        return None


def _valid_original(settings: Settings, output: RebuildOutput, source: ArchivedFile) -> bool:
    target = _safe_target(settings, output.target_relpath)
    return bool(
        target
        and output.status == "success"
        and output.sha256 == source.sha256
        and source.size_bytes is not None
        and source.sha256
        and target.is_file()
        and not target.is_symlink()
        and target.stat().st_size == source.size_bytes
        and _sha256_file(target) == source.sha256
    )


def _valid_parse(settings: Settings, output: RebuildOutput, source: ArchivedFile) -> bool:
    target = _safe_target(settings, output.target_relpath)
    if not target or output.status != "success" or not output.sha256:
        return False
    try:
        manifest = json.loads((target / ".oaradar-parse.json").read_text(encoding="utf-8"))
        return (
            isinstance(manifest, dict)
            and bool(manifest.get("engine"))
            and bool(manifest.get("engine_version"))
            and manifest.get("source_file_id") == source.id
            and manifest.get("source_sha256") == source.sha256
            and _tree_sha256(target) == output.sha256
        )
    except (OSError, TypeError, ValueError):
        return False


def _valid_markdown(settings: Settings, output: RebuildOutput) -> bool:
    target = _safe_target(settings, output.target_relpath)
    if not target or output.status != "success" or not output.sha256:
        return False
    try:
        if output.kind == "item_index":
            return _sha256_file(target) == output.sha256
        assets = target.parent / "assets" / str(output.source_file_id)
        return _publication_sha(target, assets if assets.is_dir() and not assets.is_symlink() else None) == output.sha256
    except (OSError, ValueError):
        return False


def _markdown_has_frontmatter(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not text.startswith("---\n"):
        return False
    closing = text.find("\n---\n", 4)
    if closing < 4:
        return False
    try:
        payload = yaml.safe_load(text[4:closing])
    except yaml.YAMLError:
        return False
    return isinstance(payload, dict) and bool(payload.get("oa_item_id")) and bool(payload.get("source_type"))


def _link_target(path: Path, raw: str, root: Path) -> bool:
    target = raw.strip()
    if target.startswith("<"):
        closing = target.find(">")
        if closing < 1:
            return False
        target = target[1:closing]
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or target.startswith(("/", "#")):
        return False
    target = unquote(target.split("#", 1)[0].split("?", 1)[0])
    if not target:
        return True
    candidate = (path.parent / target).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return candidate.is_file() and not candidate.is_symlink()


def _link_counts(paths: Iterable[Path], root: Path) -> tuple[int, int]:
    expected = actual = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _LINK.finditer(text):
            expected += 1
            actual += int(_link_target(path, match.group("target"), root))
    return expected, actual


def _expected_markdown_path(item: OAItem, output: RebuildOutput, source: ArchivedFile | None) -> str | None:
    try:
        item_path = markdown_item_relpath(item)
        if output.kind == "item_index":
            return (item_path / "_index.md").as_posix()
        if source is None:
            return None
        if output.kind == "body_markdown":
            filename = body_markdown_filename(item)
        elif output.kind == "attachment_markdown":
            filename = _attachment_markdown_filename(source)
        else:
            return None
        return (item_path / filename).as_posix() if filename else None
    except ValueError:
        return None


def validate_rebuild(session: Session, settings: Settings, run_id: int) -> list[ValidationCheck]:
    """Validate a current rebuild run without mutating its session or filesystem.

    The result deliberately contains only stable codes and counts; no OA-derived
    metadata, filenames, or local paths can escape this function.
    """
    with session.no_autoflush:
        outputs = list(session.scalars(select(RebuildOutput).where(RebuildOutput.run_id == run_id)))
        sources = list(session.scalars(select(ArchivedFile).join(OAItem).where(OAItem.source_channel == "done")))
        items = {item.id: item for item in session.scalars(select(OAItem).where(OAItem.source_channel == "done"))}
    source_by_id = {source.id: source for source in sources}
    originals = [output for output in outputs if output.kind == "original"]
    parses = [output for output in outputs if output.kind == "parse"]
    derived = [output for output in outputs if output.kind in _DERIVED_KINDS]
    verified = [source for source in sources if source.download_status == "verified"]
    valid_originals = [
        output for output in originals
        if (source := source_by_id.get(output.source_file_id)) is not None and _valid_original(settings, output, source)
    ]
    original_source_ids = {output.source_file_id for output in valid_originals}

    root = resolve_rebuild_root(settings)
    archive_files, archive_tree_safe = _regular_files(root / "archive")
    expected_original_paths = {
        _safe_target(settings, output.target_relpath)
        for output in valid_originals
    }
    expected_original_paths.discard(None)
    unknown_originals = sum(path.resolve() not in {value.resolve() for value in expected_original_paths} for path in archive_files)

    candidate_item_ids = {
        source.oa_item_id for source in verified if source.id in original_source_ids and items.get(source.oa_item_id, None) is not None
    }
    confirmed_candidate_ids = {item_id for item_id in candidate_item_ids if items[item_id].classification_state == "confirmed"}
    indexes = [output for output in derived if output.kind == "item_index" and output.status == "success"]
    bodies = [output for output in derived if output.kind == "body_markdown" and output.status == "success"]
    attachments = [output for output in derived if output.kind == "attachment_markdown" and output.status == "success"]

    checks: list[ValidationCheck] = []
    checks.append(_check("ORIGINALS_COMPLETE", len(verified), len(original_source_ids)))
    checks.append(_check("ORIGINAL_HASHES_MATCH", len(originals), len(valid_originals)))
    checks.append(_check("NO_UNKNOWN_ORIGINALS", 0, unknown_originals if archive_tree_safe else unknown_originals + 1))
    checks.append(_check("CONFIRMED_OUTPUTS_ONLY", 0, sum(items.get(output.oa_item_id) is None or items[output.oa_item_id].classification_state != "confirmed" for output in derived)))

    valid_indexes = [output for output in indexes if _valid_markdown(settings, output)]
    indexes_by_item = {item_id: sum(output.oa_item_id == item_id for output in valid_indexes) for item_id in confirmed_candidate_ids}
    checks.append(_check("INDEX_EXACTLY_ONE", len(confirmed_candidate_ids), sum(count == 1 for count in indexes_by_item.values())))

    numbered = {item_id for item_id in confirmed_candidate_ids if body_markdown_filename(items[item_id]) is not None}
    valid_bodies = [output for output in bodies if _valid_markdown(settings, output)]
    checks.append(_check("NUMBERED_BODY_COMPLETE", len(numbered), sum(sum(output.oa_item_id == item_id for output in valid_bodies) == 1 for item_id in numbered)))
    unnumbered = confirmed_candidate_ids - numbered
    checks.append(_check("UNNUMBERED_BODY_ABSENT", 0, sum(output.oa_item_id in unnumbered for output in bodies)))

    supported_sources = [source for source in verified if source.id in original_source_ids and Path(source.original_name).suffix.casefold() in settings.parser.supported_extensions and source.file_role in MARKDOWN_SOURCE_ROLES]
    valid_parses = [output for output in parses if (source := source_by_id.get(output.source_file_id)) is not None and _valid_parse(settings, output, source)]
    checks.append(_check("PARSE_PRODUCTS_VALID", len(supported_sources), len({output.source_file_id for output in valid_parses if output.source_file_id in {source.id for source in supported_sources}})))

    selected_body_sources = {output.source_file_id for output in valid_bodies}
    ordinary_supported = [source for source in supported_sources if source.id not in selected_body_sources]
    valid_attachments = [output for output in attachments if _valid_markdown(settings, output)]
    checks.append(_check("SUPPORTED_ATTACHMENTS_ACCOUNTED", len(ordinary_supported), len({output.source_file_id for output in valid_attachments if output.source_file_id in {source.id for source in ordinary_supported}})))

    unsupported_sources = [source for source in verified if source.id in original_source_ids and source.file_role in MARKDOWN_SOURCE_ROLES and Path(source.original_name).suffix.casefold() not in settings.parser.supported_extensions]
    index_texts: dict[int, list[str]] = {}
    for output in valid_indexes:
        path = _safe_target(settings, output.target_relpath)
        if path:
            try:
                index_texts.setdefault(output.oa_item_id, []).append(
                    path.read_text(encoding="utf-8")
                )
            except OSError:
                pass
    unsupported_reported = sum(
        any("暂不支持转换" in text for text in index_texts.get(source.oa_item_id, ()))
        and any(
            output.source_file_id == source.id
            and output.status == "failed"
            and output.error_code == "UNSUPPORTED_FORMAT"
            for output in parses
        )
        for source in unsupported_sources
    )
    checks.append(_check("UNSUPPORTED_ATTACHMENTS_INDEXED", len(unsupported_sources), unsupported_reported))

    published_markdown = [output for output in (*bodies, *attachments) if output.status == "success"]
    checks.append(_check("MARKDOWN_FRONTMATTER_VALID", len(published_markdown), sum(_markdown_has_frontmatter(target) for output in published_markdown if (target := _safe_target(settings, output.target_relpath)) is not None)))
    markdown_files, markdown_tree_safe = _regular_files(root / "markdown")
    link_expected, link_actual = _link_counts([path for path in markdown_files if path.suffix.casefold() == ".md"], root)
    checks.append(_check("ALL_LINKS_RESOLVE", link_expected, link_actual if markdown_tree_safe else -1))

    successful_derived = [output for output in derived if output.status == "success"]
    valid_paths = sum(
        output.oa_item_id in items
        and _expected_markdown_path(
            items[output.oa_item_id], output, source_by_id.get(output.source_file_id)
        ) == output.target_relpath
        for output in successful_derived
    )
    checks.append(_check("MARKDOWN_OUTPUT_PATHS_VALID", len(successful_derived), valid_paths))

    try:
        root_entries = list(root.iterdir()) if root.exists() else []
        unexpected_root_dirs = sum(entry.name not in _ROOT_DIRS or not entry.is_dir() or entry.is_symlink() for entry in root_entries)
    except OSError:
        unexpected_root_dirs = 1
    checks.append(_check("REBUILD_ROOT_LAYOUT_CLEAN", 0, unexpected_root_dirs))
    checks.append(_check("CURRENT_RUN_OUTPUTS_FINAL", len(outputs), sum(output.status != "pending" for output in outputs)))
    return checks


def validation_passed(checks: Sequence[ValidationCheck]) -> bool:
    """Return true only for the complete, fixed set of passing acceptance gates."""
    return len(checks) == 15 and all(check.ok for check in checks)
