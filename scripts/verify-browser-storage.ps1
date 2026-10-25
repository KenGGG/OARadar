# Run as the same Windows user that saved the password in the OA Chrome window.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$auditCode = @'
from pathlib import Path
import json
import sqlite3
from oa_knowledge.config import load_settings
from oa_knowledge.collector.windows_credentials import load_credential, delete_credential

settings = load_settings(Path('config.yaml'))
origin = settings.browser.base_url.rstrip('/')
legacy_present = load_credential(origin) is not None
if legacy_present:
    delete_credential(origin)
legacy_remaining = load_credential(origin) is not None
database = settings.browser_profile_path / 'Default' / 'Login Data'
count = 0
if database.is_file():
    with sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
        count = connection.execute(
            'SELECT count(*) FROM logins WHERE signon_realm = ? '
            'AND blacklisted_by_user = 0 AND length(username_value) > 0 '
            'AND length(password_value) > 0', (origin + '/',)
        ).fetchone()[0]
print(json.dumps({
    'legacy_oaradar_credential_removed': legacy_present and not legacy_remaining,
    'legacy_oaradar_credential_remaining': legacy_remaining,
    'chrome_saved_oa_login_count': count,
}, ensure_ascii=False))
raise SystemExit(1 if legacy_remaining else 0)
'@
Push-Location $projectRoot
try {
    & $pythonPath -X utf8 -c $auditCode
    if ($LASTEXITCODE -ne 0) { throw 'Browser storage verification failed.' }
} finally { Pop-Location }
