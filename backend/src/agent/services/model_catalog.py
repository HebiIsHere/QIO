"""模型发现服务：只有用户**提交连接之后**才向该连接保存的地址发一次认证请求。

四条硬约束（产品要求，也是安全边界）
--------------------------------
1. 只问**已保存的那一个地址**：不探测候选厂商、不在失败后把同一个 Key 换一家再试、
   不用大模型猜「这把 Key 属于谁」。识别厂商是本地前缀提示（providers.key_hint），
   验证与发现都只发生在用户明确选定的连接上。
2. **发现 ≠ 可用**：拿到模型列表只说明这个地址提供这些模型；是否真的能调用，
   必须由按模型的验证（verify_catalog_model）决定。发现过程**不会**对任何模型
   自动发起付费生成验证。
3. /models 是公共目录的服务商（例如 OpenRouter）如实标注：那个列表不是
   「这把 Key 全部可用的模型」，不能当权限证明。
4. 错误分类成可展示的 reason_code（断网 / 超时 / 限流 / 无权限 / 无效 Key / 无列表），
   文本先脱敏再落库；发现结果按连接缓存，并记住 checked_at 与错误，界面据此说
   「上次检查是什么时候」，而不是假装刚刚成功。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from agent.credentials.models import ModelCatalogStore
from agent.credentials.providers import (
    KIND_ANTHROPIC,
    find_preset,
    list_presets,
    preset_for_endpoint,
    resolve_endpoint,
    uses_anthropic,
)
from agent.credentials.store import compute_discovery_signature
from agent.credentials.usages import find_usage, normalize_usage_key

logger = logging.getLogger(__name__)

DISCOVERY_TIMEOUT_SECONDS = 8.0
DISCOVERY_PAGE_SIZE_DEFAULT = 50
DISCOVERY_PAGE_SIZE_MAX = 200
#: 发现结果缓存时长：这段时间内重复「获取模型」直接读库里上次的结果，不再发请求。
#: 用户点「刷新」时传 refresh=true 绕过缓存 —— 刷新是显式动作，不是后台轮询。
DISCOVERY_CACHE_SECONDS = 300.0

# 错误分类：界面按 reason_code 显示不同引导，不把「断网」说成「Key 无效」。
REASON_OFFLINE = "offline"
REASON_TIMEOUT = "timeout"
REASON_RATE_LIMIT = "rate_limit"
REASON_NO_PERMISSION = "no_permission"
REASON_INVALID_KEY = "invalid_key"
REASON_NO_LISTING = "no_listing"
REASON_ENDPOINT_MISSING = "endpoint_missing"
REASON_CREDENTIAL_UNAVAILABLE = "credential_unavailable"
REASON_SERVER_ERROR = "server_error"
REASON_CONFIG_CHANGED = "config_changed"
#: 同一个连接有更新的一次获取在跑（或已经完成）：这次较早的结果不能覆盖它。
REASON_SUPERSEDED = "superseded"
REASON_UNKNOWN = "unknown"

REASON_MESSAGES = {
    REASON_OFFLINE: "连不上这个服务地址（断网、代理或地址错误）",
    REASON_TIMEOUT: "服务响应超时，稍后可以重试",
    REASON_RATE_LIMIT: "请求太频繁被限流，稍后重试即可",
    REASON_NO_PERMISSION: "这把 Key 没有列出模型的权限，可以手动填写模型名称",
    REASON_INVALID_KEY: "API Key 无效或已被停用，请重新填写后再获取模型",
    REASON_NO_LISTING: "这个服务地址没有可用的模型列表接口，可以手动填写模型名称",
    REASON_ENDPOINT_MISSING: "这条连接没有保存服务地址，无法获取模型列表",
    REASON_CREDENTIAL_UNAVAILABLE: "这条连接当前不可用（已停用、已撤销或本机没有密钥）",
    REASON_SERVER_ERROR: "服务端出错，没能取回模型列表",
    REASON_CONFIG_CHANGED: "连接配置在获取过程中发生了变化，本次获取结果没有写入；请重新获取",
    REASON_SUPERSEDED: "这次获取已被更新的一次获取取代，结果没有写入",
    REASON_UNKNOWN: "获取模型列表时出现未知问题，可以展开技术细节",
}


class DiscoveryError(Exception):
    """一次发现失败（带可展示的分类 + 已脱敏的技术细节）。"""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(REASON_MESSAGES.get(reason_code, REASON_MESSAGES[REASON_UNKNOWN]))

    @property
    def message(self) -> str:
        return REASON_MESSAGES.get(self.reason_code, REASON_MESSAGES[REASON_UNKNOWN])


def build_store(ctx: Any) -> ModelCatalogStore:
    """按请求构造目录存储（AppContext 的连接与凭据存储是共享的）。"""
    return ModelCatalogStore(ctx.conn, ctx.credentials)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_age_seconds(checked_at: Any) -> float | None:
    text = str(checked_at or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())


def discovery_config_signature(meta: dict[str, Any] | None) -> str:
    """这次「获取模型」到底属于哪一份连接配置（F09：Key 版本 + 地址 + 协议）。

    与 credentials.store.compute_discovery_signature 同一份实现：目录、发现缓存与
    发现写入用同一个指纹，避免两处判断分叉。指纹只用于比较，不含密钥材料。
    """
    return compute_discovery_signature(
        version=(meta or {}).get("version"),
        endpoint=(meta or {}).get("endpoint"),
        kind=(meta or {}).get("kind"),
    )


def _redact(text: str, secret: str | None) -> str:
    from agent.services.verify import redact

    return redact(text, secret)


def _models_url(endpoint: str, models_path: str) -> str:
    base = (endpoint or "").strip().rstrip("/")
    if not base:
        raise DiscoveryError(REASON_ENDPOINT_MISSING)
    if base.lower().endswith("/models"):
        return base
    path = "/" + str(models_path or "/models").strip().lstrip("/")
    return base + path


def _auth_headers(secret: str, *, anthropic: bool) -> dict[str, str]:
    if anthropic:
        return {"x-api-key": secret, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {secret}"}


def _classify_status(status: int) -> str:
    if status == 401:
        return REASON_INVALID_KEY
    if status == 403:
        return REASON_NO_PERMISSION
    if status in (404, 405, 501):
        return REASON_NO_LISTING
    if status == 429:
        return REASON_RATE_LIMIT
    if status >= 500:
        return REASON_SERVER_ERROR
    return REASON_UNKNOWN


def _classify_exception(exc: BaseException) -> str:
    name = type(exc).__name__
    if isinstance(exc, httpx.TimeoutException) or "timeout" in name.lower():
        return REASON_TIMEOUT
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)) or name in {
        "ConnectError",
        "NetworkError",
        "APIConnectionError",
    }:
        return REASON_OFFLINE
    return REASON_UNKNOWN


def _capabilities_from_item(item: dict[str, Any]) -> list[str]:
    """只从服务商**明确声明**的字段取能力，不按模型名猜。

    猜错的代价是把图片发给看不见的模型、或把对话模型当嵌入模型 —— 宁可空着让界面
    如实显示「能力未知」。
    """
    caps: list[str] = []
    raw = item.get("capabilities")
    if isinstance(raw, list):
        caps.extend(str(cap) for cap in raw if str(cap).strip())
    architecture = item.get("architecture")
    if isinstance(architecture, dict):
        modalities = architecture.get("input_modalities")
        if isinstance(modalities, list):
            caps.extend(str(entry) for entry in modalities if str(entry).strip())
    elif isinstance(item.get("input_modalities"), list):
        caps.extend(str(entry) for entry in item["input_modalities"] if str(entry).strip())
    # 归一化：服务商说 image 输入 = 我们的 vision 能力。
    normalized: list[str] = []
    for cap in caps:
        lowered = cap.strip().lower()
        mapped = {"image": "vision", "images": "vision", "vision": "vision"}.get(lowered, lowered)
        if mapped and mapped not in normalized:
            normalized.append(mapped)
    return normalized


def _context_window(item: dict[str, Any]) -> int | None:
    for key in ("context_length", "context_window", "max_context_length"):
        value = item.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def normalize_model_item(item: Any) -> dict[str, Any] | None:
    """服务商返回的一项 → 统一模型记录；认不出模型名就跳过（不编造）。"""
    if isinstance(item, str):
        text = item.strip()
        return {"model_id": text} if text else None
    if not isinstance(item, dict):
        return None
    model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
    if not model_id:
        return None
    display = str(item.get("display_name") or item.get("display") or "").strip() or None
    if display is None:
        candidate = str(item.get("name") or "").strip()
        if candidate and candidate != model_id:
            display = candidate
    return {
        "model_id": model_id,
        "display_name": display,
        "capabilities": _capabilities_from_item(item),
        "context_window": _context_window(item),
    }


async def fetch_provider_models(
    *,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    timeout: float = DISCOVERY_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """向**一个**已保存的地址取模型列表；失败抛 DiscoveryError（分类明确）。

    测试替换的就是这个函数（不联网）。它只负责一次 GET，不做重试、不换地址。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers, params=params or None)
    except Exception as exc:  # noqa: BLE001 - 全部翻译成可展示的分类
        raise DiscoveryError(
            _classify_exception(exc), _redact(f"{type(exc).__name__}: {exc}", None)
        ) from exc
    secret = headers.get("Authorization", "").removeprefix("Bearer ").strip() or headers.get("x-api-key")
    if response.status_code != 200:
        raise DiscoveryError(
            _classify_status(response.status_code),
            _redact(f"HTTP {response.status_code}: {response.text[:300]}", secret),
        )
    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - 返回的不是 JSON：如实报「没有列表」
        raise DiscoveryError(REASON_NO_LISTING, _redact(f"not json: {exc}", secret)) from exc
    items = None
    if isinstance(data, dict):
        items = data.get("data") or data.get("models")
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        raise DiscoveryError(REASON_NO_LISTING, _redact(str(data)[:200], secret))
    return [
        normalized
        for normalized in (normalize_model_item(item) for item in items)
        if normalized
    ]


def _page_of(models: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    total = len(models)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or DISCOVERY_PAGE_SIZE_DEFAULT), DISCOVERY_PAGE_SIZE_MAX))
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "models": models[start:end],
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": end < total,
    }


def _page_result(
    credential_id: str,
    provider: Any,
    models: list[dict[str, Any]],
    *,
    source: str,
    page: int,
    page_size: int,
    error: str | None = None,
    reason_code: str | None = None,
    stale: bool = False,
    historical_models: list[dict[str, Any]] | None = None,
    config_signature: str = "",
) -> dict[str, Any]:
    result = _page_of(models, page, page_size)
    historical = historical_models or []
    result.update(
        {
            "credential_id": credential_id,
            "provider_id": provider.id if provider else None,
            "provider_name": provider.name if provider else None,
            "source": source,
            "error": error,
            "reason_code": reason_code,
            "stale": bool(stale),
            "config_signature": config_signature,
            "historical_models": historical,
            "historical_total": len(historical),
            "public_catalog": bool(provider and provider.public_catalog),
            "note": (provider.discovery_note if provider else "") or "",
        }
    )
    return result


def _project_model(item: dict[str, Any]) -> dict[str, Any]:
    """发现响应里的模型形状（与目录同一份事实）。"""
    return {
        "model_id": item["model_id"],
        "display_name": item["display_name"] or item["model_id"],
        "source": item["source"],
        "capabilities": item["capabilities"],
        "verify_state": item["verify_state"],
        "verified_at": item["verified_at"],
        "verify_error": item["verify_error"],
        "usable": item["usable"],
        "unavailable_reason": item["unavailable_reason"],
    }


def _catalog_models(store: ModelCatalogStore, credential_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """（当前目录模型, 旧配置历史模型）：当前返回 / 数量 / 缓存 / 重载后都读同一份划分。"""
    current, historical = store.current_model_view(credential_id)
    return [_project_model(item) for item in current], [_project_model(item) for item in historical]


#: 进程内的发现请求代号（按连接）：同配置下较早的请求晚回来时，不能覆盖较新的结果。
#: 单进程后端 + 单事件循环，字典读写不需要锁；只用于「谁是最新一次获取」。
_DISCOVERY_TOKENS: dict[str, int] = {}


def _begin_discovery(credential_id: str) -> int:
    token = int(_DISCOVERY_TOKENS.get(credential_id) or 0) + 1
    _DISCOVERY_TOKENS[credential_id] = token
    return token


def _discovery_is_latest(credential_id: str, token: int) -> bool:
    return int(_DISCOVERY_TOKENS.get(credential_id) or 0) == token


def _stale_result(
    credential_id: str,
    provider: Any,
    *,
    reason_code: str,
    page: int,
    page_size: int,
    signature: str,
    store: ModelCatalogStore,
) -> dict[str, Any]:
    current, historical = _catalog_models(store, credential_id)
    return _page_result(
        credential_id,
        provider,
        current,
        source="provider",
        page=page,
        page_size=page_size,
        error=REASON_MESSAGES.get(reason_code, REASON_MESSAGES[REASON_UNKNOWN]),
        reason_code=reason_code,
        stale=True,
        historical_models=historical,
        config_signature=signature,
    )


def _stale_reason(ctx: Any, credential_id: str, token: int, signature: str) -> str:
    """写库 / 写状态前的统一检查：先看请求顺序，再看配置归属（返修 5）。"""
    if not _discovery_is_latest(credential_id, token):
        return REASON_SUPERSEDED
    current_meta = ctx.credentials.get_metadata(credential_id)
    if current_meta is None or discovery_config_signature(current_meta) != signature:
        return REASON_CONFIG_CHANGED
    return ""


async def discover(
    ctx: Any,
    credential_id: str,
    *,
    page: int = 1,
    page_size: int = DISCOVERY_PAGE_SIZE_DEFAULT,
    refresh: bool = False,
) -> dict[str, Any]:
    """获取一条**已保存**连接的模型列表。

    调用顺序即安全边界：先确认连接存在、有可分页地址、本机有密钥；否则直接返回
    错误，不发任何请求。缓存命中时也不发请求。
    """
    store = build_store(ctx)
    meta = ctx.credentials.get_metadata(credential_id)
    if meta is None:
        raise KeyError(f"credential not found: {credential_id}")
    provider = preset_for_endpoint(meta.get("endpoint")) or find_preset(None)
    state = store.discovery_state(credential_id)
    # 这次获取归属哪一份配置：写库/缓存/状态都要按它校验（F09）。
    signature = discovery_config_signature(meta)
    # 请求顺序：同一连接后发起的获取才算最新；较早的请求晚回来不能覆盖较新的结果。
    token = _begin_discovery(credential_id)
    cached_models, cached_historical = _catalog_models(store, credential_id)

    if not refresh:
        age = _cache_age_seconds(state.get("checked_at"))
        fresh = age is not None and age <= DISCOVERY_CACHE_SECONDS
        # 缓存必须属于**当前**这份配置：换了地址 / 协议 / Key 之后，上一份配置的
        # 「上次结果」不能冒充这次的结果（否则界面拿旧列表当新事实）。
        cache_owned = str(state.get("config_signature") or "") == signature
        # 缓存既包括「上次成功拿到的模型」，也包括「上次失败的错误」：两者都在
        # 检查过的窗口内，不能让界面每点一次都重新打一遍网络（刷新是显式动作）。
        if fresh and cache_owned and (cached_models or state.get("error")):
            if state.get("error"):
                return _page_result(
                    credential_id,
                    provider,
                    cached_models,
                    source="cache",
                    page=page,
                    page_size=page_size,
                    error=str(state.get("error")),
                    reason_code=state.get("reason_code") or REASON_UNKNOWN,
                    historical_models=cached_historical,
                    config_signature=signature,
                )
            return _page_result(
                credential_id,
                provider,
                cached_models,
                source="cache",
                page=page,
                page_size=page_size,
                historical_models=cached_historical,
                config_signature=signature,
            )

    endpoint = resolve_endpoint(meta)
    if not endpoint:
        error = REASON_MESSAGES[REASON_ENDPOINT_MISSING]
        store.record_discovery(
            credential_id,
            source="provider",
            error=error,
            reason_code=REASON_ENDPOINT_MISSING,
            provider_id=provider.id if provider else None,
            config_signature=signature,
        )
        return _page_result(
            credential_id,
            provider,
            cached_models,
            source="provider",
            page=page,
            page_size=page_size,
            error=error,
            reason_code=REASON_ENDPOINT_MISSING,
            historical_models=cached_historical,
            config_signature=signature,
        )

    secret = ctx.credentials.get_secret(credential_id)
    if not secret:
        error = REASON_MESSAGES[REASON_CREDENTIAL_UNAVAILABLE]
        store.record_discovery(
            credential_id,
            source="provider",
            error=error,
            reason_code=REASON_CREDENTIAL_UNAVAILABLE,
            provider_id=provider.id if provider else None,
            config_signature=signature,
        )
        return _page_result(
            credential_id,
            provider,
            cached_models,
            source="provider",
            page=page,
            page_size=page_size,
            error=error,
            reason_code=REASON_CREDENTIAL_UNAVAILABLE,
            historical_models=cached_historical,
            config_signature=signature,
        )

    anthropic = uses_anthropic(meta, endpoint) or str(meta.get("kind") or "") == KIND_ANTHROPIC
    headers = _auth_headers(secret, anthropic=anthropic)
    params = None
    if provider is not None and provider.supports_pagination:
        params = {"page": max(1, int(page or 1)), "page_size": int(page_size)}
    try:
        url = _models_url(endpoint, provider.models_path if provider else "/models")
        fetched = await fetch_provider_models(
            url=url, headers=headers, params=params, timeout=DISCOVERY_TIMEOUT_SECONDS
        )
    except DiscoveryError as exc:
        logger.warning(
            "模型发现失败（连接 %s，分类 %s）：%s", credential_id, exc.reason_code, exc.detail
        )
        # 失败结果同样要核对顺序与配置归属：旧配置的超时 / 旧请求的失败不能覆盖
        # 新配置那次成功的获取状态（返修 5）。
        stale_reason = _stale_reason(ctx, credential_id, token, signature)
        if stale_reason:
            return _stale_result(
                credential_id,
                provider,
                reason_code=stale_reason,
                page=page,
                page_size=page_size,
                signature=signature,
                store=store,
            )
        store.record_discovery(
            credential_id,
            source="provider",
            error=exc.message,
            reason_code=exc.reason_code,
            provider_id=provider.id if provider else None,
            config_signature=signature,
        )
        return _page_result(
            credential_id,
            provider,
            cached_models,
            source="provider",
            page=page,
            page_size=page_size,
            error=exc.message,
            reason_code=exc.reason_code,
            historical_models=cached_historical,
            config_signature=signature,
        )

    # 成功 / 空结果 / 失败在写库前统一核对：① 是不是这条连接最新的获取请求；
    # ② 结果是否仍属于当前配置（配置在等待期间可能被改）。任一不符都不写库、
    # 不改获取状态 —— 旧配置超时不能覆盖新配置的成功状态（返修 5）。
    stale_reason = _stale_reason(ctx, credential_id, token, signature)
    if stale_reason:
        logger.info("模型发现结果被丢弃（连接 %s，%s）", credential_id, stale_reason)
        return _stale_result(
            credential_id,
            provider,
            reason_code=stale_reason,
            page=page,
            page_size=page_size,
            signature=signature,
            store=store,
        )

    if not fetched:
        # 200 但没有可识别模型：如实报「没有列表」，不伪造「0 个可用模型」。
        error = REASON_MESSAGES[REASON_NO_LISTING]
        store.record_discovery(
            credential_id,
            source="provider",
            error=error,
            reason_code=REASON_NO_LISTING,
            provider_id=provider.id if provider else None,
            config_signature=signature,
        )
        return _page_result(
            credential_id,
            provider,
            cached_models,
            source="provider",
            page=page,
            page_size=page_size,
            error=error,
            reason_code=REASON_NO_LISTING,
            historical_models=cached_historical,
            config_signature=signature,
        )

    store.upsert_discovered(credential_id, fetched)
    # 当前目录只记「这次真实返回 / 当前配置下已经返回过」的模型：
    # 旧地址留下的行继续留在库里（历史 / 手动 / 用途引用），但不计入本次结果。
    previous = store.current_set(credential_id)
    previous_owned = str(previous.get("config_signature") or "") == signature
    previous_ids = previous["model_ids"] if previous_owned else []
    discovered_ids = [
        str(item.get("model_id") or "") for item in fetched if item.get("model_id")
    ]
    store.record_current_set(
        credential_id,
        config_signature=signature,
        model_ids=list(dict.fromkeys([*previous_ids, *discovered_ids])),
    )
    store.record_discovery(
        credential_id,
        source="provider",
        error=None,
        provider_id=provider.id if provider else None,
        model_count=len(discovered_ids),
        config_signature=signature,
        # 只有成功取回才更新「模型来源」；之后的失败尝试不能把来源改成新配置。
        source_signature=signature,
    )
    current, historical = _catalog_models(store, credential_id)
    return _page_result(
        credential_id,
        provider,
        current,
        source="provider",
        page=page,
        page_size=page_size,
        historical_models=historical,
        config_signature=signature,
    )


def public_catalog_info() -> dict[str, Any]:
    """公共模型目录（只描述事实，不发任何请求）。

    这里刻意不在 GET 里联网：GET 是「读当前已知事实」，联网必须来自用户的显式动作。
    """
    entries = [
        {
            "provider_id": preset.id,
            "provider_name": preset.name,
            "models_path": preset.models_path,
            "auth_path": preset.auth_path,
            "public_catalog": preset.public_catalog,
            "discovery_note": preset.discovery_note,
            "note": preset.note,
        }
        for preset in list_presets()
        if preset.public_catalog
    ]
    return {
        "providers": entries,
        "note": "公共模型目录只说明服务商支持哪些模型，不代表这把 Key 有权限使用其中任何一个；"
        "核对 Key 请用模型验证功能。",
    }


async def verify_catalog_model(ctx: Any, credential_id: str, model_id: str) -> dict[str, Any]:
    """对**一个模型**做一次真实调用验证，并把结论按指纹记在模型行上。

    关键语义（与 API 契约 §6.3 一致）：
    * 结论只覆盖 (credential_id, model_id, 当前配置指纹)；
    * 配置变了（换 Key / 改地址 / 改协议）→ 旧结论 applies_to_current_config=false；
    * 迟到结果（快照指纹 != 当前指纹）只留痕，不改当前状态；
    * 单个模型失败**不**改变 Key 级 verify_state，也不影响同 Key 的其它模型。
    """
    from agent.services import verify as verify_service

    store = build_store(ctx)
    meta = ctx.credentials.get_metadata(credential_id)
    if meta is None:
        raise KeyError(f"credential not found: {credential_id}")
    model = store.get_model(credential_id, model_id)
    if model is None:
        # 手动模型名：用户明确指定要验证它，先记录成 manual（不是我们猜的）。
        model = store.upsert_model(credential_id, model_id, source="manual")
    secret = ctx.credentials.get_secret(credential_id)
    if not secret:
        return {
            "ok": False,
            "state": str(model.get("verify_state") or "unverified"),
            "reason_code": REASON_CREDENTIAL_UNAVAILABLE,
            "message": REASON_MESSAGES[REASON_CREDENTIAL_UNAVAILABLE],
            "detail": "",
            "applies_to_current_config": False,
            "verified_at": model.get("verified_at"),
            "model_id": model_id,
            "credential_id": credential_id,
            "result_state": "current",
        }
    fingerprint = store.model_fingerprint(credential_id, model_id)
    result = await verify_service.verify_model(
        secret=secret,
        endpoint=meta.get("endpoint"),
        model=model_id,
        kind=meta.get("kind"),
    )
    transient = result.reason_code in verify_service.TRANSIENT_REASONS
    outcome = store.record_verification(
        credential_id,
        model_id,
        ok=result.ok,
        reason_code=result.reason_code,
        fingerprint=fingerprint,
        transient=transient,
    )
    updated = store.get_model(credential_id, model_id) or {}
    payload = result.to_dict()
    payload.update(
        {
            "credential_id": credential_id,
            "model_id": model_id,
            "applies_to_current_config": bool(outcome.get("applied")),
            "result_state": str(outcome.get("result_state") or "current"),
            "verified_at": updated.get("verified_at"),
            "verify_state": updated.get("verify_state"),
        }
    )
    payload["state"] = str(updated.get("verify_state") or payload.get("state") or "unverified")
    if outcome.get("state_kept"):
        payload["ok"] = False
        payload["message"] = f"{result.message}（这个模型原来的验证状态保持不变）"
    elif not outcome.get("applied"):
        payload["ok"] = False
        payload["message"] = (
            f"{result.message}（这是改动前配置的历史结果，当前配置的状态没有改变）"
        )
    return payload


def resolve_usage(ctx: Any, usage_key: str) -> dict[str, Any]:
    """调试 / 界面用：一个用途现在实际会打到哪条连接的哪个模型。

    顺序与运行时一致：显式分配 → 默认模型 → 基线 tags 兼容回退；自定义标签没有
    执行路径，如实返回 supported=false。
    """
    store = build_store(ctx)
    key = normalize_usage_key(usage_key)
    spec = find_usage(key)
    target = store.resolve_usage(key) if spec is not None else None
    if target is None and spec is not None:
        target = store.legacy_fallback(key)
    if target is not None and target.get("source"):
        source = str(target["source"])
    else:
        source = None
    return {
        "usage_key": key,
        "label": spec.label if spec is not None else key,
        "supported": spec is not None,
        "resolved": target,
        "source": source,
        "note": (
            (spec.note if spec is not None else "")
            if target is not None
            else "当前没有可用目标；运行时不会替你猜一个模型"
        ),
    }