"""冻结产物自证：打包后的 qio-backend.exe 里必须真的有材料解析模块，而且真链路能跑通。

为什么需要它（2026-10-12 实测）：agent/interactive/understanding.py 用
importlib.import_module("agent.interactive.materials") 取材料解析模块，PyInstaller 的
静态分析**看不见**动态导入 —— 0.2.0 的安装包里 agent.interactive.board_store /
submission / understanding / execution 都在，唯独没有 materials，于是每次理解都返回
materials=[] 与 honesty.materialsError="材料解析模块不可用：No module named
'agent.interactive.materials'"（用户库 D:\QIO-data\app.db 里就是这么记的）。
源码、单元测试、CI 全绿，用户看到的是「已读取材料 0 项」—— 这类漏包只有对着**打包
产物**才验得出来。

两项检查（默认都跑，任一项失败即非零退出）：

1) 静态：解析 exe 里的 PyInstaller 归档（CArchive cookie → PYZ 的 marshal 模块清单），
   断言清单里有 agent.interactive.materials。清单解析不出来时退回「原始字节里有没有
   这个模块名」的弱检查，并在输出里写明用的是哪种方法（不把弱证据说成强证据）。
2) 真链路：用**临时 QIO_DATA_DIR** 起一个真后端实例（默认带 QIO_SESSION_TOKEN，
   走真实令牌认证，只监听 127.0.0.1），建板面 → 保存一张 code 卡片 → 提交，然后读它
   自己写下的 board_understanding.context，断言 honesty.materialsResolved=true、
   materials 非空、且正文真的被读到。

真链路不需要模型，也不联网：材料解析发生在调用模型**之前**，上下文与 honesty 先落库；
没有凭据时理解会在模型阶段如实失败（记录保留、可从快照重试），不影响我们要断言的事实。

用法：

    python scripts/check_frozen_materials.py                 # 自动找产物
    python scripts/check_frozen_materials.py --exe <path>    # 指定产物
    python scripts/check_frozen_materials.py --static-only   # 只做第 1 项（起不了实例时）
    python scripts/check_frozen_materials.py --keep          # 保留临时数据目录（排障）

退出码：0 = 检查全过；1 = 有失败（逐条打印 [PASS]/[FAIL]，失败带可读原因）。
只用标准库。
"""

from __future__ import annotations

import argparse
import json
import marshal
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 必须在冻结产物里的模块（PyInstaller 静态分析看不见的那一个）。
MODULE_NAME = "agent.interactive.materials"
#: 对照模块：它在 0.2.0 的坏包里就已经存在。用它证明「清单解析器真的读到了模块清单」，
#: 而不是解析出一个空壳后误报。
CONTROL_MODULE = "agent.interactive.understanding"

_PYZ_MAGIC = b"PYZ\0"
_COOKIE_MAGIC = b"MEI\014\013\012\013\016"
#: 模块清单至少有这么多条才算解析成功（真产物是数千条；解析歪了会明显偏小）。
_MIN_EXPECTED_MODULES = 100

#: onefile 产物首次启动要解包，给足时间。
STARTUP_TIMEOUT_SECONDS = 120.0
#: 从提交到「上下文落库」的等待上限（材料解析在模型调用之前，正常是秒级）。
UNDERSTANDING_TIMEOUT_SECONDS = 90.0
HTTP_TIMEOUT_SECONDS = 30.0

#: 后端只监听回环；Host 头必须是回环（防 DNS rebinding），所以用 127.0.0.1。
HOST = "127.0.0.1"
SESSION_HEADER = "x-qio-session"

USAGE = (
    "用法：python scripts/check_frozen_materials.py [--exe <qio-backend.exe>]"
    " [--static-only] [--keep]"
)


def _configure_output() -> None:
    """报告层必须能在任何控制台编码下工作（中文 + cp1252 的 CI runner 也不能崩）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            is_tty = bool(getattr(stream, "isatty", lambda: False)())
        except (OSError, ValueError):
            is_tty = False
        try:
            if is_tty:
                reconfigure(errors="backslashreplace")
            else:
                reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            continue


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _candidate_exes() -> list[Path]:
    """默认产物查找顺序：安装位置 → 仓库里的 sidecar 副本 → 构建输出目录。"""
    candidates = [
        Path(r"D:\QIO\qio-backend.exe"),
        _repo_root() / "frontend" / "src-tauri" / "binaries" / "qio-backend-x86_64-pc-windows-msvc.exe",
        _repo_root() / "backend" / "dist-sidecar" / "qio-backend.exe",
        _repo_root() / "backend" / "dist-sidecar" / "qio-backend",
    ]
    env = os.environ.get("QIO_FROZEN_EXE")
    if env:
        candidates.insert(0, Path(env))
    return candidates


def _resolve_exe(raw: str | None) -> Path | None:
    if raw:
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_file() else None
    for candidate in _candidate_exes():
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# 检查 1：冻结产物里有没有这个模块
# ---------------------------------------------------------------------------


def _pyz_module_names(data: bytes) -> set[str] | None:
    """读 PyInstaller onefile 里的 PYZ 模块清单（marshal 的 (name, (typ, pos, len)) 列表）。

    格式来自 PyInstaller 自身：CArchive 以 cookie（含 MAGIC）收尾，PYZ 流以 b"PYZ\\0" +
    4 字节 python magic + 4 字节大端 TOC 偏移开头，TOC 是 marshal 序列化的条目列表。
    读不懂就返回 None —— 由调用方退回弱检查，绝不猜。
    """
    start = data.find(_PYZ_MAGIC)
    if start < 0 or data.find(_COOKIE_MAGIC) < 0:
        return None
    if start + 12 > len(data):
        return None
    toc_offset = int.from_bytes(data[start + 8:start + 12], "big")
    if not 12 < toc_offset < len(data) - start:
        return None
    try:
        toc = marshal.loads(data[start + toc_offset:])
    except (ValueError, EOFError, TypeError):
        return None
    if isinstance(toc, dict):
        names = {str(key) for key in toc}
    elif isinstance(toc, (list, tuple)):
        names = set()
        for entry in toc:
            if isinstance(entry, (list, tuple)) and entry:
                names.add(str(entry[0]))
            elif isinstance(entry, str):
                names.add(entry)
    else:
        return None
    # 解析成功的自证：条数像样，且对照模块在里面（它在坏包里也存在）。
    if len(names) < _MIN_EXPECTED_MODULES or CONTROL_MODULE not in names:
        return None
    return names


def check_static(exe: Path) -> tuple[bool, str, list[str]]:
    """断言产物里有 agent.interactive.materials。返回 (ok, 摘要, 详细行)。"""
    data = exe.read_bytes()
    details = [f"产物：{exe}（{len(data)} 字节）"]
    names = _pyz_module_names(data)
    if names is not None:
        details.append(
            f"方法：解析 PyInstaller PYZ 模块清单（{len(names)} 条，对照模块 "
            f"{CONTROL_MODULE} 已找到 → 清单可信）"
        )
        present = MODULE_NAME in names
        if present:
            return True, f"{MODULE_NAME} 在冻结产物的模块清单里", details
        details.append(
            "原因：" + MODULE_NAME + " 不在模块清单里 —— 打包时被漏掉了。"
            "检查 backend/src/agent/interactive/understanding.py 里那条字面 import，"
            "以及 scripts/build_sidecar.ps1 / backend/qio-backend.spec 的 --hidden-import "
            "（三处任意一处缺失都会重现这个漏包）。"
        )
        return False, f"{MODULE_NAME} **不在**冻结产物的模块清单里（模块清单共 {len(names)} 条）", details

    details.append(
        "方法：PYZ 清单解析不可用，退回「原始字节里是否出现模块名」的弱检查"
        "（只对「模块名完全没出现」的漏包可靠）"
    )
    present = MODULE_NAME.encode("utf-8") in data
    if present:
        return True, f"原始字节里出现了 {MODULE_NAME}（弱证据）", details
    details.append(
        "原因：产物里连这个模块名的字节都找不到 —— 打包时被漏掉了。"
        "检查 understanding.py 的字面 import 与构建脚本的 --hidden-import。"
    )
    return False, f"产物里找不到 {MODULE_NAME}（弱检查也失败）", details


# ---------------------------------------------------------------------------
# 检查 2：真链路（临时 QIO_DATA_DIR + 真后端实例）
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


class _Client:
    """最小 HTTP 客户端：带会话令牌，只打回环。"""

    def __init__(self, port: int, token: str) -> None:
        self._base = f"http://{HOST}:{port}"
        self._token = token

    def __call__(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self._base + path, data=payload, method=method)
        request.add_header("Content-Type", "application/json")
        if self._token:
            request.add_header(SESSION_HEADER, self._token)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return response.status, _decode_body(response.read())
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也要把 body 读出来给报告
            return int(exc.code), _decode_body(exc.read())


def _decode_body(raw: bytes) -> object:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _start_backend(exe: Path, data_dir: Path, port: int, token: str, log_path: Path):
    env = dict(os.environ)
    env["QIO_DATA_DIR"] = str(data_dir)
    env["QIO_HOST"] = HOST
    env["QIO_PORT"] = str(port)
    # 真链路走真实令牌认证（不用开发豁免），令牌只是本进程生成的临时值，不落盘、不打印。
    env["QIO_SESSION_TOKEN"] = token
    env["QIO_LOG"] = "WARNING"
    for name in (
        "QIO_DB",
        "QIO_ARCHIVE",
        "QIO_SESSION_TOKEN_FILE",
        "QIO_DEV_INSECURE",
        "QIO_ALLOWED_ORIGINS",
        "QIO_ENABLE_TEST_EVENTS",
    ):
        env.pop(name, None)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    log = open(log_path, "wb")  # noqa: SIM115 - 句柄要活到进程结束
    try:
        process = subprocess.Popen(
            [str(exe)],
            env=env,
            cwd=str(data_dir),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    except OSError:
        log.close()
        raise
    return process, log


def _terminate_tree(process: subprocess.Popen) -> None:
    """杀掉实例**整棵树**（onefile 的引导进程会再起一个真身，只杀父进程会留下孤儿）。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _log_tail(log_path: Path, limit: int = 15) -> str:
    try:
        text = log_path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return "（读不到实例日志）"
    lines = [line for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-limit:]) if lines else "（实例没有输出）"


def _wait_ready(client: _Client, process: subprocess.Popen, log_path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "后端实例在就绪前退出（退出码 " + str(process.returncode) + "）："
                + _log_tail(log_path)
            )
        try:
            status, body = client("GET", "/api/health")
        except (urllib.error.URLError, OSError) as exc:
            last = type(exc).__name__ + ": " + str(exc)
        else:
            if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
                return
            last = "HTTP " + str(status) + " " + json.dumps(body, ensure_ascii=False)[:200]
        time.sleep(0.5)
    raise RuntimeError("等待后端就绪超时（" + str(int(timeout)) + " 秒）：" + (last or "没有响应"))


def _board_card() -> dict:
    """一张 code 卡片：属于材料类（file/image/code/url），默认就在可见范围内。"""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "id": "c_frozen_materials_smoke",
        "kind": "code",
        "content": "print('frozen materials smoke')\n",
        "meta": {"language": "python"},
        "x": 0.0,
        "y": 0.0,
        "w": 240.0,
        "h": 120.0,
        "checked": False,
        "hidden": False,
        "folded": False,
        "bookmarked": False,
        "deleted": False,
        "createdAt": stamp,
        "updatedAt": stamp,
    }


def _submit(client: _Client, board_id: str) -> dict:
    status, body = client("POST", "/api/interactive/boards", {"boardId": board_id, "title": "冻结产物材料自证"})
    if status != 200:
        raise RuntimeError("建板面失败：HTTP " + str(status) + " " + json.dumps(body, ensure_ascii=False)[:300])
    status, state_body = client("GET", f"/api/interactive/boards/{board_id}/state")
    if status != 200:
        raise RuntimeError("读板面失败：HTTP " + str(status) + " " + json.dumps(state_body, ensure_ascii=False)[:300])
    seq = int((state_body or {}).get("seq") or 0) if isinstance(state_body, dict) else 0
    state = {
        "boardId": board_id,
        "seq": seq,
        "cards": [_board_card()],
        "groups": [],
        "links": [],
        "selection": [],
    }
    status, saved = client("PUT", f"/api/interactive/boards/{board_id}/state", {"state": state, "reason": "frozen-smoke"})
    if status != 200:
        raise RuntimeError("保存板面失败：HTTP " + str(status) + " " + json.dumps(saved, ensure_ascii=False)[:300])
    # 保存之后板面版本会推进：提交必须带上**新的**版本，否则会被服务端的版本门拒成 stale_state。
    saved_seq = int((saved or {}).get("seq")) if isinstance(saved, dict) and saved.get("seq") is not None else None
    if saved_seq is None:
        status, state_body = client("GET", f"/api/interactive/boards/{board_id}/state")
        if status != 200:
            raise RuntimeError("保存后重读板面失败：HTTP " + str(status))
        saved_seq = int((state_body or {}).get("seq") or 0)
    status, submitted = client(
        "POST",
        f"/api/interactive/boards/{board_id}/submissions",
        {"note": "冻结产物自证：材料解析必须真的可用", "baseStateVersion": saved_seq},
    )
    if status != 200:
        raise RuntimeError("提交失败：HTTP " + str(status) + " " + json.dumps(submitted, ensure_ascii=False)[:300])
    if not isinstance(submitted, dict):
        raise RuntimeError("提交返回的不是 JSON 对象：" + str(submitted)[:200])
    delivery = submitted.get("delivery") if isinstance(submitted.get("delivery"), dict) else {}
    if not delivery.get("delivered"):
        raise RuntimeError(
            "提交没有被受理："
            + str(delivery.get("reason") or submitted.get("error") or "没有可读原因")
        )
    return submitted


def _read_context(data_dir: Path, board_id: str) -> dict | None:
    """读实例自己写下的理解上下文（只读连接；WAL 下与运行中的实例并发读是安全的）。"""
    db_path = data_dir / "app.db"
    if not db_path.is_file():
        return None
    uri = db_path.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        row = conn.execute(
            "SELECT id, status, error, context, materials FROM board_understanding"
            " WHERE board_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (board_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    def _loads(raw, fallback):
        try:
            value = json.loads(raw) if raw else fallback
        except (TypeError, ValueError):
            return fallback
        return value
    context = _loads(row[3], {})
    if not isinstance(context, dict) or not context:
        return None
    return {
        "id": row[0],
        "status": row[1],
        "error": row[2],
        "context": context,
        "materials": _loads(row[4], []),
    }


def _wait_context(data_dir: Path, board_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = "还没有理解记录"
    while time.monotonic() < deadline:
        record = _read_context(data_dir, board_id)
        if record is not None:
            return record
        last = "理解已受理但上下文还没落库"
        time.sleep(0.5)
    raise RuntimeError("等待理解上下文落库超时（" + str(int(timeout)) + " 秒）：" + last)


def check_live(exe: Path, timeout: float, keep: bool) -> tuple[bool, str, list[str]]:
    details: list[str] = []
    data_dir = Path(tempfile.mkdtemp(prefix="frozen-materials-smoke-"))
    log_path = data_dir / "instance.log"
    port = _free_port()
    token = "smoke-" + os.urandom(16).hex()
    board_id = "frozen-materials-smoke-" + str(int(time.time()))
    process = None
    log = None
    details.append(f"实例：{exe} -- 数据目录 {data_dir}（临时，QIO_DATA_DIR）-- 端口 {port}（仅回环）")
    try:
        process, log = _start_backend(exe, data_dir, port, token, log_path)
        client = _Client(port, token)
        _wait_ready(client, process, log_path, timeout)
        details.append(f"实例就绪（PID {process.pid}，/api/health = ok，令牌认证已开启）")
        submitted = _submit(client, board_id)
        submission_id = (submitted.get("submission") or {}).get("id")
        details.append(f"提交已受理：submissionId={submission_id}")
        record = _wait_context(data_dir, board_id, UNDERSTANDING_TIMEOUT_SECONDS)
        context = record["context"]
        honesty = context.get("honesty") if isinstance(context.get("honesty"), dict) else {}
        materials = [m for m in (context.get("materials") or []) if isinstance(m, dict)]
        details.append(
            "理解记录：id=" + str(record["id"]) + " status=" + str(record["status"])
            + (f" error={record['error']}" if record.get("error") else "")
        )
        details.append(
            f"上下文：materialsResolved={honesty.get('materialsResolved')!r} "
            f"materials={len(materials)} 项 materialsError={honesty.get('materialsError')!r}"
        )
        for material in materials[:5]:
            details.append(
                "  材料 "
                + str(material.get("cardId"))
                + " source=" + str(material.get("source"))
                + " chars=" + str(material.get("chars"))
                + " 正文前 40 字=" + repr(str(material.get("text") or "")[:40])
            )
        for note in (honesty.get("notes") or [])[:5]:
            details.append("  honesty.note：" + str(note))

        if not honesty.get("materialsResolved"):
            details.append(
                "原因：上下文里的 honesty.materialsResolved=false，materialsError="
                + repr(honesty.get("materialsError"))
                + " —— 冻结产物没带上材料解析模块（这就是用户看到的那句话）。"
            )
            return False, "真链路：材料解析不可用（honesty.materialsResolved=false）", details
        if not materials:
            details.append("原因：材料解析「可用」但一项材料都没解析出来（materials 为空）。")
            return False, "真链路：materials 为空", details
        read_back = [m for m in materials if str(m.get("source")) == "inline" and str(m.get("text") or "").strip()]
        if not read_back:
            details.append(
                "原因：材料解析出了条目，但没有一项是「真的读到正文」的 inline 材料："
                + repr([m.get("source") for m in materials])
            )
            return False, "真链路：material 正文没有被读到", details
        return True, f"真链路：materialsResolved=true 且材料 {len(materials)} 项（正文已读到）", details
    except (RuntimeError, OSError, urllib.error.URLError, subprocess.SubprocessError) as exc:
        details.append("原因：" + str(exc))
        details.append("实例日志尾部：" + _log_tail(log_path))
        return False, "真链路：实例没有跑通（" + str(exc)[:160] + "）", details
    finally:
        if process is not None:
            _terminate_tree(process)
        if log is not None:
            log.close()
        if keep:
            print(f"[KEEP] 临时数据目录保留在 {data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True, usage=USAGE)
    parser.add_argument("--exe", default=None, help="qio-backend.exe 路径（默认自动查找）")
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="只做静态检查（起不了实例时用；真链路默认是必须过的）",
    )
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录（排障）")
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=STARTUP_TIMEOUT_SECONDS,
        help="等待实例就绪的秒数（默认 " + str(int(STARTUP_TIMEOUT_SECONDS)) + "）",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    _configure_output()
    args = _parse_args(argv)
    exe = _resolve_exe(args.exe)
    if exe is None:
        print("[NOT RUN] 找不到冻结产物：" + (args.exe or "；".join(str(p) for p in _candidate_exes())))
        print("         用 --exe <qio-backend.exe> 指定，或先跑 scripts/build_sidecar.ps1。")
        return 1

    failures = 0
    for name, runner in (
        ("静态：产物里有 " + MODULE_NAME, lambda: check_static(exe)),
        ("真链路：临时数据目录跑一次提交", None),
    ):
        if runner is None:
            if args.static_only:
                print("[SKIP] " + name + " — 指定了 --static-only")
                continue
            ok, detail, lines = check_live(exe, args.startup_timeout, args.keep)
        else:
            ok, detail, lines = runner()
        print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}")
        for line in lines:
            print("       " + line)
        failures += 0 if ok else 1

    print(f"[DONE] 冻结产物材料自证：{2 - failures if not args.static_only else 1 - failures}/"
          f"{2 if not args.static_only else 1} 通过（{exe}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
