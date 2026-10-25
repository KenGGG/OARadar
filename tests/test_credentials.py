import sqlite3
import pytest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from oa_knowledge.collector.credentials import (
    _decrypt_linux_chrome_password,
    _decrypt_linux_v10,
    load_chrome_saved_credential,
)


def test_rejects_unknown_chrome_credential_format() -> None:
    try:
        _decrypt_linux_v10(b"unknown")
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("unsupported credential format was accepted")


def test_reads_login_database_while_writer_holds_exclusive_lock(tmp_path) -> None:
    database = tmp_path / "Default" / "Login Data"
    database.parent.mkdir()
    writer = sqlite3.connect(database)
    writer.execute("CREATE TABLE logins (username_value TEXT, password_value BLOB, signon_realm TEXT, blacklisted_by_user INTEGER, date_last_used INTEGER, date_created INTEGER)")
    writer.commit()
    writer.execute("BEGIN EXCLUSIVE")
    try:
        assert load_chrome_saved_credential(tmp_path, "https://oa.invalid") is None
    finally:
        writer.rollback()
        writer.close()


def test_decrypts_v11_chrome_password_with_keyring_secret() -> None:
    secret = b"synthetic-keyring-secret"
    password = "synthetic-password"
    import hashlib

    key = hashlib.pbkdf2_hmac("sha1", secret, b"saltysalt", 1, dklen=16)
    padder = PKCS7(128).padder()
    padded = padder.update(password.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    encrypted = b"v11" + encryptor.update(padded) + encryptor.finalize()

    assert _decrypt_linux_chrome_password(encrypted, keyring_secret=secret) == password


def test_windows_chrome_v10_uses_local_dpapi_key(tmp_path, monkeypatch):
    import base64
    import json
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from oa_knowledge.collector.credentials import _decrypt_windows_chrome_password
    key = b'k' * 32
    nonce = b'n' * 12
    wrapped = b'synthetic-wrapped-key'
    (tmp_path / 'Local State').write_text(json.dumps({
        'os_crypt': {'encrypted_key': base64.b64encode(b'DPAPI' + wrapped).decode()}
    }))
    def unprotect(payload, *, decrypt=False):
        assert payload == wrapped and decrypt
        return key
    monkeypatch.setattr('oa_knowledge.collector.session_cache._crypt', unprotect)
    value = b'v10' + nonce + AESGCM(key).encrypt(nonce, b'synthetic-password', None)
    assert _decrypt_windows_chrome_password(value, tmp_path) == 'synthetic-password'
    with pytest.raises(ValueError, match='unavailable'):
        _decrypt_windows_chrome_password(value[:-1] + bytes([value[-1] ^ 1]), tmp_path)
    with pytest.raises(ValueError, match='unsupported'):
        _decrypt_windows_chrome_password(b'v20' + value[3:], tmp_path)
