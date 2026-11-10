"""话题回答恢复记录（P1-03：上下文准备 / 回答失败的可恢复边界）。

一次逻辑话题操作在「拿掉了话题、却没完成回答」时留一条可恢复记录；恢复入口
（`POST /api/recovery/records/{record_id}/retry-answer`）只重做**未完成的步骤**：

* `failed_stage == "context_prepare"` → 只重做上下文装配 + 回答生成，
  **不重新执行** create_topic / switch_topic，不新增用户消息、不新建重复轮次；
* `failed_stage == "answer"` → 只重做回答；
* `failed_stage == "ownership"` → 显式恢复（重新绑定 + 如实说明）。

记录是 app 级内存状态（与 `topic_application_state` 同一模式）：它不是审批、
不是话题操作本身，也不改变任何真实归属；只保存「重做时需要的事实」。
阶段/失败语义的唯一来源是 `services/topic_operation.py`（不在这里另造一套）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from agent.services.topic_operation import RECOVERY_KIND, TopicStages

logger = logging.getLogger(__name__)

__all__ = [
    "RECOVERY_KIND",
    "TOPIC_RECOVERY_ATTR",
    "TopicContextPrepareError",
    "new_record_id",
    "record_topic_failure",
    "record_topic_answer_failure",
    "get_record",
    "list_records",
    "update_record",
    "find_pending",
    "mark_answered",
    "retry_topic_answer",
]


class TopicContextPrepareError(RuntimeError):
    """目标话题上下文装配失败（结构化事实，供恢复记录与说明使用）。

    turn_orchestrator 的重建闭包在失败时抛它；loop 捕获后按字段写恢复记录，
    **不**按错误文本猜阶段。`stage` 唯一取值口径见 services/topic_operation.py。
    """

    def __init__(
        self,
        detail: str = "",
        *,
        stage: str = "context_prepare",
        operation_id: str | None = None,
        topic_id: str | None = None,
    ) -> None:
        self.detail = str(detail or "")
        self.stage = str(stage or "context_prepare")
        self.operation_id = operation_id
        self.topic_id = topic_id
        super().__init__(self.detail or self.stage)

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "operation_id": self.operation_id,
            "topic_id": self.topic_id,
            "detail": self.detail,
        }

#: app 上挂记录的属性名（跨请求可读：恢复端点在另一个请求里查同一个 app）。
TOPIC_RECOVERY_ATTR = "topic_recovery_records"


def _store(scope: Any) -> dict[str, dict]:
    """取（或惰性创建）这条记录存储；只读对象退化也不影响返回的记录本身。"""
    store = getattr(scope, TOPIC_RECOVERY_ATTR, None)
    if isinstance(store, dict):
        return store
    store = {}
    try:
        setattr(scope, TOPIC_RECOVERY_ATTR, store)
    except Exception:  # noqa: BLE001 - 只读 app 替身：记录退化为本次调用可见
        logger.warning("failed to attach topic recovery store", exc_info=True)
    return store


# ── 恢复入口：只重做未完成步骤（P1-03 / P1-04）──────────────────────────


def _current_navigation_version(ctx: Any) -> int | None:
    """导航版本（C 的 navigation_version.current）；模块未就绪时明确返回 None。"""
    try:
        from agent.services.navigation_version import current as _current
    except Exception:  # noqa: BLE001 - 迁移未就绪时如实「无法校验」，不猜数字
        return None
    conn = getattr(ctx, "conn", None)
    if conn is None:
        return None
    try:
        return int(_current(conn))
    except Exception:  # noqa: BLE001 - 读版本失败同样不当作「变过」
        logger.warning("failed to read navigation version", exc_info=True)
        return None


def _stale_result(record: dict, actual: int | None) -> dict:
    message = (
        "目标位置在你后来的操作中已经改变，恢复不会覆盖你的新选择。"
        "请回到目标话题后重新发起，或忽略这条恢复记录。"
    )
    return {
        "ok": False,
        "conflict": True,
        "code": "topic_application_stale",
        "message": message,
        "record": dict(record),
        "expected_nav_version": (
            record.get("recovery_nav_version")
            if record.get("recovery_nav_version") is not None
            else record.get("base_nav_version")
        ),
        "actual_nav_version": actual,
    }


async def _generate_answer(adapter: Any, messages: list) -> str:
    """只生成回答：按同一内容角色协议解析，声明标记不进正文。"""
    from agent import prompts
    from agent.adapters.base import ChatMessage
    from agent.core.loop import _redact_published, _split_declared_answer

    request_messages = list(messages or [])
    if not getattr(adapter, "protocol_in_prompt", False):
        request_messages = [
            *request_messages,
            ChatMessage(role="system", content=prompts.answer_protocol()),
        ]
    completion = await adapter.complete(request_messages, [])
    content = completion.message.content or ""
    declared, body = _split_declared_answer(content)
    text = body if declared else content
    return _redact_published(text, holdback=False).strip()


async def retry_topic_answer(
    ctx: Any,
    record_id: str,
    *,
    expected_class: str | None = None,
    expected_status: str | None = None,
) -> dict:
    """重试一条话题回答恢复记录（幂等；只重做未完成步骤）。

    * context_prepare 失败 → 只重装配上下文 + 回答生成（不重跑 create/switch、
      不新增用户消息、不新建重复轮次）；
    * answer 失败 → 只重做回答；
    * 已成功 → 直接返回既有结果（不重复生成 / 不复制消息）；
    * 恢复前复核最新导航版本：目标位置已被用户后来的选择改变 → 如实失败、
      不覆盖，记录保持可恢复（stale ≠ 拒绝）。
    """
    record = get_record(ctx, record_id)
    if record is None:
        return {
            "ok": False,
            "code": "recovery_not_found",
            "message": "没有这条恢复记录",
            "status_code": 404,
        }
    if expected_class and str(record.get("kind")) != str(expected_class):
        return {
            "ok": False,
            "conflict": True,
            "code": "recovery_class_conflict",
            "message": "恢复记录类型与你看到的不一致，请刷新后重试",
            "record": record,
            "status_code": 409,
        }
    status = str(record.get("status") or "")
    if expected_status and status != str(expected_status) and status != "answered":
        return {
            "ok": False,
            "conflict": True,
            "code": "recovery_status_conflict",
            "message": "恢复记录状态已经变化，请刷新后重试",
            "record": record,
            "status_code": 409,
        }
    if status == "answered":
        # 幂等：已经成功过就直接返回既有结果（不再生成、不再写消息）。
        return {
            "ok": True,
            "answered": True,
            "record_id": record["record_id"],
            "turn_id": record.get("turn_id"),
            "topic_id": record.get("topic_id"),
            "answer": record.get("answer"),
            "answer_id": record.get("message_id"),
        }

    # 恢复前复核最新导航位置与操作状态：不得覆盖用户后来的手动选择。
    # 基线取**失败时刻**的位置版本（recovery_nav_version）；没有它就退回申请起点
    # 快照（旧记录兼容）。话题工具成功导航会推进版本，拿起点快照复核时用户没有
    # 新导航也会永远 stale。
    actual_version = _current_navigation_version(ctx)
    baseline_version = record.get("recovery_nav_version")
    if baseline_version is None:
        baseline_version = record.get("base_nav_version")
    navigation_version_check = "ok"
    if baseline_version is not None:
        if actual_version is None:
            navigation_version_check = "unavailable"
        elif int(actual_version) != int(baseline_version):
            update_record(
                ctx,
                record_id,
                last_error="nav_version_conflict",
                last_attempt_at=_now_iso(),
            )
            result = _stale_result(record, actual_version)
            result["status_code"] = 409
            return result

    orchestrator = getattr(ctx, "turn_orchestrator", None)
    prepare = getattr(orchestrator, "prepare_topic_answer_messages", None)
    if not callable(prepare):
        return {
            "ok": False,
            "code": "recovery_unavailable",
            "message": "恢复入口暂时不可用（没有上下文装配通道）",
            "status_code": 503,
        }
    try:
        messages = await prepare(
            turn_id=str(record.get("turn_id") or ""),
            topic_id=str(record.get("topic_id") or ""),
            user_message_id=record.get("user_message_id"),
        )
    except Exception as exc:  # noqa: BLE001 - 装配失败仍是可恢复失败，记录保留
        failure = _error_detail(exc)
        update_record(
            ctx,
            record_id,
            failed_stage="context_prepare",
            last_error=failure,
            last_attempt_at=_now_iso(),
        )
        return {
            "ok": False,
            "code": "topic_context_prepare_failed",
            "recoverable": True,
            "failed_stage": "context_prepare",
            "message": "话题已切换，回答准备失败：" + failure,
            "record_id": record["record_id"],
            "status_code": 503,
        }
    if not messages:
        update_record(
            ctx, record_id, failed_stage="context_prepare",
            last_error="empty_context", last_attempt_at=_now_iso(),
        )
        return {
            "ok": False,
            "code": "topic_context_prepare_failed",
            "recoverable": True,
            "failed_stage": "context_prepare",
            "message": "话题已切换，回答准备失败：目标话题上下文为空",
            "record_id": record["record_id"],
            "status_code": 503,
        }

    builder = getattr(ctx, "build_adapter", None)
    adapter = None
    if callable(builder):
        try:
            adapter = await builder()
        except Exception as exc:  # noqa: BLE001 - 模型通道失败：如实失败，记录保留
            failure = _error_detail(exc)
            update_record(ctx, record_id, last_error=failure, last_attempt_at=_now_iso())
            return {
                "ok": False,
                "code": "model_unavailable",
                "message": "回答生成失败（模型通道不可用）：" + failure,
                "record_id": record["record_id"],
                "status_code": 503,
            }
    if adapter is None:
        update_record(ctx, record_id, last_error="model_unavailable", last_attempt_at=_now_iso())
        return {
            "ok": False,
            "code": "model_unavailable",
            "message": "回答生成失败（没有可用的模型通道）",
            "record_id": record["record_id"],
            "status_code": 503,
        }
    try:
        answer = await _generate_answer(adapter, messages)
    except Exception as exc:  # noqa: BLE001 - 回答失败单独记 answer 阶段，单独恢复
        failure = _error_detail(exc)
        stages = _stages_from_record(record)
        stages.failed_stage = "answer"
        stages.failure = failure
        update_record(
            ctx,
            record_id,
            failed_stage="answer",
            stages=stages.as_dict(),
            last_error=failure,
            last_attempt_at=_now_iso(),
        )
        return {
            "ok": False,
            "code": "topic_answer_failed",
            "recoverable": True,
            "failed_stage": "answer",
            "message": "话题已切换，回答生成失败：" + failure,
            "record_id": record["record_id"],
            "status_code": 503,
        }
    if not answer.strip():
        update_record(
            ctx, record_id, failed_stage="answer",
            last_error="empty_answer", last_attempt_at=_now_iso(),
        )
        return {
            "ok": False,
            "code": "topic_answer_failed",
            "recoverable": True,
            "failed_stage": "answer",
            "message": "话题已切换，回答生成失败：模型没有给出可交付正文",
            "record_id": record["record_id"],
            "status_code": 503,
        }

    # 持久化：只追加**一条**归属目标话题的助手消息，不新增用户消息 / 不新建轮次。
    memory = getattr(ctx, "memory", None)
    if memory is None:
        return {
            "ok": False,
            "code": "recovery_persist_failed",
            "message": "回答生成成功，但没有可用的消息存储",
            "record_id": record["record_id"],
            "status_code": 503,
        }
    try:
        message_id, _closed = memory.append_message(
            topic_id=str(record.get("topic_id") or ""),
            role="assistant",
            content=answer,
            content_type="text",
            model=getattr(adapter, "model", None),
            raw={"recovered": True, "recovery_record_id": record["record_id"]},
            turn_id=(str(record.get("turn_id") or "") or None),
        )
    except Exception as exc:  # noqa: BLE001 - 写失败：记录保持 pending，可再重试
        failure = _error_detail(exc)
        update_record(
            ctx,
            record_id,
            last_error="persist_failed: " + failure,
            last_attempt_at=_now_iso(),
        )
        return {
            "ok": False,
            "code": "recovery_persist_failed",
            "recoverable": True,
            "message": "回答已生成但持久化失败：" + failure,
            "record_id": record["record_id"],
            "status_code": 503,
        }

    updated = update_record(
        ctx,
        record_id,
        status="answered",
        answer=answer,
        message_id=message_id,
        failed_stage="",
        last_error=None,
        last_attempt_at=_now_iso(),
    ) or record
    return {
        "ok": True,
        "answered": True,
        "record_id": updated["record_id"],
        "turn_id": updated.get("turn_id"),
        "topic_id": updated.get("topic_id"),
        "answer": answer,
        "answer_id": message_id,
        "failed_stage": record.get("failed_stage"),
        "navigation_version_check": navigation_version_check,
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _error_detail(exc: Exception) -> str:
    """异常 → 脱敏短说明（新增输出路径必须过 redact）。"""
    from agent.trace.redact import sanitize_error_text

    return sanitize_error_text(str(exc)) or type(exc).__name__


def _stages_from_record(record: dict):
    stages = TopicStages()
    payload = record.get("stages") or {}
    for name in TopicStages.__dataclass_fields__:  # type: ignore[attr-defined]
        if name in payload:
            setattr(stages, name, payload[name])
    return stages


def new_record_id() -> str:
    return f"trec_{uuid.uuid4().hex[:12]}"


def record_topic_failure(
    scope: Any,
    *,
    turn_id: str,
    topic_id: str | None,
    failed_stage: str,
    message: str,
    from_topic_id: str | None = None,
    user_message_id: str | None = None,
    operation_id: str | None = None,
    param_version: int | None = None,
    base_nav_version: int | None = None,
    # 失败时刻的真实位置版本（恢复基线）：话题工具成功导航会先推进版本，
    # 申请起点快照（base_nav_version）在恢复时必然落后，不能拿它做复核。
    recovery_nav_version: int | None = None,
    stages: TopicStages | None = None,
    record_id: str | None = None,
) -> dict:
    """写入一条可恢复记录（同 record_id 幂等覆盖，状态保持 pending）。"""
    store = _store(scope)
    rid = str(record_id or "") or new_record_id()
    record = {
        "record_id": rid,
        "kind": RECOVERY_KIND,
        "status": "pending",
        "turn_id": str(turn_id or ""),
        "topic_id": str(topic_id or ""),
        "from_topic_id": from_topic_id,
        "user_message_id": user_message_id,
        "operation_id": operation_id,
        "param_version": param_version,
        "base_nav_version": base_nav_version,
        "recovery_nav_version": recovery_nav_version,
        "failed_stage": str(failed_stage or ""),
        "message": str(message or ""),
        "answer": None,
        "stages": stages.as_dict() if stages is not None else {},
    }
    previous = store.get(rid)
    if previous is not None:
        record["status"] = previous.get("status", "pending")
        record["answer"] = previous.get("answer")
    store[rid] = record
    return dict(record)


def record_topic_answer_failure(scope: Any, **kwargs) -> dict:
    """回答阶段失败：单独记 `failed_stage="answer"`（不得与上下文失败互相覆盖）。"""
    kwargs.setdefault("failed_stage", "answer")
    return record_topic_failure(scope, **kwargs)


def get_record(scope: Any, record_id: str) -> dict | None:
    record = _store(scope).get(str(record_id or ""))
    return dict(record) if isinstance(record, dict) else None


def list_records(scope: Any, *, status: str | None = None) -> list[dict]:
    records = [dict(item) for item in _store(scope).values() if isinstance(item, dict)]
    if status is not None:
        records = [item for item in records if item.get("status") == status]
    return records


def update_record(scope: Any, record_id: str, **fields: Any) -> dict | None:
    store = _store(scope)
    record = store.get(str(record_id or ""))
    if not isinstance(record, dict):
        return None
    record.update(fields)
    return dict(record)


def find_pending(
    scope: Any, *, turn_id: str | None = None, user_message_id: str | None = None
) -> dict | None:
    for record in _store(scope).values():
        if not isinstance(record, dict) or record.get("status") != "pending":
            continue
        if turn_id is not None and record.get("turn_id") != str(turn_id):
            continue
        if user_message_id is not None and record.get("user_message_id") != user_message_id:
            continue
        return dict(record)
    return None


def mark_answered(scope: Any, record_id: str, answer: str) -> dict | None:
    """回答已经真实产生并持久化：记录收口为 answered（重复调用返回既有结果）。"""
    return update_record(
        scope,
        record_id,
        status="answered",
        answer=str(answer or ""),
        failed_stage="",
    )
