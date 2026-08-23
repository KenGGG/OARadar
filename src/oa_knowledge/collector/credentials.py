from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import sqlite3
from typing import Iterable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


@dataclass(frozen=True)
class SavedCredential:
    username: str = field(repr=False)
    password: str = field(repr=False)


def load_chrome_saved_credential(profile: Path, origin: str) -> SavedCredential | None:
    """Load one Linux Chrome mock-keychain login without logging its values."""
    database = profile / "Default" / "Login Data"
    if not database.is_file():
        return None
    # Chrome can hold an exclusive lock while updating its profile. Immutable
    # mode is safe here because this function performs one read-only snapshot.
    with sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True) as connection:
        row = connection.execute(
            """SELECT username_value, password_value
               FROM logins
               WHERE signon_realm = ? AND blacklisted_by_user = 0
               ORDER BY date_last_used DESC, date_created DESC
               LIMIT 1""",
            (f"{origin.rstrip('/')}/",),
        ).fetchone()
    if not row or not row[0] or not row[1]:
        return None
    password = _decrypt_linux_chrome_password(bytes(row[1]))
    return SavedCredential(username=str(row[0]), password=password)


def _decrypt_linux_v10(value: bytes) -> str:
    if not value.startswith(b"v10"):
        raise ValueError("unsupported Chrome credential format")
    return _decrypt_aes_cbc(value, b"peanuts")


def _decrypt_linux_chrome_password(
    value: bytes,
    *,
    keyring_secret: bytes | None = None,
) -> str:
    """Decrypt Chrome Linux v10/v11 values without persisting plaintext.

    v10 is the legacy mock-keychain format. v11 derives its key from Chrome's
    GNOME Secret Service entry.  An explicit secret is accepted for synthetic
    tests; production secrets are fetched into memory from the local keyring.
    """
    if value.startswith(b"v10"):
        return _decrypt_linux_v10(value)
    if not value.startswith(b"v11"):
        raise ValueError("unsupported Chrome credential format")
    secrets: Iterable[bytes] = (keyring_secret,) if keyring_secret is not None else _chrome_keyring_secrets()
    for secret in secrets:
        try:
            return _decrypt_aes_cbc(value, secret)
        except (UnicodeDecodeError, ValueError):
            continue
    raise ValueError("Chrome keyring credential is unavailable")


def _decrypt_aes_cbc(value: bytes, secret: bytes) -> str:
    key = hashlib.pbkdf2_hmac("sha1", secret, b"saltysalt", 1, dklen=16)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    padded = decryptor.update(value[3:]) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def _chrome_keyring_secrets() -> tuple[bytes, ...]:
    """Read Chrome's local encryption secrets from GNOME Secret Service."""
    try:
        import secretstorage

        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        return tuple(
            item.get_secret()
            for item in collection.search_items({"application": "chrome"})
            if not item.is_locked()
        )
    except Exception as exc:
        raise ValueError("Chrome keyring is unavailable") from exc
