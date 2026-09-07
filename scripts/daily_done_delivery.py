"""Daily Done-only caller of the existing scan, archive and local delivery services."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import threading

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from oa_knowledge.classification.private_config import load_private_classification_config
from oa_knowledge.config import load_settings
from oa_knowledge.db.models import OAManifestItem, PipelineTask
from oa_knowledge.scheduled_sync import run_nightly_scan
from oa_knowledge.web.worker import OperationWorker
from scripts.local_done_markdown import process, write_json


def protected_keys(bulk_root: Path) -> set[str]:
    """Fail closed if the explicitly selected running batch cannot be read."""
    summary = json.loads((bulk_root / 'summary.json').read_text())
    if type(summary['not_processed']) is not int or summary['not_processed'] < 0:
        raise ValueError('invalid bulk progress')
    if summary['not_processed'] == 0:
        return set()
    return set(json.loads((bulk_root / 'scope.json').read_text())['keys'])


@contextmanager
def lock_file(path: Path, *, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def drain(worker, root: Path, bulk_root: Path, queue_name: str) -> int:
    """Archive first; GPU/publication waits for the existing batch lock."""
    with Session(worker.engine) as session:
        rows = session.execute(select(PipelineTask.id, PipelineTask.logical_item_key).join(
            OAManifestItem, OAManifestItem.oa_item_key == PipelineTask.logical_item_key,
        ).where(
            PipelineTask.queue_name == queue_name, PipelineTask.status.in_(('queued', 'running')),
            OAManifestItem.processing_status != 'skipped',
            or_(OAManifestItem.matched_exclusion_keyword.is_(None), OAManifestItem.matched_exclusion_keyword == ''),
            PipelineTask.stage.in_(('done_capture_and_archive', 'archive_verify') if queue_name == 'realtime_done'
                                   else ('attachment_inventory', 'parse', 'classify', 'source_publish', 'index_publish')),
        )).all()
    protected = protected_keys(bulk_root)
    targets = tuple(task_id for task_id, key in rows if key not in protected)
    # The caller holds operation-worker.lock: previous owners cannot still dispatch.
    worker.production_queue.recover_abandoned(lambda owner: owner == worker.owner, task_ids=targets)
    completed = 0
    while targets:
        if queue_name == 'markdown_delivery':
            write_json(root / 'current.json', {'stage': 'waiting_for_batch_boundary'})
        # Downloads concern only non-bulk items. MD uses exactly the existing batch lock.
        path = bulk_root / 'run.lock' if queue_name == 'markdown_delivery' else root / 'download.lock'
        with lock_file(path):
            task = worker.production_queue.claim(worker.owner, queue_names=(queue_name,), task_ids=targets)
            if task is None:
                break
            if queue_name == 'realtime_done':
                worker._execute_pipeline_task(task)
                with Session(worker.engine) as session:
                    state = session.get(PipelineTask, task.id)
                    if state.status != 'queued' or state.error_code:
                        targets = tuple(x for x in targets if x != task.id)
                completed += 1
                continue
            stop = threading.Event()
            def heartbeat():
                while not stop.wait(30):
                    worker.production_queue.heartbeat(task.id, worker.owner)
            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            try:
                write_json(root / 'current.json', {'stage': 'processing', 'manifest_id': task.logical_item_key})
                row = process(worker, task.logical_item_key, root)
                with (root / 'ledger.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n'); stream.flush(); os.fsync(stream.fileno())
                if row['status'] in {'failed', 'partial'}:
                    code = 'DAILY_DELIVERY_PARTIAL' if row['status'] == 'partial' else 'DAILY_DELIVERY_FAILED'
                    worker.production_queue.fail(task.id, worker.owner, code, 'See daily local ledger', recoverable=False)
                else:
                    worker.production_queue.complete(task.id, worker.owner)
                completed += 1
            except Exception as exc:
                worker.production_queue.fail(task.id, worker.owner, 'DAILY_DELIVERY_ERROR', type(exc).__name__, recoverable=False)
                with (root / 'ledger.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'manifest_id': task.logical_item_key, 'status': 'failed',
                                             'reason': type(exc).__name__}, ensure_ascii=False) + '\n')
                    stream.flush(); os.fsync(stream.fileno())
                if str(exc).startswith('STOP:') or type(exc).__name__ == 'PrivateConfigError':
                    raise
            finally:
                stop.set(); thread.join(timeout=2)
            targets = tuple(x for x in targets if x != task.id)
    return completed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--bulk-run-dir', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='Validate local configuration only; no OA or database writes')
    args = parser.parse_args()
    settings = load_settings(args.config)
    private = load_private_classification_config(settings.classification_private_dir)
    protected = protected_keys(args.bulk_run_dir)
    if args.check:
        print(json.dumps({'config_sha256': private.config_sha256, 'protected_bulk_items': len(protected),
                          'markdown_root': str(settings.markdown_root)}, ensure_ascii=False))
        return
    root = settings.data_root / 'runs' / 'daily-done' / datetime.now().strftime('%Y%m%d')
    root.mkdir(parents=True, exist_ok=True)
    # Existing OA-worker lock prevents any second browser-owning dispatcher.
    with lock_file(settings.runtime_root / 'operation-worker.lock', blocking=False):
        worker = OperationWorker(settings, args.config)
        summary = {'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running'}
        write_json(root / 'summary.json', summary)
        try:
            summary['scan'] = run_nightly_scan(worker.engine, settings, enqueue_history=False)
            summary['archive_steps'] = drain(worker, root, args.bulk_run_dir, 'realtime_done')
            summary['delivery_items'] = drain(worker, root, args.bulk_run_dir, 'markdown_delivery')
            summary['status'] = 'finished'
        except Exception as exc:
            summary.update(status='failed', error=type(exc).__name__)
            raise
        finally:
            with Session(worker.engine) as session:
                summary['remaining_task_states'] = dict(Counter(session.scalars(select(PipelineTask.status).where(
                    PipelineTask.queue_name.in_(('realtime_done', 'markdown_delivery')),
                    PipelineTask.status != 'completed',
                ))))
            summary['updated_at'] = datetime.now(timezone.utc).isoformat()
            write_json(root / 'summary.json', summary)
            worker.engine.dispose()


if __name__ == '__main__':
    main()
