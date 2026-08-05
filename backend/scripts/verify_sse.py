"""End-to-end SSE smoke test against a real uvicorn process.

Usage:
    python scripts/verify_sse.py

Starts the backend on an ephemeral port, opens the SSE stream, publishes
one test event, reads it back, then shuts the server down.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
SRC = BACKEND / "src"
PYTHON = sys.executable
PORT = 8799


def wait_healthy(base: str, timeout_s: float = 15.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base}/api/health", timeout=1.0)
            if resp.status_code == 200 and resp.json()["status"] == "ok":
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("backend did not become healthy in time")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sa-sse-") as tmp:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env["SMART_AGENT_PORT"] = str(PORT)
        env["SMART_AGENT_DATA_DIR"] = tmp

        proc = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "uvicorn",
                "agent.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
                "--log-level",
                "warning",
            ],
            cwd=str(BACKEND),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            base = f"http://127.0.0.1:{PORT}"
            wait_healthy(base)

            def publisher() -> None:
                time.sleep(0.5)
                httpx.post(
                    f"{base}/api/events/test?event_type=WARNING",
                    json={"code": "smoke"},
                    timeout=5.0,
                )

            thread = threading.Thread(target=publisher)
            thread.start()
            with httpx.Client(timeout=10.0) as client:
                with client.stream("GET", f"{base}/api/events") as resp:
                    assert resp.status_code == 200
                    lines: list[str] = []
                    for line in resp.iter_lines():
                        lines.append(line)
                        if line.startswith("data:"):
                            break
            thread.join()
            print("SSE status:", resp.status_code)
            for line in lines:
                print(line)
            print("verify_sse: OK")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()