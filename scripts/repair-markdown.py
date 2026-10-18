"""Resume only missing attachment Markdown; all OA data stays local."""
from pathlib import Path
import json
import os
import sys
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sqlalchemy import select
from sqlalchemy.orm import Session
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, OAItem, OAManifestItem, MarkdownExport, ParseJob, ReviewEntry
from oa_knowledge.pipeline import ParsePipeline
from oa_knowledge.parsers.format_router import detect_format
from oa_knowledge.parsers.libreoffice_parser import needs_word_pdf_ocr
from oa_knowledge.source_markdown.service import publish_active_artifact
from oa_knowledge.source_library import classify_library_item, publish_library_index
from oa_knowledge.platform_lock import lock_exclusive
from oa_knowledge.archive.writer import atomic_write_bytes


def main():
    settings = load_settings(Path(__file__).resolve().parents[1] / 'config.yaml')
    engine = create_db_engine(settings.database_path)
    directory = settings.runtime_root / 'repair-20261017'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'repair.lock').open('a+') as lock:
        try:lock_exclusive(lock.fileno())
        except BlockingIOError:return
        atomic_write_bytes(json.dumps({'progress_relpath': 'repair-20261017/progress.json'}).encode(), settings.runtime_root, 'local-conversion.json')
        with Session(engine) as db:
            successful = set(db.scalars(select(MarkdownExport.source_file_id).where(MarkdownExport.status == 'success')))
            files = list(db.scalars(select(ArchivedFile).join(OAItem).join(OAManifestItem, OAManifestItem.oa_item_key == OAItem.oa_item_key).where(
                OAManifestItem.processing_status == 'downloaded',
                OAManifestItem.matched_exclusion_keyword.is_(None), ArchivedFile.download_status == 'verified').order_by(ArchivedFile.id)))
            files = [f for f in files if f.id not in successful]
            for f in files:db.expunge(f)
        # Quick conversions first, then OCR, then nested archives.
        def priority(f):
            kind = detect_format(settings.data_root / f.local_relpath)
            return (2 if kind.actual_file_type in {'zip','rar','7z'} else 1 if kind.actual_file_type == 'pdf' else 0, f.id)
        files.sort(key=priority)
        pipeline = ParsePipeline(settings, engine)
        counts = {'success': 0, 'review': 0}

        def progress(number, source=None, stage='markdown_delivery'):
            state = dict(processed=number, total=len(files), stage=stage, pid=os.getpid(), updated_at=datetime.now(timezone.utc).isoformat(), **counts)
            if source:state['current_file_id'] = str(source.id)
            atomic_write_bytes(json.dumps(state).encode(), directory, 'progress.json')

        progress(0)
        for number, source in enumerate(files):
            progress(number, source)
            error = None
            try:
                path = settings.data_root / source.local_relpath
                decision = detect_format(path)
                kind = decision.actual_file_type
                if decision.status_code != 'parseable' and kind not in {'zip','rar','7z'}:
                    raise RuntimeError('unsupported_format:' + kind)
                parser = 'archive' if kind in {'zip','rar','7z'} else 'mineru' if kind in {'pdf','png','jpg','jpeg','tif','tiff','bmp'} else 'libreoffice' if kind in {'doc','xls','ppt','pptx'} or (kind == 'docx' and needs_word_pdf_ocr(path, 'markitdown', '')) else 'markitdown'
                job_id = pipeline.enqueue(source.id, engine=parser)
                if job_id is None:raise RuntimeError('parse_not_eligible')
                with Session(engine) as db:
                    job = db.get(ParseJob, job_id)
                    if job.status == 'failed':
                        job.status = 'queued';job.error_code = None;db.commit()
                    status = job.status
                if status == 'queued':pipeline.run(job_id)
                elif status != 'completed':raise RuntimeError('parse_job_' + status)
                with Session(engine) as db:
                    record = publish_active_artifact(db, settings, source.id)
                    if record.status != 'success':raise RuntimeError(record.last_error_code or 'publication_incomplete')
                    item = db.get(OAItem, source.oa_item_id)
                    manifest = db.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == item.oa_item_key))
                    classification = classify_library_item(db, settings, manifest)
                    publish_library_index(db, settings, item, manifest, classification)
                    for review in db.scalars(select(ReviewEntry).where(ReviewEntry.file_id == source.id, ReviewEntry.kind == 'markdown_repair', ReviewEntry.status == 'pending')):
                        review.status = 'resolved'
                    db.commit()
                counts['success'] += 1
            except Exception as exc:
                error = type(exc).__name__ + ':' + str(exc)[:1000]
                counts['review'] += 1
                with Session(engine) as db:
                    review = db.scalar(select(ReviewEntry).where(ReviewEntry.file_id == source.id, ReviewEntry.kind == 'markdown_repair', ReviewEntry.status == 'pending'))
                    if review is None:
                        review = ReviewEntry(kind='markdown_repair', item_id=source.oa_item_id, file_id=source.id)
                        db.add(review)
                    review.details_json = json.dumps({'error': error}, ensure_ascii=False)
                    db.commit()
            with (directory / 'results.jsonl').open('a', encoding='utf8') as output:
                output.write(json.dumps({'file_id': str(source.id), 'status': 'review' if error else 'success', 'error': error}, ensure_ascii=False) + '\n')
            progress(number + 1)
        progress(len(files), stage='local_conversion_finished')
    engine.dispose()


if __name__ == '__main__':main()
