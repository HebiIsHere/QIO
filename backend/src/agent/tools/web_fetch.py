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
# 只有明确短语才算「登录 / 验证墙」：裸词 verify 会命中页面里无关的文字
# （真实事故：站点邀请 AI agent 读 /api/AGENTS 的说明被判成验证墙，14 次）。
BAD_HTML_FLAGS = (
    "captcha",
    "安全验证",
    "人机验证",
    "access denied",
    "verify you are human",
    "checking your browser",
    "just a moment",
)
# 页面自己声明「要有 JavaScript 才能用」：这是脚本渲染，不是登录墙。
# 真实事故：Project Amber 的正文由脚本生成，抓取器读到的只有这句提示，
# 以前被报成「需要登录或验证」，模型以为整批站点都读不到。
JS_REQUIRED_FLAGS = ("without javascript", "enable javascript", "javascript is disabled")


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
        try:
            code, html = await self.fetch_html(url)
        except Exception as exc:  # noqa: BLE001 - 网络层失败必须说清是哪个地址
            return ToolResult(
                ok=False,
                error=(
                    f"抓取失败：连不上 {url}"
                    f"（{type(exc).__name__}: {str(exc).strip() or '没有更多说明'}）"
                ),
            )
        if code != 200:
            return ToolResult(ok=False, error=f"无法读取正文（HTTP {code}）")
        low = html.lower()
        if any(f in low for f in JS_REQUIRED_FLAGS):
            return ToolResult(
                ok=False,
                error=(
                    "网页正文由 JavaScript 在浏览器里生成，抓取器读不到"
                    "（不是登录或验证问题；该站点若有数据接口，直接抓接口）"
                ),
            )
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
