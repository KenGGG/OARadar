"""Synthetic contracts for publishing Markdown solely from rebuilt evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, PipelineRun, RebuildOutput
from oa_knowledge.rebuild.markdown import (
    BodySourceDuplicateError,
    RebuildPublicationError,
    publish_rebuilt_attachment,
    publish_rebuilt_body,
)
from oa_knowledge.rebuild.parser import _tree_sha256
from oa_knowledge.rebuild.paths import resolve_rebuild_path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    value = Settings(
        app={"data_root": tmp_path / "live-data"},
        rebuild={"target_root": tmp_path / "clean-rebuild"},
    )
    value.data_root.mkdir(parents=True)
    return value


@pytest.fixture
def session(settings: Settings):
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as value:
        yield value


@pytest.fixture
def run_id(session: Session) -> int:
    run = PipelineRun(run_key="synthetic-markdown-run", pipeline_type="data_rebuild")
    session.add(run)
    session.commit()
    return run.id


def _item(
    session: Session,
    *,
    key: str,
    numbered: bool = True,
    external: bool = False,
    title: str = "合成事项",
) -> OAItem:
    value = OAItem(
        oa_item_key=f"done:{key}",
        workitem_id_text=f"WORK-{key}",
        source_channel="done",
        title=title,
        document_number="示例〔2026〕12号" if numbered else None,
        document_date=date(2026, 8, 20),
        initiated_at=datetime(2026, 8, 19, tzinfo=UTC),
        classification_state="confirmed",
        classification_source="manual",
        source_type="external" if external else "internal",
        internal_category=None if external else "风险管理",
        external_issuer="示例市工业和信息化局" if external else None,
    )
    session.add(value)
    session.commit()
    return value


def _source(
    session: Session,
    settings: Settings,
    run_id: int,
    item: OAItem,
    *,
    name: str = "普通附件.pdf",
    role: str = "direct_attachment",
    copied: bytes = b"synthetic original",
) -> tuple[ArchivedFile, RebuildOutput]:
    digest = hashlib.sha256(copied).hexdigest()
    source = ArchivedFile(
        oa_item_id=item.id,
        original_name=name,
        attachment_key=f"attachment:{item.id}:{name}:{role}",
        file_role=role,
        source_container_key="root",
        local_relpath=f"archive/raw/oa/done/private/{name}",
        size_bytes=len(copied),
        sha256=digest,
        download_status="verified",
    )
    session.add(source)
    session.flush()
    relpath = f"archive/oa/done/synthetic/{source.id}-original.bin"
    target = resolve_rebuild_path(settings, relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(copied)
    output = RebuildOutput(
        run_id=run_id,
        oa_item_id=item.id,
        source_file_id=source.id,
        kind="original",
        target_relpath=relpath,
        sha256=digest,
        status="success",
        error_code=None,
    )
    session.add(output)
    session.commit()
    return source, output


def _parse(
    session: Session,
    settings: Settings,
    run_id: int,
    source: ArchivedFile,
    *,
    status: str = "success",
    error_code: str | None = None,
) -> RebuildOutput:
    relpath = f"parse/{run_id}/{source.id}/{source.sha256}/stub"
    target = resolve_rebuild_path(settings, relpath)
    if status == "success":
        product = target / "stub-v2"
        (product / "images").mkdir(parents=True, exist_ok=True)
        (product / "document.md").write_text(
            "# 最新解析正文\n\n![示意图](images/figure.png)\n", encoding="utf-8"
        )
        (product / "images" / "figure.png").write_bytes(b"synthetic image")
        (product / "quality.json").write_text("{}\n", encoding="utf-8")
        (target / ".oaradar-parse.json").write_text(
            json.dumps(
                {
                    "engine": "stub",
                    "engine_version": "synthetic-health-v2",
                    "source_file_id": source.id,
                    "source_sha256": source.sha256,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        digest = _tree_sha256(target)
        assert digest is not None
    else:
        digest = None
    output = RebuildOutput(
        run_id=run_id,
        oa_item_id=source.oa_item_id,
        source_file_id=source.id,
        kind="parse",
        target_relpath=relpath if status == "success" else f"{relpath}/failed",
        sha256=digest,
        status=status,
        error_code=error_code,
    )
    session.add(output)
    session.commit()
    return output


def _read_output(settings: Settings, output: RebuildOutput) -> str:
    return resolve_rebuild_path(settings, output.target_relpath).read_text(
        encoding="utf-8"
    )


def _frontmatter(content: str) -> dict[str, object]:
    return yaml.safe_load(content.split("---", 2)[1])


def test_numbered_attachment_body_is_named_and_not_republished(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """Dropping body-source exclusion would create two Markdown copies of one attachment."""
    item = _item(session, key="numbered-body")
    source, _ = _source(
        session, settings, run_id, item, name="合成正文.pdf", role="official_body"
    )
    _parse(session, settings, run_id, source)

    output = publish_rebuilt_body(session, settings, run_id, item.id)

    assert output is not None
    assert output.kind == "body_markdown"
    assert output.target_relpath.endswith("示例〔2026〕12号-合成事项-正文.md")
    assert "最新解析正文" in _read_output(settings, output)
    with pytest.raises(BodySourceDuplicateError):
        publish_rebuilt_attachment(session, settings, run_id, source.id)


def test_numbered_page_body_fallback_uses_verified_current_run_snapshot(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """A numbered item without a matching attachment may use only its verified copied page body."""
    item = _item(session, key="page-body")
    snapshot, _ = _source(
        session,
        settings,
        run_id,
        item,
        name="body.html",
        role="body_snapshot",
        copied=b"<article><h1>Synthetic page body</h1><script>secret()</script></article>",
    )

    output = publish_rebuilt_body(session, settings, run_id, item.id)

    assert output is not None and output.source_file_id == snapshot.id
    content = _read_output(settings, output)
    assert "Synthetic page body" in content
    assert "secret()" not in content
    assert _frontmatter(content)["parser_name"] == "page_body"


def test_item_without_number_has_no_body(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """Adding a page snapshot must not create a body for an unnumbered item."""
    item = _item(session, key="unnumbered", numbered=False)
    _source(
        session,
        settings,
        run_id,
        item,
        name="body.html",
        role="body_snapshot",
        copied=b"<p>body</p>",
    )

    assert publish_rebuilt_body(session, settings, run_id, item.id) is None
    assert (
        session.scalar(
            select(RebuildOutput).where(RebuildOutput.kind == "body_markdown")
        )
        is None
    )


def test_attachment_requires_verified_parse_and_preserves_assets(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """Publishing must preserve parser Markdown/assets and never read the live archive path."""
    item = _item(session, key="attachment", numbered=False)
    source, _ = _source(session, settings, run_id, item, name="预算表.xlsx")
    live = settings.data_root / (source.local_relpath or "")
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"private live bytes")
    _parse(session, settings, run_id, source)

    output = publish_rebuilt_attachment(session, settings, run_id, source.id)

    assert output.kind == "attachment_markdown"
    assert output.target_relpath.endswith("预算表.xlsx.md")
    content = _read_output(settings, output)
    assert "最新解析正文" in content
    assert "private live bytes" not in content
    assert f"assets/{source.id}/stub-v2/images/figure.png" in content
    asset = (
        resolve_rebuild_path(settings, output.target_relpath).parent
        / "assets"
        / str(source.id)
        / "stub-v2/images/figure.png"
    )
    assert asset.read_bytes() == b"synthetic image"


def test_nested_parser_links_are_rebased_without_parent_traversal(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """A parser-local parent link must become a safe final link inside the item assets tree."""
    item = _item(session, key="nested-assets", numbered=False)
    source, _ = _source(session, settings, run_id, item, name="图文.pdf")
    parsed = _parse(session, settings, run_id, source)
    parse_root = resolve_rebuild_path(settings, parsed.target_relpath)
    product = parse_root / "stub-v2"
    (product / "document.md").unlink()
    (product / "documents").mkdir()
    (product / "documents" / "document.md").write_text(
        "![示意图](../images/figure.png)\n", encoding="utf-8"
    )
    parsed.sha256 = _tree_sha256(parse_root)
    session.commit()

    output = publish_rebuilt_attachment(session, settings, run_id, source.id)

    content = _read_output(settings, output)
    assert "../" not in content
    assert f"assets/{source.id}/stub-v2/images/figure.png" in content


def test_parser_file_uri_is_rejected_without_path_disclosure(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """A parser-produced host file URI must never enter delivered Markdown."""
    item = _item(session, key="file-uri", numbered=False)
    source, _ = _source(session, settings, run_id, item)
    parsed = _parse(session, settings, run_id, source)
    parse_root = resolve_rebuild_path(settings, parsed.target_relpath)
    (parse_root / "stub-v2" / "document.md").write_text(
        "[private](file:///home/example/private.txt)\n", encoding="utf-8"  # public-release: synthetic
    )
    parsed.sha256 = _tree_sha256(parse_root)
    session.commit()

    with pytest.raises(RebuildPublicationError, match="UNSAFE_PARSE_LINK"):
        publish_rebuilt_attachment(session, settings, run_id, source.id)


@pytest.mark.parametrize(
    ("status", "error_code"),
    (("failed", "RUNTIMEERROR"), ("failed", "UNSUPPORTED_FORMAT")),
)
def test_failed_or_unsupported_parse_is_refused(
    session: Session,
    settings: Settings,
    run_id: int,
    status: str,
    error_code: str,
) -> None:
    """Terminal unsupported and retryable failures are never usable parse artifacts."""
    item = _item(session, key=f"refuse-{error_code}", numbered=False)
    source, _ = _source(session, settings, run_id, item)
    _parse(session, settings, run_id, source, status=status, error_code=error_code)

    with pytest.raises(RebuildPublicationError, match=error_code):
        publish_rebuilt_attachment(session, settings, run_id, source.id)


def test_terminal_unsupported_refuses_older_successful_parse(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """A durable unsupported decision must not fall back to an older parse success."""
    item = _item(session, key="unsupported-over-success", numbered=False)
    source, _ = _source(session, settings, run_id, item)
    _parse(session, settings, run_id, source)
    _parse(
        session,
        settings,
        run_id,
        source,
        status="failed",
        error_code="UNSUPPORTED_FORMAT",
    )

    with pytest.raises(RebuildPublicationError, match="UNSUPPORTED_FORMAT"):
        publish_rebuilt_attachment(session, settings, run_id, source.id)


@pytest.mark.parametrize("mutation", ("tampered", "missing", "stale"))
def test_tampered_missing_or_stale_parse_is_refused(
    session: Session,
    settings: Settings,
    run_id: int,
    mutation: str,
) -> None:
    """Changing either parse bytes or its ledger identity must fence publication."""
    item = _item(session, key=f"invalid-{mutation}", numbered=False)
    source, _ = _source(session, settings, run_id, item)
    parsed = _parse(session, settings, run_id, source)
    target = resolve_rebuild_path(settings, parsed.target_relpath)
    if mutation == "tampered":
        (target / "stub-v2" / "document.md").write_text("tampered", encoding="utf-8")
    elif mutation == "missing":
        (target / "stub-v2" / "document.md").unlink()
    else:
        parsed.status = "failed"
        parsed.error_code = "STALE"
        session.commit()

    with pytest.raises(RebuildPublicationError):
        publish_rebuilt_attachment(session, settings, run_id, source.id)


def test_idempotent_replay_reuses_verified_output_and_other_run_cannot_overwrite(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """A verified replay converges, while a different run cannot claim its path."""
    item = _item(session, key="ownership", numbered=False)
    source, _ = _source(session, settings, run_id, item)
    _parse(session, settings, run_id, source)
    first = publish_rebuilt_attachment(session, settings, run_id, source.id)

    second = publish_rebuilt_attachment(session, settings, run_id, source.id)

    assert second.id == first.id
    other_run = PipelineRun(run_key="synthetic-other-run", pipeline_type="data_rebuild")
    session.add(other_run)
    session.commit()
    with pytest.raises(RebuildPublicationError, match="TARGET_CONFLICT"):
        publish_rebuilt_attachment(session, settings, other_run.id, source.id)


def test_source_ledger_change_after_staging_cannot_publish(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """A same-row parse mutation immediately before promotion must fail closed."""
    item = _item(session, key="race", numbered=False)
    source, _ = _source(session, settings, run_id, item)
    parsed = _parse(session, settings, run_id, source)
    import oa_knowledge.rebuild.markdown as module

    original_stage = module._stage_parsed_markdown

    def stage_then_mutate(*args, **kwargs):
        staged = original_stage(*args, **kwargs)
        with Session(session.get_bind()) as concurrent:
            fresh = concurrent.get(RebuildOutput, parsed.id)
            assert fresh is not None
            fresh.status = "failed"
            fresh.error_code = "CONCURRENT_CHANGE"
            concurrent.commit()
        return staged

    monkeypatch.setattr(module, "_stage_parsed_markdown", stage_then_mutate)

    with pytest.raises(RebuildPublicationError, match="SOURCE_CHANGED"):
        publish_rebuilt_attachment(session, settings, run_id, source.id)
    item_outputs = list(
        session.scalars(
            select(RebuildOutput).where(RebuildOutput.kind == "attachment_markdown")
        )
    )
    assert all(output.status != "success" for output in item_outputs)


@pytest.mark.parametrize("external", (False, True))
def test_frontmatter_has_complete_classification_without_absolute_paths(
    session: Session,
    settings: Settings,
    run_id: int,
    external: bool,
) -> None:
    """Removing either classification branch or leaking host paths corrupts delivered metadata."""
    item = _item(session, key=f"metadata-{external}", numbered=False, external=external)
    source, _ = _source(session, settings, run_id, item)
    _parse(session, settings, run_id, source)

    content = _read_output(
        settings, publish_rebuilt_attachment(session, settings, run_id, source.id)
    )
    header = _frontmatter(content)

    assert header == {
        "title": "合成事项",
        "oa_item_id": f"WORK-metadata-{external}",
        "document_number": None,
        "effective_date": "2026-08-20",
        "source_type": "external" if external else "internal",
        "internal_category": None if external else "风险管理",
        "external_issuer": "示例市工业和信息化局" if external else None,
        "source_sha256": source.sha256,
        "parser_name": "stub",
        "parser_version": "synthetic-health-v2",
    }
    assert str(settings.data_root) not in content
    assert str(settings.rebuild.target_root) not in content


def test_long_multibyte_attachment_filename_is_safe_and_bounded(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """Appending .md must not push a sanitized multibyte component past 240 bytes."""
    item = _item(session, key="long-name", numbered=False)
    source, _ = _source(session, settings, run_id, item, name=("附件" * 100) + ".pdf")
    _parse(session, settings, run_id, source)

    output = publish_rebuilt_attachment(session, settings, run_id, source.id)

    component = Path(output.target_relpath).name
    assert component.endswith(".pdf.md")
    assert len(component.encode("utf-8")) <= 240
    assert ".." not in Path(output.target_relpath).parts


@pytest.mark.parametrize(
    "role", ("body_snapshot", "workflow_snapshot", "metadata_snapshot")
)
def test_non_attachment_evidence_cannot_publish_as_ordinary_attachment(
    session: Session,
    settings: Settings,
    run_id: int,
    role: str,
) -> None:
    """System snapshots are evidence, never ordinary attachment Markdown sources."""
    item = _item(session, key=f"evidence-role-{role}", numbered=False)
    source, _ = _source(
        session, settings, run_id, item, name="snapshot.html", role=role
    )
    _parse(session, settings, run_id, source)

    with pytest.raises(RebuildPublicationError, match="INVALID_ATTACHMENT_ROLE"):
        publish_rebuilt_attachment(session, settings, run_id, source.id)


def test_page_body_text_and_fingerprint_come_from_same_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """A second query must not pair older text with a newer snapshot fingerprint."""
    item = _item(session, key="same-page-evidence")
    first, _ = _source(
        session,
        settings,
        run_id,
        item,
        name="first.html",
        role="body_snapshot",
        copied=b"<p>first body</p>",
    )
    second, _ = _source(
        session,
        settings,
        run_id,
        item,
        name="second.html",
        role="body_snapshot",
        copied=b"<p>second body</p>",
    )
    assert second.id > first.id

    def forbidden_legacy_loader(*_args, **_kwargs):
        pytest.fail("publisher must consume one atomic text+evidence result")

    monkeypatch.setattr(
        "oa_knowledge.rebuild.body_source.load_verified_page_body",
        forbidden_legacy_loader,
    )

    output = publish_rebuilt_body(session, settings, run_id, item.id)

    assert output is not None and output.source_file_id == second.id
    assert "second body" in _read_output(settings, output)
    assert "first body" not in _read_output(settings, output)


def test_item_metadata_mutation_at_publish_entry_is_fenced(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """Routing/frontmatter changes at the final helper boundary cannot publish stale bytes."""
    item = _item(session, key="item-fence", numbered=False)
    source, _ = _source(session, settings, run_id, item)
    _parse(session, settings, run_id, source)
    import oa_knowledge.rebuild.markdown as module

    original_publish = module._publish_staged

    def mutate_then_publish(*args, **kwargs):
        with Session(session.get_bind()) as concurrent:
            fresh = concurrent.get(OAItem, item.id)
            assert fresh is not None
            fresh.internal_category = "财务资金"
            concurrent.commit()
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(module, "_publish_staged", mutate_then_publish)

    with pytest.raises(RebuildPublicationError, match="SOURCE_CHANGED"):
        publish_rebuilt_attachment(session, settings, run_id, source.id)
    assert not list(resolve_rebuild_path(settings, "markdown").rglob("*.md"))


def test_body_role_mutation_at_publish_entry_is_fenced(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """A selected body must still be the selected body at the atomic promotion boundary."""
    item = _item(session, key="body-role-fence")
    source, _ = _source(
        session, settings, run_id, item, name="正文.pdf", role="official_body"
    )
    _parse(session, settings, run_id, source)
    import oa_knowledge.rebuild.markdown as module

    original_publish = module._publish_staged

    def mutate_then_publish(*args, **kwargs):
        with Session(session.get_bind()) as concurrent:
            fresh = concurrent.get(ArchivedFile, source.id)
            assert fresh is not None
            fresh.file_role = "metadata_snapshot"
            concurrent.commit()
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(module, "_publish_staged", mutate_then_publish)

    with pytest.raises(RebuildPublicationError, match="SOURCE_CHANGED"):
        publish_rebuilt_body(session, settings, run_id, item.id)


@pytest.mark.parametrize("failure_point", ("after_assets", "after_hardlink"))
def test_partial_self_owned_publication_can_retry_without_deleting_other_outputs(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    settings: Settings,
    run_id: int,
    failure_point: str,
) -> None:
    """A process fault after either atomic promotion is reconciled by exact-byte replay."""
    item = _item(session, key=f"partial-{failure_point}", numbered=False)
    first, _ = _source(session, settings, run_id, item, name="first.pdf")
    second, _ = _source(session, settings, run_id, item, name="second.pdf")
    _parse(session, settings, run_id, first)
    _parse(session, settings, run_id, second)
    other = publish_rebuilt_attachment(session, settings, run_id, second.id)
    other_bytes = resolve_rebuild_path(settings, other.target_relpath).read_bytes()
    import oa_knowledge.rebuild.markdown as module

    if failure_point == "after_assets":
        real_link = module.os.link

        def fail_link(*_args, **_kwargs):
            raise OSError("synthetic fault after assets")

        monkeypatch.setattr(module.os, "link", fail_link)
    else:
        real_hash = module._publication_sha
        calls = 0

        def fail_final_hash(markdown, assets):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic fault after hardlink")
            return real_hash(markdown, assets)

        monkeypatch.setattr(module, "_publication_sha", fail_final_hash)

    with pytest.raises(OSError):
        publish_rebuilt_attachment(session, settings, run_id, first.id)

    if failure_point == "after_assets":
        monkeypatch.setattr(module.os, "link", real_link)
    else:
        monkeypatch.setattr(module, "_publication_sha", real_hash)
    recovered = publish_rebuilt_attachment(session, settings, run_id, first.id)

    assert recovered.status == "success"
    assert (
        resolve_rebuild_path(settings, other.target_relpath).read_bytes() == other_bytes
    )


def test_pending_owner_replays_and_reconciles_exact_existing_artifacts(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """A crash-left pending ledger row can finish from its exact owned bytes."""
    item = _item(session, key="pending-replay", numbered=False)
    source, _ = _source(session, settings, run_id, item)
    _parse(session, settings, run_id, source)
    first = publish_rebuilt_attachment(session, settings, run_id, source.id)
    first.status = "pending"
    first.error_code = None
    session.commit()

    replayed = publish_rebuilt_attachment(session, settings, run_id, source.id)

    assert replayed.id == first.id
    assert replayed.status == "success"


def test_interleaved_runs_have_one_global_target_owner(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """SQLite's per-run unique key must not allow two runs to claim one final path."""
    item = _item(session, key="global-owner", numbered=False)
    source, original = _source(session, settings, run_id, item)
    parsed = _parse(session, settings, run_id, source)
    other = PipelineRun(
        run_key="synthetic-interleaved-run", pipeline_type="data_rebuild"
    )
    session.add(other)
    session.flush()
    session.add_all(
        (
            RebuildOutput(
                run_id=other.id,
                oa_item_id=item.id,
                source_file_id=source.id,
                kind="original",
                target_relpath=original.target_relpath,
                sha256=original.sha256,
                status="success",
                error_code=None,
            ),
            RebuildOutput(
                run_id=other.id,
                oa_item_id=item.id,
                source_file_id=source.id,
                kind="parse",
                target_relpath=parsed.target_relpath,
                sha256=parsed.sha256,
                status="success",
                error_code=None,
            ),
        )
    )
    session.commit()
    import oa_knowledge.rebuild.markdown as module

    real_reserve = module._reserve_output
    entered = False

    def interleave(*args, **kwargs):
        nonlocal entered
        if not entered:
            entered = True
            publish_rebuilt_attachment(session, settings, other.id, source.id)
        return real_reserve(*args, **kwargs)

    monkeypatch.setattr(module, "_reserve_output", interleave)

    with pytest.raises(RebuildPublicationError, match="TARGET_CONFLICT"):
        publish_rebuilt_attachment(session, settings, run_id, source.id)
    owners = list(
        session.scalars(
            select(RebuildOutput).where(
                RebuildOutput.kind == "attachment_markdown",
                RebuildOutput.target_relpath.like("markdown/%"),
            )
        )
    )
    assert (
        len(owners) == 1
        and owners[0].run_id == other.id
        and owners[0].status == "success"
    )


def test_angle_wrapped_spaced_parser_asset_link_is_preserved_safely(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """Valid Markdown angle targets with spaces must be parsed without truncation."""
    item = _item(session, key="spaced-link", numbered=False)
    source, _ = _source(session, settings, run_id, item)
    parsed = _parse(session, settings, run_id, source)
    root = resolve_rebuild_path(settings, parsed.target_relpath)
    product = root / "stub-v2"
    (product / "images" / "figure space.png").write_bytes(b"spaced asset")
    (product / "document.md").write_text(
        '![figure](<images/figure space.png> "caption")\n', encoding="utf-8"
    )
    parsed.sha256 = _tree_sha256(root)
    session.commit()

    output = publish_rebuilt_attachment(session, settings, run_id, source.id)

    content = _read_output(settings, output)
    assert f"assets/{source.id}/stub-v2/images/figure space.png" in content
    assert "caption" in content


def test_mineru_frontmatter_uses_persisted_health_version_not_directory_guess(
    session: Session,
    settings: Settings,
    run_id: int,
) -> None:
    """MinerU's actual reported version must survive even when its folder name is generic."""
    item = _item(session, key="mineru-version", numbered=False)
    source, _ = _source(session, settings, run_id, item)
    parsed = _parse(session, settings, run_id, source)
    root = resolve_rebuild_path(settings, parsed.target_relpath)
    (root / "stub-v2").rename(root / "mineru-api-v1")
    (root / ".oaradar-parse.json").write_text(
        json.dumps(
            {
                "engine": "mineru",
                "engine_version": "health-protocol-2026.8",
                "source_file_id": source.id,
                "source_sha256": source.sha256,
            }
        ),
        encoding="utf-8",
    )
    parsed.sha256 = _tree_sha256(root)
    session.commit()

    output = publish_rebuilt_attachment(session, settings, run_id, source.id)
    header = _frontmatter(_read_output(settings, output))

    assert header["parser_name"] == "mineru"
    assert header["parser_version"] == "health-protocol-2026.8"
