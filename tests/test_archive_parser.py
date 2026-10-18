import zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from oa_knowledge.parsers.archive_parser import parse_with_archive
from oa_knowledge.parsers.router import ParseResult


def test_zip_converts_member_bodies_not_just_listing(tmp_path,monkeypatch):
    source=tmp_path/'synthetic.zip'
    with zipfile.ZipFile(source,'w')as out:out.writestr('folder/a.txt','Synthetic member content '*5)
    def parse(path,settings,**kwargs):
        product=kwargs['output_dir']/'document.md';product.parent.mkdir(parents=True)
        text=path.read_text();product.write_text(text)
        return ParseResult(product,'markitdown','test',1.0,text_length=len(text))
    monkeypatch.setattr('oa_knowledge.parsers.router.parse_file',parse)
    result=parse_with_archive(source,tmp_path/'output',settings=SimpleNamespace())
    text=result.output_path.read_text()
    assert 'folder/a.txt'in text and 'Synthetic member content'in text
    assert result.quality_score==1.0


@pytest.mark.parametrize('name',['../outside.txt','/absolute.txt','C:/absolute.txt','a/'*11+'file.txt'])
def test_archive_rejects_unsafe_or_over_depth_members(tmp_path,name):
    source=tmp_path/'synthetic.zip'
    with zipfile.ZipFile(source,'w')as out:out.writestr(name,'Synthetic')
    with pytest.raises(RuntimeError,match='unsafe_archive_member|depth_limit_reached'):
        parse_with_archive(source,tmp_path/'output',settings=SimpleNamespace())
    assert not (tmp_path/'outside.txt').exists()


def test_unsupported_member_does_not_become_successful_listing(tmp_path):
    source=tmp_path/'synthetic.zip'
    with zipfile.ZipFile(source,'w')as out:out.writestr('synthetic.mp4',b'synthetic-video')
    with pytest.raises(RuntimeError,match='archive_entry_unsupported'):
        parse_with_archive(source,tmp_path/'output',settings=SimpleNamespace())


def test_archive_depth_failure_enters_review_queue(tmp_path):
    from test_source_markdown_service import _seed
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from oa_knowledge.db.models import ArchivedFile,ReviewEntry,ParseJob
    from oa_knowledge.pipeline import ParsePipeline
    import hashlib
    settings,engine,source_id,_=_seed(tmp_path)
    with Session(engine)as db:
        parent=db.get(ArchivedFile,source_id)
        path=settings.data_root/'originals/done/synthetic/deep.zip';path.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(path,'w')as out:out.writestr('folder/'*11+'synthetic.txt','Synthetic')
        file=ArchivedFile(oa_item_id=parent.oa_item_id,attachment_key='deep-synthetic',file_role='direct_attachment',
            source_container_key='root',
            original_name='deep.zip',local_relpath=path.relative_to(settings.data_root).as_posix(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),size_bytes=path.stat().st_size,download_status='verified')
        db.add(file);db.commit();file_id=file.id
    pipeline=ParsePipeline(settings,engine);job_id=pipeline.enqueue(file_id)
    with pytest.raises(RuntimeError,match='depth_limit_reached'):pipeline.run(job_id)
    with Session(engine)as db:
        assert db.get(ParseJob,job_id).status=='failed'
        assert db.scalar(select(ReviewEntry).where(ReviewEntry.file_id==file_id)).kind=='depth_limit_reached'
