import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import OAManifestItem, OAItem, ArchivedFile, TitleScreeningDecision, TitleScreeningOverride
from oa_knowledge.full_manifest import classify_manifest_rows
from oa_knowledge.title_screening import (
    ORDER, TitleRules, active_context, cached_body_evidence, import_spec, override,
    publish_confirmed_index, set_active, view, validate_folder,
)
from oa_knowledge.title_screening import edit_rules, editor_state


def specification():
    patterns = ["管理办法|批示|尽职调查报告", "项目出账申请|业务一部工作简报", "请假申请表", "出差申请|差旅费报销单",
                "离职申请表", "放款|借款|诉讼|设备部署验收", "同名账户资金划转", "CUSTOM_ADMIN", "证件外借申请表",
                "付款申请|采购申请|用印申请", "CUSTOM_EVENT", "DEFAULT"]
    return {"version": "synthetic-v1", "rules": [
        {"id": identifier, "name": identifier, "action": "保留下载" if identifier.startswith("K") else "建议排除" if identifier.startswith("E") else "先看正文", "pattern": pattern}
        for identifier, pattern in zip(ORDER, patterns, strict=True)],
        "custom_patterns": {"admin_purpose": "办公用品|商业保险|询证费用", "workflow": "付款申请|采购申请", "event": "职工比赛", "business_event_guard": "业务培训"},
        "organizations": [{"id": "synthetic", "type": "内部", "directory_label": "合成机构", "title_pattern": "合成机构", "document_code_pattern": "合字"}],
        "publication_series": [{"id": "synthetic", "directory_label": "合成刊物", "title_pattern": "合成刊物"}]}


def test_simplified_excludes_first_without_previous_protection():
    spec = specification()
    spec["rules"] = [rule for rule in spec["rules"] if rule["id"].startswith("E")]
    spec["rules"][4].update(pattern="CUSTOM_ADMIN_OR_PAYMENT", unconditional_pattern="^付款申请单")
    spec["rules"].append({"id":"DEFAULT", "name":"default", "action":"保留下载", "pattern":"DEFAULT"})
    spec["custom_patterns"].pop("event")
    spec["custom_patterns"].pop("business_event_guard")
    spec["protected_value_pattern"] = "项目放款|尽职调查报告|贷款"
    rules = TitleRules(spec)
    assert rules.evaluate(row("付款申请单 项目放款 批示"))["action"] == "skip"
    assert rules.evaluate(row("差旅费报销单 尽职调查报告"))["action"] == "skip"
    assert rules.evaluate(row("证件外借申请表 贷款"))["action"] == "skip"
    assert rules.evaluate(row("用印申请 未知用途"))["action"] == "download"
    assert rules.evaluate(row("采购申请 未知用途"))["action"] == "download"
    assert rules.evaluate(row("采购申请 办公用品"))["action"] == "skip"
    assert rules.evaluate(row("【转发】付款申请单 未知用途"))["action"] == "download"


def test_title_only_rule_precedes_value_protection_and_matches_anywhere():
    spec = specification()
    spec['title_only_pattern'] = '付款申请单'
    spec['protected_value_pattern'] = '项目放款|管理办法|风险评审'
    rules = TitleRules(spec)
    for title in ('付款申请单SYN001，待审批！事由：合成项目放款',
                  '【转发】付款申请单：风险评审', '附件付款申请单 管理办法'):
        decision = rules.evaluate(row(title))
        assert decision['action'] == 'skip'
        assert decision['rule_id'] == 'TITLE_ONLY'
    assert rules.evaluate(row('合成项目放款审批报告'))['action'] == 'download'


def test_configured_value_guard_preserves_policy_and_business_materials():
    spec=specification()
    spec['protected_value_pattern']=r'制度|管理办法|业务合同|风险评审|会议纪要|项目审批'
    rules=TitleRules(spec)
    assert rules.evaluate(row('关于修订差旅费报销制度的通知'))['action']=='download'
    assert rules.evaluate(row('差旅费报销单附项目审批材料'))['action']=='download'
    assert rules.evaluate(row('普通差旅费报销单'))['action']=='skip'


def row(title, identity="-9000000000000000001"):
    return OAManifestItem(oa_item_key=f"done:{identity}", workitem_id_text=identity, title=title,
                          list_page=1, list_ordinal=1, initiated_at=datetime(2026, 10, 9), processing_status="discovered")


@pytest.mark.parametrize("title,expected", [
    ("差旅费报销单 拜访放款项目", "skip"),
    ("差旅费报销单附尽职调查报告", "download"),
    ("采购申请 办公用品", "skip"),
    ("采购申请 办公用品及诉讼材料", "download"),
    ("付款申请 放款", "download"),
    ("付款申请 用途不明", "review"),
    ("证件外借申请表", "review"),
    ("付款申请 商业保险", "review"),
    ("付款申请 职工商业保险", "skip"),
    ("付款申请 银行询证费用", "review"),
    ("职工比赛", "review"),
    ("职工比赛与业务培训", "download"),
    ("关于合成刊物的批示", "download"),
])
def test_precedence_and_unknown_use_are_conservative(title, expected):
    assert TitleRules(specification()).evaluate(row(title))["action"] == expected


def test_issuer_is_not_inferred_from_sender_or_customer_and_publications_are_separate():
    rules = TitleRules(specification())
    customer = row("用印申请 合成机构客户合同")
    customer.sender = "合成机构"
    decision = rules.evaluate(customer)
    assert decision["classification_status"] == "needs_confirmation"
    assert decision["folder"].startswith("90_待确认/")
    publication = rules.evaluate(row("合成刊物"))
    assert publication["folder"].startswith("03_刊物与学习资料/")
    assert publication["attachment_count"] is None
    assert "-9000000000000000001" in publication["folder"]
    assert rules.evaluate(row("合成刊物 批示"))["folder"].startswith("90_待确认/")


@pytest.fixture
def setup(tmp_path, config_file):
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(row("付款申请 放款")); session.commit()
    spec = tmp_path / "spec.json"; spec.write_text(json.dumps(specification()), encoding="utf-8")
    plan_id = import_spec(settings, spec)
    yield settings, engine, plan_id
    engine.dispose()


def test_preview_has_no_download_side_effect_and_active_plan_overrides_old_blacklist(setup):
    settings, engine, plan_id = setup
    with Session(engine) as session:
        item = session.query(OAManifestItem).one()
        assert item.processing_status == "discovered"
        assert active_context(session) is None
    set_active(settings, plan_id)
    with Session(engine) as session:
        item = session.query(OAManifestItem).one()
        classify_manifest_rows(session, [item], ("付款申请",), settings.data_root)
        assert item.processing_status == "pending_download"
        assert item.matched_exclusion_keyword is None


def test_rule_editor_preview_is_read_only_and_save_creates_restorable_version(setup):
    settings, engine, plan_id = setup
    set_active(settings, plan_id)
    state = editor_state(settings)
    spec = state["spec"]
    spec["rules"][5]["enabled"] = False
    preview = edit_rules(settings, plan_id, spec)
    assert preview["counts"] == {"review": 1} and preview["changed"] == 1
    assert editor_state(settings)["plan_id"] == plan_id
    saved = edit_rules(settings, plan_id, spec, save=True)
    assert saved["plan_id"] != plan_id
    assert view(settings)["counts"] == {"review": 1}
    with pytest.raises(ValueError, match="已变更"):
        edit_rules(settings, plan_id, spec, save=True)
    set_active(settings, plan_id)
    assert view(settings)["counts"] == {"download": 1}


def test_rule_editor_keeps_manual_overrides_and_rejects_invalid_regex(setup):
    settings, engine, plan_id = setup
    set_active(settings, plan_id)
    identifier = view(settings)["items"][0]["id"]
    override(settings, identifier, action="download", reason="合成人工保留")
    spec = editor_state(settings)["spec"]
    spec["rules"][0]["pattern"] = ".*"
    assert edit_rules(settings, plan_id, spec)["counts"] == {"download": 1}
    spec["rules"][0]["pattern"] = "["
    import re
    with pytest.raises(re.error):
        edit_rules(settings, plan_id, spec, save=True)
    assert editor_state(settings)["plan_id"] == plan_id


def test_legacy_manual_exclusion_stays_until_explicit_override(setup):
    settings, engine, plan_id = setup
    with Session(engine) as session:
        item = session.query(OAManifestItem).one()
        item.processing_status = "skipped"; item.matched_exclusion_keyword = "manual:synthetic"
        session.commit()
    set_active(settings, plan_id)
    assert view(settings)["counts"] == {"skip": 1}
    set_active(settings, plan_id)
    assert view(settings)["counts"] == {"skip": 1}
    entry = view(settings)["items"][0]
    override(settings, entry["id"], action="inherit", reason="重新核对恢复规则")
    assert view(settings)["counts"] == {"download": 1}


def test_screening_api_requires_csrf_and_rejects_unconfirmed_folder(setup):
    from fastapi.testclient import TestClient
    from oa_knowledge.web.app import create_web_app
    settings, _engine, plan_id = setup
    client = TestClient(create_web_app(settings))
    token = client.get("/").cookies["oa_csrf"]
    assert client.post(f"/api/title-screening/{plan_id}/activate").status_code == 403
    headers = {"x-csrf-token": token}
    assert client.post(f"/api/title-screening/{plan_id}/activate", headers=headers).status_code == 200
    entry = client.get("/api/title-screening?action=download").json()["items"][0]
    response = client.post(f"/api/title-screening/items/{entry['id']}/override", headers=headers,
                           json={"action":"download", "reason":"合成核对意见", "folder":entry["folder"]})
    assert response.status_code == 400
    assert client.get("/api/title-screening/export.csv").status_code == 200


def test_manual_overrides_survive_refresh_and_history_is_immutable(setup):
    settings, engine, plan_id = setup
    set_active(settings, plan_id)
    identifier = view(settings)["items"][0]["id"]
    override(settings, identifier, action="skip", reason="人工确认仅索引")
    assert view(settings)["counts"] == {"skip": 1}
    override(settings, identifier, action="inherit", reason="恢复规则保留资料")
    assert view(settings)["counts"] == {"download": 1}
    with Session(engine) as session:
        events = session.query(TitleScreeningOverride).order_by(TitleScreeningOverride.id).all()
        assert len(events) == 2
        assert json.loads(events[1].previous_json)["action"] == "skip"
        item = session.query(OAManifestItem).one()
        item.title = "采购申请 办公用品"; session.commit()
    assert view(settings)["counts"] == {"skip": 1}
    with Session(engine) as session:
        assert session.query(TitleScreeningDecision).count() == 2


def test_confirmed_indexes_keep_originals_and_do_not_publish_candidates(setup):
    settings, engine, plan_id = setup
    set_active(settings, plan_id)
    assert publish_confirmed_index(settings)["confirmed_indexes"] == 0
    entry = view(settings)["items"][0]
    original = settings.data_root / "original.bin"
    original.parent.mkdir(parents=True, exist_ok=True); original.write_bytes(b"synthetic original")
    confirmed = entry["folder"].replace("90_待确认/来源待确认", "01_集团及成员机构/合成机构/业务项目")
    override(settings, entry["id"], action="download", reason="核对正文与机构落款", folder=confirmed)
    result = publish_confirmed_index(settings)
    assert result["confirmed_indexes"] == 1 and result["originals_moved"] == 0
    assert original.read_bytes() == b"synthetic original"
    assert len(list((settings.data_root / "runtime/organization-index").rglob("index.json"))) == 1
    override(settings, entry["id"], action="review", reason="再次核对下载用途")
    assert view(settings)["items"][0]["classification_status"] == "confirmed"


@pytest.mark.parametrize("folder", ["../escape", "C:/escape", "/absolute", "root/../escape", "root\\escape", "root/CON"])
def test_archive_paths_cannot_escape_or_use_windows_device_names(folder):
    with pytest.raises(ValueError):
        validate_folder(folder)


def test_local_body_reuse_requires_hash_and_keeps_unknown_attachment_count(setup):
    settings, engine, _plan_id = setup
    payload = b"<p>synthetic body</p><script>private code</script>"
    settings.data_root.mkdir(parents=True, exist_ok=True)
    source = settings.data_root / "body.html"; source.write_bytes(payload)
    with Session(engine) as session:
        manifest = session.query(OAManifestItem).one()
        archived = OAItem(oa_item_key=manifest.oa_item_key, source_channel="done", title="Synthetic")
        session.add(archived);session.flush()
        session.add(ArchivedFile(oa_item_id=archived.id, original_name="body.html", local_relpath="body.html", file_role="body_snapshot",
                                source_container_key="synthetic", attachment_key="body", download_status="verified", sha256=hashlib.sha256(payload).hexdigest()))
        session.commit()
        evidence = cached_body_evidence(session, manifest, settings)
        assert "private code" not in evidence["body"]
        assert evidence["attachment_count"] is None
        source.write_bytes(b"corrupted")
        assert cached_body_evidence(session, manifest, settings) is None


def test_body_review_checkpoints_and_never_automatically_excludes(setup, monkeypatch):
    import oa_knowledge.done_collection as collection
    import oa_knowledge.collector.review as review
    from oa_knowledge.collector.browser import LoginState
    from oa_knowledge.db.models import OperationJob, TitleReviewEvidence
    settings, engine, plan_id = setup
    settings.browser.base_url = "https://synthetic-oa.test"
    with Session(engine) as session:
        item = session.query(OAManifestItem).one(); item.title = "付款申请 用途不明"
        session.add(row("用印申请 用途不明", "synthetic-second")); session.commit()
    set_active(settings, plan_id)
    job = collection.start_collection(settings, "done_review")
    worker = SimpleNamespace(engine=engine, settings=settings, config_path=None, owner="worker-synthetic")

    class Browser:
        def __init__(self, settings):
            self.page = object(); self.context = object(); self.base_url = settings.browser.base_url
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login_with_saved_credentials(self, seconds): return LoginState.AUTHENTICATED

    class Adapter:
        def __init__(self, *args): pass
        def open_list(self): pass
        def navigate_to_page(self, page): pass
        def detail_link_for_item(self, identity): return "/synthetic-detail"

    calls = []
    def preview(*args):
        calls.append(True)
        if len(calls) == 1:
            collection.control_collection(settings, job["id"], "pause")
        return {"body": "synthetic 放款 evidence", "attachment_names": ["synthetic.pdf"], "attachment_count": None}
    monkeypatch.setattr(collection, "BrowserSession", Browser)
    monkeypatch.setattr(collection, "DoneAdapter", Adapter)
    monkeypatch.setattr(review, "preview_detail", preview)
    collection.run_review(worker, job["id"])
    with Session(engine) as session:
        persisted = session.get(OperationJob, job["id"])
        assert persisted.status == "paused" and persisted.progress_current == 1
        assert session.query(TitleReviewEvidence).count() == 1
        assert session.query(ArchivedFile).count() == 0
    collection.control_collection(settings, job["id"], "resume")
    collection.run_review(worker, job["id"])
    with Session(engine) as session:
        assert session.get(OperationJob, job["id"]).status == "completed"
        assert session.query(TitleReviewEvidence).count() == 2
        assert session.query(ArchivedFile).count() == 0
    assert view(settings)["counts"] == {"review": 2}


def test_preview_detail_reads_dom_without_clicking_or_downloading(monkeypatch):
    from oa_knowledge.collector.review import preview_detail
    from oa_knowledge.collector.detail import CollaborationDetailAdapter
    class Page:
        url = "https://synthetic-oa.test/detail"
        frames = []
        closed = False
        def goto(self, url, **kwargs): self.url = url
        def locator(self, selector): return SimpleNamespace(count=lambda: 0)
        def close(self): self.closed = True
        def expect_download(self, *args, **kwargs): raise AssertionError("must not download")
    page = Page()
    monkeypatch.setattr(CollaborationDetailAdapter, "_wait_dynamic_content", lambda *args, **kwargs: None)
    monkeypatch.setattr(CollaborationDetailAdapter, "_snapshots", lambda *args: (SimpleNamespace(html="<p>synthetic body</p>"),))
    evidence = preview_detail(SimpleNamespace(new_page=lambda: page), page.url, "https://synthetic-oa.test")
    assert evidence["body"].strip() == "synthetic body"
    assert evidence["attachment_count"] is None
    assert evidence["review_complete"] is False
    assert page.closed
