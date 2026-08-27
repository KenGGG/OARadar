from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

import yaml

SCHEMA_VERSION = "oa-markdown-v1"


@dataclass(frozen=True)
class ExportMetadata:
    source_relpath: str
    source_filename: str
    source_sha256: str
    source_size_bytes: int
    source_file_id: int | None = None
    source_channel: str = "done"
    oa_item_key: str | None = None
    logical_item_id: str | int | None = None
    parse_status: str = "success"
    actual_file_type: str | None = None
    actual_file_type_source: str | None = None
    parse_engine: str = "none"
    parse_engine_version: str = "unknown"
    parse_artifact_id: int | None = None
    parse_config_hash: str = ""
    quality_score: float | None = None
    page_count: int | None = None
    page_map_available: bool = False
    last_error_code: str | None = None
    generated_at: str | None = None
    schema_version: str = SCHEMA_VERSION


def render_markdown(metadata: ExportMetadata, body: str) -> str:
    relative = PurePosixPath(metadata.source_relpath.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("source_relpath must be safe and relative")
    payload = asdict(metadata)
    payload["id"] = f"oa-file:{metadata.source_file_id}" if metadata.source_file_id is not None else f"oa-file:sha256:{metadata.source_sha256}"
    payload["source_system"] = "oa"
    payload["source_extension"] = PurePosixPath(metadata.source_filename).suffix.lower()
    payload["source_relpath"] = relative.as_posix()
    payload["generated_at"] = metadata.generated_at or datetime.now(UTC).isoformat()
    payload["managed_by"] = "oaradar"
    payload = {key: value for key, value in payload.items() if value is not None}
    frontmatter = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip()
    error = f"\n- 错误代码：`{metadata.last_error_code}`" if metadata.last_error_code else ""
    return (
        f"---\n{frontmatter}\n---\n\n# {metadata.source_filename}\n\n"
        f"> [!info] 来源信息\n> - OA事项：{metadata.oa_item_key or '未知'}\n"
        f"> - 原始文件：`{metadata.source_filename}`\n> - 原始路径：`{relative.as_posix()}`\n"
        f"> - 文件哈希：`{metadata.source_sha256}`\n> - 转换引擎：{metadata.parse_engine}\n\n"
        f"## 文档内容\n\n{body.strip()}\n\n## 转换说明\n\n"
        f"- 转换状态：{metadata.parse_status}\n- 转换质量：{metadata.quality_score if metadata.quality_score is not None else '未知'}{error}\n"
    )
