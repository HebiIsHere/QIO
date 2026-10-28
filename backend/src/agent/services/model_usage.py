"""用途分配的原子批量保存 + 目录里「实际生效目标」的唯一算法（任务 D）。

为什么单独一个模块
------------------
前端把主对话 / 通用对话合并成一行显示（两者共用同一条调用路径），于是用户的一次
选择要同时写两个用途 key。以前前端分两次 `POST /api/models/usage` 顺序保存：
第一次成功、第二次失败就留下「主对话换了、通用对话没换」的半截配置，而运行时
（`AppContext.resolve_main_target`）又是通用对话显式优先 —— 用户看到一半、实际
调用另一半。这里把「多项用途分配」收成一个事务入口：

* 先把整批结构校验完（用途存在、连接/模型成对、不能重复），再在**一个** SQLite
  transaction 内逐项写；任意一项抛错 → 整个事务回滚，一行都不会留下
  （`agent.storage.db.transaction` 在外层事务里复用外层，不会嵌套 BEGIN）；
* 单用途旧接口 `POST /api/models/usage` 保持原样，新入口只是「多项一起成功/失败」。

「实际生效目标」= 与运行时同一份解析
------------------------------------
合并行的真实规则就是 `resolve_shared_target(["general-chat", "main-chat"],
primary_key="main-chat")`（`AppContext.resolve_main_target` 直接调它）。目录里的
`effective` 必须**调用同一个函数**算出来，而不是自己再写一套「先看 main-chat」的
判断：两份判断一旦分叉，界面显示一个、真实调用另一个，就是本轮要修的问题本身。

这里只读本地事实（设置 / 连接元数据 / 用途分配表），不发请求、不建 adapter。
"""

from __future__ import annotations

from typing import Any

from agent.credentials.usages import find_usage, normalize_usage_key
from agent.services.usage_target import (
    UsageUnavailable,
    credential_problem,
    resolve_shared_target,
)
from agent.storage.db import transaction

__all__ = [
    "MERGED_CHAT_USAGE_KEYS",
    "MERGED_CHAT_PRIMARY_KEY",
    "attach_effective_chat_target",
    "build_effective_chat_target",
    "save_assignments",
]

#: 共用同一条调用路径的两个日常入口；顺序即显式优先级（通用对话优先于主对话）。
MERGED_CHAT_USAGE_KEYS: tuple[str, str] = ("general-chat", "main-chat")
#: 真正被调用的那条路径（解析结果与错误文案里说的用途）。
MERGED_CHAT_PRIMARY_KEY = "main-chat"


def _assignment_entries(assignments: Any) -> list[dict[str, Any]]:
    """校验并规范化一批用途分配；任何一项不合法都整批拒绝（不写库）。"""
    if not isinstance(assignments, list) or not assignments:
        raise ValueError("请至少提交一项用途分配")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(assignments, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 项用途分配的格式不对")
        usage_key = str(raw.get("usage_key") or "").strip()
        if not usage_key:
            raise ValueError(f"第 {index} 项用途分配缺少用途标识")
        key = normalize_usage_key(usage_key)
        if find_usage(key) is None:
            raise ValueError(f"第 {index} 项用途分配引用了未知用途：{usage_key}")
        if key in seen:
            raise ValueError(f"第 {index} 项用途分配重复指定了同一个用途：{usage_key}")
        seen.add(key)

        credential_id = raw.get("credential_id")
        model_id = raw.get("model_id")
        credential_text = str(credential_id).strip() if credential_id is not None else ""
        model_text = str(model_id).strip() if model_id is not None else ""
        if not credential_text and model_text:
            raise ValueError(f"第 {index} 项用途分配只给了模型没给连接")
        if credential_text and not model_text:
            raise ValueError(f"第 {index} 项用途分配只给了连接没给模型")
        entries.append(
            {
                "usage_key": key,
                "credential_id": credential_text or None,
                "model_id": model_text or None,
            }
        )
    return entries


def save_assignments(store: Any, assignments: Any) -> dict[str, Any]:
    """把一批用途分配**原子地**写进 `model_usages`（全成功或全回滚）。

    返回形状与单用途接口一致：`usages` 是保存后重新读回的完整用途视图
    （每项都带 resolved），界面据此展示「刚保存并真实读回」的配置，而不是点击草稿。
    """
    entries = _assignment_entries(assignments)
    applied: list[dict[str, Any]] = []
    with transaction(store.conn):
        for index, entry in enumerate(entries, start=1):
            try:
                applied.append(
                    store.set_usage(
                        entry["usage_key"], entry["credential_id"], entry["model_id"]
                    )
                )
            except KeyError as exc:  # 连接不存在：保留 404 语义
                message = exc.args[0] if exc.args else str(exc)
                raise KeyError(f"第 {index} 项用途分配无效：{message}") from exc
            except ValueError as exc:  # 形状 / 用途 / 模型不合法：保留 400 语义
                raise ValueError(f"第 {index} 项用途分配无效：{exc}") from exc
    return {"ok": True, "assignments": applied, "usages": store.list_usages()}


def _unavailable_payload(exc: UsageUnavailable) -> dict[str, Any]:
    """解析抛出的不可用 → 目录可展示的事实（不抛给 GET 让整页失败）。"""
    return {
        "ok": False,
        "usage_key": exc.usage_key,
        "credential_id": exc.credential_id,
        "model_id": exc.model_id,
        "source": "",
        "matched_usage_key": exc.usage_key,
        "usable": False,
        "unavailable_reason": exc.message,
        "reason_code": exc.reason_code,
        "detail_code": exc.detail_code,
    }


def build_effective_chat_target(ctx: Any) -> dict[str, Any]:
    """用**运行时同一个** resolve_shared_target 算出日常对话实际会打到哪。

    成功时仍补一次连接级可用性判断（`credential_problem`）：政策回退分支不检查
    「密钥是否还可读」，而真实调用一定会在建 adapter 时失败 —— 目录不能把这种
    目标说成可用。字段口径：
    `ok / credential_id / model_id / source / matched_usage_key / usable /
    unavailable_reason / reason_code / detail_code`。
    """
    credentials = getattr(ctx, "credentials", None)
    try:
        target = resolve_shared_target(
            credentials,
            getattr(ctx, "settings_store", None),
            list(MERGED_CHAT_USAGE_KEYS),
            policy=getattr(ctx, "policy", None),
            primary_key=MERGED_CHAT_PRIMARY_KEY,
        )
    except UsageUnavailable as exc:
        return _unavailable_payload(exc)

    payload: dict[str, Any] = {
        "ok": True,
        "usage_key": target.usage_key,
        "credential_id": target.credential_id,
        "model_id": target.model_id,
        "source": target.source,
        "matched_usage_key": target.matched_usage_key or target.usage_key,
        "usable": True,
        "unavailable_reason": "",
        "reason_code": "",
        "detail_code": "",
        "degraded": target.degraded,
        "degrade_reason": target.degrade_reason,
    }
    problem = credential_problem(credentials, target.credential_id)
    if problem is not None:
        reason_code, message = problem
        payload.update(
            {
                "usable": False,
                "unavailable_reason": message,
                "reason_code": reason_code,
                "detail_code": reason_code,
            }
        )
    return payload


def attach_effective_chat_target(ctx: Any, catalog: dict[str, Any]) -> dict[str, Any]:
    """给目录里的 main-chat / general-chat 补上同一份 `effective`（就地修改并返回）。"""
    effective = build_effective_chat_target(ctx)
    for item in catalog.get("usages") or []:
        if str(item.get("usage_key") or "") in MERGED_CHAT_USAGE_KEYS:
            item["effective"] = dict(effective)
    return catalog
