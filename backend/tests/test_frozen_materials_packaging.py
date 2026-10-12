# -*- coding: utf-8 -*-
"""P0 回归：材料解析模块必须真的进得了冻结产物（2026-10-12 漏包事故）。

事故经过：understanding.py 用 importlib.import_module("agent.interactive.materials")
取材料解析模块，PyInstaller 的静态分析**看不见**动态导入；构建脚本又没有
--hidden-import。于是 0.2.0 的 qio-backend.exe 里完全没有这个模块，每次理解都返回
materials=[] 与 honesty.materialsError="材料解析模块不可用：No module named
'agent.interactive.materials'"。源码、单元测试、CI 全绿 —— 只有对着打包产物才看得出来。

这个文件锁死三件事（任何一件丢了都会让同类漏包重现）：

1. 材料模块可导入，且 understanding.py 里保留一条**字面 import**（PyInstaller 静态分析
   看得见的那道保险），同时保留 importlib 兜底与「材料解析模块不可用」的诚实降级；
2. 构建侧两道保险都在：scripts/build_sidecar.ps1 的 --hidden-import 与
   backend/qio-backend.spec 的 hiddenimports；
3. 图片材料仍然是 image_unsupported（诚实口径），而且真的经过 understanding 的取用
   路径解析出来（honesty.materialsResolved=true、materials 非空）。

打包产物本身的检查不在这里（那需要真 exe）：
    python scripts/check_frozen_materials.py --exe <qio-backend.exe>
（静态解析 PYZ 模块清单 + 用临时 QIO_DATA_DIR 跑一次真链路。）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
UNDERSTANDING_PY = BACKEND_DIR / "src" / "agent" / "interactive" / "understanding.py"
MATERIALS_PY = BACKEND_DIR / "src" / "agent" / "interactive" / "materials.py"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_sidecar.ps1"
SPEC_FILE = BACKEND_DIR / "qio-backend.spec"
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_frozen_materials.py"
MODULE_NAME = "agent.interactive.materials"
BOARD = "board_frozen_packaging"


# --- 1. 模块本身与静态可见导入 ---------------------------------------------


def test_materials_module_is_importable():
    """材料解析模块必须存在、可导入、入口可调用（漏包事故的第一现场）。"""
    import importlib

    module = importlib.import_module(MODULE_NAME)
    assert callable(getattr(module, "resolve_materials", None))
    assert MATERIALS_PY.is_file(), f"找不到材料解析模块源码：{MATERIALS_PY}"


def test_understanding_keeps_literal_import_for_static_analysis():
    """understanding.py 里必须有一条**字面** import，不能只有 importlib 动态导入。"""
    source = UNDERSTANDING_PY.read_text(encoding="utf-8")
    literal = re.search(
        r"^\s*(?:import\s+agent\.interactive\.materials\b"
        r"|from\s+agent\.interactive\s+import\s+[\w, ]*\bmaterials\b)",
        source,
        re.MULTILINE,
    )
    assert literal is not None, (
        "understanding.py 缺少对 agent.interactive.materials 的字面 import："
        "PyInstaller 的静态分析看不见 importlib.import_module，漏包事故会重现"
        "（安装包里没有这个模块 → 每次理解 materials=[]）。"
    )
    # 动态兜底与诚实降级必须保留：模块缺失也不能让理解模块自己起不来。
    assert 'import_module("agent.interactive.materials")' in source
    assert "材料解析模块不可用：" in source

    import agent.interactive.understanding as understanding

    assert getattr(understanding, "_materials_static", None) is not None, (
        "understanding 的静态导入绑定是空的：材料模块在开发环境里就没导入成功"
    )


# --- 2. 构建侧的两道保险 ---------------------------------------------------


@pytest.mark.parametrize(
    "path,pattern,what",
    [
        (
            BUILD_SCRIPT,
            r'"--hidden-import"\s*,\s*"agent\.interactive\.materials"',
            'scripts/build_sidecar.ps1 的 pyiArgs 缺少 --hidden-import "agent.interactive.materials"',
        ),
        (
            SPEC_FILE,
            r"hiddenimports\s*=\s*\[[^\]]*'agent\.interactive\.materials'",
            "backend/qio-backend.spec 的 hiddenimports 缺少 agent.interactive.materials",
        ),
    ],
    ids=["build_sidecar.ps1", "qio-backend.spec"],
)
def test_build_declares_materials_hidden_import(path: Path, pattern: str, what: str):
    """构建脚本与 spec 都必须显式声明这个模块（注释里的提及不算数）。"""
    assert path.is_file(), f"找不到构建文件：{path}"
    text = path.read_text(encoding="utf-8")
    assert re.search(pattern, text, re.MULTILINE) is not None, (
        what + "：静态分析看不见动态导入，冻结产物会再次漏掉材料解析模块。"
    )


def test_frozen_self_check_script_exists():
    """自证脚本必须还在，并且盯着的就是同一个模块名。"""
    assert CHECK_SCRIPT.is_file(), f"找不到冻结产物自证脚本：{CHECK_SCRIPT}"
    text = CHECK_SCRIPT.read_text(encoding="utf-8")
    assert MODULE_NAME in text
    assert "QIO_DATA_DIR" in text and "materialsResolved" in text


# --- 3. 图片材料：诚实口径 + 真的经过 understanding 的取用路径 -------------


def _materials_state():
    from agent.interactive import models

    state = models.empty_state(BOARD)
    image = models.new_card("image", "", meta={"name": "照片.png"})
    code = models.new_card("code", "print('x')", meta={"language": "python"})
    state["cards"] = [image, code]
    return state, image, code


def test_resolve_materials_marks_image_unsupported(db_conn):
    """图片卡 → image_unsupported（不联网、临时 db、不声称看懂图片）。"""
    from agent.interactive import materials

    state, image, code = _materials_state()
    entries = materials.resolve_materials(db_conn, state=state, cards=list(state["cards"]))
    by_card = {entry["cardId"]: entry for entry in entries}
    assert set(by_card) == {image["id"], code["id"]}

    image_entry = by_card[image["id"]]
    assert image_entry["source"] == "image_unsupported"
    assert image_entry["read"] is False
    assert image_entry["text"] == ""
    # 原因必须**可读且说清是哪一种没交付**：视觉能力不可用、或这张卡还没有关联附件。
    # （2026-10-12 视觉能力接入后，「没有附件」不再笼统说成「没有视觉能力」）
    reason = image_entry["reason"]
    assert "视觉能力" in reason or "没有关联已保存的附件" in reason, reason
    # 对照：同一批里的 code 卡片是真的读到正文的 inline 材料
    assert by_card[code["id"]]["source"] == "inline"
    assert by_card[code["id"]]["text"] == "print('x')"


def test_understanding_context_resolves_materials(db_conn):
    """理解上下文必须通过真实取用路径拿到材料：honesty.materialsResolved=true。"""
    from agent.interactive import understanding

    state, image, code = _materials_state()
    record = understanding.request_understanding(
        db_conn, board_id=BOARD, submission_id="sub_frozen_packaging", snapshot=state
    )
    context = understanding.build_context(
        db_conn, board_id=BOARD, understanding_id=record["id"]
    )
    honesty = context["honesty"]
    assert honesty["materialsResolved"] is True, honesty
    assert honesty["materialsError"] == "", honesty

    materials_out = context["materials"]
    assert {entry["cardId"] for entry in materials_out} == {image["id"], code["id"]}
    image_entry = next(e for e in materials_out if e["cardId"] == image["id"])
    assert image_entry["source"] == "image_unsupported"
    code_entry = next(e for e in materials_out if e["cardId"] == code["id"])
    assert code_entry["source"] == "inline" and code_entry["text"] == "print('x')"
    assert context["materialIds"]
