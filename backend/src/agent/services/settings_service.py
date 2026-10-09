"""设置写入的唯一入口：**先全量校验 → 单事务提交 → 再应用运行时**。

为什么要有这一层
----------------
`api/server.py` 里每个设置端点各自「边校验边写」：前面的字段已经落库、后面的
字段才发现非法（或写到一半写库失败）时，用户看到的是 400，数据库里却留下了
半套新设置 —— 界面显示、运行时行为、数据库三份状态互相打架。设置是一个
**整体**：要么整套生效，要么一个字节都不变。

调用方（Lead 接线的 `api/server.py`）只需要：

    from agent.services.settings_service import (
        SettingsService, SettingsValidationError, SettingsWriteError,
    )

    try:
        result = SettingsService(ctx).apply({"memory": body})
    except SettingsValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from None
    except SettingsWriteError as exc:
        raise HTTPException(status_code=500, detail=exc.detail) from None
    return result.sections["memory"]

语义（逐条对应 M08 验收）
------------------------
* **先全量校验**：`apply()` 在任何写库之前把补丁里的每个字段都解析、范围校验完；
  只要有一个字段非法（含未知 section / 未知字段），抛 `SettingsValidationError`
  （HTTP 400 语义），**一个字节都不写**。
* **单事务提交**：全部写入包在 `storage.db.transaction()` 里；中途任何一步失败
  整体 ROLLBACK，抛 `SettingsWriteError`，数据库保持整套旧值。
* **再应用运行时**：搜索设置（`ctx.apply_search_settings`）与工具历史清理
  （`ctx.prune_tool_outputs` / `ctx.prune_tool_records`）只在**提交成功之后**发生。
  这也保证「保留期限校验失败不触发清理」——校验失败时清理代码根本不会被执行。
* **部分更新**：补丁里出现的字段才改，未出现的字段保持现值（GET 的默认值语义不变）。
* **写只读字段**：`bocha_api_key` 是只写密钥，只校验类型与长度，**永不出现在
  返回值、错误信息或日志里**（AGENTS.md 硬约束）；对外只暴露 `bocha_has_key`。
* 各字段的合法范围与「越界是报错还是收敛到边界」沿用既有端点的既有行为：
  memory / loop / maintenance 严格报错；tools 与 search 的数值字段按边界收敛。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from agent.storage.db import transaction
from agent.storage.settings import SettingsStore

__all__ = [
    "SettingsService",
    "SettingsValidationError",
    "SettingsWriteError",
    "SettingsApplyResult",
    "apply_settings",
]


class SettingsValidationError(ValueError):
    """补丁里有非法字段：整套设置不变（HTTP 400 语义）。

    `field` 用点分路径（如 ``memory.fragment_max_turns``），`detail` 是给用户/日志
    看的一句话 —— **不得包含任何密钥原文**。
    """

    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(f"{field_path}: {message}")
        self.field = field_path
        self.reason = message
        self.detail = f"{field_path}: {message}"


class SettingsWriteError(RuntimeError):
    """校验通过但写库失败：事务已回滚，数据库保持整套旧值（HTTP 5xx 语义）。"""

    def __init__(self, detail: str, *, cause: BaseException | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.cause = cause


@dataclass(frozen=True)
class SettingsApplyResult:
    """一次成功写入的结果。"""

    #: 本次补丁涉及的 section 的权威现值（形状与对应 GET 端点完全一致）
    sections: dict[str, dict[str, Any]]
    #: 真正发生变化的字段（点分路径，已排序）；空列表 = 请求幂等、没有实际改动
    changed: tuple[str, ...] = ()
    #: 提交成功后实际执行的运行时动作（便于端点与测试观察「清理是否真的发生」）
    applied: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": True, "changed": list(self.changed), "sections": self.sections, **self.applied}


class _Context(Protocol):
    """`SettingsService` 需要的最小上下文（`AppContext` 天然满足）。"""

    conn: sqlite3.Connection
    settings_store: SettingsStore


# -- 常量：与既有端点保持一致（范围 / 默认值 / 收敛 vs 报错） ------------------

MEMORY_SECTION = "memory"
LOOP_SECTION = "loop"
MAINTENANCE_SECTION = "maintenance"
TOOLS_SECTION = "tools"
SEARCH_SECTION = "search"

SECTIONS = (MEMORY_SECTION, LOOP_SECTION, MAINTENANCE_SECTION, TOOLS_SECTION, SEARCH_SECTION)

# loop 的既有上限 / 默认值（server.py 同名常量）
LOOP_MAX_ITERATIONS_LIMIT = 1000
LOOP_DEFAULT_ITERATIONS = 128
LOOP_DEFAULT_OUTPUT_TOKENS = 51200
# maintenance
MAINTENANCE_MAX_INTERVAL_HOURS = 24 * 30
# search
SEARCH_TOP_K_MIN, SEARCH_TOP_K_MAX = 1, 20
SEARCH_MAX_FETCH_CHARS_MIN, SEARCH_MAX_FETCH_CHARS_MAX = 1000, 40000
# 只写密钥的长度上限：只做健全性校验，不回显、不进日志
SEARCH_API_KEY_MAX_CHARS = 512

_BOOL_TRUE = ("1", "true", "yes", "on")
_BOOL_FALSE = ("0", "false", "no", "off", "")


def _as_bool(value: Any, field_path: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
    raise SettingsValidationError(field_path, "must be a boolean")


def _as_int(value: Any, field_path: str) -> int:
    if isinstance(value, bool):  # bool 是 int 的子类，但「开关」不能当数字用
        raise SettingsValidationError(field_path, "must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise SettingsValidationError(field_path, "must be an integer") from None
    raise SettingsValidationError(field_path, "must be an integer")


def _as_text(value: Any, field_path: str, *, max_chars: int | None = None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SettingsValidationError(field_path, "must be a string")
    text = value.strip()
    if max_chars is not None and len(text) > max_chars:
        # 不回显原文：只说超长
        raise SettingsValidationError(field_path, f"is too long (>{max_chars} characters)")
    return text


def _require_mapping(value: Any, field_path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SettingsValidationError(field_path, "must be an object")
    return value


def _reject_unknown(keys: Any, allowed: tuple[str, ...], field_path: str) -> None:
    unknown = [str(key) for key in keys if str(key) not in allowed]
    if unknown:
        raise SettingsValidationError(
            field_path if not unknown else f"{field_path}.{unknown[0]}",
            f"unknown field (allowed: {', '.join(allowed)})",
        )


@dataclass
class _Planned:
    """校验通过、等待提交的完整写入计划（此时仍然一个字节都没写）。"""

    writes: list[tuple[str, str]] = field(default_factory=list)
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    dirty_sections: set[str] = field(default_factory=set)


class SettingsService:
    """把一次设置补丁作为一个整体处理。无状态（每次调用自带上下文）。"""

    def __init__(self, ctx: _Context) -> None:
        self.ctx = ctx
        self.store: SettingsStore = ctx.settings_store
        self.conn: sqlite3.Connection = ctx.conn

    # -- 对外唯一入口 --------------------------------------------------

    def apply(self, patch: Mapping[str, Any]) -> SettingsApplyResult:
        if not isinstance(patch, Mapping):
            raise SettingsValidationError("body", "must be an object")
        plan = self._plan(patch)

        if plan.writes:
            before = {key: self.store.get(key) for key, _ in plan.writes}
            self._commit(plan.writes)
            changed = tuple(sorted(key for key, value in plan.writes if before.get(key) != value))
        else:
            changed = ()

        applied = self._apply_runtime(plan)

        # 只回本次补丁涉及到的 section，形状与各 GET 端点一致
        sections = {name: self._read_section(name) for name in sorted(plan.dirty_sections)}
        return SettingsApplyResult(sections=sections, changed=changed, applied=applied)

    # -- 阶段 1：全量校验（不写任何东西） --------------------------------

    def _plan(self, patch: Mapping[str, Any]) -> _Planned:
        _reject_unknown(patch.keys(), SECTIONS, "body")
        plan = _Planned()
        for name in SECTIONS:
            if name not in patch:
                continue
            body = _require_mapping(patch[name], name)
            plan.dirty_sections.add(name)
            handler = getattr(self, f"_plan_{name}")
            handler(body, plan)
        return plan

    def _plan_memory(self, body: Mapping[str, Any], plan: _Planned) -> None:
        from agent.memory.fragment import (
            FRAGMENT_MAX_TURNS,
            FRAGMENT_MIN_TURNS,
            FRAGMENT_TOKENS_KEY,
            FRAGMENT_TURNS_KEY,
        )

        _reject_unknown(
            body.keys(), ("fragment_max_turns", "fragment_max_tokens"), MEMORY_SECTION
        )
        if "fragment_max_turns" in body:
            value = _as_int(body["fragment_max_turns"], f"{MEMORY_SECTION}.fragment_max_turns")
            if not (FRAGMENT_MIN_TURNS <= value <= FRAGMENT_MAX_TURNS):
                raise SettingsValidationError(
                    f"{MEMORY_SECTION}.fragment_max_turns",
                    f"must be in [{FRAGMENT_MIN_TURNS}, {FRAGMENT_MAX_TURNS}]",
                )
            plan.writes.append((FRAGMENT_TURNS_KEY, str(value)))
        if "fragment_max_tokens" in body:
            value = _as_int(body["fragment_max_tokens"], f"{MEMORY_SECTION}.fragment_max_tokens")
            if not (2_000 <= value <= 200_000):
                raise SettingsValidationError(
                    f"{MEMORY_SECTION}.fragment_max_tokens", "must be in [2000, 200000]"
                )
            plan.writes.append((FRAGMENT_TOKENS_KEY, str(value)))

    def _plan_loop(self, body: Mapping[str, Any], plan: _Planned) -> None:
        _reject_unknown(body.keys(), ("max_iterations", "output_token_budget"), LOOP_SECTION)
        if "max_iterations" in body:
            value = _as_int(body["max_iterations"], f"{LOOP_SECTION}.max_iterations")
            if not (1 <= value <= LOOP_MAX_ITERATIONS_LIMIT):
                raise SettingsValidationError(
                    f"{LOOP_SECTION}.max_iterations",
                    f"must be in [1, {LOOP_MAX_ITERATIONS_LIMIT}]",
                )
            plan.writes.append(("loop.max_iterations", str(value)))
        if "output_token_budget" in body:
            value = _as_int(body["output_token_budget"], f"{LOOP_SECTION}.output_token_budget")
            if value < 0:
                raise SettingsValidationError(
                    f"{LOOP_SECTION}.output_token_budget", "must be >= 0"
                )
            plan.writes.append(("loop.output_token_budget", str(value)))

    def _plan_maintenance(self, body: Mapping[str, Any], plan: _Planned) -> None:
        _reject_unknown(body.keys(), ("enabled", "interval_hours"), MAINTENANCE_SECTION)
        if "enabled" in body:
            value = _as_bool(body["enabled"], f"{MAINTENANCE_SECTION}.enabled")
            plan.writes.append(("maintenance.enabled", "true" if value else "false"))
        if "interval_hours" in body:
            value = _as_int(body["interval_hours"], f"{MAINTENANCE_SECTION}.interval_hours")
            if not (1 <= value <= MAINTENANCE_MAX_INTERVAL_HOURS):
                raise SettingsValidationError(
                    f"{MAINTENANCE_SECTION}.interval_hours",
                    f"must be in [1, {MAINTENANCE_MAX_INTERVAL_HOURS}]",
                )
            plan.writes.append(("maintenance.interval_hours", str(value)))

    def _plan_tools(self, body: Mapping[str, Any], plan: _Planned) -> None:
        from agent.storage.tool_records import MAX_RETENTION_DAYS

        _reject_unknown(
            body.keys(),
            ("record_outputs", "output_retention_days", "record_retention_days"),
            TOOLS_SECTION,
        )
        if "record_outputs" in body:
            value = _as_bool(body["record_outputs"], f"{TOOLS_SECTION}.record_outputs")
            plan.writes.append(("tools.record_outputs", "1" if value else "0"))
        if "output_retention_days" in body:
            value = _as_int(
                body["output_retention_days"], f"{TOOLS_SECTION}.output_retention_days"
            )
            # 既有行为：按边界收敛（不是报错）
            plan.writes.append(
                ("tools.output_retention_days", str(max(0, min(value, MAX_RETENTION_DAYS))))
            )
        if "record_retention_days" in body:
            value = _as_int(
                body["record_retention_days"], f"{TOOLS_SECTION}.record_retention_days"
            )
            # 0 = 永久保留（默认）
            plan.writes.append(
                ("tools.record_retention_days", str(max(0, min(value, MAX_RETENTION_DAYS))))
            )

    def _plan_search(self, body: Mapping[str, Any], plan: _Planned) -> None:
        _reject_unknown(
            body.keys(),
            (
                "searxng_url",
                "bocha_api_key",
                "keyless_fallback",
                "top_k_default",
                "max_fetch_chars",
            ),
            SEARCH_SECTION,
        )
        if "searxng_url" in body:
            plan.writes.append(
                (
                    "search.searxng_url",
                    _as_text(body["searxng_url"], f"{SEARCH_SECTION}.searxng_url"),
                )
            )
        if "bocha_api_key" in body:
            # 只写密钥：空字符串 = 显式清除。值不进返回值 / 错误 / 日志。
            key = _as_text(
                body["bocha_api_key"],
                f"{SEARCH_SECTION}.bocha_api_key",
                max_chars=SEARCH_API_KEY_MAX_CHARS,
            )
            plan.writes.append(("search.bocha_api_key", key))
        if "keyless_fallback" in body:
            value = _as_bool(body["keyless_fallback"], f"{SEARCH_SECTION}.keyless_fallback")
            plan.writes.append(("search.keyless_fallback", "1" if value else "0"))
        if "top_k_default" in body:
            value = _as_int(body["top_k_default"], f"{SEARCH_SECTION}.top_k_default")
            plan.writes.append(("search.top_k_default", str(max(SEARCH_TOP_K_MIN, min(value, SEARCH_TOP_K_MAX)))))
        if "max_fetch_chars" in body:
            value = _as_int(body["max_fetch_chars"], f"{SEARCH_SECTION}.max_fetch_chars")
            plan.writes.append(
                (
                    "search.max_fetch_chars",
                    str(max(SEARCH_MAX_FETCH_CHARS_MIN, min(value, SEARCH_MAX_FETCH_CHARS_MAX))),
                )
            )

    # -- 阶段 2：单事务提交 ---------------------------------------------

    def _commit(self, writes: list[tuple[str, str]]) -> None:
        try:
            with transaction(self.conn):
                for key, value in writes:
                    self.store.set(key, value)
        except Exception as exc:  # noqa: BLE001 - 统一转成「整套回滚」的可读错误
            raise SettingsWriteError(
                "设置写入失败，本次改动已整体回滚（设置保持原值）", cause=exc
            ) from exc

    # -- 阶段 3：应用运行时（只在提交成功之后） --------------------------

    def _apply_runtime(self, plan: _Planned) -> dict[str, Any]:
        applied: dict[str, Any] = {}
        if SEARCH_SECTION in plan.dirty_sections and hasattr(self.ctx, "apply_search_settings"):
            self.ctx.apply_search_settings()  # type: ignore[attr-defined]
            applied["search_applied"] = True
        if TOOLS_SECTION in plan.dirty_sections:
            prune_outputs = getattr(self.ctx, "prune_tool_outputs", None)
            prune_records = getattr(self.ctx, "prune_tool_records", None)
            if callable(prune_outputs):
                applied["purged"] = prune_outputs()
            if callable(prune_records):
                applied["records_purged"] = prune_records()
        return applied

    # -- 权威读取（与各 GET 端点同形） -----------------------------------

    def _read_section(self, name: str) -> dict[str, Any]:
        reader = getattr(self, f"_read_{name}")
        return reader()

    def _read_memory(self) -> dict[str, Any]:
        from agent.memory.fragment import resolve_max_tokens, resolve_max_turns

        return {
            "fragment_max_turns": resolve_max_turns(self.store),
            "fragment_max_tokens": resolve_max_tokens(self.store),
        }

    def _read_loop(self) -> dict[str, Any]:
        return {
            "max_iterations": self.store.get_int("loop.max_iterations", LOOP_DEFAULT_ITERATIONS),
            "output_token_budget": self.store.get_int(
                "loop.output_token_budget", LOOP_DEFAULT_OUTPUT_TOKENS
            ),
        }

    def _read_maintenance(self) -> dict[str, Any]:
        return {
            "enabled": self.store.get("maintenance.enabled", "true") != "false",
            "interval_hours": self.store.get_int("maintenance.interval_hours", 24),
        }

    def _read_tools(self) -> dict[str, Any]:
        from agent.storage.tool_records import (
            DEFAULT_RECORD_RETENTION_DAYS,
            DEFAULT_RETENTION_DAYS,
            count_records,
        )

        return {
            "record_outputs": self.store.get_bool("tools.record_outputs", True),
            "output_retention_days": max(
                0, self.store.get_int("tools.output_retention_days", DEFAULT_RETENTION_DAYS)
            ),
            "record_retention_days": max(
                0,
                self.store.get_int(
                    "tools.record_retention_days", DEFAULT_RECORD_RETENTION_DAYS
                ),
            ),
            "record_count": count_records(self.conn),
        }

    def _read_search(self) -> dict[str, Any]:
        # 只暴露「有没有 Key」，绝不回显 Key 本身
        has_key = bool(self.store.get("search.bocha_api_key", "") or "")
        return {
            "searxng_url": self.store.get("search.searxng_url", "") or "",
            "bocha_has_key": has_key,
            "keyless_fallback": self.store.get_bool("search.keyless_fallback", True),
            "top_k_default": self.store.get_int("search.top_k_default", 5),
            "max_fetch_chars": self.store.get_int("search.max_fetch_chars", 15000),
        }


def apply_settings(ctx: _Context, patch: Mapping[str, Any]) -> SettingsApplyResult:
    """便捷入口：无状态，等价于 `SettingsService(ctx).apply(patch)`。"""
    return SettingsService(ctx).apply(patch)
