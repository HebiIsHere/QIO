# QIO 联网搜索工具 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 qio 增加可插拔的联网搜索（`web_search`）与读取网页正文（`web_fetch`）两项原生工具，默认用必应/百度免密钥互兜底，博查/SearXNG 可作为可选项；并新增前端搜索配置卡片。

**Architecture:** 新增一个 `SearchService`（内含 `SearchProviderResolver`），按「稳定可达」优先级解析来源；两个原生 `Tool` 通过 `ServiceRegistry` 注入；配置走现有 `SettingsStore`（SQLite key-value）+ `GET/PUT /api/settings/search`；前端在 `SettingsView` 的 pref tab 加一张卡片。浏览器控制单独排期，本轮不做。

**Tech Stack:** Python (FastAPI, pydantic, httpx, bs4), Vue3 (TS), vitest, pytest。

**Spec:** [2026-09-08-web-search-design.md](/C:/Users/zxy/Documents/Front agent/qio/docs/superpowers/specs/2026-09-08-web-search-design.md)

## Global Constraints

- 后端工具必须继承 `agent.tools.base.Tool`，返回 `ToolResult(ok=bool, content=str, error=str|None)`。
- 通过 `ServiceRegistry`（`inject` 列表）注入服务；`ToolRegistry.register` 挂载。
- 测试后端用 `PYTHONPATH=backend/src` + pytest；fixture 有 `db_conn`、`settings`、以及 `client(db_conn, settings)`（见 `backend/tests/test_api_routes.py`）。
- 前端颜色一律 `var(--*)`（`frontend/src/styles/tokens.css`），禁止硬编码色值。
- 三声部字体：标题/话题=衬线 `--serif`；正文=无衬线 `--sans`；数据/密钥/预算/ID=等宽 `--mono`。
- 前端测试 `node node_modules/vitest/vitest.mjs run`（cwd=`frontend`）；类型检查 `node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`。
- 网络请求用 `httpx`（后端已依赖），不新增 requests 于生产代码。
- 后端无热加载，改后需重启 uvicorn（`127.0.0.1:8734`）。

---

## File Structure

- `backend/src/agent/services/search.py` —— `SearchHit` / `SearchOutcome` 数据类；`BingProvider` / `BaiduProvider` / `BochaProvider` / `SearxngProvider`（各自 `search(query, top_k)`）；`SearchProviderResolver`（优先级与互兜底）；`SearchService`（对外 `search`）。
- `backend/src/agent/tools/web_search.py` —— `WebSearchTool`。
- `backend/src/agent/tools/web_fetch.py` —— `WebFetchTool`。
- `backend/src/agent/api/server.py` —— 新增 `GET/PUT /api/settings/search`。
- `backend/src/agent/services/app.py` —— 实例化 `SearchService`、注册服务、挂载两个工具、把 `web_search` 加入 `CORE_TOOLS`。
- `frontend/src/services/api.ts` —— 新增 `getSearchSettings` / `updateSearchSettings`。
- `frontend/src/views/SettingsView.vue` —— pref tab 新增「联网搜索」卡片。
- `backend/tests/test_search.py` —— 提供方解析、resolver 优先级、三态。
- `backend/tests/test_web_search_tool.py` —— `WebSearchTool` 三态与参数校验。
- `backend/tests/test_web_fetch_tool.py` —— `WebFetchTool` 抓取/提取/预算/失败。
- `backend/tests/test_settings_search_api.py` —— `GET/PUT /api/settings/search`。
- `frontend/src/views/__tests__/SettingsView.test.ts` —— 搜索卡片渲染与读写。

---

### Task 1: 搜索提供方抽象层与解析器（`services/search.py`）

**Files:**
- Create: `backend/src/agent/services/search.py`
- Test: `backend/tests/test_search.py`

**Interfaces:**
- Produces: `SearchHit(title:str, url:str, snippet:str)`；`SearchOutcome(hits:list, status:str, error:str|None)`；`SearchService.search(query:str, top_k:int=5) -> SearchOutcome`；`SearchService.set_config(searxng_url:str|None, bocha_api_key:str|None)`。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_search.py`：
```python
from __future__ import annotations

from agent.services.search import (
    BaiduProvider,
    BingProvider,
    BochaProvider,
    SearchHit,
    SearchOutcome,
    SearchProviderResolver,
    SearxngProvider,
)


async def test_baidu_provider_sets_status_boom_on_garbage():
    # Baidu 返回安全验证页时应标记为该源失败（status='skipped'），不当作真实结果
    provider = BaiduProvider()

    async def _fake(url: str = "") -> tuple[int, str]:
        return (200, "<html>百度安全验证</html>")

    provider._fetch = _fake
    outcome = await provider.search("openai", top_k=5)
    assert outcome.status in ("skipped", "error")


def test_resolver_prefers_bocha_when_key_present():
    resolver = SearchProviderResolver(bocha_api_key="bk-1")
    # bocha key 存在时应返回 BochaProvider 作为首选
    assert any(isinstance(p, BochaProvider) for p in resolver.providers())


def test_resolver_chains_bing_then_baidu_without_key():
    resolver = SearchProviderResolver(bocha_api_key=None)
    names = [type(p).__name__ for p in resolver.providers()]
    # 无 key：Bing → Baidu 互兜底，不应包含 DuckDuckGo
    assert names[0] == "BingProvider"
    assert "BaiduProvider" in names
    assert "DuckDuckGoProvider" not in names


def test_resolver_includes_searxng_only_when_url_set():
    r_none = SearchProviderResolver(searxng_url=None)
    assert not any(isinstance(p, SearxngProvider) for p in r_none.providers())
    r_set = SearchProviderResolver(searxng_url="http://127.0.0.1:8080")
    assert any(isinstance(p, SearxngProvider) for p in r_set.providers())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_search.py -v`
Expected: FAIL（`ModuleNotFoundError: agent.services.search` / 属性缺失）

- [ ] **Step 3: 实现 `services/search.py`**

```python
"""Web search providers with a stability-first resolver.

Cross-source fallback for Chinese-network reachability:
    bocha key  -> BochaProvider        (most stable, needs key)
    no key     -> BingProvider -> BaiduProvider  (mutual fallback)
    user-set   -> SearxngProvider      (self-hosted only)
DuckDuckGo is intentionally absent (unreachable in CN).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
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
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
            return resp.status_code, resp.text

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        raise NotImplementedError


class BingProvider(_BaseProvider):
    name = "bing"

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        url = f"https://cn.bing.com/search?q={query}"
        code, html = await self._fetch(url)
        if code != 200:
            return SearchOutcome(status="error", error=f"bing http {code}")
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
        url = f"https://www.baidu.com/s?wd={query}"
        code, html = await self._fetch(url)
        if code != 200:
            return SearchOutcome(status="error", error=f"baidu http {code}")
        if _is_verify_page(html):
            return SearchOutcome(status="skipped", error="baidu anti-bot page")
        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []
        for item in soup.select("div.result, div.c-container"):
            a = item.find("a", href=True)
            if a is None or not a.get("href"):
                continue
            title = _strip_tag(a)
            snippet = _strip_tag(item.select_one("span.content-right, div.c-abstract"))
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
        # 博查 AI 端点：POST /v1/web-search；返回 {data: {webPages: {value:[{name,url,snippet}]}}}
        url = "https://api.bochaai.com/v1/web-search"
        payload = {"query": query, "count": top_k}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            return SearchOutcome(status="error", error=f"bocha http {resp.status_code}")
        data = resp.json()
        items = (data.get("data") or {}).get("webPages") or {}
        hits: list[SearchHit] = []
        for v in items.get("value") or []:
            hits.append(SearchHit(title=v.get("name", ""), url=v.get("url", ""), snippet=v.get("snippet", "")))
        return SearchOutcome(hits=hits, status="found" if hits else "empty")


class SearxngProvider(_BaseProvider):
    name = "searxng"

    def __init__(self, instance_url: str, timeout: float = 10.0) -> None:
        super().__init__(timeout)
        self.instance_url = instance_url.rstrip("/")

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        url = f"{self.instance_url}/search?q={query}&format=json"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        except Exception as exc:  # noqa: BLE001
            return SearchOutcome(status="error", error=f"searxng unreachable: {exc}")
        if resp.status_code != 200:
            return SearchOutcome(status="error", error=f"searxng http {resp.status_code}")
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype:
            return SearchOutcome(status="error", error="searxng does not serve json")
        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            return SearchOutcome(status="error", error=f"searxng bad json: {exc}")
        hits: list[SearchHit] = []
        for r in body.get("results") or []:
            hits.append(SearchHit(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("content", "")))
        return SearchOutcome(hits=hits, status="found" if hits else "empty")


class SearchProviderResolver:
    def __init__(self, searxng_url: str | None = None, bocha_api_key: str | None = None) -> None:
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

    def __init__(self, searxng_url: str | None = None, bocha_api_key: str | None = None) -> None:
        self.set_config(searxng_url=searxng_url, bocha_api_key=bocha_api_key)

    def set_config(self, searxng_url: str | None = None, bocha_api_key: str | None = None) -> None:
        self._resolver = SearchProviderResolver(searxng_url=searxng_url, bocha_api_key=bocha_api_key)

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        errors: list[str] = []
        for provider in self._resolver.providers():
            outcome = await provider.search(query, top_k)
            if outcome.status == "found":
                return outcome
            if outcome.status == "error":
                errors.append(f"{provider.name}: {outcome.error}")
            if outcome.status == "skipped":
                errors.append(f"{provider.name}: {outcome.error}")
        return SearchOutcome(status="error", error="all providers failed: " + "; ".join(errors))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_search.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/services/search.py backend/tests/test_search.py
git commit -m "feat(search): add pluggable search providers with stability-first resolver"
```

---

### Task 2: `WebSearchTool`（搜索工具）

**Files:**
- Create: `backend/src/agent/tools/web_search.py`
- Test: `backend/tests/test_web_search_tool.py`

**Interfaces:**
- Consumes: `SearchService`（注入），`SearchOutcome`。
- Produces: 导出 `WebSearchTool` 类。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_web_search_tool.py`：
```python
from __future__ import annotations

import pytest

from agent.services.search import SearchHit, SearchOutcome, SearchService
from agent.tools.web_search import WebSearchTool


class _FakeSearchService:
    def __init__(self, outcome: SearchOutcome) -> None:
        self._outcome = outcome

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        return self._outcome


async def test_found_lists_results():
    ss = _FakeSearchService(SearchOutcome(
        hits=[SearchHit("T", "http://a", "snip")],
        status="found",
    ))
    tool = WebSearchTool(ss)
    res = await tool.run(query="x")
    assert res.ok
    assert "http://a" in res.content


async def test_empty_no_hallucination():
    ss = _FakeSearchService(SearchOutcome(status="empty"))
    tool = WebSearchTool(ss)
    res = await tool.run(query="nope")
    assert res.ok
    assert "未找到" in res.content


async def test_error_returns_online_failure():
    ss = _FakeSearchService(SearchOutcome(status="error", error="boom"))
    tool = WebSearchTool(ss)
    res = await tool.run(query="x")
    assert not res.ok
    assert "无法联网" in res.error


async def test_topk_capped():
    ss = _FakeSearchService(SearchOutcome(status="found", hits=[SearchHit("t", "u", "s")]))
    tool = WebSearchTool(ss)
    await tool.run(query="x", top_k=999)
    assert tool.last_top_k == 20
```

> 注：本测试使用项目配置的 `asyncio_mode = "auto"`（pytest-asyncio），不引入 pytest-anyio 的 `anyio_backend` fixture。`last_top_k` 记录在 tool 实例上。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_web_search_tool.py -v`
Expected: FAIL（`ModuleNotFoundError: agent.tools.web_search`）

- [ ] **Step 3: 实现 `web_search.py`**

```python
"""web_search tool: query a pluggable search service (stability-first)."""

from __future__ import annotations

from typing import Any

from agent.tools.base import Tool, ToolResult


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网搜索，返回标题、链接、摘要列表。"
        "调用时机：问题需要实时/外部资料。"
        "query 必填，top_k 可选（默认 5，上限 20）。"
        "若搜索不可用会明确说明，不会编造结果。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词"},
            "top_k": {"type": "integer", "description": "返回条数，默认 5，上限 20"},
        },
        "required": ["query"],
    }
    is_concurrency_safe = True
    timeout_ms = 30_000

    def __init__(self, search_service=None) -> None:
        self.search_service = search_service

    async def run(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, error="query is required")
        top_k = int(kwargs.get("top_k", 5))
        top_k = max(1, min(top_k, 20))
        self.last_top_k = top_k
        if self.search_service is None:
            return ToolResult(ok=False, error="搜索服务未配置")
        outcome = await self.search_service.search(query, top_k)
        if outcome.status == "error":
            return ToolResult(ok=False, error=f"无法联网搜索：{outcome.error}")
        if outcome.status == "empty" or not outcome.hits:
            return ToolResult(ok=True, content="未找到相关结果")
        lines = [
            f"- {h.title} {h.url}" + (f" — {h.snippet}" if h.snippet else "")
            for h in outcome.hits
        ]
        return ToolResult(ok=True, content="\n".join(lines))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_web_search_tool.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/tools/web_search.py backend/tests/test_web_search_tool.py
git commit -m "feat(search): add web_search tool with found/empty/error tri-state"
```

---

### Task 3: `WebFetchTool`（读取网页正文）

**Files:**
- Create: `backend/src/agent/tools/web_fetch.py`
- Test: `backend/tests/test_web_fetch_tool.py`

**Interfaces:**
- Produces: 导出 `WebFetchTool`。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_web_fetch_tool.py`：
```python
from __future__ import annotations

from agent.tools.web_fetch import WebFetchTool, trim_to_budget


def test_trim_preserves_head_and_tail():
    text = "A" * 30000
    out = trim_to_budget(text, 15000)
    assert len(out) <= 15000
    assert out.startswith("AAA")
    assert out.endswith("AAA")


async def test_fetch_failure_returns_error_not_hallucination():
    tool = WebFetchTool()

    async def _fake(url: str) -> tuple[int, str]:
        return (500, "<html></html>")

    tool.fetch_html = _fake
    res = await tool.run(url="http://example.invalid")
    assert not res.ok
    assert "无法读取" in res.error
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_web_fetch_tool.py -v`
Expected: FAIL（`ModuleNotFoundError: agent.tools.web_fetch`）

- [ ] **Step 3: 实现 `web_fetch.py`**

```python
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
    half = budget // 2
    return text[:half] + "\n…[有省略]…\n" + text[-half:]


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

    def __init__(self) -> None:
        self.fetch_html = _fetch

    async def run(self, **kwargs: Any) -> ToolResult:
        url = str(kwargs.get("url", "")).strip()
        if not url:
            return ToolResult(ok=False, error="url is required")
        if not url.startswith(("http://", "https://")):
            return ToolResult(ok=False, error="url must be http(s)")
        code, html = await self.fetch_html(url)
        if code != 200:
            return ToolResult(ok=False, error=f"无法读取正文（HTTP {code}）")
        low = html.lower()
        if any(f in low for f in BAD_HTML_FLAGS):
            return ToolResult(ok=False, error="该页面需要登录或验证，无法读取正文")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "aside"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        if not text:
            return ToolResult(ok=False, error="该页面无正文可提取")
        budget = int(kwargs.get("max_chars", 15000))
        budget = max(1000, min(budget, 40000))
        return ToolResult(ok=True, content=trim_to_budget(text, budget))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_web_fetch_tool.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/tools/web_fetch.py backend/tests/test_web_fetch_tool.py
git commit -m "feat(search): add web_fetch tool to extract clean main text"
```

---

### Task 4: `GET/PUT /api/settings/search` 接口

**Files:**
- Modify: `backend/src/agent/api/server.py`（在 `/api/settings/memory` 附近添加）
- Test: `backend/tests/test_settings_search_api.py`

**Interfaces:**
- Consumes: `ctx.settings_store`。
- Produces: `GET /api/settings/search -> {searxng_url, bocha_has_key, top_k_default, max_fetch_chars}`；`PUT /api/settings/search` 接收 `{searxng_url?, bocha_api_key?, top_k_default?, max_fetch_chars?}`。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_settings_search_api.py`：
```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring


@pytest.fixture()
def client(db_conn, settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_get_missing_uses_defaults(client):
    r = client.get("/api/settings/search")
    assert r.status_code == 200
    body = r.json()
    assert "bocha_has_key" in body
    assert body["bocha_has_key"] is False
    assert "searxng_url" in body


def test_put_and_get_roundtrip(client):
    r = client.put("/api/settings/search", json={
        "searxng_url": "http://127.0.0.1:8080",
        "bocha_api_key": "bk-abc",
        "top_k_default": 8,
        "max_fetch_chars": 20000,
    })
    assert r.status_code == 200
    body = client.get("/api/settings/search").json()
    assert body["searxng_url"] == "http://127.0.0.1:8080"
    assert body["bocha_has_key"] is True
    assert "bk-abc" not in str(body)
    assert body["top_k_default"] == 8
    assert body["max_fetch_chars"] == 20000


def test_put_clears_key_when_empty(client):
    client.put("/api/settings/search", json={"bocha_api_key": "bk-1"})
    client.put("/api/settings/search", json={"bocha_api_key": ""})
    body = client.get("/api/settings/search").json()
    assert body["bocha_has_key"] is False
```

> 注：`client` fixture 需在 `test_settings_search_api.py` 内本地定义（`test_api_routes.py` 的 `client` 是模块级 fixture，不会自动作用于其他测试文件）。复用 `db_conn` + `settings` + `create_app` + `MemoryKeyring` 的既有写法。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_settings_search_api.py -v`
Expected: FAIL（路由不存在 → 404）

- [ ] **Step 3: 实现接口（server.py）**

在 `/api/settings/memory` 之后添加：
```python
    @app.get("/api/settings/search")
    async def get_search_settings() -> dict:
        store = ctx.settings_store
        bocha_key = store.get("search.bocha_api_key", "")
        return {
            "searxng_url": store.get("search.searxng_url", "") or "",
            "bocha_has_key": bool(bocha_key),
            "top_k_default": store.get_int("search.top_k_default", 5),
            "max_fetch_chars": store.get_int("search.max_fetch_chars", 15000),
        }

    @app.put("/api/settings/search")
    async def update_search_settings(body: dict) -> dict:
        store = ctx.settings_store
        if "searxng_url" in body:
            store.set("search.searxng_url", str(body.get("searxng_url") or ""))
        if "bocha_api_key" in body:
            store.set("search.bocha_api_key", str(body.get("bocha_api_key") or ""))
        if "top_k_default" in body:
            try:
                v = int(body["top_k_default"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="top_k_default must be an integer")
            store.set("search.top_k_default", str(max(1, min(v, 20))))
        if "max_fetch_chars" in body:
            try:
                v = int(body["max_fetch_chars"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_fetch_chars must be an integer")
            store.set("search.max_fetch_chars", str(max(1000, min(v, 40000))))
        return await get_search_settings()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_settings_search_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/api/server.py backend/tests/test_settings_search_api.py
git commit -m "feat(search): add GET/PUT /api/settings/search"
```

---

### Task 5: 接入 `AppContext`（实例化服务 + 挂载工具 + CORE_TOOLS）

**Files:**
- Modify: `backend/src/agent/services/app.py`
- Modify: `backend/src/agent/services/tool_router.py`（`CORE_TOOLS` 加 `web_search`）

**Interfaces:**
- Produces: `AppContext.search_service`（`SearchService`）。

- [ ] **Step 1: 在 `app.py` 构建 `SearchService` 并注册服务**

在 `self.services.register("embedding", self.embedding)` 之后添加：
```python
        from agent.services.search import SearchService
        from agent.tools.web_search import WebSearchTool
        from agent.tools.web_fetch import WebFetchTool

        _store = self.settings_store
        self.search_service = SearchService(
            searxng_url=_store.get("search.searxng_url") or None,
            bocha_api_key=_store.get("search.bocha_api_key") or None,
        )
        self.services.register("search_service", self.search_service)
        self.registry.register(WebSearchTool(self.search_service))
        self.registry.register(WebFetchTool())
```

- [ ] **Step 2: 在 `tool_router.py` 的 `CORE_TOOLS` 加 `web_search`**

```python
CORE_TOOLS = [
    "memory_search",
    "switch_topic",
    "create_topic",
    "await_task",
    "read_task_result",
    "web_search",
]
```

- [ ] **Step 3: 运行现有后端测试，确认未回归**

Run: `cd backend && $env:PYTHONPATH="src"; pytest -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/src/agent/services/app.py backend/src/agent/services/tool_router.py
git commit -m "feat(search): wire SearchService + register web_search/web_fetch + CORE_TOOLS"
```

---

### Task 6: 前端 API 客户端

**Files:**
- Modify: `frontend/src/services/api.ts`

**Interfaces:**
- Produces: `getSearchSettings(): Promise<SearchSettings>`；`updateSearchSettings(body): Promise<SearchSettings>`。

```ts
export interface SearchSettings {
  searxng_url: string;
  bocha_has_key: boolean;
  top_k_default: number;
  max_fetch_chars: number;
}
```

- [ ] **Step 1: 添加 `api.searchSettings` / `api.updateSearchSettings`**

在 `api.ts` 的 `api` 对象里追加（复用现有 `request` 帮助函数）：
```ts
  getSearchSettings: () =>
    request<SearchSettings>("/api/settings/search"),
  updateSearchSettings: (body: Record<string, unknown>) =>
    request<SearchSettings>("/api/settings/search", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
```

`SearchSettings` 接口放在 `api.ts` 顶部与 `CredentialMeta` 等并列。

- [ ] **Step 2: 跑类型检查**

Run: `cd frontend && node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`
Expected: PASS（无新增错误）

- [ ] **Step 3: Commit**

```bash
git add frontend/src/services/api.ts
git commit -m "feat(search): add search settings api client"
```

---

### Task 7: 前端「联网搜索」配置卡片

**Files:**
- Modify: `frontend/src/views/SettingsView.vue`
- Test: `frontend/src/views/__tests__/SettingsView.test.ts`

**Interfaces:**
- Consumes: `api.getSearchSettings` / `api.updateSearchSettings`。

- [ ] **Step 1: 在 pref tab 添加「联网搜索」卡片**

在 `SettingsView.vue` 的 `pref` 面板内新增一个 `section.sec`，含：
- 默认返回条数（`QNumber`，绑定 `searchTopK`）。
- 读正文字符预算（`QNumber`，绑定 `searchMaxChars`）。
- 博查 API key（密码输入，显示「已配置」状态，不回显明文）。
- SearXNG 实例 URL（文本输入，可选）。
- 保存按钮，保存后 `settingsNotice` 显示「已保存搜索配置」。

样式用 `var(--*)`，标题用 `--serif`，数值输入用 `--mono`。

- [ ] **Step 2: 更新测试`SettingsView.test.ts`**

新增用例：加载时渲染搜索卡片；保存后显示成功 notice。mock `api.getSearchSettings` / `api.updateSearchSettings`。

- [ ] **Step 3: 跑前端测试与类型检查**

Run: `cd frontend && node node_modules/vitest/vitest.mjs run && node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add frontend/src/views/SettingsView.vue frontend/src/views/__tests__/SettingsView.test.ts
git commit -m "feat(search): add search settings card to preferences tab"
```

---

### Task 8: 端到端验证

**Files:** 无新增。

- [ ] **Step 1: 运行后端全量测试**

Run: `cd backend && $env:PYTHONPATH="src"; pytest -q`
Expected: 全绿

- [ ] **Step 2: 运行前端全量测试 + 类型检查**

Run: `cd frontend && node node_modules/vitest/vitest.mjs run && node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`
Expected: 全绿

- [ ] **Step 3: 手动冒烟**

重启后端后打开 `http://127.0.0.1:5199/?fresh=1#/settings`：确认搜索卡片渲染、能保存；在对话中让 agent 调用 `web_search` 查询一个实时问题，确认返回结果；让 agent 调用 `web_fetch` 读一个正文。

- [ ] **Step 4: Commit（如有改动）**

```bash
git add -A
git commit -m "test(search): e2e verification for web search/fetch"
```
