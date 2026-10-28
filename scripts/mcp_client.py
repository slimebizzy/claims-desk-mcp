"""A minimal stateless MCP client over Streamable HTTP. Every request carries its
own protocol metadata and mirrored headers; nothing is negotiated up front."""
from __future__ import annotations

import itertools
import os
import secrets
from typing import Any, Optional

import httpx

VERSION = "2026-07-28"
BASE = os.environ.get("CLAIMS_DESK_URL", "http://127.0.0.1:8800")
CLIENT_INFO = {"name": "claims-desk-demo-client", "version": "1.0.0"}
_ids = itertools.count(1)


class McpClient:
    def __init__(self, surface: str, token: Optional[str] = None, base: str = BASE, http: Optional[httpx.Client] = None):
        self.url = f"{base}/mcp/{surface}"
        self.token = token
        self.http = http or httpx.Client(timeout=30)

    def envelope(self, method: str, params: Optional[dict] = None, meta: Optional[dict] = None, rid: Any = "auto"):
        params = dict(params or {})
        params["_meta"] = {"io.modelcontextprotocol/protocolVersion": VERSION,
                           "io.modelcontextprotocol/clientCapabilities": {},
                           "io.modelcontextprotocol/clientInfo": CLIENT_INFO, **(meta or {})}
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if rid is not None:
            msg["id"] = next(_ids) if rid == "auto" else rid
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": VERSION, "Mcp-Method": method,
                   "traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"}
        if method == "tools/call":
            headers["Mcp-Name"] = params["name"]
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return msg, headers

    def send(self, msg: dict, headers: dict) -> httpx.Response:
        return self.http.post(self.url, json=msg, headers=headers)

    def rpc(self, method: str, params: Optional[dict] = None, meta: Optional[dict] = None) -> dict:
        r = self.send(*self.envelope(method, params, meta))
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{body['error']['code']}: {body['error']['message']}")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {body}")
        return body["result"]

    def call(self, name: str, arguments: dict, meta: Optional[dict] = None) -> dict:
        return self.rpc("tools/call", {"name": name, "arguments": arguments}, meta)


def dev_token(user: str, surface: str, base: str = BASE) -> str:
    return httpx.post(f"{base}/dev/token", json={"user": user, "surface": surface}).json()["token"]
