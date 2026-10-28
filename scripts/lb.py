"""A deliberately dumb round-robin load balancer: no affinity, no retries, no
buffering of errors into generic 500s. Status codes and bodies pass through
unchanged so protocol errors stay protocol errors."""
from __future__ import annotations

import itertools
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

UPSTREAMS = os.environ.get("LB_UPSTREAMS", "http://127.0.0.1:8801,http://127.0.0.1:8802").split(",")
_next = itertools.cycle(UPSTREAMS)
HOP = {"host", "content-length", "connection", "transfer-encoding", "keep-alive"}

app = FastAPI(docs_url=None, redoc_url=None)
client = httpx.AsyncClient(timeout=30)


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request) -> Response:
    upstream = next(_next)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    headers["x-forwarded-for"] = request.client.host if request.client else ""
    r = await client.request(request.method, f"{upstream}/{path}", params=request.query_params,
                             content=await request.body(), headers=headers)
    out = {k: v for k, v in r.headers.items() if k.lower() not in HOP | {"content-encoding"}}
    out["via"] = "1.1 claims-desk-lb"
    return Response(r.content, status_code=r.status_code, headers=out)
