"""V2 Markdown Delivery classification and item-index tests."""

import sqlite3

import pytest
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ClassificationDecision, ClassificationRun, MarkdownExport, OAItem
from oa_knowledge.markdown_delivery import _classification_directory, classify_done_item, publish_item_index


def _item(session, *, title: str, sender: str | None, document_number: str | None = None) -> OAItem:
    item = OAItem(
        oa_item_key="done:classify", source_channel="done", title=title,
        sender=sender, document_number=document_number, pipeline_status="files_verified",
    )
    session.add(item)
    session.flush()
    return item


def _current_internal_decision(session, item: OAItem) -> None:
    run = ClassificationRun(
        run_id="synthetic-markdown-delivery", run_kind="full", status="completed",
        input_signature="a" * 64, manifest_sha256="a" * 64, exclusion_policy_sha256="b" * 64,
        rule_version="synthetic", schema_version="synthetic", prompt_version="synthetic",
        model_name="synthetic", private_config_sha256="c" * 64, target_count=1, excluded_count=0,
    )
    session.add(run); session.flush()
    session.add(ClassificationDecision(
        classification_run_id=run.id, oa_item_key=item.oa_item_key, version=1, is_current=True,
        decision_input_sha256="d" * 64, decision_source="manual", classification_status="classified",
        content_integrity_status="no_attachment_confirmed", content_origin="internal", flow_type="approval",
        initiator_type="internal", transfer_chain_json="[]", normalized_title=item.title,
        business_category="08_行政采购与信息化", classification_confidence=1.0,
        classification_reason_json="{}", rule_version="synthetic", private_config_sha256="c" * 64,
        manual_locked=True, actor="synthetic",
    ))
    session.flush()


def test_internal_classification_updates_only_current_four_fields(tmp_path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _item(session, title="关于预算资金管理的内部通知", sender="本公司财务部")
        classify_done_item(session, item.oa_item_key)

        assert item.source_type == "internal"
        assert item.internal_category == "财务资金"
        assert item.external_issuer is None
        assert item.classification_version == "v2-rules"


def test_external_classification_uses_sender_as_normalized_issuer(tmp_path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _item(
            session, title="关于开展专项检查的通知", sender="示例省国资委",
            document_number="示例国资〔2026〕1号",
        )
        classify_done_item(session, item.oa_item_key)

        assert item.source_type == "external"
        assert item.internal_category is None
        assert item.external_issuer == "示例省国资委"
        assert item.classification_version == "v2-rules"


def test_item_index_ledger_migration_enforces_document_kind_and_unique_item_schema(tmp_path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)

    with sqlite3.connect(settings.database_path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'markdown_exports'"
        ).fetchone()[0]
        indexes = connection.execute("PRAGMA index_list('markdown_exports')").fetchall()

    assert "ck_markdown_export_document_kind" in table_sql
    assert any(index[1] == "uq_markdown_export_item_index_schema" for index in indexes)


def test_no_attachment_item_publishes_stable_index_without_moving_archive_path(tmp_path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _item(session, title="内部工作会议", sender="本公司综合部")
        item.archive_relpath = "originals/done/synthetic/item"
        classify_done_item(session, item.oa_item_key)
        _current_internal_decision(session, item)
        first = publish_item_index(session, settings, item.oa_item_key)
        first_mtime = first.stat().st_mtime_ns
        second = publish_item_index(session, settings, item.oa_item_key)
        exports = session.query(MarkdownExport).all()

    assert first == second
    assert second.stat().st_mtime_ns == first_mtime
    content = second.read_text(encoding="utf-8")
    assert "source_type: \"internal\"" in content
    assert "无附件" in content
    assert len(exports) == 1
    assert exports[0].oa_item_id == item.id
    assert exports[0].document_kind == "item_index"
    assert exports[0].status == "success"
    assert exports[0].source_file_id is None


def test_legacy_classifier_cannot_bypass_current_classification_decision(tmp_path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _item(session, title="内部工作会议", sender="本公司综合部")
        item.archive_relpath = "originals/done/synthetic/item"
        classify_done_item(session, item.oa_item_key)

        with pytest.raises(ValueError, match="current classified decision"):
            publish_item_index(session, settings, item.oa_item_key)


def test_numbered_internal_category_uses_the_existing_markdown_directory(tmp_path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _item(session, title="通知存款", sender="本公司")
        item.source_type = "internal"
        item.internal_category = "04_财务资金与融资"

        assert _classification_directory(item).as_posix() == "内部/财务资金"


def test_readable_oa_title_directories_do_not_merge_same_title_items():
    from oa_knowledge.markdown_delivery import _item_leaf
    a = OAItem(oa_item_key='done:-11', title='关于年度工作的通知', source_type='external', external_issuer='合成机关')
    b = OAItem(oa_item_key='done:12', title=a.title, source_type='external', external_issuer='合成机关')
    assert _classification_directory(a).as_posix() == '外部/合成机关'
    assert _item_leaf(a).startswith('关于年度工作的通知')
    assert _item_leaf(a) != _item_leaf(b)
    a.title = '../../危险/标题'
    assert '/' not in _item_leaf(a)


def test_external_leaf_prefixes_only_a_normalized_confirmed_document_number():
    from oa_knowledge.markdown_delivery import _item_leaf

    external = OAItem(
        oa_item_key='done:external-number', source_type='external',
        external_issuer='合成机关', document_number=' 合成发 [ 2026 ] 0007 号 ',
        title='【公告】合成发〔2026〕0007号 关于事项的通知',
    )
    no_number = OAItem(
        oa_item_key='done:external-no-number', source_type='external',
        external_issuer='合成机关', title='关于事项的通知',
    )

    assert _item_leaf(external).startswith('合成发〔2026〕0007号 - 关于事项的通知__')
    assert _item_leaf(no_number).startswith('关于事项的通知__')


def test_external_display_name_is_clean_and_collision_safe(tmp_path):
    from oa_knowledge.markdown_delivery import _item_leaf
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        a = _item(session, title="【公告】合成发〔2026〕7号-关于事项的通知", sender=None)
        a.source_type = 'external'; a.external_issuer = '合成机关'
        base = _item_leaf(a)
        assert base == '合成发〔2026〕7号 - 关于事项的通知'
        session.add(MarkdownExport(
            oa_item_id=a.id, document_kind='item_index', source_sha256='a'*64,
            source_relpath='synthetic', markdown_relpath=f'外部/合成机关/{base}/_index.md',
            parse_engine='synthetic', parse_engine_version='1', parse_config_hash='b'*64,
            schema_version='test', status='success',
        ))
        b = OAItem(oa_item_key='done:another', source_channel='done', title=a.title,
                   source_type='external', external_issuer=a.external_issuer)
        session.add(b); session.flush()
        assert _item_leaf(a) == base
        assert _item_leaf(b).startswith(base + '__')


def test_document_number_selection_rejects_cited_number_and_keeps_current_header():
    from oa_knowledge.markdown_delivery import _extract_current_document_number

    text = '''\n合成市财政局\n合成财〔2026〕7号\n关于开展专项工作的通知\n\n根据合成政〔2025〕99号文件要求，现将有关事项通知如下。\n'''
    match = _extract_current_document_number(text)
    assert match is not None
    assert match.normalized == '合成财〔2026〕7号'
    assert match.quote == '合成财〔2026〕7号'


def test_document_number_selection_leaves_multiple_independent_documents_unset():
    from oa_knowledge.markdown_delivery import _extract_current_document_number

    text = '''\n甲局\n甲发〔2026〕1号\n关于甲事项的通知\n\n乙局\n乙发〔2026〕2号\n关于乙事项的通知\n'''
    assert _extract_current_document_number(text) is None


def test_external_index_records_document_number_and_issuer_evidence(tmp_path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _item(session, title='【公告】合成发〔2026〕7号 关于事项的通知', sender='合成机关')
        item.archive_relpath = 'originals/done/synthetic/external'
        item.source_type = 'external'
        item.external_issuer = '合成机关'
        item.document_number = '合成发〔2026〕7号'
        run = ClassificationRun(
            run_id='synthetic-external-index', run_kind='full', status='completed',
            input_signature='a' * 64, manifest_sha256='a' * 64, exclusion_policy_sha256='b' * 64,
            rule_version='synthetic', schema_version='synthetic', prompt_version='synthetic',
            model_name='synthetic', private_config_sha256='c' * 64, target_count=1, excluded_count=0,
        )
        session.add(run); session.flush()
        session.add(ClassificationDecision(
            classification_run_id=run.id, oa_item_key=item.oa_item_key, version=1, is_current=True,
            decision_input_sha256='d' * 64, decision_source='content_rule', classification_status='classified',
            content_integrity_status='no_attachment_confirmed', content_origin='external', flow_type='formal_document',
            initiator_type='internal', transfer_chain_json='[]', normalized_title=item.title,
            issuer='合成机关', canonical_issuer='合成机关', document_number='合成发〔2026〕7号',
            classification_confidence=1.0, classification_reason_json='{}', rule_version='synthetic',
            private_config_sha256='c' * 64,
        ))
        session.flush()
        destination = publish_item_index(session, settings, item.oa_item_key)
    content = destination.read_text(encoding='utf-8')
    assert 'document_number: "合成发〔2026〕7号"' in content
    assert 'canonical_issuer: "合成机关"' in content
    assert 'document_number_evidence:' in content


def test_classified_external_decision_mirrors_confirmed_document_number_to_delivery_item(tmp_path) -> None:
    from oa_knowledge.classification.service import ClassificationService

    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _item(session, title='关于事项的通知', sender='合成机关')
        run = ClassificationRun(
            run_id='synthetic-external-mirror', run_kind='full', status='completed',
            input_signature='a' * 64, manifest_sha256='a' * 64, exclusion_policy_sha256='b' * 64,
            rule_version='synthetic', schema_version='synthetic', prompt_version='synthetic',
            model_name='synthetic', private_config_sha256='c' * 64, target_count=1, excluded_count=0,
        )
        session.add(run); session.flush()
        decision = ClassificationDecision(
            classification_run_id=run.id, oa_item_key=item.oa_item_key, version=1, is_current=True,
            decision_input_sha256='d' * 64, decision_source='content_rule', classification_status='classified',
            content_integrity_status='ok', content_origin='external', flow_type='formal_document',
            initiator_type='internal', transfer_chain_json='[]', normalized_title=item.title,
            canonical_issuer='合成机关', document_number='合成发〔2026〕7号',
            classification_confidence=1.0, classification_reason_json='{}', rule_version='synthetic',
            private_config_sha256='c' * 64,
        )
        session.add(decision); session.flush()
        ClassificationService._mirror_compatibility(session, item.oa_item_key, decision)
        assert item.document_number == '合成发〔2026〕7号'
