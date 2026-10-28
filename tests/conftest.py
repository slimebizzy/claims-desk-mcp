import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["CLAIMS_DESK_DB"] = str(Path(_tmp) / "test.db")
os.environ["CLAIMS_DESK_PUBLIC_URL"] = "http://127.0.0.1:8800"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, store  # noqa: E402
from app.auth import mint  # noqa: E402
from app.server import app  # noqa: E402

VERSION = "2026-07-28"


@pytest.fixture(autouse=True)
def fresh_db():
    store.init(reset=True)
    yield


@pytest.fixture
def client():
    return TestClient(app, base_url="http://127.0.0.1:8800")


def token(user, surface="claims", **kw):
    return mint(user, config.resource_url(surface), **kw)


class Rpc:
    """Builds requests the way a correct client would, with knobs to break each part."""

    def __init__(self, client):
        self.client = client

    def __call__(self, surface, method, params=None, tok=None, rid=1, headers=None, meta=None, drop=(), name=None):
        params = dict(params or {})
        m = {"io.modelcontextprotocol/protocolVersion": VERSION, "io.modelcontextprotocol/clientCapabilities": {}}
        m.update(meta or {})
        for k in drop:
            m.pop(k, None)
        params["_meta"] = m
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if rid is not None:
            msg["id"] = rid
        h = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": VERSION, "Mcp-Method": method}
        if method == "tools/call":
            h["Mcp-Name"] = name or params.get("name")
        if tok:
            h["Authorization"] = "Bearer " + tok
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        return self.client.post(f"/mcp/{surface}", json=msg, headers=h)

    def call(self, surface, tool, args, tok, meta=None):
        r = self(surface, "tools/call", {"name": tool, "arguments": args}, tok=tok, meta=meta)
        assert r.status_code == 200, r.text
        return r.json()["result"]


@pytest.fixture
def rpc(client):
    return Rpc(client)
