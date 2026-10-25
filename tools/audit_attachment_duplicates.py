"""Read-only, local audit of alternate attachment views; no OA traffic."""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.collector.detail import DirectAttachment, deduplicate_attachment_views
from oa_knowledge.config import load_settings


def main():
    settings = load_settings(Path('config.yaml'))
    db = sqlite3.connect(settings.database_path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    groups = defaultdict(list)
    for row in db.execute('SELECT f.*,i.oa_item_key,i.title FROM files f JOIN oa_items i ON i.id=f.oa_item_id WHERE f.download_status="verified" AND f.sha256 IS NOT NULL'):
        groups[(row['oa_item_id'], row['source_container_key'], row['sha256'], row['size_bytes'])].append(dict(row))
    db.close()
    candidates = []
    hashed = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        representations = [DirectAttachment(r['attachment_key'], r['original_name'], None, r['size_bytes'],
                                           file_role=r['file_role'], content=b'same-verified-hash-group') for r in group]
        merged = deduplicate_attachment_views(representations)
        kept_keys = {r.attachment_key for r in merged}
        removed = [r for r in group if r['attachment_key'] not in kept_keys]
        if not removed:
            continue
        valid = True
        evidence = []
        for record in group:
            path = (settings.data_root / (record['local_relpath'] or '')).resolve()
            if not path.is_relative_to((settings.data_root / 'originals').resolve()) or not path.is_file():
                actual = None
            else:
                if path not in hashed:
                    hashed[path] = sha256_file(path)
                actual = hashed[path]
            verified = actual == record['sha256']
            valid = valid and verified
            evidence.append({k: record[k] for k in ('id', 'attachment_key', 'original_name', 'file_role', 'local_relpath', 'sha256', 'size_bytes')})
            evidence[-1]['actual_hash_verified'] = verified
            evidence[-1]['redundant_view'] = record['attachment_key'] not in kept_keys
        candidates.append({'oa_item_key': group[0]['oa_item_key'], 'title': group[0]['title'],
                           'container_key': group[0]['source_container_key'], 'actual_files_verified': valid,
                           'extra_file_records': len(removed), 'files': evidence})
        if len(candidates) % 100 == 0:
            print(json.dumps({'groups_checked': len(candidates)}), flush=True)
    confirmed = [r for r in candidates if r['actual_files_verified']]
    summary = {'checked_at': datetime.now(timezone.utc).isoformat(),
               'verified_duplicate_groups': len(confirmed),
               'affected_items': len({r['oa_item_key'] for r in confirmed}),
               'extra_file_records': sum(r['extra_file_records'] for r in confirmed),
               'unverified_groups': len(candidates) - len(confirmed),
               'online_counts_reverified': False}
    path = settings.runtime_root / 'duplicate-attachment-scan.json'
    path.write_text(json.dumps({'summary': summary, 'duplicate_groups': candidates}, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'summary': summary, 'report': str(path)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
