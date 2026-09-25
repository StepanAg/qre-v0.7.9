"""Keep the computer awake while the monitor runs (local long runs).
Windows: SetThreadExecutionState; macOS: `caffeinate`; Linux: no-op (use systemd-inhibit if needed).
Only the idle SLEEP is prevented; the screen may still turn off."""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from typing import Iterator

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def keep_awake(enabled: bool = True) -> Iterator[str]:
    if not enabled:
        yield "disabled"
        return
    if sys.platform == "win32":
        import ctypes
        ok = ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)  # type: ignore[attr-defined]
        try:
            yield "windows: sleep prevented" if ok else "windows: could not prevent sleep"
        finally:
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        try:
            proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            yield "macos: caffeinate not available"
            return
        try:
            yield "macos: sleep prevented (caffeinate)"
        finally:
            proc.terminate()
        return
    yield "linux: not managed (disable suspend or use systemd-inhibit)"
