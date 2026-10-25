"""Conservative local attachment-view reconciliation with recoverable backups."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.collector.detail import attachment_display_filename, attachment_filename_identity, same_attachment_filename
from oa_knowledge.config import load_settings


def alias(key):
    return key.startswith(('cap4-widget:', 'cap4:'))


def plan(db):
    groups = defaultdict(list)
    for row in db.execute('SELECT f.*,i.oa_item_key,i.title FROM files f JOIN oa_items i ON i.id=f.oa_item_id WHERE f.download_status="verified" AND f.sha256 IS NOT NULL'):
        groups[(row['oa_item_id'], row['source_container_key'], row['sha256'], row['size_bytes'])].append(dict(row))
    repairs, unresolved = [], []
    for group in groups.values():
        real = [r for r in group if not alias(r['attachment_key'])]
        for extra in [r for r in group if alias(r['attachment_key'])]:
            matches = [r for r in real if same_attachment_filename(r['original_name'], extra['original_name'])]
            exact = [r for r in matches if attachment_filename_identity(r['original_name']) == attachment_filename_identity(extra['original_name'])]
            if exact:
                matches = exact
            if len(matches) == 1:
                repairs.append({'keep': matches[0], 'extra': extra})
            elif matches:
                unresolved.append({'oa_item_key': extra['oa_item_key'], 'file_id': extra['id'], 'reason': 'ambiguous_real_attachment_ids'})
    return repairs, unresolved


def write_report(path, report):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    temporary.replace(path)


def relocate(db, root, backup, report, journal):
    for entry in report['repairs']:
        extra = entry['extra']
        source = (root / extra['local_relpath']).resolve()
        target = (backup / (str(extra['id']) + '.bin')).resolve()
        if not source.is_relative_to(root / 'originals') or not target.is_relative_to(backup):
            raise ValueError('Backup path outside allowed trees')
        if db.execute('SELECT 1 FROM files WHERE local_relpath=?', (extra['local_relpath'],)).fetchone():
            entry['backup_state'] = 'shared_path_retained'
            continue
        if target.exists():
            if sha256_file(target) != extra['sha256']:
                raise ValueError('Existing recovery copy differs')
            if source.exists():
                raise ValueError('Both backup and original exist; review before moving')
        else:
            if not source.is_file() or sha256_file(source) != extra['sha256']:
                raise ValueError('Duplicate changed before move')
            shutil.move(str(source), str(target))
        if sha256_file(target) != extra['sha256']:
            raise ValueError('Recovery copy checksum mismatch')
        entry['backup_state'] = 'backed_up'
        entry['backup_filename'] = target.name
    report['phase'] = 'completed'
    write_report(journal, report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    settings = load_settings(Path('config.yaml'))
    root = settings.data_root.resolve()
    db = sqlite3.connect(settings.database_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        if args.resume:
            journal = args.resume.resolve(strict=True)
            if not journal.is_relative_to(settings.runtime_root.resolve() / 'duplicate-attachment-backup'):
                raise ValueError('Journal outside recovery tree')
            report = json.loads(journal.read_text(encoding='utf8'))
            if report['phase'] not in {'database_updated', 'completed'}:
                raise ValueError('Database phase is not complete')
            relocate(db, root, journal.parent, report, journal)
            return
        repairs, unresolved = plan(db)
        print(json.dumps({'planned_repairs': len(repairs), 'affected_items': len({r['keep']['oa_item_key'] for r in repairs}), 'unresolved': len(unresolved)}), flush=True)
        if not args.apply:
            return
        if not repairs:
            return
        if db.execute("SELECT 1 FROM operation_jobs WHERE status IN ('running','queued')").fetchone():
            raise ValueError('Active OA job; maintenance deferred')
        hashes = {}
        for index, entry in enumerate(repairs, 1):
            for record in (entry['keep'], entry['extra']):
                path = (root / record['local_relpath']).resolve()
                if not path.is_relative_to(root / 'originals') or not path.is_file():
                    raise ValueError('Missing file or unsafe path')
                if path not in hashes:
                    hashes[path] = sha256_file(path)
                if hashes[path] != record['sha256'] or path.stat().st_size != record['size_bytes']:
                    raise ValueError('Actual file does not match archive evidence')
            if index % 100 == 0:
                print(json.dumps({'verified_groups': index}), flush=True)
        backup = (settings.runtime_root / 'duplicate-attachment-backup' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')).resolve()
        if not backup.is_relative_to(settings.runtime_root.resolve()):
            raise ValueError('Backup directory outside runtime')
        backup.mkdir(parents=True, exist_ok=False)
        with sqlite3.connect(backup / 'before-repair.sqlite3') as snapshot:
            db.backup(snapshot)
        report = {'started_at': datetime.now(timezone.utc).isoformat(), 'phase': 'prepared',
                  'repairs': repairs, 'unresolved': unresolved, 'online_counts_reverified': False}
        journal = backup / 'repair.json'
        write_report(journal, report)
        references = []
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for foreign_key in db.execute('PRAGMA foreign_key_list(' + table + ')'):
                if foreign_key[2] == 'files':
                    references.append((table, foreign_key[3]))
        db.execute('BEGIN IMMEDIATE')
        try:
            if db.execute("SELECT 1 FROM operation_jobs WHERE status IN ('running','queued')").fetchone():
                raise ValueError('OA job became active during verification')
            for entry in repairs:
                keep, extra = entry['keep'], entry['extra']
                for table, column in references:
                    count = db.execute(f'SELECT count(*) FROM {table} WHERE {column}=?', (extra['id'],)).fetchone()[0]
                    if count and table not in {'parse_jobs', 'review_queue', 'markdown_exports'}:
                        raise ValueError('Unsupported downstream references in ' + table)
                if db.execute("SELECT 1 FROM parse_jobs WHERE file_id IN (?,?) AND status='running'", (keep['id'], extra['id'])).fetchone():
                    raise ValueError('Affected attachment is being parsed')
                # Retain parsing and review history while redirecting its identity.
                db.execute('UPDATE parse_jobs SET file_id=? WHERE file_id=?', (keep['id'], extra['id']))
                db.execute('UPDATE review_queue SET file_id=? WHERE file_id=?', (keep['id'], extra['id']))
                for export in db.execute('SELECT * FROM markdown_exports WHERE source_file_id=?', (extra['id'],)).fetchall():
                    current = db.execute('SELECT * FROM markdown_exports WHERE source_file_id=? AND schema_version=?', (keep['id'], export['schema_version'])).fetchone()
                    successful = {'success', 'published', 'completed'}
                    if current is not None and not (export['status'] in successful and current['status'] not in successful):
                        db.execute("UPDATE markdown_exports SET source_file_id=NULL,status='superseded',last_error_code='DUPLICATE_ATTACHMENT_VIEW',last_error='Superseded by canonical attachment',source_relpath=? WHERE id=?", (keep['local_relpath'], export['id']))
                    else:
                        if current is not None:
                            db.execute("UPDATE markdown_exports SET source_file_id=NULL,status='superseded',last_error_code='DUPLICATE_ATTACHMENT_VIEW' WHERE id=?", (current['id'],))
                        db.execute('UPDATE markdown_exports SET source_file_id=?,source_relpath=? WHERE id=?', (keep['id'], keep['local_relpath'], export['id']))
                priority = {'official_body': 3, 'official_attachment': 2, 'direct_attachment': 1}
                live = db.execute('SELECT * FROM files WHERE id=?', (keep['id'],)).fetchone()
                role = max((live['file_role'], extra['file_role']), key=lambda r: priority.get(r, 0))
                name = attachment_display_filename(live['original_name'])
                richer = attachment_display_filename(extra['original_name'])
                if '...' in name and '...' not in richer:
                    name = richer
                db.execute('UPDATE files SET original_name=?,file_role=? WHERE id=?', (name, role, keep['id']))
                db.execute('DELETE FROM files WHERE id=?', (extra['id'],))
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('Foreign key consistency check failed')
            db.commit()
        except BaseException:
            db.rollback()
            raise
        report['phase'] = 'database_updated'
        write_report(journal, report)
        relocate(db, root, backup, report, journal)
        report['summary'] = {'repaired_extra_file_records': len(repairs),
                             'affected_items': len({r['keep']['oa_item_key'] for r in repairs}),
                             'unresolved': len(unresolved), 'backup_directory': str(backup),
                             'foreign_key_issues': len(db.execute('PRAGMA foreign_key_check').fetchall())}
        write_report(journal, report)
        write_report(settings.runtime_root / 'duplicate-attachment-repair.json', report)
        print(json.dumps(report['summary']), flush=True)
    finally:
        db.close()


if __name__ == '__main__':
    main()
