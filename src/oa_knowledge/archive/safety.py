"""Metadata-first archive safety checks; this module never extracts members."""

from __future__ import annotations

from dataclasses import dataclass
import io
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile


@dataclass(frozen=True)
class ArchiveLimits:
    max_members: int = 10_000
    max_member_bytes: int = 512 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_ratio: float = 200.0


@dataclass(frozen=True)
class InspectedArchiveMember:
    original_path: str
    normalized_path: str | None
    size_bytes: int
    compressed_size_bytes: int
    status: str
    error_code: str | None = None


@dataclass(frozen=True)
class ArchiveInspection:
    status: str
    error_code: str | None
    members: tuple[InspectedArchiveMember, ...]
    total_uncompressed_bytes: int
    total_compressed_bytes: int


def _normalized_member_path(value: str) -> str | None:
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return None
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _member_is_link_or_special(info: zipfile.ZipInfo) -> bool:
    if info.create_system != 3:
        return False
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    return kind not in {0, stat.S_IFREG, stat.S_IFDIR}


def _rejected(error_code: str, members: list[InspectedArchiveMember] | None = None) -> ArchiveInspection:
    rows = tuple(members or ())
    return ArchiveInspection(
        status="rejected",
        error_code=error_code,
        members=rows,
        total_uncompressed_bytes=sum(row.size_bytes for row in rows),
        total_compressed_bytes=sum(row.compressed_size_bytes for row in rows),
    )


def inspect_zip_bytes(payload: bytes, limits: ArchiveLimits | None = None) -> ArchiveInspection:
    """Inspect every ZIP member without writing or extracting any payload."""
    limits = limits or ArchiveLimits()
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
    except (zipfile.BadZipFile, OSError, ValueError):
        return _rejected("ARCHIVE_CORRUPTED")

    if not infos:
        return _rejected("ARCHIVE_EMPTY")
    if len(infos) > limits.max_members:
        return _rejected("ARCHIVE_MEMBER_LIMIT_EXCEEDED")

    members: list[InspectedArchiveMember] = []
    total_size = 0
    total_compressed = 0
    for info in infos:
        normalized = _normalized_member_path(info.filename)
        if normalized is None:
            return _rejected("ARCHIVE_PATH_TRAVERSAL", members)
        if info.flag_bits & 0x1:
            return _rejected("ARCHIVE_ENCRYPTED", members)
        if _member_is_link_or_special(info):
            return _rejected("ARCHIVE_LINK_REJECTED", members)
        if info.file_size > limits.max_member_bytes:
            return _rejected("ARCHIVE_SIZE_LIMIT_EXCEEDED", members)
        total_size += info.file_size
        total_compressed += info.compress_size
        if total_size > limits.max_total_bytes:
            return _rejected("ARCHIVE_SIZE_LIMIT_EXCEEDED", members)
        ratio = info.file_size / max(info.compress_size, 1)
        if ratio > limits.max_ratio:
            return _rejected("ARCHIVE_RATIO_LIMIT_EXCEEDED", members)
        members.append(InspectedArchiveMember(
            original_path=info.filename,
            normalized_path=normalized,
            size_bytes=info.file_size,
            compressed_size_bytes=info.compress_size,
            status="accepted",
        ))

    return ArchiveInspection(
        status="passed",
        error_code=None,
        members=tuple(members),
        total_uncompressed_bytes=total_size,
        total_compressed_bytes=total_compressed,
    )


def extract_zip_to_staging(
    payload: bytes,
    staging: Path,
    limits: ArchiveLimits | None = None,
) -> ArchiveInspection:
    """Inspect first, then atomically materialize accepted members below staging."""
    inspection = inspect_zip_bytes(payload, limits)
    if inspection.status != "passed":
        return inspection
    if staging.exists():
        raise FileExistsError(f"archive staging destination already exists: {staging}")
    staging.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".archive-", dir=staging.parent))
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            for info, inspected in zip(infos, inspection.members, strict=True):
                relative = PurePosixPath(inspected.normalized_path or "")
                destination = temporary.joinpath(*relative.parts)
                resolved = destination.resolve()
                if temporary.resolve() not in resolved.parents:
                    raise ValueError("ARCHIVE_PATH_TRAVERSAL")
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(prefix=".member-", dir=destination.parent)
                try:
                    with archive.open(info, "r") as source, os.fdopen(fd, "wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                        target.flush()
                        os.fsync(target.fileno())
                    if Path(temp_name).stat().st_size != inspected.size_bytes:
                        raise ValueError("archive member size changed during extraction")
                    os.replace(temp_name, destination)
                finally:
                    Path(temp_name).unlink(missing_ok=True)
        os.replace(temporary, staging)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return inspection
