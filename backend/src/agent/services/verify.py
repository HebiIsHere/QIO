"""对「用户明确选定的那一个服务地址」做一次真实调用验证。

产品约束（必须由后端保证，不能只靠前端不调用别的接口）：

1. **只请求调用方给出的 base_url**：不探测候选厂商、不在失败后把同一个 Key
   发给别家。识别 Key 属于哪家厂商是用户的选择，不是我们的猜测；
2. **协议与正式对话一致**：OpenAI 兼容类走 `probe_adapter`，Anthropic 类走
   `probe_anthropic`。以前「测试连接」一律用 OpenAI 客户端，而正式运行对
   Anthropic 端点走 `AnthropicAdapter` —— 同一把 Key 两边结论不一样；
3. **拿到模型列表 ≠ 模型可用**：模型列表只是给界面排序用的提示，验证必须真的
   发一次最小的对话请求；
4. 返回给界面/日志的文本里不出现密钥原文（异常文本也要先脱敏）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

VERIFY_TIMEOUT_SECONDS = 20.0
LIST_MODELS_TIMEOUT_SECONDS = 8.0

REASON_INVALID_KEY = "invalid_key"
REASON_MODEL_NOT_ALLOWED = "model_not_allowed"
REASON_MODEL_NOT_FOUND = "model_not_found"
REASON_MODEL_REJECTED = "model_rejected"
REASON_UNREACHABLE = "unreachable"
REASON_TIMEOUT = "timeout"
REASON_QUOTA = "quota"
REASON_RATE_LIMIT = "rate_limit"
REASON_ENDPOINT_MISSING = "endpoint_missing"
REASON_MODEL_MISSING = "model_missing"
REASON_UNKNOWN = "unknown"

# 普通用户能读懂的中文。技术细节放在 detail 里，默认收起。
REASON_MESSAGES = {
    REASON_INVALID_KEY: "API Key 无效或已被停用，请确认后重新填写",
    REASON_MODEL_NOT_ALLOWED: "这个 API Key 没有该模型的权限，请换一个模型或在厂商后台开通",
    REASON_MODEL_NOT_FOUND: "服务地址里没有这个模型，请检查模型名称",
    REASON_MODEL_REJECTED: "服务拒绝了这次测试请求，请检查模型名称是否正确",
    REASON_UNREACHABLE: "连不上这个服务地址，请检查地址、网络或代理设置",
    REASON_TIMEOUT: "服务响应超时，稍后可以重试验证",
    REASON_QUOTA: "账户额度不足或已欠费，请到厂商后台充值或换一把 Key",
    REASON_RATE_LIMIT: "请求太频繁被限流，稍后重试验证即可",
    REASON_ENDPOINT_MISSING: "还没有填写服务地址",
    REASON_MODEL_MISSING: "还没有选择模型",
    REASON_UNKNOWN: "验证时出现未知问题，可以展开技术细节或稍后重试",
}

# 「这次没验出来」但不足以否定这把钥匙的原因：网络、超时、限流、未知。
# 它们不能把一把本来可用的凭据降级成不可用（见 api/server.py::_run_verification）。
TRANSIENT_REASONS = frozenset(
    {REASON_UNREACHABLE, REASON_TIMEOUT, REASON_RATE_LIMIT, REASON_UNKNOWN}
)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason_code: str | None = None
    detail: str = ""
    mode: str | None = None

    @property
    def message(self) -> str:
        if self.ok:
            if self.mode == "text":
                return "模型可用（不支持原生工具调用，已使用兼容模式）"
            return "模型可用"
        return REASON_MESSAGES.get(self.reason_code or REASON_UNKNOWN, REASON_MESSAGES[REASON_UNKNOWN])

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": "verified" if self.ok else "failed",
            "reason_code": self.reason_code,
            "message": self.message,
            "detail": self.detail,
            "mode": self.mode,
        }


def redact(text: str, secret: str | None = None) -> str:
    """错误文本里可能带出请求内容/Key，落库与回显前统一打码。"""
    out = str(text or "")
    if secret:
        out = out.replace(secret, "••••")
    # Bearer / x-api-key 形式
    import re

    out = re.sub(r"(?i)(authorization|x-api-key)\"?\s*[:=]\s*\"?[^\"',\s}]+", r"\1: ••••", out)
    out = re.sub(r"(?i)\b(sk|gsk|xai|AIza)[-_A-Za-z0-9]{8,}", "••••", out)
    return out[:400]


def _classify(exc: BaseException) -> tuple[str, str]:
    """异常 → (reason_code, detail)。只按类名/文本分类，不猜厂商。"""
    name = type(exc).__name__
    text = str(exc)
    lowered = text.lower()
    if name in {"AuthenticationError", "PermissionDeniedError", "UnauthorizedError"}:
        if "model" in lowered and any(
            token in lowered for token in ("permission", "not allowed", "does not have access")
        ):
            return REASON_MODEL_NOT_ALLOWED, text
        return REASON_INVALID_KEY, text
    if name in {"NotFoundError"} or "model_not_found" in lowered or "does not exist" in lowered:
        return REASON_MODEL_NOT_FOUND, text
    if name in {"APITimeoutError", "ConnectTimeout", "ReadTimeout", "TimeoutException"} or "timeout" in lowered:
        return REASON_TIMEOUT, text
    if name in {"APIConnectionError", "ConnectError", "NetworkError", "ProviderInternalError"} and "internal" not in name.lower():
        return REASON_UNREACHABLE, text
    if name in {"RateLimitError", "TooManyRequestsError", "RateLimitExceeded"}:
        if "quota" in lowered or "insufficient" in lowered or "billing" in lowered or "欠费" in text:
            return REASON_QUOTA, text
        return REASON_RATE_LIMIT, text
    if name in {"BadRequestError", "UnprocessableEntityError", "InvalidRequestError", "InvalidToolCall"}:
        if "quota" in lowered or "insufficient_quota" in lowered:
            return REASON_QUOTA, text
        if "model" in lowered and any(
            token in lowered for token in ("not found", "not exist", "unknown", "invalid")
        ):
            return REASON_MODEL_NOT_FOUND, text
        return REASON_MODEL_REJECTED, text
    if "quota" in lowered or "insufficient_quota" in lowered:
        return REASON_QUOTA, text
    return REASON_UNKNOWN, f"{name}: {text}"


def _uses_anthropic(kind: str | None, endpoint: str | None) -> bool:
    from agent.adapters.anthropic import is_anthropic_endpoint

    if kind:
        return kind == "anthropic"
    return is_anthropic_endpoint(endpoint)


async def verify_model(
    *,
    secret: str,
    endpoint: str | None,
    model: str | None,
    kind: str | None = None,
    timeout: float = VERIFY_TIMEOUT_SECONDS,
) -> VerifyResult:
    """真的发一次最小对话请求；成功即「模型可用」。"""
    base_url = (endpoint or "").strip()
    model_name = (model or "").strip()
    if not base_url:
        return VerifyResult(False, REASON_ENDPOINT_MISSING)
    if not model_name:
        return VerifyResult(False, REASON_MODEL_MISSING)
    if not secret or not secret.strip():
        return VerifyResult(False, REASON_INVALID_KEY, "empty secret")

    if _uses_anthropic(kind, base_url):
        return await _verify_anthropic(secret, base_url, model_name, timeout)
    return await _verify_openai(secret, base_url, model_name, timeout)


async def _verify_openai(secret: str, base_url: str, model: str, timeout: float) -> VerifyResult:
    from agent.adapters.base import AdapterMode
    from agent.adapters.probe import probe_adapter

    client = None
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=secret, base_url=base_url, timeout=timeout, max_retries=0)
        # cache=None：验证结果绝不能复用另一把 Key 探出来的档位缓存。
        probe = await probe_adapter(client, model, endpoint=base_url, cache=None)
    except Exception as exc:  # noqa: BLE001 - 统一翻译成用户能读的分类
        code, detail = _classify(exc)
        return VerifyResult(False, code, redact(detail, secret))
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响验证结论
                logger.debug("failed to close verify client", exc_info=True)
    mode = probe.mode.value if isinstance(probe.mode, AdapterMode) else str(probe.mode)
    if mode == AdapterMode.UNSUPPORTED.value:
        return VerifyResult(False, REASON_MODEL_REJECTED, redact(probe.detail, secret))
    return VerifyResult(True, mode=mode, detail=redact(probe.detail, secret))


async def _verify_anthropic(secret: str, base_url: str, model: str, timeout: float) -> VerifyResult:
    from agent.adapters.anthropic import probe_anthropic

    try:
        result = await probe_anthropic(secret, model, base_url)
    except Exception as exc:  # noqa: BLE001
        code, detail = _classify(exc)
        return VerifyResult(False, code, redact(detail, secret))
    return VerifyResult(True, mode=str(result or "native"), detail="anthropic tool call accepted")


async def list_models(
    *,
    secret: str,
    endpoint: str | None,
    kind: str | None = None,
    timeout: float = LIST_MODELS_TIMEOUT_SECONDS,
) -> list[str]:
    """给界面用的模型候选列表；拿不到就返回空。

    这**不是**验证：`GET /models` 在很多服务上是公开目录，能返回列表不代表
    这把 Key 有权限调用其中任何一个模型。
    """
    base_url = (endpoint or "").strip().rstrip("/")
    if not base_url or not secret:
        return []
    anthropic = _uses_anthropic(kind, base_url)
    headers = (
        {"x-api-key": secret, "anthropic-version": "2023-06-01"}
        if anthropic
        else {"Authorization": f"Bearer {secret}"}
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:  # noqa: BLE001 - 列表拿不到只是少一个便利，不是错误
        return []
    items = data.get("data") or data.get("models") or []
    models = [
        str(item.get("id") or item.get("name"))
        for item in items
        if isinstance(item, dict) and (item.get("id") or item.get("name"))
    ]
    return models[:200]
