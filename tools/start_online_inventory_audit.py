"""Refresh OA lists, then launch and monitor a durable per-detail online audit."""
from pathlib import Path
from time import sleep, monotonic
import json

from sqlalchemy.orm import Session
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import OperationJob, OnlineAuditRun
from oa_knowledge.done_collection import start_collection
from oa_knowledge.online_audit import start_audit


def main():
    settings = load_settings(Path('config.yaml'))
    engine = create_db_engine(settings.database_path)
    try:
        job_id = start_collection(settings, 'done_inventory')['id']
        deadline = monotonic() + 3600
        while True:
            with Session(engine) as db:
                job = db.get(OperationJob, job_id)
                progress = {'stage': 'refresh_manifest', 'job_id': job_id,
                                  'status': job.status, 'current': job.progress_current,
                                  'total': job.progress_total}
                (settings.runtime_root / 'online-inventory-audit-progress.json').write_text(json.dumps(progress, indent=2), encoding='utf8')
                print(json.dumps(progress), flush=True)
                status = job.status
            if status == 'completed':
                break
            if status in {'auth_required', 'failed', 'paused', 'cancelled'} or monotonic() >= deadline:
                raise RuntimeError('Manifest scan requires attention; existing checkpoint retained')
            sleep(20)
        run_id = start_audit(settings, inventory_only=True)['run_id']
        while True:
            with Session(engine) as db:
                run = db.get(OnlineAuditRun, run_id)
                summary = {key: getattr(run, key) for key in ['id', 'status', 'total_items', 'completed_items', 'matched_items', 'mismatch_items', 'access_failed_items']}
            summary['verification_mode'] = 'online_detail_inventory_not_file_bytes'
            (settings.runtime_root / 'online-inventory-audit-progress.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf8')
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            if summary['status'] in {'completed', 'failed', 'paused'}:
                break
            sleep(20)
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
