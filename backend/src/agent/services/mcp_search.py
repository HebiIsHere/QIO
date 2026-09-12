"""免密钥 MCP 搜索通道（Exa / Parallel 的公共免费层）。

为什么是这条路线（2026-09-12 实测，见 docs/status.md）：
必应对匿名请求会返回「标题匹配、内容无关」的诱饵页，百度常年安全验证，公共
SearXNG 实例全部被 Anubis/Substation 挡在门外，国内免密钥引擎要么 JS 墙要么
解析不出来。这两个 MCP 服务**无需 API Key** 就能返回结构化结果（Exa 返回
标题/URL/正文摘要；Parallel 返回 JSON 结果集），本机实测可直接用。

实现取舍：
* 不引 MCP SDK，只做一次 `tools/call`（实测两个服务都接受裸 tools/call，
  无需先 initialize）；解析两种帧：Exa 是 SSE（`event: message` + `data: {...}`），
  Parallel 是纯 JSON。
* 隐私：Parallel 只发进程内随机的 `session_id`（官方用它做免费层限流），
  刻意不发 `model_name` 这类分析字段；不做任何持久化。
* 失败语义与其它 provider 对齐：限流 → skipped（触发冷却，不再反复撞），
  其它 → error；错误信息统一中文。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

import httpx

from agent.services.search import SearchHit, SearchOutcome

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"

DEFAULT_TIMEOUT_SECONDS = 30.0

_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "quota",
    "slow down",
    "429",
    "exceeded",
)

# Parallel 免费层的限流关联 id：进程内随机一次，不持久化、不跨进程复用
_SESSION_ID = uuid.uuid4().hex


def _is_rate_limitish(message: str) -> bool:
    low = (message or "").lower()
    return any(marker in low for marker in _RATE_LIMIT_MARKERS)


def _parse_sse_or_json(text: str) -> dict | None:
    """解析 MCP 响应：SSE（`data: {...}`）或纯 JSON；都不是则 None。"""
    body = (text or "").strip()
    if not body:
        return None
    if body.startswith("{"):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                continue
    return None


@dataclass
class McpCallOutcome:
    kind: str  # ok | rate_limited | error
    result: dict | None = None
    message: str | None = None


class McpHttpClient:
    """最小 JSON-RPC MCP 客户端（只做 tools/call）。"""

    def __init__(self, url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.url = url
        self.timeout = timeout

    async def _post(self, body: dict) -> tuple[int, str]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                self.url,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            return resp.status_code, resp.text

    async def call_tool(self, tool: str, arguments: dict) -> McpCallOutcome:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        try:
            status, text = await self._post(body)
        except Exception as exc:  # noqa: BLE001 - 网络异常按通道失败处理
            return McpCallOutcome("error", message=f"无法访问（{type(exc).__name__}: {exc}）")
        if status == 429:
            return McpCallOutcome("rate_limited", message="免费层限流（HTTP 429），已跳过")
        if status != 200:
            return McpCallOutcome("error", message=f"返回 HTTP {status}")
        data = _parse_sse_or_json(text)
        if data is None:
            return McpCallOutcome("error", message="响应不是合法 JSON")
        if err := data.get("error"):
            message = str((err or {}).get("message") or err)
            kind = "rate_limited" if _is_rate_limitish(message) else "error"
            return McpCallOutcome(kind, message=message[:200])
        result = data.get("result") or {}
        if result.get("isError"):
            texts = [
                str(c.get("text") or "")
                for c in (result.get("content") or [])
                if isinstance(c, dict)
            ]
            message = " ".join(t for t in texts if t).strip() or "工具返回错误"
            kind = "rate_limited" if _is_rate_limitish(message) else "error"
            return McpCallOutcome(kind, message=message[:200])
        return McpCallOutcome("ok", result=result)


def _mcp_text(result: dict) -> str:
    """取 MCP tools/call 结果里的第一段文本内容。"""
    for chunk in result.get("content") or []:
        if isinstance(chunk, dict) and chunk.get("type") == "text":
            return str(chunk.get("text") or "")
    return ""


class _McpSearchProvider:
    """共用骨架：一次 tools/call → 解析成 hits / 中文错误。"""

    name = "mcp"
    label = "MCP 搜索"
    url = ""
    tool = ""

    def __init__(self, client: McpHttpClient | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.client = client or McpHttpClient(self.url, timeout=timeout)

    def arguments(self, query: str, top_k: int) -> dict:
        raise NotImplementedError

    def parse(self, text: str, top_k: int) -> list[SearchHit]:
        raise NotImplementedError

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        outcome = await self.client.call_tool(self.tool, self.arguments(query, top_k))
        if outcome.kind == "rate_limited":
            # 界面文案统一中文：限流只说结果与含义，不回显服务端英文原文
            return SearchOutcome(
                status="skipped", error=f"{self.label}免费层限流，已跳过（稍后自动重试）"
            )
        if outcome.kind == "error":
            return SearchOutcome(status="error", error=f"{self.label}暂不可用：{outcome.message}")
        hits = self.parse(_mcp_text(outcome.result or {}), top_k)
        return SearchOutcome(hits=hits, status="found" if hits else "empty")


_EXA_BLOCK_RE = re.compile(
    r"Title:\s*(?P<title>.*?)\s*\nURL:\s*(?P<url>\S+)(?P<rest>.*?)(?=\n---|\Z)",
    re.S,
)


class ExaMcpProvider(_McpSearchProvider):
    """Exa 免费 MCP（`web_search_exa`）。

    入参要求 query + objective（两者必填）；返回是「Title/URL/Highlights」文本块。
    """

    name = "exa"
    label = "Exa 搜索"
    url = EXA_MCP_URL
    tool = "web_search_exa"

    def arguments(self, query: str, top_k: int) -> dict:
        return {
            "query": query,
            "objective": f"回答与「{query}」相关的当前问题，优先最相关、最可信的来源",
            "numResults": max(1, min(top_k, 10)),
        }

    def parse(self, text: str, top_k: int) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for match in _EXA_BLOCK_RE.finditer(text or ""):
            url = match.group("url").strip()
            if not url.startswith("http"):
                continue
            rest = match.group("rest") or ""
            snippet = rest.split("Highlights:", 1)[-1] if "Highlights:" in rest else rest
            hits.append(
                SearchHit(
                    title=match.group("title").strip(),
                    url=url,
                    snippet=" ".join(snippet.split())[:600],
                )
            )
            if len(hits) >= top_k:
                break
        return hits


class ParallelMcpProvider(_McpSearchProvider):
    """Parallel 免费 MCP（`web_search`）：objective + search_queries，返回 JSON 结果集。"""

    name = "parallel"
    label = "Parallel 搜索"
    url = PARALLEL_MCP_URL
    tool = "web_search"

    def arguments(self, query: str, top_k: int) -> dict:
        return {
            "objective": f"查找并回答：{query}",
            "search_queries": [query],
            # 只发限流用的会话 id，不发 model_name 之类的分析字段
            "session_id": _SESSION_ID,
        }

    def parse(self, text: str, top_k: int) -> list[SearchHit]:
        try:
            data = json.loads(text or "")
        except json.JSONDecodeError:
            return []
        hits: list[SearchHit] = []
        for row in data.get("results") or []:
            url = str(row.get("url") or "")
            if not url:
                continue
            excerpts = " ".join(str(e) for e in (row.get("excerpts") or []))
            hits.append(
                SearchHit(
                    title=str(row.get("title") or url),
                    url=url,
                    snippet=" ".join(excerpts.split())[:600],
                )
            )
            if len(hits) >= top_k:
                break
        return hits
