"""Offline boundary inventory; never parses, downloads or changes OA decisions."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from oa_knowledge.classification.metadata_rules import configured_document_candidates
from oa_knowledge.classification.per_item_classifier import _issuer_from_source
from oa_knowledge.classification.private_config import load_private_classification_config



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--cache-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--legacy-head', action='append', required=True,
                        help='Locally verified legacy prefix; repeat for each prefix.')
    args = parser.parse_args()
    if any(not head.strip() for head in args.legacy_head):
        parser.error('--legacy-head must not be empty')
    heads_pattern = '|'.join(re.escape(head) for head in sorted(args.legacy_head, key=len, reverse=True))
    token = re.compile(r'((?:' + heads_pattern + r')[A-Za-z一-龥]{0,20})\s*[〔［【\[]\s*\d{4}\s*[〕］】\]]\s*\d{1,6}\s*号')
    config = load_private_classification_config(Path('private/classification')).config
    db = sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    groups = defaultdict(list)
    with args.manifest.open(encoding='utf-8-sig') as stream:
        for row in csv.DictReader(stream):
            title = row['标题']
            if not any(re.search(re.escape(h) + r'.*号', title) for h in args.legacy_head):
                continue
            if configured_document_candidates(title, config):
                continue
            tokens = list(token.finditer(title))
            heads = sorted({m.group(1) for m in tokens}) or ['no_complete_token']
            key = 'done:' + row['事项ID']
            evidence = []
            files = db.execute('''SELECT f.id, f.original_name, f.file_role, f.sha256,
                p.id artifact_id, p.output_relpath, p.source_sha256, p.product_sha256
                FROM oa_items i JOIN files f ON f.oa_item_id=i.id
                JOIN parse_artifacts p ON p.content_object_id=f.content_object_id
                WHERE i.oa_item_key=? AND f.download_status='verified'
                AND p.lifecycle_status='valid' ORDER BY p.id DESC''', (key,))
            seen = set()
            for f in files:
                if f['id'] in seen:
                    continue
                path = (args.cache_root / f['output_relpath']).resolve()
                if not path.is_relative_to(args.cache_root.resolve()) or not path.is_file():
                    continue
                raw = path.read_bytes()
                if f['source_sha256'] != f['sha256'] or hashlib.sha256(raw).hexdigest() != f['product_sha256']:
                    continue
                seen.add(f['id'])
                body = raw.decode('utf-8', errors='replace')
                # Private evidence includes bounded excerpts and exact local provenance.
                hits = [m for m in token.finditer(body) if m.group(1) in heads]
                if not hits:
                    continue
                evidence.append(dict(file_id=f['id'], artifact_id=f['artifact_id'],
                    artifact_relpath=f['output_relpath'], product_sha256=f['product_sha256'],
                    file_role=f['file_role'], filename=f['original_name'],
                    header=body[:1600], signature=body[-1600:],
                    header_issuer=_issuer_from_source('document_header', body[:1600]),
                    signature_issuer=_issuer_from_source('signature', body[-1600:]),
                    number_contexts=[body[max(0,m.start()-100):m.end()+180] for m in hits[:3]]))
            item = dict(item_key=key, title=title, heads=heads, evidence=evidence,
                cited_context=bool(re.search(r'根据|依据|参照|按照|附件[:：]',title)))
            for head in heads:
                groups[head].append(item)
    result = {'database': str(args.database), 'counts': {h:len(v) for h,v in groups.items()},
        'groups': {h:{'items':v, 'samples': sorted(v,key=lambda x: (not bool(x['evidence']),x['item_key']))[:3]}
                   for h,v in groups.items()}}
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.output.open('x', encoding='utf-8') as stream:
        args.output.chmod(0o600)
        json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'counts':result['counts'], 'evidence_items':{h:sum(bool(x['evidence']) for x in v) for h,v in groups.items()}},ensure_ascii=False))


if __name__ == '__main__':
    main()
