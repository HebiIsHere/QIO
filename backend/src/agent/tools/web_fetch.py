"""web_fetch tool: fetch a URL and extract clean main text."""

from __future__ import annotations

from typing import Any

import httpx
from bs4 import BeautifulSoup

from agent.tools.base import Tool, ToolResult

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BAD_HTML_FLAGS = ("captcha", "安全验证", "人机", "verify", "access denied")


def trim_to_budget(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    sep = "\n…[有省略]…\n"
    if budget <= len(sep):
        return text[:budget]
    each = (budget - len(sep)) // 2
    return text[:each] + sep + text[-each:]


async def _fetch(url: str, timeout: float = 15.0) -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        return resp.status_code, resp.text


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "抓取指定网址并提取正文纯文本，供 agent 阅读。"
        "url 必填，max_chars 可选（默认 15000，上限 40000）。"
        "不会抓取需登录/验证的内容；失败时给出原因，不编造正文。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "目标网址"},
            "max_chars": {"type": "integer", "description": "正文预算字符数，默认 15000"},
        },
        "required": ["url"],
    }
    is_concurrency_safe = True
    timeout_ms = 40_000

    def __init__(self, fetch_html: Any = None) -> None:
        self.fetch_html = fetch_html or _fetch

    async def run(self, **kwargs: Any) -> ToolResult:
        url = str(kwargs.get("url", "") or "").strip()
        if not url:
            return ToolResult(ok=False, error="url 必填")
        if not url.lower().startswith(("http://", "https://")):
            return ToolResult(ok=False, error="url 必须是 http(s) 地址")
        code, html = await self.fetch_html(url)
        if code != 200:
            return ToolResult(ok=False, error=f"无法读取正文（HTTP {code}）")
        low = html.lower()
        if any(f in low for f in BAD_HTML_FLAGS):
            return ToolResult(
                ok=False, error="该页面需要登录或验证，无法读取正文"
            )
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "aside"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        if not text:
            return ToolResult(ok=False, error="该页面无正文可提取")
        try:
            budget = int(kwargs.get("max_chars", 15000))
        except (TypeError, ValueError):
            budget = 15000
        budget = max(1000, min(budget, 40000))
        return ToolResult(ok=True, content=trim_to_budget(text, budget))
