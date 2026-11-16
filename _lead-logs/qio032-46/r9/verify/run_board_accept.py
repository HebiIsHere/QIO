# -*- coding: utf-8 -*-
"""QIO #46 r9 真实窗口板面/草稿验收编排：安装版 QIO + 隔离数据 + fake provider + CDP。"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

VERIFY = Path("D:/qio-dev/q46-r9/_lead-logs/qio032-46/r9/verify")
sys.path.insert(0, str(VERIFY))
import win_instance_accept as W  # noqa: E402  （复用窗口/进程/lease 辅助）


def http(method, url, token=None, body=None):
    headers = {}
    if token:
        headers["authorization"] = "Bearer " + token
    data = None
    if body is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8") or "null")


def wait_http(url, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default="D:/qio-dev/win-install-a/qio.exe")
    parser.add_argument("--install-dir", default="D:/qio-dev/win-install-a")
    parser.add_argument("--work", default="D:/qio-dev/_win-r9/board-run")
    parser.add_argument("--cdp-port", type=int, default=9223)
    parser.add_argument("--provider-port", type=int, default=8811)
    args = parser.parse_args()
    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    install_dir = Path(args.install_dir)
    exe = Path(args.exe)
    data_dir = work / "data"
    W.stop_everything([install_dir])
    proc, logfile = W.launch(exe, work, data_dir, args.cdp_port, "board-app")
    lease, win = W.wait_ready(install_dir, proc.pid, timeout=240)
    if not lease or not win:
        print("APP_NOT_READY", flush=True)
        W.stop_everything([install_dir])
        return 2
    token_path = work / "tmp" / ("qio-session-%d.token" % proc.pid)
    token = ""
    for _ in range(100):
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            break
        time.sleep(0.3)
    port = W.backend_port(install_dir)
    print("APP_READY pid=%s backend=%s token=%s" % (proc.pid, port, bool(token)), flush=True)
    # fake provider
    py = "D:/qio-dev/q46-r9/backend/.venv/Scripts/python.exe"
    plog = (work / "logs" / "provider.log").open("wb")
    provider = subprocess.Popen([py, "D:/qio-dev/q46-r9/scripts/e2e_fake_provider.py", "--port", str(args.provider_port)],
                                stdout=plog, stderr=subprocess.STDOUT, cwd="D:/qio-dev/q46-r9")
    if not wait_http("http://127.0.0.1:%d/__health" % args.provider_port, 40):
        print("PROVIDER_NOT_READY", flush=True)
        provider.kill()
        W.stop_everything([install_dir])
        return 3
    # 凭据 + 默认 + fake 回复脚本（只要文本；提交受理不依赖它，但理解轮询不该报错）
    try:
        http("POST", "http://127.0.0.1:%d/__script" % args.provider_port,
             body={"steps": [{"text": "这是假模型回复（r9 验收）。"}]})
        http("POST", "http://127.0.0.1:%d/api/credentials" % port, token=token,
             body={"key_id": "r9-accept", "secret": "sk-r9-accept-fake-0001", "tags": ["main-loop"],
                   "endpoint": "http://127.0.0.1:%d/v1" % args.provider_port,
                   "default_model": "fake-model", "budget": 100000})
        http("POST", "http://127.0.0.1:%d/api/credentials/r9-accept/default" % port, token=token, body={})
        print("CREDENTIAL_OK", flush=True)
    except Exception as exc:  # noqa: BLE001
        print("CREDENTIAL_FAIL %r" % (exc,), flush=True)
    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"QIO_CDP_PORT": str(args.cdp_port), "QIO_API_PORT": str(port),
                "QIO_API_TOKEN": token, "QIO_OUT": str(out_dir)})
    run = subprocess.run(["node", str(VERIFY / "board_scenarios.mjs")], capture_output=True, text=True,
                         timeout=600, env=env)
    stdout = (run.stdout or "") + ("\n" + run.stderr if run.stderr else "")
    (work / "logs" / "board-scenarios.log").write_text(stdout, encoding="utf-8")
    print(stdout[-6000:], flush=True)
    W.stop_everything([install_dir])
    try:
        provider.kill()
    except Exception:
        pass
    return run.returncode


if __name__ == "__main__":
    sys.exit(main())
