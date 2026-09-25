from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.core.errors import StorageError

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def connect(path: Path | str, *, shared: bool = False) -> sqlite3.Connection:
    """shared=True allows the connection to be used from several threads; it must
    then only be used through app.storage.writer.SerializedWriter."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None,  # explicit transactions
                           check_same_thread=not shared)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending *.sql migrations in order. An already-applied migration whose
    file content changed is an error (history must not be rewritten)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {r["version"]: r["checksum"] for r in conn.execute("SELECT * FROM schema_migrations")}
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        raise StorageError(f"no migrations found in {migrations_dir}")
    newly: list[str] = []
    for f in files:
        sql = f.read_text(encoding="utf-8")
        version = f.stem
        cs = _checksum(sql)
        if version in applied:
            if applied[version] != cs:
                raise StorageError(f"migration {version} was modified after being applied")
            continue
        try:
            conn.execute("BEGIN")
            for stmt in _split(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?,?,?)",
                (version, cs, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise StorageError(f"migration {version} failed: {e}") from e
        newly.append(version)
    return newly


def _split(sql: str) -> list[str]:
    """Split on ';' while keeping CREATE TRIGGER ... END; blocks intact."""
    stmts, buf = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buf.append(line)
        joined = "\n".join(buf).strip()
        if sqlite3.complete_statement(joined):
            stmts.append(joined)
            buf = []
    if buf and "\n".join(buf).strip():
        raise StorageError("incomplete SQL statement at end of migration")
    return stmts


def bootstrap(path: Path | str, *, shared: bool = False) -> sqlite3.Connection:
    conn = connect(path, shared=shared)
    migrate(conn)
    return conn
