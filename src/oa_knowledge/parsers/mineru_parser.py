"""MinerU local API adapter with validated, atomic artifact extraction."""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urljoin, urlsplit

import httpx

from oa_knowledge.config import Settings
from oa_knowledge.parsers.quality import assess_quality
from oa_knowledge.parsers.router import ParseResult


class MineruResponseError(RuntimeError):
    """MinerU returned a response that cannot be published safely."""


def _transport_for_settings(settings: Settings) -> httpx.BaseTransport | None:
    """Test seam for a local HTTP transport."""
    return None


def _client(settings: Settings, *, health: bool = False) -> httpx.Client:
    cfg = settings.mineru
    timeout = httpx.Timeout(
        cfg.health_timeout_seconds if health else cfg.read_timeout_seconds,
        connect=cfg.connect_timeout_seconds,
    )
    return httpx.Client(
        base_url=cfg.api_url,
        timeout=timeout,
        transport=_transport_for_settings(settings),
        follow_redirects=False,
        trust_env=False,
    )


def _health_payload(settings: Settings, *, attempts: int = 1) -> dict:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with _client(settings, health=True) as client:
                response = client.get("/v1/health")
                response.raise_for_status()
                payload = response.json()
            break
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(2**attempt, 2))
    else:
        raise MineruResponseError(
            f"MinerU health check failed: {last_error}"
        ) from last_error
    if not isinstance(payload, dict):
        raise MineruResponseError("MinerU health check returned an invalid payload")
    return payload


def mineru_available(settings: Settings) -> bool:
    if not settings.mineru.enabled:
        return False
    try:
        _health_payload(settings)
    except MineruResponseError:
        return False
    return True


def mineru_engine_version(settings: Settings) -> str:
    """Return the local MinerU protocol/version used in a parse cache identity."""
    health = _health_payload(settings)
    return str(health.get("version") or "api-v1")


def _validate_member(name: str) -> PurePosixPath:
    member = PurePosixPath(name.replace("\\", "/"))
    if member.is_absolute() or ".." in member.parts or not member.parts or any(":" in part for part in member.parts):
        raise MineruResponseError(f"unsafe ZIP entry: {name}")
    return member


def _extract_mineru_zip(payload: bytes, destination: Path) -> Path:
    """Validate every member before extracting and return the main Markdown path."""
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            members = [
                (info, _validate_member(info.filename)) for info in archive.infolist()
            ]
            markdown = sorted(
                (
                    path
                    for info, path in members
                    if not info.is_dir() and path.suffix.lower() == ".md"
                ),
                key=lambda path: (len(path.parts), path.as_posix()),
            )
            if not markdown:
                raise MineruResponseError("MinerU ZIP contains no Markdown result")
            destination.mkdir(parents=True, exist_ok=False)
            for info, relative in members:
                target = destination.joinpath(*relative.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except zipfile.BadZipFile as exc:
        raise MineruResponseError("MinerU returned a damaged ZIP archive") from exc
    selected = destination.joinpath(*markdown[0].parts)
    try:
        if not selected.read_text(encoding="utf-8").strip():
            raise MineruResponseError(
                "MinerU ZIP contains no non-empty Markdown result"
            )
    except UnicodeError as exc:
        raise MineruResponseError("MinerU Markdown is not valid UTF-8") from exc
    return selected


def _json_response(response: httpx.Response) -> dict:
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise MineruResponseError("MinerU returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise MineruResponseError("MinerU returned an invalid object")
    return payload


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MineruResponseError("MinerU returned a missing resource identifier")
    return quote(value, safe="")


def _local_upload_url(base: str, target: str) -> str:
    resolved = urljoin(base + "/", target)
    def origin(url: str) -> tuple:
        parsed = urlsplit(url)
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    if origin(base) != origin(resolved) or urlsplit(resolved).username or urlsplit(resolved).password:
        raise MineruResponseError("MinerU upload must remain on the configured local origin")
    return resolved


def _request_parse(
    file_path: Path, settings: Settings, *, parse_method: str = "auto"
) -> httpx.Response:
    # V1 jobs are asynchronous. Never replay job submission after a disconnect:
    # the service may already have accepted it.
    deadline = time.monotonic() + settings.mineru.read_timeout_seconds
    from oa_knowledge.parsers.format_router import detect_format
    detected = detect_format(file_path)
    upload_name = file_path.name
    if detected.status_code == "parseable" and file_path.suffix.lower() != "." + detected.actual_file_type:
        # OA size decorations are display metadata. Keep the protected original
        # intact, but give the local parser a filename with its actual format.
        upload_name = file_path.stem + "." + detected.actual_file_type
    with _client(settings) as client:
        with file_path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        upload = _json_response(client.post("/v1/uploads", json={
            "filename": upload_name, "bytes": file_path.stat().st_size,
            "mime_type": mimetypes.guess_type(upload_name)[0] or "application/octet-stream",
            "purpose": "parse", "sha256sum": digest,
        }))
        upload_id = _identifier(upload.get("id"))
        if upload.get("status") == "pending":
            target = upload.get("upload_url")
            if not isinstance(target, str) or not target:
                raise MineruResponseError("MinerU returned no upload URL")
            target = _local_upload_url(settings.mineru.api_url, target)
            if upload.get("upload_method", "PUT") != "PUT":
                raise MineruResponseError("MinerU returned an unsupported upload method")
            with file_path.open("rb") as source:
                client.put(target, content=source, headers=upload.get("upload_headers") or {}).raise_for_status()
            upload = _json_response(client.post(f"/v1/uploads/{upload_id}/complete"))
        if upload.get("status") != "completed":
            raise MineruResponseError("MinerU upload did not complete")
        file_id = _identifier(upload.get("file", {}).get("id"))
        job = _json_response(client.post("/v1/parse/jobs", json={
            "files": [{"source": {"type": "file_id", "file_id": file_id}}],
            "tier": settings.mineru.tier, "ocr_mode": parse_method, "output_formats": ["zip"],
        }))
        job_id = _identifier(job.get("job_id"))
        while job.get("status") in {"queued", "running"}:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MineruResponseError(f"MinerU job {job_id} exceeded the polling deadline")
            time.sleep(min(1, remaining))
            try:
                job = _json_response(client.get(f"/v1/parse/jobs/{job_id}", timeout=min(30, remaining)))
            except httpx.TransportError:
                # Polling is read-only and safe to resume after a transient
                # disconnect; retain the existing job rather than resubmit.
                continue
        if job.get("status") != "completed":
            raise MineruResponseError(f"MinerU job ended as {job.get('status')}")
        files = job.get("files")
        if not isinstance(files, list) or len(files) != 1 or files[0].get("status") != "completed":
            raise MineruResponseError("MinerU job has no complete single-file result")
        output_id = _identifier(files[0].get("output_files", {}).get("zip", {}).get("file_id"))
        # Redirects remain disabled: OA bytes must never leave the local service.
        response = client.get(f"/v1/files/{output_id}/content")
        if 300 <= response.status_code < 400:
            raise MineruResponseError("MinerU output redirects are forbidden")
        return response


def parse_with_mineru(
    file_path: Path,
    settings: Settings,
    output_dir: Path | None = None,
    *,
    profile_version: str = "legacy",
    parse_method: str = "auto",
) -> ParseResult:
    if not settings.mineru.enabled:
        raise RuntimeError("MinerU is not enabled in configuration")
    file_path = Path(file_path)
    if not file_path.is_file():
        raise FileNotFoundError(file_path)

    # The local GPU service can briefly delay its health response while publishing
    # a previous result.  Do not turn that transient condition into a document
    # conversion failure.
    health = _health_payload(settings, attempts=3)
    target_root = output_dir or file_path.parent / ".parse"
    target_root.mkdir(parents=True, exist_ok=True)
    final_dir = target_root / "mineru-api-v1"
    staging = Path(tempfile.mkdtemp(prefix=".mineru-staging-", dir=target_root))
    shutil.rmtree(staging)

    try:
        response = _request_parse(file_path, settings, parse_method=parse_method)
        if response.status_code >= 400:
            raise MineruResponseError(
                f"MinerU HTTP {response.status_code}: {response.text[:300]}"
            )
        content_type = response.headers.get("content-type", "").lower()
        prefix = response.content.lstrip()[:32].lower()
        if "html" in content_type or prefix.startswith((b"<!doctype html", b"<html")):
            raise MineruResponseError("MinerU returned HTML instead of a parse archive")

        staged_markdown = _extract_mineru_zip(response.content, staging)
        relative_markdown = staged_markdown.relative_to(staging)
        if final_dir.exists():
            shutil.rmtree(final_dir)
        staging.rename(final_dir)
        markdown_path = final_dir / relative_markdown
        text = markdown_path.read_text(encoding="utf-8")
        quality = assess_quality(text, file_path)
        version = str(
            health.get("version") or "api-v1"
        )
        return ParseResult(
            output_path=markdown_path,
            engine="mineru",
            engine_version=version,
            quality_score=quality["quality_score"],
            warnings=quality["warnings"],
            text_length=quality["text_length"],
            chinese_char_ratio=quality["chinese_char_ratio"],
            replacement_char_ratio=quality["replacement_char_ratio"],
            table_count=quality["table_count"],
            image_count=quality["image_count"],
            profile_version=profile_version,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
