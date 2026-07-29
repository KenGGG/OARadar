import sqlite3

from oa_knowledge.collector.credentials import _decrypt_linux_v10, load_chrome_saved_credential


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
