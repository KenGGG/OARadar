from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope='session')
def migration_head() -> str:
    """Resolve the repository's migration head without opening any database."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config()
    config.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'src/oa_knowledge/db/migrations'))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


@pytest.fixture(autouse=True)
def isolate_xdg_runtime_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep default XDG runtime paths in each synthetic test sandbox."""
    home = tmp_path.parent / f"{tmp_path.name}-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Windows expanduser uses USERPROFILE rather than HOME.
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"app": {"data_root": str(tmp_path / "data")}}), encoding="utf-8")
    return path
