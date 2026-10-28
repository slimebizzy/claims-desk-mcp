"""Start two interchangeable replicas and the round-robin balancer.

    python scripts/run_cluster.py            # replicas on 8801/8802, balancer on 8800
    python scripts/run_cluster.py --reset    # start from the seed data
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from app import store
    store.init(reset="--reset" in sys.argv)

    env = {**os.environ, "CLAIMS_DESK_PUBLIC_URL": "http://127.0.0.1:8800", "PYTHONPATH": str(ROOT)}
    procs = []
    for name, port in (("r1", 8801), ("r2", 8802)):
        procs.append(subprocess.Popen([PY, "-m", "uvicorn", "app.server:app", "--host", "127.0.0.1", "--port", str(port),
                                       "--log-level", "warning"], cwd=ROOT, env={**env, "CLAIMS_DESK_REPLICA": name}))
    procs.append(subprocess.Popen([PY, "-m", "uvicorn", "scripts.lb:app", "--host", "127.0.0.1", "--port", "8800",
                                   "--log-level", "warning"], cwd=ROOT, env=env))

    for _ in range(50):
        try:
            seen = {httpx.get("http://127.0.0.1:8800/healthz").json()["replica"] for _ in range(4)}
            if seen == {"r1", "r2"}:
                break
        except Exception:
            pass
        time.sleep(0.2)
    print("Claims Desk is up: balancer http://127.0.0.1:8800 -> replicas r1 :8801, r2 :8802", flush=True)
    print("Approval console: http://127.0.0.1:8800/console", flush=True)

    def stop(*_):
        for p in procs:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while all(p.poll() is None for p in procs):
        time.sleep(0.5)
    stop()


if __name__ == "__main__":
    main()
