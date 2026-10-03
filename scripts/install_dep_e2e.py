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

无系统 Python（P4 轮新增，plan §2.4；第 2 步验收起接进 CI 的 install e2e 任务）：

  python scripts/install_dep_e2e.py --install-dir <安装目录> --work-dir <目录> --no-system-python
         [--clear-pip-cache]

这一档做四件事（缺任何一条都算**没验**：对应断言会 FAIL 并让整轮非 0 退出，不会继续跑出一条
看着漂亮的结论）：

  1. **显式断言"此刻系统 Python 不可用"**：先把子进程可见的 PATH 收窄到不含任何
     python.exe / py.exe，再用**同一份 env** 跑探针 —— where python / python3 / pythonw / py
     全部找不到，且 `py -0p` 要么跑不起来、要么一个 python.exe 都不列（产品
     tools/tool_envs.py 找机器上的 Python 走的就是这两条）。探针原文 + 注册表里登记的 Python
     （只读证据）一起写进 evidence/system-python-unavailable.txt 并打印到日志。
     第一轮收窄不干净时按"新 VM 语义"再收一次（只留 System32 / Wbem / WindowsPowerShell）；
     两轮都不干净就直接 FAIL 退出 —— 能看见系统 Python 的机器上跑出来的"用了自带运行时"是假绿。
  2. **断言起点干净**：工具环境目录（data/tool-envs）预先不存在、数据目录为空、后端 env 里没有
     PYTHONHOME / PYTHONPATH / PYTHONSTARTUP / VIRTUAL_ENV 之类的东西。
     另外（--no-docker）：PATH 上也不能露出 docker —— 产品的执行器是 auto（docker 守护进程应答
     就走容器路径，见 tools/sandbox.py::effective_executor），而这条验收要证的是**宿主路径**
     （用户机器上没装 Docker 的那一类）。CI 的 runner 自带 Docker（Windows 容器模式，拉不了
     python:3.11-slim）：不摘掉它，D-012 会走容器路径并在那里失败 —— 那是另一个结论，
     不是这条验收的证据。
  3. 由脚本注入 QIO_BUNDLED_PYTHON_DIR（模拟外壳按 §2.3 解析 resource_dir()/python-runtime），
     让安装版后端用自带运行时建出依赖环境、装依赖、并**真的调用**声明第三方依赖的工具，
     再核对"工具返回的版本 == 锁定清单里锁的版本"（D-022）。
     （解释器来自自带运行时这条写进 evidence/no-system-python-evidence.txt：pyvenv.cfg 的
     home、qio-env.json 的 python.base、以及自带运行时自己的版本输出。）
  4. 默认再起一个子进程做**对照**：同样的收窄 PATH，但自带运行时指向不存在的目录 —— 断言
     dev_run_tests 明确失败且说的是"需要 Python / 用 QIO_PYTHON 指定"这类可行动的话，
     并且没有静默换解释器把环境建出来（D-110）。

"装依赖要不要联网"照证据说，不推断：装第三方依赖走环境里 pip 的默认索引，本脚本**不**把源指向
本地。D-053 用 pip 自己的 HTTP 缓存作答 —— 装之前缓存里没有该包的下载产物、装之后有了，说明这次
安装真的发生了网络取回（判 PASS）；装之前缓存里就有，只能证明"缓存命中时能装上"，**不能**证明
"需要联网"（如实记 WARN）。CI 上带 --clear-pip-cache 从冷缓存起步（真实用户第一次用到依赖工具
时就是冷缓存；只删 <pip 缓存>\http 与 http-v2，不动任何系统状态），让这条可判定。

  诚实边界：系统里那个 Python 仍在盘上，只是这个进程看不见它 —— 不是"干净 VM 上验证过"；
  也不是"物理断网验证过"（断网只有 D-050 的近似口径 + D-052 的 WARN）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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


# ---------------------------------------------------------------- 无系统 Python 口径
#
# 「用户机器上没有 Python」这件事在本机**不能真的验**（不能把系统 Python 卸掉）。
# 能做到的等价条件，以及它差在哪：
#   * 子进程可见的 PATH 收窄到不含任何 python.exe / py.exe —— 并且用 where 探针**证明**收干净了
#     （探针输出一起留证，不靠"我以为"）；
#   * QIO_PYTHON 不传（显式指定优先级最高，传了就不是"无系统 Python"这条口径）；
#   * 由外壳注入的 QIO_BUNDLED_PYTHON_DIR 指向安装目录里的 python-runtime；
#   * （--no-docker）PATH 里也不露出 docker：产品的执行器 auto 在有可用 docker 守护进程时
#     走容器路径，那条路径验不到"自带运行时建环境"。
# 差在哪：系统里那个 Python 仍然在盘上，只是这个进程看不见它；py 启动器因为 py.exe 不在
# PATH 上也探不到。这不是"在干净 VM 上验证过"，是等价条件 —— 结论只能按这个口径写。

PYTHON_EXE_NAMES = ("python.exe", "python3.exe", "pythonw.exe", "py.exe")
PYTHON_PROBE_NAMES = ("python", "python3", "pythonw", "py")
# 宿主依赖环境这条口径还要 docker 不可达：产品的执行器默认 auto，docker 守护进程应答就走
# 容器路径（tools/sandbox.py::effective_executor）。见 --no-docker。
DOCKER_EXE_NAMES = ("docker.exe",)


def where_exe() -> str:
    """where.exe 的绝对路径。

    探针**不能**靠 "where" 这个名字：PATH 被收窄之后它自己就可能解析不到，那时探针会把
    "找不到 where" 误读成 "找不到 python" —— 那是假证据，不是证据。
    """
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return str(Path(root) / "System32" / "where.exe")


def minimal_shell_path() -> list[str]:
    """"新 VM 语义"的最小 PATH：只有裸 Windows 的 shell 目录，一个解释器都不会露出来。"""
    root = Path(os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows")
    rels = ("System32", r"System32\Wbem", r"System32\WindowsPowerShell\v1.0")
    return [str(root / rel) for rel in rels if (root / rel).is_dir()]


def sanitized_path(path_value: str, *, drop_docker: bool = False
                   ) -> tuple[str, list[str], list[dict]]:
    """把 PATH 里能露出 Python / py 启动器（以及 docker，当 drop_docker）的目录摘掉。

    返回 (新 PATH, 被摘掉的目录, 每条的摘除理由)。理由要留证：这是"模拟用户机器"的口径，
    读日志的人必须一眼看到摘了什么、为什么摘。
    """
    kept: list[str] = []
    dropped: list[str] = []
    reasons: list[dict] = []
    names = list(PYTHON_EXE_NAMES) + (list(DOCKER_EXE_NAMES) if drop_docker else [])
    for raw in (path_value or "").split(os.pathsep):
        entry = raw.strip()
        if not entry:
            continue
        hits = [name for name in names if (Path(entry) / name).exists()]
        looks_like_python = "python" in Path(entry).name.lower()
        if hits or looks_like_python:
            dropped.append(entry)
            reasons.append({"entry": entry, "hit": hits or ["目录名里带 python"]})
            continue
        kept.append(entry)
    return os.pathsep.join(kept), dropped, reasons


def probe_no_python(env: dict) -> tuple[bool, str]:
    """用**同一份 env** 跑 where 探针：python / python3 / pythonw / py 都必须找不到。"""
    lines: list[str] = ["$ where.exe = %s（用绝对路径，免得「找不到 where」被误读成「找不到 python」）"
                        % where_exe()]
    found: list[str] = []
    for name in PYTHON_PROBE_NAMES:
        code, out = base.run([where_exe(), name], timeout=60, env=env, tag="where-%s" % name)
        text = (out or "").strip()
        first = text.splitlines()[0] if text else ""
        if code == 0 and first:
            found.append("%s -> %s" % (name, first))
            lines.append("$ where %s\n%s" % (name, text))
        else:
            lines.append("$ where %s\n（未找到，exit=%s）%s" % (name, code, text[:120]))
    return (not found), "\n".join(lines)


def py_launcher_probe(env: dict) -> tuple[str, str]:
    """`py -0p` 探针：产品 _py_launcher_pythons() 找机器上的解释器就是这么找的。

    返回 (结论, 原文)。结论三种：启动器不可达 / 没列出解释器 / 列出了 N 个解释器。
    """
    launcher = shutil.which("py", path=env.get("PATH", ""))
    if not launcher:
        return "不可达（PATH 上没有 py.exe）", "（没有运行：py 启动器不在 PATH 上）"
    try:
        code, out = base.run([launcher, "-0p"], timeout=60, env=env, tag="py-0p")
    except OSError as exc:  # noqa: BLE001
        return "不可达（%s）" % exc, "（启动失败：%r）" % (exc,)
    listed = re.findall(r"([A-Za-z]:\\[^\r\n]*?python\.exe)\s*$", out or "", re.M)
    text = "$ %s -0p -> exit=%s\n%s" % (launcher, code, (out or "").strip() or "（无输出）")
    if not listed:
        return "没列出任何解释器", text
    return "列出了 %d 个解释器" % len(listed), text


def probe_no_docker(env: dict) -> tuple[bool, str]:
    """证明 docker 命令行在这份 env 里不可达（= 用户机器上没装 Docker 的那一类）。

    为什么这条也要断言：产品执行器是 auto，docker 守护进程应答就走**容器**路径 —— 那时
    "用安装包自带的 Python 建环境"根本没有被执行，D-012 的成功也证明不了宿主路径。
    """
    if not DOCKER_EXE_NAMES:
        return True, "（没有启用 docker 排除）"
    code, out = base.run([where_exe(), "docker"], timeout=60, env=env, tag="where-docker")
    text = (out or "").strip()
    clean = not (code == 0 and text)
    return clean, "$ where docker -> exit=%s\n%s" % (code, text or "（未找到）")


def _looks_like_registered_interpreter(value: str) -> bool:
    """注册表里只挑"解释器路径"那些值（DisplayName / 帮助 URL / chm 之类不算）。"""
    text = value.strip()
    return bool(re.search(r"(?i)(python|pythonw)\.exe$", text)
                or re.search(r"(?i)\\python[0-9.]+\\?$", text))


def registry_pythons() -> list[str]:
    """只读证据：机器注册表里登记了哪些 Python（py 启动器就是照这些列的）。

    这不是"可达性"证据 —— 登记了但启动器拿不到，产品同样用不上。写进证据只是让读日志的人
    一眼看到"这台机器其实装过 Python"，免得把这轮结论误读成"在一台没装过 Python 的机器上验过"。
    """
    if os.name != "nt":
        return []
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    reg = str(Path(root) / "System32" / "reg.exe")
    entries: list[str] = []
    for key in (r"HKLM\SOFTWARE\Python\PythonCore",
                r"HKLM\SOFTWARE\WOW6432Node\Python\PythonCore",
                r"HKCU\SOFTWARE\Python\PythonCore"):
        try:
            code, out = base.run([reg, "query", key, "/s"], timeout=60, tag="reg-query-python")
        except OSError:
            continue
        if code != 0:
            continue
        hive = key.split("\\")[0]
        version = "?"
        for line in (out or "").splitlines():
            text = line.strip()
            head = re.search(r"\\PythonCore\\([0-9][0-9.]*)\\InstallPath$", text)
            if head:
                version = head.group(1)
                continue
            value = re.search(r"REG_SZ\s+(.+?)\s*$", text)
            if value and _looks_like_registered_interpreter(value.group(1)):
                entries.append("%s %s -> %s" % (hive, version, value.group(1).strip()))
    return sorted(set(entries))


def assert_system_python_unavailable(env: dict) -> dict:
    """显式断言：用后端将要拿到的那份 env，此刻**拿不到**任何系统 Python。

    判据（两条都要成立，比"没有可用的 3.11"更严格）：
      1) where python / python3 / pythonw / py 全部找不到；
      2) `py -0p` 跑不起来，或一个 python.exe 都不列。
    任何一条不成立 → 这次"无系统 Python"的口径就不成立：必须停在这里，不能继续跑出
    "后端用了自带运行时"的结论（那可能是它悄悄用了机器上的解释器）。
    """
    where_clean, where_text = probe_no_python(env)
    verdict, launcher_text = py_launcher_probe(env)
    launcher_clean = verdict in ("不可达（PATH 上没有 py.exe）", "没列出任何解释器")
    registry = registry_pythons()
    ok = where_clean and launcher_clean
    text = "\n".join([
        "== 断言：此刻系统 Python 不可用（用后端将拿到的那份 env 探） ==",
        "PATH = %s" % env.get("PATH", ""),
        "",
        where_text,
        "",
        launcher_text,
        "",
        "注册表里登记的 Python（只读证据；登记 ≠ 此刻可达）: %s"
        % ("；".join(registry) if registry else "（三处 PythonCore 都没读到 InstallPath）"),
        "",
        "PYTHONHOME=%s；PYTHONPATH=%s；PYTHONSTARTUP=%s（三者都必须不存在）"
        % (env.get("PYTHONHOME", "（没有这个变量）"), env.get("PYTHONPATH", "（没有这个变量）"),
           env.get("PYTHONSTARTUP", "（没有这个变量）")),
        "",
        "结论：%s" % ("**系统 Python 不可用** —— where 探针与 py -0p 两条都拿不到解释器"
                      if ok else
                      "**断言不成立** —— 这个环境里还能拿到系统 Python，本轮结论不能算数"),
    ])
    return {"ok": ok, "text": text, "where_clean": where_clean, "launcher_verdict": verdict,
            "registry": registry}


def interpreter_info(python_exe: Path) -> dict | None:
    """跑一次自带运行时的 python -c 拿版本 —— 证据里要有"这个解释器是哪个"。"""
    if not python_exe.is_file():
        return None
    code, out = base.run([str(python_exe), "-c",
                          "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2]); "
                          "print(sys.version.split()[0])"],
                         timeout=120, tag="bundled-runtime-version")
    lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
    if code != 0 or len(lines) < 2:
        return {"exe": str(python_exe), "error": (out or "").strip()[-200:]}
    return {"exe": lines[0], "major_minor": lines[1], "version": lines[2] if len(lines) > 2 else ""}


def read_pyvenv_cfg(env_dir: Path) -> str:
    try:
        return (env_dir / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------- 联网证据（D-053）
#
# "装依赖要不要联网"不靠推断作答。装第三方包走的是环境里 pip 的默认索引（产品不把源指向
# 本地，脚本也不指），所以用 pip 自己的 HTTP 缓存作证据：
#   * 装之前缓存里没有该包的下载产物、装之后有了 → 这次安装真的从索引取回了一份产物（要联网）；
#   * 装之前缓存里就有 → 只能证明"缓存命中时能装上"，**不能**证明"需要联网"（记 WARN）；
#   * 两条都对不上（缓存目录猜错 / 没有 url）→ 记 WARN，不猜。
# CI 上用 --clear-pip-cache 从冷缓存起步（真实用户第一次用依赖工具就是冷缓存），只删
# <pip 缓存>\http 与 http-v2 两个子目录，不动任何系统状态。


def pip_cache_dir() -> Path:
    """pip 的 HTTP 缓存目录。

    为什么是"算"而不是"问"：自带运行时把 site-packages 里的 pip 裁掉了（venv 的 pip 来自
    ensurepip），装之前没有能跑 `-m pip cache dir` 的解释器。按 pip 在 Windows 上的实际口径
    （%LOCALAPPDATA%\pip\Cache）算一个，等环境建好后用环境里的 pip 核实；核实对不上就在
    D-053 里如实写"目录存疑"，不拿错目录当证据。
    """
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "pip" / "Cache"


def cache_snapshot(directory: Path) -> dict[str, int]:
    """缓存目录的文件指纹：相对路径 -> 字节数。"""
    snapshot: dict[str, int] = {}
    if not directory.is_dir():
        return snapshot
    for path in directory.rglob("*"):
        if path.is_file():
            try:
                snapshot[str(path.relative_to(directory))] = path.stat().st_size
            except OSError:
                continue
    return snapshot


def cache_key_for_url(url: str) -> str:
    """pip 的 http-v2 缓存文件名 = sha224(url)（前 5 位做目录分片）。"""
    return hashlib.sha224(url.encode("utf-8")).hexdigest()


def cached_entry(snapshot: dict[str, int], url: str) -> str | None:
    """这份快照里有没有该 url 的缓存条目（.body 是响应体）。"""
    digest = cache_key_for_url(url)
    for rel in snapshot:
        normalized = rel.replace("\\", "/")
        if normalized.endswith(digest + ".body") or normalized.endswith(digest):
            return rel
    return None


def clear_pip_cache(cache_dir: Path) -> dict:
    """清空 pip 的 HTTP 缓存（只认 <...>\pip\Cache 结尾的目录），返回删了什么。"""
    result: dict = {"dir": str(cache_dir), "refused": False, "files": 0, "bytes": 0, "reason": ""}
    normalized = str(cache_dir).replace("/", "\\").lower()
    if not normalized.endswith("\\pip\\cache"):
        result["refused"] = True
        result["reason"] = "路径不是 <...>\\pip\\Cache 结尾：拒绝删除（不做破坏性操作）"
        return result
    for name in ("http", "http-v2"):
        target = cache_dir / name
        snapshot = cache_snapshot(target)
        result["files"] += len(snapshot)
        result["bytes"] += sum(snapshot.values())
        shutil.rmtree(target, ignore_errors=True)
    return result


def record_network_evidence(cache_dir: Path, before: dict[str, int], after: dict[str, int],
                           lock: dict | None, cleared: dict, pip_reported: str) -> str:
    """D-053：这次装依赖到底有没有走网络。返回给文档用的一句话结论。"""
    packages = (lock or {}).get("packages") or []
    # 锁定清单里的下载地址字段叫 source（_packages_from_report 的结果，见 tool_envs.py）；
    # url 只是老口径的兼容别名。
    urls = [str(p.get("source") or p.get("url") or "") for p in packages
            if (p.get("source") or p.get("url"))]
    hits_before = {url: cached_entry(before, url) for url in urls}
    hits_after = {url: cached_entry(after, url) for url in urls}
    fresh = [url for url in urls if not hits_before.get(url) and hits_after.get(url)]
    cached_already = [url for url in urls if hits_before.get(url)]
    new_entries = sorted(set(after) - set(before))
    guessed = str(cache_dir)
    verified = pip_reported.strip()
    dir_ok = bool(verified) and verified.lower().replace("/", "\\").rstrip("\\").endswith(
        guessed.lower().replace("/", "\\").rstrip("\\"))
    detail = (
        "pip 缓存=%s（环境里的 pip 自报=%s，与本脚本算的一致=%s）；--clear-pip-cache 删除=%s；"
        "缓存条目 before=%d after=%d（新增 %d）；锁定包=%s；命中缓存条目的 url=%s"
        % (guessed, verified or "（没问到）", dir_ok, json.dumps(cleared, ensure_ascii=False),
           len(before), len(after), len(new_entries),
           json.dumps([{"name": p.get("name"), "version": p.get("version"),
                        "source": p.get("source") or p.get("url"), "hash": p.get("hash")}
                       for p in packages], ensure_ascii=False),
           json.dumps({u: hits_after.get(u) for u in urls}, ensure_ascii=False)))
    if not urls:
        base.record("D-053", "装依赖是否需要联网：用 pip 缓存条目回答", "WARN",
                    "锁定清单里没有 url（老 pip / freeze 退路），无法定位缓存条目 → NOT VERIFIED。" + detail)
        return "NOT VERIFIED（锁定清单里没有下载地址，无法定位缓存条目）"
    if fresh:
        # 安装前不在缓存里、安装后在了：不管目录自报对不对，pip 确实往这里写了这次安装的产物。
        base.record("D-053", "装依赖需要联网：本次安装真的从索引取回了产物",
                    "PASS", "安装前缓存里没有、安装后有了：%s。%s"
                    % (json.dumps(fresh, ensure_ascii=False), detail))
        return "需要联网：本次运行里 pip 从索引取回了一份产物（安装前不在 pip 缓存里、安装后在了）"
    if cached_already:
        base.record("D-053", "装依赖是否需要联网：本次是缓存命中，判不了",
                    "WARN", "安装前缓存里已经有这些 url 的产物：%s → 只能证明「缓存命中时能装上」，"
                            "不能证明「需要联网」，也不能推断「离线开箱可用」→ NOT VERIFIED。%s"
                    % (json.dumps(cached_already, ensure_ascii=False), detail))
        return "NOT VERIFIED（本次是缓存命中：既没证明需要联网，也没证明离线可用）"
    base.record("D-053", "装依赖是否需要联网：缓存条目对不上，判不了", "WARN",
                "缓存新增条目=%s（都没对上锁定清单里的 url；缓存目录存疑：自报=%s vs 本脚本算的=%s，"
                "一致=%s）→ NOT VERIFIED。%s"
                % (json.dumps(new_entries[:5], ensure_ascii=False), verified or "（没问到）", guessed,
                   dir_ok, detail))
    return "NOT VERIFIED（缓存条目对不上，无法判定；本轮没有证明需要联网，也没有证明离线可用）"


def write_results(work: Path) -> Path:
    out = work / "results.json"
    out.write_text(json.dumps(base.RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def missing_runtime_control_result(args, tool_ends, err, data_dir: Path) -> int:
    """缺自带运行时那条对照的判定（只在 --control-missing-runtime 下跑）。

    断言方向：dev_run_tests 必须**失败**，失败说明必须是可行动的（提到 Python / 解释器 /
    自带运行时 / QIO_PYTHON），并且**没有**建出任何 ToolEnv（建出来 = 静默换了别的解释器）。
    """
    run_tests = next((item for item in tool_ends if item.get("tool") == "dev_run_tests"), None)
    text = json.dumps({"err": err, "run_tests": run_tests}, ensure_ascii=False)
    root = data_dir / "tool-envs"
    envs = [p.name for p in root.iterdir() if p.is_dir() and p.name != "locks"] if root.is_dir() else []
    actionable = any(word in text for word in
                     ("Python", "解释器", "自带运行时", "运行时", "QIO_PYTHON", "安装包"))
    ok = bool(run_tests) and not run_tests.get("ok") and actionable and not envs
    base.record("D-110", "对照：缺自带运行时 → 可行动的明确失败（不换解释器、不假装可用）",
                "PASS" if ok else "FAIL",
                "dev_run_tests ok=%s；建出的环境=%s；err=%s；输出=%s"
                % ((run_tests or {}).get("ok"), envs or "无", err or "无", text[:400]))
    write_results(Path(args.work_dir))
    return 0 if ok else 1


def run_missing_runtime_control(args) -> None:
    """起一个**子进程**跑同一份脚本：同样收窄的 PATH，但 QIO_BUNDLED_PYTHON_DIR 指向不存在的目录。

    用全新的空数据目录与独立端口；子进程自己的结论（D-110）写进它的 results.json，
    父进程只把那条结论并进来（原始输出也一并留证）。
    """
    control_dir = Path(args.work_dir) / "control-missing-runtime"
    shutil.rmtree(control_dir, ignore_errors=True)
    control_dir.mkdir(parents=True, exist_ok=True)
    port = base._free_port(args.port + 1)
    missing = control_dir / "no-such-python-runtime"
    cmd = [sys.executable, str(Path(__file__).resolve()),
           "--install-dir", args.install_dir, "--work-dir", str(control_dir),
           "--port", str(port), "--no-system-python", "--control-missing-runtime",
           "--bundled-runtime-dir", str(missing), "--skip-missing-runtime-control"]
    if args.no_docker:
        # 对照必须和正档同一个口径：docker 可达时子进程会走容器路径，
        # 那样 D-110 验到的就是"容器镜像建不出来"，而不是"缺自带运行时".
        cmd.append("--no-docker")
    code, _out = base.run(cmd, timeout=2400, tag="control-missing-runtime")
    results_path = control_dir / "results.json"
    if not results_path.exists():
        base.record("D-110", "对照：缺自带运行时 → 可行动的明确失败", "FAIL",
                    "子进程（exit=%s）没有写出 results.json —— 对照没跑起来" % code)
        return
    results = json.loads(results_path.read_text(encoding="utf-8"))
    base.evidence("control-missing-runtime-results",
                  json.dumps(results, ensure_ascii=False, indent=2))
    target = next((item for item in results if item["id"] == "D-110"), None)
    state = (target or {}).get("state", "MISSING")
    base.record("D-110", "对照（空数据目录 + 缺自带运行时）：可行动的明确失败",
                "PASS" if state == "PASS" else "FAIL",
                "子进程 exit=%s；它自己的 D-110=%s：%s（原始结果见 evidence/control-missing-runtime-results）"
                % (code, state, str((target or {}).get("detail"))[:300]))


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
    parser.add_argument("--no-system-python", action="store_true",
                        help="无系统 Python 口径：收窄 PATH + 注入 QIO_BUNDLED_PYTHON_DIR")
    parser.add_argument("--bundled-runtime-dir", default="",
                        help="自带运行时目录（默认 <install-dir>/python-runtime）")
    parser.add_argument("--skip-missing-runtime-control", action="store_true",
                        help="跳过「缺自带运行时」的对照断言（对照子进程自己会带这个开关）")
    parser.add_argument("--control-missing-runtime", action="store_true",
                        help="内部开关：本次只跑到开发流程，判定「缺运行时是否明确失败」")
    parser.add_argument("--no-docker", action="store_true",
                        help="把 PATH 上能露出 docker.exe 的目录也摘掉（模拟没装 Docker 的用户机器）："
                             "产品的执行器 auto 会在 docker 可用时走容器路径，那条路径验不到"
                             "「用自带运行时建宿主依赖环境」")
    parser.add_argument("--clear-pip-cache", action="store_true",
                        help="先清空 pip 的 HTTP 缓存再跑（CI 用：从冷缓存起步，让"
                             "「装依赖要不要联网」这条可判定；只删 <pip 缓存>\\http 与 http-v2）")
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

    # -- 联网证据的起点：pip 的 HTTP 缓存（快照 + 可选的冷缓存起步）----------------
    cache_dir = pip_cache_dir()
    cache_cleared: dict = {"cleared": False}
    if args.clear_pip_cache:
        cache_cleared = {"cleared": True, **clear_pip_cache(cache_dir)}
    cache_before = cache_snapshot(cache_dir)

    # -- 无系统 Python 口径：收窄 PATH + 注入自带运行时（plan §2.2/§2.4）--------------
    runtime_dir: Path | None = None
    if args.no_system_python:
        if args.tool_python:
            base.record("D-103", "无系统 Python 口径下没有传 QIO_PYTHON", "FAIL",
                        "--tool-python 传了 %s：显式指定优先级最高，这条口径就验不到自带运行时"
                        % args.tool_python)
        else:
            base.record("D-103", "无系统 Python 口径下没有传 QIO_PYTHON", "PASS",
                        "只注入了 QIO_BUNDLED_PYTHON_DIR（外壳在冻结态就是这么做的）")
        # 第一轮：把 PATH 上所有能露出 python.exe / py.exe 的目录摘掉。
        new_path, dropped, dropped_reasons = sanitized_path(
            os.environ.get("PATH", ""), drop_docker=args.no_docker)
        extra["PATH"] = new_path
        probe_env = {**base.clean_env(args, args.port), **extra}
        probe_env["QIO_DATA_DIR"] = str(data_dir)
        clean, probe_text = probe_no_python(probe_env)
        rounds = [{"round": 1, "why": "摘掉 PATH 上所有能露出解释器的目录", "path": new_path,
                   "dropped": dropped, "clean": clean, "probe": probe_text}]
        if not clean:
            # 第一轮不够：PATH 上还剩着能露出解释器的目录（py.exe 住在 C:\Windows 是常见情况）。
            # 按「新 VM 语义」再收一次：只留裸 Windows 的 shell 目录，重探。
            minimal = minimal_shell_path()
            extra["PATH"] = os.pathsep.join(minimal)
            probe_env["PATH"] = extra["PATH"]
            clean2, probe_text2 = probe_no_python(probe_env)
            rounds.append({"round": 2, "why": "第一轮不够 → 按新 VM 语义只留 shell 目录",
                           "path": extra["PATH"], "minimal_shell_path": minimal,
                           "clean": clean2, "probe": probe_text2})
            clean, probe_text = clean2, probe_text2
        # 显式断言「此刻系统 Python 不可用」：where 探针 + py -0p 两条都拿不到解释器。
        assertion = assert_system_python_unavailable(probe_env)
        # docker 也要不可达（--no-docker）：不然 D-012 会走容器路径，验不到宿主环境这条。
        if args.no_docker:
            docker_clean, docker_text = probe_no_docker(probe_env)
        else:
            docker_clean, docker_text = None, "（没有启用 --no-docker：本轮不排除容器执行器）"
        base.log("== 「此刻系统 Python 不可用」断言证据（原文也落 evidence/system-python-unavailable）==")
        for line in assertion["text"].splitlines():
            base.log("   " + line)
        base.record("D-100", "前提断言：此刻系统 Python 不可用（where 探针 + py -0p 都拿不到解释器）",
                    "PASS" if (clean and assertion["ok"]) else "FAIL",
                    "PATH 收窄轮数=%d；被摘掉的目录=%s；py -0p=%s；where 探针干净=%s；探针原文见 "
                    "evidence/system-python-unavailable"
                    % (len(rounds), dropped or "无", assertion["launcher_verdict"],
                       assertion["where_clean"]))
        base.evidence("system-python-unavailable", assertion["text"] + "\n\n" + "\n\n".join(
            "-- 第 %d 轮 PATH 收窄（%s）--\nPATH = %s\n%s"
            % (r["round"], r["why"], r["path"], r["probe"]) for r in rounds))
        runtime_dir = Path(args.bundled_runtime_dir).resolve() if args.bundled_runtime_dir \
            else Path(args.install_dir) / "python-runtime"
        extra["QIO_BUNDLED_PYTHON_DIR"] = str(runtime_dir)
        runtime_python = runtime_dir / "python.exe"
        base.log("== docker 可达性（--no-docker）==")
        for line in docker_text.splitlines():
            base.log("   " + line)
        base.record("D-107", "前提断言：docker 不可达（执行器不会被 auto 选到容器路径）",
                    "PASS" if docker_clean else ("FAIL" if docker_clean is False else "WARN"),
                    ("%s；被摘掉的 PATH 条目（含理由）=%s"
                     % (docker_text.replace("\n", " | "),
                        json.dumps([r for r in dropped_reasons
                                    if any(n in r["hit"] for n in DOCKER_EXE_NAMES)], ensure_ascii=False))
                     if args.no_docker else
                     "没有开 --no-docker：产品可能走容器执行器，本轮不覆盖宿主依赖环境这条路径"))
        base.record("D-101", "安装目录里的自带运行时存在（python.exe）",
                    "PASS" if runtime_python.is_file() else "FAIL",
                    "%s；sha256=%s" % (runtime_python, sha256_of(runtime_python)[:16]
                                       if runtime_python.is_file() else "（不存在）"))
        # 起点干净之一：工具环境目录**预先不存在**（不是"拿一个早就建好的环境当证据"）。
        tool_envs_root = data_dir / "tool-envs"
        leftovers = sorted(p.name for p in data_dir.iterdir()) if data_dir.is_dir() else []
        fresh = (not tool_envs_root.exists()) and not leftovers
        base.record("D-105", "前提断言：没有预置的工具环境（data/tool-envs 不存在、数据目录为空）",
                    "PASS" if fresh else "FAIL",
                    "QIO_DATA_DIR=%s；tool-envs 已存在=%s；数据目录里已有的项=%s"
                    % (data_dir, tool_envs_root.exists(), leftovers or "无"))
        # 起点干净之二：后端 env 里没有会改变解释器解析的变量。
        leaked = sorted(k for k in probe_env
                        if k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
                                 "VIRTUAL_ENV", "CONDA_PREFIX", "PIP_INDEX_URL", "PIP_NO_INDEX")
                        or k.startswith(("PYTHON", "UV_", "PIP_")))
        base.record("D-106", "前提断言：后端 env 里没有 PYTHONHOME/PYTHONPATH/PIP_* 之类会改解释器或索引的变量",
                    "PASS" if not leaked else "FAIL",
                    "泄露的变量=%s；本轮注入的变量=%s" % (leaked or "无", sorted(extra)))
        base.evidence("no-system-python-prereq", json.dumps(
            {"path_rounds": [{k: r.get(k) for k in ("round", "why", "path", "clean")} for r in rounds],
             "dropped_path_entries": dropped,
             "dropped_path_reasons": dropped_reasons,
             "docker_probe": docker_text,
             "docker_unreachable": docker_clean,
             "assertion_text": assertion["text"],
             "where_clean": assertion["where_clean"],
             "py_launcher_verdict": assertion["launcher_verdict"],
             "registry_pythons": assertion["registry"],
             "runtime_dir": str(runtime_dir),
             "QIO_BUNDLED_PYTHON_DIR": extra["QIO_BUNDLED_PYTHON_DIR"],
             "QIO_PYTHON": extra.get("QIO_PYTHON") or "（没有传）",
             "tool_env_dir_preexisting": tool_envs_root.exists(),
             "data_dir_leftovers": leftovers,
             "pip_cache_dir": str(cache_dir),
             "pip_cache_cleared": cache_cleared,
             "pip_cache_entries_before": len(cache_before)}, ensure_ascii=False, indent=2))
        if not (clean and assertion["ok"] and docker_clean in (True, None)):
            base.log("!! 「系统 Python 不可用」这条前提不成立：本轮不能证明「用了自带运行时」，"
                     "停在这里、不写通过结论。")
            write_results(work)
            return 7

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
        # 装依赖就发生在这一步里：装完立刻取 pip 缓存的第二份快照（D-053 的"after"）。
        cache_after = cache_snapshot(cache_dir)
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
        # 注意：tool 失败时 content_preview 往往是空的，只印它会得出"没有事件"这种**误导性的**
        # 结论（本机实测踩到：明明有 TOOL_END，却因为 preview 为空被印成"没有 dev_run_tests 事件"）。
        base.record("D-012", "dev_run_tests（装依赖 + 跑测试）",
                    "PASS" if run_tests and run_tests.get("ok") else "FAIL",
                    json.dumps({k: (run_tests or {}).get(k) for k in
                                ("ok", "status", "error", "category", "content_preview")},
                               ensure_ascii=False)[:600] if run_tests else "没有 dev_run_tests 事件")
        submit = next((t for t in tool_ends if t.get("tool") == "dev_submit_tool"), None)
        base.record("D-013", "dev_submit_tool 注册成功", "PASS" if submit and submit.get("ok") else "FAIL",
                    json.dumps({k: (submit or {}).get(k) for k in
                                ("ok", "status", "error", "category", "content_preview")},
                               ensure_ascii=False)[:600] if submit else "没有 dev_submit_tool 事件")

        if args.control_missing_runtime:
            # 对照只跑到这里：判定「缺自带运行时 → 明确失败」就够了，后面几步没有对象可查。
            return missing_runtime_control_result(args, tool_ends, err, data_dir)

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

        # -- D-053 联网证据：这次装依赖到底有没有走网络（拿 pip 自己的 HTTP 缓存作答）--
        lock_record = (records[0].get("lock") if records else None) or {}
        pip_reported = "（环境里没有解释器，没能核实缓存目录）"
        if records:
            env_python = data_dir / "tool-envs" / records[0]["fingerprint"] / "Scripts" / "python.exe"
            if env_python.is_file():
                _code, _out = base.run([str(env_python), "-m", "pip", "cache", "dir"], timeout=120,
                                       tag="pip-cache-dir")
                _text = (_out or "").strip()
                pip_reported = _text.splitlines()[-1] if _text else "（没有输出）"
        network_conclusion = record_network_evidence(cache_dir, cache_before, cache_after,
                                                    lock_record, cache_cleared, pip_reported)
        base.log("== 联网结论（D-053）：%s ==" % network_conclusion)
        base.evidence("network-evidence", json.dumps({
            "pip_cache_dir_guessed": str(cache_dir),
            "pip_cache_dir_reported_by_env_pip": pip_reported,
            "cleared": cache_cleared,
            "entries_before": len(cache_before),
            "entries_after": len(cache_after),
            "new_entries": sorted(set(cache_after) - set(cache_before))[:20],
            "lock_packages": lock_record.get("packages"),
            "conclusion": network_conclusion,
        }, ensure_ascii=False, indent=2))

        # -- D-102/D-104 自带运行时口径：解释器到底是谁（写进证据，不靠推断）---------
        if args.no_system_python and runtime_dir is not None:
            env_dir = data_dir / "tool-envs" / records[0]["fingerprint"] if records else None
            cfg_text = read_pyvenv_cfg(env_dir) if env_dir else ""
            manifest_python = (records[0].get("manifest") or {}).get("python") if records else None
            lock_python = ((records[0].get("lock") or {}).get("python") if records else None) or {}
            info = interpreter_info(runtime_dir / "python.exe")
            base_dir = str((manifest_python or {}).get("base") or "")
            from_runtime = bool(base_dir) and base_dir.lower().replace("/", "\\").startswith(
                str(runtime_dir).lower().replace("/", "\\"))
            base.record("D-104", "ToolEnv 的解释器来自安装目录里的自带运行时（不是机器上的 Python）",
                        "PASS" if from_runtime else "FAIL",
                        "pyvenv.cfg 里的 home=%r（期望在 %s 下）；配置里记的解释器记录=%s"
                        % (base_dir, runtime_dir, json.dumps(manifest_python, ensure_ascii=False)))
            version_match = bool(info) and bool(lock_python.get("major_minor")) and \
                info.get("major_minor") == lock_python.get("major_minor")
            base.record("D-102", "自带运行时的版本 == 锁定清单里记的版本（同一解释器）",
                        "PASS" if version_match else "FAIL",
                        "自带运行时=%s；锁清单 python=%s；pyvenv.cfg 版本=%s"
                        % (json.dumps(info, ensure_ascii=False), json.dumps(lock_python, ensure_ascii=False),
                           (manifest_python or {}).get("version")))
            base.evidence("no-system-python-evidence", json.dumps({
                "runtime_dir": str(runtime_dir),
                "runtime_interpreter": info,
                "QIO_BUNDLED_PYTHON_DIR": extra.get("QIO_BUNDLED_PYTHON_DIR"),
                "QIO_PYTHON": extra.get("QIO_PYTHON") or "（没有传）",
                "tool_env_fingerprint": records[0]["fingerprint"] if records else None,
                "pyvenv_cfg": cfg_text,
                "qio_env_json_python": manifest_python,
                "lock_python": lock_python,
            }, ensure_ascii=False, indent=2))

        # -- D-021 注册后调用（重启前） ---------------------------------------
        call, _ap, err = run_tool_turn(client, sse, fp, TOOL_NAME, {"mode": "version"})
        version_before = extract_version(str((call or {}).get("content_preview") or ""))
        base.record("D-021", "注册后可调用（重启前）", "PASS" if call and call.get("ok") else "FAIL",
                    "ok=%s version=%s err=%s content=%s" % (
                        (call or {}).get("ok"), version_before, err or "无",
                        str((call or {}).get("content_preview"))[:200]))
        locked_six = next((p for p in (lock_record.get("packages") or [])
                           if str(p.get("name") or "").lower().replace("_", "-") == "six"), None)
        locked_version = str((locked_six or {}).get("version") or "")
        base.record("D-022", "调用结果正确：工具返回的 six 版本 == 锁定清单里锁的版本",
                    "PASS" if (call and call.get("ok") and locked_version
                               and version_before == locked_version) else "FAIL",
                    "工具返回 version=%r；锁定清单里的 six=%s"
                    % (version_before, json.dumps(locked_six, ensure_ascii=False)))

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

        # -- D-110 对照：缺自带运行时 → 明确失败（子进程，独立空数据目录）--------
        if args.no_system_python and not args.skip_missing_runtime_control:
            run_missing_runtime_control(args)

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
        write_results(Path(args.work_dir))
        return 1 if failed else 0
    finally:
        stop_backend_mine(backend, Path(args.work_dir))
        if fp_proc is not None:
            fp_proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
