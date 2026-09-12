"""文档一致性检查（stdlib only，不需要安装依赖）。

检查四类会真实造成伤害的文档漂移：

1. 里程碑状态：`docs/status.md` 是唯一事实源；其他文档不得声明与之矛盾的状态。
2. 硬编码数字：测试数 / 事件数 / 表数这类会迅速过期的数字不允许写进文档。
3. 被引用的路径必须真实存在（文档里的 `docs/...`、`backend/...` 等）。
4. 被引用的命令必须真实存在（`scripts/*.py`、`python -m agent.x.y` 等）。
5. CI 里执行的安装/测试命令，必须在 `docs/SETUP.md` 里能找到——否则
   「本地照着 SETUP 装、CI 装另一套」的漂移会重新出现。

用法：python scripts/check_docs.py
退出码：0 = 通过；1 = 存在漂移。

需要豁免某一行时，在该行加注释 `docs-check: ignore` 并写清理由。
"""

from __future__ import annotations

import re
import sys
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
]

VALID_STATUS = {"completed", "partial", "active", "planned"}

MILESTONE_HEADING = re.compile(r"^###\s+((?:M|P)\d+)\s+—")
STATUS_LINE = re.compile(r"^\s*-\s*\*\*Status：\*\*\s*(\w+)")

CONTRADICTION_WORDS = ("未实现", "实现中", "待实现", "尚未实现", "规划中")
HARDCODED = re.compile(r"\d+\s*(?:个|类|种|张|条)?\s*(?:测试|事件类型|事件|表)")
PATH_LIKE = re.compile(
    r"`((?:docs|backend|frontend|scripts)/[A-Za-z0-9_./\-*]+?"
    r"\.(?:md|py|ts|vue|json|jsonl|toml|ps1|yml|yaml|css))"
)
SCRIPT_LIKE = re.compile(r"`(scripts/[A-Za-z0-9_./\-]+\.(?:py|ps1))")
MODULE_LIKE = re.compile(r"python\s+-m\s+(agent(?:\.[a-z_]+)+)")

CI_FILE = ROOT / ".github" / "workflows" / "ci.yml"
SETUP_FILE = ROOT / "docs" / "SETUP.md"
# CI 里这些前缀的 run 命令属于「安装/测试」，必须与 SETUP 对齐
CI_COMMAND_PREFIXES = ("uv ", "npm ", "npx ", "cargo ", "python scripts/")


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


def main() -> int:
    errors: list[str] = []
    milestones = parse_status_milestones()
    check_status_values(milestones, errors)
    check_milestone_refs(milestones, errors)
    check_contradictions(milestones, errors)
    check_hardcoded_numbers(errors)
    check_referenced_paths(errors)
    check_referenced_commands(errors)
    check_ci_commands_documented(errors)

    if errors:
        print("文档一致性检查未通过：")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(f"文档一致性检查通过（{len(milestones)} 个里程碑条目）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
