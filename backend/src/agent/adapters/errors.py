"""Internal model-error taxonomy.

Adapters translate provider-specific exceptions into these classes so the
core (AgentLoop, orchestrator) never branches on an SDK's exception type.
Matching is done by class name / module so we do not import any provider SDK
just to classify errors.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for normalized provider errors."""

    code = "provider_error"


class AuthenticationError(ProviderError):
    code = "authentication"


class RateLimitError(ProviderError):
    code = "rate_limit"


class NetworkError(ProviderError):
    code = "network"


class UnsupportedCapability(ProviderError):
    code = "unsupported_capability"


class InvalidToolCall(ProviderError):
    code = "invalid_tool_call"


class ProviderInternalError(ProviderError):
    code = "provider_internal"


_AUTH = {"AuthenticationError", "PermissionDeniedError", "UnauthorizedError"}
_RATE = {"RateLimitError", "TooManyRequestsError", "RateLimitExceeded"}
_NETWORK = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "TimeoutException",
    "NetworkError",
}
_BAD_REQUEST = {"BadRequestError", "UnprocessableEntityError", "InvalidRequestError"}


def normalize_error(exc: Exception) -> ProviderError:
    """Map a provider exception to the internal taxonomy."""
    name = type(exc).__name__
    if isinstance(exc, ProviderError):
        return exc
    if name in _AUTH:
        return AuthenticationError(str(exc)[:300])
    if name in _RATE:
        return RateLimitError(str(exc)[:300])
    if name in _NETWORK:
        return NetworkError(str(exc)[:300])
    if name in _BAD_REQUEST:
        return InvalidToolCall(str(exc)[:300])
    # 模块级兜底（httpx / openai / anthropic 连接类错误的共同点）
    module = type(exc).__module__ or ""
    if "httpx" in module and any(k in name for k in ("Connect", "Timeout")):
        return NetworkError(str(exc)[:300])
    return ProviderInternalError(f"{name}: {exc}"[:300])
