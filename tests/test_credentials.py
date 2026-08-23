import sqlite3

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
