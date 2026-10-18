import subprocess
import sys

from oa_knowledge.platform_lock import lock_exclusive


def test_lock_excludes_other_process_and_releases_on_close(tmp_path):
    path = tmp_path / "worker.lock"
    code = (
        "import sys; from oa_knowledge.platform_lock import lock_exclusive; "
        "f = open(sys.argv[1], 'a+'); lock_exclusive(f.fileno())"
    )
    with path.open("a+") as file:
        lock_exclusive(file.fileno())
        blocked = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)
        assert blocked.returncode != 0
        assert b"BlockingIOError" in blocked.stderr
    released = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)
    assert released.returncode == 0, released.stderr.decode()
