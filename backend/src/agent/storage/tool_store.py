"""Persistent store for agent-created tool definitions.

Tools registered through the lifecycle are saved here so they survive
backend restarts. The registry is restored at startup by AppContext.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from agent.tools.spec import ToolDefinition


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ToolStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save(self, definition: ToolDefinition) -> None:
        """Insert or replace the definition for the tool name."""
        now = _now()
        raw = definition.model_dump_json()
        self.conn.execute(
            "INSERT INTO tools (id, name, definition, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET definition = excluded.definition, "
            "updated_at = excluded.updated_at, status = 'active'",
            (f"tool_{definition.name}", definition.name, raw, now, now),
        )
        self.conn.commit()

    def load_all(self) -> list[ToolDefinition]:
        suspended = self.suspended_names()
        rows = self.conn.execute(
            "SELECT definition FROM tools WHERE status = 'active' ORDER BY created_at"
        ).fetchall()
        definitions: list[ToolDefinition] = []
        for row in rows:
            try:
                definition = ToolDefinition(**json.loads(row["definition"]))
            except Exception:
                continue  # skip corrupt rows, keep the rest
            if definition.name in suspended:
                # 暂时停用：定义还在，但重启后也不作为可用工具加载。
                continue
            definitions.append(definition)
        return definitions

    def load(self, name: str) -> ToolDefinition | None:
        """读回某个工具当前生效的定义；没有（或已移除）返回 None。

        注册流程用它保存「上一可用版本」：注册或落库失败时回滚到这个版本，
        而不是把用户原来能用的工具一起弄没。
        """
        if self.is_suspended(name):
            return None
        row = self.conn.execute(
            "SELECT definition FROM tools WHERE name = ? AND status = 'active'",
            (name,),
        ).fetchone()
        if row is None:
            return None
        try:
            return ToolDefinition(**json.loads(row["definition"]))
        except Exception:
            return None

    # ---- 暂时停用（软禁用）：不改 tools.status 的冻结 CHECK 约束 ----------
    # 本阶段没有新增迁移，因此「停用」用 settings 键值表记录：
    # tools 行仍 active（定义、工作区、历史全部保留），加载时排除被停用的名字。
    _SUSPENDED_PREFIX = "tool_suspended:"

    @classmethod
    def _suspension_key(cls, name: str) -> str:
        return cls._SUSPENDED_PREFIX + str(name or "")

    def suspended_names(self) -> set[str]:
        prefix = self._SUSPENDED_PREFIX
        rows = self.conn.execute(
            "SELECT key FROM settings WHERE key LIKE ?", (prefix + "%",)
        ).fetchall()
        return {str(row["key"])[len(prefix):] for row in rows}

    def is_suspended(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM settings WHERE key = ?",
            (self._suspension_key(name),),
        ).fetchone()
        return row is not None

    def suspend(self, name: str, *, reason: str = "") -> bool:
        """暂时停用：保留定义与工作区，只让加载路径不再把它当成可用工具。"""
        row = self.conn.execute(
            "SELECT 1 FROM tools WHERE name = ? AND status = 'active'", (name,)
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (
                self._suspension_key(name),
                json.dumps(
                    {"name": str(name), "reason": str(reason or ""), "at": _now()},
                    ensure_ascii=False,
                ),
                _now(),
            ),
        )
        self.conn.commit()
        return True

    def resume(self, name: str) -> bool:
        """恢复停用：删掉停用标记，行仍是 active，定义原样可加载。"""
        cursor = self.conn.execute(
            "DELETE FROM settings WHERE key = ?", (self._suspension_key(name),)
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT 1 FROM tools WHERE name = ? AND status = 'active'", (name,)
        ).fetchone()
        return row is not None

    def load_any(self, name: str) -> ToolDefinition | None:
        """读回 active 定义（含已停用但保留的记录，恢复时用；removed 不复活）。"""
        row = self.conn.execute(
            "SELECT definition FROM tools WHERE name = ? AND status = 'active'",
            (name,),
        ).fetchone()
        if row is None:
            return None
        try:
            return ToolDefinition(**json.loads(row["definition"]))
        except Exception:
            return None


    def remove(self, name: str) -> None:
        self.conn.execute(
            "UPDATE tools SET status = 'removed', updated_at = ? WHERE name = ?",
            (_now(), name),
        )
        self.conn.commit()

    def list_meta(self) -> list[dict]:
        suspended = self.suspended_names()
        rows = self.conn.execute(
            "SELECT name, status, created_at, updated_at FROM tools ORDER BY created_at"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item.get("name") in suspended and item.get("status") == "active":
                item["status"] = "suspended"
            result.append(item)
        return result
