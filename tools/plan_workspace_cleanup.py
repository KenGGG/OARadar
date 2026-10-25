"""Plan conservative local cleanup; deletion is performed by native PowerShell."""
import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.config import load_settings


def tree_info(path):
    size, newest, links = 0, 0, False
    if path.is_file():
        info = path.stat()
        return info.st_size, info.st_mtime, path.is_symlink()
    for base, dirs, files in os.walk(path, followlinks=False):
        base = Path(base)
        for name in list(dirs):
            child = base / name
            if child.is_symlink() or child.is_junction():
                links = True
                dirs.remove(name)
        for name in files:
            child = base / name
            if child.is_symlink():
                links = True
                continue
            try:
                info = child.stat()
                size += info.st_size
                newest = max(newest, info.st_mtime)
            except OSError:
                links = True
    return size, newest, links


def main():
    root = Path.cwd().resolve()
    settings = load_settings(root / 'config.yaml')
    tracked = subprocess.check_output(['git', 'ls-files', '-z']).decode('utf8').split('\0')
    protected = [root / p for p in tracked if p]
    plan, skipped = [], []
    cutoff = time.time() - 2 * 86400
    def add(path, reason, require_old=False):
        path = path.resolve()
        if not path.is_relative_to(root) or any(t == path or t.is_relative_to(path) for t in protected):
            raise ValueError('Cleanup target is outside workspace or contains tracked files')
        size, newest, links = tree_info(path)
        if links or (require_old and newest > cutoff):
            skipped.append({'path': str(path), 'reason': 'recent_or_linked'})
            return
        plan.append({'path': str(path), 'reason': reason, 'bytes': size})
    for name in ['.pytest_cache', '.coverage', '.ruff_cache']:
        path = root / name
        if path.exists():
            add(path, 'regenerable_test_cache')
    for top in ['src', 'tests', 'tools', 'scripts']:
        for path in (root / top).rglob('__pycache__'):
            add(path, 'python_bytecode_cache')
    runtime = root / 'runtime'
    for path in runtime.iterdir():
        if path.is_dir() and path.name.startswith(('test-', 'repair-probe', 'publication-acl-smoke', 'longpath-check')):
            # Preserve any test profile that might hold locally saved logins.
            sensitive = any(p.name in {'Login Data', 'Cookies', 'Local State'} or p.name.startswith('browser-profile') for p in path.rglob('*'))
            if sensitive:
                skipped.append({'path': str(path), 'reason': 'possible_browser_profile'})
            else:
                add(path, 'old_test_scratch', require_old=True)
        elif path.is_file() and path.suffix == '.log':
            add(path, 'old_log', require_old=True)
    installer = runtime / 'tools/downloads'
    if (runtime / 'tools/libreoffice').is_dir() and installer.is_dir():
        add(installer, 'installed_libreoffice_download_cache', require_old=True)
    db = sqlite3.connect(settings.database_path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    refs = set()
    for table in ['parse_jobs', 'parse_artifacts']:
        cols = [r[1] for r in db.execute('pragma table_info(' + table + ')')]
        for col in ['output_relpath', 'markdown_relpath', 'assets_relpath']:
            if col not in cols:
                continue
            for (value,) in db.execute('select ' + col + ' from ' + table + ' where ' + col + ' is not null'):
                parts = Path(value.replace('\\', '/')).parts
                if 'parse' in parts and len(parts) > parts.index('parse') + 1:
                    refs.add(parts[parts.index('parse') + 1])
    orphan_names = []
    for path in (settings.parse_work_root / 'parse').iterdir():
        if not path.is_dir() or path.name in refs:
            continue
        job = db.execute('SELECT p.status,f.local_relpath,f.download_status,f.sha256 FROM parse_jobs p JOIN files f ON f.id=p.file_id WHERE p.id=?', (path.name,)).fetchone()
        if not job or job['status'] != 'completed' or job['download_status'] != 'verified' or not job['local_relpath']:
            continue
        source = (settings.data_root / job['local_relpath']).resolve()
        if not source.is_relative_to(settings.data_root.resolve() / 'originals') or not source.is_file():
            continue
        if sha256_file(source) != job['sha256']:
            continue
        before = len(plan)
        add(path, 'unreferenced_completed_parse_scratch', require_old=True)
        if len(plan) > before:
            orphan_names.append(path.name)
    db.close()
    uv = root / '.uv-cache'
    uv_size = tree_info(uv)[0] if uv.exists() else 0
    status = subprocess.check_output(['git', 'status', '--porcelain', '-z']).decode('utf8')
    report = {'workspace': str(root), 'planned': plan, 'skipped': skipped,
              'planned_bytes': sum(r['bytes'] for r in plan), 'uv_cache_bytes_before': uv_size,
              'disk_free_before': shutil.disk_usage(root).free, 'git_status_before': status,
              'orphan_parse_directories': orphan_names,
              'protected': ['data', 'runtime/state', 'browser profiles', 'active parse artifacts', '.venv', '.mineru', 'source and uncommitted work']}
    output = settings.runtime_root / 'workspace-cleanup-report.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'targets': len(plan), 'bytes': report['planned_bytes'],
                      'orphan_parse_directories': len(orphan_names), 'uv_cache_bytes': uv_size, 'report': str(output)}))


if __name__ == '__main__':
    main()
