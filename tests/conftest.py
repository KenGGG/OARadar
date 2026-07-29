from pathlib import Path

import pytest
import yaml


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"app": {"data_root": str(tmp_path / "data")}}), encoding="utf-8")
    return path
