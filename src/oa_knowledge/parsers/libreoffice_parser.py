"""Local Office conversion: legacy spreadsheets and Word PDF/OCR fallback."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import re
import zipfile
import json
from xml.etree import ElementTree as ET
from pathlib import PurePosixPath
from pathlib import Path
from dataclasses import replace

from oa_knowledge.parsers.router import ParseResult


import os
import signal


def office_executable():
    executable=shutil.which('libreoffice') or shutil.which('soffice')
    if executable:return executable
    portable=Path(__file__).resolve().parents[3]/'runtime'/'tools'/'libreoffice'
    for candidate in (portable/'program'/'soffice.com',portable/'LibreOffice'/'program'/'soffice.com',portable/'program'/'soffice.exe',portable/'LibreOffice'/'program'/'soffice.exe'):
        if candidate.is_file():return str(candidate)
    return None


def _run_office_process(command: list[str], *, timeout: int | float) -> subprocess.CompletedProcess[str]:
    """Run Office in its own process group so a timeout also stops children."""
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
    )
    job=None;kernel=None
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR];kernel.CreateJobObjectW.restype=wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE];kernel.AssignProcessToJobObject.restype=wintypes.BOOL
        kernel.TerminateJobObject.argtypes=[wintypes.HANDLE,wintypes.UINT];kernel.TerminateJobObject.restype=wintypes.BOOL
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
        job=kernel.CreateJobObjectW(None,None)
        if job and not kernel.AssignProcessToJobObject(job,wintypes.HANDLE(int(process._handle))):
            kernel.CloseHandle(job);job=None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if job:
                kernel.TerminateJobObject(job,1)
            elif os.name=='nt':
                subprocess.run(['taskkill.exe','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=20)
            else:os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    finally:
        if job:kernel.CloseHandle(job)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def needs_word_pdf_ocr(source: Path, engine: str | None, body: str) -> bool:
    """Native Office extraction alone does not recognize embedded scan text."""
    from oa_knowledge.parsers.format_router import detect_format
    if engine == 'libreoffice':
        return False
    kind = detect_format(source).actual_file_type
    if kind == 'docx':
        with zipfile.ZipFile(source) as package:
            return any(name.startswith('word/media/') for name in package.namelist())
    return kind == 'doc' and (not body.strip() or '![' in body or 'graphic' in body or '图片未' in body)


def restore_terminal_text(pdf: Path, markdown: Path) -> bool:
    """Preserve native terminal imprint text discarded by PDF layout analysis."""
    import fitz
    content_list = next(markdown.parent.glob('*_content_list.json'), None)
    middle_json = markdown.parent / 'middle_json.json'
    if content_list is None and not middle_json.is_file():
        return False
    entries = json.loads(content_list.read_text(encoding='utf8')) if content_list else []
    with fitz.open(pdf) as document:
        last = len(document) - 1
        native = '\n'.join(line for line in document[last].get_text().splitlines()
                           if not re.fullmatch(r'[—\-\s]*\d+[—\-\s]*', line))
    parsed = sum(len(x.get('text','')) + len(x.get('table_body',''))
                 for x in entries if x.get('page_idx') == last)
    if content_list is None:
        def text_length(value):
            if isinstance(value, dict):
                return sum(len(child) if key in {'content', 'text', 'html'} and isinstance(child, str)
                           else text_length(child) for key, child in value.items())
            if isinstance(value, list):
                return sum(text_length(child) for child in value)
            return 0
        middle = json.loads(middle_json.read_text(encoding='utf-8'))
        parsed = sum(text_length(page.get('blocks', [])) for page in middle.get('pages', [])
                     if page.get('page_idx') == last)
    if parsed > 20 or not 40 < len(native.strip()) < 500:
        return False
    body = markdown.read_text(encoding='utf8')
    normalized = re.sub(r'\W|_', '', native)
    existing = re.sub(r'\W|_', '', body)
    if normalized[:20] in existing and normalized[-20:] in existing:
        return False
    markdown.write_text(body.rstrip() + f'\n\n<!-- PDF 第 {last+1} 页原文 -->\n\n' + native.strip() + '\n',encoding='utf8')
    return True


def restore_scan_pages(docx: Path, pdf: Path) -> int:
    """Recover clipped full-page inline scans only; never rearrange mixed text."""
    import fitz
    w = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    a = 'http://schemas.openxmlformats.org/drawingml/2006/main'
    r = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    with zipfile.ZipFile(docx) as package:
        document = ET.fromstring(package.read('word/document.xml'))
        if any((node.text or '').strip() for node in document.iter(f'{{{w}}}t')):
            return 0
        relationships = ET.fromstring(package.read('word/_rels/document.xml.rels'))
        targets = {x.get('Id'): x.get('Target') for x in relationships if x.get('TargetMode') != 'External'}
        ordered = [targets.get(x.get(f'{{{r}}}embed')) for x in document.iter(f'{{{a}}}blip')]
        with fitz.open(pdf) as rendered:
            if len(ordered) <= len(rendered):
                return 0
        images = []
        for target in ordered:
            if not target or PurePosixPath(target).is_absolute() or '..' in PurePosixPath(target).parts:
                raise ValueError('unsafe_or_missing_word_scan_relationship')
            payload = package.read('word/' + target)
            with fitz.open(stream=payload) as image:
                width, height = image[0].rect.width, image[0].rect.height
                if width < 500 or height < 700 or not .55 < width / height < .9:
                    raise RuntimeError('word_scan_layout_requires_review')
                images.append(image.convert_to_pdf())
    replacement = pdf.with_name('recovered-scans.pdf')
    with fitz.open() as result:
        for data in images:
            with fitz.open('pdf', data) as page:
                result.insert_pdf(page)
        result.save(replacement)
    shutil.copy2(pdf, pdf.with_name('word-layout.pdf'))
    replacement.replace(pdf)
    return len(images)


def word_to_pdf(file_path: Path, output_dir: Path) -> Path:
    """Convert only a private, normalized copy, with an isolated Office profile."""
    import fitz
    from oa_knowledge.parsers.format_router import detect_format
    kind = detect_format(file_path).actual_file_type
    if kind not in {'doc', 'docx'}:
        raise ValueError('word_pdf_requires_actual_word_file')
    executable = office_executable()
    if executable is None:
        raise RuntimeError('libreoffice_unavailable')
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='word-input-', dir=output_dir,ignore_cleanup_errors=True) as name:
        temporary = Path(name)
        source = temporary / f'document.{kind}'
        shutil.copyfile(file_path, source)
        command = [executable, f'-env:UserInstallation={(temporary / "profile").as_uri()}',
                   '--headless', '--norestore', '--convert-to', 'pdf:writer_pdf_Export',
                   '--outdir', str(output_dir), str(source)]
        completed = _run_office_process(command, timeout=180)
        pdf = output_dir / 'document.pdf'
        if completed.returncode or not pdf.is_file():
            raise RuntimeError('word_pdf_conversion_failed:' + (completed.stderr or completed.stdout)[-500:])
        with fitz.open(pdf) as document:
            if not len(document) or document.is_encrypted:
                raise RuntimeError('word_pdf_empty_or_encrypted')
        if kind == 'doc':
            converted = _run_office_process([executable, command[1], '--headless', '--norestore',
                '--convert-to', 'docx', '--outdir', str(temporary), str(source)],
                timeout=180)
            normalized = temporary / 'document.docx'
            if converted.returncode or not normalized.is_file():
                raise RuntimeError('word_scan_inventory_unavailable')
        else:
            normalized = source
        restore_scan_pages(normalized, pdf)
        return pdf


def libreoffice_engine_version() -> str:
    executable = office_executable()
    if executable is None:
        return "unavailable"
    try:
        args=[executable,'--version']
        portable=Path(__file__).resolve().parents[3]/'runtime'/'tools'/'libreoffice'
        if Path(executable).resolve().is_relative_to(portable.resolve()):
            profile=portable/'version-profile'
            args.insert(1,f'-env:UserInstallation={profile.as_uri()}')
        output = subprocess.run(
            args, check=False, capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt'else 0,
        )
        match = re.search(r'\b\d+(?:\.\d+)+\b', output.stdout or output.stderr)
        return match.group(0) if match else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def parse_with_libreoffice(
    file_path: Path,
    output_dir: Path | None = None,
    *,
    profile_version: str = "legacy",
    settings=None,
) -> ParseResult:
    """Use PDF OCR for Word, retaining the existing XLS-to-XLSX fallback."""
    executable = office_executable()
    if executable is None:
        raise RuntimeError("libreoffice_unavailable")
    from oa_knowledge.parsers.format_router import detect_format
    if detect_format(file_path).actual_file_type in {'doc', 'docx'}:
        if settings is None or output_dir is None:
            raise ValueError('word_pdf_requires_local_settings_and_output')
        from oa_knowledge.parsers.mineru_parser import parse_with_mineru
        pdf = word_to_pdf(file_path, Path(output_dir) / 'word-pdf')
        result = parse_with_mineru(pdf, settings, Path(output_dir) / 'ocr',
                                  profile_version=profile_version, parse_method='ocr')
        restore_terminal_text(pdf, result.output_path)
        # Keep the converted PDF beside the product for page-level verification.
        shutil.copy2(pdf, result.output_path.parent / 'word-source.pdf')
        if pdf.with_name('word-layout.pdf').exists():
            shutil.copy2(pdf.with_name('word-layout.pdf'), result.output_path.parent / 'word-layout.pdf')
        return replace(result, engine='libreoffice', engine_version=libreoffice_engine_version())
    if detect_format(file_path).actual_file_type in {'ppt','pptx'}:
        if settings is None or output_dir is None:raise ValueError('presentation_pdf_requires_settings_and_output')
        from oa_knowledge.parsers.mineru_parser import parse_with_mineru
        output_dir=Path(output_dir).resolve();temporary=output_dir/'presentation-input';temporary.mkdir(parents=True,exist_ok=True)
        kind=detect_format(file_path).actual_file_type
        source=temporary/f'document.{kind}';shutil.copyfile(file_path,source)
        command=[executable,f'-env:UserInstallation={(temporary/"profile").as_uri()}','--headless','--norestore',
                 '--convert-to','pdf:impress_pdf_Export','--outdir',str(temporary),str(source)]
        completed=_run_office_process(command,timeout=180);pdf=temporary/'document.pdf'
        if completed.returncode or not pdf.is_file():raise RuntimeError('presentation_pdf_conversion_failed')
        result=parse_with_mineru(pdf,settings,output_dir/'ocr',profile_version=profile_version,parse_method='ocr')
        return replace(result,engine='libreoffice',engine_version=libreoffice_engine_version())
    from oa_knowledge.parsers.markitdown_parser import parse_with_markitdown

    with tempfile.TemporaryDirectory(prefix="oaradar-xls-",ignore_cleanup_errors=True) as temp_name:
        temporary = Path(temp_name)
        source=temporary/'document.xls';shutil.copyfile(file_path,source)
        command = [
            executable,
            f'-env:UserInstallation={(temporary/"profile").as_uri()}',
            "--headless",
            "--convert-to",
            "xlsx",
            "--outdir",
            str(temporary),
            str(source),
        ]
        completed = _run_office_process(command, timeout=120)
        converted = temporary / 'document.xlsx'
        if completed.returncode != 0 or not converted.is_file():
            raise RuntimeError("libreoffice_conversion_failed")
        result = parse_with_markitdown(
            converted, output_dir, profile_version=profile_version
        )
        return replace(result,engine='libreoffice',engine_version=libreoffice_engine_version())
