"""行为符号级核对（整合轮 E）：两条来源分支对同一个文件的改动，在整合基线上是否都还在。

做法（对「两条来源分支都改过的共同文件」逐个执行）：
  1. 取 merge-base 版本、rel 侧版本、irm 侧版本、整合版本；
  2. 用行级 diff 取每一侧「新增的行」，从中抽出标识符（标识符级行为符号）与字符串字面量；
  3. 只保留「该侧引入、base 里没有」的符号 —— 这是这条来源线的行为增量；
  4. 在整合版本里核对：符号是否存在（给首次出现行号）、字面量是否逐字还在。

注意边界（诚实声明）：这是**名字级**核对，证明「这条线引入的符号没有整条消失」，
不证明语义没有变化；语义结论必须另有阅读 / 实测证据（见 docs/integration-review-20261011.md）。

用法：
  backend/.venv/Scripts/python.exe scripts/irm-int-verify/check_conflict_symbols.py \
      [--base 8fa8452] [--rel 4890870] [--irm 7158152] [--int HEAD] [--json out.json]
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
STOP = set(
    """const let var function return if else for while import from export default new await async type interface class extends implements enum
true false null undefined this that string number boolean void any unknown never as of in is not and or the with
then catch try finally throw typeof instanceof delete case switch break continue do yield static public private readonly
value values name names data item items index length map filter push push_back slice splice join split trim replace
props emit refs ref reactive computed watch onMounted defineProps defineEmits withDefaults store state""".split()
)


def git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{out.stderr.strip()}")
    return out.stdout


def show(rev: str, path: str) -> str:
    return git("show", f"{rev}:{path}")


def changed_files(rev_a: str, rev_b: str) -> set[str]:
    return {line for line in git("diff", "--name-only", rev_a, rev_b).splitlines() if line.strip()}


def tokens(text: str) -> set[str]:
    return {t for t in IDENT.findall(text) if len(t) >= 4 and t not in STOP}


def literals(text: str) -> set[str]:
    out = set()
    for m in re.finditer(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'', text):
        s = m.group(1) if m.group(1) is not None else m.group(2)
        if s and len(s) >= 6:
            out.add(s)
    return out


def added_lines(base: list[str], side: list[str]) -> list[str]:
    sm = difflib.SequenceMatcher(None, base, side, autojunk=False)
    out: list[str] = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            out.extend(side[j1:j2])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="8fa8452", help="两条来源分支的 merge base")
    ap.add_argument("--rel", default="4890870", help="可靠性线（fix/main-audit-reliability）")
    ap.add_argument("--irm", default="7158152", help="真实模型线（feat/interactive-real-model）")
    ap.add_argument("--int", default="HEAD", help="被检查的整合版本（默认 HEAD）")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    shared = sorted(changed_files(args.base, args.rel) & changed_files(args.base, args.irm))
    report: dict[str, object] = {
        "script": "scripts/irm-int-verify/check_conflict_symbols.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "worktree": str(ROOT),
        "base": args.base,
        "rel": args.rel,
        "irm": args.irm,
        "integrated": git("rev-parse", args.int).strip(),
        "shared_files": shared,
        "files": {},
        "totals": {"symbols": 0, "missing_symbols": 0, "literals": 0, "missing_literals": 0},
    }
    files_out: dict[str, object] = {}
    for path in shared:
        base = show(args.base, path)
        bL = base.split("\n")
        integ = show(args.int, path)
        gL = integ.split("\n")
        first: dict[str, int] = {}
        for i, line in enumerate(gL):
            for tok in set(IDENT.findall(line)):
                first.setdefault(tok, i + 1)
        integ_lit = literals(integ)
        base_tok = tokens(base)
        entry: dict[str, object] = {}
        for label, rev in (("rel", args.rel), ("irm", args.irm)):
            side = show(rev, path)
            adds = "\n".join(added_lines(bL, side.split("\n")))
            new_syms = sorted(tokens(adds) - base_tok)
            sym_lines = {s: first.get(s) for s in new_syms}
            miss_syms = sorted(s for s, ln in sym_lines.items() if ln is None)
            lit_lines = {lit: (integ.find(lit) >= 0) for lit in sorted(literals(adds))}
            miss_lits = sorted(lit for lit, ok in lit_lines.items() if not ok)
            entry[label] = {
                "new_symbols": len(new_syms),
                "missing_symbols": miss_syms,
                "symbol_lines": sym_lines,
                "missing_literals": miss_lits,
            }
            report["totals"]["symbols"] += len(new_syms)
            report["totals"]["missing_symbols"] += len(miss_syms)
            report["totals"]["literals"] += len(lit_lines)
            report["totals"]["missing_literals"] += len(miss_lits)
        files_out[path] = entry
        print(f"{path}")
        for label in ("rel", "irm"):
            e = entry[label]
            print(f"  {label}: 新增符号 {e['new_symbols']}，整合基线里缺失 {len(e['missing_symbols'])}"
                  f"，新增字面量缺失 {len(e['missing_literals'])}")
            for lit in e["missing_literals"]:
                print(f"      [字面量缺失] {lit[:100]}")
    report["files"] = files_out
    t = report["totals"]
    print(f"\n合计：共同文件 {len(shared)}；新增符号 {t['symbols']}（缺失 {t['missing_symbols']}）；"
          f"新增字面量 {t['literals']}（缺失 {t['missing_literals']}）")
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"证据已写入 {out}")
    return 1 if (t["missing_symbols"] or t["missing_literals"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
