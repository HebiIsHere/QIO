from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:
    print(f"::error title=workflow contract::PyYAML missing: {exc}")
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
CI_FILE = WORKFLOW_DIR / "ci.yml"
TARGETED_FILE = WORKFLOW_DIR / "targeted-verify.yml"

# 与 targeted-verify.yml 共用同一份 SHA 判定实现：ci.yml 的 workflow_dispatch 身份校验调用
# 的就是 verify_sha.py。这里直接 import 它的纯判定函数，「不一致会红」由同一份代码证明，
# 而不是靠 workflow 文本里出现了某个字符串。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_sha import check_sha as verify_sha_check  # noqa: E402

DOLLAR = "$"

CI_REQUIRED_JOBS = (
    "docs",
    "backend",
    "backend-windows",
    "frontend",
    "rust",
    "rust-windows",
    "frozen-worker",
    "install-e2e",
)

TARGETED_REQUIRED_CHOICES = (
    "docs",
    "backend-py311",
    "backend-py312",
    "windows",
    "frontend",
    "rust",
    "rust-windows",
    "frozen-worker",
)

TARGETED_REQUIRED_BUILD_COMMANDS = (
    "npx vue-tsc --noEmit",
    "npm run build",
    "cargo check",
    "cargo test",
    "uv sync --frozen --extra dev",
)

# 定向筛选必须同时满足「输入参数存在」与「真的交给测试命令」。这两组标记分开写，
# 免得只断言了输入名就以为筛选生效了。
FILTER_MARKERS = (
    ("pytest_node_ids", "PYTEST_NODE_IDS", "PYTEST_K", "-k"),
    ("vitest_files", "VITEST_FILES", "VITEST_NAME", "-t"),
)
FILTER_INPUT_NAMES = ("pytest_node_ids", "vitest_files", "vitest_name")
FILTER_MARKER_NEEDLES = FILTER_INPUT_NAMES + (
    "PYTEST_NODE_IDS",
    "PYTEST_K",
    "-k",
    "VITEST_FILES",
    "VITEST_NAME",
    "--testNamePattern",
)

# selftest 合成用例的文本：既给 check_targeted 用，也给筛选标记的「同源」元断言用。
# 它必须包含真实 workflow 里的筛选标记，否则合成用例只是自说自话。
_SELFTEST_TARGETED_TEXT = "\n".join(
    [
        "--requested --actual --actual-from --expected-ref --durations node_id vitest ref ::",
        "pytest_node_ids vitest_files vitest_name",
        "PYTEST_NODE_IDS PYTEST_K -k VITEST_FILES VITEST_NAME --testNamePattern",
        "SKIPPED no runnable checks",
        "npx vue-tsc --noEmit",
        "npm run build",
        "cargo check",
        "cargo test",
        "uv sync --frozen --extra dev",
        "actions/checkout@v5",
    ]
)

# selftest 用的合成 composite action 文本（与真实 action.yml 同一组断言）。
_SELFTEST_ACTION_TEXT = "\n".join(
    [
        "ref: " + DOLLAR + "{{ inputs.ref }}",
        "python scripts/targeted-verify/verify_sha.py --requested x --actual x --actual-from arg --expected-ref y",
        'actual="' + DOLLAR + '(git rev-parse HEAD)"',
    ]
)

TARGETED_REQUIRED_INPUTS = (
    "ref",
    "sha",
    "targets",
    "pytest_node_ids",
    "pytest_k",
    "vitest_files",
    "vitest_name",
    "verify_sha_self_test",
)

# ── 2026-10-26 本轮 CI 防重复契约（工作流 D）────────────────────────────────────
# ci.yml 保留 push 全分支 + PR 的完整矩阵门禁，只额外排除本轮工作分支；
# 去重键 = **实际受测提交**（PR 头提交 / push 提交 / dispatch 的 ref 尖端）；
# workflow_dispatch 提供「按完整 SHA 锁定的一次完整矩阵」，checkout 后必须核验 SHA。
CI_PUSH_PATTERNS = (
    "**",
    "!fix/pm-*",
    "!verify/pm-*",
)
# 这些分支必须仍然触发完整矩阵（main 门禁、普通分支门禁、本轮集成分支都不得被排除误伤）。
# 本轮集成分支的这一次 push 就是本轮**唯一一次**完整矩阵（Lead 2026-10-26 裁决）。
CI_PUSH_MUST_MATCH = (
    "main",
    "fix/other-defect",
    "feat/conversation-page-v2",
    "releases/2026-10",
    "fix/post-merge-integration-cleanup-20261025",
)
# 本轮并行子智能体的工作分支：永不 push（本地 merge），排除只防误推时烧掉完整矩阵。
CI_PUSH_MUST_NOT_MATCH = (
    "fix/pm-a-approval-results",
    "fix/pm-b-interactive-mode",
    "fix/pm-c-legacy-cleanup",
    "fix/pm-d-desktop-ci-docs",
    "verify/pm-e-acceptance",
)
CI_TESTED_COMMIT_EXPR = "github.event.pull_request.head.sha || github.sha"
CI_DISPATCH_INPUTS = ("ref", "expected_sha")

# 触发场景表：用 ci.yml 的真实规则计算「什么事件会不会跑完整矩阵、去重键是什么」。
# 每条都带期望值 —— 与 YAML 实际规则不一致就 fail，这张表是断言不是说明文档。
# 字段：event / ref / sha（事件提交）/ head（PR 头提交）/ expected_sha（dispatch 输入）/
#       expect（是否期望起完整矩阵）/ expect_sha_ok（dispatch 身份校验期望结果，None=不涉及）/ note
CI_SCENARIOS = (
    {"event": "push", "ref": "refs/heads/main", "sha": "a" * 40, "head": None, "expected_sha": None,
     "expect": True, "expect_sha_ok": None, "note": "main 门禁：必须跑"},
    {"event": "push", "ref": "refs/heads/fix/other-defect", "sha": "b" * 40, "head": None, "expected_sha": None,
     "expect": True, "expect_sha_ok": None, "note": "普通分支门禁：不受本轮排除影响"},
    {"event": "push", "ref": "refs/heads/fix/pm-a-approval-results", "sha": "1" * 40, "head": None, "expected_sha": None,
     "expect": False, "expect_sha_ok": None, "note": "本轮工作分支：不自动跑，改用 targeted-verify"},
    {"event": "push", "ref": "refs/heads/verify/pm-e-acceptance", "sha": "2" * 40, "head": None, "expected_sha": None,
     "expect": False, "expect_sha_ok": None, "note": "本轮验收分支：不自动跑"},
    {"event": "push", "ref": "refs/heads/fix/post-merge-integration-cleanup-20261025", "sha": "3" * 40, "head": None,
     "expected_sha": None, "expect": True, "expect_sha_ok": None,
     "note": "本轮集成分支 push：本轮唯一一次完整矩阵"},
    {"event": "push", "ref": "refs/tags/integ-candidate", "sha": "4" * 40, "head": None, "expected_sha": None,
     "expect": False, "expect_sha_ok": None, "note": "tag push 不匹配 branches 过滤（可给 dispatch 当 ref）"},
    {"event": "pull_request", "ref": "refs/pull/42/merge", "sha": "c" * 40, "head": "a" * 40, "expected_sha": None,
     "expect": True, "expect_sha_ok": None, "note": "PR 门禁：必须跑；键 = PR 头提交（与同提交 push 同组）"},
    {"event": "pull_request", "ref": "refs/pull/43/merge", "sha": "d" * 40, "head": "b" * 40, "expected_sha": None,
     "expect": True, "expect_sha_ok": None, "note": "PR 门禁：与普通分支 push 同一 head 提交时同组"},
    {"event": "pull_request", "ref": "refs/pull/44/merge", "sha": "e" * 40, "head": "f" * 40, "expected_sha": None,
     "expect": True, "expect_sha_ok": None, "note": "本轮工作分支开 PR 仍跑完整矩阵（PR 门禁不被 push 排除影响）"},
    {"event": "workflow_dispatch", "ref": "refs/heads/fix/post-merge-integration-cleanup-20261025",
     "sha": "a" * 40, "head": None, "expected_sha": "a" * 40, "expect": True, "expect_sha_ok": True,
     "note": "dispatch：锁 expected_sha；但需默认分支已有触发器，本轮不依赖"},
    {"event": "workflow_dispatch", "ref": "refs/tags/integ-candidate", "sha": "b" * 40, "head": None,
     "expected_sha": "5" * 40, "expect": True, "expect_sha_ok": False,
     "note": "dispatch：expected_sha 与 ref 尖端不一致 → preflight 失败、run 红（同上，本轮不依赖）"},
)


class WorkflowLoader(yaml.SafeLoader):
    pass


def _ignore_unknown(loader, _suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


WorkflowLoader.add_multi_constructor("!", _ignore_unknown)


class Report:
    def __init__(self, title, verbose=False):
        self.title = title
        self.verbose = verbose
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append((name, bool(ok), detail))
        if self.verbose and not ok:
            print("    [FAIL] " + name + ("  -- " + detail if detail else ""))
        return bool(ok)

    def check_in(self, name, needle, haystack):
        ok = needle in haystack
        return self.check(name, ok, "" if ok else "missing " + repr(needle))

    def failed(self):
        return [row for row in self.rows if not row[1]]

    def render(self):
        width = max((len(row[0]) for row in self.rows), default=10)
        print("== " + self.title + " ==")
        for name, ok, detail in self.rows:
            mark = "PASS" if ok else "FAIL"
            line = "  [" + mark + "] " + name.ljust(width)
            if detail:
                line += "  -- " + detail
            print(line)
        bad = self.failed()
        if bad:
            print("::" + self.title + "::ERROR " + str(len(bad)) + " assertions failed: " + "; ".join(r[0] for r in bad))
            return 1
        print("  -> " + str(len(self.rows)) + " assertions passed")
        return 0


def load_workflow(path):
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=WorkflowLoader)
    if not isinstance(data, dict):
        raise SystemExit("::error title=workflow parse::not a mapping: " + str(path))
    return data


def workflow_text(workflow):
    return yaml.safe_dump(workflow, allow_unicode=True, sort_keys=False)


def github_branch_matches(pattern, branch):
    """按 GitHub 分支过滤语义匹配（本项目只用 * 与 **；? 由契约断言拒绝，避免假匹配）。"""
    import re as _re

    out = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        else:
            out.append(_re.escape(pattern[index]))
            index += 1
    return _re.fullmatch("".join(out), branch) is not None


def push_branch_triggered(patterns, branch):
    """GitHub push.branches 求值：按顺序匹配，后面的负向模式覆盖前面的正向匹配。"""
    matched = False
    for raw in patterns or ():
        pattern = str(raw)
        negative = pattern.startswith("!")
        body = pattern[1:] if negative else pattern
        if github_branch_matches(body, branch):
            matched = not negative
    return matched


def evaluated_dedup_key(group, *, sha, head_sha=None, workflow="CI"):
    """把 ci.yml 的 concurrency 表达式在给定事件上下文下求值（只覆盖本仓库实际用到的引用）。"""
    tested = head_sha or sha
    text = str(group)
    text = text.replace(DOLLAR + "{{ github.workflow }}", workflow)
    text = text.replace(DOLLAR + "{{ " + CI_TESTED_COMMIT_EXPR + " }}", tested)
    text = text.replace(DOLLAR + "{{ github.sha }}", sha)
    text = text.replace(DOLLAR + "{{ github.event.pull_request.head.sha }}", head_sha or "")
    return text


def build_scenario_rows(workflow):
    """用 workflow 实际规则计算触发场景表（含每条是否与期望一致）。"""
    on = parse_on(workflow)
    push = on.get("push") or {}
    patterns = push.get("branches") if isinstance(push, dict) else None
    concurrency = workflow.get("concurrency") or {}
    group = str(concurrency.get("group", ""))
    rows = []
    for spec in CI_SCENARIOS:
        event = spec["event"]
        ref = spec["ref"]
        sha = spec["sha"]
        head = spec.get("head")
        runs = False
        sha_ok = None
        tested = sha
        if event == "push":
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None
            runs = bool(branch) and push_branch_triggered(patterns, branch)
        elif event == "pull_request":
            runs = "pull_request" in on
            tested = head or sha
        elif event == "workflow_dispatch":
            runs = "workflow_dispatch" in on
            expected = spec.get("expected_sha")
            # dispatch 的 run 一定被创建；身份校验在 preflight 里比较 ref 尖端（= github.sha）
            # 与 expected_sha，不一致整个 run 红。
            sha_ok = None if expected is None else (expected == sha)
        key = evaluated_dedup_key(group, sha=sha, head_sha=head)
        runs_ok = runs is spec["expect"]
        sha_expect = spec.get("expect_sha_ok")
        sha_ok_ok = True if sha_expect is None else (sha_ok is sha_expect)
        rows.append(
            {
                "event": event,
                "ref": ref,
                "tested": tested,
                "runs": runs,
                "expect": spec["expect"],
                "runs_ok": runs_ok,
                "sha_ok": sha_ok,
                "sha_ok_ok": sha_ok_ok,
                "key": key,
                "note": spec["note"],
                "ok": runs_ok and sha_ok_ok,
            }
        )
    return rows


def check_scenarios(workflow, rep):
    """场景表断言：事件语义、dispatch 身份校验、同提交 push/PR 去重键一致。"""
    rows = build_scenario_rows(workflow)
    rep.check("场景表：所有场景的触发结论与期望一致", all(row["runs_ok"] for row in rows),
              "; ".join(row["ref"] for row in rows if not row["runs_ok"]))
    rep.check("场景表：dispatch 身份校验结论与期望一致（不一致/缩写会红）",
              all(row["sha_ok_ok"] for row in rows), "; ".join(row["ref"] for row in rows if not row["sha_ok_ok"]))
    by_ref = {(row["event"], row["ref"], row["tested"]): row["key"] for row in rows}
    keys = [row["key"] for row in rows]
    main_push_key = by_ref[("push", "refs/heads/main", "a" * 40)]
    branch_push_key = by_ref[("push", "refs/heads/fix/other-defect", "b" * 40)]
    pr_main_head_key = by_ref[("pull_request", "refs/pull/42/merge", "a" * 40)]
    pr_branch_head_key = by_ref[("pull_request", "refs/pull/43/merge", "b" * 40)]
    dispatch_key = by_ref[("workflow_dispatch",
                           "refs/heads/fix/post-merge-integration-cleanup-20261025", "a" * 40)]
    rep.check("场景表：同一提交的 push 与 PR 去重键相同", main_push_key == pr_main_head_key,
              main_push_key + " vs " + pr_main_head_key)
    rep.check("场景表：同一提交的普通分支 push 与 PR 去重键相同", branch_push_key == pr_branch_head_key,
              branch_push_key + " vs " + pr_branch_head_key)
    rep.check("场景表：dispatch 的键等于它校验过的受测提交（与同提交 push 同组）", dispatch_key == main_push_key,
              dispatch_key + " vs " + main_push_key)
    rep.check("场景表：不同提交的去重键不同（修复推送照常跑门禁）", main_push_key != branch_push_key,
              main_push_key + " vs " + branch_push_key)
    rep.check("场景表：去重键里带的是提交 SHA 而不是合成合并 SHA", all("c" * 40 not in key for key in keys),
              "PR merge SHA 不应出现在任何键里")


def print_scenarios(workflow):
    """把场景表的计算过程与结论打印出来（CI 日志里可审计）。"""
    rows = build_scenario_rows(workflow)
    print("== 触发场景表（由 ci.yml 实际规则计算；FAIL 表示规则与契约不符） ==")
    print("  " + " | ".join(("事件", "ref", "受测提交", "起完整矩阵", "dispatch校验", "去重键", "说明")))
    for row in rows:
        mark = "OK  " if row["ok"] else "FAIL"
        sha_state = "-" if row["sha_ok"] is None else ("通过" if row["sha_ok"] else "红")
        print(
            "  " + mark + " | "
            + row["event"] + " | "
            + row["ref"] + " | "
            + row["tested"][:8] + " | "
            + ("是" if row["runs"] else "否") + " | "
            + sha_state + " | "
            + row["key"] + " | "
            + row["note"]
        )
    bad = [row for row in rows if not row["ok"]]
    print("  -> " + str(len(rows)) + " 个场景，" + str(len(bad)) + " 个与契约不符")
    return not bad


def check_filter_markers(text, rep):
    """筛选契约的共用断言：真实 workflow 文本与 selftest 合成文本都过这一份。"""
    for needle in FILTER_MARKER_NEEDLES:
        rep.check_in("filter marker " + repr(needle) + " present", needle, text)


def parse_on(workflow):
    for key in ("on", True, "true", "True"):
        if key in workflow:
            value = workflow[key]
            break
    else:
        return {}
    if value is None:
        return {}
    if isinstance(value, str):
        return {value: None}
    if isinstance(value, list):
        return {str(item): None for item in value}
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def check_dispatch_preflight(job, text, rep):
    """dispatch 的身份校验必须是真实断言，不是打印：checkout + verify_sha.py + dispatch 守卫。"""
    if not isinstance(job, dict):
        job = {}
    steps = job.get("steps") or []
    checkout_steps = []
    verify_steps = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", ""))
        run = str(step.get("run", ""))
        if uses.startswith("actions/checkout@"):
            checkout_steps.append(step)
        if "verify_sha.py" in run:
            verify_steps.append(step)
    rep.check("ci.yml/ci-dedup-preflight: dispatch 会 checkout 输入 ref", bool(checkout_steps),
              "steps=" + repr([step.get("name") for step in steps]))
    if checkout_steps:
        checkout_text = workflow_text(checkout_steps[0])
        rep.check_in("ci.yml/ci-dedup-preflight: checkout ref 来自 inputs.ref", "inputs.ref", checkout_text)
        rep.check("ci.yml/ci-dedup-preflight: checkout 只在 dispatch 执行（其它事件不花 checkout 时间）",
                  "workflow_dispatch" in str(checkout_steps[0].get("if", "")), repr(checkout_steps[0].get("if")))
    rep.check("ci.yml/ci-dedup-preflight: dispatch 调用 verify_sha.py 做真实 SHA 断言", bool(verify_steps),
              "steps=" + repr([step.get("name") for step in steps]))
    if verify_steps:
        verify_text = workflow_text(verify_steps[0])
        for needle in ("--requested", "--expected-ref"):
            rep.check_in("ci.yml/ci-dedup-preflight: SHA 断言参数 " + needle, needle, verify_text)
        rep.check("ci.yml/ci-dedup-preflight: SHA 断言只在 dispatch 执行",
                  "workflow_dispatch" in str(verify_steps[0].get("if", "")), repr(verify_steps[0].get("if")))
    rep.check_in("ci.yml: preflight 打印实际受测提交", "tested commit", text)
    rep.check_in("ci.yml: preflight 打印去重键", "dedup key", text)
    # 「不一致会红」用同一份判定实现直接证明（ci.yml 的 dispatch 步骤调用的就是这份脚本）。
    same_ok, _ = verify_sha_check("a" * 40, "a" * 40, label="ci-contract")
    mismatch_ok, _ = verify_sha_check("a" * 40, "b" * 40, label="ci-contract")
    short_ok, _ = verify_sha_check("a" * 7, "a" * 40, label="ci-contract")
    rep.check("ci.yml: dispatch SHA 校验对不一致/缩写判定失败（与 targeted-verify 同一实现）",
              same_ok and not mismatch_ok and not short_ok,
              f"same={same_ok} mismatch={mismatch_ok} short_ok={short_ok}")


def check_ci(workflow, text, rep):
    on = parse_on(workflow)
    rep.check("ci.yml: still triggered by push (main/ordinary branches keep their gate)", "push" in on, "on=" + repr(sorted(on)))
    rep.check("ci.yml: still triggered by pull_request", "pull_request" in on, "on=" + repr(sorted(on)))

    push = on.get("push")
    patterns = push.get("branches") if isinstance(push, dict) else None
    rep.check("ci.yml: push declares a branch allowlist", isinstance(patterns, list) and bool(patterns), "push=" + repr(push))
    if isinstance(patterns, list) and patterns:
        rep.check("ci.yml: push allowlist 与契约逐项一致（只额外排除本轮工作分支）",
                  tuple(patterns) == CI_PUSH_PATTERNS, repr(patterns))
        rep.check("ci.yml: 第一条模式是 '**'（全分支基线）", patterns[0] == "**", repr(patterns[0]))
        rep.check("ci.yml: 后续模式全部是负向排除（! 前缀）",
                  all(isinstance(item, str) and item.startswith("!") and len(item) > 1 for item in patterns[1:]),
                  repr(list(patterns[1:])))
        rep.check("ci.yml: 模式里不使用 ?（本契约的匹配器不实现它的 GitHub 语义）",
                  all("?" not in str(item) for item in patterns), repr(patterns))
        for branch in CI_PUSH_MUST_MATCH:
            rep.check("ci.yml: push 仍触发完整矩阵 " + branch, push_branch_triggered(patterns, branch) is True)
        for branch in CI_PUSH_MUST_NOT_MATCH:
            rep.check("ci.yml: 本轮工作分支不自动跑矩阵 " + branch, push_branch_triggered(patterns, branch) is False)

    # workflow_dispatch：按完整 SHA 锁定的完整矩阵。job 不得按事件跳过，否则 dispatch 就不是完整矩阵。
    dispatch = on.get("workflow_dispatch")
    rep.check("ci.yml: workflow_dispatch 存在（按完整 SHA 锁定的完整矩阵入口）", isinstance(dispatch, dict), repr(dispatch))
    dispatch_inputs = (dispatch.get("inputs") if isinstance(dispatch, dict) else {}) or {}
    missing_inputs = [name for name in CI_DISPATCH_INPUTS if name not in dispatch_inputs]
    rep.check("ci.yml: dispatch 输入 ref + expected_sha 都在", not missing_inputs, "missing " + repr(missing_inputs))
    for name in CI_DISPATCH_INPUTS:
        spec = dispatch_inputs.get(name) or {}
        rep.check("ci.yml: dispatch 输入 " + name + " 必填", spec.get("required") is True, repr(spec))
    rep.check("ci.yml: no schedule (dedup must not add timed triggers)", "schedule" not in on)

    concurrency = workflow.get("concurrency")
    rep.check("ci.yml: workflow-level concurrency declared", isinstance(concurrency, dict), repr(concurrency))
    if isinstance(concurrency, dict):
        group = str(concurrency.get("group", ""))
        rep.check("ci.yml: concurrency 键 = 实际受测提交（PR 头提交，回退 github.sha）",
                  CI_TESTED_COMMIT_EXPR in group, group)
        rep.check("ci.yml: concurrency 键带 workflow 前缀", "github.workflow" in group, group)
        rep.check("ci.yml: cancel-in-progress explicitly true", concurrency.get("cancel-in-progress") is True,
                  repr(concurrency.get("cancel-in-progress")))
        push_key = evaluated_dedup_key(group, sha="a" * 40)
        pr_key = evaluated_dedup_key(group, sha="c" * 40, head_sha="a" * 40)
        rep.check("ci.yml: 同一提交的 push 与 PR 去重键相同（PR 合成 SHA 不再重复起矩阵）",
                  push_key == pr_key, push_key + " vs " + pr_key)
        rep.check("ci.yml: 不同提交去重键不同（修复推送照常跑门禁）",
                  push_key != evaluated_dedup_key(group, sha="b" * 40), group)
        rep.check("ci.yml: dispatch 场景键等于实际受测提交（ref 尖端 == expected_sha）",
                  evaluated_dedup_key(group, sha="a" * 40) == push_key, push_key)
    # 只看 YAML 语义（注释里提到跳过标记不算禁用 CI；注释本身不会改变任何行为）。
    workflow_body = workflow_text(workflow)
    rep.check("ci.yml: no skip-ci style check disabling",
              "skip ci" not in workflow_body and "skip-checks" not in workflow_body)

    jobs = workflow.get("jobs") or {}
    missing = [name for name in CI_REQUIRED_JOBS if name not in jobs]
    rep.check("ci.yml: every existing job kept (branch-protection check names unchanged)", not missing, "missing " + repr(missing))
    for name in CI_REQUIRED_JOBS:
        job = jobs.get(name)
        if isinstance(job, dict) and "if" in job:
            rep.check("ci.yml/" + name + ": 不按事件条件跳过（dispatch 也必须是完整矩阵）", False, "if=" + repr(job["if"]))
    rep.check_in("ci.yml: docs job runs the workflow-contract assertions", "verify_workflow_contract.py", text)
    rep.check_in("ci.yml: 写明 dispatch 需默认分支已有触发器（真实限制，本轮不依赖）", "默认分支", text)
    check_dispatch_preflight(jobs.get("ci-dedup-preflight") or {}, text, rep)


def check_targeted(workflow, text, rep, action_text=None):
    on = parse_on(workflow)
    rep.check("targeted-verify.yml: only trigger is workflow_dispatch", sorted(on) == ["workflow_dispatch"], "on=" + repr(sorted(on)))
    dispatch = on.get("workflow_dispatch")
    inputs = {}
    if isinstance(dispatch, dict):
        inputs = dispatch.get("inputs") or {}
    rep.check("targeted-verify.yml: workflow_dispatch declares inputs", bool(inputs), "inputs=" + repr(sorted(inputs)))
    missing = [name for name in TARGETED_REQUIRED_INPUTS if name not in inputs]
    rep.check("targeted-verify.yml: ref / sha / filters / target selector all present", not missing, "missing " + repr(missing))

    sha_input = inputs.get("sha") or {}
    rep.check("targeted-verify.yml: sha input is required", sha_input.get("required") is True, repr(sha_input))
    ref_input = inputs.get("ref") or {}
    rep.check("targeted-verify.yml: ref input has a default", "default" in ref_input, repr(ref_input))

    targets = inputs.get("targets") or {}
    options = targets.get("options") or []
    missing_targets = [name for name in TARGETED_REQUIRED_CHOICES if name not in options]
    rep.check("targeted-verify.yml: each CI item selectable (backend py3.11/3.12/windows/frontend/rust/docs...)", not missing_targets, "missing " + repr(missing_targets))
    rep.check("targeted-verify.yml: targets is a choice selector", targets.get("type") == "choice", repr(targets.get("type")))

    concurrency = workflow.get("concurrency")
    rep.check("targeted-verify.yml: workflow-level concurrency declared", isinstance(concurrency, dict), repr(concurrency))
    if isinstance(concurrency, dict):
        group = str(concurrency.get("group", ""))
        rep.check("targeted-verify.yml: concurrency group keyed on github.ref", "github.ref" in group, group)
    rep.check("targeted-verify.yml: contents read permission declared", "permissions" in workflow)

    jobs = workflow.get("jobs") or {}
    rep.check("targeted-verify.yml: verify-sha job exists", "verify-sha" in jobs, "jobs=" + repr(sorted(jobs)))

    checkout_jobs = []
    verify_jobs = []
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps") or []
        if not isinstance(steps, list):
            continue
        uses = [str(step.get("uses", "")) for step in steps if isinstance(step, dict)]
        runs = [str(step.get("run", "")) for step in steps if isinstance(step, dict)]
        has_checkout = any(item.startswith("actions/checkout@") for item in uses)
        has_verify = any("verify_sha.py" in item for item in runs)
        if has_checkout:
            checkout_jobs.append(name)
        if has_verify:
            verify_jobs.append(name)
        if name == "verify-sha":
            rep.check("targeted-verify.yml/verify-sha: verification script invoked", has_verify)
            continue
        if not has_checkout:
            continue
        rep.check("targeted-verify.yml/" + name + ": checks out the requested ref/sha explicitly", "ref" in text)
        rep.check("targeted-verify.yml/" + name + ": asserts actual SHA after checkout", has_verify, "job does not call verify_sha.py")
    rep.check("targeted-verify.yml: at least one job really checks out", bool(checkout_jobs), repr(checkout_jobs))
    rep.check("targeted-verify.yml: SHA assertion in more than one job (per-job evidence)", len(verify_jobs) >= 2, repr(verify_jobs))

    # SHA 核验的调用参数在复用的 composite action 里（jobs 通过它 checkout + 核验），
    # 所以断言要把 action 文本一起看，否则「参数还在不在」根本查不到。
    if action_text is None:
        action_file = ROOT / ".github" / "actions" / "verify-checkout" / "action.yml"
        rep.check("verify-checkout composite action 存在", action_file.exists(), str(action_file))
        action_text = action_file.read_text(encoding="utf-8") if action_file.exists() else ""
    sha_text = text + "\n" + action_text
    for needle in ("--requested", "--actual", "--expected-ref", "--actual-from"):
        rep.check_in("targeted-verify.yml: SHA arg " + needle + " present", needle, sha_text)
    rep.check_in("verify-checkout action: 显式 checkout 输入 ref", "ref: " + DOLLAR + "{{ inputs.ref }}", action_text)
    rep.check_in("verify-checkout action: 实际 SHA 取自 git rev-parse HEAD", "git rev-parse HEAD", action_text)
    rep.check_in("verify-checkout action: 不把 GITHUB_SHA 当实际 SHA", "--actual-from arg", action_text)

    # 精确筛选：不接受「只给文件就整套跑」那种含糊入口。
    check_filter_markers(text, rep)
    rep.check_in("targeted-verify.yml: node-id style pytest selection supported", "::", text)

    backend_job = jobs.get("backend") or {}
    backend_text = workflow_text(backend_job)
    for needle in ("PYTEST_NODE_IDS", "-k", "PYTEST_K"):
        rep.check_in("targeted-verify.yml/backend: pytest 按 " + needle + " 筛选", needle, backend_text)
    has_reject_guard = "拒绝跑整套" in backend_text
    rep.check(
        "targeted-verify.yml/backend: 没给筛选参数时拒绝跑整套（额度红线）",
        has_reject_guard,
        "" if has_reject_guard else "backend job 缺少「拒绝无筛选整套重跑」的保护",
    )
    frontend_job = jobs.get("frontend") or {}
    frontend_text = workflow_text(frontend_job)
    for needle in ("VITEST_FILES", "--testNamePattern", "VITEST_NAME"):
        rep.check_in("targeted-verify.yml/frontend: vitest 按 " + needle + " 筛选", needle, frontend_text)

    # 元断言：上面的筛选标记必须与 selftest 合成用例里的 job 定义同源，否则「绿色」只证明
    # 断言文本存在，不证明真的在筛。这里把 selftest 用例文本也过一遍同一组断言。
    probe = Report("selftest-filter-parity", verbose=rep.verbose)
    check_filter_markers(_SELFTEST_TARGETED_TEXT, probe)
    rep.check(
        "selftest 合成用例通过同一组筛选断言（防止断言退化成「文本存在」）",
        not probe.failed(),
        "; ".join(row[0] for row in probe.failed()),
    )
    for needle in TARGETED_REQUIRED_BUILD_COMMANDS:
        rep.check_in("targeted-verify.yml: can run build/check missing from failed CI: " + repr(needle), needle, text)

    rep.check("targeted-verify.yml: summary job exists", "summary" in jobs, "jobs=" + repr(sorted(jobs)))
    rep.check_in("targeted-verify.yml: summary prints SKIPPED explicitly", "SKIPPED", text)
    rep.check_in("targeted-verify.yml: fails when nothing was runnable", "no runnable checks", text)


def check_no_skip_ci(rep):
    hits = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"skip[\s_-]?ci|skip-checks", stripped, re.I):
                hits.append(path.name + ": " + stripped)
    rep.check("all workflows: no skip-ci style disabling", not hits, "; ".join(hits))


def selftest(verbose=False):
    print("== selftest: break each contract, assertions must go red ==")
    import copy

    rep = Report("selftest")
    good_ci = {
        "on": {
            "push": {"branches": list(CI_PUSH_PATTERNS)},
            "pull_request": None,
            "workflow_dispatch": {"inputs": {name: {"required": True, "type": "string"} for name in CI_DISPATCH_INPUTS}},
        },
        "concurrency": {
            "group": "ci-" + DOLLAR + "{{ github.workflow }}-" + DOLLAR + "{{ github.event.pull_request.head.sha || github.sha }}",
            "cancel-in-progress": True,
        },
        "jobs": {name: {"steps": []} for name in CI_REQUIRED_JOBS},
    }
    good_ci["jobs"]["ci-dedup-preflight"] = {
        "steps": [
            {
                "name": "Checkout the dispatched ref",
                "uses": "actions/checkout@v5",
                "if": "github.event_name == 'workflow_dispatch'",
                "with": {"ref": "inputs.ref"},
            },
            {
                "name": "Assert dispatched ref tip equals expected SHA",
                "if": "github.event_name == 'workflow_dispatch'",
                "run": "python scripts/targeted-verify/verify_sha.py --requested X --expected-ref Y",
            },
        ]
    }
    text_ci = "python scripts/targeted-verify/verify_workflow_contract.py tested commit dedup key 默认分支"

    def run_ci(data, name):
        scoped = Report(name, verbose=verbose)
        check_ci(data, text_ci, scoped)
        return scoped

    def broken_ci(mutator, name):
        data = copy.deepcopy(good_ci)
        mutator(data)
        return run_ci(data, name)

    sub = run_ci(good_ci, "selftest-ci-good")
    rep.check("intact ci.yml passes", not sub.failed(), "; ".join(row[0] for row in sub.failed()))

    scoped = broken_ci(lambda data: data.pop("concurrency"), "selftest-ci-no-concurrency")
    rep.check("removing concurrency must fail", any("concurrency" in row[0] for row in scoped.failed()))

    def wrong_group(data):
        data["concurrency"] = {"group": "ci-" + DOLLAR + "{{ github.ref }}", "cancel-in-progress": True}

    scoped = broken_ci(wrong_group, "selftest-ci-wrong-group")
    rep.check("keying concurrency on github.ref must fail", any("实际受测提交" in row[0] for row in scoped.failed()))

    def narrow_push(data):
        data["on"]["push"]["branches"] = ["main"]

    scoped = broken_ci(narrow_push, "selftest-ci-narrow-push")
    rep.check("narrowing push to main must fail", any("push allowlist" in row[0] for row in scoped.failed()))

    def exclude_main(data):
        data["on"]["push"]["branches"] = ["**", "!main"]

    scoped = broken_ci(exclude_main, "selftest-ci-main-excluded")
    rep.check("excluding main from push must fail", any("push 仍触发完整矩阵 main" in row[0] for row in scoped.failed()))

    def drop_round_exclusions(data):
        data["on"]["push"]["branches"] = ["**"]

    scoped = broken_ci(drop_round_exclusions, "selftest-ci-no-round-exclusion")
    rep.check("dropping the round-branch exclusions must fail", any("本轮工作分支不自动跑矩阵" in row[0] for row in scoped.failed()))

    def exclude_integration_branch(data):
        data["on"]["push"]["branches"] = ["**", "!fix/post-merge-*"]

    scoped = broken_ci(exclude_integration_branch, "selftest-ci-integration-excluded")
    rep.check("excluding the round integration branch must fail", any(
        "push 仍触发完整矩阵 fix/post-merge-integration-cleanup-20261025" in row[0] for row in scoped.failed()
    ))

    def drop_expected_sha(data):
        data["on"]["workflow_dispatch"]["inputs"].pop("expected_sha", None)

    scoped = broken_ci(drop_expected_sha, "selftest-ci-dispatch-inputs")
    rep.check("dropping expected_sha input must fail", any("expected_sha" in row[0] for row in scoped.failed()))

    def drop_dispatch_assert(data):
        data["jobs"]["ci-dedup-preflight"] = {
            "steps": [
                {
                    "name": "Checkout the dispatched ref",
                    "uses": "actions/checkout@v5",
                    "if": "github.event_name == 'workflow_dispatch'",
                    "with": {"ref": "inputs.ref"},
                }
            ]
        }

    scoped = broken_ci(drop_dispatch_assert, "selftest-ci-dispatch-assert")
    rep.check("dropping the dispatch SHA assertion must fail", any("verify_sha.py" in row[0] for row in scoped.failed()))

    def add_job_if(data):
        data["jobs"]["backend"] = {"steps": [], "if": "github.event_name == 'push'"}

    scoped = broken_ci(add_job_if, "selftest-ci-conditional-job")
    rep.check("conditionally skipping a matrix job must fail", any("不按事件条件跳过" in row[0] for row in scoped.failed()))

    scoped = Report("selftest-scenarios-good", verbose=verbose)
    check_scenarios(good_ci, scoped)
    rep.check("intact scenario table passes", not scoped.failed(), "; ".join(row[0] for row in scoped.failed()))

    scenario_broken = copy.deepcopy(good_ci)
    scenario_broken["on"]["push"]["branches"] = ["**", "!main"]
    scoped = Report("selftest-scenarios-bad", verbose=verbose)
    check_scenarios(scenario_broken, scoped)
    rep.check("scenario table catches an excluded main branch", bool(scoped.failed()))

    good_inputs = {name: {"required": True, "default": "x"} for name in TARGETED_REQUIRED_INPUTS}
    good_inputs["targets"] = {"type": "choice", "options": list(TARGETED_REQUIRED_CHOICES)}
    good_targeted = {
        "on": {"workflow_dispatch": {"inputs": good_inputs}},
        "concurrency": {"group": "targeted-verify-" + DOLLAR + "{ github.ref }"},
        "permissions": {"contents": "read"},
        "jobs": {
            "verify-sha": {"steps": [{"run": "python scripts/targeted-verify/verify_sha.py --requested x --actual y --actual-from arg"}]},
            "backend": {
                "steps": [
                    {"uses": "actions/checkout@v5"},
                    {"run": "python scripts/targeted-verify/verify_sha.py --requested x --actual y --actual-from arg --expected-ref refs/heads/main"},
                    {"run": "uv run --frozen pytest $env:PYTEST_NODE_IDS -k $env:PYTEST_K"},
                    {"run": "if (no PYTEST_NODE_IDS and no PYTEST_K) { 拒绝跑整套 }"},
                ]
            },
            "frontend": {
                "steps": [
                    {"uses": "actions/checkout@v5"},
                    {"run": "python scripts/targeted-verify/verify_sha.py --requested x --actual y --actual-from arg"},
                    {"run": "npx vitest run $env:VITEST_FILES --testNamePattern $env:VITEST_NAME"},
                ]
            },
            "summary": {"steps": [{"run": "echo SKIPPED; echo no runnable checks"}]},
        },
    }
    text_targeted = _SELFTEST_TARGETED_TEXT
    sub = Report("selftest-targeted-good", verbose=verbose)
    check_targeted(good_targeted, text_targeted, sub, action_text=_SELFTEST_ACTION_TEXT)
    rep.check("intact targeted-verify.yml passes", not sub.failed(), "; ".join(r[0] for r in sub.failed()))

    broken_inputs = dict(good_inputs)
    broken_inputs.pop("sha")
    broken_targeted = {
        "on": {"workflow_dispatch": {"inputs": broken_inputs}},
        "concurrency": good_targeted["concurrency"],
        "permissions": {"contents": "read"},
        "jobs": {
            "verify-sha": {"steps": [{"run": "echo no verification"}]},
            # 故意保留 checkout、去掉 SHA 核验：这正是「旧 CI 重跑旧提交」的形状。
            "backend": {
                "steps": [
                    {"uses": "actions/checkout@v5"},
                    {"run": "npx vue-tsc --noEmit"},
                    {"run": "pytest $env:PYTEST_NODE_IDS -k $env:PYTEST_K"},
                    {"run": "if (no PYTEST_NODE_IDS and no PYTEST_K) { 拒绝跑整套 }"},
                ]
            },
            "summary": {"steps": [{"run": "echo SKIPPED; echo no runnable checks"}]},
        },
    }
    sub = Report("selftest-targeted-broken", verbose=verbose)
    check_targeted(broken_targeted, text_targeted, sub, action_text=_SELFTEST_ACTION_TEXT)
    rep.check("dropping the sha input must fail", any("sha input" in row[0] for row in sub.failed()))
    rep.check("dropping the SHA assertion must fail", any("asserts actual SHA" in row[0] for row in sub.failed()))

    print("  -> selftest: " + str(len(rep.rows)) + " checks, " + str(len(rep.failed())) + " failed")
    return 1 if rep.failed() else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="parse workflow yaml and assert trigger/verify/filter contract")
    parser.add_argument("--selftest", action="store_true", help="prove the assertions can go red")
    parser.add_argument("--scenarios", action="store_true", help="只打印触发场景表（仍按真实规则判定，失败退出 1）")
    parser.add_argument("--verbose", action="store_true", help="print every failed assertion inside selftest")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest(verbose=args.verbose)

    if not CI_FILE.exists() or not TARGETED_FILE.exists():
        print("::error title=workflow contract::missing ci.yml or targeted-verify.yml")
        return 1

    ci = load_workflow(CI_FILE)
    targeted = load_workflow(TARGETED_FILE)

    if args.scenarios:
        return 0 if print_scenarios(ci) else 1

    rep = Report("workflow contract")
    print("parsed: " + str(CI_FILE.relative_to(ROOT)) + " / " + str(TARGETED_FILE.relative_to(ROOT)))
    check_ci(ci, workflow_text(ci) + CI_FILE.read_text(encoding="utf-8"), rep)
    check_targeted(targeted, workflow_text(targeted) + TARGETED_FILE.read_text(encoding="utf-8"), rep)
    check_scenarios(ci, rep)
    check_no_skip_ci(rep)
    print_scenarios(ci)
    return rep.render()


if __name__ == "__main__":
    sys.exit(main())
