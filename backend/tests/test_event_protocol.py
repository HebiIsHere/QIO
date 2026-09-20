"""事件协议守卫（第三阶段 spec 第 47~50、101、106 条）。

第三阶段明确要求：**禁止继续存在半协议**。

* 「后端会发、前端完全忽略」
* 「前端写了 case、后端永远不发」

这两类问题会让系统越来越不可维护。这里用源码扫描把协议收敛结果钉住：

1. 前端 `EVENT_TYPES` 与后端 `EventType` 必须**集合完全相等**；
2. 每个保留事件在 `backend/src/agent` 里都必须真的被产生（不是只有枚举定义）；
3. 已删除的事件（`MEMORY_INJECT`）不得在任何一侧残留；
4. 每个事件在前端要么有路由分支，要么被明确标注为「不需要前端处理」。
"""

from __future__ import annotations

import re
from pathlib import Path

from agent.api.events import EventType

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "backend" / "src" / "agent"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
EVENTS_TS = FRONTEND_SRC / "services" / "events.ts"
EVENT_STORE_TS = FRONTEND_SRC / "stores" / "events.ts"

# 协议里保留、但**不需要**前端 store 单独分支的事件：
# - TURN_START / TURN_END 等都有分支；这里留空表示「所有事件都必须被前端处理」。
FRONTEND_ROUTE_EXEMPT: set[str] = set()


def _backend_event_names() -> set[str]:
    return {e.value for e in EventType}


def _frontend_event_names() -> set[str]:
    """解析 `frontend/src/services/events.ts` 里 EVENT_TYPES 的字符串字面量。"""
    text = EVENTS_TS.read_text(encoding="utf-8")
    match = re.search(r"export const EVENT_TYPES\s*=\s*\[(.*?)\]\s*as const", text, re.S)
    assert match is not None, "frontend/src/services/events.ts 里找不到 EVENT_TYPES"
    return set(re.findall(r'"([A-Z_]+)"', match.group(1)))


def _iter_backend_sources():
    for path in BACKEND_SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path, path.read_text(encoding="utf-8")


def _has_producer(text: str, name: str) -> bool:
    """两种产生方式都算生产：`EventType.X` 直接构造，或 core/ 的 `_emit_event(X, ...)`。

    `core/turn.py` 不 import `api/`（分层约束），所以它用模块常量名发射
    `TURN_START` / `TURN_END`，这里必须认这条路径，否则守卫会漏判。
    """
    return bool(
        re.search(rf"\bEventType\.{name}\b", text)
        or re.search(rf'_emit_event\(\s*"{name}"', text)
        or re.search(rf"_emit_event\(\s*{name}\b", text)
    )


def test_frontend_and_backend_event_sets_are_identical():
    backend = _backend_event_names()
    frontend = _frontend_event_names()
    assert frontend == backend, (
        f"前端缺少：{sorted(backend - frontend)}；"
        f"前端多出：{sorted(frontend - backend)}"
    )


def test_every_event_has_a_producer_in_backend():
    """枚举里定义了却从来不发 = 半协议。"""
    names = _backend_event_names()
    sources = {
        name: [] for name in names
    }
    for path, text in _iter_backend_sources():
        if path.name == "events.py" and path.parent.name == "api":
            continue  # 枚举定义本身不算产生者
        for name in names:
            if _has_producer(text, name):
                sources[name].append(str(path.relative_to(REPO_ROOT)))
    missing = sorted(name for name, hits in sources.items() if not hits)
    assert not missing, f"这些事件只有枚举定义、没有生产代码：{missing}"


def test_every_event_is_consumed_by_frontend_store():
    text = EVENT_STORE_TS.read_text(encoding="utf-8")
    handled = set(re.findall(r'case "([A-Z_]+)"', text))
    handled |= set(re.findall(r'if \(event\.type === "([A-Z_]+)"\)', text))
    missing = sorted(_backend_event_names() - handled - FRONTEND_ROUTE_EXEMPT)
    assert not missing, f"这些事件后端会发、但前端 store 完全不处理：{missing}"


def test_deleted_event_leaves_no_trace():
    """`MEMORY_INJECT` 是第三阶段删除的事件：两侧都不得再引用。"""
    offenders: list[str] = []
    for path, text in _iter_backend_sources():
        if "MEMORY_INJECT" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    for path in FRONTEND_SRC.rglob("*"):
        if path.is_file() and path.suffix in {".ts", ".vue"}:
            if "MEMORY_INJECT" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"MEMORY_INJECT 已删除但仍有引用：{offenders}"


def test_fallback_and_capability_are_separate_concerns():
    """正常状态不显示 Capability，降级才提示：两个事件不能混成一个。"""
    names = _backend_event_names()
    assert "CAPABILITY" in names
    assert "FALLBACK" in names
    assert _frontend_event_names() >= {"CAPABILITY", "FALLBACK"}
