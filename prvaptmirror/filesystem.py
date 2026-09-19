"""Shared Linux filesystem primitives for publishing and maintenance."""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(path: Path, *, shared: bool = False, blocking: bool = True):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        # Host maintenance often runs as root against a UID-1000 bind mount.
        # Creating a root-only lock there must not prevent the app restarting.
        if os.geteuid() == 0:
            owner = path.parent.stat()
            os.fchown(fd, owner.st_uid, owner.st_gid)
        os.fchmod(fd, 0o600)
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(fd, flags | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _renameat2(a: str, b: str, flags: int) -> None:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    exchange = libc.renameat2
    exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    exchange.restype = ctypes.c_int
    if exchange(-100, os.fsencode(a), -100, os.fsencode(b), flags) != 0:
        err = ctypes.get_errno()
        raise OSError(err, "atomic rename failed: " + os.strerror(err), a)


def rename_exchange(a: str, b: str) -> None:
    _renameat2(a, b, 2)  # RENAME_EXCHANGE


def rename_noreplace(a: str, b: str) -> None:
    _renameat2(a, b, 1)  # RENAME_NOREPLACE
