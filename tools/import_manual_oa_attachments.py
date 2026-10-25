"""Validate and import user-supplied RARs locally without altering OA records."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.collector.detail import DirectAttachment
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, OAItem, OAManifestItem, OperationJob, ReviewEntry
from oa_knowledge.detail_archive import _attachment_relname


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--item-key', required=True)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    settings = load_settings(Path('config.yaml'))
    source = args.source.resolve(strict=True)
    paths = sorted(source.glob('*.rar'))
    if not paths or any(source.glob('*.crdownload')) or any(source.glob('*.part')):
        raise ValueError('No completed RAR set, or incomplete downloads are present')
    report = {'source_directory': str(source), 'oa_item_key': args.item_key,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'files': []}
    for path in paths:
        before = path.stat()
        result = subprocess.run(
            [r'C:\Program Files\7-Zip\7z.exe', 't', '-bso0', '-bse0', '-bsp0', '-p-', str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
        with path.open('rb') as stream:
            magic = stream.read(8)
        digest = sha256_file(path)
        after = path.stat()
        valid = (result.returncode == 0 and magic.startswith(b'Rar!\x1a\x07')
                 and before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns)
        row = {'name': path.name, 'size_bytes': after.st_size, 'sha256': digest,
               'rar_test_exit_code': result.returncode, 'stable_and_valid': valid}
        report['files'].append(row)
        print(json.dumps({'name': path.name, 'rar_valid': valid}, ensure_ascii=False), flush=True)
        if not valid:
            raise ValueError('RAR validation failed; nothing was imported')
    report_path = settings.runtime_root / 'manual-rar-import-report.json'
    settings.runtime_root.mkdir(parents=True, exist_ok=True)
    if args.validate_only:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        return
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as db:
            if db.scalar(select(OperationJob.id).where(OperationJob.status.in_(['running', 'queued']))):
                raise ValueError('Active OA job; import deferred to avoid concurrent archive writes')
            item = db.scalar(select(OAItem).where(OAItem.oa_item_key == args.item_key))
            manifest = db.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == args.item_key))
            if item is None or manifest is None or not item.archive_relpath:
                raise ValueError('Existing OA item with archive directory required')
            directory = PurePosixPath(item.archive_relpath)
            root = settings.data_root.resolve()
            destination = (root / directory).resolve()
            if not destination.is_relative_to(root / 'originals'):
                raise ValueError('Archive path must stay within originals')
            destination.mkdir(parents=True, exist_ok=True)
            for entry in report['files']:
                path = source / entry['name']
                existing = list(db.scalars(select(ArchivedFile).where(
                    ArchivedFile.oa_item_id == item.id, ArchivedFile.original_name == path.name)))
                if len(existing) > 1:
                    raise ValueError('Ambiguous existing filename; refusing automatic association')
                record = existing[0] if existing else None
                key = record.attachment_key if record else 'manual-import:sha256:' + entry['sha256']
                descriptor = DirectAttachment(key, path.name, None, entry['size_bytes'])
                relative = directory / _attachment_relname(descriptor)
                target = (root / relative).resolve()
                if not target.is_relative_to(destination):
                    raise ValueError('Unsafe attachment filename')
                if target.exists():
                    if sha256_file(target) != entry['sha256']:
                        raise ValueError('Existing archive differs; refusing overwrite')
                else:
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=destination, prefix='.manual-import-', delete=False) as out:
                            temporary = Path(out.name)
                            with path.open('rb') as incoming:
                                shutil.copyfileobj(incoming, out, 4 * 1024 * 1024)
                            out.flush()
                            os.fsync(out.fileno())
                        if sha256_file(temporary) != entry['sha256']:
                            raise ValueError('Copied archive checksum mismatch')
                        os.replace(temporary, target)
                    finally:
                        if temporary is not None and temporary.exists():
                            temporary.unlink()
                if sha256_file(target) != entry['sha256']:
                    raise ValueError('Destination checksum mismatch')
                if record is None:
                    record = ArchivedFile(oa_item_id=item.id, attachment_key=key,
                                          original_name=path.name, file_role='direct_attachment',
                                          source_container_key='collaboration:' + str(manifest.workitem_id_text), depth=1)
                record.local_relpath = relative.as_posix()
                record.size_bytes = entry['size_bytes']
                record.expected_size = entry['size_bytes']
                record.sha256 = entry['sha256']
                record.mime_type = 'application/vnd.rar'
                record.download_status = 'verified'
                record.verified_at = datetime.now(timezone.utc)
                record.download_attempts = (record.download_attempts or 0) + 1
                db.add(record)
                entry.update(local_relpath=relative.as_posix(), attachment_key=key,
                             identity_source='oa' if existing else 'user_supplied_manual_import',
                             destination_sha256_verified=True)
            db.flush()
            remaining = list(db.scalars(select(ArchivedFile).where(
                ArchivedFile.oa_item_id == item.id, ArchivedFile.download_status != 'verified')))
            depth_review = db.scalar(select(ReviewEntry.id).where(
                ReviewEntry.item_id == item.id, ReviewEntry.kind == 'depth_limit_reached', ReviewEntry.status == 'pending'))
            if not remaining and depth_review is None:
                manifest.processing_status = 'downloaded'
                manifest.last_error = None
                manifest.failure_stage = None
                manifest.no_attachment_confirmed = False
                item.pipeline_status = 'files_verified'
            for review in db.scalars(select(ReviewEntry).where(ReviewEntry.item_id == item.id, ReviewEntry.status == 'pending')):
                issue = json.loads(review.details_json or '{}')
                if issue.get('kind') == 'attachment_download_failed':
                    review.status = 'resolved'
            for job in db.scalars(select(OperationJob).where(OperationJob.job_type == 'done_download', OperationJob.status.in_(['failed', 'paused']))):
                parameters = json.loads(job.parameters_json or '{}')
                keys = parameters.get('keys', [])
                if args.item_key not in keys:
                    continue
                rows = list(db.scalars(select(OAManifestItem).where(OAManifestItem.oa_item_key.in_(keys))))
                job.progress_current = sum(r.processing_status in {'downloaded', 'no_attachment', 'skipped'} for r in rows)
                parameters['failed_count'] = sum(r.processing_status in {'download_failed', 'auth_required'} for r in rows)
                parameters['deferred_keys'] = [k for k in parameters.get('deferred_keys', []) if k != args.item_key]
                if len(rows) == len(keys) and job.progress_current == len(keys):
                    job.status = 'completed'
                    job.last_error_code = None
                    job.finished_at = datetime.now(timezone.utc)
                    parameters['pause_requested'] = False
                job.parameters_json = json.dumps(parameters)
            db.commit()
            report.update(imported=True, archive_relpath=directory.as_posix(),
                          manifest_status=manifest.processing_status, online_inventory_reverified=False)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        print(json.dumps({'imported_files': len(paths), 'report': str(report_path)}, ensure_ascii=False), flush=True)
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
