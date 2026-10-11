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


def check_ci(workflow, text, rep):
    on = parse_on(workflow)
    rep.check("ci.yml: still triggered by push (other branches keep their gate)", "push" in on, "on=" + repr(sorted(on)))
    rep.check("ci.yml: still triggered by pull_request", "pull_request" in on, "on=" + repr(sorted(on)))
    push = on.get("push")
    rep.check(
        "ci.yml: push covers all branches (dedup must not narrow other branches)",
        isinstance(push, dict) and push.get("branches") == ["**"],
        "push=" + repr(push),
    )
    rep.check("ci.yml: no workflow_dispatch (targeted entry is a separate workflow)", "workflow_dispatch" not in on)
    rep.check("ci.yml: no schedule (dedup must not add timed triggers)", "schedule" not in on)

    concurrency = workflow.get("concurrency")
    rep.check("ci.yml: workflow-level concurrency declared", isinstance(concurrency, dict), repr(concurrency))
    if isinstance(concurrency, dict):
        group = str(concurrency.get("group", ""))
        rep.check("ci.yml: concurrency group keyed on github.sha (push+PR of one commit share a group)", "github.sha" in group, group)
        rep.check("ci.yml: cancel-in-progress explicitly true", concurrency.get("cancel-in-progress") is True, repr(concurrency.get("cancel-in-progress")))
    rep.check("ci.yml: no skip-ci style check disabling", "skip ci" not in text and "skip-checks" not in text)

    jobs = workflow.get("jobs") or {}
    missing = [name for name in CI_REQUIRED_JOBS if name not in jobs]
    rep.check("ci.yml: every existing job kept (branch-protection check names unchanged)", not missing, "missing " + repr(missing))
    for name in CI_REQUIRED_JOBS:
        job = jobs.get(name)
        if isinstance(job, dict) and "if" in job:
            rep.check("ci.yml/" + name + ": job is not conditionally skipped", False, "if=" + repr(job["if"]))
    rep.check_in("ci.yml: docs job runs the workflow-contract assertions", "verify_workflow_contract.py", text)


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
    rep.check(
        "targeted-verify.yml/backend: 没给筛选参数时拒绝跑整套（额度红线）",
        "拒绝跑整套" in backend_text,
        "backend job 缺少「拒绝无筛选整套重跑」的保护",
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
    rep = Report("selftest")
    good_ci = {
        "on": {"push": {"branches": ["**"]}, "pull_request": None},
        "concurrency": {"group": "ci-" + DOLLAR + "{ github.workflow }-" + DOLLAR + "{ github.sha }", "cancel-in-progress": True},
        "jobs": {name: {"steps": []} for name in CI_REQUIRED_JOBS},
    }
    text_ci = "python scripts/targeted-verify/verify_workflow_contract.py"
    sub = Report("selftest-ci-good", verbose=verbose)
    check_ci(good_ci, text_ci, sub)
    rep.check("intact ci.yml passes", not sub.failed(), "; ".join(r[0] for r in sub.failed()))

    broken_concurrency = dict(good_ci)
    broken_concurrency.pop("concurrency")
    sub = Report("selftest-ci-no-concurrency", verbose=verbose)
    check_ci(broken_concurrency, text_ci, sub)
    rep.check("removing concurrency must fail", any("concurrency" in row[0] for row in sub.failed()))

    broken_sha = dict(good_ci)
    broken_sha["concurrency"] = {"group": "ci-" + DOLLAR + "{ github.ref }", "cancel-in-progress": True}
    sub = Report("selftest-ci-wrong-group", verbose=verbose)
    check_ci(broken_sha, text_ci, sub)
    rep.check("keying concurrency on github.ref must fail", any("github.sha" in row[0] for row in sub.failed()))

    broken_push = dict(good_ci)
    broken_push["on"] = {"push": {"branches": ["integrate/**"]}, "pull_request": None}
    sub = Report("selftest-ci-narrow-push", verbose=verbose)
    check_ci(broken_push, text_ci, sub)
    rep.check("narrowing push must fail", any("push covers all branches" in row[0] for row in sub.failed()))

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
    parser.add_argument("--verbose", action="store_true", help="print every failed assertion inside selftest")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest(verbose=args.verbose)

    if not CI_FILE.exists() or not TARGETED_FILE.exists():
        print("::error title=workflow contract::missing ci.yml or targeted-verify.yml")
        return 1

    ci = load_workflow(CI_FILE)
    targeted = load_workflow(TARGETED_FILE)
    rep = Report("workflow contract")
    print("parsed: " + str(CI_FILE.relative_to(ROOT)) + " / " + str(TARGETED_FILE.relative_to(ROOT)))
    check_ci(ci, workflow_text(ci) + CI_FILE.read_text(encoding="utf-8"), rep)
    check_targeted(targeted, workflow_text(targeted) + TARGETED_FILE.read_text(encoding="utf-8"), rep)
    check_no_skip_ci(rep)
    return rep.render()


if __name__ == "__main__":
    sys.exit(main())
