"""Process-wide network guard used by test.py for the offline baseline.
Any socket connect / DNS lookup raises NetworkBlocked (an OSError)."""
from __future__ import annotations

import contextlib
import socket

ACTIVE = False
_orig = {}


class NetworkBlocked(OSError):
    pass


def _blocked(*a, **k):
    raise NetworkBlocked("network access is blocked in the offline test baseline")


def install() -> None:
    global ACTIVE
    if ACTIVE:
        return
    _orig.update(connect=socket.socket.connect, connect_ex=socket.socket.connect_ex,
                 getaddrinfo=socket.getaddrinfo, create_connection=socket.create_connection)
    socket.socket.connect = _blocked          # type: ignore[method-assign]
    socket.socket.connect_ex = _blocked       # type: ignore[method-assign]
    socket.getaddrinfo = _blocked             # type: ignore[assignment]
    socket.create_connection = _blocked       # type: ignore[assignment]
    ACTIVE = True


def uninstall() -> None:
    global ACTIVE
    if not ACTIVE:
        return
    socket.socket.connect = _orig["connect"]
    socket.socket.connect_ex = _orig["connect_ex"]
    socket.getaddrinfo = _orig["getaddrinfo"]
    socket.create_connection = _orig["create_connection"]
    ACTIVE = False


@contextlib.contextmanager
def blocked():
    was = ACTIVE
    install()
    try:
        yield
    finally:
        if not was:
            uninstall()
