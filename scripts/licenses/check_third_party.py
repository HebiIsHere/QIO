#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第三方组件声明覆盖率校验（stdlib only、离线、CI 可跑）。

它回答的问题只有一个，但必须是可判定的：**锁文件里的组件，声明里一个都不能少。**
没有这条护栏，"补齐合规缺口"就会在下次加依赖时重新破掉 —— 声明不会自己长大。

检查项：
1. 三套锁文件里的每个组件都在 licenses/third-party.json 里；
2. 清单里没有 UNKNOWN 许可证（认不出来就必须人工确认，而不是蒙混过去）；
3. 每个被引用的许可证标识都有全文（licenses/texts/<id>.txt，且不是空壳）；
4. THIRD-PARTY-NOTICES.md 里每个组件都出现过（防止 JSON 改了、声明没重生成）；
5. THIRD-PARTY-NOTICES.md 里真的嵌了每份全文（防止正文与文本库脱钩）；
6. 每个组件都有版权行或上游出处（两者都没有 = 无法归属，判红）；
7. 仓库自带 LICENSE 与 licenses/README.md 存在。

用法
    python scripts/licenses/check_third_party.py            # 校验当前工作区
    python scripts/licenses/check_third_party.py --selftest # 用合成数据证明它真能红/能绿

退出码：0 = 通过；1 = 有漂移。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BQ = chr(96)
FENCE = BQ * 3

UV_LOCK = Path("backend") / "uv.lock"
NPM_LOCK = Path("frontend") / "package-lock.json"
CARGO_LOCK = Path("frontend") / "src-tauri" / "Cargo.lock"
MANIFEST = Path("licenses") / "third-party.json"
TEXTS = Path("licenses") / "texts"
NOTICES = Path("THIRD-PARTY-NOTICES.md")
PROJECT_LICENSE = Path("LICENSE")

OWN_PACKAGES = {("python", "qio"), ("npm", "qio-frontend"), ("rust", "qio")}
MIN_TEXT_BYTES = 200


def _configure_output() -> None:
    """报告层必须能在任何控制台编码下工作（英文 Windows 的 cp1252 也不能崩）。"""
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


def parse_uv_lock(root: Path) -> list:
    path = root / UV_LOCK
    if not path.exists():
        return []
    out = []
    for block in path.read_text(encoding="utf-8").split("[[package]]")[1:]:
        name = re.search(r'name = "([^"]+)"', block)
        version = re.search(r'version = "([^"]+)"', block)
        if not (name and version) or 'editable = "."' in block:
            continue
        out.append({"eco": "python", "name": name.group(1), "version": version.group(1)})
    return out


def parse_package_lock(root: Path) -> list:
    path = root / NPM_LOCK
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for key, entry in (data.get("packages") or {}).items():
        if not key.startswith("node_modules/"):
            continue
        version = str(entry.get("version") or "")
        if version:
            out.append({"eco": "npm", "name": key.split("node_modules/")[-1], "version": version})
    return out


def parse_cargo_lock(root: Path) -> list:
    path = root / CARGO_LOCK
    if not path.exists():
        return []
    out = []
    for block in path.read_text(encoding="utf-8").split("[[package]]")[1:]:
        name = re.search(r'name = "([^"]+)"', block)
        version = re.search(r'version = "([^"]+)"', block)
        if name and version:
            out.append({"eco": "rust", "name": name.group(1), "version": version.group(1)})
    return out


def locked_components(root: Path) -> list:
    seen = {}
    for item in [*parse_uv_lock(root), *parse_package_lock(root), *parse_cargo_lock(root)]:
        if (item["eco"], item["name"]) in OWN_PACKAGES:
            continue
        seen[item["eco"] + ":" + item["name"] + "@" + item["version"]] = item
    return [seen[key] for key in sorted(seen)]


def license_ids(expression: str) -> list:
    """表达式 -> 需要附全文的标识：括号只是分组，老式 "MIT/Apache-2.0" 等价于 OR。"""
    ids = []
    cleaned = (expression or "").replace("(", " ").replace(")", " ")
    for token in re.split(r"\s+(?:OR|AND|WITH)\s+|\s*/\s*", cleaned):
        token = token.strip()
        if token and token not in ids:
            ids.append(token)
    return ids


def run_checks(root: Path) -> list:
    problems = []

    if not (root / PROJECT_LICENSE).exists():
        problems.append("缺少 " + str(PROJECT_LICENSE) + "：QIO 自身的授权状态必须写清楚")
    readme = root / "licenses" / "README.md"
    if not readme.exists():
        problems.append("缺少 licenses/README.md：声明怎么生成、怎么校验要写在仓库里")

    manifest_path = root / MANIFEST
    if not manifest_path.exists():
        problems.append("缺少 " + str(MANIFEST) + "：先跑 python scripts/licenses/build_third_party.py")
        return problems
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        problems.append(str(MANIFEST) + " 解析失败：" + str(exc))
        return problems

    entries = manifest.get("components") or []
    known = {}
    for entry in entries:
        key = str(entry.get("eco")) + ":" + str(entry.get("name")) + "@" + str(entry.get("version"))
        known[key] = entry

    wanted = locked_components(root)
    if not wanted:
        problems.append("三套锁文件一个都没读到：检查路径是否变了")

    missing = []
    for item in wanted:
        key = item["eco"] + ":" + item["name"] + "@" + item["version"]
        if key not in known:
            missing.append(key)
    if missing:
        problems.append(
            "声明里缺少 " + str(len(missing)) + " 个锁文件组件（重新跑 build_third_party.py 并提交）："
            + "、".join(missing[:10])
            + ("…" if len(missing) > 10 else "")
        )

    all_entries = [*entries, *(manifest.get("assets") or [])]
    unknown = [e for e in all_entries if str(e.get("license") or "") in ("", "UNKNOWN")]
    if unknown:
        problems.append(
            "有组件没解析出许可证：" + "、".join(str(e.get("name")) for e in unknown[:10])
            + "（请在 licenses/overrides.json 的 manual 段人工确认）"
        )

    unowned = [e for e in all_entries if not e.get("copyright") and not e.get("attribution")]
    if unowned:
        problems.append(
            "有组件既没有版权行也没有上游出处：" + "、".join(str(e.get("name")) for e in unowned[:10])
        )

    referenced = set()
    for entry in all_entries:
        referenced.update(license_ids(str(entry.get("license") or "")))
    referenced.discard("")
    for license_id in sorted(referenced):
        text_path = root / TEXTS / (license_id + ".txt")
        if not text_path.exists():
            problems.append("缺少许可证全文 licenses/texts/" + license_id + ".txt")
        elif text_path.stat().st_size < MIN_TEXT_BYTES:
            problems.append("licenses/texts/" + license_id + ".txt 内容过短（不足 " + str(MIN_TEXT_BYTES) + " 字节）")

    notices_path = root / NOTICES
    if not notices_path.exists():
        problems.append("缺少 " + str(NOTICES))
        return problems
    notices = notices_path.read_text(encoding="utf-8")

    not_in_notices = []
    for entry in all_entries:
        token = str(entry.get("name")) + "@" + str(entry.get("version"))
        if token not in notices:
            not_in_notices.append(token)
    if not_in_notices:
        problems.append(
            "声明正文里没有这些组件（清单与声明不同步）：" + "、".join(not_in_notices[:10])
            + ("…" if len(not_in_notices) > 10 else "")
        )

    for license_id in sorted(referenced):
        text_path = root / TEXTS / (license_id + ".txt")
        if not text_path.exists():
            continue
        body = text_path.read_text(encoding="utf-8").strip().splitlines()
        probe = next((line.strip() for line in body if len(line.strip()) > 40), "")
        if probe and probe not in notices:
            problems.append("声明正文里没有嵌入 " + license_id + " 的全文（文本库与声明脱钩）")

    return problems


# ---------------------------------------------------------------------------
# 自检：合成一份最小工作区，逐条证明检查真的会红
# ---------------------------------------------------------------------------

FIXTURE_UV_LOCK = """version = 1

[[package]]
name = "alpha"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "qio"
version = "0.0.0"
source = { editable = "." }
"""

FIXTURE_NPM_LOCK = json.dumps(
    {
        "name": "qio-frontend",
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "qio-frontend", "version": "0.0.0"},
            "node_modules/beta": {"version": "2.0.0", "license": "MIT"},
        },
    },
    indent=2,
)

FIXTURE_CARGO_LOCK = """version = 4

[[package]]
name = "gamma"
version = "3.0.0"

[[package]]
name = "qio"
version = "0.0.0"
"""

FIXTURE_MIT = "MIT License\n\nCopyright (c) 2026 Example\n\n" + ("Permission is hereby granted. " * 12) + "\n"


def _fixture_manifest() -> dict:
    return {
        "components": [
            {"eco": "python", "name": "alpha", "version": "1.0.0", "license": "MIT", "copyright": "Copyright (c) 2026 Alpha"},
            {"eco": "npm", "name": "beta", "version": "2.0.0", "license": "MIT", "copyright": "Copyright (c) 2026 Beta"},
            {"eco": "rust", "name": "gamma", "version": "3.0.0", "license": "MIT", "copyright": "Copyright (c) 2026 Gamma"},
        ],
        "assets": [],
    }


def _fixture_notices() -> str:
    return "\n".join(
        [
            "# 第三方声明",
            "",
            "- alpha@1.0.0（python）",
            "- beta@2.0.0（npm）",
            "- gamma@3.0.0（rust）",
            "",
            "## 许可证全文",
            "",
            "### MIT",
            "",
            FENCE,
            FIXTURE_MIT.rstrip("\n"),
            FENCE,
            "",
        ]
    )


def _write_fixture(root: Path) -> None:
    (root / "backend").mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "src-tauri").mkdir(parents=True, exist_ok=True)
    (root / "licenses" / "texts").mkdir(parents=True, exist_ok=True)
    (root / UV_LOCK).write_text(FIXTURE_UV_LOCK, encoding="utf-8")
    (root / NPM_LOCK).write_text(FIXTURE_NPM_LOCK, encoding="utf-8")
    (root / CARGO_LOCK).write_text(FIXTURE_CARGO_LOCK, encoding="utf-8")
    (root / MANIFEST).write_text(json.dumps(_fixture_manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    (root / TEXTS / "MIT.txt").write_text(FIXTURE_MIT, encoding="utf-8")
    (root / NOTICES).write_text(_fixture_notices(), encoding="utf-8")
    (root / PROJECT_LICENSE).write_text("Test License\n", encoding="utf-8")
    (root / "licenses" / "README.md").write_text("# licenses\n", encoding="utf-8")


def _read(root: Path, rel: Path) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _write(root: Path, rel: Path, text: str) -> None:
    (root / rel).write_text(text, encoding="utf-8")


def _mutate_extra_lock_component(root: Path) -> None:
    _write(root, UV_LOCK, _read(root, UV_LOCK) + '\n[[package]]\nname = "delta"\nversion = "4.0.0"\n')


def _mutate_unknown_license(root: Path) -> None:
    data = json.loads(_read(root, MANIFEST))
    data["components"][0]["license"] = "UNKNOWN"
    _write(root, MANIFEST, json.dumps(data, ensure_ascii=False, indent=2))


def _mutate_missing_text(root: Path) -> None:
    (root / TEXTS / "MIT.txt").unlink()


def _mutate_stale_notices(root: Path) -> None:
    _write(root, NOTICES, _read(root, NOTICES).replace("- gamma@3.0.0（rust）\n", ""))


def _mutate_no_attribution(root: Path) -> None:
    data = json.loads(_read(root, MANIFEST))
    data["components"][1]["copyright"] = ""
    _write(root, MANIFEST, json.dumps(data, ensure_ascii=False, indent=2))


def _mutate_no_license_file(root: Path) -> None:
    (root / PROJECT_LICENSE).unlink()


def selftest() -> int:
    cases = [
        ("干净工作区应当是绿的", None, None),
        ("锁文件多了组件但声明没跟上", _mutate_extra_lock_component, "锁文件组件"),
        ("出现 UNKNOWN 许可证", _mutate_unknown_license, "没解析出许可证"),
        ("许可证全文丢失", _mutate_missing_text, "缺少许可证全文"),
        ("声明正文与清单脱钩", _mutate_stale_notices, "声明正文里没有这些组件"),
        ("组件既无版权行也无出处", _mutate_no_attribution, "既没有版权行也没有上游出处"),
        ("仓库缺 LICENSE", _mutate_no_license_file, "QIO 自身的授权状态"),
    ]
    failures = []
    passed = 0
    for label, mutate, expected in cases:
        root = Path(tempfile.mkdtemp(prefix="qio-lic-selftest-"))
        ok = False
        detail = ""
        try:
            _write_fixture(root)
            if mutate is not None:
                mutate(root)
            problems = run_checks(root)
            if expected is None:
                ok = not problems
                detail = problems[0] if problems else ""
            else:
                ok = any(expected in problem for problem in problems)
                detail = "；".join(problems) or "（没有报错）"
        finally:
            shutil.rmtree(root, ignore_errors=True)
        passed += 1 if ok else 0
        print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else " —— " + detail))
        if not ok:
            failures.append(label)
    if failures:
        print("自检 " + str(passed) + "/" + str(len(cases)) + " 通过")
        return 1
    print("自检 " + str(passed) + "/" + str(len(cases)) + " 通过：检查项既会绿也会红")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 QIO 第三方组件声明的覆盖率")
    parser.add_argument("--selftest", action="store_true", help="用合成工作区证明检查能红能绿")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    problems = run_checks(ROOT)
    if problems:
        print("第三方声明校验失败（" + str(len(problems)) + " 条）：")
        for problem in problems:
            print("  - " + problem)
        print("")
        print("修法：python scripts/licenses/build_third_party.py（必要时加 --refresh），然后提交 licenses/ 与 THIRD-PARTY-NOTICES.md")
        return 1

    manifest = json.loads((ROOT / MANIFEST).read_text(encoding="utf-8"))
    entries = [*(manifest.get("components") or []), *(manifest.get("assets") or [])]
    licenses = sorted({str(entry.get("license")) for entry in entries})
    locked = locked_components(ROOT)
    print("第三方声明校验通过。")
    print("  锁文件组件 " + str(len(locked)) + " 个；声明覆盖 " + str(len(entries)) + " 条；许可证 " + str(len(licenses)) + " 种")
    print("  声明正文：" + str(NOTICES) + "；全文库：" + str(TEXTS))
    return 0


if __name__ == "__main__":
    _configure_output()
    raise SystemExit(main())
