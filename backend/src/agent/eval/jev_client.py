"""Jev 客户端：走 OpenRouter 的 Decisions 接口。

Jev 是 TypeSafe 的 System One 判定模型，不做召回也不生成文本：发一个 `state`
加一组类型化问题（choice / score / noul），拿回带概率的类型化答案。OpenRouter
上的模型 id 是 `typesafe/jev-1.13`（别名 `~typesafe/jev-latest`），计费记在
OpenRouter 账户上，只收输入 token。

安全约定：密钥只从参数或环境变量读（Windows 上再回落到注册表 User 作用域），
任何异常消息、日志、结果文件里都不得出现密钥原文。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

DEFAULT_MODEL = "typesafe/jev-1.13"
DEFAULT_URL = "https://openrouter.ai/api/alpha/decisions"
ENV_VAR = "OPENROUTER_API_KEY"


class JevUnavailable(RuntimeError):
    """Jev 调用不可用（缺 key、网络失败、HTTP 错误、限流）。"""


def mask(secret: str | None) -> str:
    """把密钥变成可安全打印的形式。"""
    if not secret:
        return "(empty)"
    if len(secret) <= 12:
        return "***"
    return f"{secret[:8]}…{secret[-4:]}"


def _registry_key() -> str:
    """Windows 注册表 User 作用域里的 OPENROUTER_API_KEY（非 Windows 返回空）。"""
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, ENV_VAR)
            return str(value or "")
    except Exception:
        return ""


def resolve_api_key(explicit: str | None = None) -> str:
    """密钥来源顺序：显式参数 → 进程环境变量 → 注册表 User 作用域。"""
    return (explicit or os.environ.get(ENV_VAR) or _registry_key() or "").strip()


@dataclass
class JevAnswer:
    answers: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    model: str = ""

    def noul(self, question_id: str, default: float = 0.5) -> float:
        got = self.answers.get(question_id) or {}
        value = got.get("noul")
        return float(value) if isinstance(value, (int, float)) else default

    def choice(self, question_id: str) -> str | None:
        got = self.answers.get(question_id) or {}
        value = got.get("choice")
        return str(value) if value is not None else None

    def score(self, question_id: str) -> float | None:
        got = self.answers.get(question_id) or {}
        value = got.get("score")
        return float(value) if isinstance(value, (int, float)) else None


class JevClient:
    """薄客户端：一次请求可带多个问题，同时累计用量与成本。"""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        url: str = DEFAULT_URL,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
        max_retries: int = 2,
    ) -> None:
        self.api_key = resolve_api_key(api_key)
        self.model = model
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = http_client
        self.requests = 0
        self.failures = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.latencies_ms: list[float] = []

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def ask(self, state: Any, questions: dict[str, Any], *, model: str | None = None) -> JevAnswer:
        if not self.configured:
            raise JevUnavailable(
                f"缺少 {ENV_VAR}：请设置环境变量或显式传入 api_key（当前为空）"
            )
        payload = {"model": model or self.model, "state": state, "questions": questions}
        last_error = ""
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                response = self._http().post(
                    self.url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {mask(str(exc))}"
                if attempt >= self.max_retries:
                    self.failures += 1
                    raise JevUnavailable(f"Jev 请求失败（已重试 {attempt} 次）：{last_error}") from None
                time.sleep(0.5 * (attempt + 1))
                continue
            latency_ms = (time.perf_counter() - started) * 1000
            if response.status_code >= 400:
                detail = _safe_detail(response)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    last_error = f"HTTP {response.status_code} {detail}"
                    time.sleep(1.0 * (attempt + 1))
                    continue
                self.failures += 1
                raise JevUnavailable(f"Jev 返回 HTTP {response.status_code}：{detail}")
            data = response.json()
            usage = data.get("usage") or {}
            answer = JevAnswer(
                answers=data.get("answers") or {},
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cost_usd=float(usage.get("cost") or 0.0),
                latency_ms=round(latency_ms, 2),
                model=str(data.get("model") or payload["model"]),
            )
            self.requests += 1
            self.total_input_tokens += answer.input_tokens
            self.total_output_tokens += answer.output_tokens
            self.total_cost_usd += answer.cost_usd
            self.latencies_ms.append(answer.latency_ms)
            return answer
        raise JevUnavailable(f"Jev 请求失败：{last_error}")

    def summary(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "failures": self.failures,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "cost_usd": round(self.total_cost_usd, 6),
            "key": mask(self.api_key),
        }


def _safe_detail(response: httpx.Response) -> str:
    """错误详情可能回显密钥原文：形如 sk-or… 的片段一律整串替换掉。

    这里比 `mask()` 更严格 —— `mask()` 是给"当前用的是哪把 key"这种主动展示用的，
    而错误正文来自外部，不能保证片段长度，索性不给任何残留。
    """
    try:
        text = response.text
    except Exception:
        text = ""
    out = []
    for piece in text[:300].split():
        stripped = piece.strip('",')
        out.append("[redacted-key]" if stripped.startswith("sk-or") else piece)
    return " ".join(out)
