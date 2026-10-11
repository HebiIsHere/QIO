"""第二段（集成候选）验收编排器：把 E 的定向验收按固定顺序跑一遍并留下证据。

只跑**定向**项（指定文件 / 指定脚本），不跑全量 pytest、不跑全前端 vitest、不跑完整 CI。

包含：
  1. SHA 核验：--sha 必须与工作区 HEAD 一致（复用 D 的 scripts/targeted-verify/verify_sha.py，
     没有就退化成自己比对 git rev-parse HEAD）；
  2. 迁移验证脚本（verify_migrations.py，空库 / 真实库 / 补偿 / 重启 / 存量带数据 / 不变式）；
  3. 提交身份链路脚本（verify_submission_identity.py，契约 §1）；
  4. 后端定向 pytest（互动闭环 / 理解 / 执行 / 材料 / 提交 / 迁移 / 发送身份）；
  5. 前端定向 vitest（F10 提交刷新、F11 清单读取失败、理解归属、以及我自己的「只读一次」探针）。

用法（worktree 根目录）：
  backend/.venv/Scripts/python.exe scripts/irm-int-verify/run_stage2.py --sha <候选SHA> [--out-dir scripts/irm-int-verify/out]

每一项都会打印「命令 + 退出码 + 输出尾部」，并把汇总写成 <out-dir>/stage2-<sha>.json。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

BACKEND_PYTEST_TARGETS = [
    "tests/test_interactive_real_model.py",
    "tests/test_interactive_execution.py",
    "tests/test_interactive_understanding.py",
    "tests/test_interactive_store_understanding.py",
    "tests/test_interactive_materials.py",
    "tests/test_interactive_submission.py",
    "tests/test_interactive_migration_integration.py",
    "tests/test_fu_w3_b01_schema.py",
    "tests/test_audit_f04_acceptance_receipt.py",
]

FRONTEND_VITEST_TARGETS = [
    "src/acceptance-state-recovery/f10-submit-refresh.test.ts",
    "src/acceptance-state-recovery/f11-intents-load-failure.test.ts",
    "src/interactive/__tests__/irm-d-understanding-store.test.ts",
]

PROBE_SRC = ROOT / "scripts" / "irm-int-verify" / "probes" / "e-submit-read-once.probe.test.ts"
PROBE_DIR = ROOT / "frontend" / "src" / "__irm_int_e_probe__"
PROBE_TARGET = "src/__irm_int_e_probe__/e-submit-read-once.probe.test.ts"


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 1800, env_note: str = "") -> dict:
    started = time.time()
    display = " ".join(cmd)
    print(f"\n$ ({' '.join(str(c) for c in [cwd or ROOT])}) {display}" + (f"   [{env_note}]" if env_note else ""))
    try:
        proc = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True, timeout=timeout)
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", errors="replace")
        err = f"TIMEOUT after {timeout}s"
        code = 124
    tail = "\n".join((out + "\n" + err).strip().splitlines()[-25:])
    print(f"  exit={code}  ({time.time() - started:.1f}s)")
    for line in tail.splitlines():
        print("  | " + line)
    return {"cmd": display, "cwd": str(cwd or ROOT), "exit": code, "tail": tail,
            "seconds": round(time.time() - started, 1)}


def head_sha() -> str:
    out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sha", required=True, help="被验收的候选 SHA（必须等于工作区 HEAD）")
    ap.add_argument("--out-dir", default="scripts/irm-int-verify/out")
    ap.add_argument("--skip-frontend", action="store_true")
    ap.add_argument("--skip-backend", action="store_true")
    args = ap.parse_args()

    out_dir = ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    actual = head_sha()
    print(f"worktree={ROOT}\nrequested SHA={args.sha}\nworktree HEAD={actual}")
    if actual != args.sha:
        print("!! 请求的 SHA 与工作区 HEAD 不一致：先 git checkout 到候选 SHA 再跑")
        return 2

    steps: list[dict] = []

    sha_checker = ROOT / "scripts" / "targeted-verify" / "verify_sha.py"
    if sha_checker.exists():
        steps.append({"name": "sha-check",
                      **run([str(PY), str(sha_checker), "--requested", args.sha], cwd=ROOT)})
    else:
        steps.append({"name": "sha-check", "cmd": "(内置) git rev-parse HEAD", "exit": 0,
                      "tail": f"HEAD={actual}（D 的 verify_sha.py 不在时用内置比对）", "seconds": 0})

    steps.append({"name": "migrations",
                  **run([str(PY), "scripts/irm-int-verify/verify_migrations.py",
                         "--json", str(out_dir / f"verify-migrations-{args.sha[:8]}.json")], cwd=ROOT)})
    steps.append({"name": "submission-identity",
                  **run([str(PY), "scripts/irm-int-verify/verify_submission_identity.py",
                         "--json", str(out_dir / f"verify-identity-{args.sha[:8]}.json")], cwd=ROOT)})

    if not args.skip_backend:
        steps.append({"name": "backend-targeted-pytest",
                      **run(["uv", "run", "--frozen", "pytest", "-q", *BACKEND_PYTEST_TARGETS],
                            cwd=ROOT / "backend")})

    if not args.skip_frontend:
        PROBE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROBE_SRC, PROBE_DIR / Path(PROBE_TARGET).name)
        try:
            steps.append({"name": "frontend-targeted-vitest",
                          **run(["npm", "test", "--", *FRONTEND_VITEST_TARGETS, PROBE_TARGET],
                                cwd=ROOT / "frontend")})
        finally:
            shutil.rmtree(PROBE_DIR, ignore_errors=True)

    failed = [s for s in steps if s.get("exit") != 0]
    summary = {
        "script": "scripts/irm-int-verify/run_stage2.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "worktree": str(ROOT),
        "requested_sha": args.sha,
        "head_sha": actual,
        "passed": not failed,
        "failed_steps": [s["name"] for s in failed],
        "steps": steps,
    }
    out_file = out_dir / f"stage2-{args.sha[:8]}.json"
    out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n" + "=" * 70)
    for s in steps:
        print(f"  [{'PASS' if s.get('exit') == 0 else 'FAIL'}] {s['name']} (exit={s.get('exit')})")
    print(f"\n汇总：{'全部通过' if not failed else '失败项 ' + ', '.join(summary['failed_steps'])}")
    print(f"证据：{out_file}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
