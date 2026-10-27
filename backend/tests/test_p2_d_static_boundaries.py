# -*- coding: utf-8 -*-
"""D 独立静态审阅（契约 §12.1 / §12.4 / §12.5 / §12.6）：边界与接线纪律。

覆盖静态规则（不依赖运行时）：
* 模型可见固定文案只在 prompts.py（新维护模块不得自带「我正在…」提示词）；
* 维护执行 / 恢复模块只使用冻结类型与 prompts 入口，不另建一套状态字符串；
* 旧维护路径（scan_contradictions / run_dreaming / 否定词降置信度）不再被
  services/maintenance.py 的 run_once 调用，也不能被新维护模块当作回退；
* 工具清理 / 记录保留 / 自动化建议仍在 run_once 里且与知识维护分开；
* 迁移 46/47 的模块文件存在且导出冻结形状 MIGRATION = (号, [SQL])；
* 前端存在「记忆待确认」入口与「后台整理」来源筛选，筛选按 API 标签匹配。

基线说明：阶段二实现未合入时这些检查会**失败**（不是 skip），失败信息写明契约条款。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p2_d_static_boundaries.py -q
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

CLAUSE = "契约 §12.1/§12.4/§12.5/§12.6"
ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = ROOT / "backend" / "src" / "agent"
FRONTEND_SRC = ROOT / "frontend" / "src"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"{CLAUSE}：契约要求的 {name} 不可导入；阶段二实现尚未合入时属于「未验证」：{exc!r}",
            pytrace=False,
        )


def _python_files() -> list[Path]:
    return sorted(BACKEND_SRC.rglob("*.py"))


def _require_file(path: Path, what: str) -> Path:
    if not path.exists():
        pytest.fail(f"{CLAUSE}：缺少 {what}（{path.relative_to(ROOT)}）", pytrace=False)
    return path


# ---------------------------------------------------------------------------
# 提示词单文件 / 冻结类型单一来源
# ---------------------------------------------------------------------------


def test_maintenance_prompt_text_lives_only_in_prompts_py():
    """§12.1/§12.2：新增固定模型文案只进 prompts.py（不得散落在维护模块里）。"""
    phrases = ("我正在独立维护自己的知识记忆", "我正在处理用户对一个独立记忆问题")
    offenders: list[str] = []
    for path in _python_files():
        if path.name == "prompts.py" and path.parent.name == "agent":
            continue
        text = _read(path)
        for phrase in phrases:
            if phrase in text:
                offenders.append(f"{path.relative_to(ROOT)}: {phrase}")
    assert not offenders, (
        f"{CLAUSE}：冻结提示词正文出现在了 prompts.py 以外的文件：{offenders}")


def test_new_prompt_literals_are_not_embedded_as_system_messages():
    """§12.1：新维护模块不得自建含「我正在」的自述提示词（应引用 prompts 冻结键）。"""
    offenders: list[str] = []
    for name in ("runner.py", "actions.py", "questions.py", "recovery.py", "store.py"):
        path = BACKEND_SRC / "maintenance" / name
        if not path.exists():
            continue
        text = _read(path)
        if "我正在" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        f"{CLAUSE}：维护模块里出现自述提示词正文（应使用 PROMPTS 冻结键）：{offenders}")


def test_maintenance_modules_use_frozen_prompts_and_types():
    """§12.2/§12.4：执行入口引用冻结提示词与类型，不另造状态 / 文案。

    维护批次与独立回答是两个任务：维护提示词由 runner 引用，回答提示词由
    questions 引用；两者都必须经 prompts.structured_task_system 与冻结校验入口。
    """
    runner = _read(_require_file(BACKEND_SRC / "maintenance" / "runner.py",
                                 "B 的执行入口 runner.py"))
    assert "MAINTENANCE_SYSTEM" in runner, (
        f"{CLAUSE}：runner.py 未引用冻结提示词 MAINTENANCE_SYSTEM")
    assert "structured_task_system" in runner, (
        f"{CLAUSE}：runner.py 必须经 prompts.structured_task_system 组装结构化任务"
        "（契约 §2 公共结构化规则）")
    assert "validate_maintenance_output" in runner, (
        f"{CLAUSE}：runner.py 必须使用冻结校验入口 validate_maintenance_output")
    questions = _read(_require_file(BACKEND_SRC / "maintenance" / "questions.py",
                                    "B 的独立回答 questions.py"))
    assert "MAINTENANCE_ANSWER_SYSTEM" in questions, (
        f"{CLAUSE}：questions.py 未引用冻结提示词 MAINTENANCE_ANSWER_SYSTEM")
    assert "validate_resolution_output" in questions, (
        f"{CLAUSE}：questions.py 必须使用冻结校验入口 validate_resolution_output")
    models = _load("agent.maintenance.models")
    assert models is not None


def test_api_route_module_uses_frozen_read_model():
    """§12.5：API 路由读取模型来自 maintenance.models（不另造状态字符串）。"""
    route = _require_file(
        BACKEND_SRC / "api" / "memory_pending_routes.py", "C 的路由模块 memory_pending_routes.py")
    text = _read(route)
    assert "maintenance.models" in text or "from agent.maintenance import models" in text, (
        f"{CLAUSE}：memory_pending_routes.py 必须使用 maintenance.models 的冻结读取模型")
    assert "PendingItemView" in text, (
        f"{CLAUSE}：memory_pending_routes.py 必须返回 PendingItemView 形状")
    for route_path in ("/api/memory/pending", "/api/memory/maintenance/batches",
                       "/api/memory/maintenance/restore"):
        assert route_path in text, f"{CLAUSE}：路由模块缺少 {route_path}"


# ---------------------------------------------------------------------------
# 旧维护路径退休
# ---------------------------------------------------------------------------


def _function_body(text: str, name: str, *, indent: int = 4) -> str:
    pattern = re.compile(rf"^(\s*)(?:async\s+)?def\s+{re.escape(name)}\s*\(", re.MULTILINE)
    match = pattern.search(text)
    if match is None:
        return ""
    start = match.start()
    prefix = re.compile(rf"^\s{{0,{indent - 1}}}(?:async\s+)?def\s", re.MULTILINE)
    nxt = prefix.search(text, match.end())
    return text[start: nxt.start() if nxt else len(text)]


def test_old_maintenance_paths_are_retired_from_run_once():
    """§12.6：run_once 里退休 scan_contradictions（否定词降置信度）与 run_dreaming（旧 revoke）。"""
    path = _require_file(BACKEND_SRC / "services" / "maintenance.py", "既有维护服务")
    text = _read(path)
    body = _function_body(text, "run_once")
    assert body, f"{CLAUSE}：找不到 services/maintenance.py 的 run_once"
    # 只禁「真正的调用」：注释里说明为什么退休不算（退休说明本身是审计线索）。
    code = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    for retired in ("scan_contradictions", "run_dreaming"):
        assert not re.search(rf"await\s+{retired}\s*\(", code), (
            f"{CLAUSE}：run_once 仍在调用 {retired}（旧否定词降置信度 / revoke 路径必须退休）")
    for kept in ("prune_tool_outputs", "prune_tool_records", "mine_tool_candidates"):
        assert kept in code, (
            f"{CLAUSE}：run_once 必须保留 {kept}（工具清理 / 记录保留 / 自动化建议与知识维护分开）")


def test_new_maintenance_modules_do_not_reuse_old_paths():
    """§12.4：新维护模块不得把旧 revoke / 否定词降置信度路径当成回退。"""
    forbid = ("scan_contradictions", "run_dreaming", "NEGATION_WORDS", "CONFIDENCE_STEP",
              "from agent.services.maintenance import")
    offenders: list[str] = []
    for path in sorted((BACKEND_SRC / "maintenance").rglob("*.py")):
        if path.name == "models.py":
            continue
        text = _read(path)
        for token in forbid:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")
    route = BACKEND_SRC / "api" / "memory_pending_routes.py"
    if route.exists():
        text = _read(route)
        for token in forbid:
            if token in text:
                offenders.append(f"{route.relative_to(ROOT)}: {token}")
    assert not offenders, (
        f"{CLAUSE}：新维护 / API 模块复用了旧 revoke / 否定词路径：{offenders}")


def test_knowledge_maintenance_is_wired_and_turn_hook_exists():
    """§12.6：AppContext 暴露 knowledge_maintenance / maintenance_runner；成功用户
    turn 结束钩子在 turn_orchestrator；不得留「模块不可用」的静默回退。"""
    app_text = _read(_require_file(BACKEND_SRC / "services" / "app.py", "services/app.py"))
    assert "self.knowledge_maintenance" in app_text, (
        f"{CLAUSE}：AppContext 未暴露 knowledge_maintenance（Lead 接线缺失）")
    assert "self.maintenance_runner" in app_text, (
        f"{CLAUSE}：AppContext 未暴露 maintenance_runner（Lead 接线缺失）")
    assert "knowledge maintenance modules not available yet" not in app_text, (
        f"{CLAUSE}：app.py 仍保留「阶段二模块未合入」的 ImportError 静默回退，整合后必须删除")
    orchestrator = _read(
        _require_file(BACKEND_SRC / "services" / "turn_orchestrator.py", "turn_orchestrator.py"))
    assert "note_user_turn_completed" in orchestrator, (
        f"{CLAUSE}：turn_orchestrator 未在真实成功用户 turn 结束后调用 "
        "note_user_turn_completed（按 turn_id 去重由调度器负责）")
    assert "knowledge_maintenance" in orchestrator, (
        f"{CLAUSE}：turn_orchestrator 缺少 knowledge_maintenance 存在性判断（替身 ctx 兼容）")


# ---------------------------------------------------------------------------
# 迁移模块形状（§12.7）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_name", "number"),
    [("agent.storage.migration_p2_a", 46), ("agent.storage.migration_p2_b", 47)],
)
def test_migration_modules_export_frozen_shape(module_name, number):
    module = _load(module_name)
    migration = getattr(module, "MIGRATION", None)
    assert isinstance(migration, tuple) and len(migration) == 2, (
        f"{CLAUSE}：{module_name} 必须导出 MIGRATION = (号, [SQL...])，实际 {migration!r}")
    assert int(migration[0]) == number, (
        f"{CLAUSE}：{module_name} 的迁移号必须是 {number}，实际 {migration[0]}")
    statements = list(migration[1])
    assert statements, f"{CLAUSE}：{module_name} 的迁移没有 SQL 语句"
    for stmt in statements:
        assert str(stmt).strip(), f"{CLAUSE}：{module_name} 的迁移含空语句"


# ---------------------------------------------------------------------------
# 前端入口 / 来源筛选（§12.5）
# ---------------------------------------------------------------------------


def test_frontend_has_memory_pending_entry():
    """§12.5 / 改版 IA：前端必须有「待处理」「整理记录」入口，并按真实 kind 区分事项。

    集成阶段（2026-10-27）把基线单体的 `MemoryPendingPanel.vue` 拆成两个组件：
    - `PendingItems.vue`：待处理（知识建议 / 整理问题 / 实体更新候选，来源是两个真实接口）；
    - `MaintenanceRecords.vue`：整理记录（批次、受影响条目、撤回/恢复）。
    入口由 A 的 `ManagementArea.vue` 挂载（由 `PlanetView.vue` 引入），因此断言口径随结构
    迁移，而不是放宽：仍要求入口真实存在于界面结构里、按 API 返回的 kind 区分事项、
    动作集合来自服务端返回的 `actions`（不自行推断），整理记录仍走真实恢复接口。
    """
    planet = _read(_require_file(FRONTEND_SRC / "views" / "PlanetView.vue", "PlanetView.vue"))
    area_path = FRONTEND_SRC / "components" / "planet" / "ManagementArea.vue"
    area = _read(_require_file(area_path, "ManagementArea.vue"))
    entry_text = planet + "\n" + area
    for token in ("PendingItems", "MaintenanceRecords"):
        assert token in entry_text, (
            f"{CLAUSE}：前端未挂载「{token}」入口组件")
    assert any(label in entry_text for label in ("待处理", "整理记录")), (
        f"{CLAUSE}：前端未提供「待处理 / 整理记录」入口")
    pending = _read(
        _require_file(FRONTEND_SRC / "components" / "planet" / "PendingItems.vue", "PendingItems.vue"))
    # 三类事项来自真实接口的 kind 字段（不自行推断类别）
    for token in ("knowledge_proposal", "maintenance_question", "entity_candidate", "kind"):
        assert token in pending, (
            f"{CLAUSE}：待处理清单必须按 API 的 kind 区分事项（缺少 {token}）")
    # 动作集合来自服务端返回的 actions，而不是界面自己猜「能不能点」
    assert "actions" in pending, (
        f"{CLAUSE}：待处理清单的动作集合必须来自 API 的 actions 字段")
    records = _read(
        _require_file(
            FRONTEND_SRC / "components" / "planet" / "MaintenanceRecords.vue",
            "MaintenanceRecords.vue",
        )
    )
    for token in ("listMaintenanceBatches", "previewMaintenanceRestore", "restoreMaintenance"):
        assert token in records, (
            f"{CLAUSE}：整理记录必须使用真实的批次 / 预览 / 恢复接口（缺少 {token}）")


def test_frontend_source_filter_uses_api_label():
    """§12.5：来源筛选按 API 返回的标签匹配（含「后台整理」），不自行推断来源键。"""
    panel_candidates = list(FRONTEND_SRC.rglob("KnowledgePanel.vue"))
    assert panel_candidates, f"{CLAUSE}：找不到 KnowledgePanel.vue（前端结构被改动？）"
    texts = [_read(path) for path in panel_candidates]
    joined = "\n".join(texts)
    assert "后台整理" in joined, (
        f"{CLAUSE}：知识面板来源筛选项必须包含「后台整理」")
    assert re.search(r"k\.source\s*!==\s*source\.value|source\.value\s*&&\s*k\.source", joined), (
        f"{CLAUSE}：知识面板必须按 API 返回的 k.source 标签筛选（不自行推断 provenance 键）")
