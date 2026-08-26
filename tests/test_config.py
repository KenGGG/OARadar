from pathlib import Path

import pytest
from pydantic import ValidationError

from oa_knowledge.config import Settings, load_settings


def test_defaults_are_local_and_depth_ten() -> None:
    settings = Settings()
    assert settings.app.privacy_mode == "local_only"
    assert settings.collector.max_attachment_depth == 10
    assert settings.archive.max_recursive_depth == 2
    assert settings.archive.max_members == 10_000
    assert not settings.storage.sqlite_path.is_absolute()
    assert settings.web.host == "127.0.0.1"
    assert settings.web.port == 2567
    assert settings.mineru.api_url == "http://127.0.0.1:58000"
    assert settings.online_audit.item_timeout_seconds == 120
    assert settings.online_audit.download_timeout_seconds == 30
    assert settings.storage.archive_dir.as_posix() == "originals"
    assert settings.database_path == settings.state_root / "oa.db"
    assert settings.browser_profile_path == settings.cache_root / "browser-profile"
    assert settings.browser.credential_profile_path is None
    assert settings.workspace_root == settings.markdown_root
    assert settings.markdown_root == settings.data_root / "markdown"
    assert settings.markdown_root != settings.archive_root
    assert settings.runtime_root.is_relative_to(settings.state_root)
    assert not settings.runtime_root.is_relative_to(settings.data_root)


def test_browser_credential_profile_must_be_absolute(tmp_path: Path) -> None:
    settings = Settings(browser={"credential_profile_path": str(tmp_path)})
    assert settings.browser.credential_profile_path == tmp_path.resolve()
    with pytest.raises(ValidationError):
        Settings(browser={"credential_profile_path": "relative-profile"})


def test_markdown_source_dir_cannot_escape_or_target_wiki() -> None:
    for value in ("../wiki", "wiki", "raw/../wiki"):
        with pytest.raises(ValidationError):
            Settings.model_validate({
                "markdown_export": {"workspace_root": "workspace", "source_markdown_dir": value}
            })


def test_llm_provider_choice_is_local_qwen_only(tmp_path: Path) -> None:
    local = Settings(llm={"enabled": True, "active_provider": "ollama"})
    assert local.llm.provider_name == "ollama"
    assert local.llm.base_url == "http://127.0.0.1:11434/v1"
    assert local.llm.model == "qwen3.5:9b"
    assert local.llm.provider_mode == "local_only"
    assert local.llm.uses_local_gpu is True

    with pytest.raises(ValidationError, match="ollama"):
        Settings(llm={"enabled": True, "active_provider": "agnes"})
    with pytest.raises(ValidationError, match="qwen3.5:9b"):
        Settings(llm={"enabled": True, "ollama_model": "another-model"})


def test_load_settings_ignores_obsolete_remote_fields_but_keeps_local_choice(tmp_path: Path) -> None:
    path = tmp_path / "legacy.yaml"
    path.write_text(
        "llm:\n  active_provider: ollama\n  ollama_model: qwen3.5:9b\n"
        "  agnes_base_url: https://example.invalid/v1\n  agnes_model: old-remote\n",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.llm.model == "qwen3.5:9b"
    assert settings.llm.provider_mode == "local_only"


def test_archive_depth_is_independent_and_bounded() -> None:
    settings = Settings.model_validate({
        "collector": {"max_attachment_depth": 10},
        "archive": {"max_recursive_depth": 1},
    })
    assert settings.collector.max_attachment_depth == 10
    assert settings.archive.max_recursive_depth == 1
    with pytest.raises(ValidationError):
        Settings.model_validate({"archive": {"max_recursive_depth": 6}})


def test_environment_overrides_data_root(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    monkeypatch.setenv("OA_APP__DATA_ROOT", str(data_root))
    assert load_settings().data_root == data_root.resolve()


@pytest.mark.parametrize("path", ["/tmp/oa.db", "../oa.db", "C:\\oa.db"])
def test_absolute_or_escaping_database_path_rejected(path: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"storage": {"sqlite_path": path}})


def test_plaintext_credentials_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("auth:\n  password: forbidden\n", encoding="utf-8")
    with pytest.raises(ValueError, match="credential"):
        load_settings(path)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "oa.example.test"])  # public-release: synthetic
def test_web_rejects_non_loopback_hosts(host: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings.model_validate({"web": {"host": host}})


def test_mineru_api_rejects_non_loopback_hosts() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings.model_validate({"mineru": {"enabled": True, "api_url": "http://192.0.2.10:58000"}})


def test_validate_feishu_runtime_config_honors_enabled_flag(monkeypatch) -> None:
    from oa_knowledge.config import validate_feishu_runtime_config

    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    monkeypatch.setenv("FEISHU_OA_SECRET", "secret")

    # enabled=False wins even when env vars are present.
    disabled = Settings(feishu={"enabled": False})
    assert validate_feishu_runtime_config(disabled) == "disabled"

    # enabled=True with a valid webhook + secret.
    ready = Settings(feishu={"enabled": True})
    assert validate_feishu_runtime_config(ready) == "ready"


def test_validate_feishu_runtime_config_detects_misconfiguration(monkeypatch) -> None:
    from oa_knowledge.config import validate_feishu_runtime_config

    # missing webhook
    monkeypatch.delenv("FEISHU_OA_WEBHOOK", raising=False)
    monkeypatch.delenv("FEISHU_OA_SECRET", raising=False)
    assert validate_feishu_runtime_config(Settings(feishu={"enabled": True})) == "missing_webhook"

    # webhook present, secret missing
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    assert validate_feishu_runtime_config(Settings(feishu={"enabled": True})) == "missing_secret"

    # non-https / non-official host
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "http://evil.example.com/open-apis/bot/v2/hook/abc")
    assert validate_feishu_runtime_config(Settings(feishu={"enabled": True})) == "invalid_webhook"
