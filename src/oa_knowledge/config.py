from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Keys whose presence in YAML means a plaintext credential is being stored inline.
# Credentials must come from environment variables instead (see AGENTS.md). Exact
# lower-cased match avoids substring false-positives (e.g. "bypass").
FORBIDDEN_KEYS = {
    "password", "pass", "pwd", "apikey", "api_key", "access_key", "secret_key",
    "client_secret", "private_key", "credential", "credentials", "bearer",
    "authorization", "token", "secret", "cookie", "cookies", "auth",
}


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
    archive_dir: Path = Path("archive/raw/oa")
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
    content_mode: str = "summary"
    max_summary_chars: int = 500
    max_action_chars: int = 200
    max_risk_items: int = 3
    include_detail_link: bool = True


# Official Feishu/Lark custom-bot webhook hosts. The webhook path itself is a
# secret, so only these hosts are ever accepted.
OFFICIAL_FEISHU_HOSTS = frozenset({"open.feishu.cn", "open.feishu.com", "www.feishu.cn"})


def validate_feishu_runtime_config(settings: "Settings") -> str:
    """Return the effective Feishu runtime state.

    Unlike simply checking for an environment variable, this honors
    ``feishu.enabled`` and distinguishes every misconfiguration so the caller
    can fail loudly instead of silently treating a broken config as success
    (plan-0805-02 §1.1). One of:

    * ``disabled``        — ``feishu.enabled`` is false; never send.
    * ``ready``           — enabled with a valid webhook and signing secret.
    * ``missing_webhook`` — enabled but the webhook env var is empty.
    * ``missing_secret``  — enabled and webhook present, but no signing secret.
    * ``invalid_webhook`` — enabled but the webhook URL fails validation.
    """
    if not settings.feishu.enabled:
        return "disabled"
    webhook = os.environ.get(settings.feishu.webhook_env, "")
    if not webhook:
        return "missing_webhook"
    parsed = urlparse(webhook)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in OFFICIAL_FEISHU_HOSTS
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not parsed.path.startswith("/open-apis/bot/v2/hook/")
    ):
        return "invalid_webhook"
    secret = os.environ.get(settings.feishu.secret_env, "")
    if not secret:
        return "missing_secret"
    return "ready"


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


class MarkdownExportConfig(StrictModel):
    enabled: bool = True
    workspace_root: Path = Path("workspace")
    source_markdown_dir: Path = Path("raw/sources/oa")
    preserve_source_tree: bool = True
    filename_mode: str = "append_md"
    write_frontmatter: bool = True
    write_assets: bool = True
    atomic_publish: bool = True
    keep_last_success_on_failure: bool = True
    generate_failure_stub: bool = True

    @field_validator("source_markdown_dir")
    @classmethod
    def safe_source_dir(cls, value: Path) -> Path:
        value = ensure_relative(value, "markdown_export.source_markdown_dir")
        if value.parts and value.parts[0].casefold() == "wiki":
            raise ValueError("Markdown sources must not target workspace/wiki")
        return value

    @field_validator("filename_mode")
    @classmethod
    def append_md_only(cls, value: str) -> str:
        if value != "append_md":
            raise ValueError("markdown_export.filename_mode must be append_md")
        return value


class ConversionConfig(StrictModel):
    incremental: bool = True
    workers: int = Field(default=1, ge=1, le=1)
    force_rebuild: bool = False


class OnlineAuditConfig(StrictModel):
    item_timeout_seconds: int = Field(default=120, ge=30, le=600)
    download_timeout_seconds: int = Field(default=30, ge=5, le=120)


class PendingCleanupConfig(StrictModel):
    """Retention policy for short-lived pending (待办) notification data (plan-0807-1 §6.5).

    Pending items are not permanent knowledge assets. Once Feishu confirms a
    successful delivery the business payload (body, opinion text, page snapshots,
    temporary attachments, summaries) is erased, leaving only the minimal ledger
    required to prevent duplicate notifications.
    """

    auto_cleanup_after_success: bool = True
    cleanup_delay_hours: float = Field(default=0.0, ge=0, le=720)
    failed_retention_days: int = Field(default=30, ge=1, le=3650)
    keep_summary_body: bool = False
    keep_page_snapshot: bool = False
    keep_temp_attachments: bool = False
    allow_force_cleanup: bool = True


class WebConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=2567, ge=1024, le=65535)
    require_auth: bool = Field(default=False, description="Gate /api/* behind a one-time bootstrap token")

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
    markdown_export: MarkdownExportConfig = MarkdownExportConfig()
    conversion: ConversionConfig = ConversionConfig()
    online_audit: OnlineAuditConfig = OnlineAuditConfig()
    pending_cleanup: PendingCleanupConfig = PendingCleanupConfig()
    web: WebConfig = WebConfig()

    @model_validator(mode="after")
    def local_only(self) -> "Settings":
        if self.app.privacy_mode != "local_only":
            raise ValueError("privacy_mode must be local_only")
        if self.markdown_root == self.archive_root.resolve():
            raise ValueError("Markdown output must not target the raw archive")
        if self.markdown_root == (self.workspace_root / "wiki").resolve():
            raise ValueError("OARadar must not write workspace/wiki")
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

    @property
    def workspace_root(self) -> Path:
        value = self.markdown_export.workspace_root.expanduser()
        return value.resolve() if value.is_absolute() else (self.data_root / value).resolve()

    @property
    def markdown_root(self) -> Path:
        return (self.workspace_root / self.markdown_export.source_markdown_dir).resolve()


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
    env_web_require_auth = os.getenv("OA_WEB__REQUIRE_AUTH")
    if env_web_require_auth:
        raw.setdefault("web", {})["require_auth"] = env_web_require_auth.strip().lower() in {"1", "true", "yes", "on"}
    return Settings.model_validate(raw)
