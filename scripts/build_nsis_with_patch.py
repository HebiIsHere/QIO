#!/usr/bin/env python
"""把 Tauri 的 NSIS 打包过程包起来：在「生成 installer.nsi」与「makensis 编译」之间打所有权补丁。

为什么必须夹在这两步之间
------------------------
Tauri 的 NSIS 打包器是**一个进程内**依次做三件事：
  1) 用内置模板 + 仓库里的 installer-hooks.nsh 生成 target/release/nsis/<arch>/installer.nsi；
  2) 立刻用 makensis 编译它；
  3) 产出安装包。
它没有给「生成之后、编译之前」留钩子（installerHooks 只能追加宏，不能替换模板里已有的语句），
所以只能由外层在两步之间把补丁打进去 —— 这个脚本就是那个外层。

做法：一边跑 tauri build，一边盯着 installer.nsi；一旦出现且**大小连续两次不变**，
就立刻调 scripts/patch_nsis_template.py（幂等）并记录耗时。编译在补丁之后才开始，
所以不需要任何"事后修补二进制"的技巧。

安全阀：
  * 补丁失败（锚点变了等）→ 立刻杀掉 tauri build 子进程并以非 0 退出（绝不让一个
    没有补丁的安装包悄悄产出）；
  * 编译已经跑完才追上（patch 太晚）→ 事后核对补丁报告与产物，报 FAIL；
  * --check 只做静态检查，不跑构建。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCH = ROOT / "scripts" / "patch_nsis_template.py"


def _configure_output() -> None:
    """任何控制台编码下都不能崩（英文 Windows / CI runner 的 stdout 默认是 cp1252）。

    回归的事故形状（2026-10-03，Windows CI 抓到）：本脚本的日志与 JSON 报告全是中文，
    第一条 print 就 UnicodeEncodeError，打包步骤以 traceback 收场 —— 与 release_gate.py
    2026-10-02 那次是同一个坑。做法与 scripts/release_gate.py::_configure_output 一致：
    tty 保留自身编码只转义；重定向 / CI 直接写 UTF-8 字节。
    """
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

def log(msg: str) -> None:
    print("[nsis-patch] " + msg, flush=True)


def stage_guard(nsis_dir: Path, guard: Path) -> None:
    """把守卫宏拷到模板目录：installer.nsi 的 !include "qio-ownership.nsh" 是相对路径。"""
    nsis_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(guard, nsis_dir / guard.name)


GUARD_MARKER = "--allow-main-exe"  # 只出现在守卫宏里的参数：用它证明"编译产物真的带守卫"


def verify_compiled_artifact(bundle_dir: Path, since: float) -> tuple[bool, str, str]:
    """核对**编译出来的安装包**里有没有守卫宏（不是只看中间文件 installer.nsi）。

    为什么必须看产物：补丁与 makensis 是并发的（Tauri 生成完模板就立刻编译）。
    如果 makensis 已经读过旧内容，installer.nsi 事后会是"已打补丁"的样子，
    但产物里没有守卫 —— 只看中间文件就是假绿。
    NSIS 是 Unicode 构建，字符串以 UTF-16LE 存在，所以按 UTF-16LE 搜。
    """
    if not bundle_dir.is_dir():
        return False, "", "产物目录不存在：%s" % bundle_dir
    candidates = [p for p in bundle_dir.glob("QIO_*_x64-setup*.exe") if p.stat().st_mtime >= since - 1]
    if not candidates:
        return False, "", "本次构建没有产出新的安装包（%s）" % bundle_dir
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    needle = GUARD_MARKER.encode("utf-16-le")
    with newest.open("rb") as fh:
        data = fh.read()
    if needle in data:
        return True, str(newest), "产物里找到守卫标记 %r（UTF-16LE）" % GUARD_MARKER
    return False, str(newest), "产物里**没有**守卫标记 %r：补丁没赶上 makensis 读文件" % GUARD_MARKER


GUARD_MARKER = "--allow-main-exe"  # 只出现在守卫宏里的参数：用它证明"编译产物真的带守卫"


def verify_compiled_artifact(bundle_dir: Path, since: float) -> tuple[bool, str, str]:
    """核对**编译出来的安装包**里有没有守卫宏（不是只看中间文件 installer.nsi）。

    为什么必须看产物：补丁与 makensis 是并发的（Tauri 生成完模板就立刻编译）。
    如果 makensis 已经读过旧内容，installer.nsi 事后会是"已打补丁"的样子，
    但产物里没有守卫 —— 只看中间文件就是假绿。
    NSIS 是 Unicode 构建，字符串以 UTF-16LE 存在，所以按 UTF-16LE 搜。
    """
    if not bundle_dir.is_dir():
        return False, "", "产物目录不存在：%s" % bundle_dir
    candidates = [p for p in bundle_dir.glob("QIO_*_x64-setup*.exe") if p.stat().st_mtime >= since - 1]
    if not candidates:
        return False, "", "本次构建没有产出新的安装包（%s）" % bundle_dir
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    needle = GUARD_MARKER.encode("utf-16-le")
    with newest.open("rb") as fh:
        data = fh.read()
    if needle in data:
        return True, str(newest), "产物里找到守卫标记 %r（UTF-16LE）" % GUARD_MARKER
    return False, str(newest), "产物里**没有**守卫标记 %r：补丁没赶上 makensis 读文件" % GUARD_MARKER


def patch_once(nsis_dir: Path, guard: Path) -> dict:
    stage_guard(nsis_dir, guard)
    report = nsis_dir / "qio-patch-report.json"
    cmd = [sys.executable, str(PATCH), "--nsis-dir", str(nsis_dir), "--guard", str(guard),
           "--report", str(report)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    try:
        data = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    except Exception:
        data = {}
    return {"exit": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-1000:],
            "report": data}


def watch_and_patch(nsis_dir: Path, guard: Path, stop: threading.Event, state: dict,
                    timeout_s: float = 600.0) -> None:
    """盯着 installer.nsi，一出现就**在进程内**打补丁（原子替换）。

    为什么必须进程内、且不能等"稳定"：Tauri 写完 installer.nsi 会**立刻**起 makensis。
    实测两个坑：
      * 起子进程跑 patch_nsis_template.py 要 ~100ms，太慢（makensis 已经开始读了）；
      * 等"大小连续两次不变"再多花一轮轮询，同样太慢。
    所以这里：每 0.5ms 探一次 → 一出现就在本进程里 patch_text + os.replace。
    锚点没找齐（文件只写了一半）不当作致命错误，下一轮再试。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import patch_nsis_template as patcher  # noqa: PLC0415

    # 守卫宏必须先就位：installer.nsi 里的 !include "qio-ownership.nsh" 是相对路径，
    # 找不到就直接 abort（实测：漏了这一步 → `!include: could not find: "qio-ownership.nsh"`）。
    staged = False
    while not staged and not stop.is_set():
        try:
            stage_guard(nsis_dir, guard)
            staged = True
        except OSError:
            time.sleep(0.01)

    target = nsis_dir / "installer.nsi"
    deadline = time.time() + timeout_s
    while time.time() < deadline and not stop.is_set():
        if target.is_file():
            try:
                size = target.stat().st_size
            except OSError:
                size = -1
            if size > 0:
                t0 = time.time()
                try:
                    with target.open("r", encoding="utf-8", newline="") as fh:
                        text = fh.read()
                    patched, status = patcher.patch_text(text)
                except (OSError, ValueError) as exc:
                    # 文件可能只写了一半：不致命，下一轮再来
                    state["last_retry"] = repr(exc)
                    time.sleep(0.0005)
                    continue
                if status == "patched":
                    # 守卫宏必须在**这一刻**再放一次：Tauri 打包时会重建 nsis 目录，
                    # 启动时提前放的会被它清掉（实测 → `!include: could not find`）。
                    try:
                        stage_guard(nsis_dir, guard)
                    except OSError as exc:
                        state["failed"] = True
                        state["stage_error"] = repr(exc)
                        stop.set()
                        return
                    if "\r\n" in text and "\r\n" not in patched:
                        patched = patched.replace("\n", "\r\n")
                    tmp = target.with_name(target.name + ".qio-patch.tmp")
                    with tmp.open("w", encoding="utf-8", newline="") as fh:
                        fh.write(patched)
                    replaced = False
                    for _ in range(400):  # 最多等 ~2s：makensis 可能正握着这个文件
                        try:
                            os.replace(tmp, target)
                            replaced = True
                            break
                        except OSError:
                            time.sleep(0.005)
                    if not replaced:
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
                    state["patch"] = {
                        "status": status,
                        "replaced": replaced,
                        "elapsed_ms": round((time.time() - t0) * 1000, 2),
                    }
                    log("已打补丁：status=%s 原子替换=%s（%.2fms）" % (status, replaced, state["patch"]["elapsed_ms"]))
                    state["patched_at"] = time.time()
                    return
                # already-patched：模板目录里是上一轮的产物，等 Tauri 覆盖它
        time.sleep(0.0005)


def main() -> int:
    _configure_output()
    ap = argparse.ArgumentParser()
    ap.add_argument("--nsis-dir", default=str(ROOT / "frontend" / "src-tauri" / "target" / "release" / "nsis" / "x64"))
    ap.add_argument("--guard", default=str(ROOT / "frontend" / "src-tauri" / "nsis" / "qio-ownership.nsh"))
    ap.add_argument(
        "--bundle-dir",
        default=str(ROOT / "frontend" / "src-tauri" / "target" / "release" / "bundle" / "nsis"),
        help="安装包输出目录（核对编译产物里真的有守卫）",
    )
    ap.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="最多构建几次（补丁没赶上 makensis 读文件时重跑；默认 3）",
    )
    ap.add_argument("--timeout", type=float, default=900.0, help="等 installer.nsi 出现的上限（秒）")
    ap.add_argument("--check", action="store_true", help="只做静态检查（不跑构建）")
    ap.add_argument(
        "--no-patch",
        action="store_true",
        help="只跑构建、**不打补丁**。仅用于复现取证（造一份改动前的产物），绝不要用它出正式包",
    )
    ap.add_argument("command", nargs=argparse.REMAINDER, help="要包起来的构建命令")
    args = ap.parse_args()

    nsis_dir = Path(args.nsis_dir)
    guard = Path(args.guard)
    bundle_dir = Path(args.bundle_dir)
    state: dict = {}

    if args.check:
        report = patch_once(nsis_dir, guard) if (nsis_dir / "installer.nsi").is_file() else {
            "exit": 1, "report": {"detail": "还没有 installer.nsi（先跑一次构建）"}}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["exit"] == 0 else 1

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("用法：python scripts/build_nsis_with_patch.py -- <构建命令…>", file=sys.stderr)
        return 2

    # 先删掉上一轮的模板与报告：否则 watcher 会对着旧文件打补丁（幂等，但会误判"已打"）
    for stale in (nsis_dir / "installer.nsi", nsis_dir / "qio-patch-report.json"):
        try:
            stale.unlink()
        except OSError:
            pass

    # Windows 上 npm 是 npm.cmd：CreateProcess 直接执行 "npm" 会 WinError 2（实测）。
    # 用 which 解析成真实可执行文件，找不到就原样传（让错误信息保留原命令名）。
    exe = shutil.which(command[0]) or command[0]

    attempts = 1 if args.no_patch else max(1, args.attempts)
    last: dict = {}
    for attempt in range(1, attempts + 1):
        state.clear()
        for stale in (nsis_dir / "installer.nsi", nsis_dir / "qio-patch-report.json"):
            try:
                stale.unlink()
            except OSError:
                pass
        stop = threading.Event()
        watcher = None
        if not args.no_patch:
            watcher = threading.Thread(
                target=watch_and_patch, args=(nsis_dir, guard, stop, state, args.timeout), daemon=True
            )
            watcher.start()
        else:
            log("--no-patch：只跑构建，不打补丁（复现取证用；产物不是可发布/可验收的包）")
        log("开始构建（第 %d/%d 次）：%s" % (attempt, attempts, " ".join([exe] + command[1:])))
        started = time.time()
        proc = subprocess.Popen([exe] + command[1:], cwd=str(ROOT / "frontend"))
        code = proc.wait()
        stop.set()
        if watcher is not None:
            watcher.join(timeout=5)
        elapsed = round(time.time() - started, 1)

        if state.get("failed"):
            log("构建被中止：NSIS 模板补丁没打上（拒绝产出没有所有权守卫的安装包）")
            return 3

        text = ""
        if (nsis_dir / "installer.nsi").is_file():
            text = (nsis_dir / "installer.nsi").read_text(encoding="utf-8", errors="replace")
        # 判据就是 build_exit：hook 里的 !ifndef QIO_OWNERSHIP_PATCHED 让"没打补丁"变成
        # **编译期错误**（makensis 在 line 31 直接 abort），所以构建成功 = 补丁一定生效。
        # 事后在产物里找字符串是不行的：NSIS 用 LZMA 压整包，字符串搜不到（实测假阴性）。
        artifact_ok, artifact_path, artifact_detail = (code == 0), "", "构建退出码 %s（0 = 补丁已在编译期生效）" % code
        last = {
            "attempt": attempt,
            "build_exit": code,
            "build_elapsed_s": elapsed,
            "patch": state.get("patch"),
            "patched_before_build_finished": bool(state.get("patched_at")),
            "installer_nsi_has_guard_call": "!insertmacro QIO_CloseMainExeIfOwned" in text,
            "installer_nsi_has_guard_include": '!include "qio-ownership.nsh"' in text,
            "installer_nsi_has_guard_define": "!define QIO_OWNERSHIP_PATCHED" in text,
            "compiled_artifact": artifact_path,
            "compiled_artifact_has_guard": artifact_ok,
            "compiled_artifact_detail": artifact_detail,
        }
        print(json.dumps(last, ensure_ascii=False, indent=2))

        if args.no_patch:
            log("--no-patch：已跳过补丁核对（这是复现取证产物，不要当验收产物）")
            return code

        # 判据是**编译产物**里真的有守卫（installer.nsi 只是中间文件：补丁与 makensis 并发，
        # 打晚了 makensis 已经读过旧内容 —— 只看中间文件会假绿）。
        if code == 0 and artifact_ok:
            log("完成：守卫已进编译产物（%s），构建 exit=0" % artifact_path)
            return 0
        if attempt < attempts:
            log("第 %d 次没达标（build_exit=%s）：重跑一次（补丁没赶上 makensis 读文件时，"
                "hook 的 !ifndef 门禁会让编译直接失败）" % (attempt, code))
            continue
        log("FAIL：构建没成功（exit=%s）—— 拒绝把它当验收产物" % code)
        return 4
    return 4


if __name__ == "__main__":
    sys.exit(main())
