"""Own a local MinerU process only for the duration of a parse."""
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
import os
import signal
import subprocess
import time

from oa_knowledge.platform_lock import lock_exclusive


ROOT = Path(__file__).resolve().parents[3]


def python_path():
    return ROOT / '.mineru' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def installed_version():
    result = subprocess.run(
        [str(python_path()), '-c', 'from importlib.metadata import version; print(version("mineru"))'],
        capture_output=True, text=True, check=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    return result.stdout.strip()


def _stop(process):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=30, check=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=30)


@contextmanager
def runtime(settings):
    if not settings.mineru.on_demand:
        yield
        return
    from oa_knowledge.parsers.mineru_parser import _health_payload, MineruResponseError
    if not python_path().is_file():
        raise RuntimeError('Install local MinerU in .mineru before enabling on_demand')
    settings.runtime_root.mkdir(parents=True, exist_ok=True)
    with (settings.runtime_root / 'mineru-runtime.lock').open('a+b') as lock:
        deadline = time.monotonic() + settings.mineru.read_timeout_seconds
        while True:
            try:
                lock_exclusive(lock.fileno())
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('MinerU runtime is busy')
                time.sleep(0.5)
        try:
            _health_payload(settings)
        except MineruResponseError:
            pass
        else:
            raise RuntimeError('Stop the standalone MinerU service before using on_demand')
        address = urlsplit(settings.mineru.api_url)
        env = os.environ.copy()
        env.update(MINERU_HOME=str(ROOT / 'data/mineru'), MINERU_MODEL_SOURCE='local')
        command = [str(python_path()), str(ROOT / 'scripts/mineru-local.py'),
                   'api-server', '--host', address.hostname, '--port', str(address.port or 80),
                   '--tier', settings.mineru.tier, '--concurrency', '1',
                   '--disable-image-analysis', '--upload-dir', str(ROOT / 'data/mineru/uploads')]
        process = subprocess.Popen(command, cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            start_new_session=os.name != 'nt')
        try:
            deadline = time.monotonic() + settings.mineru.startup_timeout_seconds
            while True:
                if process.poll() is not None:
                    raise RuntimeError('MinerU exited during startup')
                try:
                    _health_payload(settings)
                    break
                except MineruResponseError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('MinerU startup timed out')
                    time.sleep(1)
            yield
        finally:
            if process.poll() is None:
                _stop(process)
