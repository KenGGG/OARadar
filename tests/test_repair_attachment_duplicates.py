import hashlib
import json
import sqlite3
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, ParseJob
from tools import repair_attachment_duplicates as repair


def test_bulk_repair_preserves_original_and_redirects_parse_history(config_file, monkeypatch):
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    payload = b'%PDF-1.4\nsynthetic attachment\n%%EOF'
    digest = hashlib.sha256(payload).hexdigest()
    directory = settings.data_root / 'originals/synthetic'
    directory.mkdir(parents=True)
    (directory / 'real.pdf').write_bytes(payload)
    (directory / 'widget.pdf').write_bytes(payload)
    with Session(engine) as db:
        item = OAItem(oa_item_key='done:-101', workitem_id_text='-101', title='synthetic', source_channel='done', archive_relpath='originals/synthetic')
        db.add(item); db.flush()
        records = []
        for key, name, role, path in [('-901', 'synthetic.pdf', 'direct_attachment', 'real.pdf'),
                                      ('cap4-widget:synthetic', 'synthetic.pdf (1M)', 'official_body', 'widget.pdf')]:
            record = ArchivedFile(oa_item_id=item.id, attachment_key=key, original_name=name, file_role=role,
                                  source_container_key='collaboration:-101', depth=1, local_relpath='originals/synthetic/' + path,
                                  sha256=digest, size_bytes=len(payload), download_status='verified')
            db.add(record); db.flush(); records.append(record.id)
        db.add(ParseJob(file_id=records[1], engine='synthetic', engine_version='1', config_hash='x'*64, status='completed'))
        db.commit()
    monkeypatch.setattr(repair, 'load_settings', lambda _: settings)
    monkeypatch.setattr('sys.argv', ['repair', '--apply'])
    repair.main()
    with Session(engine) as db:
        rows = list(db.scalars(select(ArchivedFile)))
        assert len(rows) == 1 and rows[0].id == records[0]
        assert rows[0].file_role == 'official_body'
        assert db.scalar(select(ParseJob)).file_id == records[0]
    assert (directory / 'real.pdf').read_bytes() == payload
    assert not (directory / 'widget.pdf').exists()
    report = json.loads((settings.runtime_root / 'duplicate-attachment-repair.json').read_text(encoding='utf8'))
    backup = Path(report['summary']['backup_directory'])
    assert (backup / (str(records[1]) + '.bin')).read_bytes() == payload
    assert (backup / 'before-repair.sqlite3').is_file()
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute('pragma foreign_key_check').fetchall() == []
    repair.main()  # A repeated cleanup is a no-op.
    engine.dispose()
