#!/usr/bin/env python3
"""定向验证入口的 SHA 核验（唯一实现，workflow 与本机共用）。

为什么单独抽出来：workflow 里内联一段 bash 做「输入 SHA == 实际 checkout SHA」，
本机无法单测，改错了也没人知道。把判定逻辑放在这里，单测直接打在这上面；
workflow 只是它的调用方，CI 上跑的是同一份代码。

用法（CI / 本机一致）：

    python scripts/targeted-verify/verify_sha.py \
        --requested "$REQUESTED_SHA" --actual "$ACTUAL_SHA" \
        --label "docs" --json-out verify-sha-docs.json

退出码：0 = 一致（并打印证据）；1 = 不一致 / 拿不到实际 SHA（并说明差异）。

环境变量（无参数时的兜底，方便本地与 CI 用同一命令行）：
    GITHUB_SHA        作为 --actual 的默认值（GitHub 上就是本次运行解析出的提交）
    GITHUB_REF_NAME   仅用于打印，不参与判定
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
SHORT_SHA_RE = re.compile(r"\A[0-9a-f]{7,39}\Z")

EXIT_OK = 0
EXIT_MISMATCH = 1


class ShaCheckError(RuntimeError):
    """参数本身不合法（不是「SHA 不一致」，调用方应把它当配置错误）。"""


def normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def classify(value: str) -> str:
    """把输入分成 full / short / invalid —— 三类处理方式完全不同。"""
    text = normalize(value)
    if FULL_SHA_RE.match(text):
        return "full"
    if SHORT_SHA_RE.match(text):
        return "short"
    return "invalid"


def read_head_sha(repo: Path, ref: str = "HEAD") -> str:
    """读工作区实际提交。失败时抛 ShaCheckError，让调用方明确失败而不是猜。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # git 不在 PATH
        raise ShaCheckError(f"无法执行 git：{exc}") from exc
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip().splitlines()
        hint = detail[0] if detail else "no output"
        raise ShaCheckError(f"git rev-parse {ref} 失败（{hint}）")
    return normalize(out.stdout)


def read_ref_sha(repo: Path, ref: str) -> str:
    """把分支/标签解析成提交 SHA。用于证明「输入 ref 解析到的就是输入 SHA」。"""
    return read_head_sha(repo, ref)


def check_sha(
    requested: str,
    actual: str,
    *,
    expected_ref: str | None = None,
    ref_sha: str | None = None,
    label: str = "verify",
    allow_short: bool = False,
) -> tuple[bool, list[str]]:
    """纯判定：返回 (是否通过, 证据行列表)。不做 IO，方便单测覆盖所有分支。"""
    lines: list[str] = []
    requested_n = normalize(requested)
    actual_n = normalize(actual)
    kind = classify(requested_n)

    lines.append(f"[{label}] requested sha : {requested_n or '(empty)'}")
    lines.append(f"[{label}] actual    sha : {actual_n or '(empty)'}")

    if kind == "invalid":
        lines.append(
            f"[{label}] FAIL 输入 sha 不是 7-40 位十六进制（收到 {requested!r}）——"
            "workflow_dispatch 的 sha 输入必须是完整提交 SHA"
        )
        return False, lines
    if kind == "short" and not allow_short:
        lines.append(
            f"[{label}] FAIL 输入 sha 只有 {len(requested_n)} 位，是缩写：缩写无法证明"
            "「跑的就是那个提交」（同名缩写可能指向别的提交）。请传完整 40 位 SHA。"
        )
        return False, lines
    if classify(actual_n) != "full":
        lines.append(
            f"[{label}] FAIL 拿不到实际 checkout 的完整 SHA（{actual_n or '(empty)'}）——"
            "不允许在「不知道跑的是哪个提交」的情况下继续"
        )
        return False, lines

    # 完整 SHA 用等值比较；允许缩写时用前缀比较（缩写本来就是前缀）。
    matched = actual_n.startswith(requested_n) if kind == "short" else requested_n == actual_n
    if not matched:
        lines.append(
            f"[{label}] FAIL SHA 不一致：请求 {requested_n}，实际 checkout {actual_n}"
        )
        lines.append(
            f"[{label}] 说明：定向验证必须跑在指定提交上。旧 CI 的 Re-run failed jobs "
            "重跑的是旧提交，所以这里显式核对，不一致就直接失败而不是继续。"
        )
        return False, lines

    if expected_ref:
        lines.append(f"[{label}] requested ref : {expected_ref}")
    if ref_sha is not None:
        ref_sha_n = normalize(ref_sha)
        lines.append(f"[{label}] ref     resolved: {ref_sha_n or '(empty)'}")
        if ref_sha_n != actual_n:
            lines.append(
                f"[{label}] FAIL ref {expected_ref!r} 解析到 {ref_sha_n}，"
                f"与实际 checkout {actual_n} 不一致"
            )
            return False, lines

    lines.append(f"[{label}] PASS SHA 核验通过：{actual_n}")
    return True, lines


def _print(lines: list[str]) -> None:
    for line in lines:
        print(line, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="定向验证入口的 SHA 核验")
    parser.add_argument("--requested", required=True, help="调用方要求验证的提交 SHA")
    parser.add_argument(
        "--actual",
        default=None,
        help="实际 checkout 的提交 SHA；省略时取 git rev-parse HEAD（不是 GITHUB_SHA）",
    )
    parser.add_argument(
        "--actual-from",
        choices=("git-head", "arg"),
        default="git-head",
        help="实际 SHA 的来源：git-head = 读工作区 HEAD（默认，唯一可信来源）；arg = 用 --actual 的值",
    )
    parser.add_argument("--expected-ref", default=None, help="调用方要求验证的 ref（分支/标签）")
    parser.add_argument(
        "--assert-github-sha",
        action="store_true",
        help="额外断言 GITHUB_SHA（事件提交）与实际 checkout 一致：PR 上这两个是不同的东西",
    )
    parser.add_argument("--repo", default=".", help="git 仓库路径（默认当前目录）")
    parser.add_argument("--label", default="targeted-verify", help="证据行的前缀标签")
    parser.add_argument("--json-out", default=None, help="把核验结果写成 JSON（台账证据）")
    parser.add_argument(
        "--allow-short",
        action="store_true",
        help="允许缩写 SHA（默认拒绝：缩写证明不了跑的是哪个提交）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo)

    # 实际提交只认 git HEAD：GITHUB_SHA 是「触发事件的提交」，PR 上是 merge commit，
    # 与 actions/checkout 真正落到工作区的提交可以不同 —— 拿它当「实际 SHA」会假绿。
    head_sha: str | None = None
    ref_sha: str | None = None
    problems: list[str] = []
    try:
        head_sha = read_head_sha(repo)
    except ShaCheckError as exc:
        problems.append(str(exc))

    if args.actual_from == "arg" and args.actual:
        actual = args.actual
        source = "--actual (arg)"
    elif args.actual_from == "arg" and not args.actual:
        actual = ""
        source = "--actual (arg, empty)"
    elif head_sha is not None:
        actual = head_sha
        source = "git rev-parse HEAD"
    else:
        actual = ""
        source = "(unavailable)"

    if not problems and args.expected_ref:
        try:
            ref_sha = read_ref_sha(repo, args.expected_ref)
        except ShaCheckError as exc:
            problems.append(str(exc))

    passed, lines = check_sha(
        args.requested,
        actual,
        expected_ref=args.expected_ref,
        ref_sha=ref_sha,
        label=args.label,
        allow_short=args.allow_short,
    )
    lines.insert(1, f"[{args.label}] actual  from : {source}")
    if head_sha is not None:
        lines.append(f"[{args.label}] git HEAD      : {head_sha}")

    github_sha = normalize(os.environ.get("GITHUB_SHA"))
    github_ref = os.environ.get("GITHUB_REF") or ""
    github_event = os.environ.get("GITHUB_EVENT_NAME") or ""
    if github_event or github_ref:
        lines.append(
            f"[{args.label}] github event  : {github_event or '(unset)'} {github_ref or '(unset)'}"
        )
    if github_sha:
        lines.append(f"[{args.label}] GITHUB_SHA    : {github_sha}（事件提交，PR 上是 merge commit）")
        if args.assert_github_sha and github_sha != normalize(actual):
            passed = False
            lines.append(
                f"[{args.label}] FAIL GITHUB_SHA {github_sha} 与实际 checkout {normalize(actual)} 不一致"
                "（--assert-github-sha 打开时必须一致）"
            )

    if problems:
        passed = False
        for problem in problems:
            lines.append(f"[{args.label}] FAIL 无法取得 git 证据：{problem}")

    # repo 里的 HEAD 与调用方给的 --actual 不一致 => 那个 actual 不是这个工作区的提交
    if passed and args.actual and head_sha is not None and head_sha != normalize(actual):
        passed = False
        lines.append(
            f"[{args.label}] FAIL --actual 给的 {normalize(actual)} 与仓库 HEAD {head_sha} 不一致"
        )

    if args.json_out:
        payload = {
            "label": args.label,
            "requestedSha": normalize(args.requested),
            "actualSha": normalize(actual),
            "actualSource": source,
            "actualFrom": args.actual_from,
            "expectedRef": args.expected_ref,
            "refSha": normalize(ref_sha) if ref_sha else None,
            "gitHead": head_sha,
            "githubSha": github_sha or None,
            "githubRef": github_ref or None,
            "githubEventName": github_event or None,
            "assertGithubSha": bool(args.assert_github_sha),
            "allowShort": bool(args.allow_short),
            "passed": bool(passed),
            "evidence": lines,
        }
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines.append(f"[{args.label}] 证据已写入 {args.json_out}")

    _print(lines)
    if not passed:
        print(f"[{args.label}] ::error title=targeted verify SHA::SHA 核验失败，已停止后续检查")
        return EXIT_MISMATCH
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
