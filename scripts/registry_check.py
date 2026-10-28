"""Check each published record against itself, its publisher, and the live server.

    python scripts/registry_check.py                 # all records in registry/
    python scripts/registry_check.py --url-override http://127.0.0.1:8800/mcp/readonly   # exercise drift

Publication (server.json) and runtime (server/discover) answer different
questions, so they are validated separately and then compared. The record
validator is a deliberately partial check of the official remote profile; run
the pinned official JSON Schema or mcp-publisher before real publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import List
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.mcp_client import McpClient  # noqa: E402

VERIFIED_PUBLISHER = "github:slimebizzy"  # what the Registry's GitHub login would prove
NAME_RE = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def validate_record(doc: dict) -> List[str]:
    errs = []
    name = doc.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name) or not 3 <= len(name) <= 200:
        errs.append("name must be namespace/server, 3-200 characters")
    desc = doc.get("description")
    if not isinstance(desc, str) or not 1 <= len(desc) <= 100:
        errs.append("description is required and at most 100 characters")
    if "title" in doc and (not isinstance(doc["title"], str) or not 1 <= len(doc["title"]) <= 100):
        errs.append("title must be 1-100 characters")
    if not isinstance(doc.get("version"), str) or not SEMVER_RE.match(doc["version"]):
        errs.append("version must be a concrete version, not a range")
    remotes = doc.get("remotes")
    if not isinstance(remotes, list) or not remotes:
        errs.append("this profile requires at least one remote")
    for i, r in enumerate(remotes or []):
        if r.get("type") not in ("streamable-http", "sse"):
            errs.append(f"remotes[{i}].type must be streamable-http or sse")
        u = urlparse(r.get("url", ""))
        local = u.hostname in ("127.0.0.1", "localhost", "::1")
        if u.scheme not in ("https",) + (("http",) if local else ()) or not u.netloc:
            errs.append(f"remotes[{i}].url must be https (http only for loopback)")
    return errs


def validate_namespace(name: str, publisher: str = VERIFIED_PUBLISHER) -> List[str]:
    kind, _, who = publisher.partition(":")
    expected = f"io.github.{who}/" if kind == "github" else ".".join(reversed(who.split("."))) + "/"
    return [] if name.startswith(expected) else [f"{name} is outside the verified namespace {expected}*"]


def probe(doc: dict, url: str) -> dict:
    base, _, surface = url.rpartition("/mcp/")
    try:
        result = McpClient(surface, base=base).rpc("server/discover")
    except (httpx.HTTPError, RuntimeError, ValueError) as e:
        return {"reachable": False, "error": str(e)[:200], "drift": [f"remote {url} is unreachable or not MCP"]}
    info = result.get("_meta", {}).get("io.modelcontextprotocol/serverInfo", {})
    drift = []
    if info.get("name") != doc.get("name"):
        drift.append(f"name: published {doc.get('name')!r}, live {info.get('name')!r}")
    if info.get("version") != doc.get("version"):
        drift.append(f"version: published {doc.get('version')!r}, live {info.get('version')!r}")
    if "tools" not in result.get("capabilities", {}):
        drift.append("live server does not advertise tools")
    return {"reachable": True, "serverInfo": info, "supportedVersions": result.get("supportedVersions"),
            "capabilities": result.get("capabilities"), "drift": drift}


def check(path: Path, url_override: str = "") -> dict:
    raw = path.read_bytes()
    doc = json.loads(raw)
    url = url_override or doc["remotes"][0]["url"]
    record_errors = validate_record(doc) + validate_namespace(doc.get("name", ""))
    live = probe(doc, url)
    return {"record": path.name, "record_digest": "sha256:" + hashlib.sha256(raw).hexdigest(), "name": doc.get("name"),
            "version": doc.get("version"), "published_remote": doc["remotes"][0]["url"], "probed_remote": url,
            "record_errors": record_errors, "live": live,
            "admit": not record_errors and live["reachable"] and not live["drift"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url-override", default="", help="probe this URL instead of the published one")
    ap.add_argument("--out", default=str(ROOT / "evidence" / "registry-report.json"))
    a = ap.parse_args()
    reports = [check(p, a.url_override) for p in sorted((ROOT / "registry").glob("*.server.json"))]
    for r in reports:
        verdict = "ADMIT" if r["admit"] else "HOLD"
        print(f"{verdict:5} {r['name']} {r['version']}  {r['probed_remote']}")
        for e in r["record_errors"] + r["live"]["drift"]:
            print(f"      - {e}")
    if not a.url_override:
        Path(a.out).write_text(json.dumps(reports, indent=2) + "\n")
    return 0 if all(r["admit"] for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
