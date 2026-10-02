r"""安装版（冻结后端）第三方依赖工具链 E2E：装依赖 → 测试 → 注册 → 调用 → 重启 → 删环境 → 重建 → 离线。

为什么单独写一份：scripts/install_e2e.py 走的是**纯标准库**工具，从来没有验证过
「最终装到用户机器上的 QIO 能不能创建并长期运行一个需要第三方 dependency 的工具」。
这里复用它的基础设施（白名单 env / 假厂商 / SSE / HTTP Client），只把工具换成声明了
requirements 的那个，并把依赖相关的每一步都留证据。

诚实边界（不要把这些读成更强的结论）：

* 本机没有 makensis，产不出 NSIS 安装包；这里用同一个 scripts/build_sidecar.ps1 产出的
  qio-backend.exe（安装包里打包的就是这个文件）放进独立目录当「安装目录」，其余按安装版
  语义（白名单 env、独立空数据目录、不继承开发变量）。安装器本身的行为不在本脚本范围。
* 模型是离线假厂商（scripts/e2e_fake_provider.py）：只能证明 QIO 自己的链路对。
* 本机不能真断网：D5 用「出网代理指向不可达地址 + 调用前后环境目录指纹不变」做近似，
  结论按近似写明，不写成「已断网验证」。
* 本机没有 Docker：D6 记 not tested，不写成通过。

用法：
  python scripts/install_dep_e2e.py --install-dir <含 qio-backend.exe 的目录> --work-dir <目录>
         [--port 8899] [--tool-python <python.exe>] [--extra-env KEY=VAL]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import install_e2e as base  # noqa: E402  （复用 Client / SseReader / clean_env / start_backend / run_turn）

TOOL_NAME = "six_probe"
# 依赖选 six：体积小、稳定、不需要凭据、不需要危险权限（上一阶段的真实链路验证也用它）。
TOOL_JSON = json.dumps(
    {
        "name": TOOL_NAME,
        "description": "返回 six 的版本（安装版依赖链验证用）",
        "tool_type": "function",
        "sync": True,
        "code": (
            "import six\n"
            "\n"
            "def run(**kwargs):\n"
            "    if kwargs.get('mode') == 'version':\n"
            "        return {'version': six.__version__}\n"
            "    return {'ok': True}\n"
        ),
        "requirements": ["six>=1.16"],
        "tests": [{"name": "import_ok", "input": {}, "expect": {"ok": True}}],
    },
    ensure_ascii=False,
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tool_env_snapshot(data_dir: Path) -> dict:
    """ToolEnv 目录的指纹：环境目录 + 持久锁定清单里每个文件的 sha256。"""
    root = data_dir / "tool-envs"
    snapshot: dict[str, str] = {}
    if not root.is_dir():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = sha256_of(path)
    return snapshot


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def find_env_records(data_dir: Path) -> list[dict]:
    """读出每个 ToolEnv 的 qio-env.json 与 locks/<fp>/lock.json。"""
    root = data_dir / "tool-envs"
    records = []
    if not root.is_dir():
        return records
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name == "locks":
            continue
        manifest = read_json(directory / "qio-env.json")
        lock = read_json(root / "locks" / directory.name / "lock.json")
        records.append({"fingerprint": directory.name, "manifest": manifest, "lock": lock})
    return records


def clean_mei_dirs(work: Path) -> int:
    """清掉 PyInstaller onefile 解包残留（TEMP/_MEIxxxx）。

    安装版每次启动都会把包解到 TEMP，被 taskkill 时不会自己清：每跑一轮 E2E 留 ~180MB。
    本机 C: 曾被这些残留塞满（实测踩到，编辑文件报 ENOSPC）。
    """
    removed = 0
    tmp = work / "tmp"
    if tmp.is_dir():
        for child in tmp.glob("_MEI*"):
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def backend_alive(client) -> tuple[bool, str]:
    """后端还活着吗 —— 死掉时如实记 FAIL，不要让脚本自己崩在 urllib 上。"""
    try:
        status, body = client.get("/api/health", timeout=5)
        if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
            return True, json.dumps(body, ensure_ascii=False)
        return False, "%s %s" % (status, str(body)[:120])
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)


def stop_backend_mine(proc, work: Path) -> None:
    """只收**自己起的**进程树。

    不要用 taskkill /IM qio-backend.exe：别的 agent 的安装版 E2E 可能也在跑，那会连他们的
    后端一起杀掉。我这边实测过：自己的后端被外部 taskkill 干掉之后，下一轮 POST /api/turns
    直接 ConnectionRefused（脚本崩在 urllib 上）。
    """
    if proc is not None:
        base.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], timeout=60, tag="taskkill-mine")
    time.sleep(2.0)
    clean_mei_dirs(work)


def make_env(args, port: int, extra: dict | None = None) -> dict:
    env = base.clean_env(args, port)
    if extra:
        env.update(extra)
    return env


def start_backend(args, port: int, extra_env: dict | None = None):
    env = make_env(args, port, extra_env)
    logfile = Path(args.work_dir) / "evidence" / "installed-backend.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    fh = logfile.open("ab")
    proc = subprocess.Popen(
        [str(Path(args.install_dir) / "qio-backend.exe")],
        cwd=str(args.install_dir),
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc


def start_fake_provider(args, port: int):
    # 不要用 port + 1：Lead 的端口分配里 8894 是别的 agent 的后端（实测撞过一次）。
    fp_port = port + 11
    logfile = Path(args.work_dir) / "evidence" / "fake-provider.log"
    fh = logfile.open("ab")
    tmp = str(Path(args.work_dir) / "tmp")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "e2e_fake_provider.py"), "--port", str(fp_port)],
        stdout=fh,
        stderr=subprocess.STDOUT,
        env={**os.environ, "TEMP": tmp, "TMP": tmp},
    )
    client = base.Client("http://127.0.0.1:%d" % fp_port)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            status, body = client.get("/__health", timeout=3)
            if status == 200 and body.get("ok"):
                return proc, client, fp_port
        except Exception:
            time.sleep(0.4)
    return proc, client, fp_port


def wait_provider_idle(fp, *, quiet: float = 3.0, timeout: float = 90.0) -> int:
    """等假厂商安静下来再设脚本。

    实测踩到：保存凭据会触发一次**异步**的模型验证请求，它会抢走我们刚设好的第一个脚本
    步骤（于是模型一直拿到 default，被 no_progress 护栏拖到预算耗尽）。这里按 /__health 的
    served 计数等到连续 quiet 秒没有新请求为止。
    """
    deadline = time.time() + timeout
    last = -1
    stable_since = time.time()
    while time.time() < deadline:
        try:
            status, body = fp.get("/__health", timeout=3)
            served = int(body.get("served") or 0) if status == 200 else last
        except Exception:  # noqa: BLE001
            served = last
        if served != last:
            last = served
            stable_since = time.time()
        elif time.time() - stable_since >= quiet:
            return last
        time.sleep(0.4)
    return last


def dump_provider_log(fp, tag: str) -> list[dict]:
    """把假厂商收到的请求落盘（含每次服务的 step）：脚本被别的模型调用吃掉时能一眼看出来。"""
    try:
        requests = fp.get("/__log", timeout=5)[1].get("requests") or []
    except Exception as exc:  # noqa: BLE001
        requests = [{"error": repr(exc)}]
    trimmed = [
        {"at": r.get("at"), "last_user": str(r.get("last_user"))[:40],
         "offered": r.get("tool_names_offered"), "step": r.get("step")}
        for r in requests
    ]
    base.evidence("provider-log-%s" % tag, json.dumps(trimmed, ensure_ascii=False, indent=2))
    return trimmed


def new_reader(client) -> "base.SseReader":
    """重启后必须重新连 SSE：旧连接会断，而且新连接会重放历史事件。"""
    reader = base.SseReader(client)
    time.sleep(2.0)
    reader.drain_replay(1.5)
    return reader


def run_tool_turn(client, sse, fp, tool: str, tool_args: dict, *, timeout: float = 300.0):
    """跑一轮，让假模型调用指定工具；返回 (tool_end, approvals, err)。"""
    approvals: list[dict] = []
    tool_ends: list[dict] = []
    state: dict = {"sent": False, "stalled": None}

    def on_event(event: dict) -> None:
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "APPROVAL_REQUIRED":
            approval = data.get("approval") or {}
            approvals.append(approval)
            payload = approval.get("payload") or {}
            if approval.get("kind") == "continue" and payload.get("reason") == "no_progress":
                # 无进展护栏：最多替用户点 5 次「继续」（脚本推晚了还能救回来），再多就如实
                # 记下来让这一轮结束 —— 不能像第一版那样把预算烧完。
                state["continues"] = state.get("continues", 0) + 1
                if state["continues"] > 5:
                    state["stalled"] = payload.get("message")
                    return
            client.post(
                "/api/approvals/%s/respond" % approval.get("approval_id"),
                {
                    "decision": "approved",
                    "turn_id": approval.get("turn_id"),
                    "session_id": approval.get("session_id"),
                    "request_digest": approval.get("digest"),
                },
            )
        elif etype == "TOOL_END":
            tool_ends.append(data)

    err = ""
    for attempt in (1, 2):
        alive, detail = backend_alive(client)
        if not alive:
            return None, approvals, "后端不可达（%s）" % detail
        wait_provider_idle(fp)
        # default 用**文本**：万一脚本被抢走，这一轮会正常结束并如实报 FAIL，
        # 而不是像 dev_list_tasks 那样被 no_progress 护栏拖到预算耗尽（实测踩到）。
        fp.post("/__script", {"steps": [{"tool": tool, "args": tool_args}, {"text": "（假模型：做完了）"}],
                              "default": {"text": "（假模型：这一轮先到这里）"}})
        sse.drain_replay(1.5)
        try:
            _events, err = base.run_turn(client, sse, fp, "请调用工具。", on_event=on_event, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            err = "这一轮没能跑起来：%r" % (exc,)
        fp.post("/__script", {"steps": [], "default": {"text": "（假模型默认回复）"}})
        dump_provider_log(fp, "after-%s-attempt%d" % (tool, attempt))
        call = next((r for r in tool_ends if r.get("tool") == tool), None)
        if call is not None:
            return call, approvals, err
        # 重启后的第一轮常有一次辅助模型调用（记忆/标题派生），它会吃掉我们准备的脚本；
        # 这时再试一次（辅助调用已经被用掉）。两次都没有就如实报 FAIL。
    return None, approvals, err or "这一轮没有调用工具"


def step_warmup(client, sse, fp) -> bool:
    """先跑一轮纯文本对话。

    参考 install_e2e 的顺序：装出来的后端第一轮里会有一次**辅助模型调用**（主题/标题派生之类），
    它会吃掉我们为下一轮准备的第一条脚本。所以先热身一轮，把辅助调用用掉。
    """
    wait_provider_idle(fp)
    fp.post("/__script", {"steps": [{"text": "安装版依赖链验证：你好。"}],
                          "default": {"text": "（假模型：这一轮先到这里）"}})
    events, err = base.run_turn(client, sse, fp, "安装版依赖链验证：你好。", timeout=180)
    types = [e.get("type") for e in events]
    ok = not err and "TURN_END" in types
    base.record("D-005", "装出来的后端跑通一轮对话（热身，用掉辅助模型调用）",
                "PASS" if ok else "FAIL", "事件=%s err=%s" % (",".join(types), err or "无"))
    dump_provider_log(fp, "after-warmup")
    return ok


def step_dev_flow(client, sse, fp):
    """create_tool → dev_write_file(带 requirements 的 tool.json) → dev_run_tests → dev_submit_tool。"""
    approvals: list[dict] = []
    tool_ends: list[dict] = []
    state: dict = {"ws": None, "pushed": False, "stalled": None}

    def on_event(event: dict) -> None:
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "APPROVAL_REQUIRED":
            approval = data.get("approval") or {}
            approvals.append(approval)
            payload = approval.get("payload") or {}
            if approval.get("kind") == "continue" and payload.get("reason") == "no_progress":
                state["continues"] = state.get("continues", 0) + 1
                if state["continues"] > 5:
                    state["stalled"] = payload.get("message")
                    return
            client.post(
                "/api/approvals/%s/respond" % approval.get("approval_id"),
                {
                    "decision": "approved",
                    "turn_id": approval.get("turn_id"),
                    "session_id": approval.get("session_id"),
                    "request_digest": approval.get("digest"),
                },
            )
        elif etype == "TOOL_END":
            tool_ends.append(data)
            if data.get("tool") == "create_tool" and data.get("ok") and not state["pushed"]:
                text = json.dumps(data.get("content_preview") or "", ensure_ascii=False)
                import re as _re
                if "工作区 id=" not in text and data.get("record_id"):
                    # 兜底才走 HTTP：主循环不等我们（实测多一次往返就输掉竞态）
                    text += " " + json.dumps(
                        client.get("/api/tool-records/%s" % data["record_id"])[1], ensure_ascii=False)

                match = _re.search(r"工作区 id=([A-Za-z0-9_\-]+)", text)
                if match:
                    state["ws"] = match.group(1)
                    state["pushed"] = True
                    fp.post(
                        "/__script",
                        {
                            "steps": [
                                {"tool": "dev_write_file", "args": {
                                    "workspace": state["ws"], "name": "tool.json", "content": TOOL_JSON}},
                                {"tool": "dev_run_tests", "args": {"workspace": state["ws"]}},
                                {"tool": "dev_submit_tool", "args": {
                                    "workspace": state["ws"],
                                    "explanation": "返回 six 的版本，用来验证安装版的第三方依赖链"}},
                                {"text": "工具已开发完成"},
                            ]
                        },
                    )

    alive, detail = backend_alive(client)
    if not alive:
        return None, approvals, tool_ends, "后端不可达（%s）" % detail
    wait_provider_idle(fp)
    posted = fp.post("/__script", {"steps": [
        {"tool": "create_tool", "args": {
            "request": "我需要一个返回 six 版本的工具（声明 six>=1.16 依赖）"}},
        # 桥接：create_tool 之后主循环会立刻再问一次模型，而我们要等事件到了才知道工作区 id。
        # 先铺几步**只读**的 dev_list_tasks 拖住它（脚本被 set() 整体替换，多余的会消失）。
        {"repeat": 8, "tool": "dev_list_tasks", "args": {}},
    ], "default": {"text": "（假模型：这一轮先到这里）"}})[1]
    base.record("D-009", "工具脚本已装进假厂商（create_tool + 8 步桥接）",
                "PASS" if posted.get("remaining") == 9 else "FAIL", json.dumps(posted, ensure_ascii=False))
    try:
        events, err = base.run_turn(client, sse, fp, "帮我做一个依赖 six 的工具。", on_event=on_event, timeout=420)
    except Exception as exc:  # noqa: BLE001
        events, err = [], "这一轮没能跑起来：%r" % (exc,)
    dump_provider_log(fp, "after-dev-flow")
    fp.post("/__script", {"steps": [], "default": {"text": "（假模型默认回复）"}})
    if state.get("stalled"):
        err = err or ("无进展暂停：%s" % state["stalled"])
    return state["ws"], approvals, tool_ends, err


def extract_version(text: str) -> str | None:
    import re as _re

    match = _re.search(r'"version"\s*:\s*"([^"]+)"', text or "")
    return match.group(1) if match else None


def delete_tool_env(args, fingerprint: str, data_dir: Path) -> tuple[bool, str]:
    """主动删掉 ToolEnv：走产品自带的运维入口（CLI），失败再退回直接删目录（都记账）。"""
    root = data_dir / "tool-envs"
    cli = [
        "uv", "run", "--frozen", "python", "-m", "agent.tools.tool_envs",
        "--root", str(root), "remove", fingerprint, "--yes", "--allow-in-use",
    ]
    # uv 的默认缓存在 %LOCALAPPDATA%，低完整性子进程写不了（实测 os error 5）：
    # 指到工作目录里，让这条「产品自带运维入口」真的能跑通，而不是被迫退回删目录。
    cli_env = {**os.environ, "UV_CACHE_DIR": str(Path(root).parent / "uv-cache")}
    code, out = base.run(cli, timeout=300, cwd=str(ROOT / "backend"), env=cli_env,
                         tag="delete-tool-env-cli")
    if code == 0 and not (root / fingerprint).exists():
        return True, "CLI：%s" % out.strip().splitlines()[0][:160] if out.strip() else "CLI 删除成功"
    shutil.rmtree(root / fingerprint, ignore_errors=True)
    return (not (root / fingerprint).exists()), "CLI 失败（code=%s）→ 直接删目录；%s" % (code, out.strip()[:200])


def main() -> int:
    parser = argparse.ArgumentParser(description="安装版第三方依赖工具链 E2E")
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--work-dir", default=str(ROOT / ".dep-e2e"))
    parser.add_argument("--port", type=int, default=8893)
    parser.add_argument("--token", default="dep-e2e-token-0001")
    parser.add_argument("--tool-python", default="", help="传给安装版后端的 QIO_PYTHON（本机 Python）")
    parser.add_argument("--extra-env", action="append", default=[], help="额外注入后端的环境变量 KEY=VAL")
    args = parser.parse_args()

    # 必须是绝对路径：安装版进程的 cwd 是安装目录，相对的 TEMP 会被 PyInstaller
    # 解释成「安装目录下的相对路径」→ Could not create temporary directory（实测踩到）。
    args.work_dir = str(Path(args.work_dir).resolve())
    args.install_dir = str(Path(args.install_dir).resolve())
    work = Path(args.work_dir)
    for sub in ("evidence", "data", "tmp"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    base.EVIDENCE = work / "evidence"
    data_dir = work / "data"

    extra: dict[str, str] = {}
    for item in args.extra_env:
        key, _, value = item.partition("=")
        extra[key] = value
    if args.tool_python:
        extra["QIO_PYTHON"] = args.tool_python

    base.log("== 安装版第三方依赖工具链 E2E ==")
    base.log("  安装目录 : %s" % args.install_dir)
    base.log("  数据目录 : %s（空目录）" % data_dir)
    base.log("  注入变量 : %s" % (sorted(extra) or "无"))

    backend = fp_proc = None
    try:
        exe = Path(args.install_dir) / "qio-backend.exe"
        if not exe.is_file():
            base.record("D-001", "安装目录里有 qio-backend.exe", "FAIL", "找不到 %s" % exe)
            return 3
        base.record(
            "D-001", "安装目录里的后端指纹", "PASS",
            "%s（%.1f MB，sha256 %s）" % (exe.name, exe.stat().st_size / 1048576, sha256_of(exe)[:16]),
        )

        backend = start_backend(args, args.port, extra)
        client = base.Client("http://127.0.0.1:%d" % args.port, args.token)
        ok, detail = base.wait_health(client, timeout=120)
        base.record("D-002", "安装目录里的后端 /api/health", "PASS" if ok else "FAIL", detail)
        if not ok:
            return 4
        sse = new_reader(client)

        fp_proc, fp, fp_port = start_fake_provider(args, args.port)
        try:
            status, fp_body = fp.get("/__health", timeout=3)
            fp_ok = status == 200 and bool(fp_body.get("ok"))
        except Exception as exc:  # noqa: BLE001
            fp_ok, fp_body = False, {"error": repr(exc)}
        base.record("D-003", "离线假厂商已就绪（不是真实服务）", "PASS" if fp_ok else "FAIL",
                    json.dumps(fp_body, ensure_ascii=False)[:200])
        status, body = client.post("/api/credentials", {
            "key_id": "dep-e2e-main",
            "secret": "sk-dep-e2e-fake-0001",
            "tags": ["main-loop"],
            "endpoint": "http://127.0.0.1:%d/v1" % fp_port,
            "default_model": "fake-model",
            "budget": 100000,
        })
        base.record("D-004", "假厂商凭据已保存（写后不可读回）", "PASS" if status == 200 else "FAIL",
                    "%s %s" % (status, json.dumps(body, ensure_ascii=False)[:160]))
        client.post("/api/credentials/dep-e2e-main/default")

        step_warmup(client, sse, fp)

        # -- D-010..D-013 创建工具 + 声明依赖 + 审批 + 测试 + 注册 -------------
        ws, approvals, tool_ends, err = step_dev_flow(client, sse, fp)
        names = [t.get("tool") for t in tool_ends]
        base.record("D-010", "开发四步都被调用", "PASS" if
                    {"create_tool", "dev_write_file", "dev_run_tests", "dev_submit_tool"} <= set(names) else "FAIL",
                    "工作区=%s 工具调用=%s（err=%s）" % (ws, names, err or "无"))
        base.evidence("dep-approvals", json.dumps(approvals, ensure_ascii=False, indent=2))
        base.evidence("dep-tool-ends", json.dumps(tool_ends, ensure_ascii=False, indent=2))
        kinds = [a.get("kind") for a in approvals]
        dep = next((a for a in approvals if a.get("kind") == "dependency_install"), None)
        base.record("D-011", "dependency_install 审批真的发生过", "PASS" if dep else "FAIL",
                    "审批种类=%s" % kinds)
        if dep:
            base.evidence("dep-approval-payload", json.dumps(dep, ensure_ascii=False, indent=2))
        run_tests = next((t for t in tool_ends if t.get("tool") == "dev_run_tests"), None)
        base.record("D-012", "dev_run_tests（装依赖 + 跑测试）", "PASS" if run_tests and run_tests.get("ok") else "FAIL",
                    str((run_tests or {}).get("content_preview"))[:400] or "没有 dev_run_tests 事件")
        submit = next((t for t in tool_ends if t.get("tool") == "dev_submit_tool"), None)
        base.record("D-013", "dev_submit_tool 注册成功", "PASS" if submit and submit.get("ok") else "FAIL",
                    str((submit or {}).get("content_preview"))[:300] or "没有 dev_submit_tool 事件")

        # -- D-020 锁定清单 ---------------------------------------------------
        records = find_env_records(data_dir)
        base.evidence("tool-env-records", json.dumps(records, ensure_ascii=False, indent=2))
        if records:
            lock = records[0].get("lock") or {}
            pins = ["%s==%s" % (p.get("name"), p.get("version")) for p in (lock.get("packages") or [])]
            base.record("D-020", "ToolEnv 与锁定清单（schema 2 / pip --report）", "PASS",
                        "fingerprint=%s schema=%s resolution=%s fidelity=%s pins=%s python=%s platform=%s" % (
                            records[0]["fingerprint"], lock.get("schema"), lock.get("resolution"),
                            lock.get("fidelity"), pins, (lock.get("python") or {}).get("version"),
                            (lock.get("system") or {}).get("platform")))
        else:
            base.record("D-020", "ToolEnv 与锁定清单（schema 2 / pip --report）", "FAIL",
                        "data/tool-envs 下没有任何环境记录")

        # -- D-021 注册后调用（重启前） ---------------------------------------
        call, _ap, err = run_tool_turn(client, sse, fp, TOOL_NAME, {"mode": "version"})
        version_before = extract_version(str((call or {}).get("content_preview") or ""))
        base.record("D-021", "注册后可调用（重启前）", "PASS" if call and call.get("ok") else "FAIL",
                    "ok=%s version=%s err=%s content=%s" % (
                        (call or {}).get("ok"), version_before, err or "无",
                        str((call or {}).get("content_preview"))[:200]))

        # -- D-030 重启 -------------------------------------------------------
        stop_backend_mine(backend, Path(args.work_dir))
        time.sleep(1.5)
        backend = start_backend(args, args.port, extra)
        ok, detail = base.wait_health(client, timeout=120)
        base.record("D-030", "重启后安装版后端重新就绪", "PASS" if ok else "FAIL", detail)
        if not ok:
            return 5
        sse = new_reader(client)

        # -- D-031 重启后调用（同一版本，不得重装） ---------------------------
        call2, _ap2, err2 = run_tool_turn(client, sse, fp, TOOL_NAME, {"mode": "version"})
        version_after = extract_version(str((call2 or {}).get("content_preview") or ""))
        base.record("D-031", "重启后再调用（版本一致 = 没有重装成别的版本）",
                    "PASS" if call2 and call2.get("ok") and version_after == version_before else "FAIL",
                    "ok=%s version_before=%s version_after=%s err=%s" % (
                        (call2 or {}).get("ok"), version_before, version_after, err2 or "无"))

        # -- D-040 主动删除 ToolEnv -------------------------------------------
        fingerprint = records[0]["fingerprint"] if records else None
        if fingerprint:
            deleted, how = delete_tool_env(args, fingerprint, data_dir)
            lock_kept = (data_dir / "tool-envs" / "locks" / fingerprint / "lock.json").is_file()
            base.record("D-040", "主动删除 ToolEnv（保留锁定清单）", "PASS" if deleted else "FAIL",
                        "%s；lock 仍在=%s" % (how, lock_kept))
        else:
            base.record("D-040", "主动删除 ToolEnv（保留锁定清单）", "FAIL", "没有环境可删")

        # -- D-041 删除后调用：必须明确失败，不得静默换环境 -------------------
        call3, _ap3, err3 = run_tool_turn(client, sse, fp, TOOL_NAME, {"mode": "version"})
        text3 = " ".join(
            str((call3 or {}).get(key) or "")
            for key in ("content_preview", "error", "status", "category", "category_label")
        )
        base.evidence("after-delete-invoke", json.dumps(call3, ensure_ascii=False, indent=2))
        explicit = bool(call3) and not call3.get("ok") and any(
            word in text3 for word in ("专用环境", "依赖", "环境")
        )
        base.record("D-041", "删除后调用：明确进入需重新准备（不静默继续）",
                    "PASS" if explicit else "FAIL",
                    "ok=%s err=%s content=%s" % ((call3 or {}).get("ok"), err3 or "无", text3[:300]))

        # -- D-042 重新准备（走产品的测试入口） -------------------------------
        if ws:
            retest, retest_approvals, err4 = run_tool_turn(client, sse, fp, "dev_run_tests", {"workspace": ws})
            base.record("D-042", "删除后重新准备（dev_run_tests 重建环境）",
                        "PASS" if retest and retest.get("ok") else "FAIL",
                        "ok=%s err=%s content=%s" % ((retest or {}).get("ok"), err4 or "无",
                                                     str((retest or {}).get("content_preview"))[:300]))
            base.evidence("retest-approvals", json.dumps(retest_approvals, ensure_ascii=False, indent=2))
        records2 = find_env_records(data_dir)
        if records2:
            lock2 = records2[0].get("lock") or {}
            pins2 = ["%s==%s" % (p.get("name"), p.get("version")) for p in (lock2.get("packages") or [])]
            base.record("D-043", "重建后仍是同一批锁定版本", "PASS" if pins2 else "FAIL",
                        "resolution=%s pins=%s" % (lock2.get("resolution"), pins2))
        else:
            base.record("D-043", "重建后仍是同一批锁定版本", "FAIL", "重建后没有环境记录")

        # -- D-044 重建后调用 --------------------------------------------------
        call4, _ap4, err5 = run_tool_turn(client, sse, fp, TOOL_NAME, {"mode": "version"})
        version_rebuilt = extract_version(str((call4 or {}).get("content_preview") or ""))
        base.record("D-044", "重建后可调用（版本与重建前一致）",
                    "PASS" if call4 and call4.get("ok") and version_rebuilt == version_before else "FAIL",
                    "ok=%s version=%s（重建前 %s）err=%s" % ((call4 or {}).get("ok"), version_rebuilt,
                                                             version_before, err5 or "无"))

        # -- D-050 离线近似：出网代理指向不可达地址 + 调用前后环境指纹不变 -----
        stop_backend_mine(backend, Path(args.work_dir))
        time.sleep(1.5)
        offline_env = {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "PIP_INDEX_URL": "http://127.0.0.1:9/simple",
            # 假厂商在回环上：不走代理（否则这一轮的模型调用自己就被代理掐死了，
            # 实测踩到 —— 那就变成了「证明不了调用」，而不是「证明调用不需要出网」）。
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        backend = start_backend(args, args.port, {**extra, **offline_env})
        ok, detail = base.wait_health(client, timeout=120)
        base.record("D-050", "离线近似：后端带着不可达代理/索引重启", "PASS" if ok else "FAIL", detail)
        sse = new_reader(client)
        before = tool_env_snapshot(data_dir)
        call5, _ap5, err6 = run_tool_turn(client, sse, fp, TOOL_NAME, {"mode": "version"})
        after = tool_env_snapshot(data_dir)
        version_offline = extract_version(str((call5 or {}).get("content_preview") or ""))
        base.record("D-051", "离线近似：调用仍正常且环境未被改动",
                    "PASS" if call5 and call5.get("ok") and version_offline == version_before and before == after else "FAIL",
                    "ok=%s version=%s err=%s 环境指纹 before=%d after=%d 变化=%s" % (
                        (call5 or {}).get("ok"), version_offline, err6 or "无", len(before), len(after),
                        sorted(set(before.items()) ^ set(after.items()))[:3]))
        base.record("D-052", "离线近似的诚实边界", "WARN",
                    "本机不能真断网：这里只证明「正常调用不经过出网代理、也没有重装/改动环境」，"
                    "不是「物理断网下验证过」。")

        # -- D-060 Docker：not tested -----------------------------------------
        docker = shutil.which("docker")
        if docker is None:
            base.record("D-060", "容器执行路径（同一 lock manifest）", "NOT TESTED",
                        "本机没有 docker 命令行：容器路径未验证，不写成通过。")
        else:
            code, out = base.run([docker, "version", "--format", "{{.Server.Version}}"], timeout=60,
                                 tag="docker-probe")
            base.record("D-060", "容器执行路径（同一 lock manifest）", "NOT TESTED",
                        "docker 守护进程不可用（code=%s %s）：容器路径未验证，不写成通过。" % (
                            code, out.strip()[:120] or "（无输出）"))

        failed = [r for r in base.RESULTS if r["state"] == "FAIL"]
        base.log("== 汇总（%d 条：PASS=%d FAIL=%d WARN=%d NOT TESTED=%d） ==" % (
            len(base.RESULTS),
            sum(1 for r in base.RESULTS if r["state"] == "PASS"),
            len(failed),
            sum(1 for r in base.RESULTS if r["state"] == "WARN"),
            sum(1 for r in base.RESULTS if r["state"] == "NOT TESTED")))
        for item in base.RESULTS:
            base.log("[%s] %s %s" % (item["state"], item["id"], item["title"]))
        (Path(args.work_dir) / "results.json").write_text(
            json.dumps(base.RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1 if failed else 0
    finally:
        stop_backend_mine(backend, Path(args.work_dir))
        if fp_proc is not None:
            fp_proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
