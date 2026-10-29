# -*- coding: utf-8 -*-
"""互动意图执行记录（R2 契约）：写入程序事实 → 读取派生展示对象。

契约：D:/qio-dev/r2-contract-intent-execution.md（Lead 固定）。要点：

* **写**：互动续作 Turn 的 user 消息新增 content_type="intent_execution"，
  raw={"intent_execution": {...}} 只放程序事实（板面 / 提交 / 意图集 / 来源 /
  尝试 / 授权版本 / record_key）。content 原样保留 —— 模型上下文、授权版本、
  已完成部分保护、恢复信息一个字都不动。事实只能来自真实绑定
  （start_interactive_turn 的 ctx 事实），禁止从消息文本 / 用户输入推断。
* **读**：所有返回消息的接口调用 decorate_messages()，按真实记录
  （board_intents / board_intent_turns / board_intent_retries / board_executions /
  board_submissions / turn_journal）派生展示就绪对象：7 档真实状态 + 中文标签、
  title 复用确认时的意图名、record_head 合并、attempts 时间线、diagnostics。
  旧消息（没有元数据）原样返回，不猜、不隐藏、不批量改写。
* record_key = "ie:" + submission_id + ":" + ",".join(sorted(intent_ids))：
  同一提交 + 同一意图集共用一条记录；不同 submission / 不同意图集绝不合并。

本模块不写任何政策判断（确认 / 审批 / 执行范围 / 恢复 / 自动重试都不在这里），
只为「这条内部工作文字如何被记录与展示」提供事实与派生。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from agent.interactive import models

logger = logging.getLogger(__name__)

#: 互动续作 user 消息的 content_type（唯一常量来源，禁止散落魔法串）。
CONTENT_TYPE = "intent_execution"
#: messages.raw 里的元数据键。
RAW_KEY = "intent_execution"

#: 7 档展示状态（契约 §三）。
STATUS_CONFIRMED = "confirmed"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_COMPLETED = "completed"
STATUS_UNKNOWN = "unknown"

STATUS_LABELS: dict[str, str] = {
    STATUS_CONFIRMED: "已确认，等待处理",
    STATUS_QUEUED: "等待处理",
    STATUS_RUNNING: "正在处理",
    STATUS_PARTIAL: "部分完成",
    STATUS_FAILED: "处理失败",
    STATUS_COMPLETED: "已完成",
    STATUS_UNKNOWN: "状态无法核对",
}

#: 真实程序来源（submission.TURN_SOURCES + 手动重试）→ 契约展示词汇
#: （confirm / resume / retry）。raw 里保留真实来源，展示层用这一份归一。
DISPLAY_SOURCES: dict[str, str] = {
    "confirm": "confirm",
    "batch_confirm": "confirm",
    "recover": "resume",
    "resume": "resume",
    "manual_retry": "retry",
    "retry": "retry",
}

#: attempts[] 的展示标签（Lead 集成反馈：前端优先用 status_label，不自己猜）。
#: 除了记录级 7 档，还覆盖 Turn 台账里可能出现的 done / cancelled / interrupted /
#: incomplete；其它状态如实给原文，不编标签。
ATTEMPT_STATUS_LABELS: dict[str, str] = {
    STATUS_COMPLETED: "已完成",
    "done": "已完成",
    STATUS_FAILED: "处理失败",
    "cancelled": "已停止",
    "interrupted": "已中断",
    "incomplete": "未完整结束",
    STATUS_RUNNING: "正在处理",
    STATUS_QUEUED: "等待处理",
    STATUS_CONFIRMED: "已确认，等待处理",
    STATUS_PARTIAL: "部分完成",
    STATUS_UNKNOWN: "状态无法核对",
}

#: 内部错误码 → 人话（内部码不上界面；未知 code 直接不显示）。
_REASON_LABELS: dict[str, str] = {
    "turn_ended_without_completion": "本轮结束但意图没有完成",
    "turn_failed": "本轮执行失败",
    "turn_cancelled": "本轮被停止",
    "user_stopped": "你停止了这次执行",
    "turn_unavailable": "主循环暂时不可用",
    "startup_retry_unavailable": "重启后主循环暂不可用",
    "turn_submit_failed": "主循环没有受理这次执行",
    "turn_id_missing": "主循环没有返回可核对的轮次身份",
}

_INTERNAL_CODE = re.compile(r"^[a-z0-9_]+$")

#: 一次运行最多承认的尝试条数（防脏数据把历史撑爆）。
_MAX_ATTEMPTS = 200


# ---------------------------------------------------------------------------
# 写入侧：程序事实
# ---------------------------------------------------------------------------


def build_record_key(submission_id: Any, intent_ids: Iterable[Any]) -> str:
    """契约 §二：同一提交 + 同一意图集（排序后）→ 同一个 record_key。

    缺 submission_id 或意图集为空时返回空串（调用方按「无法核对」处理）。
    """
    sid = str(submission_id or "").strip()
    ids = sorted({str(item).strip() for item in intent_ids or [] if str(item).strip()})
    if not sid or not ids:
        return ""
    return "ie:" + sid + ":" + ",".join(ids)


def turn_record_facts(
    *,
    board_id: Any,
    submission_id: Any,
    topic_id: Any,
    intent_ids: Sequence[Any],
    source: Any,
    turn_id: Any,
    attempt: Any,
    retry_attempt: Any = None,
    retry_limit: Any = None,
    authorization_versions: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    """一次真实续作 Turn 的程序事实（写进 TurnContext → messages.raw）。

    所有值都来自调用方**已经核过的真实记录**（board_submissions / board_intents /
    board_intent_turns / TurnContext）；这里不做任何推断。
    """
    ids = [str(item) for item in intent_ids or [] if str(item).strip()]
    versions: dict[str, int] = {}
    for key, value in dict(authorization_versions or {}).items():
        try:
            versions[str(key)] = int(value)
        except (TypeError, ValueError):
            versions[str(key)] = 0
    return {
        "record_key": build_record_key(submission_id, ids),
        "board_id": str(board_id or ""),
        "submission_id": str(submission_id or ""),
        "topic_id": str(topic_id or ""),
        "intent_ids": ids,
        "source": str(source or ""),
        "turn_id": str(turn_id or ""),
        "attempt": _as_int(attempt),
        "retry_attempt": _as_int_or_none(retry_attempt),
        "retry_limit": _as_int_or_none(retry_limit),
        "authorization_versions": versions,
    }


# ---------------------------------------------------------------------------
# 读取侧：装饰消息
# ---------------------------------------------------------------------------


def decorate_messages(
    conn: sqlite3.Connection,
    messages: Sequence[dict],
    *,
    topic_id: str | None = None,
) -> list[dict]:
    """给一页消息补上展示就绪的 intent_execution 对象（其余消息原样保留）。

    * 只处理 content_type == "intent_execution"（或 raw 里带 intent_execution 元数据）
      的消息；旧消息（普通 text）一个键都不加；
    * 整页一次批量查询（不 N+1）：同页消息共享一份记录上下文；
    * 任何读取失败都不影响消息本身返回：如实降级成 unknown + verified=false。
    """
    items = [dict(message) for message in messages or []]
    metas = [_metadata_of(message) for message in items]
    if not any(meta is not None for meta in metas):
        return items
    context: _RecordContext | None
    try:
        context = _RecordContext(conn, items, metas, topic_id=topic_id)
    except Exception as exc:  # noqa: BLE001 - 派生失败只降级，不能吞掉消息
        logger.warning("intent execution derivation unavailable: %s", exc)
        context = None
    decorated: list[dict] = []
    for message, meta in zip(items, metas):
        if meta is None:
            decorated.append(message)
            continue
        updated = dict(message)
        if context is not None:
            try:
                updated["intent_execution"] = context.view(message, meta)
            except Exception as exc:  # noqa: BLE001 - 单条失败同样如实降级
                logger.warning("intent execution view failed: %s", exc)
                updated["intent_execution"] = _unknown_view(meta)
        else:
            updated["intent_execution"] = _unknown_view(meta)
        decorated.append(updated)
    return decorated


def _metadata_of(message: Mapping[str, Any]) -> dict[str, Any] | None:
    """读一条消息的 intent_execution 元数据；None = 旧消息（原样返回）。"""
    raw = message.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    meta = raw.get(RAW_KEY)
    if isinstance(meta, dict):
        return meta
    if str(message.get("content_type") or "") == CONTENT_TYPE:
        return {}
    return None


def _key_for(meta: Mapping[str, Any], message_id: str) -> str:
    key = str(meta.get("record_key") or "").strip()
    if key:
        return key
    built = build_record_key(meta.get("submission_id") or "", meta.get("intent_ids") or [])
    return built or ("?" + str(message_id))


class _RecordContext:
    """一次请求内的记录派生上下文：整页批量查真实记录，按 record_key 缓存派生结果。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        messages: Sequence[dict],
        metas: Sequence[dict | None],
        *,
        topic_id: str | None = None,
    ) -> None:
        self.conn = conn
        self.topic_id = str(topic_id or "").strip()
        self.groups = self._load_groups(messages, metas)
        intent_ids: list[str] = []
        submission_ids: list[str] = []
        turn_ids: list[str] = []
        board_ids: list[str] = []
        for entries in self.groups.values():
            for entry in entries:
                meta = entry["meta"] or {}
                for intent_id in meta.get("intent_ids") or []:
                    value = str(intent_id).strip()
                    if value and value not in intent_ids:
                        intent_ids.append(value)
                for key, bucket in (
                    ("submission_id", submission_ids),
                    ("board_id", board_ids),
                ):
                    value = str(meta.get(key) or "").strip()
                    if value and value not in bucket:
                        bucket.append(value)
                for turn_id in (meta.get("turn_id"), entry.get("turn_id")):
                    value = str(turn_id or "").strip()
                    if value and value not in turn_ids:
                        turn_ids.append(value)
        self.intent_rows = _by_id(
            _query(
                conn,
                "SELECT * FROM board_intents WHERE id IN ({})".format(
                    _placeholders(intent_ids)
                ),
                intent_ids,
            )
        )
        self.claims = {
            str(row["intent_id"]): row
            for row in _query(
                conn,
                "SELECT * FROM board_intent_turns WHERE intent_id IN ({})".format(
                    _placeholders(intent_ids)
                ),
                intent_ids,
            )
        }
        self.retries: dict[str, list[sqlite3.Row]] = {}
        for row in _query(
            conn,
            "SELECT * FROM board_intent_retries WHERE intent_id IN ({})"
            " ORDER BY created_at, rowid".format(_placeholders(intent_ids)),
            intent_ids,
        ):
            self.retries.setdefault(str(row["intent_id"]), []).append(row)
        self.executions: dict[str, list[sqlite3.Row]] = {}
        for row in _query(
            conn,
            "SELECT * FROM board_executions WHERE intent_id IN ({})"
            " ORDER BY created_at, rowid".format(_placeholders(intent_ids)),
            intent_ids,
        ):
            self.executions.setdefault(str(row["intent_id"]), []).append(row)
        self.submissions = _by_id(
            _query(
                conn,
                "SELECT id, board_id, status, error FROM board_submissions"
                " WHERE id IN ({})".format(_placeholders(submission_ids)),
                submission_ids,
            )
        )
        self.boards = _by_id(
            _query(
                conn,
                "SELECT id, title FROM boards WHERE id IN ({})".format(
                    _placeholders(board_ids)
                ),
                board_ids,
            )
        )
        self.journal: dict[str, sqlite3.Row] = {
            str(row["turn_id"]): row
            for row in _query(
                conn,
                "SELECT turn_id, status, reason FROM turn_journal WHERE turn_id IN ({})".format(
                    _placeholders(turn_ids)
                ),
                turn_ids,
            )
        }
        self._intent_cache: dict[str, dict[str, Any]] = {}
        self._view_cache: dict[str, dict[str, Any]] = {}

    # -- 记录分组 ---------------------------------------------------------

    def _load_groups(
        self, messages: Sequence[dict], metas: Sequence[dict | None]
    ) -> dict[str, list[dict]]:
        """把同 record_key 的消息归组（含本话题里不在本页的更早消息，用于 head / attempts）。"""
        groups: dict[str, list[dict]] = {}
        seen: set[str] = set()
        if self.topic_id:
            for row in _query(
                self.conn,
                "SELECT id, created_at, content_type, raw, turn_id FROM messages"
                " WHERE content_type = ? AND fragment_id IN"
                " (SELECT id FROM fragments WHERE topic_id = ?)"
                " ORDER BY created_at, id",
                (CONTENT_TYPE, self.topic_id),
            ):
                record = dict(row)
                meta = _metadata_of(record) or {}
                entry = {
                    "id": str(record["id"]),
                    "created_at": str(record.get("created_at") or ""),
                    "turn_id": str(record.get("turn_id") or ""),
                    "meta": meta,
                }
                groups.setdefault(_key_for(meta, entry["id"]), []).append(entry)
                seen.add(entry["id"])
        # 同一 record 的消息理论上都在原提交话题里；但本轮工具换话题会把整轮搬走，
        # 这时只按话题扫描会漏掉更早的消息（head / attempts 就错了）。提交的历次
        # Turn 绑定是更稳的事实：按 submission 把它们的 user 消息也收进来。
        submission_ids: list[str] = []
        for meta in list(metas) + [
            entry["meta"] for entries in groups.values() for entry in entries
        ]:
            value = str((meta or {}).get("submission_id") or "").strip()
            if value and value not in submission_ids:
                submission_ids.append(value)
        if submission_ids:
            for row in _query(
                self.conn,
                "SELECT m.id, m.created_at, m.content_type, m.raw, m.turn_id FROM messages m"
                " WHERE m.content_type = ? AND m.turn_id IN"
                " (SELECT turn_id FROM board_submission_turns WHERE submission_id IN ({}))"
                " ORDER BY m.created_at, m.id".format(_placeholders(submission_ids)),
                (CONTENT_TYPE, *submission_ids),
            ):
                record = dict(row)
                message_id = str(record["id"])
                if message_id in seen:
                    continue
                meta = _metadata_of(record) or {}
                entry = {
                    "id": message_id,
                    "created_at": str(record.get("created_at") or ""),
                    "turn_id": str(record.get("turn_id") or ""),
                    "meta": meta,
                }
                groups.setdefault(_key_for(meta, message_id), []).append(entry)
                seen.add(message_id)
        for message, meta in zip(messages, metas):
            if meta is None:
                continue
            message_id = str(message.get("id") or "")
            if not message_id or message_id in seen:
                continue
            entry = {
                "id": message_id,
                "created_at": str(message.get("created_at") or ""),
                "turn_id": str(message.get("turn_id") or ""),
                "meta": meta,
            }
            groups.setdefault(_key_for(meta, message_id), []).append(entry)
        for entries in groups.values():
            entries.sort(key=lambda entry: (entry["created_at"], entry["id"]))
        return groups

    # -- 对外：一条消息的展示对象 -----------------------------------------

    def view(self, message: Mapping[str, Any], meta: Mapping[str, Any]) -> dict[str, Any]:
        message_id = str(message.get("id") or "")
        key = _key_for(meta, message_id)
        base = self._view_cache.get(key)
        if base is None:
            entries = self.groups.get(key) or [
                {
                    "id": message_id,
                    "created_at": str(message.get("created_at") or ""),
                    "turn_id": str(message.get("turn_id") or ""),
                    "meta": dict(meta),
                }
            ]
            base = self._derive(key, entries)
            self._view_cache[key] = base
        entries = self.groups.get(key) or []
        head_id = entries[0]["id"] if entries else message_id
        result = dict(base)
        result["record_head"] = head_id == message_id
        return result

    # -- 派生 ---------------------------------------------------------------

    def _derive(self, record_key: str, entries: Sequence[dict]) -> dict[str, Any]:
        first_meta = dict(entries[0].get("meta") or {}) if entries else {}
        intent_ids = [str(item) for item in first_meta.get("intent_ids") or [] if str(item).strip()]
        submission_id = str(first_meta.get("submission_id") or "")
        board_id = str(first_meta.get("board_id") or "")
        board_name = ""
        board_row = self.boards.get(board_id)
        if board_row is not None:
            board_name = str(board_row["title"] or "")
        turn_ids: list[str] = []
        for entry in entries:
            for candidate in ((entry.get("meta") or {}).get("turn_id"), entry.get("turn_id")):
                value = str(candidate or "").strip()
                if value and value not in turn_ids:
                    turn_ids.append(value)
        display_source = _display_source(first_meta.get("source"))
        intent_items = [self._intent_item(intent_id, entries) for intent_id in intent_ids]
        status = self._record_status(record_key, intent_ids)
        verified = status != STATUS_UNKNOWN
        with_data = [item["title"] for item in intent_items if item["title"]]
        if with_data:
            title = with_data[0] if len(with_data) == 1 else (
                f"{with_data[0]}（另有 {len(with_data) - 1} 项）"
            )
        else:
            title = "意图记录缺失" if intent_ids else "互动意图执行记录"
        result_entry = self._result_entry(board_id, intent_items)
        return {
            "record_key": record_key,
            "record_head": True,
            "title": title,
            "status": status,
            "status_label": STATUS_LABELS[status],
            "scope": {
                "board_name": board_name,
                "intent_titles": [item["title"] for item in intent_items],
                "source": display_source,
            },
            "intents": intent_items,
            "attempts": [
                self._attempt_item(entries, index, status)
                for index in range(min(len(entries), _MAX_ATTEMPTS))
            ],
            "result_entry": result_entry,
            "authorization_versions": _versions(first_meta.get("authorization_versions")),
            "diagnostics": {
                "board_id": board_id,
                "submission_id": submission_id,
                "intent_ids": intent_ids,
                "turn_ids": turn_ids,
                "record_key": record_key,
            },
            "verified": verified,
            "verified_note": "" if verified else self._unknown_note(record_key, intent_ids),
        }

    def _unknown_note(self, record_key: str, intent_ids: Sequence[str]) -> str:
        if not record_key or not record_key.startswith("ie:"):
            return "这条记录没有可核对的提交身份（submission_id），无法确认真实状态。"
        if not intent_ids:
            return "这条记录没有可核对的意图身份，无法确认真实状态。"
        missing = [intent_id for intent_id in intent_ids if intent_id not in self.intent_rows]
        if missing:
            return f"{len(missing)} 项意图的记录已不存在，无法核对真实状态。"
        return "没有可核对的意图状态记录，无法确认真实状态。"

    def _intent_item(self, intent_id: str, entries: Sequence[dict]) -> dict[str, Any]:
        state = self._intent_state(intent_id)
        attempts = sum(
            1
            for entry in entries
            if intent_id in [str(item) for item in (entry.get("meta") or {}).get("intent_ids") or []]
        )
        if state is None:
            return {
                "id": intent_id,
                "title": "（意图记录已不存在）",
                "status": STATUS_UNKNOWN,
                "status_label": STATUS_LABELS[STATUS_UNKNOWN],
                "attempts": attempts,
                "failure_reason": "",
                "result_ref": None,
            }
        return {
            "id": intent_id,
            "title": state["title"],
            "status": state["status"],
            "status_label": STATUS_LABELS[state["status"]],
            "attempts": attempts,
            "failure_reason": state["failure_reason"],
            "result_ref": state["result_ref"],
        }

    def _intent_state(self, intent_id: str) -> dict[str, Any] | None:
        if intent_id in self._intent_cache:
            return self._intent_cache[intent_id]
        row = self.intent_rows.get(intent_id)
        if row is None:
            return None
        status = _intent_status(row, self.claims.get(intent_id))
        state = {
            "title": str(row["title"] or ""),
            "status": status,
            "failure_reason": self._intent_failure_reason(intent_id, status),
            "result_ref": self._intent_result_ref(intent_id, status),
        }
        self._intent_cache[intent_id] = state
        return state

    def _intent_failure_reason(self, intent_id: str, status: str) -> str:
        if status == STATUS_COMPLETED:
            return ""
        row = self.intent_rows.get(intent_id)
        if row is not None:
            reason = _human_reason(row["reason"])
            if reason:
                return reason
        for series in reversed(self.retries.get(intent_id, [])):
            reason = _human_reason(series["last_error"])
            if reason:
                return reason
        for execution in reversed(self.executions.get(intent_id, [])):
            reason = _human_reason(execution["error"])
            if reason:
                return reason
        claim = self.claims.get(intent_id)
        if claim is not None:
            reason = _human_reason(claim["reason"])
            if reason:
                return reason
        return ""

    def _intent_result_ref(self, intent_id: str, status: str) -> dict[str, Any] | None:
        if status != STATUS_COMPLETED:
            return None
        row = self.intent_rows.get(intent_id)
        counts = _counts(models.loads(row["applied"] if row is not None else "{}", {}))
        if not any(counts.values()):
            for execution in self.executions.get(intent_id, []):
                counts = _merge_counts(
                    counts, _counts(models.loads(execution["result"], {}))
                )
        if not any(counts.values()):
            # 没有真实结果对象 → 不给结果入口（Lead 集成反馈：没有真实结果就 null）
            return None
        board_id = str(row["board_id"]) if row is not None else ""
        return {
            "kind": "board",
            "label": _result_label(counts),
            "target": board_id,
        }

    def _result_entry(
        self, board_id: str, intent_items: Sequence[dict[str, Any]]
    ) -> dict[str, Any] | None:
        completed = [item for item in intent_items if item["status"] == STATUS_COMPLETED]
        if not completed:
            return None
        counts = {"cards": 0, "groups": 0, "links": 0}
        for item in completed:
            row = self.intent_rows.get(str(item["id"]))
            if row is not None:
                counts = _merge_counts(counts, _counts(models.loads(row["applied"], {})))
        if not any(counts.values()):
            # 「已完成」但没有任何真实结果对象：不给结果入口（不编造可点入口）
            return None
        return {
            "kind": "board",
            "label": _result_label(counts),
            "target": board_id,
        }

    def _record_status(self, record_key: str, intent_ids: Sequence[str]) -> str:
        if not record_key or not record_key.startswith("ie:") or not intent_ids:
            return STATUS_UNKNOWN
        states = [self._intent_state(intent_id) for intent_id in intent_ids]
        if any(state is None for state in states):
            return STATUS_UNKNOWN
        values = [state["status"] for state in states if state is not None]
        if not values:
            return STATUS_UNKNOWN
        if all(value == STATUS_COMPLETED for value in values):
            return STATUS_COMPLETED
        if any(value == STATUS_RUNNING for value in values):
            return STATUS_RUNNING
        if any(value == STATUS_QUEUED for value in values):
            return STATUS_QUEUED
        if any(value == STATUS_COMPLETED for value in values):
            return STATUS_PARTIAL
        if any(value == STATUS_FAILED for value in values):
            return STATUS_FAILED
        if self._last_attempt_failed(intent_ids):
            return STATUS_FAILED
        if any(value == STATUS_CONFIRMED for value in values):
            return STATUS_CONFIRMED
        return STATUS_UNKNOWN

    def _last_attempt_failed(self, intent_ids: Sequence[str]) -> bool:
        """重试台账里出现过真实失败 / 停止，才说明最后一次尝试失败。

        注意：manual_attempts > 0 不算失败 —— 用户点一次「重试」本身就会建系列，
        那时还没有任何失败事实（不能因此把状态说成处理失败）。
        """
        for intent_id in intent_ids:
            for row in self.retries.get(intent_id, []):
                if str(row["status"] or "") in ("exhausted", "stopped"):
                    return True
                if _as_int(row["attempts"]) > 0:
                    return True
                if _human_reason(row["last_error"]):
                    return True
        return False

    def _attempt_item(self, entries: Sequence[dict], index: int, record_status: str) -> dict[str, Any]:
        entry = entries[index]
        meta = dict(entry.get("meta") or {})
        intent_ids = [str(item) for item in meta.get("intent_ids") or [] if str(item).strip()]
        turn_id = str(meta.get("turn_id") or entry.get("turn_id") or "")
        is_last = index == len(entries) - 1
        status, reason = self._attempt_status(entry, intent_ids, turn_id, is_last, record_status)
        return {
            "attempt": _as_int(meta.get("attempt")),
            "source": _display_source(meta.get("source")),
            "turn_id": turn_id,
            "status": status,
            # Lead 集成反馈：每条 attempt 直接给中文标签，前端优先用它显示。
            "status_label": ATTEMPT_STATUS_LABELS.get(status, status),
            "reason": reason,
            "started_at": entry.get("created_at") or "",
        }

    def _attempt_status(
        self,
        entry: Mapping[str, Any],
        intent_ids: Sequence[str],
        turn_id: str,
        is_last: bool,
        record_status: str,
    ) -> tuple[str, str]:
        if is_last:
            # 最近一次尝试按这一轮自己的台账定性：Turn 还在跑 → running/queued；
            # 台账已终态 → 只有真完成才算 completed；台账说失败/停止 → failed。
            # 台账缺失时才退回记录级状态（那是「真实记录能给的最好答案」）。
            journal = self.journal.get(turn_id)
            journal_status = str(journal["status"] or "") if journal is not None else ""
            journal_reason = _human_reason(journal["reason"]) if journal is not None else ""
            if journal_status == "queued":
                return STATUS_QUEUED, ""
            if journal_status == "running":
                return STATUS_RUNNING, ""
            if journal_status in ("failed", "unavailable"):
                return STATUS_FAILED, journal_reason or self._record_failure_reason(intent_ids)
            if journal_status in ("cancelled", "interrupted"):
                return STATUS_FAILED, journal_reason or "这一轮被停止"
            if journal_status == "incomplete":
                return STATUS_FAILED, journal_reason or "这一轮没有合法结束标记"
            if journal_status == "completed":
                if all(
                    state is not None and state["status"] == STATUS_COMPLETED
                    for state in (self._intent_state(intent_id) for intent_id in intent_ids)
                ):
                    return STATUS_COMPLETED, ""
                failure_reason = journal_reason or self._record_failure_reason(intent_ids)
                if record_status in (STATUS_FAILED, STATUS_PARTIAL) and failure_reason:
                    return STATUS_FAILED, failure_reason
                if failure_reason:
                    # 这一轮结束但意图没完成，且台账里有真实失败依据（例如正在重试）
                    return STATUS_FAILED, failure_reason
                return STATUS_UNKNOWN, "本轮结束但还没有完成这些意图"
            reason = (
                self._record_failure_reason(intent_ids)
                if record_status in (STATUS_FAILED, STATUS_PARTIAL)
                else ""
            )
            if record_status in STATUS_LABELS:
                return record_status, reason
            return STATUS_UNKNOWN, ""
        journal = self.journal.get(turn_id)
        journal_status = str(journal["status"] or "") if journal is not None else ""
        journal_reason = _human_reason(journal["reason"]) if journal is not None else ""
        if journal_status in ("failed", "unavailable"):
            return STATUS_FAILED, journal_reason
        if journal_status in ("cancelled", "interrupted"):
            return STATUS_FAILED, journal_reason or "这一轮被停止"
        if journal_status == "incomplete":
            return STATUS_FAILED, journal_reason or "这一轮没有合法结束标记"
        if journal_status == "queued":
            return STATUS_QUEUED, ""
        if journal_status == "running":
            return STATUS_RUNNING, ""
        # 后面还有尝试 → 这个更早的尝试没有完成本次记录要做的事（契约示例即 failed）。
        reason = journal_reason or self._record_failure_reason(intent_ids)
        return STATUS_FAILED, reason or "本轮没有完成全部意图"

    def _record_failure_reason(self, intent_ids: Sequence[str]) -> str:
        for intent_id in intent_ids:
            state = self._intent_state(intent_id)
            if state is not None and state["status"] in (STATUS_FAILED, STATUS_PARTIAL):
                if state["failure_reason"]:
                    return state["failure_reason"]
        for intent_id in intent_ids:
            for row in reversed(self.retries.get(intent_id, [])):
                reason = _human_reason(row["last_error"])
                if reason:
                    return reason
        for intent_id in intent_ids:
            for row in reversed(self.executions.get(intent_id, [])):
                reason = _human_reason(row["error"])
                if reason:
                    return reason
        return ""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values) if values else "NULL"


def _query(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> list[sqlite3.Row]:
    """只读批量查询；表缺列 / 旧库缺表时如实降级为空（不阻断消息返回）。"""
    try:
        return list(conn.execute(sql, tuple(params)).fetchall())
    except sqlite3.Error as exc:
        logger.warning("intent execution query degraded: %s", exc)
        return []


def _by_id(rows: Iterable[sqlite3.Row]) -> dict[str, sqlite3.Row]:
    out: dict[str, sqlite3.Row] = {}
    for row in rows:
        try:
            out[str(row["id"])] = row
        except (KeyError, IndexError):  # pragma: no cover - 防御性
            continue
    return out


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _display_source(source: Any) -> str:
    text = str(source or "").strip()
    if not text:
        return ""
    return DISPLAY_SOURCES.get(text, "")


def _versions(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, int] = {}
    for key, item in value.items():
        try:
            out[str(key)] = int(item)
        except (TypeError, ValueError):
            out[str(key)] = 0
    return out


def _human_reason(value: Any) -> str:
    """内部错误码翻成人话；未知内部码不上界面（不伪造原因）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    label = _REASON_LABELS.get(text)
    if label:
        return label
    if _INTERNAL_CODE.match(text):
        return ""
    return text


def _intent_status(row: sqlite3.Row, claim: sqlite3.Row | None) -> str:
    """单条意图的 7 档展示状态（只依据真实状态 / 认领记录）。

    顺序有讲究：completed 是终态、真实完成不会被「还在收尾的认领」盖掉；
    其余状态下，活跃认领（active）才是「正在处理 / 正在重试」的最新事实 ——
    重试一轮跑起来时意图行仍停在 failed / paused，不能因此显示成处理失败。
    """
    status = str(row["status"] or "")
    if status == "completed":
        return STATUS_COMPLETED
    claim_status = str(claim["status"] or "") if claim is not None else ""
    if claim_status == "active":
        return STATUS_RUNNING
    if status == "running":
        return STATUS_RUNNING
    if claim_status == "claiming":
        return STATUS_QUEUED
    if status == "partially_completed":
        return STATUS_PARTIAL
    if status in ("failed", "cancelled", "rejected", "paused"):
        return STATUS_FAILED
    if status == "approved":
        return STATUS_CONFIRMED
    return STATUS_UNKNOWN


def _counts(value: Any) -> dict[str, int]:
    data = value if isinstance(value, Mapping) else {}
    return {
        "cards": len([item for item in data.get("cardIds") or [] if str(item)]),
        "groups": len([item for item in data.get("groupIds") or [] if str(item)]),
        "links": len([item for item in data.get("linkIds") or [] if str(item)]),
    }


def _merge_counts(base: Mapping[str, int], extra: Mapping[str, int]) -> dict[str, int]:
    return {
        key: max(int(base.get(key) or 0), int(extra.get(key) or 0))
        for key in ("cards", "groups", "links")
    }


def _result_label(counts: Mapping[str, int]) -> str:
    parts: list[str] = []
    if counts.get("cards"):
        parts.append(f"{counts['cards']} 张卡片")
    if counts.get("groups"):
        parts.append(f"{counts['groups']} 个分组")
    if counts.get("links"):
        parts.append(f"{counts['links']} 条关系")
    return "板面结果：" + "、".join(parts) if parts else "板面结果"


def _unknown_view(meta: Mapping[str, Any]) -> dict[str, Any]:
    """降级对象：读取派生失败时如实给 unknown（不猜状态、不隐藏消息）。"""
    key = str(meta.get("record_key") or "")
    ids = [str(item) for item in meta.get("intent_ids") or [] if str(item).strip()]
    return {
        "record_key": key,
        "record_head": True,
        "title": "意图执行记录（暂时无法核对）",
        "status": STATUS_UNKNOWN,
        "status_label": STATUS_LABELS[STATUS_UNKNOWN],
        "scope": {
            "board_name": "",
            "intent_titles": [],
            "source": _display_source(meta.get("source")),
        },
        "intents": [
            {
                "id": intent_id,
                "title": "（意图记录已不存在）",
                "status": STATUS_UNKNOWN,
                "status_label": STATUS_LABELS[STATUS_UNKNOWN],
                "attempts": 0,
                "failure_reason": "",
                "result_ref": None,
            }
            for intent_id in ids
        ],
        "attempts": [],
        "result_entry": None,
        "authorization_versions": _versions(meta.get("authorization_versions")),
        "diagnostics": {
            "board_id": str(meta.get("board_id") or ""),
            "submission_id": str(meta.get("submission_id") or ""),
            "intent_ids": ids,
            "turn_ids": [str(meta.get("turn_id") or "")] if meta.get("turn_id") else [],
            "record_key": key,
        },
        "verified": False,
        "verified_note": "读取意图执行记录失败，无法核对真实状态。",
    }
