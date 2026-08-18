from pathlib import Path

from oa_knowledge.config import Settings
from oa_knowledge.enrich.context_budget import LocalModelProfile
from oa_knowledge.ops.doctor import run_doctor
from oa_knowledge.ops.health import utc_age_hours


def test_doctor_reports_discovered_local_qwen_context(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(app={"data_root": tmp_path}, llm={"enabled": True})
    tmp_path.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "oa_knowledge.ops.doctor.discover_ollama_profile",
        lambda *_args, **_kwargs: LocalModelProfile("qwen3.5:9b", 131_072, True),
    )

    check = next(item for item in run_doctor(settings) if item.name == "local_qwen")

    assert check.ok is True
    assert "131072" in check.detail


def test_utc_age_hours_treats_sqlite_naive_timestamp_as_utc() -> None:
    from datetime import UTC, datetime

    age = utc_age_hours(
        "2026-08-16T04:30:00",
        now=datetime(2026, 8, 16, 5, 0, tzinfo=UTC),
    )

    assert age == 0.5
