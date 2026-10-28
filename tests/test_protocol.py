"""Wire-level contract: stateless envelope, discovery, headers, errors."""
from app.protocol import canonical
from conftest import VERSION, token


def test_discover_needs_no_session_or_token(rpc):
    r = rpc("claims", "server/discover")
    body = r.json()["result"]
    assert r.status_code == 200
    assert body["resultType"] == "complete"
    assert body["supportedVersions"] == [VERSION]
    assert body["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "io.github.slimebizzy/claims-desk-reserves"


def test_missing_protocol_version_is_invalid_params(rpc):
    r = rpc("claims", "server/discover", drop=["io.modelcontextprotocol/protocolVersion"])
    assert r.status_code == 400 and r.json()["error"]["code"] == -32602


def test_non_string_protocol_version_is_invalid_params(rpc):
    r = rpc("claims", "server/discover", meta={"io.modelcontextprotocol/protocolVersion": 20260728})
    assert r.json()["error"]["code"] == -32602


def test_unsupported_version_returns_32022_with_exact_data(rpc):
    r = rpc("claims", "server/discover", meta={"io.modelcontextprotocol/protocolVersion": "2025-06-18"},
            headers={"MCP-Protocol-Version": "2025-06-18"})
    err = r.json()["error"]
    assert err["code"] == -32022
    assert err["data"] == {"supported": [VERSION], "requested": "2025-06-18"}


def test_header_version_mismatch_is_32020_and_never_reaches_tools(rpc, client):
    r = rpc("claims", "tools/call", {"name": "claim.get", "arguments": {"claim_id": "CL-1042"}},
            tok=token("dana.ruiz"), headers={"MCP-Protocol-Version": "2025-11-25"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    from app import store
    conn = store.connect()
    rows = conn.execute("SELECT outcome FROM audit WHERE tool='claim.get'").fetchall()
    assert [x["outcome"] for x in rows] == ["protocol_error"]  # rejected before policy or handler


def test_method_and_name_headers_must_match_body(rpc):
    tok = token("dana.ruiz")
    r = rpc("claims", "tools/list", tok=tok, headers={"Mcp-Method": "tools/call"})
    assert r.json()["error"]["code"] == -32020
    r = rpc("claims", "tools/call", {"name": "claim.get", "arguments": {"claim_id": "CL-1042"}}, tok=tok,
            name="claim.set_reserve")
    assert r.json()["error"]["code"] == -32020


def test_notification_gets_202_and_no_body(rpc):
    r = rpc("claims", "notifications/cancelled", {"requestId": 7}, rid=None)
    assert r.status_code == 202 and r.content == b""


def test_response_id_matches_request_id(rpc):
    for rid in (1, "abc-9", 0):
        assert rpc("claims", "server/discover", rid=rid).json()["id"] == rid


def test_parse_error_and_bad_accept(client):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    r = client.post("/mcp/claims", content=b"{not json", headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    r = client.post("/mcp/claims", json={}, headers={"Accept": "application/json"})
    assert r.status_code == 406


def test_foreign_origin_is_refused(rpc):
    r = rpc("claims", "server/discover", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_unknown_method(rpc):
    r = rpc("claims", "resources/list", tok=token("dana.ruiz"))
    assert r.json()["error"]["code"] == -32601


def test_tools_list_is_byte_stable_and_cache_scoped(rpc):
    tok = token("dana.ruiz")
    a = rpc("claims", "tools/list", tok=tok).json()["result"]
    b = rpc("claims", "tools/list", tok=tok).json()["result"]
    assert canonical(a) == canonical(b)
    assert [t["name"] for t in a["tools"]] == sorted(t["name"] for t in a["tools"])
    assert a["cacheScope"] == "private" and a["ttlMs"] > 0 and a["resultType"] == "complete"


def test_tool_visibility_follows_scope(rpc):
    names = lambda tok, s: [t["name"] for t in rpc(s, "tools/list", tok=tok).json()["result"]["tools"]]  # noqa: E731
    assert names(token("dana.ruiz"), "claims") == ["approval.status", "claim.get", "claim.set_reserve"]
    assert names(token("copilot-svc", "read"), "read") == ["claim.get", "policy.search"]


def test_malformed_arguments_are_a_tool_error_not_a_crash(rpc):
    res = rpc.call("claims", "claim.set_reserve", {"claim_id": "CL-1042", "amount": "lots"}, token("dana.ruiz"))
    assert res["isError"] is True
    assert res["structuredContent"]["status"] == "invalid"


def test_unknown_tool_on_this_surface(rpc):
    r = rpc("read", "tools/call", {"name": "claim.set_reserve", "arguments": {}}, tok=token("copilot-svc", "read"))
    assert r.json()["error"]["code"] == -32602
