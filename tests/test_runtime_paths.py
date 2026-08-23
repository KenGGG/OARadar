"""Final data directory and local runtime path contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from oa_knowledge.config import Settings
from oa_knowledge.runtime_paths import resolve_original_path


def test_settings_places_runtime_state_outside_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    cache_root = tmp_path / "cache"

    settings = Settings.model_validate({
        "app": {"data_root": data_root},
        "runtime": {"state_root": state_root, "cache_root": cache_root},
    })

    assert settings.database_path == state_root / "oa.db"
    assert settings.browser_profile_path == cache_root / "browser-profile"
    assert settings.parse_work_root == cache_root / "work"
    assert settings.originals_root == data_root / "originals"
    assert settings.markdown_root == data_root / "markdown"


def test_runtime_roots_cannot_overlap_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    with pytest.raises(ValueError, match="runtime.state_root"):
        Settings.model_validate({
            "app": {"data_root": data_root},
            "runtime": {"state_root": data_root / "state", "cache_root": tmp_path / "cache"},
        })


@pytest.mark.parametrize("relpath", ("raw/done/item/file.pdf", "originals/../state/oa.db"))
def test_resolve_original_path_rejects_legacy_and_escape_paths(
    tmp_path: Path, relpath: str,
) -> None:
    settings = Settings.model_validate({
        "app": {"data_root": tmp_path / "data"},
        "runtime": {"state_root": tmp_path / "state", "cache_root": tmp_path / "cache"},
    })
    settings.data_root.mkdir()

    with pytest.raises(ValueError):
        resolve_original_path(settings, relpath)


def test_resolve_original_path_accepts_only_final_data_root(tmp_path: Path) -> None:
    settings = Settings.model_validate({
        "app": {"data_root": tmp_path / "data"},
        "runtime": {"state_root": tmp_path / "state", "cache_root": tmp_path / "cache"},
    })
    settings.originals_root.mkdir(parents=True)

    assert resolve_original_path(settings, "originals/done/item-1/file.pdf") == (
        settings.data_root / "originals/done/item-1/file.pdf"
    )
