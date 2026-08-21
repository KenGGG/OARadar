"""Deterministic, rebuild-only body evidence for numbered Done items."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import ClassVar, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, OAItem, RebuildOutput
from oa_knowledge.rebuild.paths import (
    COMPONENT_MAX_BYTES,
    resolve_rebuild_path,
    safe_component,
)


@dataclass(frozen=True)
class BodySource:
    kind: Literal["attachment", "page_body", "none"]
    source_file_id: int | None
    reason: str


_ATTACHMENT_ROLE_PRIORITY = {
    "official_body": 0,
    "official_attachment": 1,
    "direct_attachment": 2,
    "associated_document": 3,
    "opinion_attachment": 4,
}
_BODY_SUFFIX = "-正文.md"


def _normalized(value: str | None) -> str:
    """Normalize only formatting differences while retaining full identifiers."""
    if not value:
        return ""
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _has_document_number(item: OAItem) -> bool:
    return bool(_normalized(item.document_number))


def _attachment_sort_key(source: ArchivedFile) -> tuple[int, int, int]:
    return (
        _ATTACHMENT_ROLE_PRIORITY[source.file_role],
        source.depth,
        source.id if source.id is not None else 0,
    )


def _candidate_attachments(files: Sequence[ArchivedFile]) -> list[ArchivedFile]:
    return [
        source for source in files
        if source.download_status == "verified" and source.file_role in _ATTACHMENT_ROLE_PRIORITY
    ]


def _filename_matches(item: OAItem, source: ArchivedFile) -> bool:
    name = _normalized(PurePosixPath(source.original_name).stem)
    document_number = _normalized(item.document_number)
    title = _normalized(item.title)
    return bool(name) and (
        "正文" in name
        or (bool(document_number) and document_number in name)
        or (bool(title) and title in name)
    )


def select_body_source(
    item: OAItem, files: Sequence[ArchivedFile], page_body_available: bool,
) -> BodySource:
    """Choose one numbered item's strongest available body evidence deterministically."""
    if not _has_document_number(item):
        return BodySource(kind="none", source_file_id=None, reason="no_document_number")

    attachments = _candidate_attachments(files)
    official_bodies = [source for source in attachments if source.file_role == "official_body"]
    if official_bodies:
        source = min(official_bodies, key=_attachment_sort_key)
        return BodySource(kind="attachment", source_file_id=source.id, reason="official_body")

    filename_matches = [source for source in attachments if _filename_matches(item, source)]
    if filename_matches:
        source = min(filename_matches, key=_attachment_sort_key)
        return BodySource(kind="attachment", source_file_id=source.id, reason="filename_match")

    if page_body_available:
        return BodySource(kind="page_body", source_file_id=None, reason="verified_page_body")
    return BodySource(kind="none", source_file_id=None, reason="no_body_evidence")


def _truncate_utf8(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").rstrip(". ")


def body_markdown_filename(item: OAItem) -> str | None:
    """Return the bounded main-body filename for a numbered item, or no filename."""
    if not _has_document_number(item):
        return None
    try:
        number = safe_component(item.document_number or "", max_chars=COMPONENT_MAX_BYTES)
        title = safe_component(item.title or "", max_chars=COMPONENT_MAX_BYTES)
    except ValueError:
        return None
    content_budget = COMPONENT_MAX_BYTES - len(_BODY_SUFFIX.encode("utf-8")) - 1
    title_reserve = min(len(title.encode("utf-8")), content_budget // 2)
    number = _truncate_utf8(number, content_budget - title_reserve)
    title = _truncate_utf8(title, content_budget - len(number.encode("utf-8")))
    if not number or not title:
        return None
    return f"{number}-{title}{_BODY_SUFFIX}"


class _BodyTextParser(HTMLParser):
    """Extract visible text without treating scripts or styles as document content."""

    _BLOCK_TAGS: ClassVar[frozenset[str]] = frozenset({
        "address", "article", "br", "div", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "li", "main", "p", "section", "table", "td", "th", "tr",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._suppressed_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        lowered = tag.casefold()
        if lowered in {"script", "style"}:
            self._suppressed_depth += 1
        elif not self._suppressed_depth and lowered in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in {"script", "style"} and self._suppressed_depth:
            self._suppressed_depth -= 1
        elif not self._suppressed_depth and lowered in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self._parts.append(data)

    def text(self) -> str | None:
        value = " ".join("".join(self._parts).split())
        return value or None


def _sanitize_html_to_text(content: str) -> str | None:
    parser = _BodyTextParser()
    parser.feed(content)
    parser.close()
    return parser.text()


def load_verified_page_body(
    session: Session, settings: Settings, item_id: int, *, run_id: int,
) -> str | None:
    """Read sanitized page text solely from one verified copied body snapshot."""
    rows = session.execute(
        select(ArchivedFile, RebuildOutput)
        .join(RebuildOutput, RebuildOutput.source_file_id == ArchivedFile.id)
        .where(
            ArchivedFile.oa_item_id == item_id,
            ArchivedFile.file_role == "body_snapshot",
            ArchivedFile.download_status == "verified",
            ArchivedFile.size_bytes.is_not(None),
            ArchivedFile.sha256.is_not(None),
            RebuildOutput.run_id == run_id,
            RebuildOutput.oa_item_id == item_id,
            RebuildOutput.kind == "original",
            RebuildOutput.status == "success",
            RebuildOutput.sha256 == ArchivedFile.sha256,
        )
        .order_by(RebuildOutput.id.desc())
    ).all()
    for snapshot, output in rows:
        if snapshot.size_bytes is None or snapshot.sha256 is None:
            continue
        try:
            target = resolve_rebuild_path(settings, output.target_relpath)
            content = target.read_bytes()
        except (OSError, ValueError):
            continue
        if (
            len(content) != snapshot.size_bytes
            or hashlib.sha256(content).hexdigest() != snapshot.sha256
        ):
            continue
        return _sanitize_html_to_text(content.decode("utf-8", errors="replace"))
    return None
