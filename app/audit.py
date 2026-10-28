"""Audit trail. Every request that reaches a surface gets one row: who, which
issuer and audience, what tool, what policy decided, how long it took and how
it ended. Arguments are stored redacted, results only as a digest. The trail
lives outside anything a model sees."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional

from . import config, store
from .protocol import canonical, digest

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b")
SENSITIVE_KEYS = {"phone", "email", "claimant", "name", "reason"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted:" + digest(v)[7:19] + "]" if k in SENSITIVE_KEYS else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _PHONE.sub("[phone]", _EMAIL.sub("[email]", value))
    return value


def traceparent_id(header: Optional[str]) -> Optional[str]:
    if header:
        parts = header.split("-")
        if len(parts) == 4 and len(parts[1]) == 32:
            return parts[1]
    return None


def record(conn: sqlite3.Connection, *, surface: str, rpc_id: Any, trace_id: Optional[str], method: str,
           tool: Optional[str] = None, caller=None, decision: str = "", reason: str = "", args: Any = None,
           result: Any = None, outcome: str = "", latency_ms: float = 0.0) -> None:
    conn.execute(
        "INSERT INTO audit (at, replica, surface, rpc_id, trace_id, method, tool, actor, issuer, audience, decision, "
        "reason, args_digest, args_redacted, result_digest, outcome, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (store.now(), config.REPLICA, surface, None if rpc_id is None else str(rpc_id), trace_id, method, tool,
         getattr(caller, "sub", None), getattr(caller, "issuer", None), getattr(caller, "audience", None), decision,
         reason[:300], digest(args) if args is not None else None,
         canonical(redact(args)) if args is not None else None,
         digest(result) if result is not None else None, outcome, round(latency_ms, 2)))


def recent(conn: sqlite3.Connection, limit: int = 100) -> list:
    rows = conn.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["args_redacted"] = json.loads(d["args_redacted"]) if d["args_redacted"] else None
        out.append(d)
    return out
