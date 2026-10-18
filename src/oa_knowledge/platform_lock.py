"""Nonblocking process locks, released when the owning file is closed."""

from __future__ import annotations

import errno
import os


def lock_exclusive(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        # Windows locks a byte range. All workers contend for the first byte.
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BlockingIOError(errno.EAGAIN, "worker lock is held") from exc
            raise
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
