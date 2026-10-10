"""D 有界探针：真链路里「需要审批的工具调用」会不会发出 APPROVAL_REQUIRED。

背景：浏览器级审批截图一开始没能触发（见 docs/verification-d-phase2.md §3.4）。
根因是**我的驱动脚本**两处 bug，不是实现：
1) run_shell 的参数是 cmd，我写成了 command（工具直接报「cmd 必填」，根本没到审批）；
2) 假厂商脚本是 FIFO，凭据验证会先吃掉若干步，轮到本轮时只剩文本回复 → 工具没执行。
本脚本把这两点都修掉，用来给出「机制是否存在」的确定性答案。

跑法（会自己起 provider + uvicorn，跑完自己收）：

    backend\.venv\Scripts\python.exe scripts\verify-approval-probe.py

结论口径：本脚本只证明「QIO 自己的链路会发 APPROVAL_REQUIRED」；
假厂商是本机扮演的，不证明任何真实厂商行为。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PROVIDER_PORT = 8798
BACKEND_PORT = 8734


def _wait(url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return True
        except Exception:  # noqa: BLE001 - 探针：连不上就继续等
            time.sleep(0.4)
    return False


def main() -> int:
    data_dir = pathlib.Path(tempfile.mkdtemp(prefix="qio-approval-probe-"))
    provider = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "verify_stream_provider.py"), "--port", str(PROVIDER_PORT)],
        cwd=str(BACKEND),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND / "src")
    env["QIO_DATA_DIR"] = str(data_dir)
    env["QIO_DEV_INSECURE"] = "1"
    server = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "agent.main:create_app", "--factory",
            "--host", "127.0.0.1", "--port", str(BACKEND_PORT), "--log-level", "warning",
        ],
        cwd=str(BACKEND),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % BACKEND_PORT
    prov = "http://127.0.0.1:%d" % PROVIDER_PORT
    failures: list[str] = []
    try:
        if not _wait(prov + "/__health"):
            raise SystemExit("provider 没起来（端口可能被占用）")
        if not _wait(base + "/api/health"):
            raise SystemExit("后端没起来（端口可能被占用）")

        # repeat 是关键：凭据验证会先吃掉若干步，本轮必须还能拿到工具调用
        httpx.post(
            prov + "/__script",
            json={
                "steps": [
                    {
                        "tool_chunks": [
                            {
                                "id": "c1",
                                "name": "run_shell",
                                "args_fragments": ['{"cmd": "echo qio-approval-probe"}'],
                            }
                        ],
                        "chunk_delay_ms": 10,
                        "repeat": 4,
                    },
                    {"chunks": ["已跳过。"], "chunk_delay_ms": 10},
                ],
                "default": {"chunks": ["（默认）"], "chunk_delay_ms": 10},
            },
            timeout=10,
        )
        cred = httpx.post(
            base + "/api/credentials",
            json={
                "provider": "custom",
                "endpoint": prov + "/v1",
                "secret": "sk-approval-probe",
                "default_model": "verify-model",
                "tags": ["main-loop"],
            },
            timeout=60,
        ).json()
        print("credential:", json.dumps(cred.get("verify"), ensure_ascii=False)[:160])

        events: list[dict] = []

        def reader() -> None:
            with httpx.stream("GET", base + "/api/events", timeout=httpx.Timeout(60.0)) as resp:
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        events.append(json.loads(line[5:].strip()))

        threading.Thread(target=reader, daemon=True).start()
        time.sleep(1.0)
        turn = httpx.post(base + "/api/turns", json={"message": "请运行一条命令"}, timeout=30).json()
        turn_id = turn.get("turn_id")
        deadline = time.time() + 25
        while time.time() < deadline:
            if any(
                e.get("type") == "APPROVAL_REQUIRED" for e in events
            ):
                break
            time.sleep(0.2)

        types = [e.get("type") for e in events]
        approval = [e for e in events if e.get("type") == "APPROVAL_REQUIRED"]
        print("sse_event_types:", types)
        print("APPROVAL_REQUIRED count:", len(approval))
        if approval:
            payload = approval[0]["data"].get("approval") or approval[0]["data"]
            print("approval payload:", json.dumps(payload, ensure_ascii=False)[:500])
        if not approval:
            failures.append("真链路没有发出 APPROVAL_REQUIRED（run_shell 应无条件请求审批）")
        if "TOOL_START" not in types:
            failures.append("没有 TOOL_START：工具没被执行到（检查假厂商脚本是否被验证步骤吃掉）")
        if turn_id:
            try:
                httpx.post(base + "/api/turns/cancel", timeout=10)
            except Exception:  # noqa: BLE001
                pass
    finally:
        for proc in (server, provider):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()

    if failures:
        print("[FAIL]")
        for item in failures:
            print("  -", item)
        return 1
    print("[PASS] 真链路发出了 APPROVAL_REQUIRED（机制存在）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
