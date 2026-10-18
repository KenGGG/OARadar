"""Local title suggestions, independent of original-file and publication state.

Imported specifications are data, never commands. A title cannot confirm an
issuer or authorize moving files. All manual changes are append-only events.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path, PurePosixPath

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import (
    OAManifestItem, OperationJob, TitleScreeningDecision, TitleScreeningOverride,
    TitleScreeningPlan, TitleReviewEvidence, OAItem, ArchivedFile,
)

ACTIONS = {"保留下载": "download", "建议排除": "skip", "先看正文": "review"}
LABELS = {value: key for key, value in ACTIONS.items()}
ORDER = ["K01", "K02", "E01", "E02", "E03", "K03", "E04", "E05", "E06", "R01", "R02", "K99"]
SIMPLE_ORDER = ["E01", "E02", "E03", "E04", "E05", "E06", "DEFAULT"]


def simplified(spec: dict) -> bool:
    return [rule.get("id") for rule in spec.get("rules", [])] == SIMPLE_ORDER


def title_hash(title: str) -> str:
    return hashlib.sha256(title.encode("utf-8")).hexdigest()


def normalize(title: str) -> str:
    value = unicodedata.normalize("NFKC", title)
    value = re.sub(r"^(?:\s*(?:【(?:转发|文件传阅)】|\[(?:转发|文件传阅)\]|转发[:：]))+", "", value)
    return " ".join(value.split())


def validate_spec(spec: dict) -> None:
    if not isinstance(spec, dict) or not isinstance(spec.get("version"), str):
        raise ValueError("invalid title specification")
    rules = spec.get("rules", [])
    if spec.get("protected_value_pattern"):
        re.compile(spec["protected_value_pattern"])
    if spec.get("title_only_pattern"):
        re.compile(spec["title_only_pattern"])
    if [rule.get("id") for rule in rules] not in (ORDER, SIMPLE_ORDER):
        raise ValueError("规则缺失或保护顺序改变")
    for rule in rules:
        if "enabled" in rule and not isinstance(rule["enabled"], bool):
            raise ValueError("规则开关必须是布尔值")
        if rule.get("pattern") == "DEFAULT" and rule.get("enabled") is False:
            raise ValueError("默认保留规则不能关闭")
        if rule.get("action") not in ACTIONS:
            raise ValueError("invalid rule action")
        if not isinstance(rule.get("pattern"), str) or not rule["pattern"].strip():
            raise ValueError("匹配表达式不能留空；暂不使用时请关闭规则")
        expected = "download" if rule["id"].startswith("K") or rule["id"] == "DEFAULT" else "skip" if rule["id"].startswith("E") else "review"
        if ACTIONS[rule["action"]] != expected:
            raise ValueError("保护规则不能改为排除")
        if rule.get("pattern") == "CUSTOM_ADMIN_OR_PAYMENT":
            re.compile(rule["unconditional_pattern"], re.I)
        elif rule.get("pattern") not in {"CUSTOM_ADMIN", "CUSTOM_EVENT", "DEFAULT"}:
            re.compile(rule["pattern"], re.I)
    for key in (("admin_purpose", "workflow") if simplified(spec) else ("admin_purpose", "workflow", "event", "business_event_guard")):
        re.compile(spec["custom_patterns"][key], re.I)
    for group, fields in (("organizations", ("title_pattern", "document_code_pattern")),
                          ("publication_series", ("title_pattern",))):
        for record in spec.get(group, []):
            safe_segment(record["directory_label"])
            for field in fields:
                if record.get(field):
                    re.compile(record[field], re.I)


def safe_segment(value: str, limit: int = 52) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:limit].rstrip(" .")
    if not value or value in {".", ".."}:
        return "未命名"
    if re.match(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "_" + value
    return value


def validate_folder(value: str) -> str:
    if "\\" in value or ":" in value:
        raise ValueError("归档路径必须是相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"..", "."} for part in value.split("/")) or not value:
        raise ValueError("归档路径不能越出资料库")
    if any(safe_segment(part, 240) != part for part in path.parts) or len(value) > 220:
        raise ValueError("归档路径包含非法字符或过长")
    return value


class TitleRules:
    def __init__(self, spec: dict, *, conservative: bool = True):
        validate_spec(spec)
        self.spec = spec
        self.conservative = conservative and not simplified(spec)
        self.rules = [(rule, re.compile(rule["pattern"], re.I) if rule["pattern"] not in
                       {"CUSTOM_ADMIN", "CUSTOM_ADMIN_OR_PAYMENT", "CUSTOM_EVENT", "DEFAULT"} else None) for rule in spec["rules"]]
        self.custom = {key: re.compile(value, re.I) for key, value in spec["custom_patterns"].items()}

    def evaluate(self, row: OAManifestItem) -> dict:
        title = " ".join(unicodedata.normalize("NFKC", row.title).split()) if simplified(self.spec) else normalize(row.title)
        title_only = self.spec.get("title_only_pattern")
        if title_only and re.search(title_only, title):
            result = {"action": "skip", "rule_id": "TITLE_ONLY",
                      "reason": "标题命中仅保留记录规则；不下载附件、不转换、不分类",
                      "evidence_source": "title_rule", "version": self.spec["version"]}
            result.update(self.classify(row, title, result["rule_id"]))
            return result
        protected = self.spec.get("protected_value_pattern")
        if protected and not simplified(self.spec) and re.search(protected, title):
            result = {"action": "download", "rule_id": "VALUE_PROTECTION",
                      "reason": "制度、业务项目、合同、风险、会议或监管资料优先保留",
                      "evidence_source": "title_rule", "version": self.spec["version"]}
            result.update(self.classify(row, title, result["rule_id"]))
            return result
        result = None
        for rule, pattern in self.rules:
            if rule.get("enabled") is False:
                continue
            marker = rule["pattern"]
            if marker == "CUSTOM_ADMIN_OR_PAYMENT":
                matched = bool((rule.get("unconditional_pattern") and re.search(rule["unconditional_pattern"], title, re.I)) or
                               (self.spec["custom_patterns"]["workflow"].strip() and self.spec["custom_patterns"]["admin_purpose"].strip()
                                and self.custom["workflow"].search(title) and self.custom["admin_purpose"].search(title)))
            elif marker == "CUSTOM_ADMIN":
                matched = bool(self.spec["custom_patterns"]["workflow"].strip() and self.spec["custom_patterns"]["admin_purpose"].strip()
                               and self.custom["workflow"].search(title) and self.custom["admin_purpose"].search(title))
            elif marker == "CUSTOM_EVENT":
                matched = bool(self.custom["event"].search(title) and not self.custom["business_event_guard"].search(title))
            else:
                matched = marker == "DEFAULT" or bool(pattern and pattern.search(title))
            if not matched:
                continue
            action = ACTIONS[rule["action"]]
            reason = rule.get("note", rule["name"])
            if self.conservative and rule["id"] == "E06":
                action, reason = "review", "证件外借用途尚未核实；默认保留下载"
            if self.conservative and rule["id"] == "E05" and (
                re.search(r"询证|审计", title) or
                (re.search(r"商业保险|年金", title) and not re.search(r"职工|员工|福利|工会", title))
            ):
                action, reason = "review", "保险、年金或询证费用用途存在歧义；默认保留下载"
            result = {"action": action, "rule_id": rule["id"], "reason": reason,
                      "evidence_source": "title_rule", "version": self.spec["version"] + ("+safe-v1" if self.conservative else "")}
            break
        assert result is not None
        result.update(self.classify(row, title, result["rule_id"]))
        return result

    def classify(self, row: OAManifestItem, title: str, rule: str) -> dict:
        publications = [record["directory_label"] for record in self.spec.get("publication_series", [])
                        if re.search(record["title_pattern"], title, re.I)]
        organizations = []
        basis = []
        for record in self.spec.get("organizations", []):
            code = record.get("document_code_pattern")
            # Names anywhere are only mentioned parties. Even code matches are
            # title evidence, never confirmed authorship.
            if code and re.search(code, title, re.I):
                organizations.append(record); basis.append("标题文号候选")
            elif re.search(record["title_pattern"], title, re.I):
                organizations.append(record); basis.append("标题提及机构，发文单位待核实")
        unique = {record["directory_label"]: record for record in organizations}
        organizations = list(unique.values())
        topic = "其他事项"
        for name, pattern in (
            ("公司治理", r"制度|管理办法|董事会|决议|会议纪要|批示|批复"),
            ("风险管理", r"诉讼|和解|保全|风险|抵押|质押|解押|整改"),
            ("业务项目", r"项目|尽调|租后|租赁物|验收|回购|业务合同"),
            ("财务资金", r"放款|资金|借款|贷款|财务|租金|保证金|付款|报销"),
            ("经营管理", r"业务[一二]部|内部周报|经营|工作简报"),
            ("人力行政", r"职工|员工|请假|休假|人事|行政|出差"),
            ("信息化", r"信息化|系统|软件|电脑|网络"),
        ):
            if re.search(pattern, title):
                topic = name; break
        special = rule in {"K01", "K02", "K03"} or bool(re.search(r"批示|批复|决议|会议纪要|业务[一二]部.*工作简报|内部周报|租后检查|尽调报告", title))
        if publications and not special:
            root = "03_刊物与学习资料/" + safe_segment(publications[0])
        elif len(organizations) == 1 and basis == ["标题文号候选"]:
            record = organizations[0]
            root = ("01_集团及成员机构/" if record["type"] == "内部" else "02_外部机构/") + safe_segment(record["directory_label"]) + "/" + topic
        else:
            root = "90_待确认/" + ("外部联合或合办来文" if len(organizations) > 1 else
                "外部来文_机构待确认" if "文件传阅" in row.title else "来源待确认")
        initiated = row.initiated_at
        date = initiated.strftime("%Y-%m-%d") if initiated else "日期待确认"
        period = initiated.strftime("%Y年/%m月") if initiated else "年份待确认/月份待确认"
        identifier = row.workitem_id_text or row.oa_item_key
        # Numeric OA IDs are preserved verbatim, including negative signs.
        identity = safe_segment(identifier, 80)
        if identity != identifier:
            identity += "_" + title_hash(identifier)[:12]
        folder = f"{root}/{period}/{date}_{safe_segment(row.title, 38)}__OA_{identity}"
        return {"organizations": list(unique), "organization_basis": basis,
                "confidence": "candidate", "classification_status": "needs_confirmation",
                "topic": topic, "publications": publications, "folder": folder,
                "sensitive": bool(re.search(r"敏感|免予公开|请勿外传|涉密|秘密|机密", title)),
                "attachment_count": None}


class ScreeningContext:
    def __init__(self, session: Session, plan: TitleScreeningPlan):
        self.session, self.plan = session, plan
        self.rules = TitleRules(json.loads(plan.spec_json))
        self.decisions = {(record.oa_item_key, record.title_sha256): record for record in session.scalars(
            select(TitleScreeningDecision).where(TitleScreeningDecision.plan_id == plan.id))}
        self.overrides = {}
        for event in session.scalars(select(TitleScreeningOverride).order_by(TitleScreeningOverride.id)):
            self.overrides[event.oa_item_key] = event
        from oa_knowledge.archive_classification import active_context as archive_context
        self.archiving = archive_context(session)

    def decision(self, row: OAManifestItem) -> dict:
        key = (row.oa_item_key, title_hash(row.title))
        record = self.decisions.get(key)
        if record is None:
            result = self.rules.evaluate(row)
            result["source_snapshot"] = {"item_id": row.workitem_id_text, "title": row.title,
                "sender": row.sender, "initiated_at": row.initiated_at.isoformat() if row.initiated_at else None,
                "completed_at": row.completed_at.isoformat() if row.completed_at else row.completed_at_text,
                "original_status": row.processing_status}
            record = TitleScreeningDecision(plan_id=self.plan.id, oa_item_key=row.oa_item_key,
                                           title_sha256=key[1], result_json=json.dumps(result, ensure_ascii=False))
            self.session.add(record); self.decisions[key] = record
        else:
            result = json.loads(record.result_json)
        # Date/list metadata may improve without changing the title.
        fresh = self.rules.classify(row, normalize(row.title), result["rule_id"])
        result.update(fresh)
        event = self.overrides.get(row.oa_item_key)
        if event:
            result = {**result, "action": event.action if event.action != "inherit" else result["action"], "manual_reason": event.reason,
                      "manual_override": True, "override_id": event.id}
        elif (row.matched_exclusion_keyword or "").startswith("manual:"):
            result = {**result, "action": "skip", "manual_override": False, "legacy_manual": True,
                      "manual_reason": "保留已有人工排除：" + row.matched_exclusion_keyword}
        # Explicit title-only exclusions also take precedence over older
        # manual download decisions and cached decisions from this plan.
        screening = self.rules.evaluate(row)
        if screening["action"] == "skip":
            result.update(screening)
        if self.archiving:
            archive = self.archiving.decision(row, result["action"])
            result.update(archive)
            result.update(folder=archive["virtual_archive_path"] or "",
                          organizations=[archive["archive_organization"]] if archive["archive_organization"] else archive.get("source_organizations", []),
                          organization_basis=[archive["classification_reason"]],
                          publications=[archive["archive_series"]] if archive["archive_series"] else [],
                          topic=None, confidence="rule" if not archive["needs_review"] else "candidate")
        if event and event.folder and (not self.archiving or result["action"] == "download"):
            result.update(folder=event.folder, classification_status="confirmed", confidence="manual",
                          classification_reason=event.reason)
            if self.archiving:
                from oa_knowledge.archive_classification import CATEGORIES
                result.update(virtual_archive_path=event.folder, needs_review=False,
                              classification_source="manual_confirmation",
                              source_organization_verified=True,
                              archive_category_id=next((category for category, root in CATEGORIES.items() if event.folder.split("/", 1)[0] == root), "MANUAL"))
                parts = event.folder.split("/")
                category = result["archive_category_id"]
                result.update(archive_year=parts[-2], matched_mapping_id=None,
                              archive_organization=parts[1] if category in {"GROUP", "EXTERNAL"} else None,
                              archive_series=parts[1] if category == "PUBLICATION" else None,
                              organization_basis=[event.reason])
                if category in {"GROUP", "EXTERNAL"}:
                    result["organizations"] = [parts[1]]
                if category == "PUBLICATION":
                    result["publications"] = [parts[1]]
        result["plan_id"] = self.plan.id
        result["original_status"] = row.processing_status
        result["original_title"] = row.title
        result["item_id"] = row.workitem_id_text
        result["initiated_at"] = row.initiated_at.isoformat() if row.initiated_at else None
        result["completed_at"] = row.completed_at.isoformat() if row.completed_at else row.completed_at_text
        result["label"] = LABELS[result["action"]]
        result["download_eligible"] = result["action"] != "skip"
        return result


def active_context(session: Session) -> ScreeningContext | None:
    plan = session.scalar(select(TitleScreeningPlan).where(TitleScreeningPlan.active.is_(True)))
    return ScreeningContext(session, plan) if plan else None


def assert_attachment_eligible(session: Session, item) -> None:
    """Screen before reading an attachment, regardless of classification."""
    if item is None:
        return
    manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == item.oa_item_key))
    if manifest is None:
        return
    plan = session.scalar(select(TitleScreeningPlan).where(TitleScreeningPlan.active.is_(True)))
    excluded = manifest.processing_status == "skipped" or bool(manifest.matched_exclusion_keyword)
    if excluded or (plan and TitleRules(json.loads(plan.spec_json)).evaluate(manifest)["action"] == "skip"):
        raise ValueError("ATTACHMENT_EXCLUDED_BY_SCREENING")


def editor_state(settings) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            plan = session.scalar(select(TitleScreeningPlan).where(TitleScreeningPlan.active.is_(True)))
            history = [{"id": row.id, "version": row.version, "active": row.active} for row in session.scalars(
                select(TitleScreeningPlan).order_by(TitleScreeningPlan.id.desc()).limit(20))]
            return {"plan_id": plan.id if plan else None, "spec": json.loads(plan.spec_json) if plan else None, "history": history}
    finally:
        engine.dispose()


def edit_rules(settings, base_plan_id: int, spec: dict, *, save: bool = False) -> dict:
    from datetime import datetime, timezone
    from uuid import uuid4
    validate_spec(spec)
    rules = TitleRules(spec)
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            if save:
                session.execute(text("BEGIN IMMEDIATE")); assert_idle(session)
            context = active_context(session)
            if context is None or context.plan.id != base_plan_id:
                raise ValueError("当前方案已变更，请重新读取后编辑")
            if simplified(spec) != simplified(context.rules.spec):
                raise ValueError("微调不能改变方案模式")
            rows = list(session.scalars(select(OAManifestItem)))
            counts, changes = Counter(), []
            for row in rows:
                previous = context.decision(row)
                result = rules.evaluate(row)
                event = context.overrides.get(row.oa_item_key)
                if (event and event.action != "inherit") or previous.get("legacy_manual"):
                    result["action"] = previous["action"]
                counts[result["action"]] += 1
                if result["action"] != previous["action"]:
                    changes.append({"item_id": row.workitem_id_text, "title": row.title,
                                    "before": LABELS[previous["action"]], "after": LABELS[result["action"]]})
            response = {"counts": dict(counts), "changed": len(changes), "examples": changes[:20]}
            if save:
                spec = {**spec, "version": f"{context.rules.spec['version'].split('-edit-')[0]}-edit-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}"}
                raw = json.dumps(spec, ensure_ascii=False, sort_keys=True)
                plan = TitleScreeningPlan(version=spec["version"] + ("" if simplified(spec) else "+safe-v1"), spec_json=raw,
                    source_sha256=hashlib.sha256(raw.encode()).hexdigest(), active=True)
                context.plan.active = False
                session.add(plan); session.flush()
                from oa_knowledge.full_manifest import classify_manifest_rows, effective_exclusion_keywords
                classify_manifest_rows(session, rows, effective_exclusion_keywords(session), settings.data_root)
                session.commit()
                response.update(plan_id=plan.id, version=plan.version)
            return response
    finally:
        engine.dispose()


def assert_idle(session: Session) -> None:
    if session.scalar(select(OperationJob.id).where(OperationJob.status.in_(("queued", "running"))).limit(1)):
        raise ValueError("请先暂停运行中的采集任务，再修改筛选方案")


def import_spec(settings, spec_path: Path) -> int:
    raw = spec_path.read_bytes()
    spec = json.loads(raw.decode("utf-8-sig"))
    validate_spec(spec)
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            session.execute(text("BEGIN IMMEDIATE")); assert_idle(session)
            digest = hashlib.sha256(raw).hexdigest()
            plan = session.scalar(select(TitleScreeningPlan).where(TitleScreeningPlan.source_sha256 == digest))
            if plan is None:
                plan = TitleScreeningPlan(version=spec["version"] + ("" if simplified(spec) else "+safe-v1"), spec_json=json.dumps(spec, ensure_ascii=False), source_sha256=digest)
                session.add(plan); session.flush()
            context = ScreeningContext(session, plan)
            for row in session.scalars(select(OAManifestItem)):
                context.decision(row)
            session.commit()
            return plan.id
    finally:
        engine.dispose()


def set_active(settings, plan_id: int, enabled: bool = True) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            session.execute(text("BEGIN IMMEDIATE")); assert_idle(session)
            plan = session.get(TitleScreeningPlan, plan_id)
            if plan is None:
                raise ValueError("筛选方案不存在")
            for existing in session.scalars(select(TitleScreeningPlan)):
                existing.active = enabled and existing.id == plan_id
            session.flush()
            from oa_knowledge.full_manifest import classify_manifest_rows, effective_exclusion_keywords
            rows = list(session.scalars(select(OAManifestItem)))
            classify_manifest_rows(session, rows, effective_exclusion_keywords(session), settings.data_root)
            session.commit()
            return {"plan_id": plan_id, "active": enabled}
    finally:
        engine.dispose()


def view(settings, *, action: str | None = None, query: str = "", page: int = 1, plan_id: int | None = None, category: str | None = None) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            plan = session.get(TitleScreeningPlan, plan_id) if plan_id else session.scalar(
                select(TitleScreeningPlan).order_by(TitleScreeningPlan.active.desc(), TitleScreeningPlan.id.desc()).limit(1))
            if plan is None:
                return {"plan": None, "items": [], "total": 0, "counts": {}}
            context = ScreeningContext(session, plan)
            items, counts, category_counts = [], Counter(), Counter()
            for row in session.scalars(select(OAManifestItem).order_by(OAManifestItem.list_ordinal, OAManifestItem.id)):
                result = context.decision(row); counts[result["action"]] += 1
                if result.get("archive_category_id"):
                    category_counts[result["archive_category_id"]] += 1
                if action and result["action"] != action:
                    continue
                if category and result.get("archive_category_id") != category:
                    continue
                if query and query.casefold() not in (row.title + " " + " ".join(result["organizations"])).casefold():
                    continue
                items.append({"id": row.id, "item_id": row.workitem_id_text, "title": row.title,
                              "original_status": row.processing_status, "archive_relpath": row.archive_relpath, **result})
            total = len(items)
            session.commit()
            return {"plan": {"id": plan.id, "version": plan.version, "active": plan.active,
                             "mode": "exclude_first" if simplified(json.loads(plan.spec_json)) else "protected"},
                    "counts": dict(counts), "category_counts": dict(category_counts),
                    "classification_version": context.archiving.plan.version if context.archiving else None,
                    "total": total, "items": items[(page - 1) * 50:page * 50]}
    finally:
        engine.dispose()


def override(settings, manifest_id: int, *, action: str, reason: str, folder: str | None = None) -> dict:
    if action not in {"skip", "download", "review", "inherit"} or len(reason.strip()) < 3:
        raise ValueError("请选择处理建议并填写至少三个字的依据")
    if folder:
        folder = validate_folder(folder)
        if folder.split("/", 1)[0] not in {"01_集团及成员机构", "02_外部机构", "03_刊物与学习资料", "01_本公司资料", "02_集团及成员企业", "03_外部机构来文", "04_刊物与学习资料"}:
            raise ValueError("正式目录须选择已核实机构或刊物；待确认路径不能确认为正式归档")
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            session.execute(text("BEGIN IMMEDIATE")); assert_idle(session)
            row = session.get(OAManifestItem, manifest_id)
            context = active_context(session)
            if row is None or context is None:
                raise ValueError("事项不存在或方案尚未启用")
            previous = context.decision(row)
            if folder:
                identifier = row.workitem_id_text or row.oa_item_key
                if context.archiving:
                    expected = safe_segment(identifier, 90)
                    if expected != identifier:
                        expected += "_" + title_hash(identifier)[:12]
                    if folder.split("/")[-1] != expected:
                        raise ValueError("五类归档事项目录必须仅使用事项ID")
                    root = folder.split("/")[0]
                    required = 3 if root == "01_本公司资料" else 4
                    if len(folder.split("/")) != required:
                        raise ValueError("五类归档仅允许机构或刊物、年份、事项ID，不设置月、项目或部门子目录")
                    if not re.fullmatch(r"(?:19|20)\d{2}|年份待确认", folder.split("/")[-2]):
                        raise ValueError("归档年份须为四位年份或年份待确认")
                elif not folder.split("/")[-1].endswith(f"__OA_{safe_segment(identifier, 80)}"):
                    raise ValueError("事项目录须保留完整事项ID")
            if folder is None and action != "inherit" and previous["classification_status"] == "confirmed":
                folder = previous["folder"]
            session.add(TitleScreeningOverride(oa_item_key=row.oa_item_key, action=action,
                        reason=reason.strip(), folder=folder, previous_json=json.dumps(previous, ensure_ascii=False)))
            session.flush()
            from oa_knowledge.full_manifest import classify_manifest_rows
            classify_manifest_rows(session, [row], (), settings.data_root)
            session.commit()
            return {"id": manifest_id, "action": action, "classification_confirmed": bool(folder)}
    finally:
        engine.dispose()


def body_suggestion(session: Session, evidence: dict) -> dict:
    context = active_context(session)
    if context is None or not evidence.get("body"):
        return {"action": "review", "reason": "正文尚不可读，默认继续下载"}
    body = normalize(evidence["body"])
    for rule, pattern in context.rules.rules:
        if rule.get("enabled") is not False and rule["id"] in {"K01", "K02", "K03"} and pattern.search(body):
            return {"action": "download", "rule_id": rule["id"], "reason": "正文包含正式文件或业务保护线索，建议保留；待人工确认"}
    return {"action": "review", "reason": "正文用途仍需人工核对，默认继续下载"}


def cached_body_evidence(session: Session, row: OAManifestItem, settings) -> dict | None:
    from oa_knowledge.collector.review import html_text
    item = session.scalar(select(OAItem).where(OAItem.oa_item_key == row.oa_item_key))
    if item is None:
        return None
    files = list(session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id)))
    bodies = []
    for file in files:
        if file.file_role != "body_snapshot" or file.download_status != "verified" or not file.local_relpath:
            continue
        path = (settings.data_root / file.local_relpath).resolve()
        if not path.is_relative_to(settings.data_root.resolve()) or not path.is_file():
            continue
        raw = path.read_bytes()
        if not file.sha256 or hashlib.sha256(raw).hexdigest() != file.sha256:
            continue
        bodies.append(html_text(raw.decode("utf-8", errors="replace")))
    if not bodies:
        return None
    body = "\n".join(bodies)
    return {"body": body[:200000], "body_truncated": len(body) > 200000,
            "attachment_names": [file.original_name for file in files if file.file_role in {"direct_attachment", "official_attachment", "official_body"}],
            "attachment_count": None, "inventory_scope": "archived_files_only", "source": "verified_local_body",
            "review_complete": False, "content_available": True, "warning": "复用已校验正文；已有文件记录不代表完整OA附件目录。"}


def review_detail(settings, manifest_id: int) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            row = session.get(OAManifestItem, manifest_id)
            if row is None:
                raise ValueError("事项不存在")
            evidence = session.scalar(select(TitleReviewEvidence).where(
                TitleReviewEvidence.oa_item_key == row.oa_item_key,
                TitleReviewEvidence.title_sha256 == title_hash(row.title)).order_by(TitleReviewEvidence.id.desc()).limit(1))
            payload = json.loads(evidence.payload_json) if evidence else cached_body_evidence(session, row, settings)
            events = [{"id": event.id, "action": event.action, "reason": event.reason, "folder": event.folder,
                       "at": event.created_at.isoformat(), "previous": json.loads(event.previous_json)} for event in session.scalars(
                           select(TitleScreeningOverride).where(TitleScreeningOverride.oa_item_key == row.oa_item_key).order_by(TitleScreeningOverride.id))]
            return {"id": manifest_id, "evidence": payload, "history": events}
    finally:
        engine.dispose()


def publish_confirmed_index(settings) -> dict:
    """Build local reference indexes, preserving canonical originals in place."""
    from oa_knowledge.archive.writer import atomic_write_bytes
    from uuid import uuid4
    engine = create_db_engine(settings.database_path)
    base = settings.data_root / "runtime" / "organization-index"
    run = "run-" + uuid4().hex[:12]
    root = base / run
    if not root.resolve().is_relative_to(settings.data_root.resolve()):
        raise ValueError("归档索引目录越出资料库")
    count = 0
    try:
        with Session(engine) as session:
            session.execute(text("BEGIN IMMEDIATE")); assert_idle(session)
            context = active_context(session)
            if context is None:
                raise ValueError("请先启用筛选方案")
            for row in session.scalars(select(OAManifestItem)):
                result = context.decision(row)
                if result["classification_status"] != "confirmed" or result["action"] == "skip":
                    continue
                folder = validate_folder(result["folder"])
                destination = (root / folder / "index.json").resolve()
                if not destination.is_relative_to(root.resolve()):
                    raise ValueError("归档路径越界")
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Only references are published. The full original title and
                # ID remain in metadata even when directory names are shortened.
                content = json.dumps({"oa_item_key": row.oa_item_key,
                    "item_id": row.workitem_id_text, "title": row.title, "initiated_at": row.initiated_at.isoformat() if row.initiated_at else None,
                    "canonical_archive_relpath": row.archive_relpath, "decision": result}, ensure_ascii=False, indent=2)
                atomic_write_bytes(content.encode("utf-8"), root, destination.relative_to(root.resolve()))
                count += 1
            session.commit()
        atomic_write_bytes(json.dumps({"run": run, "confirmed_indexes": count}).encode("utf-8"), base, "current.json")
        return {"confirmed_indexes": count, "relpath": f"runtime/organization-index/{run}", "originals_moved": 0}
    finally:
        engine.dispose()


def export_csv(settings) -> bytes:
    engine = create_db_engine(settings.database_path)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(("事项ID", "原标题", "发起人", "发起时间", "处理时间", "下载建议", "规则版本", "规则编号", "依据", "判断来源", "机构候选", "机构判断依据",
                     "分类状态", "候选归档路径", "人工意见", "原件状态", "现有原件目录", "附件数量", "主题", "刊物", "敏感标记", "归档类别", "归档机构", "归档刊物系列", "归档年份", "分类规则版本", "分类规则编号", "映射编号", "分类依据", "分类来源", "需确认", "主题或多来源归集", "来源机构", "来源核实", "来源证据等级", "来源关系", "来源标题段", "人工候选类别"))
    try:
        with Session(engine) as session:
            plan = session.scalar(select(TitleScreeningPlan).order_by(TitleScreeningPlan.active.desc(), TitleScreeningPlan.id.desc()).limit(1))
            if plan:
                context = ScreeningContext(session, plan)
                for row in session.scalars(select(OAManifestItem).order_by(OAManifestItem.list_ordinal, OAManifestItem.id)):
                    result = context.decision(row)
                    writer.writerow((row.workitem_id_text or row.oa_item_key, row.title, row.sender or "",
                        row.initiated_at.isoformat() if row.initiated_at else "", row.completed_at.isoformat() if row.completed_at else row.completed_at_text or "",
                        result["label"], result["version"], result["rule_id"],
                        result["reason"], result["evidence_source"], "；".join(result["organizations"]), "；".join(result["organization_basis"]),
                        result["classification_status"], result["folder"], result.get("manual_reason", ""), row.processing_status, row.archive_relpath or "", "未知",
                        result["topic"], "；".join(result["publications"]), "是" if result["sensitive"] else "否",
                        *[result.get(key, "") for key in ("archive_category_id", "archive_organization", "archive_series", "archive_year", "classification_version", "matched_rule_id", "matched_mapping_id", "classification_reason", "classification_source", "needs_review", "archive_bucket")],
                        "；".join(result.get("source_organizations", [])), *[result.get(key, "") for key in ("source_organization_verified", "source_evidence_level", "source_relationship", "source_heading", "suggested_category_id")]))
                session.commit()
        return stream.getvalue().encode("utf-8-sig")
    finally:
        engine.dispose()
