"""Approval records.

An approval is not a scope and not a chat message. It is a signed record bound
to one actor, one tool, one argument digest and one audience, with an expiry and
single use. Change one argument and the digest no longer matches.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from typing import Optional

from . import config, store
from .protocol import canonical


class ApprovalError(Exception):
    pass


def bound_args(args: dict) -> dict:
    """What an approval binds to: everything except the idempotency key, so a
    client retry with a fresh key still matches the approved action."""
    return {k: v for k, v in args.items() if k != "idempotency_key"}


def _sign(rec: dict) -> str:
    fields = {k: rec[k] for k in ("id", "request_id", "approver", "actor", "tool", "digest", "audience", "expires")}
    return hmac.new(config.APPROVAL_SECRET.encode(), canonical(fields).encode(), hashlib.sha256).hexdigest()


def create_request(conn: sqlite3.Connection, *, actor: str, tool: str, args: dict, digest: str, audience: str,
                   claim: dict, amount: int, actor_limit: Optional[int], required_level: int, rule: str) -> dict:
    # One pending request per exact action; asking again returns the same one.
    row = conn.execute("SELECT * FROM approval_requests WHERE actor=? AND digest=? AND audience=? AND status='pending' "
                       "AND expires > ?", (actor, digest, audience, store.now())).fetchone()
    if row:
        return dict(row)
    rid = "req_" + secrets.token_hex(6)
    now = store.now()
    conn.execute(
        "INSERT INTO approval_requests (id, actor, tool, args, digest, audience, claim_id, current_reserve, amount, "
        "actor_limit, required_level, rule, status, created, expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?)",
        (rid, actor, tool, canonical(bound_args(args)), digest, audience, claim["id"], claim["reserve"], amount,
         actor_limit, required_level, rule, now, now + config.REQUEST_TTL_S))
    return dict(conn.execute("SELECT * FROM approval_requests WHERE id=?", (rid,)).fetchone())


def decide(conn: sqlite3.Connection, request_id: str, approver: str, approver_level: int, approve: bool,
           note: str = "") -> dict:
    with store.tx(conn):
        req = conn.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone()
        if not req:
            raise ApprovalError("No such request")
        if req["status"] != "pending" or req["expires"] <= store.now():
            raise ApprovalError(f"Request is {req['status'] if req['status'] != 'pending' else 'expired'}")
        if approver == req["actor"]:
            raise ApprovalError("You can't approve your own request")
        if approve and approver_level < req["required_level"]:
            title = config.LEVEL_TITLES.get(req["required_level"], "a higher level")
            raise ApprovalError(f"This amount needs {title} authority")
        now = store.now()
        approval_id = None
        if approve:
            rec = {"id": "apr_" + secrets.token_hex(8), "request_id": request_id, "approver": approver,
                   "actor": req["actor"], "tool": req["tool"], "digest": req["digest"], "audience": req["audience"],
                   "expires": now + config.APPROVAL_TTL_S}
            conn.execute(
                "INSERT INTO approvals (id, request_id, approver, approver_level, actor, tool, digest, audience, "
                "expires, single_use, signature) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (rec["id"], request_id, approver, approver_level, rec["actor"], rec["tool"], rec["digest"],
                 rec["audience"], rec["expires"], _sign(rec)))
            approval_id = rec["id"]
        conn.execute("UPDATE approval_requests SET status=?, decided_by=?, decided_at=?, decision_note=?, approval_id=? "
                     "WHERE id=?", ("approved" if approve else "denied", approver, now, note[:500], approval_id,
                                    request_id))
        return dict(conn.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone())


def verify(conn: sqlite3.Connection, approval_id: str, *, actor: str, tool: str, digest: str, audience: str,
           amount: int) -> sqlite3.Row:
    """Check a presented approval against the exact call. Must run inside the
    caller's write transaction so the single-use mark is atomic with the write."""
    rec = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    if not rec:
        raise ApprovalError("Unknown approval")
    fields = {k: rec[k] for k in ("id", "request_id", "approver", "actor", "tool", "digest", "audience", "expires")}
    if not hmac.compare_digest(_sign(fields), rec["signature"]):
        raise ApprovalError("Approval signature is invalid")
    if rec["used_at"] is not None:
        raise ApprovalError("Approval was already used")
    if rec["expires"] <= store.now():
        raise ApprovalError("Approval expired")
    if rec["actor"] != actor:
        raise ApprovalError("Approval was granted to a different user")
    if rec["tool"] != tool or rec["audience"] != audience:
        raise ApprovalError("Approval is for a different action")
    if rec["digest"] != digest:
        raise ApprovalError("Arguments differ from what was approved")
    limit = config.authority_limit(rec["approver_level"])
    if limit is not None and amount > limit:
        raise ApprovalError("Approver's authority does not cover this amount")
    return rec


def mark_used(conn: sqlite3.Connection, approval_id: str) -> None:
    conn.execute("UPDATE approvals SET used_at=? WHERE id=? AND used_at IS NULL", (store.now(), approval_id))


def get_request(conn: sqlite3.Connection, request_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone()
    return dict(row) if row else None
