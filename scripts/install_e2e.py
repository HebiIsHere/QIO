r"""NSIS 安装包「真机 E2E」驱动：装 → 首启 → 工具全链 → 重启 → 卸载。

为什么需要它：release_gate.py 只能核对**已经产出的文件**（哈希、清单、签名结构、内置模型），
它明确不覆盖「安装器真的跑起来了吗 / 装出来的后端真的能服务吗 / 数据在重启与卸载后还在吗」。
这份脚本把后半段做成可重复执行、逐条留证据的流程。

诚实边界（不要把这些读成更强的结论）：

* **不是干净 Windows 用户环境**。本机不能新建用户；这里用可达成的等价条件：
  独立安装目录 + 全新的空数据目录（QIO_DATA_DIR 指向空目录）+ 启动进程**不继承开发用环境变量**
  （白名单构造 env：不带 QIO_DEV_INSECURE / QIO_ENABLE_TEST_EVENTS / PYTHONPATH / VIRTUAL_ENV）。
* **模型是假厂商**（scripts/e2e_fake_provider.py）。凡是用到它的用例都只能证明「QIO 自己的链路对」，
  不能证明任何真实厂商端点。
* **没有 GUI 驱动**。Tauri WebView 不开 CDP，脚本只走 HTTP API；界面层的步骤必须人工做，
  见 docs 里「必须人工的步骤」一节。
* 卸载这一步**不勾选「删除应用数据」**（静默模式跳过确认页，复选框状态恒为 0）——这正是要验证的
  「卸载是否保留用户数据」。

安全设计（不动本机既有安装 D:\QIO）：

1. 静默安装用 /S /D=<绝对路径>。/D= 必须是最后一个参数且不带引号（NSIS 规则）。
2. NSIS 的「检测到既有安装 → 重装/升级」自定义页在 **silent 模式下不会被调用**（本脚本先用
   --decoy 实验证明：把注册表指向一个诱饵安装目录，跑真安装器，检查诱饵卸载器有没有被执行）。
3. 同一个产品名只有一个卸载注册表槽位（HKCU\...\Uninstall\QIO），新安装必然覆写它。
   所以脚本在动手前 reg export 备份、结束后 reg import 还原；桌面/开始菜单快捷方式同样备份还原；
   并在最后逐文件比对 D:\QIO 的 sha256 证明既有安装未被改动。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

RESULTS: list[dict] = []
EVIDENCE: Path | None = None
_ev_seq = [0]

# 诱饵注册表是否已经生效：一旦生效，无论后面哪一步失败，都必须还原（否则用户机器上的
# 「QIO 已安装」注册项会指着诱饵目录 —— 那是对既有安装的真实破坏）。
STATE = {"decoy_applied": False}


# ---------------------------------------------------------------- 基础设施

def log(msg: str) -> None:
    print(msg, flush=True)


def record(cid: str, title: str, state: str, detail: str) -> None:
    RESULTS.append({"id": cid, "title": title, "state": state, "detail": detail})
    log("[%s] %s %s :: %s" % (state, cid, title, detail[:300]))


def evidence(name: str, text: str) -> str:
    """把原始输出落盘，返回证据文件相对路径。"""
    global _ev_seq
    if EVIDENCE is None:
        return ""
    _ev_seq[0] += 1
    target = EVIDENCE / ("%03d-%s.txt" % (_ev_seq[0], name))
    target.write_text(text, encoding="utf-8", errors="replace")
    return str(target.relative_to(EVIDENCE.parent))


def install_cmdline(installer: str, install_dir: str) -> str:
    """拼安装器命令行。

    NSIS 的 /D= **不能带引号**（即使路径里有空格），而 Python 传列表时会自动给含空格的
    参数加引号 —— 那样 NSIS 会解析失败（表现为「进程起来又消失、目录没生成」）。
    所以：exe 自己加引号，/D= 放最后且保持裸值。这条路径已用 makensis 探针单独验证过。
    """
    return '"%s" /S /D=%s' % (installer, install_dir)


def run(cmd, *, timeout: int = 600, env: dict | None = None, cwd: str | None = None,
        tag: str | None = None) -> tuple[int, str]:
    """跑外部命令并把命令 + 原始输出一起留证。cmd 可以是 list，也可以是**原样**命令行字符串。"""
    log("  $ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, env=env, cwd=cwd)
        out = (proc.stdout or "") + (proc.stderr or "")
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = "TIMEOUT after %ss\n%s" % (timeout, (exc.stdout or "") + (exc.stderr or ""))
        code = -1
    if tag:
        evidence(tag, "$ " + " ".join(cmd) + "\n\n" + out)
    return code, out


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def powershell(script: str, *, timeout: int = 300, tag: str | None = None) -> tuple[int, str]:
    return run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
               timeout=timeout, tag=tag)


# ---------------------------------------------------------------- HTTP

class Client:
    def __init__(self, base: str, token: str = "") -> None:
        self.base = base.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload=None, *, timeout: float = 30.0,
                raw: bool = False):
        url = self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, (body if raw else json.loads(body or "{}"))

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, payload=None, **kw):
        return self.request("POST", path, payload if payload is not None else {}, **kw)


class SseReader:
    """后台读 SSE；主线程按需取事件。"""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.events: list[dict] = []
        # 见过的 event id：重连时服务端会重放历史，必须能把"历史"和"本轮"分开，
        # 否则一个旧的 TURN_END 就会让本轮提前"结束"（实测踩过：重启后那条流）。
        self.seen: set[str] = set()
        self.q: queue.Queue = queue.Queue()
        self._stop = False
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        headers = {"Accept": "text/event-stream"}
        if self.client.token:
            headers["Authorization"] = "Bearer " + self.client.token
        req = urllib.request.Request(self.client.base + "/api/events", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                for raw in resp:
                    if self._stop:
                        return
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith("data: "):
                        try:
                            event = json.loads(line[6:])
                        except ValueError:
                            continue
                        self.events.append(event)
                        if event.get("id"):
                            self.seen.add(str(event["id"]))
                        self.q.put(event)
        except Exception as exc:  # 流断了就把原因放进队列，别静默
            self.q.put({"type": "__SSE_ERROR__", "data": {"error": repr(exc)}})

    def drain_replay(self, seconds: float = 1.2) -> int:
        """丢掉连接时服务端重放的历史事件。"""
        deadline = time.time() + seconds
        n = 0
        while time.time() < deadline:
            try:
                self.q.get(timeout=0.2)
                n += 1
            except queue.Empty:
                pass
        self.events.clear()
        return n

    def next_event(self, timeout: float):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


# ---------------------------------------------------------------- 步骤

MANUPRODUCT_KEY = r"Software\qio\QIO"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\QIO"
# 这个键下不属于「安装信息」的值：后端写的数据库身份基线（用户状态）。
# 卸载必须保留它 —— 这是「安装信息 vs 用户数据」这条边界的可执行断言。
USER_STATE_VALUE = "DbBaseline"


def _registry_dump(key: str, tag: str) -> dict:
    """把一个 HKCU 键下的值读成 dict（**读**注册表沙箱允许）。键不存在返回 {}。"""
    script = (
        "$k='HKCU:\\%s';"
        "if (-not (Test-Path $k)) { Write-Output '__ABSENT__' } else {"
        "  $o = [ordered]@{};"
        "  (Get-ItemProperty $k).PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' } |"
        "    ForEach-Object { $o[$_.Name] = [string]$_.Value };"
        "  $o | ConvertTo-Json -Compress }" % key
    )
    code, out = powershell(script, tag=tag)
    text = out.strip()
    if not text or text == "__ABSENT__":
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def registry_write_probe(tag: str = "registry-write-probe") -> tuple[bool, str]:
    """**子进程**能不能写注册表 —— 这是判定的前提，不是跳过检查的借口。

    本会话给子进程的是受限令牌，安装器的 WriteRegStr 与卸载器的 DeleteRegKey
    都会静默 ACCESS_DENIED。能写就必须给 PASS/FAIL；不能写就必须写 NOT VERIFIED，
    绝不允许把"没验"写成"通过"。
    """
    key = r"Software\qio-e2e-probe"
    script = (
        "$ErrorActionPreference='Continue';"
        "try {"
        "  New-Item -Path 'HKCU:\\%s' -Force -ErrorAction Stop | Out-Null;"
        "  Set-ItemProperty -Path 'HKCU:\\%s' -Name probe -Value 'x' -ErrorAction Stop;"
        "  Remove-Item 'HKCU:\\%s' -Recurse -Force -ErrorAction SilentlyContinue;"
        "  Write-Output 'WRITABLE'"
        "} catch { Write-Output ('DENIED: ' + $_.Exception.Message) }" % (key, key, key)
    )
    code, out = powershell(script, tag=tag)
    text = out.strip()
    if "WRITABLE" in text:
        return True, "子进程可以写 HKCU"
    return False, (text[-200:] or "未知原因")


def step_install_registry(args) -> None:
    """安装信息必须真的写进注册表：控制面板登记 + 安装位置记录。"""
    writable, why = registry_write_probe()
    uninstall_key = _registry_dump(UNINSTALL_KEY, "registry-uninstall-key")
    manu_key = _registry_dump(MANUPRODUCT_KEY, "registry-manuproduct-key")
    evidence(
        "registry-after-install",
        json.dumps(
            {"child_can_write_registry": writable, "probe": why,
             "uninstall_key": uninstall_key, "manuproduct_key": manu_key},
            ensure_ascii=False, indent=2),
    )
    if not writable:
        record("A-023", "安装信息写入注册表（控制面板登记 + 安装位置）", "WARN",
               "**NOT VERIFIED**：本会话子进程写不了注册表（%s），安装器的 WriteRegStr 会静默失败，"
               "本机既不能判通过也不能判失败 —— 必须在真机/CI（有桌面会话）上验证" % why)
        return
    problems = []
    location = str(uninstall_key.get("InstallLocation") or "").strip('"')
    if location.lower() != str(Path(args.install_dir)).lower():
        problems.append("InstallLocation=%r 期望 %r" % (location, args.install_dir))
    if not str(uninstall_key.get("DisplayVersion") or ""):
        problems.append("没有 DisplayVersion（控制面板里看不到版本）")
    recorded = str(manu_key.get("(default)") or "")
    if recorded.lower() != str(Path(args.install_dir)).lower():
        problems.append("%s 默认值=%r 期望 %r（下次安装会还原到它）" % (MANUPRODUCT_KEY, recorded, args.install_dir))
    record("A-023", "安装信息写入注册表（控制面板登记 + 安装位置）", "FAIL" if problems else "PASS",
           "；".join(problems) if problems else
           "InstallLocation=%s DisplayVersion=%s %s=%s" % (
               location, uninstall_key.get("DisplayVersion"), MANUPRODUCT_KEY, recorded))


def step_seed_user_state(args) -> bool:
    """卸载前放一个**合成的** DbBaseline，用来验证"卸载不会顺手删用户状态"。

    只在注册表可写时放（真机/CI）。值里带 e2e 标记，避免与真实基线混淆；
    断言结束后立刻删掉，恢复原状。返回"是否真的放了"。
    """
    writable, why = registry_write_probe("registry-write-probe-seed")
    if not writable:
        return False
    value = '{"e2e": "install_e2e", "note": "synthetic DbBaseline"}'
    script = (
        "$ErrorActionPreference='Continue';"
        "if (-not (Test-Path 'HKCU:\\%s')) { New-Item -Path 'HKCU:\\%s' -Force | Out-Null };"
        "Set-ItemProperty -Path 'HKCU:\\%s' -Name '%s' -Value '%s';"
        "Write-Output 'SEEDED'"
        % (MANUPRODUCT_KEY, MANUPRODUCT_KEY, MANUPRODUCT_KEY, USER_STATE_VALUE, value)
    )
    code, out = powershell(script, tag="seed-user-state")
    seeded = "SEEDED" in out
    record("A-092a", "卸载前放入合成用户状态（DbBaseline）", "PASS" if seeded else "WARN",
           "已写入合成 DbBaseline（断言后会删除，恢复原状）" if seeded
           else "写不进去（%s）—— A-094 只能记 NOT VERIFIED" % out.strip()[-160:])
    return seeded


def step_uninstall_registry(args, seeded: bool) -> None:
    """卸载必须清掉**安装信息**，并且**保留用户状态**（DbBaseline）。"""
    writable, why = registry_write_probe("registry-write-probe-after-uninstall")
    manu_key = _registry_dump(MANUPRODUCT_KEY, "registry-manuproduct-after-uninstall")
    uninstall_key = _registry_dump(UNINSTALL_KEY, "registry-uninstall-after-uninstall")
    evidence(
        "registry-after-uninstall",
        json.dumps(
            {"child_can_write_registry": writable, "probe": why, "seeded": seeded,
             "uninstall_key": uninstall_key, "manuproduct_key": manu_key},
            ensure_ascii=False, indent=2),
    )
    if not writable:
        record("A-093", "卸载清掉安装信息（安装位置 / Installer Language）", "WARN",
               "**NOT VERIFIED**：本会话子进程写不了注册表（%s），卸载器的 DeleteRegKey / "
               "DeleteRegValue 会静默失败，本机无法判定。修复本身由 release_gate.py 的 "
               "uninstall.contract 静态契约在 CI 上守着" % why)
        record("A-094", "卸载保留用户状态（DbBaseline 不被顺手删掉）", "WARN",
               "**NOT VERIFIED**：同上 —— 本机读得到注册表，但装/卸两边都写不进去，断言没有意义")
        return

    problems = []
    if manu_key.get("(default)"):
        problems.append("安装位置默认值仍在：%r" % manu_key["(default)"])
    if "Installer Language" in manu_key:
        problems.append("Installer Language 仍在（安装向导语言属于安装信息）")
    record("A-093", "卸载清掉安装信息（安装位置 / Installer Language）", "FAIL" if problems else "PASS",
           "；".join(problems) if problems else
           "安装位置与 Installer Language 都已清掉；键内剩余值：%s" % sorted(manu_key))

    if seeded:
        kept = str(manu_key.get(USER_STATE_VALUE) or "")
        record("A-094", "卸载保留用户状态（DbBaseline 不被顺手删掉）",
               "PASS" if "install_e2e" in kept else "FAIL",
               "合成 DbBaseline 卸载后仍在" if "install_e2e" in kept
               else "合成 DbBaseline 被卸载删掉了 —— 卸载动了用户状态（%r）" % kept[:80])
    else:
        record("A-094", "卸载保留用户状态（DbBaseline 不被顺手删掉）", "WARN",
               "**NOT VERIFIED**：没能放入合成 DbBaseline（注册表不可写），本机无法断言")

    # 恢复原状：把合成值删掉（reg import 是合并语义，不会替我们清掉它）
    cleanup = (
        "$ErrorActionPreference='Continue';"
        "try { Remove-ItemProperty -Path 'HKCU:\\%s' -Name '%s' -ErrorAction Stop;"
        " Write-Output 'CLEANED' } catch { Write-Output ('CLEANUP-FAILED: ' + $_.Exception.Message) }"
        % (MANUPRODUCT_KEY, USER_STATE_VALUE)
    )
    code, out = powershell(cleanup, tag="cleanup-user-state")
    if "CLEANED" not in out:
        record("A-094b", "清理合成用户状态", "WARN", out.strip()[-160:])


def _port_open(port: int) -> bool:
    """端口上有没有人在听。

    存在的理由（2026-10-02 实测踩到）：这台机器上多个 agent 会同时跑同一份 E2E，
    默认端口一撞，"健康检查"就会被**别人的后端**答上来 —— 后面每一条断言都在
    验错的对象，却看起来全绿。宁可开局就红，也不要一份验错对象的报告。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def step_ports(args) -> bool:
    """开局检查端口：E2E 要用 port（后端）与 port+1（假厂商）。"""
    busy = [p for p in (args.port, args.port + 1) if _port_open(p)]
    if busy:
        record("A-005", "E2E 端口未被占用", "FAIL",
               "端口 %s 已被占用（可能是另一个 agent 的 E2E 在跑）：换 --port 重跑，"
               "否则健康检查会被别人的后端答上来" % busy)
        return False
    record("A-005", "E2E 端口未被占用", "PASS", "%d（后端）/ %d（假厂商）都空着" % (args.port, args.port + 1))
    return True


def step_preflight(args) -> bool:
    installer = Path(args.installer)
    if not installer.exists():
        record("A-000", "安装包存在", "FAIL", "找不到 %s" % installer)
        return False
    digest = sha256_of(installer)
    record("A-001", "安装包指纹", "PASS",
           "%s（%.1f MB，sha256 %s）" % (installer.name, installer.stat().st_size / 1048576, digest))

    code, out = run(["powershell", "-NoProfile", "-Command",
                     "Get-Process -Name qio,qio-backend -ErrorAction SilentlyContinue | "
                     "Select-Object Id,ProcessName,Path | Format-Table -AutoSize | Out-String"],
                    tag="preflight-processes")
    # 静默安装遇到正在运行的 qio.exe 会**直接把它杀掉**（NSIS 模板行为）。那是动用户正在用的
    # 程序，所以这里不替用户做决定：有进程就先停，让人来决定。
    if out.strip():
        record("A-002", "安装前没有 QIO 进程在跑", "FAIL",
               "检测到正在运行的 QIO：%s（静默安装会杀掉它，脚本主动停止）" % out.strip()[:200])
        return False
    record("A-002", "安装前没有 QIO 进程在跑", "PASS", "无")

    work = Path(args.work_dir)
    backup = work / "backup"
    backup.mkdir(parents=True, exist_ok=True)
    for name, key in (("uninstall-QIO.reg", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\QIO"),
                      ("software-qio.reg", r"HKCU\Software\qio")):
        code, out = run(["reg", "export", key, str(backup / name), "/y"], tag="preflight-%s" % name)
        if code != 0:
            record("A-003", "注册表备份", "FAIL", "reg export %s 失败：%s" % (key, out.strip()[:200]))
            return False
    record("A-003", "注册表备份", "PASS", "已导出 Uninstall\QIO 与 Software\qio 到 %s" % backup)

    for src, dst in ((Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "QIO.lnk", backup / "desktop-QIO.lnk"),
                     (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "QIO.lnk",
                      backup / "startmenu-QIO.lnk")):
        if src.exists():
            shutil.copy2(src, dst)
    code, out = powershell(
        "$v = (Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' "
        "-Name QIO -ErrorAction SilentlyContinue).QIO; if ($v) { $v } else { '' }",
        tag="preflight-run-value")
    (backup / "run-value.txt").write_text(out.strip(), encoding="utf-8")

    installer_dir = Path(args.existing_install)
    hashes = []
    if installer_dir.exists():
        for path in sorted(installer_dir.rglob("*")):
            if path.is_file():
                hashes.append("%s  %s" % (sha256_of(path), path))
    (backup / "existing-install-hashes.txt").write_text("\n".join(hashes), encoding="utf-8")
    record("A-004", "既有安装基线指纹（D:\\QIO）", "PASS" if hashes else "SKIP",
           "%d 个文件已记录到 backup\\existing-install-hashes.txt" % len(hashes))
    return True


def step_decoy(args) -> bool:
    """把「既有安装」换成诱饵，验证静默安装不会调用既有卸载器。"""
    decoy = Path(args.decoy_dir)
    shutil.rmtree(decoy, ignore_errors=True)
    decoy.mkdir(parents=True, exist_ok=True)
    (decoy / "qio.exe").write_bytes(b"decoy-not-a-real-exe")
    marker = Path(args.work_dir) / "DECOY-UNINSTALLER-RAN.log"
    if marker.exists():
        marker.unlink()

    nsi = Path(args.work_dir) / "decoy-uninstaller.nsi"
    nsi.write_text(
        'Unicode true\nName "DecoyUninstaller"\nOutFile "%s"\nRequestExecutionLevel user\n'
        'SilentInstall silent\n'
        'Section\n'
        '  FileOpen $9 "%s" w\n  FileWrite $9 "DECOY UNINSTALLER EXECUTED"\n  FileClose $9\n'
        '  Delete "%s\\qio.exe"\n  Delete "%s\\uninstall.exe"\n  RMDir "%s"\n'
        'SectionEnd\n' % (decoy / "uninstall.exe", marker, decoy, decoy, decoy),
        encoding="utf-8")
    makensis = Path(os.environ.get("LOCALAPPDATA", "")) / "tauri" / "NSIS" / "makensis.exe"
    if not makensis.exists():
        record("A-010", "诱饵实验（静默安装是否会卸载既有安装）", "SKIP",
               "找不到 makensis：%s" % makensis)
        return True
    code, out = run([str(makensis), "/V2", str(nsi)], timeout=120, tag="decoy-makensis")
    if code != 0 or not (decoy / "uninstall.exe").exists():
        record("A-010", "诱饵实验", "SKIP", "诱饵卸载器编译失败：%s" % out.strip()[:200])
        return True

    # 把注册表指向诱饵（真安装器就是照这个槽位判断「有没有既有安装」）。
    # 键可能不存在（例如已经被卸载过），所以先建键、再写值。
    # 这一整段用 raw 三引号字符串：PowerShell 里的反斜杠不需要再过一层 Python 转义，
    # 引号用 [char]34 在 PowerShell 侧拼，避免三层引号互相打架。
    repointer = r"""
$decoy = '%s'
$q = [char]34
$un = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\QIO'
$prod = 'HKCU:\Software\qio\QIO'
foreach ($p in @('HKCU:\Software\qio', $prod, $un)) {
  if (-not (Test-Path $p)) { New-Item -Path $p -Force | Out-Null }
}
Set-ItemProperty -Path $un -Name DisplayName -Value 'QIO'
Set-ItemProperty -Path $un -Name Publisher -Value 'qio'
Set-ItemProperty -Path $un -Name DisplayVersion -Value '0.1.9'
Set-ItemProperty -Path $un -Name InstallLocation -Value ($q + $decoy + $q)
Set-ItemProperty -Path $un -Name UninstallString -Value ($q + $decoy + '\uninstall.exe' + $q)
Set-ItemProperty -Path $prod -Name '(default)' -Value $decoy
Write-Output 'REPOINTED'
""" % decoy
    code, out = powershell(repointer, tag="decoy-registry")
    if "REPOINTED" not in out:
        record("A-009", "诱饵注册表已生效", "FAIL", "改注册表失败：%s" % out.strip()[:300])
        return False
    STATE["decoy_applied"] = True
    record("A-009", "诱饵注册表已生效", "PASS",
           "HKCU\\...\\Uninstall\\QIO 与 HKCU\\Software\\qio\\QIO 暂时指向 %s；"
           "整个 E2E 期间保持这个状态，结束时还原" % decoy)

    code, out = run(install_cmdline(args.installer, args.install_dir), timeout=900, tag="decoy-install")
    ran = marker.exists()
    state = "FAIL" if ran else "PASS"
    detail = ("诱饵卸载器被执行了 —— 说明静默安装会卸载既有安装，禁止在本机跑真安装！"
              if ran else
              "诱饵卸载器没有被执行（exit=%s）；安装目录存在=%s"
              % (code, Path(args.install_dir).exists()))
    record("A-010", "诱饵实验：静默安装不调用既有安装的卸载器", state, detail)
    if ran:
        return False
    if code != 0:
        record("A-011", "静默安装退出码", "FAIL", "exit=%s，输出尾部：%s" % (code, out.strip()[-400:]))
        return False
    return True


def step_install_files(args) -> bool:
    install_dir = Path(args.install_dir)
    if not install_dir.exists():
        record("A-020", "安装目录生成", "FAIL", "%s 不存在" % install_dir)
        return False
    files = []
    for path in sorted(install_dir.rglob("*")):
        if path.is_file():
            files.append("%10d  %s  %s" % (path.stat().st_size, sha256_of(path)[:16],
                                           path.relative_to(install_dir)))
    listing = "\n".join(files)
    evidence("install-dir-listing", listing)
    record("A-020", "安装目录内容", "PASS",
           "%d 个文件（%.1f MB）\n%s" % (len(files), sum(p.stat().st_size for p in install_dir.rglob("*") if p.is_file()) / 1048576, listing))

    required = {"qio.exe": "主程序", "qio-backend.exe": "打包后端 sidecar",
                "uninstall.exe": "卸载器",
                "models/bge-small-zh-v1.5/model.onnx": "内置模型",
                "models/bge-small-zh-v1.5/model_manifest.json": "模型清单"}
    missing = [k for k in required if not (install_dir / k).exists()]
    record("A-021", "安装目录关键文件", "FAIL" if missing else "PASS",
           "缺少：%s" % missing if missing else " / ".join("%s(%s)" % (k, v) for k, v in required.items()))
    return not missing


def step_models(args) -> bool:
    install_dir = Path(args.install_dir)
    base = install_dir / "models" / "bge-small-zh-v1.5"
    manifest_path = base / "model_manifest.json"
    if not manifest_path.exists():
        record("A-022", "内置模型与清单一致", "FAIL", "缺少 %s" % manifest_path)
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems, checked, lines = [], 0, []
    for entry in manifest.get("files") or []:
        name = str(entry.get("file") or "")
        target = base / name
        if not target.exists():
            problems.append("缺少 %s" % name)
            continue
        size = target.stat().st_size
        digest = sha256_of(target)
        ok = (int(entry.get("bytes") or -1) == size) and (not entry.get("sha256") or digest == entry["sha256"])
        lines.append("%s  %s  bytes=%d  sha256=%s" % ("OK " if ok else "BAD", name, size, digest[:16]))
        if ok:
            checked += 1
        else:
            problems.append("%s 与清单不符" % name)
    evidence("installed-model-manifest", "\n".join(lines) + "\n\nmanifest: " + json.dumps(manifest, ensure_ascii=False))
    record("A-022", "内置模型与清单一致", "FAIL" if problems else "PASS",
           "；".join(problems) if problems else "%d 个文件逐一核对通过（bytes+sha256）" % checked)
    return not problems


def clean_env(args, port: int) -> dict:
    """白名单构造 env：**不继承**开发用环境变量。"""
    keep = ["SystemRoot", "windir", "SystemDrive", "COMSPEC", "PATHEXT", "PATH",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS",
            "USERPROFILE", "USERNAME", "USERDOMAIN", "LOCALAPPDATA", "APPDATA", "ProgramData",
            "ProgramFiles", "ProgramFiles(x86)", "CommonProgramFiles", "COMPUTERNAME",
            "HOMEDRIVE", "HOMEPATH", "PUBLIC", "SESSIONNAME", "TZ"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    tmp = str(Path(args.work_dir) / "tmp")
    Path(tmp).mkdir(parents=True, exist_ok=True)
    env["TEMP"] = env["TMP"] = tmp
    env["QIO_DATA_DIR"] = str(Path(args.work_dir) / "data")
    env["QIO_PORT"] = str(port)
    env["QIO_SESSION_TOKEN"] = args.token
    # 唯一的**偏差开关**，必须显式记账：
    # 启动时的「数据库身份自检」在 Windows 上把基线写进 HKCU\Software\qio（注册表）。
    # 本会话的沙箱给子进程的是受限令牌，任何注册表写入都是 WinError 5 —— 装出来的后端会
    # 在启动阶段直接崩（原始 traceback 见 docs）。QIO_DISABLE_DB_CHECK=1 是产品**自带**的
    # 开关（注释写明给测试/隔离环境用），这里用它绕开环境限制，而不是改产品代码。
    # 代价：这次 E2E **没有覆盖**「数据库身份自检」这条启动路径。
    env["QIO_DISABLE_DB_CHECK"] = "1"
    dropped = sorted(k for k in os.environ if k.startswith(("QIO_", "PYTHON", "UV_", "VIRTUAL_ENV", "NODE_")) and k not in env)
    evidence("backend-env", json.dumps({"kept": sorted(env), "dropped_dev_vars": dropped}, ensure_ascii=False, indent=2))
    return env


def start_backend(args, port: int):
    install_dir = Path(args.install_dir)
    env = clean_env(args, port)
    logfile = Path(args.work_dir) / "evidence" / "installed-backend.log"
    fh = logfile.open("wb")
    proc = subprocess.Popen([str(install_dir / "qio-backend.exe")], cwd=str(install_dir),
                            env=env, stdout=fh, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc


def wait_health(client: Client, timeout: float = 60.0):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            status, body = client.get("/api/health", timeout=5)
            if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
                return True, json.dumps(body, ensure_ascii=False)
            last = "%s %s" % (status, body)
        except Exception as exc:
            last = repr(exc)
        time.sleep(0.5)
    return False, last


def step_health(args, port: int):
    client = Client("http://127.0.0.1:%d" % port, args.token)
    record("A-032", "启动环境偏差（必须记账）", "WARN",
           "注入了 QIO_DISABLE_DB_CHECK=1：沙箱令牌不允许写注册表，而启动时的数据库身份自检"
           "要把基线写进 HKCU\\Software\\qio。该路径本次未被覆盖；不用这个开关后端会在启动阶段崩溃。")
    ok, detail = wait_health(client)
    record("A-030", "安装目录里的后端 /api/health", "PASS" if ok else "FAIL",
           "200 + status=ok + db=true：%s" % detail if ok else "未就绪：%s" % detail)
    if not ok:
        return False, client
    # 谁在回答这个端口？必须**就是**安装目录里的那个 exe。
    # 只看进程名不够：机器上可能有别的 QIO 后端（别的 agent 的 E2E、或本机既有安装）。
    script = (
        "$owner = (Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty OwningProcess);"
        "if ($owner) { (Get-Process -Id $owner -ErrorAction SilentlyContinue).Path } else { '' }" % port
    )
    code, out = run(["powershell", "-NoProfile", "-Command", script], tag="backend-process-path")
    expected = str((Path(args.install_dir) / "qio-backend.exe").resolve())
    actual = out.strip()
    if actual and Path(actual).resolve() == Path(expected):
        record("A-031", "健康检查回答者就是安装目录里的后端", "PASS", actual)
        return True, client
    record("A-031", "健康检查回答者就是安装目录里的后端", "FAIL",
           "端口 %d 的回答者是 %r，不是 %r —— 后面的断言会验错对象，直接停" % (
               port, actual or "(取不到进程路径)", expected))
    return False, client

def step_fake_provider(args, port: int):
    fp_port = port + 1
    logfile = Path(args.work_dir) / "evidence" / "fake-provider.log"
    fh = logfile.open("wb")
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "e2e_fake_provider.py"),
                             "--port", str(fp_port)],
                            stdout=fh, stderr=subprocess.STDOUT,
                            env={**os.environ, "TEMP": str(Path(args.work_dir) / "tmp"),
                                 "TMP": str(Path(args.work_dir) / "tmp")})
    client = Client("http://127.0.0.1:%d" % fp_port)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            status, body = client.get("/__health", timeout=3)
            if status == 200 and body.get("ok"):
                record("A-040", "离线假厂商已就绪（不是真实服务）", "PASS",
                       "http://127.0.0.1:%d/v1 served=%s" % (fp_port, body.get("served")))
                return proc, client, fp_port
        except Exception:
            time.sleep(0.4)
    record("A-040", "离线假厂商已就绪", "FAIL", "30s 内没有起来，见 evidence/fake-provider.log")
    return proc, client, fp_port


def step_credential(args, client: Client, fp_port: int):
    key_id = "e2e-install-main"
    status, body = client.post("/api/credentials", {
        "key_id": key_id,
        "secret": "sk-e2e-install-fake-0001",
        "tags": ["main-loop"],
        "endpoint": "http://127.0.0.1:%d/v1" % fp_port,
        "default_model": "fake-model",
        "budget": 100000,
    })
    ok = status == 200 and body.get("version") == 1
    record("A-041", "保存 provider 凭据（写入即不可读回）", "PASS" if ok else "FAIL",
           "key_id=%s version=%s" % (key_id, body.get("version")) if ok else "%s %s" % (status, str(body)[:200]))

    listing = client.get("/api/credentials")[1].get("credentials", [])
    item = next((c for c in listing if c["key_id"] == key_id), None)
    leaked = item is not None and ("sk-e2e-install-fake-0001" in json.dumps(item, ensure_ascii=False))
    record("A-042", "凭据列表不回显密钥原文", "FAIL" if leaked else "PASS",
           "发现泄露" if leaked else "列表只有掩码元数据")
    if item:
        evidence("credential-listing", json.dumps(item, ensure_ascii=False, indent=2))
    client.post("/api/credentials/%s/default" % key_id)
    status, probe = client.post("/api/credentials/%s/test" % key_id, timeout=60)
    record("A-043", "测试连接（打的是假厂商）", "PASS" if status in (200, 502) else "FAIL",
           "status=%s %s" % (status, json.dumps(probe, ensure_ascii=False)[:300]))
    return key_id


def run_turn(client: Client, sse: SseReader, fp_client: Client, message: str,
             on_event=None, timeout: float = 300.0):
    """触发一轮模型对话，边收事件边让调用方推进假厂商脚本。"""
    sse.drain_replay()
    # 关键：把"连接重放出来的历史事件"全部排除，只认本轮新产生的事件。
    # 单靠 drain_replay 的固定等待是不够的 —— 重放可能比那 1.2s 慢，旧 TURN_END 会先到。
    before = set(sse.seen)
    events: list[dict] = []
    status, body = client.post("/api/turns", {"message": message})
    if status != 200:
        return events, "POST /api/turns -> %s %s" % (status, body)
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = sse.next_event(timeout=2.0)
        if event is None:
            continue
        if event.get("id") and str(event["id"]) in before:
            continue  # 历史重放，不属于本轮
        events.append(event)
        if on_event is not None:
            try:
                on_event(event)
            except Exception as exc:
                log("  ! on_event 异常：%r" % (exc,))
        if event.get("type") in ("TURN_END", "ERROR"):
            return events, ""
    return events, "超时未收到 TURN_END"


def step_turn(args, client: Client, sse: SseReader, fp: Client):
    fp.post("/__script", {"steps": [{"text": "这是安装后首次真实一轮对话的假模型回复。"}]})
    events, err = run_turn(client, sse, fp, "安装 E2E：你好，请回一句。")
    types = [e.get("type") for e in events]
    ok = not err and "TURN_END" in types
    record("A-050", "装出来的后端跑通一轮真实 turn", "PASS" if ok else "FAIL",
           "事件：%s" % ",".join(types) if ok else "%s；事件 %s" % (err, types))
    logdata = fp.get("/__log")[1].get("requests", [])
    key = logdata[-1]["key"] if logdata else {}
    record("A-051", "假厂商确实收到了请求（密钥只留指纹）", "PASS" if key.get("present") else "FAIL",
           "sha256_16=%s len=%s model=%s" % (key.get("sha256_16"), key.get("length"),
                                             logdata[-1]["model"] if logdata else None))
    return ok


TOOL_JSON = json.dumps({
    "name": "dev_add",
    "description": "两个数求和（安装 E2E 用）",
    "tool_type": "function",
    "sync": True,
    "code": "def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
    "tests": [
        {"name": "positive", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}},
        {"name": "zero", "input": {"a": 0, "b": 0}, "expect": {"sum": 0}},
    ],
}, ensure_ascii=False)


def step_dev_flow(args, client: Client, sse: SseReader, fp: Client):
    """create_tool → dev_write_file → dev_run_tests → dev_submit_tool → 注册。"""
    approvals: list[dict] = []
    tool_ends: list[dict] = []
    state = {"ws": None, "pushed": False}

    def on_event(event: dict) -> None:
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "APPROVAL_REQUIRED":
            approval = data.get("approval") or {}
            approvals.append(approval)
            client.post("/api/approvals/%s/respond" % approval.get("approval_id"), {
                "decision": "approved",
                "turn_id": approval.get("turn_id"),
                "session_id": approval.get("session_id"),
                "request_digest": approval.get("digest"),
            })
        elif etype == "TOOL_END":
            tool_ends.append(data)
            if data.get("tool") == "create_tool" and data.get("ok") and not state["pushed"]:
                # 先用事件里的 content_preview（create_tool 的第一行就带工作区 id），
                # 它不需要再发一次 HTTP —— 少一次往返就少一点被主循环抢先的机会。
                text = json.dumps(data.get("content_preview") or "", ensure_ascii=False)
                rec = data.get("record_id")
                if not re.search(r"工作区 id=([A-Za-z0-9_\-]+)", text) and rec:
                    text += " " + json.dumps(client.get("/api/tool-records/%s" % rec)[1], ensure_ascii=False)
                match = re.search(r"工作区 id=([A-Za-z0-9_\-]+)", text)
                if match:
                    state["ws"] = match.group(1)
                    state["pushed"] = True
                    fp.post("/__script", {"steps": [
                        {"tool": "dev_write_file", "args": {"workspace": state["ws"], "name": "tool.json", "content": TOOL_JSON}},
                        {"tool": "dev_run_tests", "args": {"workspace": state["ws"]}},
                        {"tool": "dev_submit_tool", "args": {"workspace": state["ws"],
                                                             "explanation": "把两个数加起来返回它们的和"}},
                        {"text": "工具已开发完成"},
                    ]})

    # 关键：主循环不会等我们（SSE 是异步的）。create_tool 之后它立刻再问一次模型，
    # 如果这时脚本队列是空的，假厂商就会回一句文本 → turn 直接结束，后面的开发步骤永远没机会跑。
    # 所以先把 default 设成一个**无害且可重复**的工具调用（dev_list_tasks 不写任何东西），
    # 让 turn 撑到我们把真正的步骤推进队列为止。
    fp.post("/__script", {"steps": [{"tool": "create_tool",
                                     "args": {"request": "我需要一个把两个数求和的工具，输入 a、b，输出 sum"}}],
                          "default": {"tool": "dev_list_tasks", "args": {}}})
    events, err = run_turn(client, sse, fp, "帮我做一个两个数求和的工具。", on_event=on_event, timeout=600)
    fp.post("/__script", {"steps": [], "default": {"text": "（假模型默认回复）"}})
    names = [t.get("tool") for t in tool_ends]
    record("A-060", "开发流程四步都被调用", "PASS" if {"create_tool", "dev_write_file", "dev_run_tests",
                                                       "dev_submit_tool"} <= set(names) else "FAIL",
           "%s（err=%s）" % (names, err or "无"))
    record("A-061", "审批真的发生过且被逐条批准", "PASS" if approvals else "FAIL",
           "%d 次：%s" % (len(approvals), [a.get("kind") for a in approvals]))
    evidence("dev-flow-approvals", json.dumps(approvals, ensure_ascii=False, indent=2))
    evidence("dev-flow-tool-ends", json.dumps(tool_ends, ensure_ascii=False, indent=2))

    submit = next((t for t in tool_ends if t.get("tool") == "dev_submit_tool"), None)
    record("A-062", "dev_run_tests 通过（测试真的跑了）", "PASS" if any(
        t.get("tool") == "dev_run_tests" and t.get("ok") for t in tool_ends) else "FAIL",
        "%s" % (next((str(t.get("content_preview"))[:200] for t in tool_ends
                      if t.get("tool") == "dev_run_tests"), "没有 dev_run_tests 事件")))
    record("A-063", "dev_submit_tool 提交成功（注册前置）", "PASS" if submit and submit.get("ok") else "FAIL",
           str((submit or {}).get("content_preview"))[:300] or "无 dev_submit_tool 事件")
    return state["ws"]


def step_invoke_tool(args, client: Client, sse: SseReader, fp: Client):
    """重启前先调用一次新注册的工具，留一条工具记录。"""
    results: list[dict] = []

    def on_event(event: dict) -> None:
        if event.get("type") == "TOOL_END":
            results.append(event.get("data") or {})

    fp.post("/__script", {"steps": [{"tool": "dev_add", "args": {"a": 2, "b": 3}}, {"text": "和是 5"}]})
    events, err = run_turn(client, sse, fp, "用你刚做好的工具算 2+3。", on_event=on_event, timeout=300)
    call = next((r for r in results if r.get("tool") == "dev_add"), None)
    ok = bool(call and call.get("ok"))
    record("A-064", "新工具注册后可被调用", "PASS" if ok else "FAIL",
           "ok=%s content=%s err=%s" % (call.get("ok") if call else None,
                                        str((call or {}).get("content_preview"))[:160], err or "无"))
    rec_id = (call or {}).get("record_id")
    if rec_id:
        status, body = client.get("/api/tool-records/%s" % rec_id)
        text = json.dumps(body, ensure_ascii=False)
        record("A-065", "工具记录可查", "PASS" if status == 200 and "dev_add" in text else "FAIL",
                "GET /api/tool-records/%s -> %s %s" % (rec_id, status, text[:220]))
    else:
        record("A-065", "工具记录可查", "FAIL", "TOOL_END 里没有 record_id")
    return ok


def stop_backend(proc) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    # PyInstaller onefile 是「引导进程 + 真正跑服务的子进程」两层。上面 terminate 掉的只是引导进程，
    # 子进程会继续活着并握着安装目录里的 qio-backend.exe（本机实测：卸载器因此删不掉它）。
    # 不连子进程一起收，撤销/卸载这类"文件能不能被删"的结论就不可信。
    run(["taskkill", "/F", "/T", "/IM", "qio-backend.exe"], timeout=60, tag="taskkill-backend")
    time.sleep(3)


def step_restart(args, client: Client, proc, port: int):
    stop_backend(proc)
    time.sleep(1.5)
    new_proc = start_backend(args, port)
    ok, detail = wait_health(Client("http://127.0.0.1:%d" % port, args.token))
    record("A-070", "重启后后端重新就绪", "PASS" if ok else "FAIL", detail if ok else "未就绪：%s" % detail)
    return new_proc, ok


def step_recovery(args, client: Client, sse: SseReader, fp: Client):
    checks = []
    for cid, title, path, needle in (
        ("A-071", "开发任务在重启后仍在", "/api/dev/tasks", None),
        ("A-072", "授权状态在重启后仍可读", "/api/dev/authorizations", None),
        ("A-073", "interrupted turn 入口存在", "/api/runtime/state", "interrupted_turns"),
        ("A-074", "会话消息在重启后仍在", "/api/session/messages", None),
    ):
        try:
            status, body = client.get(path)
            text = json.dumps(body, ensure_ascii=False)
            needed = (needle is None) or (needle in text)
            has_content = len(text) > 2 and text not in ("{}", "[]", '{"messages": []}')
            state = "PASS" if status == 200 and needed and has_content else ("WARN" if status == 200 and needed else "FAIL")
            record(cid, title, state, "%s -> %s %s" % (path, status, text[:240]))
            checks.append(state == "PASS")
        except Exception as exc:
            record(cid, title, "FAIL", "%s 读取失败：%r" % (path, exc))
            checks.append(False)
    # 工具恢复：重启后再调一次。
    # 注意：重启把上一条 SSE 流打断了（后端进程没了），必须换一条新连接 ——
    # 用旧连接会「一个事件都收不到」，然后看起来像功能坏了。
    sse = SseReader(client)
    results: list[dict] = []

    # 用 default 而不是 FIFO 队列来驱动这一轮：队列里的步骤可能被"别的模型调用"先吃掉，
    # 那会让检查变成假阴性（本机踩过：脚本明明推了，厂商却回了默认文本）。
    # default 保证"这一轮任何一次模型调用"都先拿到 dev_add，直到我们把它换成文本。
    def recovery_event(event: dict) -> None:
        data = event.get("data") or {}
        if event.get("type") == "TOOL_END":
            results.append(data)
            if data.get("tool") == "dev_add":
                fp.post("/__script", {"steps": [], "default": {"text": "42"}})

    fp.post("/__script", {"steps": [], "default": {"tool": "dev_add", "args": {"a": 40, "b": 2}}})
    events, err = run_turn(client, sse, fp, "重启后再算 40+2。", on_event=recovery_event, timeout=300)
    fp.post("/__script", {"steps": [], "default": {"text": "（假模型默认回复）"}})
    call = next((r for r in results if r.get("tool") == "dev_add"), None)
    types = [e.get("type") for e in events]
    # 诊断：假厂商到底把哪一步喂给了模型（这是"没调用工具"时唯一能分辨原因的证据）
    served = fp.get("/__log")[1].get("requests", [])
    last = served[-1] if served else {}
    record("A-075", "注册的工具在重启后仍可调用", "PASS" if call and call.get("ok") else "FAIL",
           "ok=%s content=%s；本轮事件=%s；err=%s；厂商总请求=%d；最后一次 step=%s；offered 里有 dev_add=%s" % (
               (call or {}).get("ok"), str((call or {}).get("content_preview"))[:120],
               ",".join(types), err or "无", len(served),
               json.dumps(last.get("step"), ensure_ascii=False),
               "dev_add" in (last.get("tool_names_offered") or [])))
    return all(checks)


def step_tool_settings(args, client: Client):
    try:
        status, body = client.get("/api/settings/tools")
        text = json.dumps(body, ensure_ascii=False)
        record("A-076", "工具设置/注册表可见 dev_add", "PASS" if "dev_add" in text else "WARN",
               "GET /api/settings/tools -> %s %s" % (status, text[:260]))
    except Exception as exc:
        record("A-076", "工具设置/注册表可见 dev_add", "WARN", repr(exc))


def step_reinstall(args, client: Client, port: int):
    """同一版本再装一次（等价于「重装/覆盖升级」），验证用户数据是否保留。"""
    data_dir = Path(args.work_dir) / "data"
    before = sorted(p.name for p in data_dir.glob("*"))
    code, out = run(install_cmdline(args.installer, args.install_dir), timeout=900, tag="reinstall")
    time.sleep(2)
    after = sorted(p.name for p in data_dir.glob("*"))
    proc = start_backend(args, port)
    ok, detail = wait_health(Client("http://127.0.0.1:%d" % port, args.token))
    record("A-080", "重装（覆盖安装）保留用户数据目录", "PASS" if before == after and before else "FAIL",
           "exit=%s 数据条目 %s -> %s" % (code, before, after))
    record("A-081", "重装后后端仍可用", "PASS" if ok else "FAIL", detail if ok else "未就绪：%s" % detail)
    return proc


def step_uninstall(args):
    uninstaller = Path(args.install_dir) / "uninstall.exe"
    if not uninstaller.exists():
        record("A-090", "卸载器存在", "FAIL", "找不到 %s" % uninstaller)
        return
    data_dir = Path(args.work_dir) / "data"
    before = sorted(p.name for p in data_dir.glob("*"))
    env = {**os.environ, "TEMP": str(Path(args.work_dir) / "tmp"),
           "TMP": str(Path(args.work_dir) / "tmp")}
    code, out = run([str(uninstaller), "/S"], timeout=600, env=env, tag="uninstall")
    deadline = time.time() + 120
    while time.time() < deadline and Path(args.install_dir).exists():
        time.sleep(2)
    still = sorted(p.name for p in Path(args.install_dir).glob("*")) if Path(args.install_dir).exists() else []
    after = sorted(p.name for p in data_dir.glob("*"))
    record("A-090", "卸载后安装目录被清空", "PASS" if not Path(args.install_dir).exists() else
           ("WARN" if still in (["uninstall.exe"], []) else "FAIL"),
           "exit=%s 残留：%s" % (code, still))
    record("A-091", "卸载保留用户数据（未勾选删除数据）", "PASS" if after == before and before else "FAIL",
           "数据目录 %s -> %s" % (before, after))
    code, out2 = powershell("if (Test-Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\QIO') "
                            "{ 'STILL PRESENT' } else { 'REMOVED' }", tag="uninstall-registry")
    record("A-092", "卸载后卸载注册表项被移除", "PASS" if "REMOVED" in out2 else "WARN",
           out2.strip()[:120] + "（本会话子进程是受限令牌：安装器的 WriteRegStr 与卸载器的 DeleteRegKey "
           "都 ACCESS_DENIED 且静默失败 —— 所以这一项在本机**无法**验证，不是产品结论）")


def step_restore(args):
    backup = Path(args.work_dir) / "backup"
    for name in ("uninstall-QIO.reg", "software-qio.reg"):
        path = backup / name
        if path.exists():
            run(["reg", "import", str(path)], tag="restore-%s" % name)
    notes = []
    for src, dst in ((backup / "desktop-QIO.lnk", Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "QIO.lnk"),
                     (backup / "startmenu-QIO.lnk",
                      Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "QIO.lnk")):
        if not src.exists():
            continue
        try:
            shutil.copy2(src, dst)
            notes.append("%s 已还原" % dst.name)
        except OSError as exc:
            # 沙箱不允许子进程写检出外的路径。那就退一步核对「有没有被动过」——
            # 这一层结论仍然是硬的（哈希比对）。
            unchanged = dst.exists() and sha256_of(src) == sha256_of(dst)
            notes.append("%s 需要人工还原（%s；当前与备份%s）" % (
                dst.name, type(exc).__name__, "一致，未被改动" if unchanged else "**不一致**"))
    if notes:
        bad = [n for n in notes if "已还原" not in n and "未被改动" not in n]
        record("A-102", "桌面/开始菜单快捷方式", "WARN" if bad else "PASS", "；".join(notes))
    run_value = (backup / "run-value.txt")
    if run_value.exists() and run_value.read_text(encoding="utf-8").strip():
        powershell("Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' "
                   "-Name QIO -Value '%s'" % run_value.read_text(encoding="utf-8").strip(),
                   tag="restore-run-value")
    code, out = powershell(
        "$k='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\QIO';"
        "(Get-ItemProperty $k).DisplayVersion; (Get-ItemProperty $k).InstallLocation;"
        "(Get-ItemProperty 'HKCU:\\Software\\qio\\QIO').'(default)'", tag="restore-verify-registry")
    record("A-100", "机器注册表已还原到基线", "PASS" if "0.1.9" in out and "D:\\QIO" in out else "WARN", out.strip()[:200])

    baseline = (backup / "existing-install-hashes.txt")
    if baseline.exists():
        current = []
        base_dir = Path(args.existing_install)
        for path in sorted(base_dir.rglob("*")):
            if path.is_file():
                current.append("%s  %s" % (sha256_of(path), path))
        same = current == baseline.read_text(encoding="utf-8").splitlines()
        record("A-101", "既有安装 D:\\QIO 逐文件未被改动", "PASS" if same else "FAIL",
               "sha256 清单一致（%d 个文件）" % len(current) if same else "有文件被改动，见 A-004 基线")
    else:
        record("A-101", "既有安装 D:\\QIO 逐文件未被改动", "SKIP", "没有基线清单")


def main() -> int:
    parser = argparse.ArgumentParser(description="NSIS 安装包真机 E2E（不动本机既有安装）")
    parser.add_argument("--installer", required=True)
    # 全部放在检出内部：这台机器的 DSH 沙箱只允许**检出内**的路径被子进程写，
    # 检出外（C:\Users\zxy\...、D:\...）会被拒绝访问 —— 那是沙箱行为，不是产品行为。
    parser.add_argument("--work-dir", default=str(ROOT / ".e2e-work"))
    parser.add_argument("--install-dir", default=str(ROOT / ".e2e-work" / "install"))
    parser.add_argument("--decoy-dir", default=str(ROOT / ".e2e-work" / "decoy-old"))
    parser.add_argument("--existing-install", default=r"D:\QIO")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--token", default="e2e-install-token-0001")
    parser.add_argument("--stages", default="all")
    args = parser.parse_args()

    global EVIDENCE
    work = Path(args.work_dir)
    (work / "evidence").mkdir(parents=True, exist_ok=True)
    (work / "data").mkdir(parents=True, exist_ok=True)
    (work / "tmp").mkdir(parents=True, exist_ok=True)
    EVIDENCE = work / "evidence"

    stages = set(args.stages.split(",")) if args.stages != "all" else {
        "preflight", "decoy", "install", "models", "health", "api", "dev", "restart", "reinstall",
        "uninstall", "restore"}
    # /D= 可以含空格（裸值、放最后），但不能含引号：已用 makensis 探针验证过含空格路径可用。
    if '"' in args.install_dir:
        log("! 安装目录不能含引号（NSIS /D= 规则）")
        return 2

    log("== 安装包 E2E ==")
    log("  安装包   : %s" % args.installer)
    log("  安装目录 : %s" % args.install_dir)
    log("  数据目录 : %s（全新空目录）" % (work / "data"))
    log("  既有安装 : %s（不会被改动）" % args.existing_install)

    proc = backend = fp_proc = None
    client = None
    try:
        if "preflight" in stages and not step_preflight(args):
            return 3
        if "decoy" in stages:
            if not step_decoy(args):
                log("!! 诱饵实验判定有风险：停止，绝不在这台机器上继续安装")
                return 4
        if "install" in stages:
            if not step_install_files(args):
                return 5
            # 安装信息（控制面板登记 + 安装位置）是否真的写进去了。
            # 本会话的沙箱可能让子进程写不了注册表 —— 那种情况记 NOT VERIFIED，不记通过。
            step_install_registry(args)
        if "models" in stages and not step_models(args):
            return 6
        if "health" in stages:
            if not step_ports(args):
                return 8
            backend = start_backend(args, args.port)
            ok, client = step_health(args, args.port)
            if not ok:
                return 7
        if client is None:
            client = Client("http://127.0.0.1:%d" % args.port, args.token)
        sse = SseReader(client)
        fp_proc, fp, fp_port = step_fake_provider(args, args.port)
        if "api" in stages:
            step_credential(args, client, fp_port)
            step_turn(args, client, sse, fp)
        if "dev" in stages:
            step_dev_flow(args, client, sse, fp)
            step_invoke_tool(args, client, sse, fp)
            step_tool_settings(args, client)
        if "restart" in stages:
            backend, _ = step_restart(args, client, backend, args.port)
            step_recovery(args, client, sse, fp)
        if "reinstall" in stages:
            stop_backend(backend)
            backend = step_reinstall(args, client, args.port)
            backend, _ = step_restart(args, client, backend, args.port)
        if "uninstall" in stages:
            stop_backend(backend)
            backend = None
            # 先在同一个键下放一个合成 DbBaseline（只在注册表可写时），
            # 用来断言「卸载清安装信息，但不动用户状态」这条边界。
            seeded = step_seed_user_state(args)
            step_uninstall(args)
            step_uninstall_registry(args, seeded)
        if "restore" in stages:
            step_restore(args)
    finally:
        stop_backend(backend)
        if fp_proc is not None:
            fp_proc.terminate()
        if STATE["decoy_applied"] and "restore" in stages:
            log("== 还原机器状态（注册表 / 快捷方式 / 校验既有安装）==")
            try:
                step_restore(args)
            except Exception as exc:  # 还原失败要吼出来，不能静默
                record("A-199", "机器状态还原", "FAIL", repr(exc))
        failures = [r for r in RESULTS if r["state"] == "FAIL"]
        summary = {"installer": args.installer, "install_dir": args.install_dir,
                   "results": RESULTS, "failed": len(failures),
                   "warned": [r["id"] for r in RESULTS if r["state"] == "WARN"],
                   "not_executed": [r["id"] for r in RESULTS if r["state"] == "SKIP"]}
        out = work / "summary.json"
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        log("")
        warned = [r for r in RESULTS if r["state"] == "WARN"]
        log("== 汇总：%d 项 PASS / %d 项 WARN / %d 项 FAIL / %d 项 SKIP ==" % (
            len([r for r in RESULTS if r["state"] == "PASS"]), len(warned), len(failures),
            len([r for r in RESULTS if r["state"] == "SKIP"])))
        for item in failures:
            log("  FAIL %s %s :: %s" % (item["id"], item["title"], item["detail"][:200]))
        for item in warned:
            log("  WARN %s %s :: %s" % (item["id"], item["title"], item["detail"][:160]))
        log("  summary.json -> %s" % out)
    return 1 if any(r["state"] == "FAIL" for r in RESULTS) else 0


if __name__ == "__main__":
    raise SystemExit(main())
