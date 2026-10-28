"""Capture golden and negative cases on the real wire, directly against one
replica and again through the balancer, and prove the intermediary changes
nothing: same status, same body digest. Secrets are redacted before writing.

    python scripts/wire_evidence.py      # needs scripts/run_cluster.py running
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config  # noqa: E402
from app.auth import mint  # noqa: E402
from scripts.mcp_client import VERSION  # noqa: E402

DIRECT, PROXY = "http://127.0.0.1:8801", "http://127.0.0.1:8800"


def meta(version=VERSION):
    return {"io.modelcontextprotocol/protocolVersion": version, "io.modelcontextprotocol/clientCapabilities": {}}


def case(name, surface, method, params=None, rid=1, headers=None, token=None, expect=None):
    params = dict(params or {})
    params.setdefault("_meta", meta())
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    if rid is not None:
        msg["id"] = rid
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": params["_meta"].get("io.modelcontextprotocol/protocolVersion", VERSION)
         if isinstance(params["_meta"].get("io.modelcontextprotocol/protocolVersion"), str) else VERSION,
         "Mcp-Method": method}
    if method == "tools/call":
        h["Mcp-Name"] = params.get("name", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(headers or {})
    return {"name": name, "surface": surface, "msg": msg, "headers": h, "expect": expect}


def digest(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def redact_headers(h: dict) -> dict:
    return {k: ("Bearer REDACTED" if k.lower() == "authorization" else v) for k, v in h.items()}


def cases():
    dana = mint("dana.ruiz", config.resource_url("claims"))
    svc_read = mint("copilot-svc", config.resource_url("read"))
    svc_claims = mint("copilot-svc", config.resource_url("claims"))
    read_aud = mint("dana.ruiz", config.resource_url("read"))
    get = {"name": "claim.get", "arguments": {"claim_id": "CL-1042"}}
    return [
        case("discover", "claims", "server/discover", expect=(200, None)),
        case("tools_list", "claims", "tools/list", token=dana, expect=(200, None)),
        case("tools_call_get", "read", "tools/call", get, token=svc_read, expect=(200, None)),
        case("missing_version", "claims", "server/discover", {"_meta": {"io.modelcontextprotocol/clientCapabilities": {}}},
             expect=(400, -32602)),
        case("non_string_version", "claims", "server/discover",
             {"_meta": {**meta(), "io.modelcontextprotocol/protocolVersion": 20260728}}, expect=(400, -32602)),
        case("unsupported_version", "claims", "server/discover", {"_meta": meta("2025-06-18")}, expect=(400, -32022)),
        case("header_version_mismatch", "read", "tools/call", get, token=svc_read,
             headers={"MCP-Protocol-Version": "2025-11-25"}, expect=(400, -32020)),
        case("header_name_mismatch", "read", "tools/call", get, token=svc_read,
             headers={"Mcp-Name": "claim.set_reserve"}, expect=(400, -32020)),
        case("notification", "claims", "notifications/cancelled", {"requestId": 1}, rid=None, expect=(202, None)),
        case("no_token", "claims", "tools/list", expect=(401, None)),
        case("wrong_audience", "claims", "tools/list", token=read_aud, expect=(401, None)),
        case("missing_scope", "claims", "tools/call",
             {"name": "claim.set_reserve", "arguments": {"claim_id": "CL-1042", "amount": 1, "reason": "",
                                                          "idempotency_key": "wire-0001"}},
             token=svc_claims, expect=(403, None)),
        case("malformed_arguments", "read", "tools/call", {"name": "claim.get", "arguments": {"claim_id": 42}},
             token=svc_read, expect=(200, None)),
        case("unknown_method", "claims", "resources/list", token=dana, expect=(200, -32601)),
    ]


def run(base, c, http):
    r = http.post(f"{base}/mcp/{c['surface']}", content=json.dumps(c["msg"]).encode(), headers=c["headers"])
    return r


def main() -> int:
    out, failures = [], []
    with httpx.Client(timeout=30) as http:
        for c in cases():
            d, p = run(DIRECT, c, http), run(PROXY, c, http)
            err = d.json().get("error") if d.content else None
            code = err.get("code") if isinstance(err, dict) else None  # auth errors are OAuth-shaped, not JSON-RPC
            exp_status, exp_code = c["expect"]
            same = d.status_code == p.status_code and digest(d.content) == digest(p.content)
            ok = d.status_code == exp_status and (exp_code is None or code == exp_code) and same
            if c["name"] == "notification":
                ok = ok and d.content == b"" and p.content == b""
            if c["name"] not in ("notification",) and d.content and "id" in c["msg"]:
                ok = ok and d.json().get("id", c["msg"]["id"]) == c["msg"]["id"]
            if not ok:
                failures.append(c["name"])
            out.append({
                "case": c["name"], "pass": ok,
                "request": {"url": f"/mcp/{c['surface']}", "headers": redact_headers(c["headers"]), "body": c["msg"]},
                "direct": {"status": d.status_code, "replica": d.headers.get("x-replica"), "body_digest": digest(d.content),
                           "body": d.json() if d.content else None,
                           "www_authenticate": d.headers.get("www-authenticate")},
                "proxy": {"status": p.status_code, "replica": p.headers.get("x-replica"), "via": p.headers.get("via"),
                          "body_digest": digest(p.content)},
                "proxy_preserved": same,
            })
    lines = "".join(json.dumps(x, sort_keys=True) + "\n" for x in out)
    path = ROOT / "evidence" / "wire.jsonl"
    path.write_text(lines)
    (ROOT / "evidence" / "wire.digest").write_text(digest(lines.encode()) + "\n")
    for x in out:
        print(f"{'PASS' if x['pass'] else 'FAIL'}  {x['case']:24} direct {x['direct']['status']}  proxy {x['proxy']['status']}"
              f"  {'same bytes' if x['proxy_preserved'] else 'DIFFERENT'}")
    print(f"\n{len(out) - len(failures)}/{len(out)} cases pass. Evidence: evidence/wire.jsonl")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
