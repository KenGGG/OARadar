from pathlib import Path

import pytest

from oa_knowledge.config import load_settings


def test_env_is_read_beside_config_and_reloaded_without_restart(config_file, monkeypatch):
    monkeypatch.delenv("OA_BASE_URL", raising=False)
    env = config_file.parent / ".env"
    env.write_text('OA_BASE_URL=https://synthetic-a.invalid\nOA_CONTEXT_PATH=/seeyon\nOA_DONE_LIST_PATH="/seeyon/list?method=moreDone"\n', encoding="utf-8")
    first = load_settings(config_file)
    assert first.browser.base_url == "https://synthetic-a.invalid"
    assert first.browser.done_list_path == "/seeyon/list?method=moreDone"
    env.write_text("OA_BASE_URL=https://synthetic-b.invalid\n", encoding="utf-8")
    assert load_settings(config_file).browser.base_url == "https://synthetic-b.invalid"


def test_env_file_takes_precedence_over_stale_imported_environment(config_file, monkeypatch):
    (config_file.parent / ".env").write_text("OA_BASE_URL=https://synthetic-file.invalid\n", encoding="utf-8")
    monkeypatch.setenv("OA_BASE_URL", "https://synthetic-process.invalid")
    assert load_settings(config_file).browser.base_url == "https://synthetic-file.invalid"
    (config_file.parent / ".env").write_text("OA_BASE_URL=https://synthetic-updated.invalid\n", encoding="utf-8")
    assert load_settings(config_file).browser.base_url == "https://synthetic-updated.invalid"


@pytest.mark.parametrize("assignment", [
    "OA_BASE_URL=https://synthetic.invalid/seeyon", "OA_BASE_URL=https://user:synthetic@oa.invalid",
    "OA_BASE_URL=file:///tmp/oa", "OA_LOGIN_PATH=//other.invalid/login",
])
def test_invalid_env_connection_rejected(config_file: Path, assignment):
    (config_file.parent / ".env").write_text(assignment + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(config_file)
