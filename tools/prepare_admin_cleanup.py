"""Verify cleanup targets and retain one current SQLite/config recovery point."""
from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone
import json
import re
import shutil
import sqlite3
import sys

from oa_knowledge.config import load_settings
from oa_knowledge.archive.integrity import sha256_file


def main():
    root = Path.cwd().resolve()
    settings = load_settings(root / 'config.yaml')
    if '--record-results' in sys.argv:
        plan = json.loads((settings.runtime_root / 'admin-cleanup-plan.json').read_text(encoding='utf-8-sig'))
        removed = {r['path'] for r in plan.get('results', []) if r['status'] == 'removed' and r['kind'] == 'verified_duplicate_backup'}
        repair_path = settings.runtime_root / 'duplicate-attachment-repair.json'
        if repair_path.exists():
            repair = json.loads(repair_path.read_text(encoding='utf8'))
            if repair.get('summary', {}).get('backup_directory') in removed:
                repair['summary'].update(backup_available=False, backup_deleted_at=plan['completed_at'],
                                         current_recovery_snapshot=plan['current_recovery_snapshot'])
                repair_path.write_text(json.dumps(repair, ensure_ascii=False, indent=2), encoding='utf8')
        print(json.dumps({'duplicate_backup_folders_removed': len(removed)}))
        return
    db = sqlite3.connect(settings.database_path, timeout=30)
    db.row_factory = sqlite3.Row
    if db.execute("SELECT 1 FROM operation_jobs WHERE status IN ('running','queued')").fetchone():
        raise ValueError('An OA operation is active; retry maintenance after it finishes')
    if db.execute("SELECT 1 FROM parse_jobs WHERE status='running'").fetchone():
        raise ValueError('A document parser is active; retry after it finishes')
    if db.execute("SELECT 1 FROM pipeline_tasks WHERE status='running'").fetchone():
        raise ValueError('A processing task is active; retry after it finishes')
    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise ValueError('Database verification failed')
    backup = (settings.runtime_root / 'maintenance-backup/current').resolve()
    backup.mkdir(parents=True, exist_ok=True)
    snapshot = backup / 'database.sqlite3.new'
    if snapshot.exists():
        snapshot.unlink()
    with closing(sqlite3.connect(snapshot)) as saved:
        db.backup(saved)
        if saved.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Recovery snapshot verification failed')
    snapshot.replace(backup / 'database.sqlite3')
    for name in ['config.yaml', '.env']:
        if (root / name).is_file():
            shutil.copy2(root / name, backup / name)
    old = json.loads((settings.runtime_root / 'workspace-cleanup-report.json').read_text(encoding='utf-8-sig'))
    proposed = [Path(e['path']) for e in old['planned']]
    proposed += [Path(e['preserved_path']) for e in old.get('protected_remnant_moves', []) if e['status'] == 'consolidated']
    targets, skips = [], []
    references = set()
    for table in ['parse_jobs', 'parse_artifacts']:
        columns = [r[1] for r in db.execute('PRAGMA table_info(' + table + ')')]
        for col in ['output_relpath', 'markdown_relpath', 'assets_relpath']:
            if col not in columns:
                continue
            for (value,) in db.execute(f'SELECT {col} FROM {table} WHERE {col} IS NOT NULL'):
                parts = Path(value.replace('\\', '/')).parts
                if 'parse' in parts and len(parts) > parts.index('parse') + 1:
                    references.add(parts[parts.index('parse') + 1])
    for path in proposed:
        path = path.resolve()
        if not path.is_relative_to(root) or not path.exists():
            continue
        rel = path.relative_to(root).as_posix()
        if path.is_file() and path.suffix == '.log' and datetime.now(timezone.utc).timestamp() - path.stat().st_mtime < 2 * 86400:
            skips.append(str(path))
            continue
        allowed = (re.fullmatch(r'runtime/(?:test-[^/]+|repair-probe[^/]*|publication-acl-smoke[^/]*|longpath-check[^/]*)', rel)
                   or re.fullmatch(r'runtime/[^/]+\.log', rel)
                   or rel == 'runtime/tools/downloads'
                   or rel.startswith('runtime/cleanup-remnants/'))
        if rel.startswith('runtime/cache/work/parse/'):
            job_id = path.name
            allowed = job_id.isdigit() and job_id not in references
            row = db.execute('SELECT status FROM parse_jobs WHERE id=?', (job_id,)).fetchone()
            allowed = allowed and row is not None and row['status'] == 'completed'
        if not allowed:
            skips.append(str(path))
            continue
        targets.append({'path': str(path), 'kind': 'old_scratch'})
    checked = {}
    discarded = []
    parent = settings.runtime_root / 'duplicate-attachment-backup'
    if parent.is_dir():
        for folder in parent.iterdir():
            journal = folder / 'repair.json'
            if not folder.is_dir() or not journal.is_file():
                continue
            record = json.loads(journal.read_text(encoding='utf8'))
            if record.get('phase') == 'completed':
                keep = [e['keep'] for e in record['repairs']]
            elif record.get('completed'):
                keep = [record['canonical_file_before']]
            else:
                raise ValueError('An unfinished repair backup must not be deleted')
            for before in keep:
                row = db.execute('SELECT * FROM files WHERE id=?', (before['id'],)).fetchone()
                if row is None or row['download_status'] != 'verified' or row['sha256'] != before['sha256']:
                    raise ValueError('Retained attachment record cannot be confirmed')
                source = (settings.data_root / row['local_relpath']).resolve()
                if not source.is_relative_to(settings.data_root.resolve() / 'originals') or not source.is_file():
                    raise ValueError('Retained original is missing')
                if source not in checked:
                    checked[source] = sha256_file(source)
                if checked[source] != before['sha256']:
                    raise ValueError('Retained original checksum mismatch')
            targets.append({'path': str(folder.resolve()), 'kind': 'verified_duplicate_backup'})
            discarded.append(str(folder.resolve()))
    db.close()
    report = {'prepared_at': datetime.now(timezone.utc).isoformat(), 'workspace': str(root),
              'current_recovery_snapshot': str(backup), 'targets': targets, 'skipped': skips,
              'retained_originals_rehashed': len(checked), 'duplicate_backups_to_remove': discarded}
    output = settings.runtime_root / 'admin-cleanup-plan.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'targets': len(targets), 'originals_rehashed': len(checked),
                      'backup': str(backup), 'plan': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
