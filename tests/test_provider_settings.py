from pathlib import Path

import yaml

from oa_knowledge.config import load_settings
from oa_knowledge.web.provider_settings import provider_settings_view, update_provider_settings


def test_provider_settings_hide_secrets_and_update_only_safe_fields(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    monkeypatch.setenv(settings.llm.api_key_env, "synthetic-secret")
    view = provider_settings_view(settings)
    assert view["agnes"]["api_key_configured"] is True
    assert "synthetic-secret" not in str(view)

    result = update_provider_settings(config_file, {
        "agnes": {"active_provider": "agnes", "agnes_model": "agnes-synthetic", "temperature": 0.2},
        "feishu": {"enabled": True, "retry_attempts": 2},
    })
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert raw["llm"]["agnes_model"] == "agnes-synthetic"
    assert raw["feishu"]["enabled"] is True
    assert result["restart_required"] is True


def test_provider_settings_reject_credentials(config_file: Path) -> None:
    try:
        update_provider_settings(config_file, {"agnes": {"api_key": "forbidden"}})
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("credential field was accepted")
