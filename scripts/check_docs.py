"""文档一致性检查（stdlib only，不需要安装依赖）。

检查四类会真实造成伤害的文档漂移：

1. 里程碑状态：`docs/status.md` 是唯一事实源；其他文档不得声明与之矛盾的状态。
2. 硬编码数字：测试数 / 事件数 / 表数这类会迅速过期的数字不允许写进文档。
3. 被引用的路径必须真实存在（文档里的 `docs/...`、`backend/...` 等）。
4. 被引用的命令必须真实存在（`scripts/*.py`、`python -m agent.x.y` 等）。
5. CI 里执行的安装/测试命令，必须在 `docs/SETUP.md` 里能找到——否则
   「本地照着 SETUP 装、CI 装另一套」的漂移会重新出现。
6. `docs/status.md` 顶部的「最后核对」不能落后于正文里已经记到的最新日期——
   读者就是靠这个字段判断「这份状态有多新」；正文写到 2026-10-03 而顶部还挂 2026-09-12，
   比缺一条数据更误导（2026-10-03 由用户发现）。

用法：python scripts/check_docs.py
退出码：0 = 通过；1 = 存在漂移。

需要豁免某一行时，在该行加注释 `docs-check: ignore` 并写清理由。
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STATUS_FILE = ROOT / "docs" / "status.md"
# 除 status.md 外需要参与一致性检查的文档
OTHER_DOCS = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "SETUP.md",
    ROOT / "docs" / "frontend-requirements.md",
    ROOT / "docs" / "frontend-design.md",
    ROOT / "docs" / "frontend-components.md",
    ROOT / "docs" / "release-qualification.md",
    # 长期测试体系的护栏清单：它引用的路径/命令必须真实存在，所以也纳入一致性检查
    ROOT / "docs" / "longterm-testing.md",
    # 后端进程生命周期的实测记录（同样引用脚本与路径）
    ROOT / "docs" / "process-lifecycle-verification.md",
]

VALID_STATUS = {"completed", "partial", "active", "planned"}

# 「最后核对：2026-10-03（main 分支）」——日期后可以带别的说明
LAST_CHECKED = re.compile(r"最后核对：\s*(\d{4}-\d{2}-\d{2})")
# 正文里的日期字面量（用来判断「这份文件已经记到哪天」）
DATE_LITERAL = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")

MILESTONE_HEADING = re.compile(r"^###\s+((?:M|P)\d+)\s+—")
STATUS_LINE = re.compile(r"^\s*-\s*\*\*Status：\*\*\s*(\w+)")

CONTRADICTION_WORDS = ("未实现", "实现中", "待实现", "尚未实现", "规划中")
HARDCODED = re.compile(r"\d+\s*(?:个|类|种|张|条)?\s*(?:测试|事件类型|事件|表)")
# 注意扩展名的**顺序**：`json` 放在 `jsonl` 前面会把 `x.jsonl` 截成 `x.json`，
# 于是 `.jsonl` 路径永远被报成「不存在」（2026-10-02 由 docs/longterm-testing.md 暴露）。
# 前缀相同的扩展名一律长者在前。
PATH_LIKE = re.compile(
    r"`((?:docs|backend|frontend|scripts)/[A-Za-z0-9_./\-*]+?"
    r"\.(?:md|py|ts|vue|jsonl|json|toml|ps1|yaml|yml|css))"
)
SCRIPT_LIKE = re.compile(r"`(scripts/[A-Za-z0-9_./\-]+\.(?:py|ps1))")
MODULE_LIKE = re.compile(r"python\s+-m\s+(agent(?:\.[a-z_]+)+)")

CI_FILE = ROOT / ".github" / "workflows" / "ci.yml"
SETUP_FILE = ROOT / "docs" / "SETUP.md"
# CI 里这些前缀的 run 命令属于「安装/测试」，必须与 SETUP 对齐
CI_COMMAND_PREFIXES = ("uv ", "npm ", "npx ", "cargo ", "python scripts/")


def _configure_output() -> None:
    """报告层必须能在任何控制台编码下工作（英文 Windows 的 cp1252 也不能崩）。

    与 scripts/release_gate.py / scripts/frozen_worker_smoke.py 同一套做法：
    tty 保留自己的编码 + backslashreplace；重定向/CI 写 UTF-8 字节。
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


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_status_milestones() -> dict[str, str]:
    """从 status.md 解析 {里程碑: 状态}。"""
    milestones: dict[str, str] = {}
    current: str | None = None
    for line in read(STATUS_FILE).splitlines():
        heading = MILESTONE_HEADING.match(line)
        if heading:
            current = heading.group(1)
            continue
        status = STATUS_LINE.match(line)
        if status and current is not None:
            milestones[current] = status.group(1)
            current = None
    return milestones


def check_status_values(milestones: dict[str, str], errors: list[str]) -> None:
    if not milestones:
        errors.append(f"{STATUS_FILE.relative_to(ROOT)}：没有解析到任何里程碑条目")
        return
    for name, status in milestones.items():
        if status not in VALID_STATUS:
            errors.append(
                f"{STATUS_FILE.relative_to(ROOT)}：{name} 的状态 `{status}` 不在 "
                f"{sorted(VALID_STATUS)} 里"
            )


def check_milestone_refs(milestones: dict[str, str], errors: list[str]) -> None:
    """其他文档引用的 M/P 编号必须在 status.md 里存在。"""
    for doc in OTHER_DOCS:
        if not doc.exists():
            errors.append(f"缺少文档：{doc.relative_to(ROOT)}")
            continue
        for lineno, line in enumerate(read(doc).splitlines(), start=1):
            if "docs-check: ignore" in line:
                continue
            for ref in re.findall(r"\b([MP]\d+)\b", line):
                if ref not in milestones:
                    errors.append(
                        f"{doc.relative_to(ROOT)}:{lineno}：引用 {ref}，"
                        f"但 docs/status.md 里没有这个编号"
                    )


def check_contradictions(milestones: dict[str, str], errors: list[str]) -> None:
    """已完成的里程碑，不得被其他文档说成未实现/实现中。"""
    done = {name for name, status in milestones.items() if status == "completed"}
    for doc in OTHER_DOCS:
        if not doc.exists():
            continue
        for lineno, line in enumerate(read(doc).splitlines(), start=1):
            if "docs-check: ignore" in line:
                continue
            refs = set(re.findall(r"\b([MP]\d+)\b", line))
            hits = [w for w in CONTRADICTION_WORDS if w in line]
            if not hits:
                continue
            for ref in sorted(refs & done):
                errors.append(
                    f"{doc.relative_to(ROOT)}:{lineno}：{ref} 在 docs/status.md 中标为 "
                    f"completed，这里却说「{hits[0]}」"
                )


def check_hardcoded_numbers(errors: list[str]) -> None:
    docs = [STATUS_FILE, *OTHER_DOCS]
    for doc in docs:
        if not doc.exists():
            continue
        for lineno, line in enumerate(read(doc).splitlines(), start=1):
            if "docs-check: ignore" in line:
                continue
            match = HARDCODED.search(line)
            if match:
                errors.append(
                    f"{doc.relative_to(ROOT)}:{lineno}：出现会过期的硬编码数字 "
                    f"「{match.group(0)}」；改成指向代码或命令"
                )


def check_referenced_paths(errors: list[str]) -> None:
    docs = [STATUS_FILE, *OTHER_DOCS]
    checked: set[tuple[str, str]] = set()
    for doc in docs:
        if not doc.exists():
            continue
        for lineno, line in enumerate(read(doc).splitlines(), start=1):
            for rel in PATH_LIKE.findall(line) + SCRIPT_LIKE.findall(line):
                if "*" in rel or "..." in rel:
                    continue
                key = (str(doc.relative_to(ROOT)), rel)
                if key in checked:
                    continue
                checked.add(key)
                if not (ROOT / rel).exists():
                    errors.append(
                        f"{doc.relative_to(ROOT)}:{lineno}：引用不存在的路径 `{rel}`"
                    )


def check_referenced_commands(errors: list[str]) -> None:
    docs = [STATUS_FILE, *OTHER_DOCS]
    for doc in docs:
        if not doc.exists():
            continue
        for lineno, line in enumerate(read(doc).splitlines(), start=1):
            for module in MODULE_LIKE.findall(line):
                rel = Path("backend/src") / (module.replace(".", "/") + ".py")
                if not (ROOT / rel).exists():
                    errors.append(
                        f"{doc.relative_to(ROOT)}:{lineno}：命令引用的模块 "
                        f"`{module}` 不存在（应为 {rel.as_posix()}）"
                    )


def check_ci_commands_documented(errors: list[str]) -> None:
    """CI 的安装/测试命令必须在 SETUP.md 中可查。"""
    if not CI_FILE.exists() or not SETUP_FILE.exists():
        errors.append("缺少 .github/workflows/ci.yml 或 docs/SETUP.md")
        return
    setup = read(SETUP_FILE)
    for lineno, line in enumerate(read(CI_FILE).splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("run:"):
            continue
        command = stripped[len("run:") :].strip()
        if not command.startswith(CI_COMMAND_PREFIXES):
            continue
        # SETUP 里可以省略 -q 这类静音参数
        core = command.replace(" -q", "")
        if core not in setup:
            errors.append(
                f"{CI_FILE.relative_to(ROOT)}:{lineno}：CI 执行 `{command}`，"
                f"但 docs/SETUP.md 里找不到 `{core}`"
            )


def last_checked_problem(text: str, *, today: date | None = None) -> str | None:
    """「最后核对」是不是落后于正文日期：有问题返回可行动的描述，没问题返回 None。

    纯函数（只看传入的文本）—— `--selftest` 用合成文本验证它真的能红、也能绿。
    只用**不晚于今天**的日期算「已记到」：正文里可能有路线图/迁移通知那类未来日期
    （例如「ubuntu-latest 2026-10-19 起迁移」），拿它们当「已记到」会立刻变成假警报。
    """
    declared = LAST_CHECKED.search(text)
    if declared is None:
        return "找不到「最后核对：YYYY-MM-DD」字段（读者靠它判断状态有多新）"
    try:
        declared_date = date.fromisoformat(declared.group(1))
    except ValueError:
        return f"「最后核对」不是合法日期：{declared.group(1)}"

    limit = today or date.today()
    latest: tuple[date, str, int] | None = None
    for match in DATE_LITERAL.finditer(text):
        raw = match.group(1)
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            continue
        if parsed > limit:
            continue  # 未来日期（路线图/迁移通知）：不算「已经记到」
        if latest is None or parsed > latest[0]:
            latest = (parsed, raw, text.count("\n", 0, match.start()) + 1)
    if latest is None:  # 防御：头顶那个合法日期一定命中过，正常到不了这里
        return "正文里找不到任何日期，无法判断「最后核对」是否过期"
    if declared_date < latest[0]:
        return (
            f"顶部「最后核对」= {declared.group(1)}，但正文已记到 {latest[1]}"
            f"（第 {latest[2]} 行）——请把「最后核对」更新到 >= {latest[1]}"
        )
    return None


def check_last_checked(errors: list[str]) -> None:
    """把 last_checked_problem 接到 docs/status.md 上。"""
    problem = last_checked_problem(read(STATUS_FILE))
    if problem:
        errors.append(f"{STATUS_FILE.relative_to(ROOT)}：{problem}")


def selftest() -> int:
    """离线自检：这条检查必须能正确变红、也能变绿（CI 的 docs 任务每次都会跑）。

    为什么要它：一个永远返回「没问题」的检查等于纸面能力。这里用合成文本把
    「过期 / 新鲜 / 只有未来日期 / 缺字段 / 非法日期 / 正文无日期」六种情况都钉住。
    """
    today = date(2026, 10, 3)
    cases: list[tuple[str, str, bool]] = [
        (
            "过期：顶部 2026-09-12 < 正文 2026-10-03",
            "最后核对：2026-09-12（`main` 分支）。\n\n- 2026-10-03 做完 X。\n",
            True,
        ),
        (
            "新鲜：顶部与正文同为 2026-10-03",
            "最后核对：2026-10-03。\n\n- 2026-10-03 做完 X。\n",
            False,
        ),
        (
            "正文只有未来日期（路线图/迁移通知）→ 不算「已记到」",
            "最后核对：2026-10-03。\n\n- ubuntu-latest 2026-10-19 起迁移到 Ubuntu 26。\n",
            False,
        ),
        ("缺「最后核对」字段", "没有这个字段。\n\n- 2026-10-03 做完 X。\n", True),
        ("「最后核对」不是合法日期", "最后核对：2026-13-99。\n\n- 2026-10-03 做完 X。\n", True),
        (
            "正文只有更早的日期 → 不该误报",
            "最后核对：2026-10-03。\n\n- 2026-09-30 做完 X。\n",
            False,
        ),
    ]
    failures = 0
    for label, text, expect_problem in cases:
        problem = last_checked_problem(text, today=today)
        ok = (problem is not None) == expect_problem
        if not ok:
            failures += 1
        state = "ok" if ok else "FAIL"
        detail = f"（{problem}）" if problem else ""
        print(
            f"  [{state}] {label} → 期望{'红' if expect_problem else '绿'}，"
            f"实际{'红' if problem else '绿'}{detail}"
        )
    summary = "全部符合预期" if not failures else f"{failures} 个不符合预期"
    print(f"check_docs --selftest：{len(cases)} 个场景，{summary}")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    if argv and "--selftest" in argv:
        return selftest()
    errors: list[str] = []
    milestones = parse_status_milestones()
    check_status_values(milestones, errors)
    check_milestone_refs(milestones, errors)
    check_contradictions(milestones, errors)
    check_hardcoded_numbers(errors)
    check_referenced_paths(errors)
    check_referenced_commands(errors)
    check_ci_commands_documented(errors)
    check_last_checked(errors)

    if errors:
        print("文档一致性检查未通过：")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(f"文档一致性检查通过（{len(milestones)} 个里程碑条目）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
