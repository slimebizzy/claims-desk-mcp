"""Shared durable state.

Replicas keep nothing in process memory that a later request depends on. Claims,
idempotency results, approval requests, approval records and the audit trail all
live in one SQLite file (WAL mode), so any replica can serve any request.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from . import config

SEED = config.ROOT / "data" / "seed_claims.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY, data TEXT NOT NULL, reserve INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS reserve_history (
  claim_id TEXT, at REAL, actor TEXT, old INTEGER, new INTEGER, reason TEXT, approval_id TEXT, replica TEXT
);
CREATE TABLE IF NOT EXISTS idempotency (
  actor TEXT, key TEXT, digest TEXT, result TEXT, at REAL, PRIMARY KEY (actor, key)
);
CREATE TABLE IF NOT EXISTS approval_requests (
  id TEXT PRIMARY KEY, actor TEXT, tool TEXT, args TEXT, digest TEXT, audience TEXT,
  claim_id TEXT, current_reserve INTEGER, amount INTEGER, actor_limit INTEGER, required_level INTEGER, rule TEXT,
  status TEXT, created REAL, expires REAL, decided_by TEXT, decided_at REAL, decision_note TEXT, approval_id TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, request_id TEXT, approver TEXT, approver_level INTEGER, actor TEXT, tool TEXT,
  digest TEXT, audience TEXT, expires REAL, single_use INTEGER, used_at REAL, signature TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, replica TEXT, surface TEXT, rpc_id TEXT, trace_id TEXT,
  method TEXT, tool TEXT, actor TEXT, issuer TEXT, audience TEXT, decision TEXT, reason TEXT,
  args_digest TEXT, args_redacted TEXT, result_digest TEXT, outcome TEXT, latency_ms REAL
);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init(path: Optional[Path] = None, reset: bool = False) -> None:
    conn = connect(path)
    try:
        if reset:
            for t in ("claims", "reserve_history", "idempotency", "approval_requests", "approvals", "audit"):
                conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.executescript(SCHEMA)
        if not conn.execute("SELECT 1 FROM claims LIMIT 1").fetchone():
            for c in json.loads(SEED.read_text()):
                conn.execute("INSERT INTO claims (id, data, reserve) VALUES (?, ?, ?)",
                             (c["id"], json.dumps(c), c["reserve"]))
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction that takes the lock up front, so two replicas
    changing the same claim serialize instead of losing an update."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def get_claim(conn: sqlite3.Connection, claim_id: str) -> Optional[dict]:
    row = conn.execute("SELECT data, reserve, version FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if not row:
        return None
    c = json.loads(row["data"])
    c["reserve"], c["version"] = row["reserve"], row["version"]
    return c


def list_claims(conn: sqlite3.Connection) -> List[dict]:
    return [get_claim(conn, r["id"]) for r in conn.execute("SELECT id FROM claims ORDER BY id")]


def now() -> float:
    return time.time()
