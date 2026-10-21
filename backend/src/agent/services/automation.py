# -*- coding: utf-8 -*-
"""自动化建议（角色 C，阶段四契约 §14.5）。

职责：重复出现的用户请求 → 是否值得形成自动化工具建议。

硬性口径：
* 判断依据 = 完整请求 + 实际使用结果 + 已有工具目录（不是只看前 200 字，也不只看文字相似）；
* 输出固定 schema：{"suggestions": [{name, description, reason, parameters, evidence_ids}]}；
* 允许空：模型不可用、输出无效、没有稳定能力缺口时都返回空并说明原因，绝不伪造草案；
* 建议不是开发授权：这里只产出建议与审批请求，用户同意后由既有 tool_candidate 审批进入
  ctx.dev_workspaces.create 的统一开发流程；
* 已拒绝且没有新依据的同一建议不重复催问（拒绝记录持久在 settings 表，不新增迁移）。

模型调用走公共结构化任务入口 run_structured_task：
system = prompts.structured_task_system("AUTOMATION_SUGGESTION_SYSTEM", schema, limits)
（公共结构化规则 + 任务规则 + 程序生成的 schema + limits），
user = 一条独立 JSON 数据（json.dumps），不拼接主对话、不注入聊天回答标记。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.tools.contracts import json_schema_for

logger = logging.getLogger(__name__)

#: 单轮最多采纳的建议数（schema 与本地校验共用同一组上限）。
MAX_SUGGESTIONS = 3
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 2000
MAX_REASON_CHARS = 1500
MAX_EVIDENCE_IDS = 20

AUTOMATION_LIMITS: dict[str, int] = {
    "max_suggestions": MAX_SUGGESTIONS,
    "max_name_chars": MAX_NAME_CHARS,
    "max_description_chars": MAX_DESCRIPTION_CHARS,
    "max_reason_chars": MAX_REASON_CHARS,
    "max_evidence_ids": MAX_EVIDENCE_IDS,
}

#: 拒绝记录在 settings 表里的键（不新增迁移）。
REJECTIONS_KEY = "automation.rejected_suggestions"


class AutomationSuggestion(BaseModel):
    """一条自动化工具建议（模型输出单项；结构合法性本地强制执行）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    reason: str = Field(default="", max_length=MAX_REASON_CHARS)
    parameters: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_IDS)

    @field_validator("name", "description", "reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class AutomationSuggestionOutput(BaseModel):
    """自动化建议任务的完整结构化输出（允许列表为空）。"""

    model_config = ConfigDict(extra="forbid")

    suggestions: list[AutomationSuggestion] = Field(
        default_factory=list, max_length=MAX_SUGGESTIONS
    )


def automation_output_schema() -> dict[str, Any]:
    """展示给模型的建议输出 JSON Schema（与本地校验同一类型）。"""
    return json_schema_for(AutomationSuggestionOutput)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _load_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """从模型输出里取出 JSON 对象；允许代码块围栏包裹。"""
    import re

    raw = str(text or "").strip()
    match = re.search(r"\x60{3}(?:json)?\s*(.*?)\x60{3}", raw, re.DOTALL)
    candidate = match.group(1) if match else raw
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "输出必须是 JSON 对象"
    return data, None


def _field_errors(exc: Exception) -> list[str]:
    errors: list[str] = []
    for item in getattr(exc, "errors", lambda: [])():
        location = ".".join(str(part) for part in item.get("loc", ()))
        errors.append(f"{location}: {item.get('msg')}")
    return errors or [str(exc)]


def validate_automation_output(text: str) -> tuple[AutomationSuggestionOutput | None, list[str]]:
    """解析 + 本地校验建议输出；无效返回 (None, 字段错误)。"""
    payload, error = _load_json_object(text)
    if payload is None:
        return None, [error or "输出不可解析"]
    try:
        return AutomationSuggestionOutput.model_validate(payload), []
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        return None, _field_errors(exc)


def suggestion_signature(suggestion: Mapping[str, Any]) -> str:
    """建议的稳定指纹：名字 + 描述（拒绝记录按它判重）。"""
    name = _clean_text(suggestion.get("name")).lower()
    description = _clean_text(suggestion.get("description"))
    digest = hashlib.sha1((name + "\n" + description).encode("utf-8")).hexdigest()
    return digest


def basis_fingerprint(requests: Iterable[str]) -> str:
    """当前判断依据的指纹：完整请求文本集合（排序后）。"""
    normalised = sorted({_clean_text(item) for item in requests if _clean_text(item)})
    digest = hashlib.sha1("\n".join(normalised).encode("utf-8")).hexdigest()
    return digest


def load_rejections(conn) -> dict[str, dict[str, Any]]:
    """读取拒绝记录（settings 表；不存在或损坏都按空处理，不影响维护）。"""
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (REJECTIONS_KEY,)
        ).fetchone()
    except Exception:  # noqa: BLE001 - 旧库 / 替身连接不阻塞维护
        return {}
    if row is None:
        return {}
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_rejections(conn, data: Mapping[str, Any]) -> None:
    payload = json.dumps(dict(data), ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        (REJECTIONS_KEY, payload, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def record_rejection(
    conn,
    *,
    suggestion: Mapping[str, Any],
    reason: str,
    basis: str,
) -> None:
    """记录用户拒绝：同一指纹 + 同一依据下不再重复催问。"""
    signature = suggestion_signature(suggestion)
    data = load_rejections(conn)
    data[signature] = {
        "name": _clean_text(suggestion.get("name")),
        "description": _clean_text(suggestion.get("description")),
        "basis": str(basis or ""),
        "reason": _clean_text(reason)[:500],
        "rejected_at": datetime.now(timezone.utc).isoformat(),
    }
    if len(data) > 50:
        for key in list(data)[: len(data) - 50]:
            data.pop(key, None)
    _save_rejections(conn, data)


def filter_suggestions(
    suggestions: Sequence[AutomationSuggestion],
    *,
    existing_tool_names: Iterable[str],
    rejections: Mapping[str, Mapping[str, Any]],
    basis: str,
) -> tuple[list[AutomationSuggestion], list[dict[str, str]]]:
    """程序级过滤：已有工具能覆盖 / 已拒绝且无新依据的建议不进入审批。

    返回 (保留的建议, 跳过的原因列表)。纯函数，便于测试。
    """
    existing = {_clean_text(name).lower() for name in existing_tool_names if _clean_text(name)}
    kept: list[AutomationSuggestion] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()
    for suggestion in suggestions:
        signature = suggestion_signature(
            {"name": suggestion.name, "description": suggestion.description}
        )
        if signature in seen:
            skipped.append({"name": suggestion.name, "reason": "duplicate"})
            continue
        seen.add(signature)
        if suggestion.name.strip().lower() in existing:
            skipped.append({"name": suggestion.name, "reason": "existing_tool"})
            continue
        rejected = rejections.get(signature)
        if isinstance(rejected, Mapping) and str(rejected.get("basis") or "") == str(basis or ""):
            skipped.append({"name": suggestion.name, "reason": "rejected_no_new_basis"})
            continue
        kept.append(suggestion)
    return kept, skipped


def build_request_data(
    *,
    requests: Sequence[Mapping[str, Any]],
    actual_results: Sequence[Mapping[str, Any]],
    existing_tools: Sequence[Mapping[str, Any]],
    rejected: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """组建议任务的独立 JSON 数据（只含数据，不含 system / 回答标记）。"""
    return {
        "requests": [dict(item) for item in requests],
        "actual_results": [dict(item) for item in actual_results],
        "existing_tools": [dict(item) for item in existing_tools],
        "previously_rejected": [dict(item) for item in rejected],
        "limits": dict(AUTOMATION_LIMITS),
    }


async def suggest_automations(
    adapter: Any,
    *,
    requests: Sequence[Mapping[str, Any]],
    actual_results: Sequence[Mapping[str, Any]] = (),
    existing_tools: Sequence[Mapping[str, Any]] = (),
    rejected: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[AutomationSuggestion], str | None]:
    """一次自动化建议的结构化调用（允许空；失败返回空 + 原因，不伪造）。"""
    if adapter is None:
        return [], "no adapter"
    if not requests:
        return [], "no repeated requests"
    from agent.maintenance.runner import run_structured_task

    data = build_request_data(
        requests=requests,
        actual_results=actual_results,
        existing_tools=existing_tools,
        rejected=rejected,
    )
    try:
        value, errors, _meta = await run_structured_task(
            adapter,
            prompt_key="AUTOMATION_SUGGESTION_SYSTEM",
            schema=automation_output_schema(),
            limits=AUTOMATION_LIMITS,
            data=data,
            validate=validate_automation_output,
        )
    except Exception as exc:  # noqa: BLE001 - 后台建议失败不影响主流程
        from agent.trace.redact import redact_text

        reason = redact_text(f"{type(exc).__name__}: {exc}") or "模型调用失败"
        logger.warning("automation suggestion failed: %s", reason)
        return [], reason
    if value is None:
        return [], "; ".join(errors[:3]) or "输出无效"
    return list(value.suggestions), None


def existing_tool_catalog() -> list[dict[str, str]]:
    """内置工具目录（名字 + 说明），让模型判断是否已有工具满足需求。"""
    from agent.prompts import TOOL_DESCRIPTIONS

    return [
        {"name": str(name), "description": _clean_text(description)}
        for name, description in TOOL_DESCRIPTIONS.items()
    ]
