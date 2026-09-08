"""Local Done delivery: a resumable caller of existing services, no collector."""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from oa_knowledge.archive import sha256_file
from oa_knowledge.classification.per_item_classifier import CLASSIFIER_VERSION
from oa_knowledge.classification.private_config import load_private_classification_config
from oa_knowledge.config import load_settings
from oa_knowledge.db.models import ArchivedFile, ClassificationDecision, ClassificationRunItem, MarkdownExport, OAItem, OAManifestItem, ParseArtifact, ParseJob
from oa_knowledge.markdown_delivery import _classification_directory, _item_leaf, publish_item_index, require_current_classified_decision
from oa_knowledge.pipeline import ParsePipeline
from oa_knowledge.runtime_paths import resolve_cache_path, resolve_original_path
from oa_knowledge.source_markdown.service import _active_artifact, _destination, publish_active_artifact
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES
from oa_knowledge.web.worker import OperationWorker


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def prepare(settings, root):
    if (root / 'scope.json').exists():
        return
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = root / 'backup'
    backup.mkdir(exist_ok=True)
    with sqlite3.connect(f'file:{settings.database_path}?mode=ro', uri=True) as source:
        with sqlite3.connect(backup / 'oa.db') as target:
            source.backup(target)
        rows = source.execute('SELECT m.oa_item_key,m.processing_status,m.matched_exclusion_keyword,i.id FROM oa_manifest_items m LEFT JOIN oa_items i ON i.oa_item_key=m.oa_item_key ORDER BY m.id').fetchall()
        files = source.execute("SELECT oa_item_id,local_relpath,content_object_id FROM files WHERE download_status='verified'").fetchall()
    if not (backup / 'markdown').exists():
        shutil.copytree(settings.markdown_root, backup / 'markdown', symlinks=True)
    by_item = defaultdict(list)
    for item_id, path, content_id in files:
        by_item[item_id].append((path, content_id))
    buckets = defaultdict(list)
    keys = []
    for key, state, excluded, item_id in rows:
        keys.append(key)
        sources = by_item[item_id]
        if excluded or state != 'downloaded' or not 1 <= len(sources) <= 3:
            continue
        suffix = Path(sources[0][0] or '').suffix.lower()
        buckets[(suffix, bool(sources[0][1]))].append(key)
    first = []
    while len(first) < 20 and any(buckets.values()):
        for bucket in sorted(buckets):
            if buckets[bucket] and len(first) < 20:
                first.append(buckets[bucket].pop(0))
    config = load_private_classification_config(settings.classification_private_dir)
    write_json(root / 'scope.json', {'keys': first + [k for k in keys if k not in first], 'first': first, 'config_sha256': config.config_sha256, 'classifier_version': CLASSIFIER_VERSION, 'database_backup': str(backup / 'oa.db'), 'output_backup': str(backup / 'markdown')})
    print(json.dumps({'prepared': len(keys), 'first': len(first), 'backup': str(backup)}, ensure_ascii=False), flush=True)


def current_ledger(root):
    result = {}
    if (root / 'ledger.jsonl').exists():
        for line in (root / 'ledger.jsonl').read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            previous = result.get(row['manifest_id'], {})
            row['created_this_run'] = previous.get('created_this_run', False) or (row.get('status') == 'complete' and not row.get('reused', False))
            result[row['manifest_id']] = row
    return result


def summarize(root):
    scope = json.loads((root / 'scope.json').read_text())
    rows = current_ledger(root)
    counts = Counter(row['status'] for row in rows.values())
    summary = {'scope_done_items': len(scope['keys']), 'excluded': counts['excluded'], 'complete_new_or_updated': sum(x['status']=='complete' and x.get('created_this_run', False) for x in rows.values()), 'complete_reused': sum(x['status']=='complete' and not x.get('created_this_run', False) for x in rows.values()), 'partial': counts['partial'], 'final_needs_review': counts['needs_review'], 'failed_or_missing': counts['failed'], 'awaiting_evidence': counts['awaiting_evidence'], 'not_processed': len(scope['keys'])-len(rows), 'attachment_markdown': sum(x.get('attachments', 0) for x in rows.values()), 'item_indexes': sum(x.get('indexes', 0) for x in rows.values()), 'first_processed': sum(k in rows for k in scope['first'])}
    missing, evidence = Counter(), Counter()
    for row in rows.values():
        if row['status'] != 'needs_review':
            continue
        reason = row.get('reason') or {}
        if isinstance(reason, dict):
            missing[reason.get('review_reason') or 'unspecified'] += 1
            step = reason.get('evidence_step')
            code = (step.get('rejection_code') if isinstance(step, dict) else None) or reason.get('evidence_step_code')
            evidence[code or 'no_recorded_evidence_failure'] += 1
    summary.update(updated_at=datetime.now(timezone.utc).isoformat(), processed=len(rows), excluded_scope='scanned_this_run', review_missing_fields=dict(missing), review_evidence_failures=dict(evidence))
    write_json(root / 'summary.json', summary)
    fields = ['manifest_id', 'original_relpaths', 'classification', 'status', 'markdown_relpaths', 'reason', 'reused', 'attachments', 'indexes']
    with (root / 'ledger.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows.values():
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k,v in row.items() if k in fields})
    return summary


def protect_output(session, settings, destination, root):
    try:
        destination.resolve().relative_to(settings.markdown_root.resolve())
    except ValueError as exc:
        raise RuntimeError('STOP:output_outside_markdown_root') from exc
    if not destination.exists():
        return
    rel = destination.relative_to(settings.markdown_root).as_posix()
    record = session.scalar(select(MarkdownExport).where(MarkdownExport.markdown_relpath == rel))
    if record is None or record.markdown_sha256 != sha256_file(destination):
        raise ValueError('unowned_or_manually_changed_output:' + rel)
    saved = root / 'prior-managed-output' / rel
    if not saved.exists():
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, saved)
    assets = destination.with_name(destination.stem + '.assets')
    saved_assets = saved.with_name(saved.stem + '.assets')
    if assets.exists() and not saved_assets.exists():
        shutil.copytree(assets, saved_assets, symlinks=True)


def semantic_evidence(worker, key, root):
    from oa_knowledge.classification.agnes_eligibility import AgnesEligibility
    from oa_knowledge.classification.semantic_classifier import SemanticClassifier, JsonSemanticCache
    from oa_knowledge.classification.semantic_package_loader import DatabaseSemanticPackageLoader
    from oa_knowledge.classification.semantic_run import SemanticReviewService
    from oa_knowledge.enrich.provider import make_llm_client

    settings = worker.settings
    factory = sessionmaker(worker.engine, expire_on_commit=False)
    loader = DatabaseSemanticPackageLoader(factory, settings)
    client = make_llm_client(settings.llm, max_retries=0)
    classifier = SemanticClassifier(client, client, JsonSemanticCache(root / 'local-model-cache'), prompt_version='agnes-classifier-v1.1', local_model=settings.llm.model)
    def classify(package, eligibility):
        result = classifier.classify(package, AgnesEligibility('local_only', 'local_delivery_only'))
        known = package.current_classification.get('content_origin')
        if result.outcome is not None and known and result.outcome.content_origin != known:
            result = replace(result, outcome=None, rejection_code='determined_origin_conflict')
        return result
    service = SemanticReviewService(factory, SimpleNamespace(classify=classify), loader)
    with factory() as session:
        current = session.scalar(select(ClassificationDecision).where(ClassificationDecision.oa_item_key==key, ClassificationDecision.is_current.is_(True)))
        run_id = 'local-md-' + hashlib.sha256(f'{key}:{current.id}'.encode()).hexdigest()[:48]
    config = load_private_classification_config(settings.classification_private_dir)
    service.create_run(run_id, (key,), private_config_sha256=config.config_sha256)
    service.recover_interrupted(run_id)
    progress = service.process_next(run_id, limit=1)
    if progress.failed:
        raise RuntimeError('local_semantic_step_failed')
    with factory() as session:
        current = session.scalar(select(ClassificationDecision).where(ClassificationDecision.oa_item_key==key, ClassificationDecision.is_current.is_(True)))
        from oa_knowledge.classification.service import ClassificationService
        ClassificationService._mirror_compatibility(session, key, current)
        session.commit()
        return current.classification_status


def parse_missing(worker, source):
    from oa_knowledge.classification.parse_cache import ParseCacheService, ParseRequest
    from oa_knowledge.parsers.format_router import detect_format, parser_attempts
    from oa_knowledge.parsers.router import resolve_parser_version

    settings = worker.settings
    route = detect_format(resolve_original_path(settings, source.local_relpath))
    cache = ParseCacheService(sessionmaker(worker.engine, expire_on_commit=False), settings)
    engines = ('libreoffice',) if route.actual_file_type in {'doc','docx'} and settings.mineru.enabled else parser_attempts(route, mineru_enabled=settings.mineru.enabled)[:2]
    for engine in engines:
        profile = 'word-pdf-ocr-v3' if engine == 'libreoffice' and route.actual_file_type in {'doc','docx'} else 'local-delivery-v2'
        request = ParseRequest(file_id=source.id, content_sha256=source.sha256, parser_name=engine, parser_version=resolve_parser_version(engine, settings), parse_profile_version=profile, parse_config_sha256=hashlib.sha256(json.dumps({'parser':settings.parser.model_dump(mode='json'), 'mineru':settings.mineru.model_dump(mode='json'), 'engine':engine},sort_keys=True).encode()).hexdigest(), metadata_unresolved=False, purpose='candidate_markdown')
        result = cache.get_or_parse(request)
        if result.status == 'parsed':
            return result.artifact_id
    raise ValueError('normal_parser_could_not_produce_full_text')


def process(worker, key, root):
    settings = worker.settings
    row = {'manifest_id': key, 'status': 'failed', 'reason': '', 'original_relpaths': [], 'markdown_relpaths': [], 'classification': {}, 'attachments': 0, 'indexes': 0}
    with Session(worker.engine) as session:
        manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == key))
        if manifest.processing_status == 'skipped' or manifest.matched_exclusion_keyword:
            return {**row, 'status':'excluded'}
        item = session.scalar(select(OAItem).where(OAItem.oa_item_key == key, OAItem.source_channel == 'done'))
        if item is None or not item.archive_relpath:
            return {**row, 'reason':'missing_done_archive'}
        files = list(session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id, ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES)).order_by(ArchivedFile.id)))
        if not files and not manifest.no_attachment_confirmed:
            return {**row, 'reason':'attachment_inventory_not_confirmed'}
        for source in files:
            row['original_relpaths'].append(source.local_relpath)
            if not source.local_relpath or 'pending' in Path(source.local_relpath).parts:
                return {**row, 'reason':'invalid_or_pending_source'}
            path = resolve_original_path(settings, source.local_relpath)
            if source.download_status != 'verified' or not path.is_file():
                return {**row, 'reason':'missing_or_unverified_original'}
            if not source.sha256 or sha256_file(path) != source.sha256:
                return {**row, 'reason': 'original_hash_mismatch'}
        prior = current_ledger(root).get(key, {})
        current = session.scalar(select(ClassificationDecision).where(ClassificationDecision.oa_item_key==key, ClassificationDecision.is_current.is_(True)))
        metadata_sha = hashlib.sha256(json.dumps([item.title,item.sender,item.document_number,[(f.id,f.sha256)for f in files]],ensure_ascii=False).encode()).hexdigest()
        if prior.get('status') == 'complete' and prior.get('metadata_sha') == metadata_sha and current and prior.get('decision_id') == current.id:
            exports = list(session.scalars(select(MarkdownExport).where(MarkdownExport.oa_item_id==item.id)))
            if len(exports) == len(files)+1 and all(x.status=='success' and (settings.markdown_root/x.markdown_relpath).is_file() and sha256_file(settings.markdown_root/x.markdown_relpath)==x.markdown_sha256 for x in exports):
                return {**prior, 'reused':True}
        row['metadata_sha'] = metadata_sha
    status = worker._classify_archived_item(key)
    if status == 'needs_review':
        write_json(root / 'current.json', {'manifest_id':key,'pid':os.getpid(),'stage':'local_evidence_and_qwen'})
        status = semantic_evidence(worker, key, root)
    with Session(worker.engine) as session:
        decision = session.scalar(select(ClassificationDecision).where(ClassificationDecision.oa_item_key == key, ClassificationDecision.is_current.is_(True)))
        row['classification'] = {'status':status,'origin':decision.content_origin,'category':decision.business_category,'issuer':decision.canonical_issuer}
        row['decision_id'] = decision.id
        if status != 'classified':
            reason = json.loads(decision.classification_reason_json or '{}')
            audit = session.scalar(select(ClassificationRunItem).where(ClassificationRunItem.oa_item_key==key, ClassificationRunItem.last_error_detail.is_not(None)).order_by(ClassificationRunItem.id.desc()).limit(1))
            if audit:
                reason['evidence_step_code'] = audit.last_error_code
                try:
                    reason['evidence_step'] = json.loads(audit.last_error_detail)
                except json.JSONDecodeError:
                    reason['evidence_step'] = audit.last_error_detail
                if 'model_request_failed' in audit.last_error_detail or audit.last_error_code == 'semantic_attempt_failed':
                    return {**row, 'status':'failed', 'reason':reason}
            # Do not label an unperformed model escalation as final human review.
            return {**row, 'status':'needs_review', 'reason':reason}
        require_current_classified_decision(session, key)
        item = session.get(OAItem, item.id)
        destinations = [_destination(settings, source, item) for source in files]
        destinations.append(settings.markdown_root / _classification_directory(item) / _item_leaf(item) / '_index.md')
        for destination in destinations:
            protect_output(session, settings, destination, root)
        before = [(p.as_posix(), sha256_file(p)) for p in destinations if p.is_file()]
    failures = []
    pipeline = ParsePipeline(settings, worker.engine)
    for source in files:
        try:
            from oa_knowledge.markdown_export.publisher import IMAGE_LINK
            with Session(worker.engine) as session:
                fresh = session.get(ArchivedFile, source.id)
                existing_artifact = _active_artifact(session, fresh)
                regenerate = False
                cached_body = ''
                if existing_artifact:
                    cached_path = resolve_cache_path(settings, existing_artifact.output_relpath)
                    if cached_path.is_file():
                        cached_body = cached_path.read_text(encoding='utf-8',errors='replace')
                        regenerate = any(not link.startswith('data:') and not (cached_path.parent/link).is_file() for link in IMAGE_LINK.findall(cached_body))
                from oa_knowledge.parsers.libreoffice_parser import needs_word_pdf_ocr
                regenerate = regenerate or needs_word_pdf_ocr(resolve_original_path(settings, source.local_relpath), existing_artifact.engine if existing_artifact else None, cached_body)
            if regenerate:
                artifact_id = parse_missing(worker, source)
                from oa_knowledge.db.models import ContentObject
                with Session(worker.engine) as session:
                    fresh = session.get(ArchivedFile, source.id)
                    content = session.get(ContentObject, fresh.content_object_id)
                    content.active_parse_artifact_id = artifact_id
                    session.commit()
            job_id = pipeline.enqueue(source.id)
            if job_id is not None:
                with Session(worker.engine) as session:
                    job = session.get(ParseJob, job_id)
                    state = job.status
                if state == 'queued':
                    pipeline.run(job_id)
                elif state not in ('completed',):
                    if state == 'running':
                        raise ValueError('existing_parse_job_running_not_reset')
                    parse_missing(worker, source)
            else:
                parse_missing(worker, source)
            with Session(worker.engine) as session:
                fresh_source = session.get(ArchivedFile, source.id)
                artifact = _active_artifact(session, fresh_source)
                if artifact is None or artifact.content_object_id != fresh_source.content_object_id or artifact.source_sha256 != fresh_source.sha256:
                    raise ValueError('no_valid_full_parse_artifact')
                product = resolve_cache_path(settings, artifact.output_relpath)
                if not artifact.product_sha256 or sha256_file(product) != artifact.product_sha256:
                    raise ValueError('parse_product_hash_mismatch')
                body = product.read_text(encoding='utf-8', errors='strict')
                if len(body.strip()) < 40 or '[中间内容已截断]' in body:
                    raise ValueError('empty_or_truncated_parse_product')
                export = publish_active_artifact(session, settings, source.id)
                rendered = (settings.markdown_root / export.markdown_relpath).read_text(encoding='utf-8')
                if '[图片未包含在解析结果中]' in rendered or '[图片未嵌入：' in rendered:
                    export.status = 'failed'
                    export.last_error_code = 'MISSING_IMAGE_ASSET'
                    export.last_error = 'Full text preserved; parser image assets unavailable.'
                    failures.append({'file_id':source.id,'reason':'MISSING_IMAGE_ASSET'})
                session.commit()
                row['markdown_relpaths'].append(export.markdown_relpath)
                row['attachments'] += 1
        except Exception as exc:
            failures.append({'file_id':source.id,'reason':str(exc)[:500]})
    with Session(worker.engine) as session:
        destination = publish_item_index(session, settings, key)
        session.commit()
        row['markdown_relpaths'].append(destination.relative_to(settings.markdown_root).as_posix())
        row['indexes'] = 1
    for source in files:
        if sha256_file(resolve_original_path(settings, source.local_relpath)) != source.sha256:
            raise RuntimeError('STOP:original_changed_during_delivery')
    row['status'] = 'partial' if failures else 'complete'
    row['reason'] = failures
    after = [(p.as_posix(), sha256_file(p)) for p in destinations if p.is_file()]
    row['reused'] = before == after and not failures
    return row


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('command', choices=['prepare','run','status'])
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--repeat-first', action='store_true')
    args = parser.parse_args()
    settings = load_settings(Path('config.yaml'))
    assert settings.data_root.resolve() == Path('/data/Projects/OARadar/data')
    assert settings.markdown_root.resolve() == Path('/data/Projects/OARadar/data/markdown')
    root = args.run_dir.resolve()
    root.relative_to(settings.data_root.resolve())
    if args.command == 'prepare':
        prepare(settings, root)
        return
    if args.command == 'status':
        print(json.dumps(summarize(root), ensure_ascii=False))
        return
    scope = json.loads((root / 'scope.json').read_text())
    private = load_private_classification_config(settings.classification_private_dir)
    if private.config_sha256 != scope['config_sha256'] or CLASSIFIER_VERSION != scope['classifier_version']:
        raise RuntimeError('STOP:configuration_or_classifier_changed')
    with (root / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = current_ledger(root)
        keys = scope['first'] if args.repeat_first else [k for k in scope['keys'] if k not in previous]
        worker = OperationWorker(settings)
        try:
            for key in keys[:args.limit]:
                write_json(root / 'current.json', {'manifest_id':key,'pid':os.getpid(),'stage':'processing'})
                try:
                    row = process(worker, key, root)
                except Exception as exc:
                    if str(exc).startswith('STOP:') or type(exc).__name__ == 'PrivateConfigError':
                        raise
                    row = {'manifest_id':key,'status':'failed','reason':str(exc)[:800]}
                with (root / 'ledger.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                print(json.dumps({'manifest_id':key,'status':row['status'],'attachments':row.get('attachments',0)},ensure_ascii=False), flush=True)
                summarize(root)
        finally:
            worker.close()
        write_json(root / 'current.json', {'stage':'batch_finished','pid':os.getpid()})
        print(json.dumps(summarize(root), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
