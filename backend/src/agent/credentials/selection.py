"""凭据目录与自主选择辅助（阶段四 A；契约 §14.3）。

两件事共用同一份实现，避免「目录里能看到的」和「子任务真正能选的」漂移：

1. 目录（供 list_available_credentials 工具只读展示）：每项给出 credential_ref /
   provider / models / capabilities / availability / usage_limits；
   **永不返回密钥原文**，也不把业务服务凭据（搜索等设置里的 Key）当成模型凭据。
2. 选择（供 subagent 工具与调用方使用）：尊重用户/工具定义的明确指定；只从政策
   允许的候选里选（active、启用、验证通过、用途标签、预算未耗尽）；没有明确指定时
   按「用户默认项 → 预算余额降序 → key_id 稳定排序」自主选择，不固定首条。
   旧 credential_key 只在这里归一化为 credential_ref；双参不一致直接拒绝。

选择结果只包含元数据（含 credential_ref），绝不包含密钥；实际请求由既有
adapter/adapter_factory 用 credential_ref 取密钥并发起，用量归因也回到同一 ref。
"""

from __future__ import annotations

import re
from typing import Any

from agent.credentials.policy import BudgetExhausted, ensure_budget_available
from agent.credentials.providers import (
    CATEGORY_LABELS,
    KIND_ANTHROPIC,
    KIND_OPENAI,
    PRESETS,
    uses_anthropic,
)
from agent.credentials.store import USABLE_VERIFY_STATES

MODEL_CREDENTIAL_KIND = "model"
SERVICE_CREDENTIAL_KIND = "service"

SUBTASK_TAG = "subagent"
MAIN_LOOP_TAG = "main-loop"

MAX_CREDENTIAL_REF_CHARS = 200
MAX_MODEL_CHARS = 128
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}$")

#: 业务服务凭据：只报告「有没有配置 / 是否可用」，永不返回值本身。
#: 这些键属于设置（settings），与模型凭据（credentials 表）是两类东西。
SERVICE_CREDENTIAL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "search.bocha_api_key",
        "provider": "bocha",
        "name": "博查搜索",
        "capabilities": ["web_search"],
        "section": "search",
        "kind": "api_key",
    },
    {
        "id": "search.searxng_url",
        "provider": "searxng",
        "name": "SearXNG 搜索地址",
        "capabilities": ["web_search"],
        "section": "search",
        "kind": "endpoint",
    },
)


class CredentialSelectionError(RuntimeError):
    """没有可用的凭据 / 指定不合法：调用方据此如实失败，不静默换别的凭据。"""


def preset_for(endpoint: str | None, kind: str | None = None) -> dict[str, Any] | None:
    """根据端点地址找厂商预设（唯一来源 providers.PRESETS）。"""
    text = str(endpoint or "").strip().rstrip("/")
    if text:
        for preset in PRESETS:
            base = preset.base_url.rstrip("/")
            if text == base or text.startswith(base + "/"):
                return preset.to_dict()
    stored = str(kind or "").strip()
    if stored:
        for preset in PRESETS:
            if preset.kind == stored:
                return preset.to_dict()
    return None


def _protocol_of(meta: dict[str, Any]) -> str:
    kind = str(meta.get("kind") or "").strip()
    if kind in (KIND_OPENAI, KIND_ANTHROPIC):
        return kind
    return KIND_ANTHROPIC if uses_anthropic(None, meta.get("endpoint")) else KIND_OPENAI


def _capabilities_of(meta: dict[str, Any], protocol: str) -> list[str]:
    capabilities = ["chat", "tools"]
    tags = {str(tag) for tag in (meta.get("tags") or [])}
    if "vision" in tags:
        capabilities.append("vision")
    if "embedding" in tags:
        capabilities.append("embedding")
    if protocol == KIND_ANTHROPIC:
        capabilities.append("long_context")
    return capabilities


def _availability(meta: dict[str, Any], store: Any) -> dict[str, Any]:
    """政策可用性：状态 / 启用 / 验证 / 预算四项，原因如实写出（不含密钥）。"""
    reasons: list[str] = []
    # 真实凭据元数据总会带这些字段；旧替身 / 精简元数据缺字段时不臆断「不可用」，
    # 只对**明确写出**的不可用状态给出原因（安全判断不靠猜测）。
    if "status" in meta and str(meta.get("status") or "") != "active":
        reasons.append("凭据状态不是 active")
    if "enabled" in meta and not bool(meta.get("enabled", True)):
        reasons.append("凭据已停用")
    if "verify_state" in meta:
        verify_state = str(meta.get("verify_state") or "unverified")
        if verify_state not in USABLE_VERIFY_STATES:
            reasons.append(f"凭据尚未通过验证（{verify_state}）")
    budget_left: float | None = None
    reader = getattr(store, "budget_left", None)
    if callable(reader):
        try:
            budget_left = reader(str(meta.get("id")))
        except Exception:  # noqa: BLE001 - 账本读失败不等于可用
            reasons.append("用量账本暂不可读")
    if budget_left is not None and budget_left <= 0:
        reasons.append("累计用量上限已耗尽")
    return {
        "usable": not reasons,
        "reasons": reasons,
        "budget_left": budget_left,
    }


def model_credential_entry(store: Any, meta: dict[str, Any]) -> dict[str, Any]:
    """一条模型凭据的目录项（只有元数据，没有密钥）。"""
    ref = str(meta.get("id") or "")
    endpoint = meta.get("endpoint")
    preset = preset_for(endpoint, meta.get("kind"))
    protocol = _protocol_of(meta)
    provider = str((preset or {}).get("id") or "custom")
    provider_name = str((preset or {}).get("name") or "自定义端点")
    default_model = str(meta.get("default_model") or "").strip()
    models: list[str] = []
    if default_model:
        models.append(default_model)
    suggested = str((preset or {}).get("suggested_model") or "").strip()
    if suggested and suggested not in models:
        models.append(suggested)
    availability = _availability(meta, store)
    return {
        "kind": MODEL_CREDENTIAL_KIND,
        "credential_ref": ref,
        "provider": provider,
        "provider_name": provider_name,
        "provider_category": str((preset or {}).get("category") or ""),
        "provider_category_label": CATEGORY_LABELS.get(
            str((preset or {}).get("category") or ""), ""
        ),
        "protocol": protocol,
        "models": models,
        "default_model": default_model or None,
        "capabilities": _capabilities_of(meta, protocol),
        "availability": availability,
        "usage_limits": {
            "budget": meta.get("budget"),
            "budget_used": meta.get("budget_used"),
            "budget_left": availability.get("budget_left"),
        },
        "tags": list(meta.get("tags") or []),
        "is_default": bool(meta.get("is_default")),
        "verify_state": str(meta.get("verify_state") or "unverified"),
    }


def service_credential_entries(settings_store: Any) -> list[dict[str, Any]]:
    """业务服务凭据目录：只说明是否已配置与能力，不返回任何值。"""
    entries: list[dict[str, Any]] = []
    for spec in SERVICE_CREDENTIAL_SPECS:
        configured = False
        if settings_store is not None:
            getter = getattr(settings_store, "get", None)
            if callable(getter):
                try:
                    configured = bool(str(getter(spec["id"]) or "").strip())
                except Exception:  # noqa: BLE001 - 设置读失败按未配置如实报告
                    configured = False
        entries.append(
            {
                "kind": SERVICE_CREDENTIAL_KIND,
                "credential_ref": spec["id"],
                "provider": spec["provider"],
                "provider_name": spec["name"],
                "provider_category": "",
                "provider_category_label": "",
                "protocol": "",
                "models": [],
                "default_model": None,
                "capabilities": list(spec["capabilities"]),
                "availability": {
                    "usable": configured,
                    "reasons": [] if configured else ["尚未配置"],
                    "budget_left": None,
                },
                "usage_limits": {"budget": None, "budget_used": None, "budget_left": None},
                "tags": [spec["section"]],
                "is_default": False,
                "verify_state": "configured" if configured else "unconfigured",
                "service_kind": spec["kind"],
            }
        )
    return entries


def list_available_credentials(
    store: Any,
    *,
    purpose: str = "subagent",
    settings_store: Any = None,
) -> list[dict[str, Any]]:
    """按 purpose 列出可用凭据目录项；模型凭据与业务服务凭据分开展示。"""
    wanted = str(purpose or "subagent").strip().lower()
    if wanted not in ("subagent", "model", "service", "all"):
        raise CredentialSelectionError("purpose 只能是 subagent / model / service / all")
    entries: list[dict[str, Any]] = []
    if wanted in ("subagent", "model", "all"):
        lister = getattr(store, "list_credentials", None)
        metas = list(lister()) if callable(lister) else []
        for meta in metas:
            if not isinstance(meta, dict):
                continue
            tags = {str(tag) for tag in (meta.get("tags") or [])}
            if wanted == "subagent" and not ({SUBTASK_TAG, MAIN_LOOP_TAG} & tags):
                # 非子任务用途的专项凭据（vision 等）默认不列出；明确指定仍可用。
                continue
            entry = model_credential_entry(store, meta)
            entry["purpose_fit"] = SUBTASK_TAG in tags if wanted == "subagent" else True
            entries.append(entry)
    if wanted in ("service", "all"):
        entries.extend(service_credential_entries(settings_store))
    return entries


def normalize_credential_selector(
    *,
    credential_ref: Any = None,
    credential_key: Any = None,
) -> tuple[str, bool]:
    """把新旧两种写法归一化为 credential_ref；双参不一致直接拒绝。

    返回 (credential_ref, legacy)，legacy=True 表示来源是旧 credential_key。
    """
    ref = str(credential_ref or "").strip()
    key = str(credential_key or "").strip()
    if ref and key and ref != key:
        raise CredentialSelectionError(
            "credential_ref 与旧 credential_key 同时给出但不一致：拒绝自动猜测，请只使用 credential_ref"
        )
    if ref:
        return ref, False
    if key:
        return key, True
    return "", False


def validate_model_name(model: Any) -> str:
    """模型名严格校验：非空、有限字符集与长度；不做网络探测。"""
    text = str(model or "").strip()
    if not text:
        raise CredentialSelectionError("model 不能为空")
    if len(text) > MAX_MODEL_CHARS or not _MODEL_RE.match(text):
        raise CredentialSelectionError(
            f"model 不合法（只允许字母数字与 . _ / : -，长度 1..{MAX_MODEL_CHARS}）"
        )
    return text


def _explicit_choice(store: Any, ref: str, *, legacy: bool) -> dict[str, Any]:
    meta = store.get_metadata(ref) if hasattr(store, "get_metadata") else None
    if meta is None:
        raise CredentialSelectionError(f"没有这条凭据：{ref}")
    availability = _availability(meta, store)
    if not availability["usable"]:
        raise CredentialSelectionError(
            f"凭据 {ref} 当前不可用：" + "；".join(availability["reasons"])
        )
    entry = model_credential_entry(store, meta)
    model = str(entry.get("default_model") or "").strip()
    if not model and entry.get("models"):
        model = str(entry["models"][0])
    return {
        "credential_ref": ref,
        "model": model,
        "provider": entry["provider"],
        "provider_name": entry["provider_name"],
        "protocol": entry["protocol"],
        "tags": entry["tags"],
        "is_default": entry["is_default"],
        "explicit": True,
        "legacy": legacy,
        "availability": entry["availability"],
        "usage_limits": entry["usage_limits"],
    }


def resolve_credential_choice(
    store: Any,
    *,
    credential_ref: Any = None,
    credential_key: Any = None,
    purpose: str = "subagent",
    fallback_ref: Any = None,
) -> dict[str, Any]:
    """为一次子任务选择凭据：尊重明确指定，否则按政策自主选择。

    顺序：明确指定（_qio_subtask.credential_ref / 旧 credential_key / 工具定义的
    credential_ref）→ 用途标签候选中的用户默认项 → 预算余额降序 → key_id 稳定排序。
    任何不可用都抛 CredentialSelectionError（调用方如实失败，不静默换用别的）。
    """
    ref, legacy = normalize_credential_selector(
        credential_ref=credential_ref, credential_key=credential_key
    )
    if ref:
        return _explicit_choice(store, ref, legacy=legacy)
    fallback = str(fallback_ref or "").strip()
    if fallback:
        choice = _explicit_choice(store, fallback, legacy=False)
        choice["explicit"] = False
        choice["from_tool_definition"] = True
        return choice

    wanted = str(purpose or "subagent").strip().lower()
    tag = SUBTASK_TAG if wanted == "subagent" else MAIN_LOOP_TAG
    candidates: list[dict[str, Any]] = []
    lister = getattr(store, "list_tagged", None)
    if callable(lister):
        try:
            candidates = [m for m in lister(tag) if isinstance(m, dict)]
        except Exception:  # noqa: BLE001 - 读候选失败按没有候选处理（如实失败）
            candidates = []
    if not candidates and tag != MAIN_LOOP_TAG:
        # 没有专项标签时回落到主循环可用凭据（旧行为，明确标注来源）
        try:
            candidates = (
                [m for m in lister(MAIN_LOOP_TAG) if isinstance(m, dict)]
                if callable(lister)
                else []
            )
        except Exception:  # noqa: BLE001
            candidates = []
    if not candidates:
        default_meta = None
        getter = getattr(store, "effective_default", None)
        if callable(getter):
            try:
                default_meta = getter()
            except Exception:  # noqa: BLE001
                default_meta = None
        if isinstance(default_meta, dict):
            candidates = [default_meta]
    if not candidates:
        raise CredentialSelectionError(
            "没有可用于子任务的凭据：请先在设置中配置并验证一条可用的模型凭据"
        )
    default_meta = None
    getter = getattr(store, "effective_default", None)
    if callable(getter):
        try:
            default_meta = getter()
        except Exception:  # noqa: BLE001
            default_meta = None
    default_ref = str((default_meta or {}).get("id") or "")
    chosen = None
    for meta in candidates:
        if str(meta.get("id") or "") == default_ref:
            chosen = meta
            break
    if chosen is None:
        for meta in candidates:
            if bool(meta.get("is_default")):
                chosen = meta
                break
    if chosen is None:
        chosen = candidates[0]  # list_tagged 已按预算余额降序、key_id 稳定排序
    chosen_ref = str(chosen.get("id") or "")
    try:
        ensure_budget_available(store, chosen_ref)
    except BudgetExhausted as exc:
        raise CredentialSelectionError(f"凭据 {chosen_ref} 的用量上限已耗尽，未发起子任务") from exc
    entry = model_credential_entry(store, chosen)
    model = str(entry.get("default_model") or "").strip()
    if not model and entry.get("models"):
        model = str(entry["models"][0])
    if not model:
        raise CredentialSelectionError(
            f"凭据 {chosen_ref} 没有可用模型：请在选择时显式给出 model 或先设置默认模型"
        )
    return {
        "credential_ref": chosen_ref,
        "model": model,
        "provider": entry["provider"],
        "provider_name": entry["provider_name"],
        "protocol": entry["protocol"],
        "tags": entry["tags"],
        "is_default": entry["is_default"],
        "explicit": False,
        "legacy": False,
        "from_tool_definition": False,
        "availability": entry["availability"],
        "usage_limits": entry["usage_limits"],
    }


def choose_model(choice: dict[str, Any], requested: Any = None) -> str:
    """控制字段给出的 model 必须合法；缺省用目录为这条凭据解析出的模型。"""
    if requested is None or str(requested).strip() == "":
        model = str(choice.get("model") or "").strip()
        if not model:
            raise CredentialSelectionError("没有可用模型：请显式给出 model")
        return validate_model_name(model)
    return validate_model_name(requested)
