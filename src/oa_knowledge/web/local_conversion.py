"""Read registered local conversion progress without contacting OA."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sqlalchemy import select
from oa_knowledge.db.models import ArchivedFile, OAManifestItem


def process_running(pid: int) -> bool | None:
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return None if ctypes.get_last_error() == 5 else False
        try:
            code = wintypes.DWORD()
            return code.value == 259 if api.GetExitCodeProcess(handle, ctypes.byref(code)) else None
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def read_local_conversion(settings, session=None):
    registration = settings.runtime_root / 'local-conversion.json'
    if not registration.exists():
        return None
    try:
        reference = json.loads(registration.read_text(encoding='utf8'))
        root = settings.runtime_root.resolve()
        path = (root / reference['progress_relpath']).resolve()
        path.relative_to(root)
        raw = json.loads(path.read_text(encoding='utf8'))
        processed, total = raw.get('processed', 0), raw.get('total', 0)
        if any(type(v) is not int or v < 0 for v in (processed, total)) or processed > total:
            raise ValueError('invalid progress')
        pid = raw.get('pid')
        if type(pid) is not int or pid <= 0:
            raise ValueError('invalid worker PID')
        updated = datetime.fromisoformat(raw['updated_at'])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        stage = raw.get('stage', 'unknown')
        alive = process_running(pid)
        finished = stage in {'local_conversion_finished', 'sequence_finished'}
        status = 'finished' if finished else 'blocked' if stage == 'blocked' else 'stopped' if alive is False else 'running' if alive else 'unknown'
        result = dict(available=True, live=True, status=status, stage=stage,
                      scope_done_items=total, processed=processed, updated_at=updated.isoformat(),
                      stale=age > 900, current_title=None, current_attachment=None,
                      parser=raw.get('parser'), actual_format=raw.get('actual_format'))
        if session is not None:
            if raw.get('current_item'):
                row = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == str(raw['current_item'])))
                result['current_title'] = row.title if row else None
            if raw.get('current_file_id'):
                source = session.get(ArchivedFile, int(raw['current_file_id']))
                result['current_attachment'] = source.original_name if source else None
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return dict(available=False, live=True, message='当前转换进度暂不可读，请稍后刷新')
