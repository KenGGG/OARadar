"""OA credentials in the current user's Windows Credential Manager."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from urllib.parse import urlsplit

from .credentials import SavedCredential


def credential_target(origin: str) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("OA address must be an HTTP(S) origin without credentials")
    return f"OARadar/OA/{parsed.scheme}://{parsed.netloc.lower()}"


class Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _api():
    if os.name != "nt":
        raise ValueError("Windows credential storage is unavailable on this platform")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    return api


def save_credential(origin: str, username: str, password: str) -> None:
    if not username.strip() or not password or "\0" in username or "\0" in password:
        raise ValueError("username and password are required")
    encoded = password.encode("utf-16-le")
    if len(encoded) > 2560:
        raise ValueError("password exceeds Windows credential storage limit")
    api = _api()
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    value = Credential(Type=1, TargetName=credential_target(origin), Comment="OARadar local OA login",
                       CredentialBlobSize=len(encoded), CredentialBlob=blob, Persist=2, UserName=username)
    try:
        if not api.CredWriteW(ctypes.byref(value), 0):
            raise ValueError("Windows credential storage write failed")
    finally:
        ctypes.memset(blob, 0, len(encoded))


def load_credential(origin: str) -> SavedCredential | None:
    if os.name != "nt":
        return None
    api = _api()
    pointer = ctypes.POINTER(Credential)()
    if not api.CredReadW(credential_target(origin), 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:
            return None
        raise ValueError("Windows credential storage read failed")
    try:
        value = pointer.contents
        password = ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("utf-16-le")
        return SavedCredential(value.UserName, password)
    finally:
        api.CredFree(pointer)


def delete_credential(origin: str) -> None:
    api = _api()
    if not api.CredDeleteW(credential_target(origin), 1, 0) and ctypes.get_last_error() != 1168:
        raise ValueError("Windows credential storage delete failed")
