"""Read-only OA browser jobs in the signed-in Windows desktop session."""
from __future__ import annotations
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
from oa_knowledge.archive.writer import atomic_write_bytes
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import OperationJob
from oa_knowledge.platform_lock import lock_exclusive
from oa_knowledge.web.local_conversion import process_running


def desktop_owner(settings):
    if os.name != 'nt':
        return None
    try:
        state = json.loads((settings.runtime_root / 'desktop-collection.json').read_text(encoding='utf8'))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state['heartbeat_at'])).total_seconds()
        if age < 30 and process_running(int(state['pid'])) is not False:
            return f"worker-{int(state['pid'])}"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def reserve_desktop(settings, job):
    owner = desktop_owner(settings)
    if owner:
        job.lease_owner = owner
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)


def claim_desktop_job(engine, owner, job_types):
    try:
        with Session(engine) as db:
            db.execute(text('BEGIN IMMEDIATE'))
            for job in db.scalars(select(OperationJob).where(OperationJob.job_type.in_(job_types), OperationJob.status.in_(('queued','running'))).order_by(OperationJob.id)):
                previous = job.lease_owner
                ours = previous == owner
                abandoned = previous is None
                if previous and previous.startswith('worker-'):
                    try:abandoned = process_running(int(previous[7:])) is False
                    except ValueError:pass
                if not ours and not abandoned:continue
                if job.status == 'running' and not abandoned:continue
                job.status = 'running'
                job.lease_owner = owner
                job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
                result = job.id, job.job_type
                db.commit()
                return result
    except OperationalError:
        # Retry a busy database without tearing down an authenticated browser.
        return None
    return None


def run_desktop_collection(settings, config_path):
    import oa_knowledge.done_collection as collection
    from oa_knowledge.collector.browser import BrowserSession
    from oa_knowledge.done_collection import JOB_TYPES, run_inventory, run_download, run_login, run_review
    engine = create_db_engine(settings.database_path)
    owner = f'worker-{os.getpid()}'
    worker = SimpleNamespace(settings=settings, config_path=config_path, engine=engine, owner=owner)
    stop = threading.Event()
    browser = None

    class SharedBrowser:
        def __init__(self, current_settings, headed=False):
            self.settings, self.headed = current_settings, headed

        def __enter__(self):
            nonlocal browser
            if browser is not None and (browser.page.is_closed() or (self.headed and not browser.headed)):
                browser.__exit__(None, None, None)
                browser = None
            if browser is None:
                browser = BrowserSession(self.settings, headed=self.headed)
                browser.__enter__()
            return browser

        def __exit__(self, *args):
            # Keep the authenticated browser alive across login and page batches.
            # Some legacy OA servers revoke session cookies on browser exit.
            from oa_knowledge.collector.browser import LoginState
            if browser.login_state() == LoginState.AUTHENTICATED:
                from oa_knowledge.collector.session_cache import save_session
                save_session(self.settings, browser.context)
            return False

    collection.BrowserSession = SharedBrowser

    def heartbeat():
        while not stop.is_set():
            try:
                atomic_write_bytes(json.dumps({'pid': os.getpid(), 'heartbeat_at': datetime.now(timezone.utc).isoformat()}).encode(), settings.runtime_root, 'desktop-collection.json')
                with Session(engine) as db:
                    db.execute(text('BEGIN IMMEDIATE'))
                    for job in db.scalars(select(OperationJob).where(OperationJob.lease_owner == owner, OperationJob.status.in_(('queued', 'running')))):
                        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
                        job.heartbeat_at = datetime.now(timezone.utc)
                        if job.job_type == 'done_download':
                            from oa_knowledge.db.models import OAManifestItem
                            params = json.loads(job.parameters_json)
                            rows = list(db.scalars(select(OAManifestItem).where(OAManifestItem.oa_item_key.in_(params['keys']))))
                            job.progress_current = sum(r.processing_status in {'downloaded','no_attachment','skipped'} for r in rows)
                    db.commit()
            except Exception:
                # Long archive commits can briefly occupy SQLite's writer.
                # An advisory heartbeat must retry rather than kill its thread.
                pass
            stop.wait(5)

    settings.runtime_root.mkdir(parents=True, exist_ok=True)
    with (settings.runtime_root / 'desktop-collection.lock').open('a+') as lock:
        try:
            lock_exclusive(lock.fileno())
        except BlockingIOError:
            engine.dispose()
            return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            while not stop.wait(1):
                claimed = claim_desktop_job(engine, owner, JOB_TYPES)
                if claimed is None:continue
                job_id, kind = claimed
                try:
                    if kind == 'done_download':
                        import oa_knowledge.cli as cli
                        from oa_knowledge.done_collection import _finish_collection
                        from oa_knowledge.db.models import OAManifestItem
                        from oa_knowledge.resources import ResourceCoordinator
                        coordinator = ResourceCoordinator(engine)
                        lease = coordinator.acquire('oa_browser', owner, ttl_seconds=1800, uses_local_gpu=False)
                        if lease is None:
                            _finish_collection(engine, job_id, 'queued')
                            continue
                        cli.BrowserSession = SharedBrowser
                        try:
                            with Session(engine) as db:
                                params = json.loads(db.get(OperationJob, job_id).parameters_json)
                            cli.manifest_download(max_items=max(1, len(params['keys'])), headed=False, config=config_path,
                                error_type=None, item_id=None, item_ids=None, failed_only=False,
                                recheck_no_attachment=False, audit_all=False, collection_job=job_id)
                            with Session(engine) as db:
                                job = db.get(OperationJob, job_id)
                                rows = list(db.scalars(select(OAManifestItem).where(OAManifestItem.oa_item_key.in_(params['keys']))))
                                job.progress_current = sum(r.processing_status in {'downloaded','no_attachment','skipped'} for r in rows)
                                paused = json.loads(job.parameters_json).get('pause_requested')
                                status = 'paused' if paused else 'completed' if job.progress_current == len(rows) else 'failed'
                                db.commit()
                            _finish_collection(engine, job_id, status, None if status == 'completed' else '有未完成附件，可继续重试')
                        except Exception as exc:
                            _finish_collection(engine, job_id, 'auth_required' if getattr(exc, 'exit_code', None) == 3 else 'failed', 'desktop_download_' + type(exc).__name__)
                        finally:
                            coordinator.release(lease, owner)
                    else:
                        {'done_inventory': run_inventory, 'done_login': run_login, 'done_review': run_review}[kind](worker, job_id)
                except Exception as exc:
                    from oa_knowledge.done_collection import _finish_collection
                    _finish_collection(engine, job_id, 'failed', 'desktop_collection_' + type(exc).__name__)
                with Session(engine) as db:
                    job = db.get(OperationJob, job_id)
                    if job.status == 'queued':
                        reserve_desktop(settings, job)
                        db.commit()
                    completed_download = kind == 'done_download' and job.status in {'completed','failed'}
                if completed_download and browser is not None:
                    # An explicit login may show Chrome; release it only after
                    # the batch finishes. The next scheduled batch starts hidden.
                    browser.__exit__(None, None, None)
                    browser = None
        finally:
            stop.set()
            thread.join(timeout=10)
            if browser is not None:
                browser.__exit__(None, None, None)
            engine.dispose()


if __name__ == '__main__':
    import sys
    from pathlib import Path
    from oa_knowledge.config import load_settings
    config = Path(sys.argv[1]).resolve()
    run_desktop_collection(load_settings(config), config)
