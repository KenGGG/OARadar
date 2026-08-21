"""Publish classified Markdown solely from verified rebuild evidence."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlparse

import yaml
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, OAItem, RebuildOutput
from oa_knowledge.rebuild.body_source import (
    body_markdown_filename,
    load_verified_page_body,
    select_body_source,
)
from oa_knowledge.rebuild.parser import (
    _file_matches,
    _promote_no_clobber,
    _tree_sha256,
    _validated_regular_files,
)
from oa_knowledge.rebuild.paths import (
    COMPONENT_MAX_BYTES,
    effective_item_date,
    markdown_item_relpath,
    resolve_rebuild_path,
    safe_component,
)


class RebuildPublicationError(RuntimeError):
    """A rebuilt Markdown product could not be published safely."""


class BodySourceDuplicateError(RebuildPublicationError):
    """The selected main-body attachment cannot also be an ordinary attachment."""


@dataclass(frozen=True)
class _ParseEvidence:
    output_id: int
    original_output_id: int
    source_file_id: int
    oa_item_id: int
    parse_relpath: str
    parse_sha256: str
    original_relpath: str
    source_sha256: str
    source_size: int
    parser_name: str
    parser_version: str


@dataclass(frozen=True)
class _PageEvidence:
    output_id: int
    source_file_id: int
    oa_item_id: int
    relpath: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class _StagedPublication:
    directory: Path
    markdown: Path
    assets: Path | None
    sha256: str


_MARKDOWN_LINK = re.compile(r"(?P<prefix>!?)\[(?P<label>[^]]*)\]\((?P<target>[^)]+)\)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _truncate_utf8(value: str, max_bytes: int) -> str:
    return (
        value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").rstrip(". ")
    )


def _attachment_markdown_filename(source: ArchivedFile) -> str:
    raw_suffix = PurePosixPath(source.original_name).suffix
    raw_stem = (
        source.original_name[: -len(raw_suffix)] if raw_suffix else source.original_name
    )
    stem = safe_component(raw_stem, max_chars=max(len(raw_stem), 1))
    suffix = (
        safe_component(raw_suffix, max_chars=max(len(raw_suffix), 1))
        if raw_suffix
        else ""
    )
    final_suffix = f"{suffix}.md"
    stem = _truncate_utf8(stem, COMPONENT_MAX_BYTES - len(final_suffix.encode("utf-8")))
    if not stem:
        raise RebuildPublicationError("UNSAFE_TARGET")
    return f"{stem}{final_suffix}"


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _strict_asset_relpath(value: Path) -> PurePosixPath:
    relative = PurePosixPath(value.as_posix())
    if not relative.parts or any(
        part in {"", ".", ".."}
        or len(part.encode("utf-8")) > COMPONENT_MAX_BYTES
        or safe_component(part, max_chars=max(len(part), 1)) != part
        for part in relative.parts
    ):
        raise RebuildPublicationError("UNSAFE_PARSE_ASSET")
    return relative


def _parser_version(parser_name: str, markdown_path: Path, parse_root: Path) -> str:
    relative = markdown_path.relative_to(parse_root)
    product = relative.parts[0] if len(relative.parts) > 1 else ""
    for prefix in (f"{parser_name}-v", f"{parser_name}-"):
        if product.startswith(prefix) and product[len(prefix) :]:
            return product[len(prefix) :]
    return "unknown"


def _resolve_original(
    session: Session,
    settings: Settings,
    *,
    run_id: int,
    source: ArchivedFile,
) -> tuple[RebuildOutput, Path] | None:
    if (
        source.size_bytes is None
        or not source.sha256
        or source.download_status != "verified"
    ):
        return None
    rows = session.scalars(
        select(RebuildOutput)
        .where(
            RebuildOutput.run_id == run_id,
            RebuildOutput.oa_item_id == source.oa_item_id,
            RebuildOutput.source_file_id == source.id,
            RebuildOutput.kind == "original",
            RebuildOutput.status == "success",
            RebuildOutput.sha256 == source.sha256,
        )
        .order_by(RebuildOutput.id.desc())
    ).all()
    for output in rows:
        try:
            path = resolve_rebuild_path(settings, output.target_relpath)
        except ValueError:
            continue
        if _file_matches(path, size_bytes=source.size_bytes, sha256=source.sha256):
            return output, path
    return None


def _resolve_parse_evidence(
    session: Session,
    settings: Settings,
    *,
    run_id: int,
    source_file_id: int,
) -> tuple[_ParseEvidence, Path, Path]:
    source = session.get(ArchivedFile, source_file_id)
    if source is None:
        raise RebuildPublicationError("SOURCE_NOT_FOUND")
    original = _resolve_original(session, settings, run_id=run_id, source=source)
    if original is None:
        raise RebuildPublicationError("REBUILT_ORIGINAL_UNAVAILABLE")
    original_output, _ = original
    parses = session.scalars(
        select(RebuildOutput)
        .where(
            RebuildOutput.run_id == run_id,
            RebuildOutput.oa_item_id == source.oa_item_id,
            RebuildOutput.source_file_id == source.id,
            RebuildOutput.kind == "parse",
        )
        .order_by(RebuildOutput.id.desc())
    ).all()
    if any(
        row.status == "failed" and row.error_code == "UNSUPPORTED_FORMAT"
        for row in parses
    ):
        raise RebuildPublicationError("UNSUPPORTED_FORMAT")
    success = next(
        (row for row in parses if row.status == "success" and row.sha256), None
    )
    if success is None:
        failure = next((row for row in parses if row.status == "failed"), None)
        raise RebuildPublicationError(
            failure.error_code
            if failure and failure.error_code
            else "PARSE_UNAVAILABLE"
        )
    try:
        parse_root = resolve_rebuild_path(settings, success.target_relpath)
        product_sha = _tree_sha256(parse_root)
    except (OSError, ValueError):
        product_sha = None
    if product_sha != success.sha256:
        raise RebuildPublicationError("PARSE_INVALID")
    markdown_files = [
        path
        for path in _validated_regular_files(parse_root)
        if path.suffix.casefold() == ".md"
    ]
    if len(markdown_files) != 1:
        raise RebuildPublicationError("PARSE_MARKDOWN_AMBIGUOUS")
    markdown_path = markdown_files[0]
    parser_name = PurePosixPath(success.target_relpath).name
    evidence = _ParseEvidence(
        output_id=success.id,
        original_output_id=original_output.id,
        source_file_id=source.id,
        oa_item_id=source.oa_item_id,
        parse_relpath=success.target_relpath,
        parse_sha256=success.sha256,
        original_relpath=original_output.target_relpath,
        source_sha256=source.sha256 or "",
        source_size=source.size_bytes or 0,
        parser_name=parser_name,
        parser_version=_parser_version(parser_name, markdown_path, parse_root),
    )
    return evidence, parse_root, markdown_path


def _fresh_parse_matches(
    session: Session,
    settings: Settings,
    *,
    run_id: int,
    evidence: _ParseEvidence,
) -> bool:
    with session.get_bind().connect() as connection:
        parse_row = connection.execute(
            select(
                RebuildOutput.id,
                RebuildOutput.oa_item_id,
                RebuildOutput.source_file_id,
                RebuildOutput.target_relpath,
                RebuildOutput.sha256,
                RebuildOutput.status,
            ).where(
                RebuildOutput.id == evidence.output_id, RebuildOutput.run_id == run_id
            )
        ).one_or_none()
        original_row = connection.execute(
            select(
                RebuildOutput.id,
                RebuildOutput.oa_item_id,
                RebuildOutput.source_file_id,
                RebuildOutput.target_relpath,
                RebuildOutput.sha256,
                RebuildOutput.status,
            ).where(
                RebuildOutput.id == evidence.original_output_id,
                RebuildOutput.run_id == run_id,
            )
        ).one_or_none()
        source_row = connection.execute(
            select(
                ArchivedFile.id,
                ArchivedFile.oa_item_id,
                ArchivedFile.size_bytes,
                ArchivedFile.sha256,
                ArchivedFile.download_status,
            ).where(ArchivedFile.id == evidence.source_file_id)
        ).one_or_none()
    if (
        parse_row
        != (
            evidence.output_id,
            evidence.oa_item_id,
            evidence.source_file_id,
            evidence.parse_relpath,
            evidence.parse_sha256,
            "success",
        )
        or original_row
        != (
            evidence.original_output_id,
            evidence.oa_item_id,
            evidence.source_file_id,
            evidence.original_relpath,
            evidence.source_sha256,
            "success",
        )
        or source_row
        != (
            evidence.source_file_id,
            evidence.oa_item_id,
            evidence.source_size,
            evidence.source_sha256,
            "verified",
        )
    ):
        return False
    try:
        original_path = resolve_rebuild_path(settings, evidence.original_relpath)
        parse_path = resolve_rebuild_path(settings, evidence.parse_relpath)
        return (
            _file_matches(
                original_path,
                size_bytes=evidence.source_size,
                sha256=evidence.source_sha256,
            )
            and _tree_sha256(parse_path) == evidence.parse_sha256
        )
    except (OSError, ValueError):
        return False


def _frontmatter(
    item: OAItem,
    *,
    source_sha256: str,
    parser_name: str,
    parser_version: str,
) -> str:
    payload = {
        "title": item.title,
        "oa_item_id": item.workitem_id_text or item.oa_item_key,
        "document_number": item.document_number,
        "effective_date": effective_item_date(item).isoformat(),
        "source_type": item.source_type,
        "internal_category": item.internal_category,
        "external_issuer": item.external_issuer,
        "source_sha256": source_sha256,
        "parser_name": parser_name,
        "parser_version": parser_version,
    }
    return f"---\n{yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip()}\n---\n\n"


def _rewrite_links(
    content: str, *, markdown_path: Path, parse_root: Path, asset_name: str
) -> str:
    def replace(match: re.Match[str]) -> str:
        raw_target = match.group("target").strip()
        target = raw_target.split(maxsplit=1)[0]
        parsed = urlparse(target)
        if (
            "\\" in target
            or _CONTROL.search(target)
            or PureWindowsPath(target).is_absolute()
            or parsed.scheme == "file"
            or target.startswith("/")
        ):
            raise RebuildPublicationError("UNSAFE_PARSE_LINK")
        if parsed.scheme or target.startswith("#"):
            return match.group(0)
        relative = PurePosixPath(target)
        source_asset = (markdown_path.parent / Path(*relative.parts)).resolve()
        parse_resolved = parse_root.resolve()
        if (
            parse_resolved not in (source_asset, *source_asset.parents)
            or not source_asset.is_file()
        ):
            raise RebuildPublicationError("MISSING_PARSE_ASSET")
        final_target = PurePosixPath(asset_name) / _strict_asset_relpath(
            source_asset.relative_to(parse_resolved)
        )
        return f"{match.group('prefix')}[{match.group('label')}]({final_target.as_posix()})"

    return _MARKDOWN_LINK.sub(replace, content)


def _publication_sha(markdown: Path, assets: Path | None) -> str:
    digest = hashlib.sha256()
    digest.update(b"markdown\0")
    digest.update(hashlib.sha256(markdown.read_bytes()).digest())
    if assets is not None:
        tree_hash = _tree_sha256(assets)
        if tree_hash is None:
            raise RebuildPublicationError("EMPTY_ASSET_TREE")
        digest.update(b"assets\0")
        digest.update(tree_hash.encode("ascii"))
    return digest.hexdigest()


def _stage_parsed_markdown(
    item: OAItem,
    source: ArchivedFile,
    evidence: _ParseEvidence,
    parse_root: Path,
    parser_markdown: Path,
    *,
    target_dir: Path,
    filename: str,
) -> _StagedPublication:
    staging = Path(tempfile.mkdtemp(prefix=".rebuild-markdown-", dir=target_dir))
    try:
        staged_markdown = staging / filename
        asset_name = f"assets/{source.id}"
        staged_assets = staging / "assets" / str(source.id)
        asset_files = [
            path
            for path in _validated_regular_files(parse_root)
            if path != parser_markdown
        ]
        if asset_files:
            for asset in asset_files:
                relative = _strict_asset_relpath(asset.relative_to(parse_root))
                destination = staged_assets / Path(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(asset, destination)
        body = parser_markdown.read_text(encoding="utf-8")
        body = _rewrite_links(
            body,
            markdown_path=parser_markdown,
            parse_root=parse_root,
            asset_name=asset_name,
        )
        content = (
            _frontmatter(
                item,
                source_sha256=evidence.source_sha256,
                parser_name=evidence.parser_name,
                parser_version=evidence.parser_version,
            )
            + body.rstrip()
            + "\n"
        )
        staged_markdown.write_text(content, encoding="utf-8")
        with staged_markdown.open("rb") as handle:
            os.fsync(handle.fileno())
        assets = staged_assets if staged_assets.exists() else None
        return _StagedPublication(
            staging, staged_markdown, assets, _publication_sha(staged_markdown, assets)
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _stage_page_markdown(
    item: OAItem,
    body: str,
    evidence: _PageEvidence,
    *,
    target_dir: Path,
    filename: str,
) -> _StagedPublication:
    staging = Path(tempfile.mkdtemp(prefix=".rebuild-markdown-", dir=target_dir))
    try:
        markdown = staging / filename
        content = (
            _frontmatter(
                item,
                source_sha256=evidence.sha256,
                parser_name="page_body",
                parser_version="html-text-v1",
            )
            + body.rstrip()
            + "\n"
        )
        markdown.write_text(content, encoding="utf-8")
        with markdown.open("rb") as handle:
            os.fsync(handle.fileno())
        return _StagedPublication(
            staging, markdown, None, _publication_sha(markdown, None)
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _target_output(
    session: Session,
    *,
    target_relpath: str,
) -> RebuildOutput | None:
    return session.scalar(
        select(RebuildOutput)
        .where(
            RebuildOutput.target_relpath == target_relpath,
        )
        .order_by(RebuildOutput.id.desc())
    )


def _verified_publication(
    settings: Settings,
    output: RebuildOutput,
    *,
    source_file_id: int | None,
    kind: str,
) -> bool:
    if (
        output.status != "success"
        or output.sha256 is None
        or output.source_file_id != source_file_id
        or output.kind != kind
    ):
        return False
    target = resolve_rebuild_path(settings, output.target_relpath)
    assets = target.parent / "assets" / str(source_file_id)
    try:
        return (
            target.is_file()
            and _publication_sha(target, assets if assets.is_dir() else None)
            == output.sha256
        )
    except (OSError, ValueError, RebuildPublicationError):
        return False


def _ensure_target_available(
    session: Session,
    settings: Settings,
    *,
    run_id: int,
    source_file_id: int | None,
    kind: str,
    target_relpath: str,
) -> RebuildOutput | None:
    owner = _target_output(session, target_relpath=target_relpath)
    if owner is not None:
        if (
            owner.run_id == run_id
            and owner.source_file_id == source_file_id
            and owner.kind == kind
            and _verified_publication(
                settings, owner, source_file_id=source_file_id, kind=kind
            )
        ):
            return owner
        raise RebuildPublicationError("TARGET_CONFLICT")
    target = resolve_rebuild_path(settings, target_relpath)
    assets = target.parent / "assets" / str(source_file_id)
    if target.exists() or assets.exists():
        raise RebuildPublicationError("TARGET_CONFLICT")
    return None


def _reserve_output(
    session: Session,
    *,
    run_id: int,
    item_id: int,
    source_file_id: int | None,
    kind: str,
    target_relpath: str,
    sha256: str,
) -> RebuildOutput:
    output = RebuildOutput(
        run_id=run_id,
        oa_item_id=item_id,
        source_file_id=source_file_id,
        kind=kind,
        target_relpath=target_relpath,
        sha256=sha256,
        status="pending",
        error_code=None,
    )
    session.add(output)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise RebuildPublicationError("TARGET_CONFLICT") from exc
    return output


def _publish_staged(
    session: Session,
    settings: Settings,
    output: RebuildOutput,
    staged: _StagedPublication,
) -> RebuildOutput:
    target = resolve_rebuild_path(settings, output.target_relpath)
    if output.source_file_id is None and staged.assets is not None:
        raise RebuildPublicationError("ASSET_OWNER_MISSING")
    assets_target = target.parent / "assets" / str(output.source_file_id)
    try:
        if staged.assets is not None:
            assets_target.parent.mkdir(parents=True, exist_ok=True)
            if not _promote_no_clobber(staged.assets, assets_target):
                raise RebuildPublicationError("TARGET_CONFLICT")
        try:
            os.link(staged.markdown, target)
        except FileExistsError as exc:
            raise RebuildPublicationError("TARGET_CONFLICT") from exc
        try:
            descriptor = os.open(
                target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
        if (
            _publication_sha(target, assets_target if assets_target.is_dir() else None)
            != staged.sha256
        ):
            raise RebuildPublicationError("PUBLICATION_VERIFICATION_FAILED")
        output.status = "success"
        output.error_code = None
        session.commit()
        return output
    except BaseException as exc:
        output.status = "failed"
        output.error_code = (
            str(exc)
            if isinstance(exc, RebuildPublicationError)
            else type(exc).__name__.upper()
        )
        session.commit()
        raise
    finally:
        shutil.rmtree(staged.directory, ignore_errors=True)


def _resolve_page_evidence(
    session: Session,
    settings: Settings,
    *,
    run_id: int,
    item_id: int,
) -> tuple[_PageEvidence, str] | None:
    text = load_verified_page_body(session, settings, item_id, run_id=run_id)
    if text is None:
        return None
    rows = session.execute(
        select(ArchivedFile, RebuildOutput)
        .join(
            RebuildOutput,
            RebuildOutput.source_file_id == ArchivedFile.id,
        )
        .where(
            ArchivedFile.oa_item_id == item_id,
            ArchivedFile.file_role == "body_snapshot",
            ArchivedFile.download_status == "verified",
            RebuildOutput.run_id == run_id,
            RebuildOutput.kind == "original",
            RebuildOutput.status == "success",
            RebuildOutput.sha256 == ArchivedFile.sha256,
        )
        .order_by(RebuildOutput.id.desc())
    ).all()
    for source, output in rows:
        if source.sha256 and source.size_bytes is not None:
            path = resolve_rebuild_path(settings, output.target_relpath)
            if _file_matches(path, size_bytes=source.size_bytes, sha256=source.sha256):
                return _PageEvidence(
                    output.id,
                    source.id,
                    item_id,
                    output.target_relpath,
                    source.sha256,
                    source.size_bytes,
                ), text
    return None


def _fresh_page_matches(
    session: Session,
    settings: Settings,
    *,
    run_id: int,
    evidence: _PageEvidence,
) -> bool:
    with session.get_bind().connect() as connection:
        row = connection.execute(
            select(
                RebuildOutput.id,
                RebuildOutput.oa_item_id,
                RebuildOutput.source_file_id,
                RebuildOutput.target_relpath,
                RebuildOutput.sha256,
                RebuildOutput.status,
                ArchivedFile.size_bytes,
                ArchivedFile.sha256,
                ArchivedFile.download_status,
            )
            .join(ArchivedFile, RebuildOutput.source_file_id == ArchivedFile.id)
            .where(
                RebuildOutput.id == evidence.output_id,
                RebuildOutput.run_id == run_id,
            )
        ).one_or_none()
    if row != (
        evidence.output_id,
        evidence.oa_item_id,
        evidence.source_file_id,
        evidence.relpath,
        evidence.sha256,
        "success",
        evidence.size_bytes,
        evidence.sha256,
        "verified",
    ):
        return False
    return _file_matches(
        resolve_rebuild_path(settings, evidence.relpath),
        size_bytes=evidence.size_bytes,
        sha256=evidence.sha256,
    )


def _publish_parsed(
    session: Session,
    settings: Settings,
    *,
    run_id: int,
    item: OAItem,
    source: ArchivedFile,
    filename: str,
    kind: str,
) -> RebuildOutput:
    item_relpath = markdown_item_relpath(item)
    target_relpath = (item_relpath / filename).as_posix()
    existing = _ensure_target_available(
        session,
        settings,
        run_id=run_id,
        source_file_id=source.id,
        kind=kind,
        target_relpath=target_relpath,
    )
    if existing is not None:
        return existing
    evidence, parse_root, parser_markdown = _resolve_parse_evidence(
        session,
        settings,
        run_id=run_id,
        source_file_id=source.id,
    )
    target_dir = resolve_rebuild_path(settings, item_relpath)
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = _stage_parsed_markdown(
        item,
        source,
        evidence,
        parse_root,
        parser_markdown,
        target_dir=target_dir,
        filename=filename,
    )
    if not _fresh_parse_matches(session, settings, run_id=run_id, evidence=evidence):
        shutil.rmtree(staged.directory, ignore_errors=True)
        raise RebuildPublicationError("SOURCE_CHANGED")
    output = _reserve_output(
        session,
        run_id=run_id,
        item_id=item.id,
        source_file_id=source.id,
        kind=kind,
        target_relpath=target_relpath,
        sha256=staged.sha256,
    )
    if not _fresh_parse_matches(session, settings, run_id=run_id, evidence=evidence):
        output.status, output.error_code = "failed", "SOURCE_CHANGED"
        session.commit()
        shutil.rmtree(staged.directory, ignore_errors=True)
        raise RebuildPublicationError("SOURCE_CHANGED")
    return _publish_staged(session, settings, output, staged)


def publish_rebuilt_attachment(
    session: Session,
    settings: Settings,
    run_id: int,
    source_file_id: int,
) -> RebuildOutput:
    """Publish one ordinary attachment from a verified current-run parse product."""
    with Session(bind=session.get_bind(), expire_on_commit=False) as ledger:
        source = ledger.get(ArchivedFile, source_file_id)
        if source is None:
            raise RebuildPublicationError("SOURCE_NOT_FOUND")
        item = ledger.get(OAItem, source.oa_item_id)
        if item is None:
            raise RebuildPublicationError("ITEM_NOT_FOUND")
        files = list(
            ledger.scalars(
                select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id)
            )
        )
        page_available = (
            _resolve_page_evidence(
                ledger,
                settings,
                run_id=run_id,
                item_id=item.id,
            )
            is not None
        )
        body = select_body_source(item, files, page_available)
        if body.kind == "attachment" and body.source_file_id == source.id:
            raise BodySourceDuplicateError("SELECTED_BODY_SOURCE")
        return _publish_parsed(
            ledger,
            settings,
            run_id=run_id,
            item=item,
            source=source,
            filename=_attachment_markdown_filename(source),
            kind="attachment_markdown",
        )


def publish_rebuilt_body(
    session: Session,
    settings: Settings,
    run_id: int,
    item_id: int,
) -> RebuildOutput | None:
    """Publish exactly one numbered-item body from selected rebuilt evidence."""
    with Session(bind=session.get_bind(), expire_on_commit=False) as ledger:
        item = ledger.get(OAItem, item_id)
        if item is None:
            raise RebuildPublicationError("ITEM_NOT_FOUND")
        filename = body_markdown_filename(item)
        if filename is None:
            return None
        files = list(
            ledger.scalars(
                select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id)
            )
        )
        page = _resolve_page_evidence(ledger, settings, run_id=run_id, item_id=item.id)
        selected = select_body_source(item, files, page is not None)
        if selected.kind == "attachment" and selected.source_file_id is not None:
            source = ledger.get(ArchivedFile, selected.source_file_id)
            if source is None:
                raise RebuildPublicationError("BODY_SOURCE_NOT_FOUND")
            return _publish_parsed(
                ledger,
                settings,
                run_id=run_id,
                item=item,
                source=source,
                filename=filename,
                kind="body_markdown",
            )
        if selected.kind != "page_body" or page is None:
            raise RebuildPublicationError("BODY_SOURCE_UNAVAILABLE")
        evidence, body = page
        item_relpath = markdown_item_relpath(item)
        target_relpath = (item_relpath / filename).as_posix()
        existing = _ensure_target_available(
            ledger,
            settings,
            run_id=run_id,
            source_file_id=evidence.source_file_id,
            kind="body_markdown",
            target_relpath=target_relpath,
        )
        if existing is not None:
            return existing
        target_dir = resolve_rebuild_path(settings, item_relpath)
        target_dir.mkdir(parents=True, exist_ok=True)
        staged = _stage_page_markdown(
            item,
            body,
            evidence,
            target_dir=target_dir,
            filename=filename,
        )
        if not _fresh_page_matches(ledger, settings, run_id=run_id, evidence=evidence):
            shutil.rmtree(staged.directory, ignore_errors=True)
            raise RebuildPublicationError("SOURCE_CHANGED")
        output = _reserve_output(
            ledger,
            run_id=run_id,
            item_id=item.id,
            source_file_id=evidence.source_file_id,
            kind="body_markdown",
            target_relpath=target_relpath,
            sha256=staged.sha256,
        )
        if not _fresh_page_matches(ledger, settings, run_id=run_id, evidence=evidence):
            output.status, output.error_code = "failed", "SOURCE_CHANGED"
            ledger.commit()
            shutil.rmtree(staged.directory, ignore_errors=True)
            raise RebuildPublicationError("SOURCE_CHANGED")
        return _publish_staged(ledger, settings, output, staged)
