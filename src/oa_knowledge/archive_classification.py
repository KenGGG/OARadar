"""Independent source classification; never controls retrieval or moves files."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchiveClassificationPlan, ArchiveClassificationResult, TitleReviewEvidence
from oa_knowledge.title_screening import safe_segment, title_hash

CATEGORIES = {"SELF": "01_本公司资料", "GROUP": "02_集团及成员企业", "EXTERNAL": "03_外部机构来文",
              "PUBLICATION": "04_刊物与学习资料", "UNKNOWN": "05_待确认"}
RULES = ["C01", "C02", "C03", "C04", "C99"]
CLASSIFIER_REVISION = "five-category-source-11"


def validate_spec(spec: dict) -> None:
    if not isinstance(spec, dict) or not isinstance(spec.get("version"), str):
        raise ValueError("invalid archive classification specification")
    delivery = spec.get('delivery', {})
    if not isinstance(delivery,dict) or ('attachments_only' in delivery and not isinstance(delivery['attachments_only'],bool)):
        raise ValueError('attachments_only must be a boolean')
    priority=spec.get('source_priority',{})
    if not isinstance(priority,dict) or ('primary_group_document_code'in priority and type(priority['primary_group_document_code'])is not bool):
        raise ValueError('primary_group_document_code must be a boolean')
    if 'primary_external_document_code'in priority and type(priority['primary_external_document_code'])is not bool:
        raise ValueError('primary_external_document_code must be a boolean')
    if priority.get('combined_publication')not in {None,'first_title_series'}:
        raise ValueError('invalid combined publication policy')
    if [rule.get("id") for rule in spec["classification"]["rules"]] != RULES:
        raise ValueError("五类归档规则顺序不完整")
    folders = {row["id"]: row["folder"] for row in spec["folder_structure"]["categories"]}
    legacy = {**CATEGORIES, "UNKNOWN": "99_待确认"}
    if folders not in (CATEGORIES, legacy):
        raise ValueError("归档类别须为规定的五类目录")
    if spec["folder_structure"].get("create_month_subfolder") is not False:
        raise ValueError("五类简化归档不支持月份子目录")
    for rule, expected in zip(spec["classification"]["rules"], ("PUBLICATION", "SELF", "GROUP", "EXTERNAL", "UNKNOWN"), strict=True):
        if rule["category"] != expected:
            raise ValueError("归档规则类别与顺序不一致")
    for pattern in spec["classification"]["self_workflow_title_patterns"]:
        re.compile(pattern)
    records = [spec["mappings"]["self_organization"], *spec["mappings"]["group_organizations"], *spec["mappings"]["external_organizations"]]
    for record in records:
        if not record.get("name") or not record.get("issuer_name_pattern"):
            raise ValueError("机构名称与匹配表达式不可为空")
        re.compile(record["issuer_name_pattern"])
        if record.get("document_code_pattern"):
            re.compile(record["document_code_pattern"])
    for record in spec["mappings"]["publication_series"]:
        if not record.get("name") or not record.get("title_pattern"):
            raise ValueError("刊物系列名称与表达式不可为空")
        re.compile(record["title_pattern"])
    from oa_knowledge.archive_patterns import validate_second_pass
    validate_second_pass(spec.get("second_pass", {}))


class ArchiveRules:
    def __init__(self, spec: dict):
        validate_spec(spec)
        self.spec = spec
        self.records = [("SELF", spec["mappings"]["self_organization"])] + [
            ("GROUP", row) for row in spec["mappings"]["group_organizations"]] + [
            ("EXTERNAL", row) for row in spec["mappings"]["external_organizations"]]
        self.workflow = [re.compile(pattern) for pattern in spec["classification"]["self_workflow_title_patterns"]]

    def normalize(self, title: str) -> str:
        value = " ".join(unicodedata.normalize("NFKC", title).split())
        prefixes = [unicodedata.normalize("NFKC", value) for value in self.spec["normalization"].get("optional_display_prefixes", [])]
        while any(value.startswith(prefix) for prefix in prefixes):
            value = next(value[len(prefix):].lstrip() for prefix in prefixes if value.startswith(prefix))
        return re.sub(r"\s*\(由[^()]+原发\)\s*$", "", value)

    def issuer_matches(self, names: list[str]) -> list[tuple[str, dict]]:
        matches = {}
        for name in names:
            name = unicodedata.normalize("NFKC", name).strip()
            for category, record in self.records:
                if re.fullmatch(record["issuer_name_pattern"], name):
                    matches[record["id"]] = (category, record)
        return list(matches.values())

    def primary_group_code(self, title: str):
        """A leading group document code identifies the OA main document.

        Ancillary attachments can be issued by other organizations. Citations
        and codes occurring later in an internal workflow title do not qualify.
        """
        if not self.spec.get('source_priority', {}).get('primary_group_document_code'):
            return None
        heading=self.normalize(title)
        heading=re.sub(r'^(?:【[^】]{1,30}】\s*)+','',heading)
        heading=re.sub(r'^(?:[（(](?:以此为准|盖章|盖章版|转发)[）)]\s*)+','',heading)
        matches=[]
        for category,record in self.records:
            code=record.get('document_code_pattern')
            if category=='GROUP' and code and re.match(
                r'(?:'+code+r')\s*[\[〔【(]\s*(?:19|20)\d{2}\s*[\]〕】)]\s*第?\s*\d+\s*(?:号|(?=[-—:：]))',heading):
                matches.append((category,record))
        if len(matches)!=1:return None
        main_heading=heading.split('关于',1)[0]
        other_codes=[record['id'] for category,record in self.records
                     if record.get('document_code_pattern') and re.search(
                         r'(?:'+record['document_code_pattern']+r')\s*[\[〔【(]\s*(?:19|20)\d{2}',main_heading)]
        return matches[0] if len(set(other_codes))==1 else None

    def primary_external_code(self, title: str):
        if not self.spec.get('source_priority',{}).get('primary_external_document_code'):
            return None
        heading=self.normalize(title)
        heading=re.sub(r'^(?:【[^】]{1,30}】\s*)+','',heading)
        matches=[]
        for category,record in self.records:
            code=record.get('document_code_pattern')
            if category!='EXTERNAL' or not code:continue
            for match in re.finditer(r'(?:'+code+r')\s*[\[〔【(]\s*(?:19|20)\d{2}\s*[\]〕】)]\s*第?\s*\d+\s*号',heading):
                prefix=heading[:match.start()].rstrip()
                suffix=heading[match.end():].strip()
                trailing=(prefix.endswith(('【','（','(')) and re.fullmatch(r'[】）)]',suffix)
                          and re.search(r'(?:通知|通报|函|公告|意见|办法|指引|方案)[【（(]$',prefix))
                if match.start()==0 or trailing:matches.append((category,record))
        unique={record['id']:(category,record)for category,record in matches}
        return next(iter(unique.values())) if len(unique)==1 else None

    @staticmethod
    def body_issuers(body: str) -> list[str]:
        # Only explicit issuer labels are evidence; merely appearing in body
        # text or an OA user label is never enough.
        return list(dict.fromkeys(re.findall(r"(?:^|\n)\s*(?:发文单位|发文机关|文件落款|落款单位)\s*[:：]\s*([^\n\r]{2,100})", body)))

    def _prefix_matches(self, title: str) -> list[tuple[str, dict]]:
        result = {}
        for category, record in self.records:
            match = re.match(record["issuer_name_pattern"], title)
            if not match:
                continue
            rest = title[match.end():].lstrip()
            if re.match(r"(?:关于|印发|转发|发布|下发|通知|通报|公告|文件|函|致|[:：])", rest):
                result[record["id"]] = (category, record)
            elif re.match(r"[、与和及]", rest):
                heading = re.split(r"关于|印发|发布|通知|通报", title, maxsplit=1)[0]
                for other_category, other in self.records:
                    if re.search(other["issuer_name_pattern"], heading):
                        result[other["id"]] = (other_category, other)
            else:
                # Joint issuer headings also use a plain space/newline.
                for other_category, other in self.records:
                    second=re.match(other["issuer_name_pattern"],rest)
                    if second and re.match(r"(?:关于|印发|发布|通知|通报)",rest[second.end():].lstrip()):
                        result[record["id"]]=(category,record)
                        result[other["id"]]=(other_category,other)
        return list(result.values())

    def classify(self, row, action: str, *, issuer_names: list[str] | None = None) -> dict:
        date = row.initiated_at or row.completed_at
        year = str(date.year) if date else self.spec["folder_structure"].get("missing_year_value", "年份待确认")
        base = {"oa_item_id": row.workitem_id_text or row.oa_item_key, "original_title": row.title,
                "archive_year": year, "archive_organization": None, "archive_series": None,
                "matched_mapping_id": None, "classification_version": self.spec["version"],
                "classification_source": "title_rule", "needs_review": False,
                "source_organization_verified": False, "source_organizations": [], "source_evidence_level": "title",
                "archive_bucket": None, "suggested_category_id": None, "source_relationship": None,
                "source_heading": None}
        if action != "download":
            return {**base, "archive_category_id": "EXCLUDED" if action == "skip" else "UNKNOWN",
                    "virtual_archive_path": None, "matched_rule_id": None,
                    "classification_reason": "已排除，仅保留索引" if action == "skip" else "等待下载意见复核",
                    "classification_status": "excluded" if action == "skip" else "needs_confirmation",
                    "needs_review": action != "skip"}
        title = self.normalize(row.title)
        own_workflow = any(pattern.search(title) for pattern in self.workflow) and not title.startswith("关于")
        series = [record for record in self.spec["mappings"]["publication_series"] if re.search(record["title_pattern"], title)]
        category, record, rule, reason = "UNKNOWN", None, "C99", "标题缺少可核实来源，保留待确认"
        if series and not own_workflow:
            main_series=None
            if len(series)>1 and self.spec.get('source_priority',{}).get('combined_publication')=='first_title_series':
                positions=[(re.search(record['title_pattern'],title).start(),record)for record in series]
                first=min(position for position,_ in positions)
                primary=[record for position,record in positions if position==first]
                if len(primary)==1:main_series=primary[0]
            if len(series) == 1 or main_series:
                if main_series:
                    base['related_publication_series']=[record['name']for record in series if record['id']!=main_series['id']]
                    base['classification_source']='combined_publication_primary'
                    series=[main_series]
                category, record, rule, reason = "PUBLICATION", series[0], "C01", "明确的具名刊物或固定学习系列"
            else:
                reason = "单个事项包含多个独立刊物系列，无法确定共同系列"
        else:
            explicit = self.issuer_matches(issuer_names or [])
            codes = {}
            for source_category, mapping in self.records:
                pattern = mapping.get("document_code_pattern")
                if pattern and re.search(r"(?:" + pattern + r")\s*[\[〔【(]\s*(?:19|20)\d{2}\s*[\]〕】)]\s*第?\d+\s*号", title):
                    codes[mapping["id"]] = (source_category, mapping)
            primary_group = self.primary_group_code(row.title) or (None if own_workflow else self.primary_external_code(row.title))
            sources = [primary_group] if primary_group else explicit or ([] if own_workflow else list(codes.values()) or self._prefix_matches(title))
            if issuer_names and not primary_group and (not explicit or any(not self.issuer_matches([name]) for name in issuer_names)):
                reason = "正文发文单位未唯一匹配机构字典"
            elif len(sources) > 1:
                reason = "存在多个独立发文机构，来源冲突"
            elif sources:
                category, record = sources[0]
                rule = {"SELF":"C02", "GROUP":"C03", "EXTERNAL":"C04"}[category]
                reason = "明确的正文发文单位" if explicit else "唯一正式文号" if codes else "标题起始段明确发文主体"
                if explicit:
                    base["classification_source"] = "body_issuer_label"
                if primary_group:
                    reason='主文正式文号确定来源；随附参考文件不改变事项来源'
                    base['classification_source']='primary_group_document_code' if category=='GROUP' else 'primary_external_document_code'
            elif own_workflow:
                category, record, rule, reason = "SELF", self.spec["mappings"]["self_organization"], "C02", "明确的本公司业务审批或办理流程"
        second = None
        if category == "UNKNOWN":
            from oa_knowledge.archive_patterns import suggest, normalized_fields
            second = suggest(self.spec.get("second_pass", {}), row.title, explicit_body=bool(issuer_names))
            if second:
                category, rule, reason = second["category"], second["id"], second["reason"]
                label, kind = second.get("label"), second["kind"]
                record = {"id": second["id"], "name": label} if label and category != "UNKNOWN" else None
                if category == "SELF":
                    record = self.spec["mappings"]["self_organization"]
                base.update(classification_source="second_pass_title", source_evidence_level=kind,
                            archive_bucket=label if kind in {"theme", "joint"} else None,
                            suggested_category_id=second.get("suggested_category"))
                if kind in {"issuer", "joint"}:
                    fields = normalized_fields(row.title)
                    multiple = bool(re.search(r"[；;]|[两三四五六七八九十\d]+文合办", row.title))
                    heading = "；".join(part.split("关于", 1)[0] for part in re.split(r"[；;]", fields["clean"])) if multiple else fields["head"]
                    base["source_heading"] = heading
                    base["source_relationship"] = "multiple_documents" if multiple else "joint_heading" if kind == "joint" else "single_heading"
                    base["source_organizations"] = list(dict.fromkeys(
                        match.group() for _, mapping in self.records for match in re.finditer(mapping["issuer_name_pattern"], heading)))
        if not second and record and category in {"SELF", "GROUP", "EXTERNAL"}:
            base["source_organizations"] = [record["name"]]
        base["source_organization_verified"] = base["classification_source"] == "body_issuer_label"
        identifier = str(base["oa_item_id"])
        safe_id = safe_segment(identifier, 90)
        if safe_id != identifier:
            safe_id += "_" + title_hash(identifier)[:12]
        parts = [CATEGORIES[category]]
        if category in {"GROUP", "EXTERNAL", "PUBLICATION"}:
            parts.append(safe_segment(record["name"]))
        parts += [safe_segment(year), safe_id]
        return {**base, "archive_category_id": category,
                "archive_organization": record["name"] if record and category != "PUBLICATION" and not (second and second["kind"] in {"theme", "joint"}) else None,
                "archive_series": record["name"] if record and category == "PUBLICATION" else None,
                "matched_mapping_id": record["id"] if record else None, "matched_rule_id": rule,
                "virtual_archive_path": "/".join(parts), "classification_reason": reason,
                "classification_status": "needs_confirmation" if category == "UNKNOWN" else "source_pending" if second and second["kind"] == "theme" else "classified",
                "needs_review": category == "UNKNOWN" or bool(second and second["kind"] == "theme")}


class ArchiveContext:
    def __init__(self, session: Session, plan: ArchiveClassificationPlan, settings=None):
        self.session, self.plan = session, plan
        self.settings = settings
        self.rules = ArchiveRules(json.loads(plan.spec_json))
        self.results = {(row.oa_item_key, row.input_sha256): row for row in session.scalars(
            select(ArchiveClassificationResult).where(ArchiveClassificationResult.plan_id == plan.id))}
        self.enriched = {}
        for record in self.results.values():
            result = json.loads(record.result_json)
            if result.get("library_revision") == CLASSIFIER_REVISION and result.get("library_metadata"):
                identity = (record.oa_item_key, result["library_metadata"])
                if identity not in self.enriched or record.id > self.enriched[identity].id:
                    self.enriched[identity] = record
        self.issuers = {}
        for evidence in session.scalars(select(TitleReviewEvidence).order_by(TitleReviewEvidence.id)):
            payload = json.loads(evidence.payload_json)
            self.issuers[(evidence.oa_item_key, evidence.title_sha256)] = self.rules.body_issuers(payload.get("body", ""))

    def decision(self, row, action: str) -> dict:
        if self.settings is not None:
            from oa_knowledge.source_library import classify_library_item
            return classify_library_item(self.session, self.settings, row, action, plan=self.plan)
        metadata = title_hash(json.dumps([row.title, str(row.initiated_at), str(row.completed_at), action], ensure_ascii=False))
        enriched = self.enriched.get((row.oa_item_key, metadata))
        if enriched:
            return json.loads(enriched.result_json)
        issuers = self.issuers.get((row.oa_item_key, title_hash(row.title)), [])
        raw = json.dumps([CLASSIFIER_REVISION, row.title, str(row.initiated_at), str(row.completed_at), action, issuers], ensure_ascii=False)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        existing = self.results.get((row.oa_item_key, digest))
        if existing is not None:
            return json.loads(existing.result_json)
        result = self.rules.classify(row, action, issuer_names=issuers)
        record = ArchiveClassificationResult(plan_id=self.plan.id, oa_item_key=row.oa_item_key,
            input_sha256=digest, result_json=json.dumps(result, ensure_ascii=False))
        self.session.add(record); self.results[(row.oa_item_key, digest)] = record
        return result


def active_context(session: Session, settings=None) -> ArchiveContext | None:
    plan = session.scalar(select(ArchiveClassificationPlan).where(ArchiveClassificationPlan.active.is_(True)))
    return ArchiveContext(session, plan, settings) if plan else None


def settings_state(settings) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            plan = session.scalar(select(ArchiveClassificationPlan).where(ArchiveClassificationPlan.active.is_(True)))
            return {"plan_id": plan.id if plan else None, "spec": json.loads(plan.spec_json) if plan else None,
                    "history": [{"id": row.id, "version": row.version, "active": row.active} for row in session.scalars(
                        select(ArchiveClassificationPlan).order_by(ArchiveClassificationPlan.id.desc()).limit(20))]}
    finally:
        engine.dispose()


def apply_spec(settings, spec: dict, *, base_plan_id: int | None = None, save: bool = False) -> dict:
    from oa_knowledge.title_screening import active_context as screening_context, assert_idle
    from oa_knowledge.db.models import OAManifestItem
    validate_spec(spec)
    rules = ArchiveRules(spec)
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            if save:
                session.execute(text("BEGIN IMMEDIATE")); assert_idle(session)
            old = session.scalar(select(ArchiveClassificationPlan).where(ArchiveClassificationPlan.active.is_(True)))
            if base_plan_id is not None and (old is None or old.id != base_plan_id):
                raise ValueError("分类方案已改变，请重新读取")
            screening = screening_context(session)
            if screening is None:
                raise ValueError("请先启用下载筛选方案")
            counts, downloads, rule_counts = Counter(), Counter(), Counter()
            source_pending = 0
            issuers = {}
            for evidence in session.scalars(select(TitleReviewEvidence).order_by(TitleReviewEvidence.id)):
                issuers[(evidence.oa_item_key, evidence.title_sha256)] = rules.body_issuers(json.loads(evidence.payload_json).get("body", ""))
            rows = list(session.scalars(select(OAManifestItem)))
            for row in rows:
                current = screening.decision(row)
                result = rules.classify(row, current["action"], issuer_names=issuers.get((row.oa_item_key, title_hash(row.title)), []))
                event = screening.overrides.get(row.oa_item_key)
                if event and event.folder and current["action"] == "download":
                    result["archive_category_id"] = next((key for key, root in CATEGORIES.items() if event.folder.split("/", 1)[0] == root), "MANUAL")
                downloads[current["action"]] += 1
                counts[result["archive_category_id"]] += 1
                if result["classification_source"] == "second_pass_title":
                    rule_counts[result["matched_rule_id"]] += 1
                source_pending += result["classification_status"] == "source_pending"
            response = {"counts": dict(counts), "download_counts": dict(downloads), "total": len(rows),
                        "second_pass_counts": dict(rule_counts), "source_pending": source_pending}
            if save:
                if old:
                    old.active = False
                if base_plan_id is not None:
                    spec = {**spec, "version": f"{spec['version'].split('-edit-')[0]}-edit-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}"}
                raw = json.dumps(spec, ensure_ascii=False, sort_keys=True)
                plan = ArchiveClassificationPlan(version=spec["version"], spec_json=raw,
                    source_sha256=hashlib.sha256(raw.encode()).hexdigest(), active=True)
                session.add(plan); session.flush()
                classification = ArchiveContext(session, plan)
                for row in rows:
                    classification.decision(row, screening.decision(row)["action"])
                session.commit(); response.update(plan_id=plan.id, version=plan.version)
            return response
    finally:
        engine.dispose()


def import_spec(settings, path: Path) -> dict:
    return apply_spec(settings, json.loads(path.read_text(encoding="utf-8-sig")), save=True)


def activate(settings, plan_id: int) -> dict:
    from oa_knowledge.title_screening import assert_idle
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            session.execute(text("BEGIN IMMEDIATE")); assert_idle(session)
            if session.get(ArchiveClassificationPlan, plan_id) is None:
                raise ValueError("分类版本不存在")
            for plan in session.scalars(select(ArchiveClassificationPlan)):
                plan.active = plan.id == plan_id
            session.commit()
            return {"plan_id": plan_id}
    finally:
        engine.dispose()
