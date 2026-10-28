"""web_fetch tool: fetch a URL and extract clean main text."""

from __future__ import annotations

from typing import Any

import httpx
from bs4 import BeautifulSoup

from agent import prompts
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
    description = prompts.TOOL_DESCRIPTIONS["web_fetch"]
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": prompts.argument_description("web_fetch", "url"),
            },
            "max_chars": {
                "type": "integer",
                "description": prompts.argument_description("web_fetch", "max_chars"),
            },
        },
        "required": ["url"],
    }
    is_concurrency_safe = True
    timeout_ms = 40_000

    #: 正文预算的默认值：与 services/settings_service.py 的既有默认保持同一个数。
    DEFAULT_MAX_CHARS = 15000
    MAX_CHARS_MIN, MAX_CHARS_MAX = 1000, 40000

    def __init__(self, fetch_html: Any = None, settings: Any = None) -> None:
        self.fetch_html = fetch_html or _fetch
        # 「设置 → 模型与服务 → 联网搜索 → 正文预算」的真实读取点。
        # 以前这里是硬编码 15000：设置在界面上能改，运行期根本不看（等于没有这个设置）。
        # 必须**每次调用时**读（用户改完立刻生效），不能在 import/构造期固化。
        self.settings = settings

    def _default_max_chars(self) -> int:
        getter = getattr(self.settings, "get_int", None)
        if not callable(getter):
            return self.DEFAULT_MAX_CHARS
        try:
            value = int(getter("search.max_fetch_chars", self.DEFAULT_MAX_CHARS))
        except Exception:  # noqa: BLE001 - 读设置失败按基线默认值，不影响抓取
            return self.DEFAULT_MAX_CHARS
        return max(self.MAX_CHARS_MIN, min(value, self.MAX_CHARS_MAX))

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
        requested = kwargs.get("max_chars", None)
        if requested is None or requested == "":
            budget = self._default_max_chars()
        else:
            try:
                budget = int(requested)
            except (TypeError, ValueError):
                budget = self._default_max_chars()
        budget = max(self.MAX_CHARS_MIN, min(budget, self.MAX_CHARS_MAX))
        return ToolResult(ok=True, content=trim_to_budget(text, budget))
