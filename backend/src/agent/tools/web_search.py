"""web_search tool: query a pluggable search service (stability-first).

失败时的行为约定：把中文原因（含建议）如实回报给模型，并明确不要在
同一轮里对同一查询反复重试 —— 通道被风控时重试只会得到同样的结果。
"""

from __future__ import annotations

from typing import Any

from agent import prompts
from agent.tools.base import Tool, ToolResult


class WebSearchTool(Tool):
    name = "web_search"
    description = prompts.TOOL_DESCRIPTIONS["web_search"]
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": prompts.argument_description("web_search", "query"),
            },
            "top_k": {
                "type": "integer",
                "description": prompts.argument_description("web_search", "top_k"),
            },
        },
        "required": ["query"],
    }
    is_concurrency_safe = True
    timeout_ms = 30_000

    #: 返回条数的默认值与范围：与 services/settings_service.py 的 SEARCH_TOP_K_* 同口径。
    DEFAULT_TOP_K = 5
    TOP_K_MIN, TOP_K_MAX = 1, 20

    def __init__(self, search_service: Any = None, settings: Any = None) -> None:
        self.search_service = search_service
        # 「设置 → 模型与服务 → 联网搜索 → 返回条数」的真实读取点：以前硬编码 5，
        # 设置改了运行期不看。**每次调用时**读，用户改完立刻生效。
        self.settings = settings

    def _default_top_k(self) -> int:
        getter = getattr(self.settings, "get_int", None)
        if not callable(getter):
            return self.DEFAULT_TOP_K
        try:
            value = int(getter("search.top_k_default", self.DEFAULT_TOP_K))
        except Exception:  # noqa: BLE001 - 读设置失败按基线默认值
            return self.DEFAULT_TOP_K
        return max(self.TOP_K_MIN, min(value, self.TOP_K_MAX))

    async def run(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query", "") or "").strip()
        if not query:
            return ToolResult(ok=False, error="query is required")
        requested = kwargs.get("top_k", None)
        if requested is None or requested == "":
            top_k = self._default_top_k()
        else:
            try:
                top_k = int(requested)
            except (TypeError, ValueError):
                top_k = self._default_top_k()
        top_k = max(self.TOP_K_MIN, min(top_k, self.TOP_K_MAX))
        if self.search_service is None:
            return ToolResult(ok=False, error="搜索服务未配置")
        outcome = await self.search_service.search(query, top_k)
        if outcome.status == "error":
            return ToolResult(
                ok=False,
                error=f"无法联网搜索：{outcome.error}（不要重复重试同一查询）",
            )
        if outcome.status == "empty" or not outcome.hits:
            return ToolResult(ok=True, content="未找到相关结果")
        lines = [
            f"- {h.title} {h.url}" + (f" — {h.snippet}" if h.snippet else "")
            for h in outcome.hits
        ]
        return ToolResult(ok=True, content="\n".join(lines))
