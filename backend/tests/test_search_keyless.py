"""免密钥搜索通道：Exa / Parallel 的免费 MCP，以及 DuckDuckGo HTML 抓取。

全部打桩 HTTP（不联网）；真实可用性由 2026-09-12 的实测记录在 docs/status.md。
"""

from __future__ import annotations

import json

from agent.services.mcp_search import (
    ExaMcpProvider,
    McpHttpClient,
    ParallelMcpProvider,
    _parse_sse_or_json,
)
from agent.services.search import DuckDuckGoProvider, SearchProviderResolver, SearchService

EXA_SSE = (
    "event: message\n"
    'data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":'
    '"Title: 今日天气-上海市气象局\\nURL: http://sh.cma.gov.cn/sh/tqyb/jrtq/\\n'
    "Highlights:\\n今日天气 上海中心气象台\\n...\\n\\n---\\n\\n"
    'Title: 上海-天气预报 - 中央气象台\\nURL: https://www.nmc.cn/publish/forecast/ASH/shanghai.html\\n'
    'Highlights:\\n上海 24小时预报 7天预报 29℃\\n"}]}}\n\n'
)

PARALLEL_JSON = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "_meta": {"parallel/usage": [{"name": "sku_search", "count": 1}]},
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "search_id": "search_x",
                            "results": [
                                {
                                    "url": "https://weather.com/shanghai",
                                    "title": "Weather for Shanghai",
                                    "publish_date": "2026-09-12",
                                    "excerpts": ["Today 2026-09-12: Mostly clear."],
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        },
    }
)

DDG_HTML = """
<html><body>
  <div class="result">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.weather.com.cn%2Fweather%2F101020100.shtml&amp;rut=x">
      上海天气预报_上海7天天气预报</a>
    <a class="result__snippet">上海 天气 预报 今天 23℃ 晴</a>
  </div>
  <div class="result">
    <a rel="nofollow" class="result__a" href="https://www.nmc.cn/publish/forecast/ASH/shanghai.html">
      上海-天气预报 - 中央气象台</a>
    <a class="result__snippet">上海 24小时预报 7天预报</a>
  </div>
</body></html>
"""

DDG_CHALLENGE = "<html><body>Unfortunately, bots use DuckDuckGo too. Please complete the following challenge.</body></html>"


def test_parse_sse_or_json_handles_both_framings():
    assert _parse_sse_or_json(EXA_SSE)["result"]["content"][0]["type"] == "text"
    assert _parse_sse_or_json(PARALLEL_JSON)["result"]["_meta"]
    assert _parse_sse_or_json("not json at all") is None


def _mcp_client(payload: str, *, status: int = 200, capture: list | None = None):
    client = McpHttpClient("https://mcp.example/mcp")

    async def fake_post(body: dict):
        if capture is not None:
            capture.append(body)
        return status, payload

    client._post = fake_post
    return client


async def test_exa_provider_maps_query_and_parses_hits():
    sent: list[dict] = []
    provider = ExaMcpProvider(client=_mcp_client(EXA_SSE, capture=sent))

    outcome = await provider.search("上海 天气", top_k=3)

    assert outcome.status == "found"
    assert [h.title for h in outcome.hits] == [
        "今日天气-上海市气象局",
        "上海-天气预报 - 中央气象台",
    ]
    assert outcome.hits[0].url == "http://sh.cma.gov.cn/sh/tqyb/jrtq/"
    assert "上海中心气象台" in outcome.hits[0].snippet
    body = sent[0]
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "web_search_exa"
    assert body["params"]["arguments"]["query"] == "上海 天气"
    assert body["params"]["arguments"]["objective"]
    assert body["params"]["arguments"]["numResults"] == 3


async def test_parallel_provider_maps_queries_and_parses_json_hits():
    sent: list[dict] = []
    provider = ParallelMcpProvider(client=_mcp_client(PARALLEL_JSON, capture=sent))

    outcome = await provider.search("上海 天气", top_k=3)

    assert outcome.status == "found"
    assert outcome.hits[0].title == "Weather for Shanghai"
    assert outcome.hits[0].url == "https://weather.com/shanghai"
    assert "Mostly clear" in outcome.hits[0].snippet
    args = sent[0]["params"]["arguments"]
    assert args["search_queries"] == ["上海 天气"]
    assert args["objective"]
    assert args["session_id"]  # 免费层限流用的会话 id（进程内随机，不持久化）
    assert "model_name" not in args  # 不做产品分析上报


async def test_mcp_rate_limit_is_skipped_in_chinese():
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "Free tier rate limit exceeded, try again later"},
        }
    )
    provider = ExaMcpProvider(client=_mcp_client(payload))
    outcome = await provider.search("上海 天气", top_k=3)
    assert outcome.status == "skipped"
    assert outcome.error and "限流" in outcome.error


async def test_mcp_error_is_reported_in_chinese():
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid arguments"}}
    )
    provider = ExaMcpProvider(client=_mcp_client(payload))
    outcome = await provider.search("上海 天气", top_k=3)
    assert outcome.status == "error"
    assert outcome.error and "Exa" in outcome.error and "不可用" in outcome.error


async def test_mcp_tool_error_is_reported():
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"isError": True, "content": [{"type": "text", "text": "boom"}]},
        }
    )
    provider = ExaMcpProvider(client=_mcp_client(payload))
    outcome = await provider.search("上海 天气", top_k=3)
    assert outcome.status == "error"
    assert outcome.error and "Exa" in outcome.error


async def test_ddg_provider_parses_and_unwraps_redirect_links():
    provider = DuckDuckGoProvider()

    async def fake_fetch(url: str):
        return 200, DDG_HTML

    provider._fetch = fake_fetch
    outcome = await provider.search("上海 天气", top_k=5)

    assert outcome.status == "found"
    assert [h.title for h in outcome.hits] == [
        "上海天气预报_上海7天天气预报",
        "上海-天气预报 - 中央气象台",
    ]
    # uddg= 重定向要还原成真实 URL
    assert outcome.hits[0].url == "https://www.weather.com.cn/weather/101020100.shtml"
    assert outcome.hits[1].url == "https://www.nmc.cn/publish/forecast/ASH/shanghai.html"


async def test_ddg_challenge_page_is_skipped_not_empty():
    provider = DuckDuckGoProvider()

    async def fake_fetch(url: str):
        return 202, DDG_CHALLENGE

    provider._fetch = fake_fetch
    outcome = await provider.search("上海 天气", top_k=5)
    assert outcome.status == "skipped"
    assert outcome.error and "DuckDuckGo" in outcome.error


async def test_ddg_http_429_is_skipped():
    provider = DuckDuckGoProvider()

    async def fake_fetch(url: str):
        return 429, "too many requests"

    provider._fetch = fake_fetch
    outcome = await provider.search("上海 天气", top_k=5)
    assert outcome.status == "skipped"


async def test_ddg_unrelated_results_are_gated():
    provider = DuckDuckGoProvider()

    async def fake_fetch(url: str):
        return 200, (
            '<html><div class="result"><a class="result__a" href="https://x.example/a">'
            "Netflix - 知乎</a><a class=\"result__snippet\">热门话题</a></div></html>"
        )

    provider._fetch = fake_fetch
    outcome = await provider.search("上海 天气", top_k=5)
    assert outcome.status == "skipped"
    assert outcome.hits == []


def test_provider_order_keyless_before_bing_and_baidu():
    names = [p.name for p in SearchProviderResolver().providers()]
    assert names == ["exa", "parallel", "duckduckgo", "bing", "baidu"]

    with_self_hosted = [
        p.name
        for p in SearchProviderResolver(
            searxng_url="https://searx.example", bocha_api_key="k"
        ).providers()
    ]
    assert with_self_hosted == ["bocha", "searxng", "exa", "parallel", "duckduckgo", "bing", "baidu"]

    keyless_off = [p.name for p in SearchProviderResolver(keyless=False).providers()]
    assert keyless_off == ["bing", "baidu"]


async def test_keyless_flag_off_never_calls_keyless_providers():
    calls: list[str] = []

    async def bing_fetch(url: str):
        calls.append("bing")
        return 200, "<html><title>x</title><div class='b_no'>没有找到</div></html>"

    svc = SearchService()
    svc.set_config(searxng_url=None, bocha_api_key=None, keyless_fallback=False)
    providers = svc._resolver.providers()
    assert [p.name for p in providers] == ["bing", "baidu"]
    for p in providers:
        p._fetch = bing_fetch
    svc._resolver.providers = lambda: providers  # type: ignore[assignment]

    await svc.search("上海 天气", top_k=3)
    assert "exa" not in calls and "duckduckgo" not in calls


async def test_service_prefers_keyless_mcp_when_available():
    svc = SearchService()
    providers = svc._resolver.providers()
    assert providers[0].name == "exa"
    # 全部打桩：测试绝不能真的联网（免密钥通道会发真实请求）
    for provider in providers:
        provider.client = _mcp_client(EXA_SSE)  # type: ignore[attr-defined]
    svc._resolver.providers = lambda: providers  # type: ignore[assignment]

    outcome = await svc.search("上海 天气", top_k=3)
    assert outcome.status == "found"
    assert outcome.hits[0].title == "今日天气-上海市气象局"
