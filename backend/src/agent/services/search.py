"""Web search providers with a stability-first resolver.

Cross-source fallback tuned for Chinese-network reachability:
    bocha key  -> BochaProvider        (most stable, needs a key)
    always     -> BingProvider         (www.bing.com/search + RSS 兜底)
    user-set   -> SearxngProvider      (self-hosted instance URL only)
    last       -> BaiduProvider        (反爬严重，排在最后并带冷却)

DuckDuckGo is intentionally absent because it is unreachable from mainland
China; falling back to it would silently erase search capability.

实测坑（2026-09-12）：
* `https://cn.bing.com/search?q=...` 会被 302 到 bing 首页（14KB、0 个结果容器），
  provider 只看到 HTTP 200 → 报 "empty" → 白白回退到百度 → 百度再反爬，
  用户看到的就是「agent 一直在试百度、每次都被拒」。因此这里固定用
  `https://www.bing.com/search`（中文市场参数），并在 HTML 解析不到结果时
  用 `&format=rss` 再兜底一次；「既不是人机验证、也不是真·无结果」的页面
  一律按通道错误上报，而不是谎报「没有结果」。
* 所有通道错误信息都是中文；被反爬的通道会冷却一段时间，不再反复重试。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import quote, urlencode

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Anti-bot / captcha / access-wall markers. If a page is short AND hits one of
# these, treat it as a blocker rather than a genuine empty result set.
VERIFY_PATTERNS = ("安全验证", "百度安全", "校验", "verify", "captcha", "人机")
# 真·无结果的页面特征（区别于「被跳转/改版」）
NO_RESULTS_PATTERNS = ("没有找到", "找不到", "no results", "b_no")

BING_SEARCH_URL = "https://www.bing.com/search"
BING_MARKET = {"mkt": "zh-CN", "setlang": "zh-Hans"}

# 用户可读的通道名（错误信息里出现的一律用中文）
PROVIDER_LABELS = {"bocha": "博查", "bing": "必应", "searxng": "SearXNG", "baidu": "百度"}
PROVIDER_LABELS.update(
    {"exa": "Exa 搜索", "parallel": "Parallel 搜索", "duckduckgo": "DuckDuckGo"}
)


def provider_label(name: str) -> str:
    return PROVIDER_LABELS.get(name, name)


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


@dataclass
class SearchOutcome:
    hits: list[SearchHit] = field(default_factory=list)
    status: str = "empty"  # found | empty | error | skipped
    error: str | None = None


def _is_verify_page(text: str) -> bool:
    """Detect an anti-bot / captcha page so we do not treat it as results."""
    low = text.lower()
    return any(p in low for p in VERIFY_PATTERNS) and len(text) < 4000


def _is_no_results_page(text: str) -> bool:
    """真·无结果页面（区别于被跳转到首页/改版）。"""
    low = text.lower()
    return any(p in low for p in NO_RESULTS_PATTERNS)


def _meaningful_tokens(text: str) -> set[str]:
    """查询/结果里「有意义」的词：ascii 词 ≥2 字符，或中日韩 2-gram。"""
    from agent.selector.tokenize import tokenize

    return {t for t in tokenize(text or "") if len(t) >= 2}


def _looks_unrelated(query: str, hits: list[SearchHit]) -> bool:
    """结果与查询「毫无交集」时判定为风控诱饵页。

    实测（2026-09-12）：必应在匿名 / 无 Cookie 时会返回「标题匹配 query、内容却是
    热门站点」的诱饵页（「Python asyncio 教程」→ 阿拉伯语教育站 / Netflix / ChatGPT）。
    这种结果绝不能当事实塞给模型；判定刻意宽松：只要有一条结果命中任意一个
    有意义词就放行（避免误伤语义召回）。
    """
    query_tokens = _meaningful_tokens(query)
    if not query_tokens:
        return False  # 查询过短（单字/单字母）无法判断 → 不拦
    for hit in hits:
        if query_tokens & _meaningful_tokens(f"{hit.title} {hit.snippet}"):
            return False
    return True


def _strip_tag(node: Any) -> str:
    return node.get_text(" ", strip=True) if node else ""


class _BaseProvider:
    name = "base"

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._fetch = self._default_fetch

    async def _default_fetch(self, url: str) -> tuple[int, str]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
            return resp.status_code, resp.text

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        raise NotImplementedError


class BingProvider(_BaseProvider):
    name = "bing"

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        url = f"{BING_SEARCH_URL}?{urlencode({'q': query, **BING_MARKET})}"
        code, html = await self._fetch(url)
        if code != 200:
            return SearchOutcome(status="error", error=f"必应返回 HTTP {code}")
        if _is_verify_page(html):
            return SearchOutcome(status="skipped", error="必应要求人机验证（反爬），已跳过")
        hits = self._parse_html(html, top_k)
        if hits:
            if _looks_unrelated(query, hits):
                return SearchOutcome(
                    status="skipped",
                    error="必应返回的结果与查询无关（疑似风控拦截）",
                )
            return SearchOutcome(hits=hits, status="found")
        if _is_no_results_page(html):
            return SearchOutcome(status="empty")
        # HTML 里没有结果容器：可能是被重定向到首页或页面改版 → 用 RSS 兜底一次
        rss_code, rss = await self._fetch(url + "&format=rss")
        if rss_code == 200:
            if _is_verify_page(rss):
                return SearchOutcome(status="skipped", error="必应要求人机验证（反爬），已跳过")
            rss_hits = self._parse_rss(rss, top_k)
            if rss_hits:
                if _looks_unrelated(query, rss_hits):
                    return SearchOutcome(
                        status="skipped",
                        error="必应 RSS 返回的结果与查询无关（疑似风控拦截）",
                    )
                return SearchOutcome(hits=rss_hits, status="found")
            if _is_no_results_page(rss):
                return SearchOutcome(status="empty")
        return SearchOutcome(
            status="error",
            error=f"必应未返回结果页（服务端 HTTP {rss_code}，可能被重定向或页面改版）",
        )

    @staticmethod
    def _parse_html(html: str, top_k: int) -> list[SearchHit]:
        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []
        for li in soup.select("li.b_algo"):
            # 结果标题/链接在 h2 里；li.find("a") 会抓到站点引用锚点（a.tilk），
            # 标题会变成「github.com https://github.com › xxx」这类垃圾。
            a = li.select_one("h2 a[href]") or li.find("a", href=True)
            if not a:
                continue
            title = _strip_tag(a)
            snippet = _strip_tag(li.select_one("p"))
            hits.append(SearchHit(title=title, url=a["href"], snippet=snippet))
            if len(hits) >= top_k:
                break
        return hits

    @staticmethod
    def _parse_rss(xml_text: str, top_k: int) -> list[SearchHit]:
        """解析 Bing 的 RSS 兜底结果。

        用 stdlib 的 ElementTree：`<link>` 在 HTML 解析器里是 void 元素，
        BeautifulSoup(html.parser) 会把链接文本丢掉（实测），XML 解析才可靠。
        """
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []

        def child_text(node: Any, name: str) -> str:
            for child in node:
                if child.tag.split("}")[-1] == name:
                    return (child.text or "").strip()
            return ""

        hits: list[SearchHit] = []
        for item in root.iter():
            if item.tag.split("}")[-1] != "item":
                continue
            link = child_text(item, "link")
            if not link:
                continue
            hits.append(
                SearchHit(
                    title=child_text(item, "title"),
                    url=link,
                    snippet=child_text(item, "description"),
                )
            )
            if len(hits) >= top_k:
                break
        return hits


class BaiduProvider(_BaseProvider):
    name = "baidu"

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        url = f"https://www.baidu.com/s?wd={quote(query)}"
        code, html = await self._fetch(url)
        if code != 200:
            return SearchOutcome(status="error", error=f"百度返回 HTTP {code}")
        if _is_verify_page(html):
            return SearchOutcome(status="skipped", error="百度要求人机验证（反爬），已跳过")
        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []
        for item in soup.select("div.result, h3.t, div.c-container"):
            a = (
                (item.select_one("h3 a[href]") or item.select_one("a[href]"))
                if item.name != "a"
                else item
            )
            if a is None or not a.get("href"):
                continue
            title = _strip_tag(a)
            snippet = _strip_tag(
                item.select_one("span.content-right, div.c-abstract")
            )
            hits.append(SearchHit(title=title, url=a["href"], snippet=snippet))
            if len(hits) >= top_k:
                break
        if hits and _looks_unrelated(query, hits):
            return SearchOutcome(
                status="skipped",
                error="百度返回的结果与查询无关（疑似风控拦截）",
            )
        return SearchOutcome(hits=hits, status="found" if hits else "empty")


class BochaProvider(_BaseProvider):
    name = "bocha"

    def __init__(self, api_key: str, timeout: float = 10.0) -> None:
        super().__init__(timeout)
        self.api_key = api_key

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        url = "https://api.bochaai.com/v1/web-search"
        payload = {"query": query, "count": top_k}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001
            return SearchOutcome(status="error", error=f"博查无法访问：{exc}")
        if resp.status_code != 200:
            return SearchOutcome(
                status="error", error=f"博查返回 HTTP {resp.status_code}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            return SearchOutcome(status="error", error=f"博查返回内容不是合法 JSON：{exc}")
        # Defensive parse; the exact nesting may vary by Bocha version.
        items: list[dict[str, Any]] = []
        web_pages = ((data.get("data") or {}).get("webPages")) or {}
        items = web_pages.get("value") or []
        hits: list[SearchHit] = []
        for v in items:
            hits.append(
                SearchHit(
                    title=v.get("name", ""),
                    url=v.get("url", ""),
                    snippet=v.get("snippet", ""),
                )
            )
        return SearchOutcome(hits=hits, status="found" if hits else "empty")


class SearxngProvider(_BaseProvider):
    name = "searxng"

    def __init__(self, instance_url: str, timeout: float = 10.0) -> None:
        super().__init__(timeout)
        self.instance_url = instance_url.rstrip("/")

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        url = f"{self.instance_url}/search?q={quote(query)}&format=json"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True
            ) as client:
                resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        except Exception as exc:  # noqa: BLE001
            return SearchOutcome(
                status="error", error=f"SearXNG 无法访问：{exc}"
            )
        if resp.status_code != 200:
            return SearchOutcome(
                status="error", error=f"SearXNG 返回 HTTP {resp.status_code}"
            )
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype:
            return SearchOutcome(
                status="error", error="SearXNG 未开启 JSON 输出（请在实例设置里启用 json 格式）"
            )
        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            return SearchOutcome(
                status="error", error=f"SearXNG 返回内容不是合法 JSON：{exc}"
            )
        hits: list[SearchHit] = []
        for r in body.get("results") or []:
            hits.append(
                SearchHit(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=r.get("content", ""),
                )
            )
        return SearchOutcome(hits=hits, status="found" if hits else "empty")


class DuckDuckGoProvider(_BaseProvider):
    """DuckDuckGo HTML 端点（免密钥，OpenClaw 的 keyless provider 同款）。

    实测（2026-09-12）：本机可返回真实中文结果；连续请求会收到 202 挑战页或 429，
    因此挑战/限流一律按 skipped 处理（进入冷却），不当作「没有结果」。
    """

    name = "duckduckgo"
    HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
    CHALLENGE_MARKERS = (
        "unfortunately, bots use duckduckgo too",
        "anomaly",
        "challenge",
        "blocked",
    )

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        url = f"{self.HTML_ENDPOINT}?{urlencode({'q': query, 'kl': 'cn-zh'})}"
        try:
            code, html = await self._fetch(url)
        except Exception as exc:  # noqa: BLE001
            return SearchOutcome(status="error", error=f"DuckDuckGo 无法访问：{exc}")
        low = (html or "").lower()
        if code == 429:
            return SearchOutcome(status="skipped", error="DuckDuckGo 限流（HTTP 429），已跳过")
        if code != 200 or any(m in low for m in self.CHALLENGE_MARKERS):
            return SearchOutcome(
                status="skipped",
                error=f"DuckDuckGo 要求人机校验（HTTP {code}），已跳过",
            )
        hits = self._parse_html(html, top_k)
        if hits and _looks_unrelated(query, hits):
            return SearchOutcome(
                status="skipped", error="DuckDuckGo 返回的结果与查询无关（疑似风控拦截）"
            )
        return SearchOutcome(hits=hits, status="found" if hits else "empty")

    @staticmethod
    def _unwrap(href: str) -> str:
        """DDG 的结果链接是 //duckduckgo.com/l/?uddg=<encoded> 形式，要还原真实 URL。"""
        from urllib.parse import parse_qs, unquote, urlparse

        if "uddg=" in href:
            query = urlparse(href if href.startswith("http") else f"https:{href}").query
            values = parse_qs(query).get("uddg")
            if values:
                return unquote(values[0])
        return href

    @classmethod
    def _parse_html(cls, html: str, top_k: int) -> list[SearchHit]:
        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []
        for node in soup.select("a.result__a"):
            href = node.get("href") or ""
            if not href:
                continue
            snippet_node = node.find_next(class_="result__snippet")
            hits.append(
                SearchHit(
                    title=_strip_tag(node),
                    url=cls._unwrap(href),
                    snippet=_strip_tag(snippet_node),
                )
            )
            if len(hits) >= top_k:
                break
        return hits


class SearchProviderResolver:
    """Ordered provider chain. Order encodes "stability first" preference."""

    def __init__(
        self,
        searxng_url: str | None = None,
        bocha_api_key: str | None = None,
        keyless: bool = True,
    ) -> None:
        self.searxng_url = searxng_url
        self.bocha_api_key = bocha_api_key
        self.keyless = keyless

    def providers(self) -> list[_BaseProvider]:
        chain: list[_BaseProvider] = []
        if self.bocha_api_key:
            chain.append(BochaProvider(self.bocha_api_key))
        if self.searxng_url:
            # 自建实例：用户显式配置，按原样排在免密钥通道之前
            chain.append(SearxngProvider(self.searxng_url))
        if self.keyless:
            # 免密钥通道（2026-09-12 实测可用）：Exa / Parallel 免费 MCP + DuckDuckGo HTML
            from agent.services.mcp_search import ExaMcpProvider, ParallelMcpProvider

            chain.append(ExaMcpProvider())
            chain.append(ParallelMcpProvider())
            chain.append(DuckDuckGoProvider())
        # 抓取类通道排在最后：必应会返回风控诱饵页，百度常年安全验证
        chain.append(BingProvider())
        chain.append(BaiduProvider())
        return chain


class SearchService:
    """Facade the tools call; tries providers in order, stops at first found.

    被判定为反爬（skipped）的通道会冷却一段时间，避免每次搜索都去撞同一堵墙 ——
    这正是用户看到的「agent 反复尝试百度、每次都被拒」的来源。
    """

    def __init__(
        self,
        searxng_url: str | None = None,
        bocha_api_key: str | None = None,
        keyless_fallback: bool = True,
    ) -> None:
        # 每个通道最近一次真实检查的结论（进程内；重启后回到「未检查」）。
        self._checks: dict[str, ChannelCheck] = {}
        # 结论对应的配置指纹：配置变了就作废旧结论（不拿旧结论骗人）。
        self._check_fingerprints: dict[str, tuple] = {}
        self.set_config(
            searxng_url=searxng_url,
            bocha_api_key=bocha_api_key,
            keyless_fallback=keyless_fallback,
        )

    def set_config(
        self,
        searxng_url: str | None = None,
        bocha_api_key: str | None = None,
        keyless_fallback: bool = True,
    ) -> None:
        self._resolver = SearchProviderResolver(
            searxng_url=searxng_url,
            bocha_api_key=bocha_api_key,
            keyless=keyless_fallback,
        )
        # 配置变化（例如用户填了博查 Key）后重新给所有通道机会
        self._disabled_until: dict[str, float] = {}
        # 只作废**受影响通道**的检查结论：关闭免密钥通道不等于博查 Key 变了，
        # 不能顺手把别的通道的「检查通过」也抹掉。
        self._drop_stale_checks()

    # -- 通道状态 / 真实连接检查 -----------------------------------------

    @staticmethod
    def _digest(value: str) -> str:
        """配置指纹（截断摘要）：只用于比较配置有没有变，不保存、不打印原文。"""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

    def _channel_fingerprint(self, channel_id: str) -> tuple:
        """这条通道的「检查结论还成立吗」的判据（含密钥摘要，不含密钥原文）。"""
        spec = _CHANNEL_BY_ID.get(channel_id) or {}
        if spec.get("needs_key"):
            key = str(self._resolver.bocha_api_key or "").strip()
            return (channel_id, bool(key), self._digest(key))
        if spec.get("needs_url"):
            return (channel_id, str(self._resolver.searxng_url or "").strip())
        if spec.get("keyless"):
            return (channel_id, bool(self._resolver.keyless))
        return (channel_id,)

    def _drop_stale_checks(self) -> None:
        """配置变了的通道：旧结论作废，回到「未检查」。"""
        for channel_id in list(self._checks):
            fingerprint = self._channel_fingerprint(channel_id)
            if self._check_fingerprints.get(channel_id) != fingerprint:
                self._checks[channel_id] = ChannelCheck()
                self._check_fingerprints[channel_id] = fingerprint

    def chain_ids(self) -> list[str]:
        """当前真实回退顺序（通道 id 列表；纯本地，不发请求）。"""
        return [provider.name for provider in self._resolver.providers()]

    def _configured(self, channel_id: str) -> bool:
        """配置齐不齐 —— 与「检查通过」是两件事。"""
        spec = _CHANNEL_BY_ID.get(channel_id) or {}
        if spec.get("needs_key"):
            return bool(str(self._resolver.bocha_api_key or "").strip())
        if spec.get("needs_url"):
            return bool(str(self._resolver.searxng_url or "").strip())
        return True

    def channel_status(
        self, channel_ids: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """各通道状态（不含任何密钥，也不发请求）。

        ``usable is None`` 表示**没有检查过**：界面必须显示「未检查」，
        不能因为 configured=True 就画成「已验证可用」。
        """
        chain = self.chain_ids()
        wanted = [
            spec
            for spec in CHANNEL_SPECS
            if not channel_ids or spec["id"] in set(channel_ids)
        ]
        rows: list[dict[str, Any]] = []
        for spec in wanted:
            channel_id = spec["id"]
            check = self._checks.get(channel_id) or ChannelCheck()
            enabled = channel_id in chain
            if check.usable is None:
                check_state = "unchecked"
            else:
                check_state = "passed" if check.usable else "failed"
            rows.append(
                {
                    "id": channel_id,
                    "label": spec["label"],
                    "group": spec.get("group", ""),
                    "needs_key": bool(spec.get("needs_key")),
                    "needs_url": bool(spec.get("needs_url")),
                    "keyless": bool(spec.get("keyless")),
                    "configured": self._configured(channel_id),
                    "enabled": enabled,
                    "position": (chain.index(channel_id) + 1) if enabled else None,
                    "usable": check.usable,
                    "check_state": check_state,
                    "checked_at": check.checked_at,
                    "checking": check.checking,
                    "reason_code": check.reason_code,
                    "message": check.message,
                }
            )
        return rows

    async def check_channels(
        self, channel_ids: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """对指定通道发**真实**的极小请求并记录结论，返回全部通道状态。

        约定：
        * 一次检查就是一次真实调用，成功与否只由这次调用的结果决定
          （不存在「定时任务成功过所以显示可用」这种事）；
        * 已经在检查的通道不重复发请求（``checking=True`` 直接返回）；
        * 未配置的通道不发请求，直接记 ``not_configured``；
        * 任何异常都变成结论（``unknown``/``unreachable``），不让接口 500。
        """
        wanted = [
            spec["id"]
            for spec in CHANNEL_SPECS
            if not channel_ids or spec["id"] in set(channel_ids)
        ]
        # 并发检查：每条通道一次独立请求，互不影响。串行 7 条 × 8s 超时最坏要等一分钟，
        # 「点一下检查连接」不能这么慢；并发后最坏只等一条的时间。
        await asyncio.gather(
            *(self._check_one(channel_id) for channel_id in wanted),
            return_exceptions=True,
        )
        return self.channel_status(wanted)

    async def _check_one(self, channel_id: str) -> None:
        """检查一条通道并把结论写回（异常一律变成结论，不让整批失败）。"""
        check = self._checks.setdefault(channel_id, ChannelCheck())
        if check.checking:
            return
        configured = self._configured(channel_id)
        check.checking = True
        self._check_fingerprints[channel_id] = self._channel_fingerprint(channel_id)
        try:
            if not configured:
                probe = ChannelProbe(
                    False,
                    REASON_NOT_CONFIGURED,
                    f"{_CHANNEL_BY_ID[channel_id]['label']}还没有配置（没有可检查的凭据或地址）",
                )
            else:
                probe = await self._probe_channel(channel_id)
        except Exception as exc:  # noqa: BLE001 - 检查自身出错也是「这次没结论」
            probe = ChannelProbe(
                False, REASON_UNKNOWN, f"检查失败（{type(exc).__name__}）"
            )
        finally:
            check.checking = False
        check.usable = probe.usable
        check.reason_code = probe.reason_code
        check.message = probe.message
        check.checked_at = _now_iso()

    async def _probe_channel(self, channel_id: str) -> ChannelProbe:
        """把通道 id 分派到对应的真实检查实现。"""
        spec = _CHANNEL_BY_ID[channel_id]
        label = spec["label"]
        if channel_id == "bocha":
            return await probe_bocha(self._resolver.bocha_api_key)
        if channel_id == "searxng":
            return await probe_searxng(self._resolver.searxng_url)
        provider = next(
            (item for item in self._resolver.providers() if item.name == channel_id),
            None,
        )
        if provider is None:
            return ChannelProbe(False, REASON_DISABLED, f"{label}当前没有参与搜索")
        return await probe_provider(provider, label)

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        from agent.services.params import SEARCH

        errors: list[str] = []
        cooldown = max(0, int(SEARCH.provider_cooldown_seconds))
        now = time.monotonic()
        for provider in self._resolver.providers():
            label = provider_label(provider.name)
            if self._disabled_until.get(provider.name, 0.0) > now:
                errors.append(f"{label}：此前被反爬拦截，{cooldown // 60} 分钟内不再尝试")
                continue
            outcome = await provider.search(query, top_k)
            if outcome.status == "found":
                return outcome
            if outcome.status == "skipped":
                # 反爬/人机验证：冷却该通道，别在后续搜索里反复撞
                self._disabled_until[provider.name] = now + cooldown
                if outcome.error:
                    errors.append(f"{label}：{outcome.error}")
                continue
            if outcome.error:
                errors.append(f"{label}：{outcome.error}")
        if errors:
            return SearchOutcome(
                status="error",
                error=(
                    "联网搜索暂不可用（"
                    + "；".join(errors)
                    + "）。可在「设置 → 模型与联网」配置博查 API Key，"
                    "或配置可用的 SearXNG 实例来恢复联网搜索。"
                ),
            )
        return SearchOutcome(status="empty")


# ---------------------------------------------------------------------------
# 通道状态与真实连接检查（设置页改版 §9）
# ---------------------------------------------------------------------------
#
# 为什么单独一块
# --------------
# 「已配置」和「连接检查通过」是两件事，以前只有一个布尔（有没有填 Key）：
# 用户填错 Key、SearXNG 没开 JSON、免密钥通道被风控，界面上都显示成「已配置」。
# 这里把两者分开：
#
# * configured —— 配置齐不齐（有 Key / 有地址 / 通道本身免密钥）—— 纯本地判断；
# * usable —— **真的发过一次极小请求**之后的结论；没检查过就是 None（未检查），
#   绝不用「定时任务成功过」或「上次检查过」冒充当前可用。
#
# 免密钥通道不是总开关：关掉它只从回退链里移除 exa / parallel / duckduckgo，
# 既不清除博查 Key / SearXNG 地址，也不改变其余通道的相对顺序（见 chain_ids）。

CHECK_QUERY = "QIO 连接检查"
CHECK_TIMEOUT_SECONDS = 8.0

REASON_OK = "ok"
REASON_EMPTY = "empty"
REASON_NOT_CONFIGURED = "not_configured"
REASON_DISABLED = "disabled"
REASON_UNREACHABLE = "unreachable"
REASON_TIMEOUT = "timeout"
REASON_RATE_LIMIT = "rate_limit"
REASON_INVALID_KEY = "invalid_key"
REASON_BLOCKED = "blocked"
REASON_HTTP_ERROR = "http_error"
REASON_BAD_RESPONSE = "bad_response"
REASON_JSON_DISABLED = "json_disabled"
REASON_UNKNOWN = "unknown"

#: 每个通道的静态元数据。**id 与 provider.name 一致**（回退链按 name 对齐）。
CHANNEL_SPECS: tuple[dict[str, Any], ...] = (
    {"id": "bocha", "label": "博查", "group": "key", "needs_key": True, "keyless": False},
    {
        "id": "searxng",
        "label": "SearXNG（自建）",
        "group": "self_hosted",
        "needs_url": True,
        "keyless": False,
    },
    {"id": "exa", "label": "Exa 搜索（免密钥）", "group": "keyless", "keyless": True},
    {
        "id": "parallel",
        "label": "Parallel 搜索（免密钥）",
        "group": "keyless",
        "keyless": True,
    },
    {
        "id": "duckduckgo",
        "label": "DuckDuckGo（免密钥）",
        "group": "keyless",
        "keyless": True,
    },
    {"id": "bing", "label": "必应", "group": "scraper", "keyless": False},
    {"id": "baidu", "label": "百度", "group": "scraper", "keyless": False},
)

_CHANNEL_BY_ID = {spec["id"]: spec for spec in CHANNEL_SPECS}


@dataclass
class ChannelProbe:
    """一次真实检查的结论：能用了没有 + 原因码 + 给用户看的中文。"""

    usable: bool
    reason_code: str
    message: str


@dataclass
class ChannelCheck:
    """某通道最近一次检查的事实（不持久化：重启后重新检查，未知比假通过好）。"""

    usable: bool | None = None
    checked_at: str | None = None
    reason_code: str | None = None
    message: str = ""
    checking: bool = False

    def reset(self) -> None:
        self.usable = None
        self.checked_at = None
        self.reason_code = None
        self.message = ""
        self.checking = False


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def _http_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = CHECK_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    """一次极小请求（连接检查的唯一网络出口；测试打桩替换它，不联网）。"""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        if payload is None:
            resp = await client.get(url, headers=headers or {})
        else:
            resp = await client.post(url, json=payload, headers=headers or {})
        return resp.status_code, resp.text


def _classify_exception(exc: BaseException, label: str) -> ChannelProbe:
    """网络异常 → 原因码。只按异常类型分类，不猜内容。"""
    name = type(exc).__name__
    if isinstance(exc, httpx.TimeoutException) or "timeout" in name.lower():
        return ChannelProbe(
            False, REASON_TIMEOUT, f"{label}响应超时（检查请求已取消，稍后可重试）"
        )
    if isinstance(exc, (httpx.HTTPError, OSError)):
        return ChannelProbe(
            False,
            REASON_UNREACHABLE,
            f"连不上{label}：请检查网络、代理或地址（{name}）",
        )
    return ChannelProbe(False, REASON_UNKNOWN, f"{label}检查失败（{name}）")


def _classify_status(code: int, label: str) -> ChannelProbe:
    if code in (401, 403):
        return ChannelProbe(False, REASON_INVALID_KEY, f"{label}凭据无效或没有权限（HTTP {code}）")
    if code == 429:
        return ChannelProbe(False, REASON_RATE_LIMIT, f"{label}限流（HTTP 429），稍后可重试")
    return ChannelProbe(False, REASON_HTTP_ERROR, f"{label}返回 HTTP {code}")


async def probe_bocha(
    api_key: str | None, *, timeout: float = CHECK_TIMEOUT_SECONDS
) -> ChannelProbe:
    """博查：一次 count=1 的最小搜索。区分凭据无效 / 限流 / 断网 / 超时。"""
    key = str(api_key or "").strip()
    if not key:
        return ChannelProbe(False, REASON_NOT_CONFIGURED, "还没有填写博查 API Key")
    try:
        code, text = await _http_request(
            "https://api.bochaai.com/v1/web-search",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            payload={"query": CHECK_QUERY, "count": 1},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 网络异常统一翻译成原因码
        return _classify_exception(exc, "博查")
    if code >= 400:
        return _classify_status(code, "博查")
    try:
        json.loads(text)
    except (TypeError, ValueError):
        return ChannelProbe(False, REASON_BAD_RESPONSE, "博查返回内容不是合法 JSON")
    return ChannelProbe(True, REASON_OK, "博查可用（检查请求返回正常）")


async def probe_searxng(
    instance_url: str | None, *, timeout: float = CHECK_TIMEOUT_SECONDS
) -> ChannelProbe:
    """SearXNG：真的打一次 /search?format=json（这正是正式搜索用的入口）。"""
    base = str(instance_url or "").strip().rstrip("/")
    if not base:
        return ChannelProbe(False, REASON_NOT_CONFIGURED, "还没有填写 SearXNG 地址")
    url = f"{base}/search?" + urlencode({"q": CHECK_QUERY, "format": "json"})
    try:
        code, text = await _http_request(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return _classify_exception(exc, "SearXNG")
    if code >= 400:
        return _classify_status(code, "SearXNG")
    try:
        json.loads(text)
    except (TypeError, ValueError):
        return ChannelProbe(
            False,
            REASON_JSON_DISABLED,
            "SearXNG 没有开启 JSON 输出（请在实例设置里启用 json 格式）",
        )
    return ChannelProbe(True, REASON_OK, "SearXNG 可用（JSON 输出正常）")


async def probe_provider(provider: _BaseProvider, label: str) -> ChannelProbe:
    """用通道自己的实现真打一次（免密钥 MCP / 抓取类共用）。"""
    try:
        outcome = await provider.search(CHECK_QUERY, top_k=1)
    except Exception as exc:  # noqa: BLE001 - 通道实现抛错也要给结论，不能让检查失败变 500
        return _classify_exception(exc, label)
    if outcome.status == "found":
        return ChannelProbe(True, REASON_OK, f"{label}可用（检查请求有返回）")
    if outcome.status == "empty":
        # 通道本身通了，只是这次没搜到结果：可用性不能因此判失败（查询是固定的探测词）。
        return ChannelProbe(True, REASON_EMPTY, f"{label}可达，但这次检查没有返回结果")
    text = str(outcome.error or "")
    low = text.lower()
    if outcome.status == "skipped":
        if "限流" in text or "429" in low:
            return ChannelProbe(False, REASON_RATE_LIMIT, f"{label}限流，稍后可重试")
        return ChannelProbe(False, REASON_BLOCKED, f"{label}要求人机验证（反爬），当前不可用")
    if "401" in low or "403" in low or "无效" in text:
        return ChannelProbe(False, REASON_INVALID_KEY, f"{label}凭据无效或没有权限")
    if "无法访问" in text or "连不上" in text:
        return ChannelProbe(False, REASON_UNREACHABLE, f"连不上{label}：{text}")
    if "超时" in text or "timeout" in low:
        return ChannelProbe(False, REASON_TIMEOUT, f"{label}响应超时")
    return ChannelProbe(False, REASON_HTTP_ERROR, f"{label}不可用：{text or '未知原因'}")
