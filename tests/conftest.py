from pathlib import Path

import pytest
import yaml


@pytest.fixture(autouse=True)
def isolate_xdg_runtime_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep default XDG runtime paths in each synthetic test sandbox."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"app": {"data_root": str(tmp_path / "data")}}), encoding="utf-8")
    return path
