from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileIntegrity:
    valid: bool
    status: str
    size_bytes: int
    sha256: str | None


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    """SHA-256 hex digest of a string, the text counterpart of ``sha256_file``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inspect_file(path: Path, expected_kind: str | None = None) -> FileIntegrity:
    size = path.stat().st_size
    if size == 0:
        return FileIntegrity(False, "rejected_zero_byte", 0, None)
    head = path.read_bytes()[:4096].lstrip().lower()
    is_html = head.startswith((b"<!doctype html", b"<html"))
    html_error = any(marker in head for marker in (b"login_username", b"login_password", b"/cas/login", b"login_button"))
    if (is_html and expected_kind != "html_attachment") or html_error or head.startswith((b"{\"error\"", b"{\"code\"")):
        return FileIntegrity(False, "rejected_error_page", size, None)
    if expected_kind == "html_attachment" and not is_html:
        return FileIntegrity(False, "rejected_type_mismatch", size, None)
    if expected_kind == "pdf" and not head.startswith(b"%pdf-"):
        return FileIntegrity(False, "rejected_type_mismatch", size, None)
    return FileIntegrity(True, "verified", size, sha256_file(path))
