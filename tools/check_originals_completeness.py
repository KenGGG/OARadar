"""Read-only local reconciliation of Done manifest, archive records and originals."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from oa_knowledge.config import load_settings


def main():
    settings = load_settings(Path('config.yaml'))
    root = settings.data_root.resolve()
    originals = (root / 'originals').resolve()
    db = sqlite3.connect(settings.database_path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    manifest = [dict(r) for r in db.execute('SELECT * FROM oa_manifest_items')]
    eligible = {r['oa_item_key']: r for r in manifest
                if r['processing_status'] != 'skipped' and not r['matched_exclusion_keyword']}
    files = [dict(r) for r in db.execute(
        'SELECT f.*, i.oa_item_key FROM files f JOIN oa_items i ON i.id=f.oa_item_id')]
    sync = db.execute('SELECT * FROM oa_manifest_syncs ORDER BY id DESC LIMIT 1').fetchone()
    db.close()
    issues = []
    verified_by_item = Counter()
    hash_cache = {}
    paths = set()
    file_statuses = Counter()
    bytes_hashed = 0
    for index, record in enumerate(files, 1):
        key = record['oa_item_key']
        file_statuses[record['download_status']] += 1
        relative = record['local_relpath']
        reason = None
        if not relative:
            reason = 'missing_archive_path'
        else:
            path = (root / relative).resolve()
            if not path.is_relative_to(originals):
                reason = 'outside_originals'
            else:
                paths.add(path)
                try:
                    if not path.is_file():
                        reason = 'missing_file'
                    else:
                        size = path.stat().st_size
                        if size == 0:
                            reason = 'zero_byte_file'
                        elif record['size_bytes'] is not None and size != record['size_bytes']:
                            reason = 'size_mismatch'
                        elif record['sha256']:
                            if path not in hash_cache:
                                digest = hashlib.sha256()
                                with path.open('rb') as source:
                                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                                        digest.update(chunk)
                                hash_cache[path] = digest.hexdigest()
                                bytes_hashed += size
                            if hash_cache[path] != record['sha256']:
                                reason = 'sha256_mismatch'
                        else:
                            reason = 'missing_expected_sha256'
                except OSError as error:
                    reason = 'unreadable_' + type(error).__name__
        if record['download_status'] != 'verified' and reason is None:
            reason = 'database_download_not_verified'
        if reason:
            issues.append({'oa_item_key': key, 'file_id': str(record['id']),
                           'local_relpath': relative, 'reason': reason,
                           'eligible': key in eligible})
        else:
            verified_by_item[key] += 1
        if index % 500 == 0:
            print(json.dumps({'files_checked': index, 'total': len(files),
                              'issues': len(issues)}), flush=True)
    item_issues = []
    for key, record in eligible.items():
        reasons = []
        if record['processing_status'] not in {'downloaded', 'no_attachment'}:
            reasons.append('item_not_complete')
        if record['processing_status'] == 'downloaded' and verified_by_item[key] == 0:
            reasons.append('downloaded_item_without_valid_archived_file')
        if record['processing_status'] == 'no_attachment' and not record['no_attachment_confirmed']:
            reasons.append('no_attachment_not_confirmed')
        if reasons:
            item_issues.append({'oa_item_key': key, 'status': record['processing_status'],
                                'reasons': reasons, 'failure_stage': record['failure_stage'],
                                'last_error': record['last_error']})
    summary = {
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'manifest_total': len(manifest), 'eligible_items': len(eligible),
        'excluded_items': len(manifest) - len(eligible),
        'eligible_status_counts': dict(Counter(r['processing_status'] for r in eligible.values())),
        'latest_manifest_sync': dict(sync) if sync else None,
        'file_records_checked': len(files), 'file_status_counts': dict(file_statuses),
        'unique_files_hashed': len(hash_cache), 'bytes_hashed': bytes_hashed,
        'file_issue_counts': dict(Counter(r['reason'] for r in issues)),
        'eligible_file_issues': sum(r['eligible'] for r in issues),
        'item_issues': len(item_issues),
        'online_completeness_reverified': False,
    }
    report = settings.runtime_root / 'originals-completeness-check.json'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({'summary': summary, 'item_issues': item_issues,
                                 'file_issues': issues}, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'summary': summary, 'report': str(report)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
