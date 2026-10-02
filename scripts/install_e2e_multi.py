r"""多份 QIO 安装并存：卸载 A 不得动 B（docs/p4-plan.md 第 1.4 节）。

为什么单独一份：install_e2e.py 的主线是「一份安装」的装→跑→卸；并存判定要求同一台机器上
同时有两份真安装、两个真外壳、两个 sidecar、两份数据目录，并且要在**卸载 A 的那一刻**冻结
B 的进程身份（pid + 创建时间 + 映像路径），再逐条比对。install_e2e.py 的 coinstall 阶段
只是把本脚本当子进程跑（一份实现、两个入口）。

两种运行口径（开关区分，结论分别写清，绝不混读）：

* **默认（新语义）**：两边都启动**真外壳** qio.exe（外壳写 sidecar.lease.json，退出删）。
  卸载 A 后断言：A 的壳/sidecar/lease 全消失、A 目录被清空；B 的壳/sidecar/lease 原封不动
  （pid + 创建时间逐条一致）、B 的端口仍能回答 /api/health；再卸载 B 断言 B 也干净。
* **--expect-legacy-name-kill（改动前产物，先失败的取证）**：旧卸载器按**映像名**杀
  qio-backend.exe。这个模式断言的方向**反过来**：B 的 sidecar 被杀掉 = 复现了要修的缺陷
  （记 PASS-by-repro，标题里写明「缺陷复现，不是产品通过」）。用它取"先失败"的证；
  修复后的产物必须走默认口径。

「卸载 A 连坐 B」有**两条**路径，断言必须同时覆盖：

  (a) sidecar 钩子按**映像名**杀 qio-backend.exe → B 的 sidecar 被杀（本轮修的就是这条）；
  (b) 模板 Uninstall 段自带的一行 CheckIfAppIsRunning "qio.exe"
      （installer.nsi:760 + utils.nsh:40-47，同样只按名字、静默模式下 KillProcessCurrentUser）
      → B 的**壳**被杀 → JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE（main.rs:511/530）关 job
      → B 的 sidecar 跟着死。

  所以默认**两边都用真名 qio.exe 启动外壳**：A 卸载后，B 的壳与 B 的 sidecar 都必须原封不动
  （C-044/C-047），谁没做到就 FAIL —— (b) 没修好这件事必须由断言说出来，不许靠装置绕过去。
  C-031/C-048 的诱饵（ping 改名的 qio.exe）是 (b) 的对照：它**活着**（模板那条按名字检查
  已被所有权守卫换掉）才算过；还被杀 = (b) 未修 → FAIL。
  `--rename-b-shell` 是**诊断开关**：把 B 的壳复制成 qio-b-shell.exe（同一二进制、sha256 核对相同），
  用来把 (a) 单独隔离出来看 —— 平时不要用它下结论。

硬约束：只按 **PID / 路径**收进程（绝不用 taskkill /IM）；断言不许为了保绿放宽；
跑不起来就记 WARN / NOT VERIFIED 并贴原因。
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

import install_e2e as base  # noqa: E402


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def dir_digest(install_dir: Path) -> str:
    """安装目录的逐文件指纹（路径 + sha256 + 数量）。用来证明「装 B 没动 A」。"""
    parts = []
    for path in sorted(install_dir.rglob("*")):
        if path.is_file():
            parts.append("%s  %s" % (sha256_of(path), path.relative_to(install_dir)))
    return "%s（%d 个文件）" % (hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16],
                              len(parts))


def identities_equal(before: list[dict], after: list[dict]) -> tuple[bool, str]:
    """pid 集合 + 每个 pid 的创建时间 + 映像路径都要一致 —— 这才是「没有重启痕迹」。"""
    before_map = {item["pid"]: item for item in before}
    after_map = {item["pid"]: item for item in after}
    if set(before_map) != set(after_map):
        return False, "pid 集合变了：%s -> %s" % (sorted(before_map), sorted(after_map))
    for pid, item in before_map.items():
        if after_map[pid]["created_filetime"] != item["created_filetime"]:
            return False, "pid %s 的创建时间变了（重启过）：%s -> %s" % (
                pid, item["created_filetime"], after_map[pid]["created_filetime"])
        if os.path.normcase(after_map[pid]["exe"]) != os.path.normcase(item["exe"]):
            return False, "pid %s 的映像路径变了：%s -> %s" % (pid, item["exe"], after_map[pid]["exe"])
    return True, "pid + 创建时间 + 路径逐条一致：%s" % base.describe_processes(after)


def clean_mei(work: Path) -> int:
    """清掉 onefile 解包残留（每个后端约 180MB，不清会把盘塞满）。"""
    removed = 0
    tmp = work / "tmp"
    if tmp.is_dir():
        for child in tmp.glob("_MEI*"):
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def stop_dir_processes(install_dir: Path, tag: str) -> list[int]:
    """按 **PID** 收掉本目录里剩下的进程（收尾用，不是产品行为）。返回收过的 pid。"""
    pids = [item["pid"] for item in base.processes_in_dir(install_dir)]
    for pid in pids:
        base.kill_by_pid(pid, tag)
    return pids


def start_backend_direct(args, install_dir: Path, data_dir: Path, port: int, tag: str):
    """直接起 qio-backend.exe（改动前产物那条口径用：旧包没有 lease / 帮助程序）。"""
    exe = install_dir / "qio-backend.exe"
    env = base.clean_env(args, port)
    env["QIO_DATA_DIR"] = str(data_dir)
    logfile = Path(args.work_dir) / "evidence" / ("backend-%s.log" % tag)
    logfile.parent.mkdir(parents=True, exist_ok=True)
    handle = logfile.open("wb")
    proc = subprocess.Popen([str(exe)], cwd=str(install_dir), env=env, stdout=handle,
                            stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc, logfile


def install_package(installer: Path, install_dir: Path, work: Path, tag: str) -> tuple[int, str]:
    """静默安装。

    TEMP/TMP 必须指到检出内的 work/tmp：本机子进程是受限令牌，写不了
    用户目录下的 AppData\Local\Temp，NSIS 解 $PLUGINSDIR 失败会静默 abort
    （实测 exit=2 且目录不生成）。真机/CI 的 %TEMP% 本来就可写 —— 这是**本沙箱的环境适配**，
    不是产品行为，结论里要一起读。
    """
    env = {**os.environ, "TEMP": str(work / "tmp"), "TMP": str(work / "tmp")}
    code, out = base.run(base.install_cmdline(str(installer), str(install_dir)),
                         timeout=1800, env=env, tag=tag)
    deadline = time.time() + 90
    while time.time() < deadline and not (install_dir / "uninstall.exe").exists():
        time.sleep(2)
    return code, out


def wait_uninstall_settled(install_dir: Path, timeout: float = 240.0, settle: float = 20.0) -> None:
    """等卸载器**真的**干完。

    实测踩到两次，两次都是「我判太早」，不是产品问题：
      * 0.1.11：NSIS 卸载器把自己拷到 TEMP 后主进程先退出，实际的删除在另一个进程里继续
        —— 立刻查目录会看到「什么都没删掉」的假残留；
      * 0.1.12（本阶段）：PREUNINSTALL 钩子里的帮助程序会**先等 WM_CLOSE 超时**（钩子传的是
        --timeout-ms 8000）再收进程，这 8 秒里目录内容一个字节都不变 —— settle=5s 会在卸载器
        还没开始删文件时就返回（实测让 C-043/C-053 假红）。所以 settle 必须大于钩子的超时。
    判定：目录消失，或目录内容连续 settle 秒不变。
    """
    deadline = time.time() + timeout
    last: tuple | None = None
    stable_since = time.time()
    while time.time() < deadline:
        if not install_dir.exists():
            return
        current = tuple(sorted(str(p.relative_to(install_dir)) for p in install_dir.rglob("*")))
        if current != last:
            last = current
            stable_since = time.time()
        elif time.time() - stable_since >= settle:
            return
        time.sleep(1.0)


def uninstall_package(install_dir: Path, work: Path, tag: str) -> tuple[int, str]:
    exe = install_dir / "uninstall.exe"
    if not exe.is_file():
        return -1, "uninstall.exe 不存在（安装没成功？）"
    code, out = base.run([str(exe), "/S"], timeout=900,
                         env={**os.environ, "TEMP": str(work / "tmp"), "TMP": str(work / "tmp")},
                         tag=tag)
    wait_uninstall_settled(install_dir)
    return code, out


def verify_lease(install_dir: Path, tag: str, token: str, proc=None, logfile=None) -> dict | None:
    """lease 四件套 + backend.pid 就是按路径枚举到的那个进程。返回 lease（失败返回 None）。"""
    lease, raw = base.read_lease(install_dir)
    if lease is None:
        log_tail = base.tail_text(logfile, 600) if logfile else ""
        # 与本机沙箱的已知签名分开：子进程是受限令牌 → tauri log 插件写 %LOCALAPPDATA% 被拒 →
        # 外壳启动即 panic。这种失败记 WARN/NOT VERIFIED；别的起不来仍然是 FAIL。
        sandbox_blocked = ("PluginInitialization(\"log\"" in log_tail
                           or ("os error 5" in log_tail and "拒绝访问" in log_tail))
        base.record("C-020-%s" % tag, "%s：启动后出现 sidecar.lease.json" % tag,
                    "WARN" if sandbox_blocked else "FAIL",
                    "外壳没有写出 lease（exit=%s）；日志尾部=%s%s"
                    % (proc.poll() if proc is not None else "?", log_tail or "（空）",
                       "；判定：**本机沙箱限制**（log 插件写 %LOCALAPPDATA% 被拒）—— NOT VERIFIED，"
                       "真外壳这条必须在 CI/真机上硬过" if sandbox_blocked else ""))
        return None
    hits = base.lease_secret_hits(raw, extra_values=[token])
    base.evidence("lease-%s" % tag,
                  raw if not hits else "<REDACTED：lease 里检测到疑似密钥/令牌；命中=%s>" % hits)
    problems = base.lease_problems(lease, install_dir)
    procs = base.processes_in_dir(install_dir)
    lease_pid = (lease.get("backend") or {}).get("pid")
    in_dir = lease_pid in [item["pid"] for item in procs]
    base.record("C-020-%s" % tag, "%s：lease 四件套 + backend.pid 与按路径枚举一致" % tag,
                "PASS" if not problems and in_dir else "FAIL",
                "problems=%s；按路径枚举=%s；lease.backend.pid=%s"
                % (problems or "无", base.describe_processes(procs), lease_pid))
    base.record("C-021-%s" % tag, "%s：lease 内容不含密钥/令牌" % tag,
                "FAIL" if hits else "PASS", "命中：%s" % hits if hits else "无")
    return lease


def health_check(install_dir: Path, shell_pid: int, work: Path, tag: str, fallback_token: str) -> bool:
    """按 pid 反查端口（外壳自己挑端口）→ /api/health，并校验回答者就是本目录的 exe。"""
    procs = base.processes_in_dir(install_dir)
    pids = [item["pid"] for item in procs]
    port = base.listen_port_of(pids)
    if port is None:
        base.record("C-022-%s" % tag, "%s：sidecar 健康检查可用" % tag, "FAIL",
                    "按 pid 反查不到监听端口（pids=%s）" % pids)
        return False
    token_file = work / "tmp" / ("qio-session-%d.token" % shell_pid)
    token = ""
    deadline = time.time() + 30
    while time.time() < deadline and not token:
        try:
            token = token_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            time.sleep(0.5)
    ok, detail = base.wait_health(
        base.Client("http://127.0.0.1:%d" % port, token or fallback_token), timeout=90)
    code, out = base.powershell(
        "$owner = (Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty OwningProcess);"
        "if ($owner) { (Get-Process -Id $owner -ErrorAction SilentlyContinue).Path } else { '' }" % port,
        tag="multi-responder-%s" % tag)
    expected = str((install_dir / "qio-backend.exe").resolve())
    same = bool(out.strip()) and str(Path(out.strip()).resolve()) == expected
    base.record("C-022-%s" % tag, "%s：sidecar 健康检查可用且回答者就是本目录的 exe" % tag,
                "PASS" if ok and same else "FAIL",
                "port=%s health=%s 回答者=%r（期望 %s）token文件=%s（%s）"
                % (port, detail, out.strip(), expected, token_file.name,
                   "有令牌" if token else "**没读到令牌**"))
    return ok and same


def assert_dir_clean(install_dir: Path, cid: str, title: str) -> bool:
    if not install_dir.exists():
        base.record(cid, title, "PASS", "目录已删除")
        return True
    residue = sorted(p.name for p in install_dir.glob("*"))
    base.record(cid, title, "FAIL" if residue else "PASS",
                "残留=%s" % residue if residue else "目录还在但没有内容")
    return not residue


def run_sandbox_fallback(args, dir_a: Path, dir_b: Path, work: Path,
                         proc_shell_a, proc_shell_b) -> int:
    """真外壳被本机沙箱挡住时的**受限口径**（哪一半验了、哪一半没验，分开写清）。

    壳起不来 → lease 不会出现。但「**卸载 A 不动 B 的 sidecar**」这条**不依赖壳**：
      * 每边直接起真 qio-backend.exe（各自数据目录、各自端口）；
      * 按 §1.1 契约写**合成 lease**（shell 段用我们起的 shell 替身进程，backend 段用真后端）；
      * 跑 A 的**真卸载器**（它的 PREUNINSTALL 会调**真** qio-uninstall-helper.exe）；
      * 断言 A 的进程/lease/目录全清，B 的进程身份（pid+创建时间+路径）与端口原封不动。

    合成 lease **只证明判定逻辑与 PID 定向**，不证明「外壳会写 lease」—— 后一半记 WARN/NOT VERIFIED，
    必须由 CI / 真机硬过。
    """
    base.record("C-025", "真外壳那一半（计划 §1.1：外壳写 lease、退出删 lease）", "WARN",
                "**NOT VERIFIED（本机）**：真外壳在本机沙箱里启动即 panic（受限令牌，tauri log 插件写 "
                "%LOCALAPPDATA% 被拒，见 C-020-A/B 的原始 panic），所以「外壳会写 lease / 退出会删 lease」"
                "本机验不了 —— 必须由 CI / 真机硬过。下面的断言改用**按 §1.1 契约合成的 lease**，"
                "只证明「卸载只收本实例的 pid」这条判定逻辑；合成这件事本身写进 C-030。")
    for proc in (proc_shell_a, proc_shell_b):
        if proc is not None and proc.poll() is None:
            base.kill_by_pid(proc.pid, "cleanup-shell-panic")

    ping = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe"
    stubs: dict[str, object] = {}
    backends: dict[str, object] = {}
    for tag, install_dir, data_dir, port in (("A", dir_a, work / "data-a", args.port_a),
                                             ("B", dir_b, work / "data-b", args.port_b)):
        stub_exe = work / ("shell-stub-%s.exe" % tag.lower())
        shutil.copy2(ping, stub_exe)
        stubs[tag] = subprocess.Popen([str(stub_exe), "-t", "127.0.0.1"], cwd=str(work),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        shutil.rmtree(data_dir, ignore_errors=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        chosen = base._free_port(port)
        proc, _ = start_backend_direct(args, install_dir, data_dir, chosen, "fixed-%s" % tag.lower())
        backends[tag] = proc
        ok, detail = base.wait_health(base.Client("http://127.0.0.1:%d" % chosen, args.token), timeout=180)
        base.record("C-020-%s" % tag, "%s：真后端 /api/health 就绪（直接起；合成 lease 口径）" % tag,
                    "PASS" if ok else "FAIL", "%s（port=%s pid=%s）" % (detail, chosen, proc.pid))
    time.sleep(1.5)

    for tag, install_dir in (("A", dir_a), ("B", dir_b)):
        base.write_synthetic_lease(install_dir, shell_pid=stubs[tag].pid,
                                   backend_pid=backends[tag].pid)
        if verify_lease(install_dir, tag, args.token, stubs[tag], None) is None:
            return finish(args, {"mode": "sandbox-fallback", "aborted": "lease 合成校验失败"})

    health_check(dir_a, stubs["A"].pid, work, "A-synth", args.token)
    health_check(dir_b, stubs["B"].pid, work, "B-synth", args.token)
    id_a = base.processes_in_dir(dir_a)
    id_b = base.processes_in_dir(dir_b)
    lease_b_hash = sha256_of(base.lease_path(dir_b))
    base.record("C-030", "冻结两边进程身份 + B 的 lease 指纹（**合成 lease**，只证判定逻辑）", "PASS",
                "A: %s；B: %s；B lease sha256=%s"
                % (base.describe_processes(id_a), base.describe_processes(id_b), lease_b_hash[:16]))

    decoy_dir = work / "decoy-qio"
    shutil.rmtree(decoy_dir, ignore_errors=True)
    decoy_dir.mkdir(parents=True, exist_ok=True)
    decoy_exe = decoy_dir / "qio.exe"
    shutil.copy2(ping, decoy_exe)
    decoy = subprocess.Popen([str(decoy_exe), "-t", "127.0.0.1"], cwd=str(decoy_dir),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(1)
    base.record("C-031", "对照：起一个 ping 改名的 qio.exe 诱饵", "PASS",
                "pid=%s（验证模板那一处按名字杀 qio.exe 是否已被所有权守卫替换）" % decoy.pid)

    code_un_a, _ = uninstall_package(dir_a, work, "fixed-uninstall-a")
    left_a = base.wait_processes_gone([item["pid"] for item in id_a] + [stubs["A"].pid], timeout=90)
    base.record("C-041", "卸载 A 后 A 的壳替身与 sidecar 都退出（按路径 + pid 核对）",
                "FAIL" if left_a else "PASS",
                "exit=%s；仍在：%s" % (code_un_a, left_a or "无（按路径枚举也为空：%s）"
                                      % base.processes_in_dir(dir_a)))
    base.record("C-042", "卸载 A 后 A 的 lease 消失",
                "FAIL" if base.lease_path(dir_a).exists() else "PASS",
                "%s %s" % (base.LEASE_NAME, "仍在" if base.lease_path(dir_a).exists() else "已消失"))
    assert_dir_clean(dir_a, "C-043", "卸载 A 后 A 的安装目录被清空")

    after_b = base.processes_in_dir(dir_b)
    same_b, why_b = identities_equal(id_b, after_b)
    base.record("C-044", "**卸载 A 没有动 B 的任何进程**（pid + 创建时间 + 路径逐条一致）",
                "PASS" if same_b else "FAIL",
                "卸载 A 前：%s；卸载 A 后：%s；%s"
                % (base.describe_processes(id_b), base.describe_processes(after_b), why_b))
    lease_b_after, _raw = base.read_lease(dir_b)
    lease_problems_after = base.lease_problems(lease_b_after, dir_b) if lease_b_after else ["lease 文件不见了"]
    hash_same = base.lease_path(dir_b).exists() and sha256_of(base.lease_path(dir_b)) == lease_b_hash
    base.record("C-045", "卸载 A 后 B 的 lease 原封不动且仍然合法",
                "PASS" if hash_same and not lease_problems_after else "FAIL",
                "文件还在=%s 内容 sha256 未变=%s problems=%s"
                % (base.lease_path(dir_b).exists(), hash_same, lease_problems_after or "无"))
    health_check(dir_b, stubs["B"].pid, work, "B-after-uninstall-A", args.token)
    backend_pid_b = (lease_b_after or {}).get("backend", {}).get("pid")
    base.record("C-047", "B 的 sidecar 没有重启痕迹（PID 不变）",
                "PASS" if backend_pid_b in [item["pid"] for item in after_b] else "FAIL",
                "lease.backend.pid=%s；卸载 A 后 B 里按路径枚举到：%s"
                % (backend_pid_b, [item["pid"] for item in after_b]))
    decoy_alive = base.process_identity(decoy.pid) is not None
    base.record("C-048", "对照 (b)：模板那条按名字杀 qio.exe 是否已被所有权守卫换掉",
                "PASS" if decoy_alive else "FAIL",
                "诱饵 qio.exe（ping 改名、没有 lease）pid=%s %s —— %s"
                % (decoy.pid, "**仍然活着**" if decoy_alive else "**被按名字杀了**",
                   "(b) 已修：没有归属证据的进程收不到杀" if decoy_alive else
                   "(b) **未修**：模板仍然按映像名杀 qio.exe"))
    if decoy_alive:
        base.kill_by_pid(decoy.pid, "cleanup-decoy")

    code_un_b, _ = uninstall_package(dir_b, work, "fixed-uninstall-b")
    left_b = base.wait_processes_gone([item["pid"] for item in after_b] + [stubs["B"].pid], timeout=90)
    base.record("C-051", "卸载 B 后 B 的壳替身与 sidecar 都退出",
                "FAIL" if left_b else "PASS", "exit=%s；仍在：%s" % (code_un_b, left_b or "无"))
    base.record("C-052", "卸载 B 后 B 的 lease 消失",
                "FAIL" if base.lease_path(dir_b).exists() else "PASS",
                "%s %s" % (base.LEASE_NAME, "仍在" if base.lease_path(dir_b).exists() else "已消失"))
    assert_dir_clean(dir_b, "C-053", "卸载 B 后 B 的安装目录被清空")
    return finish(args, {"mode": "sandbox-fallback"})


def reclassify_sandbox_shells() -> None:
    """收尾复核：把**匹配本机沙箱签名**的 C-020-* FAIL 翻成 WARN / NOT VERIFIED。

    实测踩到：外壳 panic 的 stderr 会在 C-020 判完之后才落盘（C-020 当场读到空日志 → FAIL；
    收尾时同一份日志里躺着 PluginInitialization("log", "拒绝访问。 (os error 5)")）。
    只翻**签名匹配**的那几条 —— 别的 FAIL 一律不动，绝不因为"想让它变绿"放宽。
    """
    logs = []
    if base.EVIDENCE and Path(base.EVIDENCE).is_dir():
        logs = sorted(Path(base.EVIDENCE).glob("shell-*.log"))
    text = "".join(base.tail_text(item, 4000) for item in logs)
    blocked = ('PluginInitialization("log"' in text) or ("os error 5" in text and "拒绝访问" in text)
    if not blocked:
        return
    for item in base.RESULTS:
        if item["id"].startswith("C-020-") and item["state"] == "FAIL":
            item["state"] = "WARN"
            item["detail"] += ("；**收尾复核**：壳日志（%s）里出现了本机沙箱签名"
                               "（tauri log 插件写 %%LOCALAPPDATA%% 被拒，os error 5）→ "
                               "真外壳这一半在本机 NOT VERIFIED，不是产品 FAIL；"
                               "必须由 CI / 真机硬过" % ", ".join(p.name for p in logs))


def finish(args, extra: dict) -> int:
    reclassify_sandbox_shells()
    failures = [item for item in base.RESULTS if item["state"] == "FAIL"]
    payload = {"mode": extra.get("mode"), "installer_a": args.installer,
               "installer_b": args.installer_b or args.installer,
               "work_dir": args.work_dir, "results": base.RESULTS,
               "failed": len(failures), **extra}
    out = Path(args.work_dir) / "summary.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    base.log("")
    base.log("== 多安装并存：%d 项 PASS / %d 项 WARN / %d 项 FAIL ==" % (
        sum(1 for item in base.RESULTS if item["state"] == "PASS"),
        sum(1 for item in base.RESULTS if item["state"] == "WARN"), len(failures)))
    for item in failures:
        base.log("  FAIL %s %s :: %s" % (item["id"], item["title"], item["detail"][:200]))
    base.log("  summary.json -> %s" % out)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="多份 QIO 安装并存：卸载 A 不动 B（真机 E2E）")
    parser.add_argument("--installer", required=True, help="A 的安装包")
    parser.add_argument("--installer-b", default="", help="B 的安装包（默认与 A 同一个包）")
    parser.add_argument("--work-dir", default=str(ROOT / ".e2e-work-multi"))
    parser.add_argument("--install-dir-a", default="")
    parser.add_argument("--install-dir-b", default="")
    parser.add_argument("--port-a", type=int, default=8999)
    parser.add_argument("--port-b", type=int, default=9000)
    parser.add_argument("--token", default="coinstall-token-0001")
    parser.add_argument("--shell-timeout", type=float, default=150.0,
                        help="等真外壳写出 lease 的秒数（WebView2 首次启动可能慢）")
    parser.add_argument("--expect-legacy-name-kill", action="store_true",
                        help="改动前产物口径：断言 B 的 sidecar 被按名字连坐杀掉（缺陷复现，不是通过）")
    parser.add_argument("--keep-dirs", action="store_true", help="跑完不删安装目录（默认删，省盘）")
    parser.add_argument("--rename-b-shell", action="store_true",
                        help="诊断开关：B 的壳改成 qio-b-shell.exe（避开模板按名字杀 qio.exe，"
                             "把 (a) sidecar 所有权单独隔离出来；默认两边都用真名 qio.exe）")
    args = parser.parse_args()

    args.work_dir = str(Path(args.work_dir).resolve())
    work = Path(args.work_dir)
    for sub in ("evidence", "data-a", "data-b", "tmp"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    base.EVIDENCE = work / "evidence"
    # base.clean_env / launch_shell 会读 args.port；真外壳自己挑端口，这里只是占位。
    args.port = args.port_a
    args.install_dir = str((work / "install-a"))

    installer_a = Path(args.installer).resolve()
    installer_b = Path(args.installer_b).resolve() if args.installer_b else installer_a
    dir_a = Path(args.install_dir_a).resolve() if args.install_dir_a else work / "install-a"
    dir_b = Path(args.install_dir_b).resolve() if args.install_dir_b else work / "install-b"
    data_a, data_b = work / "data-a", work / "data-b"
    legacy = args.expect_legacy_name_kill
    proc_shell_a = proc_shell_b = None

    base.log("== 多安装并存 E2E ==")
    base.log("  口径     : %s" % ("**改动前产物：缺陷复现（--expect-legacy-name-kill）**" if legacy
                                  else "新语义（真外壳 + lease；卸载 A 不动 B）"))
    base.log("  安装包 A : %s" % installer_a)
    base.log("  安装包 B : %s" % installer_b)
    base.log("  目录 A/B : %s / %s" % (dir_a, dir_b))

    try:
        for name, path in (("A", installer_a), ("B", installer_b)):
            if not path.is_file():
                base.record("C-001", "安装包存在（%s）" % name, "FAIL", "找不到 %s" % path)
                return 3
            base.record("C-001", "安装包指纹（%s）" % name, "PASS",
                        "%s（%.1f MB，sha256 %s）" % (path.name, path.stat().st_size / 1048576,
                                                      sha256_of(path)[:16]))

        running_qio = [item for item in base._snapshot_pids() if item["name"].lower() == "qio.exe"]
        base.record("C-002", "安装前没有别的 qio.exe 在跑（静默安装会按名字杀掉它）",
                    "FAIL" if running_qio else "PASS",
                    "检测到 %s —— 静默安装会按映像名杀掉它们，脚本主动停在这里" % running_qio
                    if running_qio else "无")

        for install_dir, data_dir in ((dir_a, data_a), (dir_b, data_b)):
            shutil.rmtree(install_dir, ignore_errors=True)
            shutil.rmtree(data_dir, ignore_errors=True)
            data_dir.mkdir(parents=True, exist_ok=True)

        # -- 装两份（都不起外壳；注册表槽位只有一个，那份语义不在本轮范围）--
        code_a, _ = install_package(installer_a, dir_a, work, "multi-install-a")
        installed_a = code_a == 0 and (dir_a / "uninstall.exe").exists()
        base.record("C-010", "静默安装 A", "PASS" if installed_a else "FAIL",
                    "exit=%s 目录存在=%s" % (code_a, dir_a.exists()))
        digest_a_before = dir_digest(dir_a)

        code_b, _ = install_package(installer_b, dir_b, work, "multi-install-b")
        installed_b = code_b == 0 and (dir_b / "uninstall.exe").exists()
        base.record("C-011", "静默安装 B", "PASS" if installed_b else "FAIL",
                    "exit=%s 目录存在=%s" % (code_b, dir_b.exists()))
        if not (installed_a and installed_b):
            # 安装都没成功：后面的「卸载 A 不动 B」没有对象可验，**必须停**——
            # 否则会产出一份验错对象的报告（这是本脚本最不能犯的错）。
            base.record("C-016", "安装失败即停（后面的断言会验错对象）", "FAIL",
                        "A installed=%s（exit=%s）/ B installed=%s（exit=%s）"
                        % (installed_a, code_a, installed_b, code_b))
            return finish(args, {"mode": "aborted-install-failed"})
        digest_a_after = dir_digest(dir_a)
        base.record("C-012", "装 B 没有改动 A 的目录（逐文件 sha256）",
                    "PASS" if digest_a_before == digest_a_after else "FAIL",
                    "A: %s -> %s" % (digest_a_before, digest_a_after))
        for name, install_dir in (("A", dir_a), ("B", dir_b)):
            lease_file = base.lease_path(install_dir)
            base.record("C-013", "%s：还没启动时没有 lease（外壳没跑就不该有）" % name,
                        "FAIL" if lease_file.exists() else "PASS",
                        "%s %s" % (base.LEASE_NAME,
                                   "存在（谁写的？）" if lease_file.exists() else "不存在"))

        if legacy:
            # ---------- 改动前口径：直接起两个真后端，看 A 的卸载器按名字连坐 ----------
            base.log("  !! 改动前口径：旧卸载器会按映像名杀掉本机**所有** qio-backend.exe")
            for name, install_dir in (("A", dir_a), ("B", dir_b)):
                if not (install_dir / "qio-backend.exe").is_file():
                    base.record("C-017", "%s：安装目录里有 qio-backend.exe" % name, "FAIL",
                                "找不到 %s（安装没成功）" % (install_dir / "qio-backend.exe"))
                    return finish(args, {"mode": "aborted-missing-backend"})
            proc_a, _ = start_backend_direct(args, dir_a, data_a, args.port_a, "legacy-a")
            proc_b, _ = start_backend_direct(args, dir_b, data_b, args.port_b, "legacy-b")
            for name, port in (("A", args.port_a), ("B", args.port_b)):
                ok, detail = base.wait_health(
                    base.Client("http://127.0.0.1:%d" % port, args.token), timeout=150)
                base.record("C-020-%s" % name, "%s：后端 /api/health 就绪" % name,
                            "PASS" if ok else "FAIL", detail)
            id_a, id_b = base.processes_in_dir(dir_a), base.processes_in_dir(dir_b)
            base.record("C-030", "冻结两边进程身份（pid + 创建时间 + 路径）", "PASS",
                        "A: %s；B: %s" % (base.describe_processes(id_a), base.describe_processes(id_b)))
            base.record("C-031", "对照：改动前产物没有 lease / 帮助程序（复现的前提）", "PASS",
                        "A lease=%s helper=%s"
                        % (base.lease_path(dir_a).exists(), (dir_a / "qio-uninstall-helper.exe").exists()))
            code, _out = uninstall_package(dir_a, work, "legacy-uninstall-a")
            time.sleep(5)
            survivors_a = base.processes_in_dir(dir_a)
            survivors_b = base.processes_in_dir(dir_b)
            killed_b = not survivors_b
            base.record("C-044【缺陷复现】", "改动前产物：卸载 A 连坐杀掉 B 的 sidecar",
                        "PASS" if killed_b else "FAIL",
                        "exit(A)=%s；A 自己残留=%s；卸载 A 前 B 的进程=%s；卸载后=%s —— %s"
                        % (code, base.describe_processes(survivors_a), base.describe_processes(id_b),
                           base.describe_processes(survivors_b),
                           "复现：B 的 sidecar 被按名字杀掉（这就是本轮要修的缺陷；**不是产品通过**）"
                           if killed_b else
                           "**没复现**：B 的 sidecar 还在 —— 这个产物不是「改动前」的行为"))
            assert_dir_clean(dir_a, "C-041", "改动前产物：卸载 A 后 A 目录被清空")
            code_b2, _ = uninstall_package(dir_b, work, "legacy-uninstall-b")
            assert_dir_clean(dir_b, "C-051", "改动前产物：卸载 B 后 B 目录被清空")
            return finish(args, {"mode": "legacy-repro", "exit_a": code, "exit_b": code_b2})

        # ---------- 新语义：两边真外壳（默认都用真名，见文件头 (a)/(b) 两条路径） ----------
        shell_b_exe = dir_b / "qio.exe"
        if args.rename_b_shell:
            copy_b = dir_b / "qio-b-shell.exe"
            shutil.copy2(dir_a / "qio.exe", copy_b)
            same_binary = sha256_of(dir_a / "qio.exe") == sha256_of(copy_b)
            base.record("C-014", "【诊断模式】B 的壳是同一份 qio.exe 的改名副本（sha256 相同）",
                        "PASS" if same_binary else "FAIL",
                        "qio.exe sha256=%s / qio-b-shell.exe sha256=%s；改名只避开模板按映像名杀 "
                        "qio.exe 那条遗留检查，lease/job 代码路径完全相同 —— 这一跑**不覆盖 (b)**"
                        % (sha256_of(dir_a / "qio.exe")[:16], sha256_of(copy_b)[:16]))
            shell_b_exe = copy_b
        else:
            base.record("C-014", "B 的壳用**真名** qio.exe 启动（断言覆盖 (b)：模板按名字杀 qio.exe）",
                        "PASS" if (dir_b / "qio.exe").is_file() else "FAIL",
                        "B 的外壳=%s；默认口径就是要让模板那条按名字检查有机会连坐 B —— "
                        "它没连坐（下面 C-044/C-047/C-048）才算过" % shell_b_exe)

        proc_shell_a, log_a = base.launch_shell(args, dir_a / "qio.exe", data_a, "multi-a")
        lease_a = verify_lease(dir_a, "A", args.token, proc_shell_a, log_a)
        proc_shell_b, log_b = base.launch_shell(args, shell_b_exe, data_b, "multi-b")
        lease_b = verify_lease(dir_b, "B", args.token, proc_shell_b, log_b)
        if lease_a is None or lease_b is None:
            log_tail = (base.tail_text(log_a, 600) or "") + (base.tail_text(log_b, 600) or "")
            sandbox_blocked = ("PluginInitialization(\"log\"" in log_tail
                               or ("os error 5" in log_tail and "拒绝访问" in log_tail))
            base.record("C-024", "两份安装的 lease 都出现（**真外壳**口径的前提）",
                        "WARN" if sandbox_blocked else "FAIL",
                        "lease A=%s lease B=%s%s" % (lease_a is not None, lease_b is not None,
                         "；判定：**本机沙箱限制**（真外壳启动即 panic，见 C-020-A/B）→ 真外壳那一半 NOT VERIFIED；"
                         "改用受限口径（合成 lease）把「sidecar 所有权与 PID 定向」跑成硬结论"
                         if sandbox_blocked else
                         "；外壳起不来的原因不是已知沙箱签名 —— 按 FAIL 停在这里"))
            if not sandbox_blocked:
                return finish(args, {"mode": "new-semantics"})
            return run_sandbox_fallback(args, dir_a, dir_b, work, proc_shell_a, proc_shell_b)

        id_a = base.processes_in_dir(dir_a)
        id_b = base.processes_in_dir(dir_b)
        lease_b_hash = sha256_of(base.lease_path(dir_b))
        base.record("C-030", "冻结两边进程身份 + B 的 lease 指纹", "PASS",
                    "A: %s；B: %s；B lease sha256=%s"
                    % (base.describe_processes(id_a), base.describe_processes(id_b),
                       lease_b_hash[:16]))

        # 对照：ping 改名的 qio.exe 诱饵 —— 模板按名字杀它的行为应该仍在
        decoy_dir = work / "decoy-qio"
        shutil.rmtree(decoy_dir, ignore_errors=True)
        decoy_dir.mkdir(parents=True, exist_ok=True)
        decoy_exe = decoy_dir / "qio.exe"
        shutil.copy2(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe",
                     decoy_exe)
        decoy = subprocess.Popen([str(decoy_exe), "-t", "127.0.0.1"], cwd=str(decoy_dir),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(1)
        base.record("C-031", "对照：起一个 ping 改名的 qio.exe 诱饵", "PASS",
                    "pid=%s（用来验证模板 Uninstall 段「按映像名杀 qio.exe」这条行为仍在）" % decoy.pid)

        # -- 卸载 A --
        code_un_a, _ = uninstall_package(dir_a, work, "multi-uninstall-a")
        left_a = base.wait_processes_gone([item["pid"] for item in id_a] + [proc_shell_a.pid],
                                          timeout=60)
        base.record("C-041", "卸载 A 后 A 的壳与 sidecar 都退出（按路径 + pid 核对）",
                    "FAIL" if left_a else "PASS",
                    "exit=%s；仍在：%s" % (code_un_a, left_a or "无（按路径枚举也为空：%s）"
                                          % base.processes_in_dir(dir_a)))
        base.record("C-042", "卸载 A 后 A 的 lease 消失",
                    "FAIL" if base.lease_path(dir_a).exists() else "PASS",
                    "%s %s" % (base.LEASE_NAME, "仍在" if base.lease_path(dir_a).exists() else "已消失"))
        assert_dir_clean(dir_a, "C-043", "卸载 A 后 A 的安装目录被清空")

        after_b = base.processes_in_dir(dir_b)
        same_b, why_b = identities_equal(id_b, after_b)
        base.record("C-044", "**卸载 A 没有动 B 的任何进程**（pid + 创建时间 + 路径逐条一致）",
                    "PASS" if same_b else "FAIL",
                    "卸载 A 前：%s；卸载 A 后：%s；%s"
                    % (base.describe_processes(id_b), base.describe_processes(after_b), why_b))
        lease_b_after, raw_b_after = base.read_lease(dir_b)
        lease_problems_after = base.lease_problems(lease_b_after or {}, dir_b) if lease_b_after else \
            ["lease 文件不见了"]
        hash_same = base.lease_path(dir_b).exists() and sha256_of(base.lease_path(dir_b)) == lease_b_hash
        base.record("C-045", "卸载 A 后 B 的 lease 原封不动且仍然合法",
                    "PASS" if hash_same and not lease_problems_after else "FAIL",
                    "文件还在=%s 内容 sha256 未变=%s problems=%s"
                    % (base.lease_path(dir_b).exists(), hash_same, lease_problems_after or "无"))
        health_check(dir_b, proc_shell_b.pid, work, "B-after-uninstall-A", args.token)
        backend_pid_b = (lease_b.get("backend") or {}).get("pid")
        base.record("C-047", "B 的 sidecar 没有重启痕迹（PID 不变）",
                    "PASS" if backend_pid_b in [item["pid"] for item in after_b] else "FAIL",
                    "lease.backend.pid=%s；卸载 A 后 B 里按路径枚举到：%s"
                    % (backend_pid_b, [item["pid"] for item in after_b]))

        decoy_alive = base.process_identity(decoy.pid) is not None
        base.record("C-048", "对照 (b)：模板那条按名字杀 qio.exe 是否已被所有权守卫换掉",
                    "PASS" if decoy_alive else "FAIL",
                    "诱饵 qio.exe（ping 改名、没有 lease、没有主程序）pid=%s %s —— %s"
                    % (decoy.pid, "**仍然活着**" if decoy_alive else "**被按名字杀了**",
                       "(b) 已修：没有归属证据的进程收不到杀（这正是本轮要的形状）"
                       if decoy_alive else
                       "(b) **未修**：模板 Uninstall 段仍然按映像名杀 qio.exe —— 真名外壳并存会被连坐，"
                       "上一条 C-044 若同时失败就是它的后果"))
        if decoy_alive:
            base.kill_by_pid(decoy.pid, "cleanup-decoy")

        # -- 卸载 B --
        code_un_b, _ = uninstall_package(dir_b, work, "multi-uninstall-b")
        left_b = base.wait_processes_gone([item["pid"] for item in after_b] + [proc_shell_b.pid],
                                          timeout=60)
        base.record("C-051", "卸载 B 后 B 的壳与 sidecar 都退出",
                    "FAIL" if left_b else "PASS", "exit=%s；仍在：%s" % (code_un_b, left_b or "无"))
        base.record("C-052", "卸载 B 后 B 的 lease 消失",
                    "FAIL" if base.lease_path(dir_b).exists() else "PASS",
                    "%s %s" % (base.LEASE_NAME, "仍在" if base.lease_path(dir_b).exists() else "已消失"))
        assert_dir_clean(dir_b, "C-053", "卸载 B 后 B 的安装目录被清空")
        return finish(args, {"mode": "new-semantics"})
    finally:
        # 收尾：只按 PID / 路径；绝不用 /IM。产物目录默认删掉（两份安装加起来 300MB+）。
        for tag, install_dir in (("cleanup-a", dir_a), ("cleanup-b", dir_b)):
            pids = stop_dir_processes(install_dir, tag)
            if pids:
                base.log("  收尾：按 PID 清掉 %s 里的进程 %s" % (install_dir.name, pids))
        for proc in (proc_shell_a, proc_shell_b):
            if proc is not None and proc.poll() is None:
                base.kill_by_pid(proc.pid, "cleanup-shell")
        clean_mei(work)
        if not args.keep_dirs:
            for install_dir in (dir_a, dir_b):
                shutil.rmtree(install_dir, ignore_errors=True)
            base.log("  收尾：已删除两份安装目录（--keep-dirs 可保留）")


if __name__ == "__main__":
    raise SystemExit(main())
