"""Single-writer discipline for SQLite.

Every write goes through ONE SerializedWriter owning ONE connection. A process-
wide lock serialises transactions (BEGIN IMMEDIATE), so concurrent producers
(future WS streams) cannot hit 'database is locked' against each other.

Today ingestion is single-threaded, so this lock-based writer is sufficient.
When WS ingestion arrives, producers keep calling the same API; the
implementation can switch to a dedicated writer thread + queue without changing
callers (see docs/V070_MARKET_DATA.md)."""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from typing import Iterator

from app.core.errors import StorageError


class SerializedWriter:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as e:
                raise StorageError(f"cannot begin write transaction: {e}") from e
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except sqlite3.Error as e:
                self._rollback()
                raise StorageError(f"write failed: {e}") from e
            except BaseException:
                self._rollback()
                raise

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self.conn
            except sqlite3.Error as e:
                raise StorageError(f"read failed: {e}") from e

    def _rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
