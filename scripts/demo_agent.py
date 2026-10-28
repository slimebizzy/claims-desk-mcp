"""Claude as the adjuster's MCP client.

    python scripts/demo_agent.py            # asks to raise CL-1042 to $62,000, then waits for a supervisor
    python scripts/demo_agent.py --no-wait  # stop at approval_required

The tool list comes from the live server's tools/list, so the agent sees exactly
what the server advertises to Dana's token. The one client-side adaptation:
Claude tool names can't contain dots, and the approval id travels in
params._meta, so set_reserve gets an optional approval_id input that this
client moves into _meta before sending. Needs ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from app.protocol import META_APPROVAL  # noqa: E402
from scripts.mcp_client import McpClient, dev_token  # noqa: E402

MODEL = "claude-opus-5-5"
SYSTEM = ("You help Dana Ruiz, an Associate Adjuster at Cobalt Harbor Mutual (a fictional insurer), work her claims "
          "through the Claims Desk tools. Be brief. Check the claim before changing it. If a change needs approval, "
          "say who has to approve it and stop; don't try to work around the limit. Never invent an approval id.")
PROMPT = ("The roof inspection on CL-1042 came back: the whole roof and both skylights need replacing. "
          "Raise the reserve to $62,000.")


def claude_tools(mcp_tools):
    out = []
    for t in mcp_tools:
        schema = json.loads(json.dumps(t["inputSchema"]))
        if t["name"] == "claim.set_reserve":
            schema["properties"]["approval_id"] = {"type": "string", "description": "Approval id from "
                                                   "approval_status, once a supervisor has approved this exact change."}
        out.append({"name": t["name"].replace(".", "_"), "description": t["description"], "input_schema": schema})
    return out


def run_turns(client, mcp, tools, messages, by_name):
    for _ in range(10):
        response = client.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM, tools=tools, messages=messages,
                                          thinking={"type": "adaptive"})
        messages.append({"role": "assistant", "content": response.content})
        for b in response.content:
            if b.type == "text" and b.text.strip():
                print(f"\nClaude: {b.text.strip()}")
        if response.stop_reason != "tool_use":
            return response
        results = []
        for b in response.content:
            if b.type != "tool_use":
                continue
            name = by_name[b.name]
            args = dict(b.input)
            meta = {META_APPROVAL: args.pop("approval_id")} if "approval_id" in args else None
            print(f"\n  -> {name} {json.dumps(args)}" + (f"  [approval {meta[META_APPROVAL]}]" if meta else ""))
            res = mcp.call(name, args, meta)
            sc = res.get("structuredContent", {})
            print(f"  <- {'error ' if res.get('isError') else ''}{json.dumps(sc)[:220]}")
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(sc),
                            "is_error": bool(res.get("isError"))})
        messages.append({"role": "user", "content": results})
    return None


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # show progress live when piped
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("prompt", nargs="?", default=PROMPT)
    a = ap.parse_args()
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT.parent / "coverage-copilot" / ".env")

    mcp = McpClient("claims", dev_token("dana.ruiz", "claims"))
    listed = mcp.rpc("tools/list")["tools"]
    tools = claude_tools(listed)
    by_name = {t["name"].replace(".", "_"): t["name"] for t in listed}
    client = anthropic.Anthropic(timeout=60, max_retries=2)

    print(f"Dana: {a.prompt}")
    messages = [{"role": "user", "content": a.prompt}]
    run_turns(client, mcp, tools, messages, by_name)

    pending = None
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for r in m["content"]:
                if r.get("type") == "tool_result" and '"approval_required"' in r["content"]:
                    pending = json.loads(r["content"])["request_id"]
    if not pending or a.no_wait:
        return 0

    print(f"\n... waiting for a supervisor to decide {pending} at http://127.0.0.1:8800/console#{pending}")
    while True:
        st = mcp.call("approval.status", {"request_id": pending})["structuredContent"]
        if st["status"] != "pending":
            break
        time.sleep(3)
    follow = ("Priya says she approved it. Go ahead." if st["status"] == "approved"
              else "Did my supervisor decide on that request?")
    print(f"\nDana: {follow}")
    messages.append({"role": "user", "content": follow})
    run_turns(client, mcp, tools, messages, by_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
