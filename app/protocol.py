"""The stateless MCP envelope (revision 2026-07-28).

There is no initialize handshake and no session. Every request carries its own
protocol version and client capabilities in params._meta, and every result
carries resultType and the server's identity. A replica can validate any request
from the message alone.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from . import config

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"
META_APPROVAL = "io.github.slimebizzy/approvalId"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None, http_status: int = 200):
        super().__init__(message)
        self.code, self.message, self.data, self.http_status = code, message, data, http_status

    def body(self, rpc_id: Any) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


def server_info(surface: str) -> dict:
    return {"name": config.SURFACES[surface]["registry_name"], "version": config.SERVER_VERSION}


def complete(result: dict, surface: str) -> dict:
    out = dict(result)
    out["resultType"] = "complete"
    meta = dict(out.get("_meta") or {})
    meta[META_SERVER] = server_info(surface)
    out["_meta"] = meta
    return out


def check_shape(msg: Any) -> None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        raise RpcError(INVALID_REQUEST, "Invalid Request", http_status=400)
    rid = msg.get("id")
    if "id" in msg and (not isinstance(rid, (str, int)) or isinstance(rid, bool)):
        raise RpcError(INVALID_REQUEST, "Invalid Request: id must be a string or integer", http_status=400)
    if "params" in msg and not isinstance(msg["params"], dict):
        raise RpcError(INVALID_PARAMS, "params must be an object", http_status=400)


def check_meta(params: dict) -> str:
    """Validate per-request protocol metadata. Returns the requested version."""
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise RpcError(INVALID_PARAMS, "params._meta is required")
    version = meta.get(META_VERSION)
    if not isinstance(version, str):
        raise RpcError(INVALID_PARAMS, f"params._meta[{META_VERSION!r}] must be a string")
    if version not in config.SUPPORTED_VERSIONS:
        raise RpcError(UNSUPPORTED_VERSION, "Unsupported protocol version",
                       {"supported": list(config.SUPPORTED_VERSIONS), "requested": version})
    if not isinstance(meta.get(META_CAPS), dict):
        raise RpcError(INVALID_PARAMS, f"params._meta[{META_CAPS!r}] must be an object")
    return version


def check_headers(headers: Dict[str, str], msg: dict, version: str) -> None:
    """Mirrored headers must agree with the body, or intermediaries that route
    on headers would see a different request than the server executes."""
    def mismatch(name: str, header_val: Optional[str], body_val: Any) -> RpcError:
        return RpcError(HEADER_MISMATCH, f"{name} header does not match the request body",
                        {"header": name, "headerValue": header_val, "bodyValue": body_val}, http_status=400)

    hv = headers.get("mcp-protocol-version")
    if hv != version:
        raise mismatch("MCP-Protocol-Version", hv, version)
    hm = headers.get("mcp-method")
    if hm != msg["method"]:
        raise mismatch("Mcp-Method", hm, msg["method"])
    if msg["method"] in ("tools/call", "resources/read", "prompts/get"):
        name = (msg.get("params") or {}).get("name")
        hn = headers.get("mcp-name")
        if hn != name:
            raise mismatch("Mcp-Name", hn, name)


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(obj).encode()).hexdigest()


# --- a small JSON Schema subset, enough for bounded tool inputs ---------------

def validate(schema: dict, value: Any, path: str = "$") -> List[str]:
    errs: List[str] = []
    t = schema.get("type")
    types = {"object": dict, "string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list}
    if t:
        ok = isinstance(value, types[t]) and not (t in ("integer", "number") and isinstance(value, bool))
        if not ok:
            return [f"{path}: expected {t}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: must be one of {schema['enum']}")
    if t == "string":
        if len(value) < schema.get("minLength", 0):
            errs.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            errs.append(f"{path}: does not match {schema['pattern']}")
    if t in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: above {schema['maximum']}")
    if t == "object":
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}.{req}: required")
        for k, v in value.items():
            if k in props:
                errs.extend(validate(props[k], v, f"{path}.{k}"))
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}.{k}: unexpected property")
    return errs
