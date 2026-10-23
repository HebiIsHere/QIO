"""R9-H D：固定查询 CI 失败复现与规则审计（脚本，不触发 CI）。

用途：
* 记录上一轮真实 CI（run 39899916527 / 39903261628）的失败项与当前代码状态；
* 用生产代码的规则函数复现 Linux 上的 fixed_queries 失败场景（/usr/local/bin、/opt/<...>/bin
  的 npm 入口；以及真实工具流程把 program_path 填成 resolve 后脚本目标的情况）；
* 输出 JSON 证据到 _r9h-evidence/d-ci-audit.json；全部断言通过 exit 0。

用法（worktree backend 目录）：
    uv run --frozen python ../scripts/r9h_ci_audit.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend" / "src"))

import agent.security.fixed_queries as fq  # noqa: E402
from agent.security.fixed_queries import FixedQueryFacts, classify_fixed_query  # noqa: E402

CI_RUNS = [
    {
        "run_id": 39899916527,
        "sha": "4ad0d9e6e831c2fc1afef0f8c0dd7f2749790726",
        "workflow": "CI",
        "conclusion": "failure",
        "failing_jobs": [
            "backend (windows-latest)",
            "backend (py3.12)",
            "backend (py3.11)",
            "install e2e (windows-latest)",
        ],
        "root_causes": [
            "补偿迁移 58 只抄了 56+57，存量库补偿链不完整（52/53/55 对象缺失）",
            "模型调用入口清单未登记 security/review.py、security/resolution.py",
            "取消准备轮测试把模型调用次数写死，未计入每轮要求解析调用",
            "install e2e 假厂商 FIFO 脚本被要求解析调用消耗",
        ],
        "fix_commit": "c085b745",
    },
    {
        "run_id": 39903261628,
        "sha": "c085b745d8adffe89aa7ae3fc555bc93f68f995d",
        "workflow": "CI",
        "conclusion": "failure",
        "failing_jobs": [
            "backend (windows-latest)",
            "backend (py3.12)",
            "backend (py3.11)",
        ],
        "failing_tests": [
            "tests/test_interactive_during_heavy_work.py::test_health_probe_stays_responsive_while_slow_prediction_runs（355ms > 300ms，runner 调度地板，R9-F W4 已有诊断）",
            "tests/test_r9_acceptance_e2e.py::test_restricted_write_pends_then_approval_executes（审批执行 8s 未完成）",
            "tests/test_r9g_fixed_queries.py::test_node_npm_version_forms（Linux /usr/local/bin/npm 符号链接入口被判 None）",
            "tests/test_tool_restore.py::test_restore_agent_tool_after_restart（自建工具审批绑定）",
        ],
        "fix_commit": "b0eb34b3（/usr/local/bin 精确白名单 + 旧验收/工具恢复要求绑定）",
        "remaining_after_fix": [
            "/opt/<...>/bin 的系统安装布局仍不在精确白名单（本轮 R9-H §7 修复）",
            "真实工具流程 program_path 为 resolve 后的脚本目标（npm-cli.js），入口名丢失（本轮按系统根 + 同族脚本识别）",
            "健康探测 355ms 为 runner 调度地板问题（R9-F W4 诊断，非固定查询；不归本轮）",
        ],
    },
]


def _facts(program_path: str | None, program_name: str, argv: tuple[str, ...], *,
           cwd: str | None = None, shell: bool = False) -> FixedQueryFacts:
    return FixedQueryFacts(
        kind="shell" if shell else "program",
        program_path=program_path,
        program_name=program_name,
        argv=argv,
        cwd=cwd,
        shell=shell,
    )


def _rule(raw: str, real: str, name: str, argv: tuple[str, ...] = ("--version",)) -> dict:
    facts = _facts(raw, name, argv)
    return {
        "identity_rule_problem": fq._identity_rule_problem(facts, raw_path=raw, real_text=real),
        "entry_dir": str(Path(raw).parent.as_posix()),
        "entry_is_system_bin_dir": fq._is_posix_system_bin_dir(str(Path(raw).parent.as_posix())),
        "target": real,
        "target_under_system_roots": fq._under_posix_system_roots(real),
        "script_entry_allowed": fq._posix_script_entry_allowed(raw, real, name),
        "family_of_entry": fq._family(name),
        "resolved_script_family": fq._resolved_script_family(real),
    }


def _host_check(name: str) -> dict:
    found = shutil.which(name)
    if not found:
        return {"found": None}
    facts = _facts(found, name, ("--version",))
    result = classify_fixed_query(facts)
    return {
        "found": found,
        "kind": None if result is None else result.kind,
    }


def main() -> int:
    scenarios = {
        "ci_run2_usr_local_bin_npm_entry": _rule(
            "/usr/local/bin/npm",
            "/usr/local/lib/node_modules/npm/bin/npm-cli.js",
            "npm",
        ),
        "opt_hostedtoolcache_npm_entry": _rule(
            "/opt/hostedtoolcache/node/20.19.0/x64/bin/npm",
            "/opt/hostedtoolcache/node/20.19.0/x64/lib/node_modules/npm/bin/npm-cli.js",
            "npm",
        ),
        "opt_node_pnpm_entry": _rule(
            "/opt/node/bin/pnpm",
            "/opt/node/lib/node_modules/pnpm/bin/pnpm.cjs",
            "pnpm",
        ),
        "tool_flow_resolved_npm_cli_js": {
            "identity_rule_problem": fq._identity_rule_problem(
                _facts(
                    "/opt/node/lib/node_modules/npm/bin/npm-cli.js",
                    "npm-cli.js",
                    ("--version",),
                ),
                raw_path="/opt/node/lib/node_modules/npm/bin/npm-cli.js",
                real_text="/opt/node/lib/node_modules/npm/bin/npm-cli.js",
            ),
            "resolved_script_family": fq._resolved_script_family(
                "/opt/node/lib/node_modules/npm/bin/npm-cli.js"
            ),
        },
        "negatives": {
            "temp_entry": _rule("/tmp/bin/npm", "/tmp/evil/npm-cli.js", "npm"),
            "workspace_target": _rule(
                "/usr/local/bin/npm", "/home/user/project/npm-cli.js", "npm"
            ),
            "arbitrary_opt_program": {
                "family": fq._family("aws"),
                "script_entry_allowed": fq._posix_script_entry_allowed(
                    "/opt/tools/bin/aws", "/opt/tools/lib/aws.js", "aws"
                ),
            },
        },
    }

    checks: list[tuple[str, bool]] = []

    def _expect(label: str, condition: bool) -> None:
        checks.append((label, bool(condition)))

    _expect(
        "ci /usr/local/bin/npm 身份规则放行",
        scenarios["ci_run2_usr_local_bin_npm_entry"]["identity_rule_problem"] is None,
    )
    _expect(
        "/opt hostedtoolcache npm 身份规则放行",
        scenarios["opt_hostedtoolcache_npm_entry"]["identity_rule_problem"] is None,
    )
    _expect(
        "/opt node pnpm 脚本入口放行",
        scenarios["opt_node_pnpm_entry"]["script_entry_allowed"] is True,
    )
    _expect(
        "工具真实流程 resolve 目标按同族识别",
        scenarios["tool_flow_resolved_npm_cli_js"]["resolved_script_family"] == "npm",
    )
    _expect(
        "临时目录入口拒绝",
        scenarios["negatives"]["temp_entry"]["identity_rule_problem"] is not None,
    )
    _expect(
        "工作区目标拒绝",
        scenarios["negatives"]["workspace_target"]["identity_rule_problem"] is not None,
    )
    _expect(
        "/opt 任意程序不放行",
        scenarios["negatives"]["arbitrary_opt_program"]["family"] is None
        and scenarios["negatives"]["arbitrary_opt_program"]["script_entry_allowed"] is False,
    )

    host = {name: _host_check(name) for name in ("python", "node", "npm", "git")}
    payload = {
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "platform": sys.platform,
        "python": sys.version,
        "ci_runs": CI_RUNS,
        "scenarios": scenarios,
        "host_real_programs": host,
        "checks": [{"label": label, "ok": ok} for label, ok in checks],
        "all_ok": all(ok for _, ok in checks),
    }
    out = REPO / "_r9h-evidence" / "d-ci-audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"checks": payload["checks"], "all_ok": payload["all_ok"],
                      "host": host, "evidence": str(out)}, ensure_ascii=False, indent=1))
    return 0 if payload["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
