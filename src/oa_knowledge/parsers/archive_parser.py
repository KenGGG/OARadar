"""Convert archive member bodies locally without extracting archive paths."""
from __future__ import annotations
import os,re,json,shutil,subprocess,zipfile,hashlib
from pathlib import Path,PurePosixPath
from urllib.parse import quote,unquote
from dataclasses import asdict
from oa_knowledge.parsers.router import ParseResult

LIMIT_BYTES=1024*1024*1024
LIMIT_MEMBER=200*1024*1024
LIMIT_FILES=3000


def _seven_zip():
    binary=shutil.which('7z') or shutil.which('7zz')
    if binary:return binary
    known=Path('C:/Program Files/7-Zip/7z.exe')
    if known.is_file():return str(known)
    raise RuntimeError('archive_extractor_unavailable')


def _validate_name(name,depth):
    p=PurePosixPath(name.replace('\\','/'))
    if p.is_absolute() or '..' in p.parts or re.match(r'^[A-Za-z]:',name):raise RuntimeError('unsafe_archive_member')
    if depth+len(p.parts)>10:raise RuntimeError('depth_limit_reached')


def _members(source,target,budget,depth):
    def reserve(name,size):
        _validate_name(name,depth)
        if size>LIMIT_MEMBER:raise RuntimeError('archive_member_size_limit')
        budget['bytes']+=size;budget['files']+=1
        if budget['bytes']>LIMIT_BYTES or budget['files']>LIMIT_FILES:raise RuntimeError('archive_expansion_limit')
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source)as archive:
            for number,info in enumerate(archive.infolist()):
                if info.is_dir()or info.filename.startswith('__MACOSX/')or Path(info.filename).name.lower()in {'.ds_store','thumbs.db'}:continue
                if info.flag_bits&1:raise RuntimeError('encrypted_archive')
                reserve(info.filename,info.file_size)
                extension=Path(info.filename).suffix.lower()
                extension=extension if re.fullmatch(r'\.[a-z0-9]{1,10}',extension)else '.bin'
                dest=target/f'member-{number:04d}{extension}'
                with archive.open(info)as incoming,dest.open('wb')as out:
                    size=0
                    while data:=incoming.read(1024*1024):
                        size+=len(data)
                        if size>LIMIT_MEMBER:raise RuntimeError('archive_member_size_limit')
                        out.write(data)
                yield info.filename,dest
    else:
        binary=_seven_zip()
        listing=subprocess.run([binary,'l','-slt','-sccUTF-8','-p__oaradar_no_password__',str(source)],capture_output=True,timeout=60)
        if listing.returncode:raise RuntimeError('archive_listing_failed_or_encrypted')
        body=listing.stdout.decode('utf8',errors='replace').split('----------',1)[-1]
        entries=[]
        for block in re.split(r'\r?\n\r?\n',body.strip()):
            fields=dict(line.split(' = ',1)for line in block.splitlines()if ' = 'in line)
            if not fields.get('Path')or fields.get('Folder')=='+'or fields.get('Attributes','').startswith('D'):continue
            if fields.get('Encrypted')=='+':raise RuntimeError('encrypted_archive')
            name=fields['Path'];reserve(name,int(fields.get('Size',0)));entries.append(name)
        for n,name in enumerate(entries):
            suffix=Path(name).suffix.lower();suffix=suffix if re.fullmatch(r'\.[a-z0-9]{1,10}',suffix)else '.bin'
            dest=target/f'member-{n:04d}{suffix}'
            with dest.open('wb')as out:
                process=subprocess.run([binary,'x','-so','-spd','-p__oaradar_no_password__',str(source),'--',name],stdout=out,stderr=subprocess.PIPE,timeout=120)
            if process.returncode or dest.stat().st_size>LIMIT_MEMBER:raise RuntimeError('archive_extraction_failed')
            yield name,dest


def parse_with_archive(file_path:Path,output_dir:Path,*,settings,profile_version='legacy')->ParseResult:
    from oa_knowledge.parsers.format_router import detect_format
    from oa_knowledge.parsers.router import parse_file
    from oa_knowledge.parsers.libreoffice_parser import needs_word_pdf_ocr
    root=Path(output_dir).resolve();root.mkdir(parents=True,exist_ok=True)
    budget={'files':0,'bytes':0};qualities=[]
    def walk(source,where,depth):
        if depth>10:raise RuntimeError('depth_limit_reached')
        where.mkdir(parents=True,exist_ok=True);sections=[]
        for number,(name,member)in enumerate(_members(source,where,budget,depth)):
            kind=detect_format(member)
            child=where/f'parsed-{number:04d}'
            if kind.actual_file_type in {'zip','rar','7z'}:
                text=walk(member,child,depth+1)
            else:
                if kind.status_code!='parseable':raise RuntimeError('archive_entry_unsupported:'+name)
                engine='mineru'if kind.actual_file_type in {'pdf','png','jpg','jpeg','tiff','tif','bmp'}else 'libreoffice'if kind.actual_file_type in {'doc','xls','ppt','pptx'}else 'markitdown'
                checkpoint=child/'member-result.json';result=None
                fingerprint=hashlib.sha256(member.read_bytes()).hexdigest()
                if checkpoint.is_file():
                    cached=json.loads(checkpoint.read_text(encoding='utf8'));product=(root/cached['result']['output_path']).resolve()
                    if product.is_relative_to(root)and product.is_file()and cached['source']==fingerprint and hashlib.sha256(product.read_bytes()).hexdigest()==cached['sha256']:
                        result=ParseResult(**{**cached['result'],'output_path':product})
                if result is None:
                    result=parse_file(member,settings,engine=engine,output_dir=child,profile_version=profile_version)
                    text=result.output_path.read_text(encoding='utf8')
                    if needs_word_pdf_ocr(member,result.engine,text):
                        result=parse_file(member,settings,engine='libreoffice',output_dir=child/'office',profile_version=profile_version)
                    if re.search('StrangeNoGraphicData|图片未包含|图片未嵌入',result.output_path.read_text(encoding='utf8')):raise RuntimeError('archive_member_images_incomplete:'+name)
                    child.mkdir(parents=True,exist_ok=True)
                    checkpoint.write_text(json.dumps({'source':fingerprint,'sha256':hashlib.sha256(result.output_path.read_bytes()).hexdigest(),'result':{**asdict(result),'output_path':result.output_path.relative_to(root).as_posix()}},ensure_ascii=False),encoding='utf8')
                qualities.append(result.quality_score)
                text=result.output_path.read_text(encoding='utf8')
                def image(match):
                    url=match.group(1)
                    if re.match('https?://|data:',url):return match.group(0)
                    imagepath=(result.output_path.parent/unquote(url)).resolve();imagepath.relative_to(root)
                    return match.group(0).replace(url,quote(imagepath.relative_to(root).as_posix(),safe='/'),1)
                text=re.sub(r'!\[[^\]]*\]\(([^)]+)\)',image,text)
            sections.append('## '+name.replace('\n',' ')+'\n\n'+text)
        if not sections:raise RuntimeError('archive_has_no_convertible_members')
        return '\n\n'.join(sections)
    text=walk(Path(file_path),root/'members',0);product=root/'document.md';product.write_text(text,encoding='utf8')
    return ParseResult(product,'archive','archive-v1',min(qualities,default=0),text_length=len(text),profile_version=profile_version)
