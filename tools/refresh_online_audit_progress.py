"""Publish durable audit counters without starting a new audit."""
import json
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import OnlineAuditRun

settings = load_settings(Path(__file__).resolve().parents[1] / 'config.yaml')
engine = create_db_engine(settings.database_path)
try:
    with Session(engine) as session:
        run = session.scalar(select(OnlineAuditRun).order_by(OnlineAuditRun.id.desc()).limit(1))
        if run:
            result = {key: getattr(run, key) for key in (
                'id', 'status', 'total_items', 'completed_items', 'matched_items',
                'mismatch_items', 'access_failed_items')}
            job = run.job_id
            from oa_knowledge.db.models import OperationJob
            operation = session.get(OperationJob, job) if job else None
            inventory = bool(json.loads(operation.parameters_json or '{}').get('inventory_only')) if operation else False
            result['verification_mode'] = 'online_detail_inventory_not_file_bytes' if inventory else 'content'
            path = settings.runtime_root / 'online-inventory-audit-progress.json'
            temporary = path.with_suffix('.watchdog.tmp')
            temporary.write_text(json.dumps(result, indent=2), encoding='utf8')
            temporary.replace(path)
            print(json.dumps(result))
finally:
    engine.dispose()
