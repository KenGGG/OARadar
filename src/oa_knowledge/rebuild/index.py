"""Publish one complete item index from verified rebuild evidence only."""

from __future__ import annotations

import hashlib
import os
import posixpath
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, OAItem, RebuildOutput
from oa_knowledge.rebuild.body_source import (
    body_markdown_filename,
    load_verified_page_body_evidence,
    select_body_source,
)
from oa_knowledge.rebuild.markdown import (
    _attachment_markdown_filename,
    _publication_sha,
)
from oa_knowledge.rebuild.parser import _file_matches, _tree_sha256
from oa_knowledge.rebuild.paths import markdown_item_relpath, resolve_rebuild_path
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES


class RebuildIndexError(RuntimeError):
    """A complete and trustworthy rebuilt item index cannot be published."""


@dataclass(frozen=True)
class _OriginalEvidence:
    source: ArchivedFile
    output: RebuildOutput


@dataclass(frozen=True)
class _AttachmentState:
    source: ArchivedFile
    state: str
    output: RebuildOutput | None = None


def _error(code: str) -> RebuildIndexError:
    return RebuildIndexError(code)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _link_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _relative_link(source: str, *, item_relpath: PurePosixPath) -> str:
    relative = posixpath.relpath(source, start=item_relpath.as_posix())
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or "\\" in relative or not relative:
        raise _error("UNSAFE_LINK_TARGET")
    return "".join(
        quote(character, safe="")
        if character in {"#", "%", "?", "(", ")", "<", ">", '"', "'"}
        or character.isspace()
        else character
        for character in relative
    )


def _item_lines(item: OAItem) -> list[str]:
    try:
        effective_date = next(
            value
            for value in (item.document_date, item.initiated_at, item.completed_at)
            if value is not None
        )
    except StopIteration as exc:
        raise _error("EFFECTIVE_DATE_REQUIRED") from exc
    date_text = (
        effective_date.date().isoformat()
        if hasattr(effective_date, "date")
        else effective_date.isoformat()
    )
    classification = (
        f"内部事项／{item.internal_category}"
        if item.source_type == "internal"
        else f"外部事项／{item.external_issuer}"
    )
    return [
        f"# {item.title}",
        "",
        f"- 文号：{item.document_number or '无'}",
        f"- 发起人或发文机构：{item.sender or item.department or '未记录'}",
        f"- 成文时间：{item.document_date.isoformat() if item.document_date else '未记录'}",
        f"- 发起时间：{item.initiated_at.date().isoformat() if item.initiated_at else '未记录'}",
        f"- 办结时间：{item.completed_at.date().isoformat() if item.completed_at else '未记录'}",
        f"- 分类：{classification}",
        f"- 生效日期：{date_text}",
    ]


def _valid_original(
    settings: Settings, source: ArchivedFile, output: RebuildOutput
) -> bool:
    if (
        output.status != "success"
        or output.kind != "original"
        or output.source_file_id != source.id
        or output.oa_item_id != source.oa_item_id
        or source.download_status != "verified"
        or source.size_bytes is None
        or not source.sha256
        or output.sha256 != source.sha256
    ):
        return False
    try:
        return _file_matches(
            resolve_rebuild_path(settings, output.target_relpath),
            size_bytes=source.size_bytes,
            sha256=source.sha256,
        )
    except (OSError, ValueError):
        return False


def _original_evidence(
    session: Session, settings: Settings, *, run_id: int, item_id: int
) -> dict[int, _OriginalEvidence]:
    sources = list(
        session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == item_id))
    )
    outputs = list(
        session.scalars(
            select(RebuildOutput)
            .where(
                RebuildOutput.run_id == run_id,
                RebuildOutput.oa_item_id == item_id,
                RebuildOutput.kind == "original",
                RebuildOutput.status == "success",
            )
            .order_by(RebuildOutput.id.desc())
        )
    )
    by_source = {source.id: source for source in sources}
    result: dict[int, _OriginalEvidence] = {}
    for output in outputs:
        source = by_source.get(output.source_file_id)
        if (
            source is not None
            and source.id not in result
            and _valid_original(settings, source, output)
        ):
            result[source.id] = _OriginalEvidence(source, output)
    for source in sources:
        if source.file_role in MARKDOWN_SOURCE_ROLES and source.id not in result:
            raise _error("REBUILT_ORIGINAL_UNAVAILABLE")
    return result


def _outputs_at_target(session: Session, target_relpath: str) -> list[RebuildOutput]:
    return list(
        session.scalars(
            select(RebuildOutput)
            .where(RebuildOutput.target_relpath == target_relpath)
            .order_by(RebuildOutput.id)
        )
    )


def _exact_markdown(
    session: Session,
    settings: Settings,
    *,
    run_id: int,
    item_id: int,
    source_file_id: int,
    kind: str,
    target_relpath: str,
) -> RebuildOutput | None:
    rows = _outputs_at_target(session, target_relpath)
    if len(rows) != 1:
        return None
    output = rows[0]
    if (
        output.run_id != run_id
        or output.oa_item_id != item_id
        or output.source_file_id != source_file_id
        or output.kind != kind
        or output.status != "success"
        or not output.sha256
    ):
        return None
    try:
        target = resolve_rebuild_path(settings, target_relpath)
        assets = target.parent / "assets" / str(source_file_id)
        actual = _publication_sha(target, assets if assets.is_dir() else None)
    except (OSError, ValueError, RebuildIndexError):
        return None
    return output if actual == output.sha256 else None


def _parse_state(
    session: Session, settings: Settings, *, run_id: int, source: ArchivedFile
) -> str:
    rows = list(
        session.scalars(
            select(RebuildOutput)
            .where(
                RebuildOutput.run_id == run_id,
                RebuildOutput.oa_item_id == source.oa_item_id,
                RebuildOutput.source_file_id == source.id,
                RebuildOutput.kind == "parse",
            )
            .order_by(RebuildOutput.id.desc())
        )
    )
    if any(
        row.status == "failed" and row.error_code == "UNSUPPORTED_FORMAT"
        for row in rows
    ):
        return "unsupported"
    success = next(
        (row for row in rows if row.status == "success" and row.sha256), None
    )
    if success is not None:
        try:
            if (
                _tree_sha256(resolve_rebuild_path(settings, success.target_relpath))
                == success.sha256
            ):
                return "success"
        except (OSError, ValueError):
            pass
        raise _error("PARSE_INVALID")
    if any(row.status == "failed" for row in rows):
        return "retryable"
    raise _error("PARSE_UNAVAILABLE")


def _attachment_states(
    session: Session,
    settings: Settings,
    *,
    run_id: int,
    item: OAItem,
    originals: dict[int, _OriginalEvidence],
    selected_body_id: int | None,
) -> list[_AttachmentState]:
    states: list[_AttachmentState] = []
    item_relpath = markdown_item_relpath(item)
    for evidence in sorted(originals.values(), key=lambda value: value.source.id):
        source = evidence.source
        if (
            source.file_role not in MARKDOWN_SOURCE_ROLES
            or source.id == selected_body_id
        ):
            continue
        parse_state = _parse_state(session, settings, run_id=run_id, source=source)
        if parse_state == "unsupported":
            states.append(_AttachmentState(source, "unsupported"))
            continue
        if parse_state == "retryable":
            states.append(_AttachmentState(source, "retryable"))
            continue
        target = (item_relpath / _attachment_markdown_filename(source)).as_posix()
        output = _exact_markdown(
            session,
            settings,
            run_id=run_id,
            item_id=item.id,
            source_file_id=source.id,
            kind="attachment_markdown",
            target_relpath=target,
        )
        if output is None:
            raise _error("ATTACHMENT_MARKDOWN_UNAVAILABLE")
        states.append(_AttachmentState(source, "success", output))
    return states


def _body_output(
    session: Session,
    settings: Settings,
    *,
    run_id: int,
    item: OAItem,
    originals: dict[int, _OriginalEvidence],
) -> RebuildOutput | None:
    filename = body_markdown_filename(item)
    all_bodies = list(
        session.scalars(
            select(RebuildOutput).where(
                RebuildOutput.run_id == run_id,
                RebuildOutput.oa_item_id == item.id,
                RebuildOutput.kind == "body_markdown",
            )
        )
    )
    if filename is None:
        if all_bodies:
            raise _error("UNNUMBERED_BODY_MARKDOWN")
        return None
    if len(all_bodies) > 1:
        raise _error("BODY_MARKDOWN_AMBIGUOUS")
    page = load_verified_page_body_evidence(session, settings, item.id, run_id=run_id)
    selected = select_body_source(
        item,
        [evidence.source for evidence in originals.values()],
        page is not None,
    )
    if selected.kind == "none":
        raise _error("BODY_SOURCE_UNAVAILABLE")
    source_id = (
        selected.source_file_id
        if selected.kind == "attachment"
        else page.source_file_id
    )
    if source_id is None:
        raise _error("BODY_SOURCE_UNAVAILABLE")
    target = (markdown_item_relpath(item) / filename).as_posix()
    output = _exact_markdown(
        session,
        settings,
        run_id=run_id,
        item_id=item.id,
        source_file_id=source_id,
        kind="body_markdown",
        target_relpath=target,
    )
    if output is None:
        raise _error("BODY_MARKDOWN_UNAVAILABLE")
    return output


def _render(
    item: OAItem,
    *,
    item_relpath: PurePosixPath,
    originals: dict[int, _OriginalEvidence],
    body: RebuildOutput | None,
    attachments: list[_AttachmentState],
) -> bytes:
    lines = _item_lines(item)
    lines.extend(("", "## 正文", ""))
    if body is None:
        lines.append("- 无文号事项不生成正文。")
    else:
        lines.append(
            f"- [正文]({_relative_link(body.target_relpath, item_relpath=item_relpath)})"
        )
    lines.extend(("", "## 原始附件", ""))
    for evidence in sorted(originals.values(), key=lambda value: value.source.id):
        lines.append(
            f"- [{_link_label(evidence.source.original_name)}]"
            f"({_relative_link(evidence.output.target_relpath, item_relpath=item_relpath)})"
        )
    lines.extend(("", "## 附件 Markdown", ""))
    successful = [state for state in attachments if state.state == "success"]
    if successful:
        for state in successful:
            assert state.output is not None
            lines.append(
                f"- [{_link_label(state.source.original_name)}]"
                f"({_relative_link(state.output.target_relpath, item_relpath=item_relpath)})"
            )
    else:
        lines.append("- 无。")
    statuses = [state for state in attachments if state.state != "success"]
    lines.extend(("", "## 转换状态", ""))
    if statuses:
        for state in statuses:
            status = (
                "暂不支持转换" if state.state == "unsupported" else "转换失败，等待重试"
            )
            lines.append(f"- {_link_label(state.source.original_name)}：{status}")
    else:
        lines.append("- 全部可转换附件均已生成 Markdown。")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _build_content(
    session: Session, settings: Settings, *, run_id: int, item_id: int
) -> tuple[str, bytes]:
    item = session.get(OAItem, item_id)
    if item is None:
        raise _error("ITEM_NOT_FOUND")
    try:
        item_relpath = markdown_item_relpath(item)
    except ValueError as exc:
        raise _error("CONFIRMED_CLASSIFICATION_AND_DATE_REQUIRED") from exc
    originals = _original_evidence(session, settings, run_id=run_id, item_id=item.id)
    body = _body_output(
        session, settings, run_id=run_id, item=item, originals=originals
    )
    attachments = _attachment_states(
        session,
        settings,
        run_id=run_id,
        item=item,
        originals=originals,
        selected_body_id=body.source_file_id if body is not None else None,
    )
    return (item_relpath / "_index.md").as_posix(), _render(
        item,
        item_relpath=item_relpath,
        originals=originals,
        body=body,
        attachments=attachments,
    )


def _reserve(
    session: Session,
    *,
    run_id: int,
    item_id: int,
    target_relpath: str,
    sha256: str,
) -> RebuildOutput:
    try:
        session.rollback()
        session.execute(text("BEGIN IMMEDIATE"))
        owners = _outputs_at_target(session, target_relpath)
        if len(owners) > 1 or any(
            owner.run_id != run_id
            or owner.oa_item_id != item_id
            or owner.source_file_id is not None
            or owner.kind != "item_index"
            for owner in owners
        ):
            session.rollback()
            raise _error("TARGET_CONFLICT")
        if owners:
            output = owners[0]
            output.sha256, output.status, output.error_code = sha256, "pending", None
        else:
            output = RebuildOutput(
                run_id=run_id,
                oa_item_id=item_id,
                source_file_id=None,
                kind="item_index",
                target_relpath=target_relpath,
                sha256=sha256,
                status="pending",
                error_code=None,
            )
            session.add(output)
        session.commit()
        return output
    except IntegrityError as exc:
        session.rollback()
        raise _error("TARGET_CONFLICT") from exc


def _existing_exact_index(
    session: Session,
    settings: Settings,
    *,
    run_id: int,
    item_id: int,
    target_relpath: str,
    content: bytes,
) -> RebuildOutput | None:
    owners = _outputs_at_target(session, target_relpath)
    if len(owners) > 1:
        raise _error("TARGET_CONFLICT")
    if not owners:
        try:
            if resolve_rebuild_path(settings, target_relpath).exists():
                raise _error("TARGET_CONFLICT")
        except ValueError as exc:
            raise _error("UNSAFE_TARGET") from exc
        return None
    output = owners[0]
    if (
        output.run_id != run_id
        or output.oa_item_id != item_id
        or output.source_file_id is not None
        or output.kind != "item_index"
    ):
        raise _error("TARGET_CONFLICT")
    try:
        exact = resolve_rebuild_path(settings, target_relpath).read_bytes() == content
    except (OSError, ValueError):
        exact = False
    if output.status == "success":
        if exact and output.sha256 == _sha256(content):
            return output
        raise _error("TARGET_CONFLICT")
    return None


def _publish(
    session: Session,
    settings: Settings,
    *,
    output: RebuildOutput,
    content: bytes,
    validate: Callable[[], bool],
) -> RebuildOutput:
    target = resolve_rebuild_path(settings, output.target_relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".rebuild-index-", dir=target.parent, delete=False
        ) as staged:
            staged_path = Path(staged.name)
            staged.write(content)
            staged.flush()
            os.fsync(staged.fileno())
        if not validate():
            raise _error("SOURCE_CHANGED")
        if target.exists():
            if target.read_bytes() != content:
                raise _error("TARGET_CONFLICT")
        else:
            try:
                os.link(staged_path, target)
            except FileExistsError as exc:
                raise _error("TARGET_CONFLICT") from exc
        descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if target.read_bytes() != content:
            raise _error("PUBLICATION_VERIFICATION_FAILED")
        output.status, output.error_code = "success", None
        session.commit()
        return output
    except BaseException as exc:
        output.status = "failed"
        output.error_code = (
            str(exc)
            if isinstance(exc, RebuildIndexError)
            else type(exc).__name__.upper()
        )
        session.commit()
        raise
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


def publish_rebuilt_index(
    session: Session, settings: Settings, run_id: int, item_id: int
) -> RebuildOutput:
    """Publish one complete `_index.md`, without reading OA or live/archive Markdown."""
    with Session(bind=session.get_bind(), expire_on_commit=False) as ledger:
        target_relpath, content = _build_content(
            ledger, settings, run_id=run_id, item_id=item_id
        )
        existing = _existing_exact_index(
            ledger,
            settings,
            run_id=run_id,
            item_id=item_id,
            target_relpath=target_relpath,
            content=content,
        )
        if existing is not None:
            return existing
        output = _reserve(
            ledger,
            run_id=run_id,
            item_id=item_id,
            target_relpath=target_relpath,
            sha256=_sha256(content),
        )

        def validate() -> bool:
            with Session(bind=ledger.get_bind()) as fresh:
                try:
                    current_target, current_content = _build_content(
                        fresh, settings, run_id=run_id, item_id=item_id
                    )
                except RebuildIndexError:
                    return False
                return current_target == target_relpath and current_content == content

        return _publish(
            ledger, settings, output=output, content=content, validate=validate
        )
