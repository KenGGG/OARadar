from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


FORBIDDEN_KEYS = {"password", "cookie", "cookies", "authorization", "token", "secret"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppConfig(StrictModel):
    timezone: str = "Asia/Shanghai"
    data_root: Path = Path("data")
    privacy_mode: str = "local_only"


class BrowserConfig(StrictModel):
    base_url: str = "https://oa.example.invalid"
    context_path: str = "/oa"
    login_path: str = "/oa/login"
    executable_path: Path = Path("/usr/bin/google-chrome")
    user_data_dir: Path = Path("runtime/browser-profile")
    headless: bool = True
    ignore_https_errors: bool = False
    done_list_path: str = "/oa/done"
    navigation_timeout_seconds: int = Field(default=30, ge=5, le=120)

    @field_validator("user_data_dir")
    @classmethod
    def relative_profile(cls, value: Path) -> Path:
        return ensure_relative(value, "browser.user_data_dir")


class CollectorConfig(StrictModel):
    overlap_days: int = Field(default=7, ge=0)
    daily_stop_pages: int = Field(default=5, ge=1)
    max_attachment_depth: int = Field(default=10, ge=1, le=10)
    list_page_delay_seconds: float = Field(default=0.5, ge=0.2, le=10)
    item_delay_seconds: float = Field(default=0.5, ge=0.2, le=30)
    download_timeout_seconds: int = Field(default=60, ge=10, le=600)
    attachment_total_timeout_seconds: int = Field(default=900, ge=60, le=1800)
    excluded_title_patterns: tuple[str, ...] = ("出差申请", "休假申请", "请假申请", "报销单")
    exclude_title_keywords: tuple[str, ...] = ()

    @property
    def effective_exclude_title_keywords(self) -> tuple[str, ...]:
        """Canonical title-only exclusions, with the legacy key kept compatible."""
        return self.exclude_title_keywords or self.excluded_title_patterns


class StorageConfig(StrictModel):
    sqlite_path: Path = Path("state/oa.db")
    archive_dir: Path = Path("raw")
    journal_mode: str = "WAL"
    compute_sha256: bool = True

    @field_validator("sqlite_path", "archive_dir")
    @classmethod
    def relative_storage(cls, value: Path, info) -> Path:
        return ensure_relative(value, f"storage.{info.field_name}")

    @field_validator("journal_mode")
    @classmethod
    def wal_only(cls, value: str) -> str:
        if value.upper() != "WAL":
            raise ValueError("stage 1 requires SQLite WAL mode")
        return "WAL"


class ArchiveConfig(StrictModel):
    max_recursive_depth: int = Field(default=2, ge=0, le=5)
    max_members: int = Field(default=10_000, ge=1, le=100_000)
    max_member_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    max_total_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)
    max_ratio: float = Field(default=200.0, gt=1, le=10_000)


class ParserConfig(StrictModel):
    default_engine: str = "markitdown"
    supported_extensions: list[str] = [
        ".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm",
        ".txt", ".csv", ".json", ".xml", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
    ]
    max_file_size_mb: int = 100


class MineruConfig(StrictModel):
    enabled: bool = False
    api_url: str = "http://127.0.0.1:58000"
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    read_timeout_seconds: float = Field(default=1800.0, gt=0, le=7200)
    health_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    workers: int = 1
    model_source: str = "local"
    output_content_list: bool = True

    @field_validator("api_url")
    @classmethod
    def loopback_api_only(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("mineru.api_url must use a loopback HTTP address")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("mineru.api_url must not contain credentials, query, or fragment")
        return value.rstrip("/")

    @field_validator("workers")
    @classmethod
    def single_worker_only(cls, value: int) -> int:
        if value != 1:
            raise ValueError("mineru.workers must be 1")
        return value


class FeishuConfig(StrictModel):
    enabled: bool = False
    webhook_env: str = "FEISHU_OA_WEBHOOK"
    secret_env: str = "FEISHU_OA_SECRET"
    message_type: str = "interactive"
    max_items_per_section: int = 10
    redact_confidential: bool = True
    retry_attempts: int = 3


class LlmConfig(StrictModel):
    enabled: bool = False
    active_provider: str = "ollama"
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_model: str = "qwen3.5:9b"
    agnes_base_url: str = "https://apihub.agnes-ai.com/v1"
    agnes_model: str = "agnes-2.0-flash"
    provider_name: str = "newapi"
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key_env: str = "LOCAL_LLM_API_KEY"
    model: str = "glm-4.7-flash:q4_K_M"
    text_model: str = "glm-4.7-flash"
    vision_model: str = "qwen3-vl:8b"
    temperature: float = 0.1
    timeout_seconds: int = 180
    max_tokens: int = 4096
    max_retries: int = 2
    provider_mode: str = "local_only"
    supports_json_schema: bool = False
    supports_vision: bool = False
    uses_local_gpu: bool = False
    allow_confidential: bool = False
    allow_restricted: bool = False
    require_redaction: bool = True
    max_concurrency: int = 1

    @model_validator(mode="after")
    def apply_provider_choice(self) -> "LlmConfig":
        if self.active_provider not in {"ollama", "agnes"}:
            raise ValueError("llm.active_provider must be ollama or agnes")
        if self.active_provider == "ollama":
            self.provider_name = "ollama"
            self.base_url = self.ollama_base_url
            self.model = self.ollama_model
            self.text_model = self.ollama_model
            self.api_key_env = "OLLAMA_API_KEY"
            self.provider_mode = "local_only"
            self.uses_local_gpu = True
        else:
            self.provider_name = "agnes"
            self.base_url = self.agnes_base_url
            self.model = self.agnes_model
            self.text_model = self.agnes_model
            self.api_key_env = "AGNES_API_KEY"
            self.provider_mode = "approved_remote"
            self.uses_local_gpu = False
        return self


class ProcessingConfig(StrictModel):
    enabled: bool = True
    max_workers: int = 1
    batch_limit: int = 50
    cpu_weight: int = 50
    io_class: str = "idle"
    pause_on_backfill_degradation: bool = True


class WebConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=2567, ge=1024, le=65535)

    @field_validator("host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("web.host must be a loopback address")
        return value


class Settings(StrictModel):
    app: AppConfig = AppConfig()
    browser: BrowserConfig = BrowserConfig()
    collector: CollectorConfig = CollectorConfig()
    storage: StorageConfig = StorageConfig()
    archive: ArchiveConfig = ArchiveConfig()
    parser: ParserConfig = ParserConfig()
    mineru: MineruConfig = MineruConfig()
    feishu: FeishuConfig = FeishuConfig()
    llm: LlmConfig = LlmConfig()
    processing: ProcessingConfig = ProcessingConfig()
    web: WebConfig = WebConfig()

    @model_validator(mode="after")
    def local_only(self) -> "Settings":
        if self.app.privacy_mode != "local_only":
            raise ValueError("privacy_mode must be local_only")
        return self

    @property
    def data_root(self) -> Path:
        return self.app.data_root.expanduser().resolve()

    @property
    def database_path(self) -> Path:
        return self.data_root / self.storage.sqlite_path

    @property
    def archive_root(self) -> Path:
        return self.data_root / self.storage.archive_dir


def ensure_relative(value: Path, field: str) -> Path:
    if value.is_absolute() or ".." in value.parts or re.match(r"^[A-Za-z]:[\\/]", str(value)):
        raise ValueError(f"{field} must be a safe relative path")
    return value


def _reject_secrets(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ValueError(f"plaintext credential key is forbidden: {'.'.join((*path, str(key)))}")
            _reject_secrets(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secrets(child, (*path, str(index)))


def load_settings(path: Path | None = None) -> Settings:
    raw: dict[str, Any] = {}
    if path:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("configuration root must be a mapping")
        raw = loaded
    _reject_secrets(raw)
    env_root = os.getenv("OA_APP__DATA_ROOT")
    if env_root:
        raw.setdefault("app", {})["data_root"] = env_root
    env_db = os.getenv("OA_STORAGE__SQLITE_PATH")
    if env_db:
        raw.setdefault("storage", {})["sqlite_path"] = env_db
    env_web_host = os.getenv("OA_WEB__HOST")
    if env_web_host:
        raw.setdefault("web", {})["host"] = env_web_host
    env_web_port = os.getenv("OA_WEB__PORT")
    if env_web_port:
        raw.setdefault("web", {})["port"] = env_web_port
    return Settings.model_validate(raw)
