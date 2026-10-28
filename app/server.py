"""Streamable HTTP entry point: one POST endpoint per surface, one JSON response
per request, no sessions. Any replica can answer any request."""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response

from . import approvals, audit, config, store, tools
from .auth import AuthError, Caller, authenticate, mint, require_scope
from .protocol import (INTERNAL_ERROR, INVALID_PARAMS, METHOD_NOT_FOUND, PARSE_ERROR, RpcError, check_headers,
                       check_meta, check_shape, complete)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init()
    yield


app = FastAPI(title="Claims Desk MCP", docs_url=None, redoc_url=None, lifespan=lifespan)
STATIC = config.ROOT / "app" / "static"
CONSOLE_AUDIENCE = f"{config.PUBLIC_BASE_URL}/console"


def _json(body: Any, status: int = 200, headers: Optional[dict] = None) -> JSONResponse:
    h = {"X-Replica": config.REPLICA}
    h.update(headers or {})
    return JSONResponse(body, status_code=status, headers=h)


def discover_result(surface: str) -> dict:
    return complete({
        "supportedVersions": list(config.SUPPORTED_VERSIONS),
        "capabilities": {"tools": {"listChanged": False}},
        "instructions": "Claims tools for Cobalt Harbor Mutual (fictional). Reserve changes above your authority "
                        "need a supervisor's approval record.",
        "ttlMs": 3_600_000,
        "cacheScope": "public",
    }, surface)


@app.post("/mcp/{surface}")
async def mcp(surface: str, request: Request) -> Response:
    t0 = time.perf_counter()
    trace_id = audit.traceparent_id(request.headers.get("traceparent"))
    if surface not in config.SURFACES:
        return _json({"error": "not_found"}, 404)

    origin = request.headers.get("origin")
    if origin and origin not in config.ALLOWED_ORIGINS:
        return _json({"error": "forbidden_origin"}, 403)
    accept = request.headers.get("accept", "")
    if "application/json" not in accept or "text/event-stream" not in accept:
        return _json({"error": "not_acceptable", "detail": "Accept must include application/json and "
                      "text/event-stream"}, 406)
    raw = await request.body()
    # SQLite calls block, so the request runs on the thread pool, not the event loop.
    return await run_in_threadpool(_handle, surface, request.headers, raw, trace_id, t0)


def _handle(surface: str, headers, raw: bytes, trace_id: Optional[str], t0: float) -> Response:
    rpc_id: Any = None
    method = "?"
    tool_name: Optional[str] = None
    caller: Optional[Caller] = None
    conn = store.connect()
    try:
        try:
            msg = json.loads(raw)
        except ValueError:
            raise RpcError(PARSE_ERROR, "Parse error", http_status=400)
        check_shape(msg)
        rpc_id, method = msg.get("id"), msg["method"]
        params = msg.get("params") or {}
        if method == "tools/call" and isinstance(params.get("name"), str):
            tool_name = params["name"][:64]  # recorded even if the request is rejected below
        try:
            version = check_meta(params)
        except RpcError as e:
            e.http_status = 400
            raise
        check_headers({k.lower(): v for k, v in headers.items()}, msg, version)

        if "id" not in msg:  # a notification: accepted, never answered
            audit.record(conn, surface=surface, rpc_id=None, trace_id=trace_id, method=method, outcome="accepted",
                         latency_ms=(time.perf_counter() - t0) * 1000)
            return Response(status_code=202, headers={"X-Replica": config.REPLICA})

        if method == "server/discover":
            result = discover_result(surface)
        elif method == "ping":
            result = complete({}, surface)
        else:
            caller = authenticate(headers.get("authorization"), surface)
            require_scope(caller, "claims:read")
            if method == "tools/list":
                result = complete({"tools": tools.visible_tools(surface, caller), "ttlMs": config.LIST_TTL_MS,
                                   "cacheScope": "private"}, surface)
            elif method == "tools/call":
                if tool_name not in config.SURFACES[surface]["tools"]:
                    raise RpcError(INVALID_PARAMS, f"Unknown tool: {tool_name}")
                require_scope(caller, tools.TOOL_DEFS[tool_name]["scope"])
                args = params.get("arguments", {})
                if not isinstance(args, dict):
                    raise RpcError(INVALID_PARAMS, "arguments must be an object")
                out = tools.call(conn, surface, caller, tool_name, args, params.get("_meta") or {})
                result = complete({"content": [{"type": "text", "text": json.dumps(out.structured)}],
                                   "structuredContent": out.structured, "isError": out.is_error}, surface)
                audit.record(conn, surface=surface, rpc_id=rpc_id, trace_id=trace_id, method=method, tool=tool_name,
                             caller=caller, decision=out.decision, reason=out.reason, args=args,
                             result=out.structured, outcome="error" if out.is_error else "ok",
                             latency_ms=(time.perf_counter() - t0) * 1000)
                return _json({"jsonrpc": "2.0", "id": rpc_id, "result": result})
            else:
                raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

        audit.record(conn, surface=surface, rpc_id=rpc_id, trace_id=trace_id, method=method, caller=caller,
                     decision="allow", outcome="ok", latency_ms=(time.perf_counter() - t0) * 1000)
        return _json({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    except RpcError as e:
        audit.record(conn, surface=surface, rpc_id=rpc_id, trace_id=trace_id, method=method, tool=tool_name,
                     caller=caller, decision="reject", reason=f"{e.code} {e.message}", outcome="protocol_error",
                     latency_ms=(time.perf_counter() - t0) * 1000)
        return _json(e.body(rpc_id), e.http_status)
    except AuthError as e:
        audit.record(conn, surface=surface, rpc_id=rpc_id, trace_id=trace_id, method=method, tool=tool_name,
                     caller=caller, decision="deny", reason=f"{e.error}: {e.description}", outcome="auth_error",
                     latency_ms=(time.perf_counter() - t0) * 1000)
        return _json({"error": e.error, "error_description": e.description}, e.status,
                     {"WWW-Authenticate": e.www_authenticate(surface)})
    except Exception:  # never leak internals to the client
        audit.record(conn, surface=surface, rpc_id=rpc_id, trace_id=trace_id, method=method, tool=tool_name,
                     caller=caller, decision="error", outcome="internal_error",
                     latency_ms=(time.perf_counter() - t0) * 1000)
        return _json(RpcError(INTERNAL_ERROR, "Internal error").body(rpc_id), 500)
    finally:
        conn.close()


@app.get("/.well-known/oauth-protected-resource/mcp/{surface}")
def protected_resource(surface: str) -> Response:
    if surface not in config.SURFACES:
        return _json({"error": "not_found"}, 404)
    return _json({"resource": config.resource_url(surface), "authorization_servers": [config.TRUSTED_ISSUER],
                  "scopes_supported": config.SURFACES[surface]["scopes"], "bearer_methods_supported": ["header"]})


@app.get("/healthz")
def healthz() -> Response:
    conn = store.connect()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return _json({"status": "ok", "replica": config.REPLICA, "version": config.SERVER_VERSION})


# --- approval console ---------------------------------------------------------

@app.get("/console")
def console() -> FileResponse:
    return FileResponse(STATIC / "console.html")


def _console_caller(request: Request) -> Caller:
    from .auth import AuthError as _AE
    import jwt
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise _AE(401, "invalid_request", "Sign in to the console")
    try:
        claims = jwt.decode(header[7:], config.TOKEN_SECRET, algorithms=["HS256"], audience=CONSOLE_AUDIENCE,
                            issuer=config.TRUSTED_ISSUER)
    except jwt.PyJWTError:
        raise _AE(401, "invalid_token", "Session expired; sign in again")
    caller = Caller(sub=claims["sub"], issuer=claims["iss"], audience=CONSOLE_AUDIENCE,
                    scopes=str(claims.get("scope", "")).split(), level=int(claims.get("level", 0)))
    require_scope(caller, "claims:approve")
    return caller


def _console_error(e: AuthError) -> Response:
    return _json({"error": e.error, "error_description": e.description}, e.status)


@app.get("/console/api/people")
def console_people() -> Response:
    if not config.DEV_MODE:
        return _json({"people": []})
    people = [{"id": k, "name": v["name"], "level": v["level"], "title": config.LEVEL_TITLES.get(v["level"], "Service")}
              for k, v in config.USERS.items() if "claims:approve" in v["scopes"]]
    return _json({"people": people})


@app.post("/console/api/dev-login")
async def console_dev_login(request: Request) -> Response:
    """Demo sign-in. Stands in for the organization's SSO; disabled outside dev mode."""
    if not config.DEV_MODE or request.client is None or request.client.host not in ("127.0.0.1", "::1", "testclient"):
        return _json({"error": "disabled"}, 404)
    body = await request.json()
    user = config.USERS.get(body.get("user", ""))
    if not user or "claims:approve" not in user["scopes"]:
        return _json({"error": "unknown_approver"}, 400)
    return _json({"token": mint(body["user"], CONSOLE_AUDIENCE, ttl_s=8 * 3600), "user": body["user"]})


def _enrich(conn, req: dict) -> dict:
    claim = store.get_claim(conn, req["claim_id"]) or {}
    args = json.loads(req["args"])
    actor = config.USERS.get(req["actor"], {})
    out = {k: req[k] for k in ("id", "actor", "tool", "digest", "audience", "claim_id", "current_reserve", "amount",
                                "required_level", "rule", "status", "created", "expires", "decided_by", "decided_at",
                                "decision_note", "approval_id")}
    out.update({
        "actor_name": actor.get("name", req["actor"]), "actor_level": actor.get("level"),
        "actor_title": config.LEVEL_TITLES.get(actor.get("level", 0), ""),
        "actor_limit": req["actor_limit"],  # the limit that applied, including the §2.3 catastrophe raise
        "required_title": config.LEVEL_TITLES.get(req["required_level"], ""),
        "reason": args.get("reason", ""),
        "claim": {k: claim.get(k) for k in ("id", "policy", "state", "peril", "summary", "date_of_loss", "catastrophe",
                                             "siu", "reserve")},
        "claimant": (claim.get("claimant") or {}).get("name"),
        "decided_by_name": config.USERS.get(req["decided_by"] or "", {}).get("name"),
    })
    if req["approval_id"]:
        rec = conn.execute("SELECT expires, used_at FROM approvals WHERE id=?", (req["approval_id"],)).fetchone()
        out["approval"] = dict(rec) if rec else None
    return out


@app.get("/console/api/requests")
def console_requests(request: Request) -> Response:
    try:
        caller = _console_caller(request)
    except AuthError as e:
        return _console_error(e)
    conn = store.connect()
    try:
        rows = conn.execute("SELECT * FROM approval_requests ORDER BY created DESC LIMIT 100").fetchall()
        reqs = [_enrich(conn, dict(r)) for r in rows]
        for r in reqs:
            if r["status"] == "pending" and r["expires"] <= store.now():
                r["status"] = "expired"
        me = config.USERS.get(caller.sub, {})
        return _json({"me": {"id": caller.sub, "name": me.get("name"), "level": caller.level,
                             "title": config.LEVEL_TITLES.get(caller.level), "limit": config.authority_limit(caller.level)},
                      "requests": reqs, "now": store.now()})
    finally:
        conn.close()


@app.post("/console/api/requests/{request_id}/decision")
async def console_decide(request_id: str, request: Request) -> Response:
    try:
        caller = _console_caller(request)
    except AuthError as e:
        return _console_error(e)
    body = await request.json()
    conn = store.connect()
    t0 = time.perf_counter()
    try:
        approve = bool(body.get("approve"))
        try:
            req = approvals.decide(conn, request_id, caller.sub, caller.level, approve, str(body.get("note", "")))
        except approvals.ApprovalError as e:
            audit.record(conn, surface="console", rpc_id=request_id, trace_id=None, method="approval/decide",
                         tool="claim.set_reserve", caller=caller, decision="deny", reason=str(e), outcome="rejected",
                         latency_ms=(time.perf_counter() - t0) * 1000)
            return _json({"error": str(e)}, 409)
        audit.record(conn, surface="console", rpc_id=request_id, trace_id=None, method="approval/decide",
                     tool="claim.set_reserve", caller=caller, decision="approved" if approve else "denied",
                     reason=req.get("approval_id") or "", args={"request_id": request_id, "digest": req["digest"]},
                     outcome="ok", latency_ms=(time.perf_counter() - t0) * 1000)
        return _json({"request": _enrich(conn, req)})
    finally:
        conn.close()


@app.get("/console/api/audit")
def console_audit(request: Request) -> Response:
    try:
        _console_caller(request)
    except AuthError as e:
        return _console_error(e)
    conn = store.connect()
    try:
        return _json({"events": audit.recent(conn, 60)})
    finally:
        conn.close()


@app.post("/dev/token")
async def dev_token(request: Request) -> Response:
    """Demo-only token minting for the scripted client. Disabled outside dev mode."""
    if not config.DEV_MODE or request.client is None or request.client.host not in ("127.0.0.1", "::1", "testclient"):
        return _json({"error": "disabled"}, 404)
    body = await request.json()
    if body.get("user") not in config.USERS or body.get("surface") not in config.SURFACES:
        return _json({"error": "bad_request"}, 400)
    return _json({"token": mint(body["user"], config.resource_url(body["surface"]))})
