import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from oa_knowledge.cli import app
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import OAManifestItem, PipelineTask


def _seed(config_file: Path) -> object:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OAManifestItem(
            oa_item_key="done:synthetic-cli", title="synthetic", list_page=1,
            processing_status="pending_download",
        ))
        session.commit()
    engine.dispose()
    return settings


def _task_count(settings) -> int:
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        count = session.scalar(select(func.count(PipelineTask.id))) or 0
    engine.dispose()
    return count


def test_done_converge_dry_run_is_read_only(config_file: Path) -> None:
    settings = _seed(config_file)

    result = CliRunner().invoke(app, ["done-converge", "--dry-run", "--config", str(config_file)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["download_created"] == 1
    assert _task_count(settings) == 0


def test_done_converge_apply_creates_task(config_file: Path) -> None:
    settings = _seed(config_file)

    result = CliRunner().invoke(app, ["done-converge", "--config", str(config_file)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert payload["download_created"] == 1
    assert _task_count(settings) == 1
