"""Rebuild the generated Obsidian Vault from SQLite and immutable parse artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, ContentObject, OAItem, OAManifestItem, ParseArtifact


@dataclass(frozen=True)
class Classification:
    parts: tuple[str, ...]
    confidence: float
    basis: tuple[str, ...]
    knowledge_value: str
    method: str = "deterministic_rule"
    normalized_title: str = ""
    forwarding_evidence: str = ""
    project_name: str = ""
    project_unresolved: bool = False
    effectiveness_status: str = "unknown"


def safe_name(value: str, limit: int = 150) -> str:
    value = re.sub(r"^(?:【公告】|【通知】|【文件传阅】)+", "", value).strip()
    value = re.sub(r"\s*\(由[^()]*原发\)\s*$", "", value).strip()
    value = re.sub(r"[\\/:*?\"<>|\[\]#^\r\n\t]+", "_", value).strip(" ._")
    value = value or "未命名"
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    encoded = encoded[:limit]
    while True:
        try:
            return encoded.decode("utf-8").rstrip(" ._")
        except UnicodeDecodeError:
            encoded = encoded[:-1]


def stable_item_id(logical_item_id: int | None, oa_id: str) -> str:
    return f"LI{logical_item_id:06d}" if logical_item_id is not None else f"W{safe_name(oa_id, 50)}"


def _classify_item_legacy(title: str, sender: str) -> Classification:
    text = f"{title} {sender}"
    if "上级控股集团" in text:
        second = "02_制度办法" if any(x in text for x in ("制度", "办法", "细则", "规则")) else "01_正式发文"
        if any(x in text for x in ("财务", "资金", "账户", "预算", "税务")): second = "04_财务资金"
        elif any(x in text for x in ("人力", "人员", "任免", "职务", "培训")): second = "06_人力行政"
        elif any(x in text for x in ("会议", "董事会", "决议")): second = "08_会议与决策"
        return Classification(("02_上级控股集团文件", second), .93, ("文号、标题或发文主体命中上级控股集团",), "high")
    if "上级金融集团" in text:
        second = "02_制度办法" if any(x in text for x in ("制度", "办法", "细则")) else "03_经营管理"
        if any(x in text for x in ("报送", "统计", "填报")): second = "06_统计报送"
        elif any(x in text for x in ("考核", "评价")): second = "07_考核评价"
        elif any(x in text for x in ("会议", "决议")): second = "08_会议与决策"
        return Classification(("03_上级金融集团文件", second), .9, ("标题或主体命中上级金融集团",), "high")
    government = ("区委", "区政府", "市政府", "人大常委会", "政协", "财政局", "国资", "监管局", "开发区", "省科技厅", "上级")
    if "文件传阅" in title or any(x in text for x in government):
        second = "01_政策法规" if any(x in text for x in ("条例", "法规", "政策", "实施意见")) else "03_通知通报"
        if any(x in text for x in ("监管", "督查", "检查")): second = "02_监管要求"
        elif any(x in text for x in ("财政", "国资")): second = "04_财政与国资"
        elif any(x in text for x in ("统计", "报送", "填报")): second = "05_统计报送"
        return Classification(("01_政府及上级部门文件", second), .88, ("内部传阅标题或政府主体证据",), "high")
    project = ("融资租赁", "承租", "项目", "立项", "尽调", "投放", "出账", "租后", "还款", "结清")
    if any(x in text for x in project):
        stage = "01_项目开发与准入"
        if any(x in text for x in ("立项", "尽调")): stage = "02_立项与尽调"
        elif any(x in text for x in ("评审", "决议", "审批")): stage = "03_评审与决策"
        elif any(x in text for x in ("合同", "保证", "抵押", "质押")): stage = "04_合同与增信"
        elif any(x in text for x in ("投放", "出账")): stage = "05_投放与出账"
        elif any(x in text for x in ("登记", "保险")): stage = "06_登记与保险"
        elif "租后" in text: stage = "07_租后管理"
        elif any(x in text for x in ("变更", "展期")): stage = "08_项目变更"
        elif any(x in text for x in ("还款", "结清", "退出")): stage = "09_结清与退出"
        return Classification(("04_本公司文件", "03_融资租赁项目", stage), .87, ("事项核心为融资租赁项目办理",), "high")
    if any(x in text for x in ("股东会", "董事会", "办公会", "议案", "决议", "授权", "组织架构")):
        third = "01_股东会与董事会" if any(x in text for x in ("股东会", "董事会")) else "03_议案与决议"
        return Classification(("04_本公司文件", "01_公司治理", third), .84, ("公司治理关键词",), "high")
    if any(x in text for x in ("制度", "办法", "细则", "规程", "正式通知", "表单", "模板")):
        third = "01_公司制度" if any(x in text for x in ("制度", "办法", "细则", "规程")) else "04_表单与模板"
        return Classification(("04_本公司文件", "02_制度与正式发文", third), .8, ("内部制度或模板关键词",), "high")
    processes = (("付款", "01_付款与报销"), ("报销", "01_付款与报销"), ("用印", "02_用印与证照"), ("印鉴", "02_用印与证照"), ("证照", "02_用印与证照"), ("出差", "03_出差与请假"), ("请假", "03_出差与请假"), ("休假", "03_出差与请假"), ("采购", "04_采购与资产"), ("资产", "04_采购与资产"))
    for keyword, third in processes:
        if keyword in text:
            return Classification(("04_本公司文件", "08_内部事务流程", third), .9, (f"内部流程关键词：{keyword}",), "archive_only")
    if any(x in text for x in ("报送", "填报", "统计", "工作材料")):
        return Classification(("04_本公司文件", "09_统计报送与工作材料"), .76, ("内部统计报送关键词",), "low")
    if any(x in text for x in ("银行", "证券", "律师", "会计师", "协会")):
        second = "02_银行及金融机构" if any(x in text for x in ("银行", "证券")) else "03_中介机构"
        return Classification(("05_外部单位文件", second), .65, ("外部机构关键词，主体仍需复核",), "medium")
    return Classification(("90_待分类", "04_低置信度"), .35, ("缺少可靠来源主体和事项性质证据",), "medium")


BUSINESS_STAFF: set[str] = set()
RISK_STAFF: set[str] = set()
GENERAL_STAFF: set[str] = set()
FINANCE_STAFF: set[str] = set()
UPSTREAM_STAFF: set[str] = set()
GUARANTEE_STAFF: set[str] = set()


def normalize_title(title: str) -> tuple[str, str]:
    forwarding = ""
    match = re.search(r"[（(]?由([^()（）]+?)原发[）)]?", title)
    if match:
        forwarding = match.group(1).strip()
    value = re.sub(r"[（(]?由[^()（）]+?原发[）)]?", "", title)
    value = re.sub(r"^(?:【(?:文件传阅|传阅件|公告|通知|重要文件|★重要文件|以此为准|免予公开|\d{1,2}月\d{1,2}日)】)+", "", value)
    value = re.sub(r"^(?:（?盖章版）?|以此件为准|请以此为准|转发|（?两文合办）?|三文合发)[-_：:、\s]*", "", value)
    return value.strip(" -_：:（）()"), forwarding


def _sender_in(sender: str, names: set[str]) -> bool:
    return any(name in sender for name in names)


def _project_stage(text: str) -> str:
    rules = (
        (("立项", "尽调", "尽职调查", "项目预审", "现场调查"), "02_立项与尽调"),
        (("评审", "评审会", "审查意见", "项目决议", "风险审查", "项目审批"), "03_评审与决策"),
        (("合同审批", "融资租赁合同", "保证合同", "抵押合同", "担保", "增信", "法律文本"), "04_合同与增信"),
        (("出账", "放款", "投放", "起租", "租金表", "IRR", "付款条件"), "05_投放与出账"),
        (("中登", "抵押登记", "租赁登记", "保险", "权属登记"), "06_登记与保险"),
        (("租后", "资产分类", "风险评级", "租后检查", "贷后", "存续期"), "07_租后管理"),
        (("方案变更", "租金调整", "主体变更", "担保变更", "展期", "补充协议"), "08_项目变更"),
        (("提前还款", "提前结清", "结清", "项目终止", "回购", "退出", "所有权转移"), "09_结清与退出"),
    )
    for words, stage in rules:
        if any(word in text for word in words):
            return stage
    return "01_项目开发与准入"


def _project_name(title: str) -> str:
    cleaned = re.sub(r"^(?:印鉴使用申请表|内部事项呈批表|业务合同审批表|项目审批表)[-_：:]*", "", title)
    patterns = (
        r"关于(.{2,50}?项目)(?:的|全部|提前|方案|业务)",
        r"([\u4e00-\u9fffA-Za-z0-9、，·]{2,50}?(?:联合承租|承租|融资租赁|经营租赁)?项目)",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if match:
            value = re.sub(r"^(?:关于|审议)", "", match.group(1)).strip(" -_：:，,、")
            if value not in {"融资租赁项目", "租赁项目", "项目"}:
                return safe_name(value, 120)
    return ""


def _subject_business(source: str, text: str) -> str:
    personnel = ("任免", "任职", "免职", "职务调整", "职级晋升", "工作分工", "领导分工", "人员安排", "聘任", "解聘", "选举结果")
    if any(x in text for x in personnel): return "03_人事任免" if source in {"02_上级控股集团文件", "03_上级金融集团文件"} else "04_人事任免"
    if any(x in text for x in ("工会", "党支部", "党建", "党员", "纪检", "廉政", "四风", "警示教育", "安全生产", "保密", "国家安全")):
        return "09_党群工会" if source in {"02_上级控股集团文件", "03_上级金融集团文件"} else "08_党群工会"
    if any(x in text for x in ("制度", "办法", "规定", "细则", "指引", "规则")): return "02_制度办法" if source != "01_政府及上级部门文件" else "01_政策法规"
    if any(x in text for x in ("财务", "资金", "预算", "决算", "账户", "税务")): return "05_财务资金" if source in {"02_上级控股集团文件", "03_上级金融集团文件"} else "05_财政与国资"
    if any(x in text for x in ("风险", "合规", "审计", "监管评级")): return "06_风险合规" if source in {"02_上级控股集团文件", "03_上级金融集团文件"} else "02_监管要求"
    if any(x in text for x in ("考核", "评价")):
        return "02_监管要求" if source == "01_政府及上级部门文件" else "07_考核评价"
    if any(x in text for x in ("会议", "决议", "议案")): return "08_会议与决策" if source != "01_政府及上级部门文件" else "07_会议与决策"
    if any(x in text for x in ("报送", "统计", "填报")): return "10_统计报送" if source in {"02_上级控股集团文件", "03_上级金融集团文件"} else "06_统计报送"
    return "01_正式发文" if source in {"02_上级控股集团文件", "03_上级金融集团文件"} else "03_通知通报"


def classify_item(title: str, sender: str) -> Classification:
    normalized, forwarding = normalize_title(title)
    text = f"{normalized} {sender}"
    effectiveness = "revising" if any(x in text for x in ("征求意见", "修订稿", "草案", "讨论稿")) else "unknown"
    number = re.search(r"(?:示例集团|示例公司)(?:人|党)?〔?\d{4}〕?\d+号", normalized)
    source = ""
    if "上级控股集团" in normalized: source = "02_上级控股集团文件"
    elif "上级金融集团" in normalized: source = "03_上级金融集团文件"
    elif re.search(r"示例融资租赁|本公司", normalized): source = "04_本公司文件"
    government = ("国务院", "中共中央", "中央纪委", "省委", "省政府", "市委", "市政府", "区委", "区政府", "开发区管委会", "国资局", "国资委", "组织部", "纪委监委", "总工会", "科学技术协会", "人大", "政协", "金融局", "工信局", "工业和信息化局", "住建局", "住房和城乡建设局", "商务局", "财政局", "统计局", "科技局", "科技厅", "民政局", "水务局", "文旅局", "文化广电旅游局", "街道党工委")
    if not source and any(x in normalized for x in government): source = "01_政府及上级部门文件"
    if source:
        if source == "04_本公司文件":
            return _classify_company(normalized, sender, forwarding, effectiveness, "document_number_or_organization" if number else "issuing_organization", .97)
        business = _subject_business(source, normalized)
        return Classification((source, business), .97 if number else .92, ("明确文号或实际发文机构",), "high", "document_number_or_organization", normalized, forwarding, effectiveness_status=effectiveness)
    is_forward = any(x in title for x in ("文件传阅", "传阅件", "文件分送", "文件翻印", "转发文件"))
    if is_forward:
        external = "05_外部单位文件" if any(x in normalized for x in ("银行", "律师事务所", "会计师事务所", "协会")) else "01_政府及上级部门文件"
        business = "02_银行及金融机构" if external == "05_外部单位文件" and "银行" in normalized else (_subject_business(external, normalized) if external != "05_外部单位文件" else "09_其他外部单位")
        return Classification((external, business), .75, ("文件传阅且主体按标题确定或采用上级文件兜底",), "high", "forwarding_rule", normalized, forwarding, effectiveness_status=effectiveness)
    return _classify_company(normalized, sender, forwarding, effectiveness, "strong_rule_or_staff_profile", .82)


def _classify_company(title: str, sender: str, forwarding: str, effectiveness: str, method: str, confidence: float) -> Classification:
    text=f"{title} {sender}"
    project_words=("融资租赁", "经营租赁", "联合租赁", "厂商租赁", "承租", "立项", "尽调", "评审", "合同审批", "出账", "放款", "投放", "租后", "方案变更", "提前还款", "结清")
    if any(x in text for x in project_words):
        project=_project_name(title); stage=_project_stage(text)
        return Classification(("04_本公司文件","03_融资租赁项目",safe_name(project or "00_通用及未命名项目",120),stage),.9,("融资租赁项目生命周期强规则",),"high","project_rule",title,forwarding,project,not bool(project),effectiveness)
    if any(x in text for x in ("股东会", "董事会", "董事会议题申请书", "董事会材料", "董事会决议", "办公会议题申请书", "办公会会议纪要", "三重一大", "授权", "公司章程", "组织架构")):
        return Classification(("04_本公司文件","01_公司治理"),.9,("公司治理标题强规则",),"high","title_strong_rule",title,forwarding,effectiveness_status=effectiveness)
    if any(x in text for x in ("工会", "党支部", "党建", "党员", "纪检", "廉政", "四风", "警示教育", "安全生产", "保密", "国家安全")):
        return Classification(("04_本公司文件","08_党群纪检与安全"),.9,("党群纪检安全标题强规则",),"medium","title_strong_rule",title,forwarding,effectiveness_status=effectiveness)
    form=any(x in title for x in ("印鉴使用申请表","综合管理部部门章使用申请表","内部事项呈批表"))
    if form:
        department="06_综合管理"
        if _sender_in(sender,BUSINESS_STAFF): department="04_业务管理"
        elif _sender_in(sender,RISK_STAFF): department="05_风险管理"
        elif _sender_in(sender,FINANCE_STAFF): department="07_财务管理"
        return Classification(("04_本公司文件",department),.9,("标准流程表单及发起人岗位画像",),"low","standard_form_staff_profile",title,forwarding,effectiveness_status=effectiveness)
    if any(x in text for x in ("风险评级","风险排查","合规预审","审计自查","风控制度","法律审查","合规","监管评级","风险报告","项目风险","租后风险")) or _sender_in(sender,RISK_STAFF):
        return Classification(("04_本公司文件","05_风险管理"),confidence,("风险主题或风险岗位画像",),"medium",method,title,forwarding,effectiveness_status=effectiveness)
    if any(x in text for x in ("预算","决算","财务报表","资金","银行账户","询证函","网银","税务","工资计提","财务数据","资金计划","原始凭证")) or _sender_in(sender,FINANCE_STAFF):
        return Classification(("04_本公司文件","07_财务管理"),confidence,("财务主题或财务岗位画像",),"medium",method,title,forwarding,effectiveness_status=effectiveness)
    if any(x in text for x in ("制度","办法","规定","细则","指引","规则","工作方案","议事规则")):
        return Classification(("04_本公司文件","02_制度与正式发文"),.86,("本公司制度正式发文强规则",),"high","title_strong_rule",title,forwarding,effectiveness_status=effectiveness)
    if _sender_in(sender,BUSINESS_STAFF): department="04_业务管理"
    elif _sender_in(sender,GENERAL_STAFF): department="06_综合管理"
    elif _sender_in(sender,UPSTREAM_STAFF) or _sender_in(sender,GUARANTEE_STAFF):
        return Classification(("03_上级金融集团文件","04_经营管理"),.65,("上级流转或融资担保岗位确定性兜底",),"medium","staff_profile_fallback",title,forwarding,effectiveness_status=effectiveness)
    elif "上级控股集团" in sender:
        return Classification(("02_上级控股集团文件","04_经营管理"),.7,("上级控股集团发起人画像兜底",),"medium","staff_profile_fallback",title,forwarding,effectiveness_status=effectiveness)
    else: department="09_统计报送与工作材料"
    return Classification(("04_本公司文件",department),.58,("确定性业务兜底，不进入人工待分类",),"low","deterministic_fallback",title,forwarding,effectiveness_status=effectiveness)


def fm(data: dict) -> str:
    return "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _is_package(file: ArchivedFile) -> bool:
    name = file.original_name.lower()
    return any(ext in name for ext in (".zip", ".rar", ".7z", ".tar", ".gz"))


def rebuild_vault(settings: Settings, engine, target: Path) -> dict:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in ("00_知识库入口", "01_政府及上级部门文件", "02_上级控股集团文件", "03_上级金融集团文件", "04_本公司文件", "05_外部单位文件", "80_专题与索引", "99_系统/内容对象"):
        (target / name).mkdir(parents=True, exist_ok=True)
    counts = Counter(); paths: dict[str, str] = {}; attachment_content_refs = Counter()
    with Session(engine) as db:
        manifests = list(db.scalars(select(OAManifestItem).where(
            OAManifestItem.processing_status == "downloaded",
            or_(OAManifestItem.matched_exclusion_keyword.is_(None), OAManifestItem.matched_exclusion_keyword == ""),
        ).order_by(OAManifestItem.id)))
        items = {x.oa_item_key: x for x in db.scalars(select(OAItem)).all()}
        files_by_item: dict[int, list[ArchivedFile]] = {}
        for file in db.scalars(select(ArchivedFile).order_by(ArchivedFile.id)).all():
            files_by_item.setdefault(file.oa_item_id, []).append(file)
        artifacts = {x.id: x for x in db.scalars(select(ParseArtifact)).all()}
        contents = {x.id: x for x in db.scalars(select(ContentObject)).all()}

        for content in contents.values():
            artifact = artifacts.get(content.active_parse_artifact_id or -1)
            if not artifact or artifact.lifecycle_status != "valid": continue
            source = settings.data_root / "parse" / artifact.output_relpath
            if not source.is_file(): continue
            title = f"内容对象 {content.id}"
            body = source.read_text(encoding="utf-8", errors="replace")
            rel = Path("99_系统/内容对象") / f"CO-{content.id}__{safe_name(title)}.md"
            write(target / rel, fm({"managed_by":"oaradar","doc_kind":"content_object","content_object_id":content.id,"sha256":content.sha256,"active_parse_artifact_id":artifact.id,"parse_engine":artifact.engine}) + f"\n# {title}\n\n## 正文\n\n{body}\n")
            counts["content_objects"] += 1

        for manifest in manifests:
            item = items.get(manifest.oa_item_key)
            oa_id = manifest.workitem_id_text or (item.workitem_id_text if item else None) or manifest.oa_item_key
            sid = stable_item_id(item.logical_item_id if item else None, oa_id)
            classification = classify_item(manifest.title, manifest.sender or (item.sender if item else "") or "")
            short = safe_name(classification.normalized_title or manifest.title)
            folder_rel = Path(*classification.parts) / f"OA-{sid}__{short}"
            folder = target / folder_rel
            folder.mkdir(parents=True, exist_ok=True)
            paths[oa_id] = folder_rel.as_posix(); counts[classification.parts[0]] += 1
            counts[f"method:{classification.method}"] += 1
            if classification.forwarding_evidence: counts[f"forward:{classification.parts[0]}"] += 1
            if classification.project_name: counts["named_project_items"] += 1
            if classification.project_unresolved: counts["unnamed_project_items"] += 1
            item_files = files_by_item.get(item.id, []) if item else []
            attachments = [f for f in item_files if f.file_role in {"direct_attachment", "official_attachment"}]
            evidence = [f for f in item_files if f.file_role not in {"direct_attachment", "official_attachment"}]
            packages = [f for f in attachments if _is_package(f)]
            ordinary = [f for f in attachments if not _is_package(f)]
            att_links=[]; package_links=[]; parsed_count=0
            for ordinal, file in enumerate(ordinary, 1):
                rel = folder_rel / "附件" / f"ATT-F{file.id}__{safe_name(Path(file.original_name).stem)}.md"
                content = contents.get(file.content_object_id or -1)
                artifact = artifacts.get(content.active_parse_artifact_id or -1) if content else None
                parse_ok = bool(artifact and artifact.lifecycle_status == "valid")
                if parse_ok: parsed_count += 1; attachment_content_refs[content.id] += 1
                meta={"managed_by":"oaradar","doc_kind":"oa_attachment","source_attachment_id":f"F{file.id}","content_object_id":content.id if content else None,"active_parse_artifact_id":artifact.id if parse_ok else None,"original_filename":file.original_name,"source_order":ordinal,"document_role":file.file_role,"reused_content":bool(content and attachment_content_refs[content.id]>1),"privacy_level":"internal","parse_status":"valid" if parse_ok else "unparsed","download_status":file.download_status,"source_relpath":file.local_relpath}
                body=f"# {file.original_name}\n\n来源文件：`{file.local_relpath or '无本地路径'}`\n"
                if parse_ok: body += f"\n## 解析正文\n\n![[99_系统/内容对象/CO-{content.id}__内容对象 {content.id}#正文]]\n"
                else: body += "\n> [!warning] 尚无通过质量门禁的解析正文。\n"
                write(target/rel, fm(meta)+"\n"+body); att_links.append((rel, file.original_name)); counts["attachments"]+=1
            for file in packages:
                package_dir = folder_rel / "压缩包" / f"AP-F{file.id}__{safe_name(Path(file.original_name).stem)}"
                rel = package_dir / f"AP-F{file.id}__压缩包目录.md"
                meta={"managed_by":"oaradar","doc_kind":"archive_package","archive_package_id":f"F{file.id}","original_filename":file.original_name,"sha256":file.sha256,"archive_format":Path(file.original_name).suffix.lower().lstrip('.'),"member_count":0,"extraction_status":"not_indexed","security_status":"pending","source_relpath":file.local_relpath}
                body=f"# {file.original_name}\n\n> [!warning] 压缩包已保留在事项内，但数据库尚无结构化成员目录；需后续安全解包。\n"
                write(target/rel,fm(meta)+"\n"+body); package_links.append((rel,file.original_name)); counts["packages"]+=1
            has_workflow=any(f.file_role == "workflow_snapshot" and f.download_status == "verified" for f in evidence)
            parsed_evidence=parsed_count + sum(1 for f in evidence if f.content_object_id and (contents.get(f.content_object_id) and artifacts.get(contents[f.content_object_id].active_parse_artifact_id or -1)))
            base={"managed_by":"oaradar","doc_kind":"oa_item","logical_item_id":sid,"oa_item_id":oa_id,"title":classification.normalized_title or manifest.title,"raw_title":manifest.title,"normalized_title":classification.normalized_title or manifest.title,"forwarding_evidence":classification.forwarding_evidence or None,"primary_source_category":classification.parts[0],"primary_business_category":classification.parts[1] if len(classification.parts)>1 else None,"primary_process_stage":classification.parts[-1] if len(classification.parts)>2 else None,"classification_method":classification.method,"source_organizations":[manifest.sender] if manifest.sender else [],"related_organizations":[],"projects":[classification.project_name] if classification.project_name else [],"project_extraction_status":"unresolved" if classification.project_unresolved else "resolved" if classification.project_name else None,"business_domains":[],"oa_item_types":[],"knowledge_topics":[],"effectiveness_status":classification.effectiveness_status,"lifecycle_status":"done_final","knowledge_value":classification.knowledge_value,"knowledge_status":"published" if parsed_evidence else "pending_parse","classification_basis":list(classification.basis),"classification_confidence":classification.confidence,"classification_rule_version":"vault-taxonomy-2026-07-28-v2","review_status":"unreviewed","first_seen_at":manifest.first_seen_at.isoformat() if manifest.first_seen_at else None,"completed_at":manifest.completed_at.isoformat() if manifest.completed_at else None,"attachment_count":len(attachments),"archive_package_count":len(packages)}
            overview_rel=folder_rel/f"OA-{sid}__事项总览.md"
            lines=[f"# {classification.normalized_title or manifest.title}","","## 事项概要","",f"- 原始标题：{manifest.title}",f"- 发起人或单位：{manifest.sender or '未记录'}",f"- 处理状态：{manifest.processing_status}",f"- 主分类：{' / '.join(classification.parts)}","","## 核心结果","", "当前结果以 OA 台账状态和本地归档证据为准。","","## 主要时间线","",f"- 首次发现：{manifest.first_seen_at or '未记录'}",f"- 办理完成：{manifest.completed_at or '未记录'}","","## 流程摘要",""]
            lines += ([f"参见 [[OA-{sid}__流程与意见]]。"] if has_workflow else ["当前数据库没有可发布的真实流程快照。"]) + ["","## 附件清单",""]
            lines += [f"- [[{p.relative_to(folder_rel).as_posix()[:-3]}|{label}]]" for p,label in att_links] or ["- 无普通附件引用。"]
            lines += ["","## 压缩包清单",""] + ([f"- [[{p.relative_to(folder_rel).as_posix()[:-3]}|{label}]]" for p,label in package_links] or ["- 无压缩包。"])
            lines += ["","## 知识提炼链接",""] + ([f"[[OA-{sid}__知识提炼]]"] if parsed_evidence else ["待解析：当前没有通过质量门禁的正文或附件，不生成知识提炼。"])
            lines += ["","## 原始来源和回溯","",*[f"- `{f.file_role}`：`{f.local_relpath or '无本地路径'}`" for f in evidence]]
            write(target/overview_rel,fm(base)+"\n"+"\n".join(lines)+"\n"); counts["overviews"]+=1
            if parsed_evidence:
                knowledge_rel=folder_rel/f"OA-{sid}__知识提炼.md"
                klines=[f"# {classification.normalized_title or manifest.title}：知识提炼","","## 已通过质量门禁的来源","",f"- [[OA-{sid}__事项总览]]", *[f"- [[{p.relative_to(folder_rel).as_posix()[:-3]}|{label}]]" for p,label in att_links if "ATT-" in p.name],"","## 说明","","本文件只索引已通过质量门禁的正文证据；尚未运行结构化大模型提炼，不编造业务结论。"]
                write(target/knowledge_rel,fm({**base,"doc_kind":"knowledge_extraction","knowledge_status":"published"})+"\n"+"\n".join(klines)+"\n"); counts["knowledge"]+=1
            else: counts["pending_parse"]+=1
            if has_workflow:
                flow_rel=folder_rel/f"OA-{sid}__流程与意见.md"
                flines=[f"# {classification.normalized_title or manifest.title}：流程与意见","","## 流程时间线","",f"- 办理时间：{manifest.completed_at or '未记录'}",f"- 当前归档状态：{manifest.processing_status}","","## 流程证据",""]+[f"- `{f.file_role}`：`{f.local_relpath or '无本地路径'}`" for f in evidence if f.file_role=="workflow_snapshot"]+["","## Pending 与 Done 差异","","当前仅发布数据库中可追溯的已办归档证据；不推断缺失意见。"]
                write(target/flow_rel,fm({**base,"doc_kind":"workflow_opinion"})+"\n"+"\n".join(flines)+"\n"); counts["flows"]+=1

    index_names=("01_项目索引.base","02_实际发文机构索引.base","03_人事任免索引.base","04_制度文件索引.base","05_事项类型索引.base","06_业务领域索引.base","07_年度索引.base","08_发起人索引.base","09_分类置信度索引.base","10_待解析索引.base")
    for name in index_names:
        write(target/"80_专题与索引"/name, yaml.safe_dump({"filters":{"and":["managed_by == 'oaradar'","doc_kind == 'oa_item'"]},"views":[{"type":"table","name":name[:-5]}]},allow_unicode=True,sort_keys=False))
    write(target/"00_知识库入口/README.md", fm({"managed_by":"oaradar","doc_kind":"vault_entry"})+"\n# OARadar 知识库\n\n按来源主体、事项性质和 OA 事项文件夹浏览。\n")
    report={"generated_at":datetime.now(timezone.utc).isoformat(),"manifest_items":len(manifests),"counts":dict(counts),"classification_counts":{k:v for k,v in counts.items() if k[:2].isdigit()},"method_counts":{k.removeprefix('method:'):v for k,v in counts.items() if k.startswith('method:')},"forwarding_distribution":{k.removeprefix('forward:'):v for k,v in counts.items() if k.startswith('forward:')},"paths_sha256":hashlib.sha256(json.dumps(paths,sort_keys=True).encode()).hexdigest()}
    write(target/"99_系统/迁移报告.json",json.dumps(report,ensure_ascii=False,indent=2))
    return report


def validate_vault(target: Path, expected_items: int) -> dict:
    files=list(target.rglob('*')); notes=[p for p in files if p.suffix=='.md']; overview=list(target.rglob('OA-*__事项总览.md'))
    broken=[]
    stems={p.stem for p in notes}
    link_re=re.compile(r"!?\[\[([^\]|#]+)")
    for note in notes:
        for raw in link_re.findall(note.read_text(encoding='utf-8',errors='replace')):
            candidate=raw.strip(); name=Path(candidate).name
            if name not in stems and not (target/f"{candidate}.md").is_file(): broken.append(f"{note.relative_to(target)} -> {candidate}")
    duplicate_ids=[]; seen=set()
    for note in overview:
        text=note.read_text(encoding='utf-8'); match=re.search(r"^logical_item_id:\s*['\"]?([^'\"\n]+)",text,re.M)
        if match:
            if match.group(1) in seen: duplicate_ids.append(match.group(1))
            seen.add(match.group(1))
    return {"markdown":len(notes),"overviews":len(overview),"expected_items":expected_items,"duplicate_stable_ids":duplicate_ids,"broken_links":broken,"valid":len(overview)==expected_items and not duplicate_ids and not broken}


def atomic_switch(current: Path, next_vault: Path) -> None:
    retired=current.with_name(f"{current.name}.retired-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    os.replace(current,retired)
    try: os.replace(next_vault,current)
    except Exception:
        os.replace(retired,current); raise
