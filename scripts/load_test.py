"""Concurrency through the balancer, with no affinity.

    python scripts/load_test.py      # needs scripts/run_cluster.py --reset running

Checks:
  * every response is a well-formed JSON-RPC reply whose id matches its request
  * both replicas served traffic, alternating with no session or stickiness
  * concurrent writes to one claim from two replicas lose no update
  * the same idempotency key sent twice at once applies once
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import store  # noqa: E402
from scripts.mcp_client import BASE, McpClient, dev_token  # noqa: E402


async def main() -> int:
    dana = McpClient("claims", dev_token("dana.ruiz", "claims"))
    reader = McpClient("read", dev_token("copilot-svc", "read"))
    jobs = []
    # 100 reads spread over both surfaces
    for i in range(50):
        jobs.append(("list", *dana.envelope("tools/list")))
        jobs.append(("get", *reader.envelope("tools/call", {"name": "claim.get", "arguments": {"claim_id": "CL-1042"}})))
    # 60 concurrent writes to one claim, all within Dana's authority on CL-1120
    for i in range(60):
        args = {"claim_id": "CL-1120", "amount": 6000 + i * 10, "reason": "load test adjustment",
                "idempotency_key": f"load-write-{i:04d}"}
        jobs.append(("write", *dana.envelope("tools/call", {"name": "claim.set_reserve", "arguments": args})))
    # 20 keys, each sent twice at the same moment
    for i in range(20):
        args = {"claim_id": "CL-1103", "amount": 9000 + i, "reason": "duplicate delivery test",
                "idempotency_key": f"load-dupe-{i:04d}"}
        for _ in range(2):
            jobs.append(("dupe", *dana.envelope("tools/call", {"name": "claim.set_reserve", "arguments": args})))

    async with httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=200)) as http:
        async def send(kind, msg, headers):
            url = dana.url if "/claims" in dana.url and kind in ("list", "write", "dupe") else reader.url
            r = await http.post(url, json=msg, headers=headers)
            return kind, msg["id"], r.status_code, r.headers.get("x-replica"), r.json()

        t0 = time.perf_counter()
        results = await asyncio.gather(*(send(k, m, h) for k, m, h in jobs))
        elapsed = time.perf_counter() - t0

    problems = []
    replicas = Counter(rep for *_, rep, _ in results)
    for kind, rid, status, rep, body in results:
        if status != 200 or body.get("id") != rid or body.get("jsonrpc") != "2.0" or "result" not in body:
            problems.append(f"{kind} id={rid}: HTTP {status} {json.dumps(body)[:120]}")
        elif body["result"].get("resultType") != "complete":
            problems.append(f"{kind} id={rid}: missing resultType")
        elif kind in ("write", "dupe") and body["result"]["isError"]:
            problems.append(f"{kind} id={rid}: {body['result']['structuredContent']}")

    conn = store.connect()
    writes = conn.execute("SELECT COUNT(*), COUNT(DISTINCT replica) FROM reserve_history WHERE claim_id='CL-1120'").fetchone()
    version = store.get_claim(conn, "CL-1120")["version"]
    dupes = conn.execute("SELECT COUNT(*) FROM reserve_history WHERE claim_id='CL-1103'").fetchone()[0]
    replayed = sum(1 for k, *_, b in results if k == "dupe" and b.get("result", {}).get("structuredContent", {}).get("replayed"))

    report = {
        "requests": len(results), "elapsed_s": round(elapsed, 2), "replicas": dict(replicas),
        "protocol_problems": problems[:10],
        "lost_updates": {"writes_sent": 60, "writes_recorded": writes[0], "replicas_that_wrote": writes[1],
                         "claim_version": version, "expected_version": 61},
        "idempotency": {"keys": 20, "sends": 40, "applied": dupes, "replayed": replayed},
    }
    ok = (not problems and set(replicas) == {"r1", "r2"} and writes[0] == 60 and version == 61 and dupes == 20
          and replayed == 20)
    report["pass"] = ok
    (ROOT / "evidence" / "load-test.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
