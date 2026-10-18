"""Five-source library decisions enriched only from verified local artifacts."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import select, func
from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.archive_classification import ArchiveRules, CATEGORIES, CLASSIFIER_REVISION
from oa_knowledge.db.models import ArchiveClassificationPlan, ArchiveClassificationResult, ArchivedFile, OAItem
from oa_knowledge.runtime_paths import resolve_cache_path
from oa_knowledge.title_screening import safe_segment, title_hash


def attachment_only_library(session):
    plan = session.scalar(select(ArchiveClassificationPlan).where(ArchiveClassificationPlan.active.is_(True)))
    return bool(plan and json.loads(plan.spec_json).get('delivery', {}).get('attachments_only'))


def _readable_leaf(row):
    date = row.initiated_at.strftime('%Y-%m-%d') if row.initiated_at else '日期待确认'
    # Retain punctuation and the original wording; replace Windows-invalid
    # characters only. Full titles and textual OA IDs remain in metadata.
    return date + '_' + safe_segment(row.title, 100)


def item_directory_name(session, row):
    from oa_knowledge.db.models import OAManifestItem
    day = row.initiated_at.strftime('%Y-%m-%d') if row.initiated_at else None
    condition = func.date(OAManifestItem.initiated_at) == day if day else OAManifestItem.initiated_at.is_(None)
    peers = session.scalars(select(OAManifestItem).where(condition).order_by(OAManifestItem.id)).all()
    used = set()
    for peer in peers:
        base = _readable_leaf(peer)
        leaf, number = base, 1
        while leaf.casefold() in used:
            number += 1
            leaf = f'{base}_{number}'
        used.add(leaf.casefold())
        if peer.oa_item_key == row.oa_item_key:
            return leaf
    return _readable_leaf(row)


def _external_evidence(rules, title, filenames, bodies):
    """Recognize explicit government headings/signatures, never body mentions."""
    organization = re.compile(r'(?:中共)?(?:广州市黄埔区|广州开发区|黄埔区|广州市|广东省)[\u4e00-\u9fff]{0,25}?(?:委员会办公室|委员会|办公室|组织部|总工会|人民政府|研究室|中心|局|厅)')
    found = []
    def valid(name):
        return not re.search(r'公司|集团|股份',name) and len(re.findall(r'广州市黄埔区|广州开发区|黄埔区|广州市|广东省',name))==1
    def heading(value):
        value = re.sub(r'^[#*\s]+','',value).strip()
        if re.match(r'根据|按照|依据|引用|参照|抄送|主送|致[:：]',value):return
        match = organization.match(value)
        if match and valid(match.group()) and re.match(r'\s*(?:关于|文件|通知|公告|函|[、与和及]|$)',value[match.end():]):
            found.append((match.group(),False))
    heading(rules.normalize(title))
    for name in filenames:heading(name)
    for body in bodies:
        lines=[re.sub(r'^[#*\s]+','',line).strip() for line in body.splitlines()
               if line.strip() and not re.match(r'\s*(?:!\[|<|https?://)',line)]
        for line in lines[:16]:heading(line)
        for n,line in enumerate(lines):
            explicit=re.match(r'(?:发文单位|发文机关|文件落款|落款单位)\s*[:：]\s*(.+)',line)
            name=explicit.group(1) if explicit else line
            if organization.fullmatch(name) and valid(name) and (explicit or re.search(r'(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月',''.join(lines[n+1:n+5]))):
                found.append((name,True))
    return found


def _corporate_evidence(rules,title,bodies):
    found={}
    def match_name(name):
        name=re.sub(r'^[#*\s]+','',name).strip()
        name=re.sub(r'^中共','',name)
        for category,record in rules.records:
            if category not in {'SELF','GROUP'}:continue
            match=re.match(record['issuer_name_pattern'],name)
            if match and re.fullmatch(r'(?:党委|党支部|纪委|纪律检查委员会|工会委员会|工会|委员会|办公室)*',name[match.end():]):
                return category,record
    clean=rules.normalize(title)
    title_head=re.split(r'关于|文件|通知|公告',clean,maxsplit=1)[0]
    if title_head!=clean:
        hit=match_name(title_head)
        if hit:found[hit[1]['id']]=hit
    # A company named as the owner of a board resolution or management
    # document is the source; a company merely mentioned as a counterparty is not.
    document_suffix=r'(?:第[一二三四五六七八九十百0-9]+届)?(?:第[一二三四五六七八九十百0-9]+次)?(?:董事会|监事会|办公会|专题会议纪要|业务情况报告|工作报告|工会费用支出单)'
    def document_heading(line):
        line=re.sub(r'^[#*\s]+','',line).strip()
        for category,record in rules.records:
            if category not in {'SELF','GROUP'}:continue
            match=re.match(record['issuer_name_pattern'],line)
            if match and re.match(document_suffix,line[match.end():].lstrip()):
                found[record['id']]=(category,record)
    document_heading(clean)
    for body in bodies:
        lines=[line.strip()for line in body.splitlines()if line.strip() and not re.match(r'\s*(?:!\[|<|https?://)',line)]
        for line in lines[:12]:document_heading(line)
        for n,line in enumerate(lines):
            explicit=re.match(r'(?:发文单位|发文机关|文件落款|落款单位)\s*[:：]\s*(.+)',line)
            hit=match_name(explicit.group(1) if explicit else line)
            if hit and (explicit or re.search(r'(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月',''.join(lines[n+1:n+5]))):
                found[hit[1]['id']]=hit
    return list(found.values())


def _external_location(result, spec):
    names=list(dict.fromkeys([name for name in [result.get('archive_organization'),*result.get('source_organizations',[])] if name]))
    regions=set();labels=[]
    aliases=spec.get('external_routing',{}).get('agency_labels',{})
    for name in names:
        if re.search(r'黄埔|广州开发区',name):regions.add('黄埔区')
        elif re.search(r'广州市|广州',name):regions.add('广州市')
        elif '广东省' in name:regions.add('广东省')
        else:regions.add('其他机构')
        label=aliases.get(name)
        if not label:
            label=re.sub(r'^(?:中共)?(?:广州市黄埔区|广州开发区|黄埔区|广州市|广东省)','',name)
            if label.startswith('委'):label='区'+label
        labels.append(label)
    region=next(iter(regions)) if len(regions)==1 else '其他机构'
    labels=list(dict.fromkeys(labels))
    agency=labels[0] if len(labels)==1 else '联合发文' if labels else '机构待确认'
    return region,agency


def classify_library_item(session, settings, row, action="download", *, plan=None):
    plan = plan or session.scalar(select(ArchiveClassificationPlan).where(ArchiveClassificationPlan.active.is_(True)))
    if plan is None:
        raise ValueError("active five-source classification plan is required")
    rules = ArchiveRules(json.loads(plan.spec_json))
    if row.processing_status == 'skipped' or row.matched_exclusion_keyword:
        action = 'skip'
    if action != 'download':
        # Screening ends the pipeline before reading filenames or bodies.
        return {**rules.classify(row,action), 'library_revision':CLASSIFIER_REVISION}
    item = session.scalar(select(OAItem).where(OAItem.oa_item_key == row.oa_item_key))
    files = list(session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id).order_by(ArchivedFile.id))) if item else []
    evidence, signatures, filenames = [], [], []
    from oa_knowledge.source_markdown.service import _active_artifact
    for source in files:
        filenames.append(source.original_name or "")
        artifact = _active_artifact(session, source)
        signatures.append((source.id, source.sha256, source.download_status,
                           artifact.id if artifact else None, artifact.product_sha256 if artifact else None))
        if artifact is None or source.download_status != "verified" or artifact.source_sha256 != source.sha256:
            continue
        path = resolve_cache_path(settings, artifact.output_relpath)
        if path.is_file() and sha256_file(path) == artifact.product_sha256:
            evidence.append(path.read_text(encoding="utf-8"))
    metadata = title_hash(json.dumps([row.title, str(row.initiated_at), str(row.completed_at), action], ensure_ascii=False))
    folder_leaf = item_directory_name(session,row)
    digest = title_hash(json.dumps([CLASSIFIER_REVISION, metadata, signatures, plan.source_sha256, folder_leaf], ensure_ascii=False))
    cached = session.scalar(select(ArchiveClassificationResult).where(
        ArchiveClassificationResult.plan_id == plan.id, ArchiveClassificationResult.oa_item_key == row.oa_item_key,
        ArchiveClassificationResult.input_sha256 == digest))
    if cached:
        return json.loads(cached.result_json)

    issuers = []
    for body in evidence:
        issuers.extend(rules.body_issuers(body))
        # A known organization alone on a closing line, followed by a date,
        # is a signature. Mentions in paragraphs and OA senders are not.
        # Annexes may follow the signature. Ignore image/seal markup, and
        # inspect standalone known issuer lines throughout the main product.
        lines = [line for line in body.splitlines() if line.strip() and not re.match(r'\s*(?:!\[|<|https?://)',line)]
        for position, line in enumerate(lines):
            name = re.sub(r"[\s#*]", "", line).rstrip('：:')
            if rules.issuer_matches([name]) and re.search(r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月", "".join(lines[position+1:position+5])):
                issuers.append(name)
    result = rules.classify(row, action, issuer_names=list(dict.fromkeys(issuers)))
    own = any(pattern.search(rules.normalize(row.title)) for pattern in rules.workflow)
    if (result['archive_category_id']=='UNKNOWN' and not own
            and not _corporate_evidence(rules,row.title,[])
            and not re.search(r'董事会|监事会|办公会|内部事项|业务交接|我司',row.title)):
        headings=list(filenames)
        for text in evidence:
            headings.extend(re.sub(r'^[#*\s]+','',line).strip()for line in text.splitlines()[:12])
        primary=[hit for heading in headings if (hit:=rules.primary_group_code(heading))]
        identities={record['id']for _,record in primary}
        if len(identities)==1:
            _,record=primary[0]
            result.update(archive_category_id='GROUP',archive_organization=record['name'],archive_series=None,
                matched_mapping_id=record['id'],matched_rule_id='C03',source_organizations=[record['name']],
                source_organization_verified=False,classification_status='classified',needs_review=False,
                classification_source='primary_group_document_code',
                classification_reason='附件自身标题或主文页首具有唯一集团正式文号；参考文件不改变来源')
    # Several verified external co-issuers remain external; their individual
    # names are metadata, not a new project/theme directory.
    if action == "download" and issuers and result['archive_category_id']!='PUBLICATION' and result.get('classification_source')not in {'primary_group_document_code','primary_external_document_code'}:
        matches = [rules.issuer_matches([name]) for name in set(issuers)]
        flattened = [match for group in matches for match in group]
        if all(matches) and len({mapping['id'] for _,mapping in flattened}) > 1 and all(category=='EXTERNAL' for category,_ in flattened):
            result.update(archive_category_id='EXTERNAL',archive_organization=None,archive_bucket='联合来文',
                source_organizations=list(dict.fromkeys(mapping['name'] for _,mapping in flattened)),
                classification_source='body_joint_issuers',classification_status='classified',needs_review=False,
                source_organization_verified=True,
                classification_reason='多个已核实的外部联合发文单位')
    transmitted = bool(re.search(r"文件传阅|转发|传阅|关于(?:印发|发布|实施)", row.title))
    if action == "download" and own and not transmitted and result["archive_category_id"] != "PUBLICATION" and result.get('classification_source')not in {'primary_group_document_code','primary_external_document_code'}:
        organization = rules.spec["mappings"]["self_organization"]
        result.update(archive_category_id="SELF", archive_organization=organization["name"],
            archive_series=None, matched_rule_id="C02", classification_source="self_workflow",
            source_organizations=[organization['name']],source_organization_verified=False,source_evidence_level='workflow',
            classification_reason="本公司办理的业务或经营管理事项", classification_status="classified", needs_review=False)
    elif action == "download" and result["archive_category_id"] == "UNKNOWN" and not issuers:
        # Supplement metadata with each attachment's own heading/document code.
        candidates = []
        for heading in [row.title,*filenames]:
            matched=rules._prefix_matches(rules.normalize(heading))
            if len(matched)>1 and all(category=='EXTERNAL' for category,_ in matched):
                for _,mapping in matched:
                    candidates.append({**result,'archive_category_id':'EXTERNAL','archive_organization':mapping['name'],
                        'archive_series':None,'classification_status':'classified','needs_review':False})
        from types import SimpleNamespace
        body_headings = []
        for text in evidence:
            for line in text.splitlines()[:12]:
                heading = re.sub(r"^[#*\s]+", "", line).strip()
                if re.match(r"(?:根据|按照|依据|引用|参照)", heading):
                    continue
                has_code = any(mapping.get("document_code_pattern") and re.match(
                    r"(?:" + mapping["document_code_pattern"] + r")\s*[\[〔【(]", heading)
                    for _, mapping in rules.records)
                if has_code or rules._prefix_matches(heading):
                    body_headings.append(heading)
        for heading in filenames + body_headings:
            proxy = SimpleNamespace(title=heading, initiated_at=row.initiated_at, completed_at=row.completed_at,
                workitem_id_text=row.workitem_id_text, oa_item_key=row.oa_item_key)
            candidate = rules.classify(proxy, "download")
            if candidate["archive_category_id"] != "UNKNOWN":
                candidates.append(candidate)
        identities = {(x["archive_category_id"], x.get("archive_organization"), x.get("archive_series")) for x in candidates}
        if len(identities) == 1:
            result = {**candidates[0], "original_title": row.title, "classification_source": "attachment_heading_or_code"}
        elif identities and all(identity[0]=='EXTERNAL' for identity in identities):
            result.update(archive_category_id='EXTERNAL',archive_organization=None,archive_bucket='联合来文',
                source_organizations=sorted({identity[1] for identity in identities if identity[1]}),
                classification_source='attachment_joint_codes',classification_status='classified',needs_review=False,
                classification_reason='多个正式外部文号或明确发文标题')
        elif len(identities)>1:
            result.update(classification_reason='多个附件来源冲突',needs_review=True)

    learning=rules.spec.get('learning_materials',{})
    if result['archive_category_id']=='UNKNOWN' and learning.get('title_pattern') and re.search(learning['title_pattern'],row.title):
        # Meeting arrangements and blank sign-in sheets are not study texts.
        confirmed=[text for text in evidence if len(text)>300 and re.search(learning.get('body_pattern',r'(?!)'),text)]
        if confirmed and '冲突' not in result.get('classification_reason',''):
            result.update(archive_category_id='PUBLICATION',archive_organization=None,
                archive_series=learning['series'],source_organizations=[],classification_status='classified',needs_review=False,
                classification_source='learning_title_and_body',classification_reason='明确学习材料标题且附件包含理论学习正文',matched_rule_id='C01')
    routing=rules.spec.get('external_routing',{})
    if result['archive_category_id']=='UNKNOWN' and routing.get('enabled'):
        corporate=_corporate_evidence(rules,row.title,evidence)
        # An explicit company board/document heading can resolve conflicts
        # caused by ancillary reference attachments in the same OA package.
        title_corporate=_corporate_evidence(rules,row.title,[])
        if len(title_corporate)==1:
            corporate=title_corporate
        if len(corporate)==1 and (title_corporate or '冲突' not in result.get('classification_reason','')):
            category,record=corporate[0]
            result.update(archive_category_id=category,archive_organization=record['name'],archive_series=None,
                source_organizations=[record['name']],classification_status='classified',needs_review=False,
                classification_source='corporate_issuer',classification_reason='明确的本公司或集团机构及其党群组织发文',
                matched_rule_id='C02' if category=='SELF' else 'C03')
        elif len(corporate)>1:
            result.update(classification_reason='多个企业发文主体来源冲突',needs_review=True)
    if result['archive_category_id']=='UNKNOWN' and routing.get('enabled'):
        hints=_external_evidence(rules,row.title,filenames,evidence)
        corporate_issuer=any(re.search(r'公司|集团',name)for name in issuers)
        conflict=bool(re.search(r'冲突|多个独立|多个.*系列',result.get('classification_reason','')))
        if hints and not corporate_issuer and not conflict:
            names=list(dict.fromkeys(name for name,_ in hints))
            result.update(archive_category_id='EXTERNAL',archive_organization=names[0] if len(names)==1 else None,
                archive_series=None,source_organizations=names,classification_status='classified',needs_review=False,
                source_organization_verified=any(verified for _,verified in hints),classification_source='external_heading_or_signature',
                classification_reason='正式机构标题或正文落款识别到外部发文机关',matched_rule_id='C04')
        elif ((routing.get('transmission_fallback') and '【文件传阅】' in row.title)
              or (routing.get('original_sender_fallback') and re.search(r'[（(]由[^()（）\r\n]{1,60}原发[）)]\s*$',row.title))) and not corporate_issuer and not conflict:
            result.update(archive_category_id='EXTERNAL',archive_organization=None,archive_series=None,
                source_organizations=[],classification_status='source_pending',needs_review=True,
                source_organization_verified=False,classification_source='transmission_title' if '【文件传阅】'in row.title else'original_sender_title',matched_rule_id='C04',
                classification_reason='文件传阅或原发标记事项优先归外部来文；具体发文机构仍待正文核实')

    category = result["archive_category_id"]
    if category != "EXCLUDED":
        parts = [CATEGORIES[category]]
        if category == "GROUP":
            parts.append(safe_segment(result.get("archive_organization") or result.get("archive_bucket") or "其他机构"))
        elif category == "EXTERNAL":
            region,agency=_external_location(result,rules.spec)
            result['archive_region']=region
            parts.append(region)
            if routing.get('enabled'):
                result['archive_agency']=agency
                parts.append(safe_segment(agency))
        elif category == "PUBLICATION":
            parts.append(safe_segment(result.get("archive_series") or "学习参考"))
        parts.extend([safe_segment(result["archive_year"]), folder_leaf])
        result["virtual_archive_path"] = "/".join(parts)
    result.update(library_revision=CLASSIFIER_REVISION, library_metadata=metadata)
    record = ArchiveClassificationResult(plan_id=plan.id, oa_item_key=row.oa_item_key, input_sha256=digest,
        result_json=json.dumps(result,ensure_ascii=False))
    session.add(record); session.flush()
    return result


def source_directory(decision: dict) -> Path:
    """A source directory ends in initiation date and original OA title."""
    if not decision.get("virtual_archive_path"):
        raise ValueError("excluded items have no active library directory")
    return Path(decision["virtual_archive_path"])


def publish_library_index(session, settings, item, manifest, decision):
    import os
    import yaml
    from datetime import datetime, timezone
    from urllib.parse import quote
    from oa_knowledge.archive.writer import atomic_write_bytes
    from oa_knowledge.db.models import MarkdownExport
    from oa_knowledge.markdown_export.render import SCHEMA_VERSION
    from oa_knowledge.markdown_relocation import retire_rendered_output
    from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES
    files=list(session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id==item.id,
        ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES)).order_by(ArchivedFile.id)))
    exports={record.source_file_id:record for record in session.scalars(select(MarkdownExport).where(
        MarkdownExport.oa_item_id==item.id,MarkdownExport.document_kind=='attachment'))}
    if attachment_only_library(session):
        # Completion is a database fact, independent of a generated index.
        remaining = sum(not (exports.get(source.id) and exports[source.id].status == 'success'
            and (settings.workspace_root / exports[source.id].markdown_relpath).is_file()
            and sha256_file(settings.workspace_root / exports[source.id].markdown_relpath) == exports[source.id].markdown_sha256)
            for source in files)
        if not files and not manifest.no_attachment_confirmed:
            remaining += 1
        if manifest.processing_status not in {'downloaded','no_attachment'}:
            remaining += 1
        return settings.markdown_root / source_directory(decision), remaining
    destination=settings.markdown_root/source_directory(decision)/'_index.md'
    from oa_knowledge.markdown_classification import classify_document
    heading_type=classify_document(item.title)
    tags=set()if heading_type.needs_review else {heading_type.document_type}
    lines=[]; remaining=0
    for source in files:
        exported=exports.get(source.id)
        display=(source.original_name or '').replace('\\','\\\\').replace('[',r'\[').replace(']',r'\]')
        original=settings.data_root/source.local_relpath if source.local_relpath else None
        original_link=quote(os.path.relpath(original,destination.parent).replace(os.sep,'/'),safe='/') if original else None
        if exported and exported.status=='success':
            target=settings.workspace_root/exported.markdown_relpath
            if target.is_file() and sha256_file(target)==exported.markdown_sha256:
                metadata=yaml.safe_load(target.read_text(encoding='utf8')[4:].split('\n---\n',1)[0])
                tags.update(metadata.get('tags',[]))
                link=quote(os.path.relpath(target,destination.parent).replace(os.sep,'/'),safe='/')
                lines.append(f'- [{display}]({link})'+(f' · [原件]({original_link})'if original_link else ''))
                continue
        remaining+=1
        status=exported.status if exported else '待转换'
        lines.append(f'- {display}：{status}'+(f' · [原件]({original_link})'if original_link else ''))
    if not files:
        lines.append('- 无附件（已确认）。'if manifest.no_attachment_confirmed else '- 附件清单尚未确认。')
        remaining+=not manifest.no_attachment_confirmed
    complete=remaining==0 and manifest.processing_status in {'downloaded','no_attachment'}
    metadata={key:decision.get(key)for key in ('archive_category_id','archive_organization','archive_series',
        'archive_region','archive_year','source_organizations','classification_version','classification_source',
        'classification_reason','classification_status','needs_review','virtual_archive_path','library_revision')}
    metadata.update(title=item.title,oa_item_key=str(item.oa_item_key),source_channel='done',managed_by='oaradar',
        tags=sorted(tags),archive_status=manifest.processing_status,delivery_status='complete'if complete else'incomplete',
        initiated_at=item.initiated_at.isoformat()if item.initiated_at else None)
    text='---\n'+yaml.safe_dump(metadata,allow_unicode=True,sort_keys=False).rstrip()+'\n---\n\n# '+item.title+'\n\n## 附件\n\n'+'\n'.join(lines)+'\n'
    relative=destination.relative_to(settings.workspace_root).as_posix()
    record=session.scalar(select(MarkdownExport).where(MarkdownExport.oa_item_id==item.id,
        MarkdownExport.document_kind=='item_index',MarkdownExport.schema_version==SCHEMA_VERSION))
    old_relpath=record.markdown_relpath if record else None
    old_sha256=record.markdown_sha256 if record else None
    if record is None:
        record=MarkdownExport(oa_item_id=item.id,document_kind='item_index',schema_version=SCHEMA_VERSION,
            source_relpath=item.archive_relpath or '',source_sha256=title_hash(item.oa_item_key),
            markdown_relpath=relative,parse_engine='item_index',parse_engine_version=CLASSIFIER_REVISION,
            parse_config_hash=decision['classification_version'])
        session.add(record)
    atomic_write_bytes(text.encode('utf8'),settings.workspace_root,relative)
    record.markdown_relpath=relative;record.markdown_sha256=title_hash(text);record.status='success'
    record.generated_at=datetime.now(timezone.utc)
    if old_relpath and old_sha256:retire_rendered_output(settings,old_relpath,old_sha256,relative,record.markdown_sha256)
    session.flush()
    return destination,remaining
