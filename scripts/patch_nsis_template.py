#!/usr/bin/env python
"""构建期补丁：把 Tauri 生成模板里**那一行按可执行文件名**的主程序检查，换成所有权守卫。

为什么需要它（2026-10-03 逐行核对 + 实测）
----------------------------------------
Tauri 生成的 `target/release/nsis/x64/installer.nsi` 卸载段里有：

    !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"

而 `utils.nsh` 里这个宏用的是 `FindProcessCurrentUser` / `KillProcessCurrentUser` ——
**只按可执行文件名**判定与结束。静默卸载直接杀、交互卸载点确认也杀，杀的是当前用户
**所有** qio.exe（同一个模板还用它查主程序 qio.exe）。被杀的壳持有 Job Object
（KILL_ON_JOB_CLOSE），它一死 job 关闭 → 它那份安装的 backend 进程树一起死。
于是「卸载 A 误杀 B 的 sidecar」有一条绕过 sidecar 所有权的路径：**杀壳 → 关 job → 杀树**。

Tauri 的 installerHooks 只能追加宏、不能替换模板里已有的语句，所以这里做一次
**机械、可验证、幂等**的补丁：只替换卸载段那一处，换成
`!insertmacro QIO_CloseMainExeIfOwned`（定义在 frontend/src-tauri/nsis/qio-ownership.nsh，
构建期由本脚本的调用方复制到模板目录，并由 installer.nsi !include 进来）。

为什么不是 fork 模板：模板 900+ 行、由 Tauri 生成，fork 一份等于把 Tauri 的每次升级都
变成人工合并。机械补丁只依赖两个锚点（卸载段注释 + 那一行本身），锚点变了就**失败**，
不会静默失效。

用法
----
    python scripts/patch_nsis_template.py --nsis-dir <含 installer.nsi 的目录> [--check] [--report <json>]

    --check   只报告会不会打补丁、当前状态，不写盘（CI 的静态闸门用）
    --report  把结果写成 JSON（构建 manifest / 发布闸门核对用）

退出码：0 = 成功（已打 / 已经打过 / --check 下状态已知）；1 = 失败（锚点缺失或不唯一、
守卫宏缺失）。**失败必须让构建停下来** —— 静默失效等于这条漏洞又回来了。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

UNINSTALL_ANCHOR = "  ; Delete the app directory and its content from disk\n"
INSTALL_CHECK = '  !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"\n'
UTILS_INCLUDE = '!include "utils.nsh"\n'
GUARD_INCLUDE = '!include "qio-ownership.nsh"\n'
# 这个 define 是**编译期门禁**：installer-hooks.nsh 里 !ifndef QIO_OWNERSHIP_PATCHED 就 !error。
# 于是「补丁有没有进产物」变成编译期事实 —— 构建成功 = 补丁一定生效。
# （不要事后去产物里找字符串：NSIS 用 LZMA 压整包，字符串搜不到，会得到假阴性。）
GUARD_DEFINE = "!define QIO_OWNERSHIP_PATCHED 1\n"
GUARD_CALL = "  !insertmacro QIO_CloseMainExeIfOwned\n"
GUARD_MACRO = "!macro QIO_CloseMainExeIfOwned"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def patch_text(text: str) -> tuple[str, str]:
    """返回 (新文本, 状态)。状态 = already-patched | patched。失败抛 ValueError。

    行尾说明：Tauri 生成的 installer.nsi 是 CRLF，而本文件里的锚点按 LF 写。
    先把 CRLF 归一成 LF 再匹配，最后按原文的行尾还原（不改变文件的其它部分）。
    """
    if "\r\n" in text:
        text = text.replace("\r\n", "\n")
    if GUARD_CALL in text:
        return text, "already-patched"

    total = text.count(INSTALL_CHECK)
    if total != 2:
        raise ValueError(
            "期望模板里恰好出现 2 处主程序检查（安装段 + 卸载段），实际 %d 处："
            "Tauri 模板结构可能变了，补丁拒绝静默失效。" % total
        )
    if text.count(UNINSTALL_ANCHOR) != 1:
        raise ValueError(
            "卸载段锚点 %r 出现 %d 次，期望 1 次：模板结构可能变了。"
            % (UNINSTALL_ANCHOR.strip(), text.count(UNINSTALL_ANCHOR))
        )

    # 两处检查的顺序是固定的：安装段在前、卸载段在后（卸载段那一处紧跟在
    # NSIS_HOOK_PREUNINSTALL 之后、"Delete the app directory" 注释之前）。
    # 锚点（"Delete the app directory"）在卸载段检查的**后面**，所以这里取最后一处检查，
    # 并用锚点做"确实在卸载段里"的交叉验证。
    anchor_at = text.index(UNINSTALL_ANCHOR)
    first_check = text.index(INSTALL_CHECK)
    check_at = text.rindex(INSTALL_CHECK)
    if first_check == check_at:
        raise ValueError("两处主程序检查是同一处：模板结构与预期不符。")
    if check_at > anchor_at:
        raise ValueError("卸载段的检查出现在锚点之后：模板结构与预期不符。")

    patched = text[:check_at] + GUARD_CALL + text[check_at + len(INSTALL_CHECK):]
    if patched.count(GUARD_CALL) != 1 or patched.count(INSTALL_CHECK) != 1:
        raise ValueError("补丁后计数不对（守卫调用 %d / 主程序检查 %d）：拒绝。"
                         % (patched.count(GUARD_CALL), patched.count(INSTALL_CHECK)))
    if GUARD_INCLUDE not in patched or GUARD_DEFINE not in patched:
        if patched.count(UTILS_INCLUDE) != 1:
            raise ValueError('模板里找不到唯一的 !include "utils.nsh"，无法插入守卫宏的 include。')
        patched = patched.replace(UTILS_INCLUDE, UTILS_INCLUDE + GUARD_INCLUDE + GUARD_DEFINE, 1)
    return patched, "patched"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NSIS 模板所有权补丁")
    parser.add_argument("--nsis-dir", required=True, help="含 installer.nsi 的目录（生成模板所在处）")
    parser.add_argument("--guard", default="", help="守卫宏文件（默认 frontend/src-tauri/nsis/qio-ownership.nsh）")
    parser.add_argument("--check", action="store_true", help="只检查，不写盘")
    parser.add_argument("--report", default="", help="把结果写成 JSON")
    args = parser.parse_args(argv)

    nsis_dir = Path(args.nsis_dir)
    installer = nsis_dir / "installer.nsi"
    guard = Path(args.guard) if args.guard else (
        Path(__file__).resolve().parent.parent / "frontend" / "src-tauri" / "nsis" / "qio-ownership.nsh"
    )

    result: dict = {"installer": str(installer), "guard": str(guard), "check_only": bool(args.check)}

    def finish(code: int, status: str, detail: str = "") -> int:
        result["status"] = status
        result["ok"] = code == 0
        if detail:
            result["detail"] = detail
        if args.report:
            Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code

    if not installer.is_file():
        return finish(1, "missing-installer",
                      "找不到 %s（构建期补丁必须在 Tauri 生成模板之后跑）" % installer)
    if not guard.is_file():
        return finish(1, "missing-guard", "找不到守卫宏文件 %s" % guard)
    guard_text = guard.read_text(encoding="utf-8")
    if GUARD_MACRO not in guard_text:
        return finish(1, "guard-invalid", "%s 里没有 %s 的定义" % (guard, GUARD_MACRO))

    # newline="" ：保留文件自己的行尾（Tauri 生成的是 CRLF，patch_text 内部归一化后再还原）
    with installer.open("r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    # 行尾归一化后的副本：所有字符串判定都用它（Tauri 生成的是 CRLF，锚点按 LF 写）
    norm = text.replace("\r\n", "\n")
    result["sha256_before"] = sha256_text(text)
    result["had_install_check"] = INSTALL_CHECK in norm
    result["had_guard_include"] = GUARD_INCLUDE in norm
    result["had_guard_define"] = GUARD_DEFINE in norm
    result["had_guard_call"] = GUARD_CALL in norm

    try:
        patched, status = patch_text(text)
    except ValueError as exc:
        return finish(1, "anchor-mismatch", str(exc))

    result["sha256_after"] = sha256_text(patched)
    result["changed"] = patched != text

    if status == "already-patched":
        # 守卫宏住在同目录的 qio-ownership.nsh 里（模板用 !include 引入），不在 installer.nsi 正文里。
        # 所以这里核对的是"include 在 + 那个文件在 + 里面有宏定义"。
        if GUARD_INCLUDE not in norm or GUARD_DEFINE not in norm:
            return finish(1, "guard-include-missing",
                          "模板调用了守卫宏但没有 !include \"qio-ownership.nsh\"：构建会以 macro not found 失败")
        staged = nsis_dir / "qio-ownership.nsh"
        if not staged.is_file():
            return finish(1, "guard-not-staged",
                          "守卫宏 %s 不在模板目录 %s —— !include 会失败" % (staged.name, nsis_dir))
        if GUARD_MACRO not in staged.read_text(encoding="utf-8"):
            return finish(1, "guard-invalid-staged",
                          "%s 里没有 %s 的定义" % (staged, GUARD_MACRO))
        return finish(0, status, "模板已经打过补丁（幂等）")

    if args.check:
        return finish(0, "would-patch", "检查模式：补丁可以打，但未写盘")

    # patch_text 内部把 CRLF 归一成 LF 再匹配；写回前按原文行尾还原（Tauri 生成的是 CRLF）。
    if "\r\n" in text and "\r\n" not in patched:
        patched = patched.replace("\n", "\r\n")
    # **原子替换**：makensis 与补丁是并发的（Tauri 生成完模板就立刻编译），
    # 就地 open(w)+write 会让 makensis 读到写了一半的文件 —— 实测报
    # `Invalid command: "!either"`（正是注释里 "either" 那个词被撕开）。
    # 先写同目录临时文件，再 os.replace 原子换名：makensis 要么看到旧的完整文件，
    # 要么看到新的完整文件，绝不会看到撕裂的中间态。
    tmp = installer.with_name(installer.name + ".qio-patch.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        fh.write(patched)
    last_error = None
    for _ in range(200):  # 最多等 ~2s：makensis 可能正握着这个文件
        try:
            os.replace(tmp, installer)
            last_error = None
            break
        except OSError as exc:  # 共享冲突：等一下再试
            last_error = exc
            time.sleep(0.01)
    if last_error is not None:
        try:
            tmp.unlink()
        except OSError:
            pass
        return finish(1, "replace-failed", "原子替换 installer.nsi 失败：%s" % last_error)
    if not (nsis_dir / "qio-ownership.nsh").is_file():
        return finish(1, "guard-not-staged",
                      "守卫宏 %s 没有出现在模板目录 %s —— !include 会失败（构建期必须先复制）"
                      % (guard.name, nsis_dir))
    result["note"] = "守卫宏由 installer.nsi 的 !include \"qio-ownership.nsh\" 带入（同目录副本）"
    return finish(0, status)


if __name__ == "__main__":
    sys.exit(main())
