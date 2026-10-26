"""Minimal inotify wrapper so waiting on files never needs a polling loop."""

from __future__ import annotations

import ctypes
import os
import struct
from pathlib import Path

CREATE = 0x00000100
MOVED_TO = 0x00000080
MODIFY = 0x00000002
CLOSE_WRITE = 0x00000008
DELETE = 0x00000200
ATTRIB = 0x00000004
ISDIR = 0x40000000
_EVENT = struct.Struct("iIII")

_libc = ctypes.CDLL(None, use_errno=True)
_libc.inotify_init1.argtypes = [ctypes.c_int]
_libc.inotify_init1.restype = ctypes.c_int
_libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_libc.inotify_add_watch.restype = ctypes.c_int
_libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
_libc.inotify_rm_watch.restype = ctypes.c_int


def watch(directory: Path, mask: int) -> int:
    """Return a nonblocking inotify fd watching one directory."""
    fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1")
    if _libc.inotify_add_watch(fd, os.fsencode(directory), mask) < 0:
        code = ctypes.get_errno()
        os.close(fd)
        raise OSError(code, "inotify_add_watch " + str(directory))
    return fd


class Watcher:
    """One nonblocking inotify fd over many directories, reporting (path, mask) per event."""

    def __init__(self) -> None:
        self.fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self.directories: dict[int, Path] = {}

    def add(self, directory: Path, mask: int) -> None:
        wd = _libc.inotify_add_watch(self.fd, os.fsencode(directory), mask)
        if wd < 0:
            raise OSError(ctypes.get_errno(), "inotify_add_watch " + str(directory))
        self.directories[wd] = directory

    def remove(self, directory: Path) -> None:
        """Stop watching `directory` (a no-op when it is not watched or already gone)."""
        for wd, known in list(self.directories.items()):
            if known == directory:
                del self.directories[wd]
                _libc.inotify_rm_watch(self.fd, wd)

    def read(self) -> list[tuple[Path, int]]:
        """Every queued event; an empty list when none is queued."""
        events = []
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return events
            offset = 0
            while offset < len(data):
                wd, mask, _cookie, length = _EVENT.unpack_from(data, offset)
                name = data[offset + _EVENT.size : offset + _EVENT.size + length].rstrip(b"\0")
                offset += _EVENT.size + length
                directory = self.directories.get(wd)
                if directory is not None:
                    events.append((directory / os.fsdecode(name) if name else directory, mask))

    def close(self) -> None:
        os.close(self.fd)


def drain(fd: int) -> None:
    try:
        while os.read(fd, 65536):
            pass
    except BlockingIOError:
        return


def wait_for_path(path: Path, timeout: float) -> bool:
    """Block until path exists, woken by directory events, bounded by timeout seconds."""
    import select
    import time

    fd = watch(path.parent, CREATE | MOVED_TO)
    try:
        deadline = time.monotonic() + timeout
        while not path.exists():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if select.select([fd], [], [], remaining)[0]:
                drain(fd)
        return True
    finally:
        os.close(fd)
