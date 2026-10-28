"""Tool surface and the per-call policy decision.

Scopes say what kind of thing a caller may do. Policy decides whether this
caller may do this exact thing to this claim, here, now: assignment, authority
limits from the Claims Handling Guidelines, file-note rules and approvals.
Annotations describe risk to clients; they are never trusted for enforcement.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import approvals, config, store
from .auth import Caller
from .ingest import load_corpus
from .protocol import META_APPROVAL, digest, validate
from .retrieval import Retriever

CLAIM_ID = {"type": "string", "pattern": r"CL-\d{4}", "description": "Claim number, for example CL-1042."}

TOOL_DEFS: Dict[str, dict] = {
    "claim.get": {
        "title": "Get claim",
        "description": "Look up one claim: policy, loss, peril, assigned adjuster, current reserve and SIU status. "
                       "Claimant contact details are masked unless you are assigned to the claim.",
        "inputSchema": {"type": "object", "properties": {"claim_id": CLAIM_ID},
                        "required": ["claim_id"], "additionalProperties": False},
        "outputSchema": {"type": "object", "required": ["id", "reserve", "adjuster"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "scope": "claims:read",
    },
    "claim.set_reserve": {
        "title": "Set claim reserve",
        "description": "Set the reserve on a claim, in whole dollars. Amounts above your authority level return "
                       "approval_required with a request id; once a supervisor approves, call again with the same "
                       "arguments and the approval id in _meta. A change of more than 50% needs a reason of at "
                       "least 20 characters. idempotency_key makes retries safe.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "claim_id": CLAIM_ID,
                "amount": {"type": "integer", "minimum": 0, "maximum": 5_000_000},
                "reason": {"type": "string", "maxLength": 1000},
                "idempotency_key": {"type": "string", "pattern": r"[A-Za-z0-9_-]{8,64}"},
            },
            "required": ["claim_id", "amount", "reason", "idempotency_key"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "required": ["status"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
        "scope": "claims:write",
    },
    "approval.status": {
        "title": "Check an approval request",
        "description": "Check a reserve approval request you made. When a supervisor approves it, the result includes "
                       "the approval id to send with your retry.",
        "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string", "pattern": r"req_[0-9a-f]{12}"}},
                        "required": ["request_id"], "additionalProperties": False},
        "outputSchema": {"type": "object", "required": ["status"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "scope": "claims:write",
    },
    "policy.search": {
        "title": "Search policy and guidelines",
        "description": "Search Cobalt Harbor policy forms and internal claims guidelines. Returns matching sections "
                       "with their section ids and text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 3, "maxLength": 300},
                "state": {"type": "string", "enum": ["CA", "TX", "OTHER"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "required": ["query"], "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "required": ["results"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "scope": "claims:read",
    },
}


def describe(name: str) -> dict:
    d = TOOL_DEFS[name]
    return {"name": name, **{k: d[k] for k in ("title", "description", "inputSchema", "outputSchema", "annotations")}}


def visible_tools(surface: str, caller: Caller) -> List[dict]:
    names = [n for n in config.SURFACES[surface]["tools"] if TOOL_DEFS[n]["scope"] in caller.scopes]
    return [describe(n) for n in sorted(names)]  # stable order keeps prompt caches warm


@dataclass
class Outcome:
    structured: dict
    is_error: bool = False
    decision: str = "allow"   # allow | deny | approval_required | invalid
    reason: str = ""
    redact: List[str] = field(default_factory=list)


_retriever: Optional[Retriever] = None


def retriever() -> Retriever:
    global _retriever
    if _retriever is None:
        _retriever = Retriever(load_corpus())
    return _retriever


def _mask(value: str) -> str:
    return value[:1] + "•" * max(len(value) - 3, 3) + value[-2:] if value else value


def _claim_view(claim: dict, caller: Caller) -> dict:
    view = {k: v for k, v in claim.items() if k != "claimant"}
    claimant = dict(claim["claimant"])
    if caller.sub != claim["adjuster"] and "claims:approve" not in caller.scopes:
        claimant["phone"], claimant["email"] = _mask(claimant["phone"]), _mask(claimant["email"])
    view["claimant"] = claimant
    view["adjuster_name"] = config.USERS.get(claim["adjuster"], {}).get("name", claim["adjuster"])
    return view


def claim_get(conn: sqlite3.Connection, caller: Caller, args: dict, meta: dict) -> Outcome:
    claim = store.get_claim(conn, args["claim_id"])
    if not claim:
        return Outcome({"status": "not_found", "claim_id": args["claim_id"]}, True, "deny", "unknown claim")
    return Outcome(_claim_view(claim, caller), redact=["claimant"])


def policy_search(conn: sqlite3.Connection, caller: Caller, args: dict, meta: dict) -> Outcome:
    state = args.get("state")
    hits = retriever().search(args["query"], role="adjuster", state=None if state == "OTHER" else state,
                              k=args.get("limit", 4))
    return Outcome({"results": [{"section": h.chunk.chunk_id, "title": h.chunk.title, "document": h.chunk.doc_title,
                                 "text": h.chunk.text} for h in hits]})


def actor_limit(caller: Caller, claim: dict) -> Tuple[Optional[int], str]:
    """Authority per Claims Handling Guidelines §2.1, with the §2.3 catastrophe
    raise for Level 1 on wind and hail claims not under fraud review.
    Returns the limit and the section it comes from."""
    if caller.level == 1 and claim.get("catastrophe") and claim.get("peril") in ("wind", "hail") and not claim.get("siu"):
        return config.CAT_LEVEL1_LIMIT, "§2.3, catastrophe authority"
    return config.authority_limit(caller.level), "§2.1"


def set_reserve(conn: sqlite3.Connection, caller: Caller, args: dict, meta: dict, audience: str) -> Outcome:
    actor = caller.sub
    key = args["idempotency_key"]
    bound = approvals.bound_args(args)
    args_digest = digest(bound)

    with store.tx(conn):
        prior = conn.execute("SELECT digest, result FROM idempotency WHERE actor=? AND key=?", (actor, key)).fetchone()
        if prior:
            if prior["digest"] != args_digest:
                return Outcome({"status": "rejected", "error": "idempotency_key was already used with different "
                                "arguments"}, True, "deny", "idempotency key reuse")
            replay = json.loads(prior["result"])
            replay["replayed"] = True
            return Outcome(replay, decision="allow", reason="idempotent replay")

        claim = store.get_claim(conn, args["claim_id"])
        if not claim:
            return Outcome({"status": "not_found", "claim_id": args["claim_id"]}, True, "deny", "unknown claim")
        if claim["adjuster"] != actor:
            return Outcome({"status": "denied", "error": f"{args['claim_id']} is assigned to "
                            f"{config.USERS.get(claim['adjuster'], {}).get('name', claim['adjuster'])}"},
                           True, "deny", "not assigned")

        old, new = claim["reserve"], args["amount"]
        if old and abs(new - old) / old > 0.5 and len(args["reason"].strip()) < 20:
            return Outcome({"status": "invalid", "error": "A reserve change of more than 50% needs a file note: give "
                            "a reason of at least 20 characters (Claims Handling Guidelines §2.2)."},
                           True, "invalid", "missing file note")

        limit, source = actor_limit(caller, claim)
        approval_id = (meta or {}).get(META_APPROVAL)
        used_approval = None
        if limit is not None and new > limit:
            if not approval_id:
                needed = max(config.level_needed(new), 3)
                title = config.LEVEL_TITLES.get(caller.level, "your level")
                article = "an" if title[0].lower() in "aeiou" else "a"
                rule = (f"${new:,} is above the ${limit:,} reserve authority of {article} {title} "
                        f"(Claims Handling Guidelines {source}).")
                req = approvals.create_request(conn, actor=actor, tool="claim.set_reserve", args=args,
                                               digest=args_digest, audience=audience, claim=claim, amount=new,
                                               required_level=needed, rule=rule)
                return Outcome({"status": "approval_required", "request_id": req["id"], "digest": args_digest,
                                "required_level": needed, "required_role": config.LEVEL_TITLES[needed], "rule": rule,
                                "console_url": f"{config.PUBLIC_BASE_URL}/console#{req['id']}",
                                "next": "After approval, call again with the same arguments and put the approval id "
                                        f"in params._meta[{META_APPROVAL!r}]."},
                               True, "approval_required", "above authority")
            try:
                approvals.verify(conn, approval_id, actor=actor, tool="claim.set_reserve", digest=args_digest,
                                 audience=audience, amount=new)
            except approvals.ApprovalError as e:
                return Outcome({"status": "denied", "error": str(e)}, True, "deny", f"approval: {e}")
            approvals.mark_used(conn, approval_id)
            used_approval = approval_id

        conn.execute("UPDATE claims SET reserve=?, version=version+1 WHERE id=?", (new, claim["id"]))
        conn.execute("INSERT INTO reserve_history VALUES (?,?,?,?,?,?,?,?)",
                     (claim["id"], store.now(), actor, old, new, args["reason"], used_approval, config.REPLICA))
        result = {"status": "updated", "claim_id": claim["id"], "previous_reserve": old, "reserve": new,
                  "version": claim["version"] + 1, "approval_id": used_approval}
        conn.execute("INSERT INTO idempotency VALUES (?,?,?,?,?)",
                     (actor, key, args_digest, json.dumps(result), store.now()))
        return Outcome(result, decision="allow", reason="approved" if used_approval else "within authority")


def approval_status(conn: sqlite3.Connection, caller: Caller, args: dict, meta: dict) -> Outcome:
    req = approvals.get_request(conn, args["request_id"])
    if not req or req["actor"] != caller.sub:  # don't confirm other people's requests exist
        return Outcome({"status": "not_found"}, True, "deny", "unknown request")
    status = req["status"] if not (req["status"] == "pending" and req["expires"] <= store.now()) else "expired"
    out = {"status": status, "request_id": req["id"], "claim_id": req["claim_id"], "amount": req["amount"],
           "decided_by": config.USERS.get(req["decided_by"] or "", {}).get("name"), "note": req["decision_note"]}
    if status == "approved":
        rec = conn.execute("SELECT expires, used_at FROM approvals WHERE id=?", (req["approval_id"],)).fetchone()
        out.update({"approval_id": req["approval_id"], "expires_at": rec["expires"], "used": rec["used_at"] is not None})
    return Outcome(out)


HANDLERS: Dict[str, Callable] = {
    "approval.status": approval_status,
    "claim.get": claim_get,
    "policy.search": policy_search,
}


def call(conn: sqlite3.Connection, surface: str, caller: Caller, name: str, args: dict, meta: dict) -> Outcome:
    """Validate arguments against the advertised schema, then run policy and the handler."""
    d = TOOL_DEFS[name]
    errs = validate(d["inputSchema"], args)
    if errs:
        return Outcome({"status": "invalid", "errors": errs}, True, "invalid", "; ".join(errs)[:200])
    if name == "claim.set_reserve":
        return set_reserve(conn, caller, args, meta, audience=caller.audience)
    return HANDLERS[name](conn, caller, args, meta)
