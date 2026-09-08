"""Web search providers with a stability-first resolver.

Cross-source fallback tuned for Chinese-network reachability:
    bocha key  -> BochaProvider        (most stable, needs a key)
    no key     -> BingProvider -> BaiduProvider  (mutual fallback)
    user-set   -> SearxngProvider      (self-hosted instance URL only)

DuckDuckGo is intentionally absent because it is unreachable from mainland
China; falling back to it would silently erase search capability.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Anti-bot / captcha / access-wall markers. If a page is short AND hits one of
# these, treat it as a blocker rather than a genuine empty result set.
VERIFY_PATTERNS = ("安全验证", "百度安全", "校验", "verify", "captcha", "人机")


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
        url = f"https://cn.bing.com/search?q={quote(query)}"
        code, html = await self._fetch(url)
        if code != 200:
            return SearchOutcome(status="error", error=f"bing http {code}")
        if _is_verify_page(html):
            return SearchOutcome(status="skipped", error="bing anti-bot page")
        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []
        for li in soup.select("li.b_algo"):
            a = li.find("a", href=True)
            if not a:
                continue
            title = _strip_tag(a)
            snippet = _strip_tag(li.select_one("p"))
            hits.append(SearchHit(title=title, url=a["href"], snippet=snippet))
            if len(hits) >= top_k:
                break
        return SearchOutcome(hits=hits, status="found" if hits else "empty")


class BaiduProvider(_BaseProvider):
    name = "baidu"

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        url = f"https://www.baidu.com/s?wd={quote(query)}"
        code, html = await self._fetch(url)
        if code != 200:
            return SearchOutcome(status="error", error=f"baidu http {code}")
        if _is_verify_page(html):
            return SearchOutcome(status="skipped", error="baidu anti-bot page")
        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []
        for item in soup.select("div.result, h3.t, div.c-container"):
            a = item.find("a", href=True) if item.name != "a" else item
            if a is None or not a.get("href"):
                continue
            title = _strip_tag(a)
            snippet = _strip_tag(
                item.select_one("span.content-right, div.c-abstract")
            )
            hits.append(SearchHit(title=title, url=a["href"], snippet=snippet))
            if len(hits) >= top_k:
                break
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
            return SearchOutcome(status="error", error=f"bocha unreachable: {exc}")
        if resp.status_code != 200:
            return SearchOutcome(
                status="error", error=f"bocha http {resp.status_code}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            return SearchOutcome(status="error", error=f"bocha bad json: {exc}")
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
                status="error", error=f"searxng unreachable: {exc}"
            )
        if resp.status_code != 200:
            return SearchOutcome(
                status="error", error=f"searxng http {resp.status_code}"
            )
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype:
            return SearchOutcome(
                status="error", error="searxng does not serve json"
            )
        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            return SearchOutcome(
                status="error", error=f"searxng bad json: {exc}"
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


class SearchProviderResolver:
    """Ordered provider chain. Order encodes "stability first" preference."""

    def __init__(
        self,
        searxng_url: str | None = None,
        bocha_api_key: str | None = None,
    ) -> None:
        self.searxng_url = searxng_url
        self.bocha_api_key = bocha_api_key

    def providers(self) -> list[_BaseProvider]:
        chain: list[_BaseProvider] = []
        if self.bocha_api_key:
            chain.append(BochaProvider(self.bocha_api_key))
        chain.append(BingProvider())
        chain.append(BaiduProvider())
        if self.searxng_url:
            chain.append(SearxngProvider(self.searxng_url))
        return chain


class SearchService:
    """Facade the tools call; tries providers in order, stops at first found."""

    def __init__(
        self,
        searxng_url: str | None = None,
        bocha_api_key: str | None = None,
    ) -> None:
        self.set_config(searxng_url=searxng_url, bocha_api_key=bocha_api_key)

    def set_config(
        self,
        searxng_url: str | None = None,
        bocha_api_key: str | None = None,
    ) -> None:
        self._resolver = SearchProviderResolver(
            searxng_url=searxng_url, bocha_api_key=bocha_api_key
        )

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        errors: list[str] = []
        for provider in self._resolver.providers():
            outcome = await provider.search(query, top_k)
            if outcome.status == "found":
                return outcome
            if outcome.error:
                errors.append(f"{provider.name}: {outcome.error}")
        if errors:
            return SearchOutcome(
                status="error", error="all providers failed: " + "; ".join(errors)
            )
        return SearchOutcome(status="empty")
