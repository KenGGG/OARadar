"""Origin-bound browser cookies, encrypted for the local Windows user."""
import ctypes
import json
import os
from urllib.parse import urlsplit
from oa_knowledge.archive.writer import atomic_write_bytes


def _crypt(payload, *, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    function = api.CryptUnprotectData if decrypt else api.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('Local browser session encryption unavailable')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def save_session(settings, context):
    if os.name != 'nt':return
    origin = settings.browser.base_url.rstrip('/')
    # cookies(urls=...) includes only cookies applicable to this OA origin.
    data = json.dumps({'origin': origin, 'cookies': context.cookies([origin])}).encode('utf8')
    atomic_write_bytes(_crypt(data), settings.browser_profile_path, 'oa-session.dpapi')


def restore_session(settings, context):
    if os.name != 'nt':return
    path = settings.browser_profile_path / 'oa-session.dpapi'
    if not path.is_file():return
    try:
        state = json.loads(_crypt(path.read_bytes(), decrypt=True))
        origin = settings.browser.base_url.rstrip('/')
        host = urlsplit(origin).hostname
        if state.get('origin') != origin:return
        cookies = [c for c in state['cookies'] if c['domain'].lstrip('.') == host or host.endswith('.' + c['domain'].lstrip('.'))]
        context.add_cookies(cookies)
    except (OSError, ValueError, KeyError, TypeError):
        # A different Windows user cannot decrypt this session. Let the normal
        # local login flow request authentication instead of exposing secrets.
        return
