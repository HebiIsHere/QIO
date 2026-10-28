"""搜索通道状态 / 真实连接检查 / 参数接线（设置页改版 §9）。

三条硬要求，每条都有用例：
1. 「已配置」≠「检查通过」：没检查过 `usable is None`，界面必须显示「未检查」；
2. 免密钥通道不是总开关：关掉它不清除博查 Key / SearXNG 地址，也不改其余通道顺序；
3. 参数（返回条数 / 正文预算）**每次调用时**从设置读，改完立刻生效。

全部打桩 HTTP（`agent.services.search._http_request` / provider 的 `search`），不联网。
"""

from __future__ import annotations

import httpx
import pytest

from agent.services import search as search_module
from agent.services.search import (
    REASON_BLOCKED,
    REASON_EMPTY,
    REASON_INVALID_KEY,
    REASON_JSON_DISABLED,
    REASON_OK,
    REASON_RATE_LIMIT,
    REASON_TIMEOUT,
    REASON_UNREACHABLE,
    ChannelProbe,
    SearchService,
)
from agent.storage.settings import SettingsStore
from agent.tools.web_fetch import WebFetchTool
from agent.tools.web_search import WebSearchTool


@pytest.fixture()
def settings_store(db_conn) -> SettingsStore:
    return SettingsStore(db_conn)


@pytest.fixture()
def service() -> SearchService:
    return SearchService()


def _row(rows: list[dict], channel_id: str) -> dict:
    return next(row for row in rows if row["id"] == channel_id)


# -- 已配置 vs 检查通过 --------------------------------------------------------


def test_configured_is_not_verified(service):
    """只填了 Key 不等于可用：没检查过就是「未检查」。"""
    service.set_config(bocha_api_key="bk-test", searxng_url="http://127.0.0.1:8080")
    rows = service.channel_status()

    bocha = _row(rows, "bocha")
    assert bocha["configured"] is True
    assert bocha["usable"] is None
    assert bocha["checked_at"] is None
    assert bocha["check_state"] == "unchecked"

    searxng = _row(rows, "searxng")
    assert searxng["configured"] is True and searxng["usable"] is None

    # 未配置的通道同样不能显示成可用
    bare = SearchService().channel_status()
    assert _row(bare, "bocha")["configured"] is False
    assert _row(bare, "bocha")["usable"] is None


def test_status_carries_no_secret(service):
    service.set_config(bocha_api_key="bk-super-secret")
    payload = str(service.channel_status())
    assert "bk-super-secret" not in payload


async def test_check_marks_channel_usable_after_real_probe(service, monkeypatch):
    service.set_config(bocha_api_key="bk-test")

    async def fake_http(url, *, headers=None, payload=None, timeout=None):  # noqa: ANN001
        return 200, '{"data": {"webPages": {"value": []}}}'

    monkeypatch.setattr(search_module, "_http_request", fake_http)
    rows = await service.check_channels(["bocha"])
    bocha = _row(rows, "bocha")
    assert bocha["usable"] is True
    assert bocha["check_state"] == "passed"
    assert bocha["reason_code"] == REASON_OK
    assert bocha["checked_at"]


@pytest.mark.parametrize(
    "status, expected",
    [(401, REASON_INVALID_KEY), (403, REASON_INVALID_KEY), (429, REASON_RATE_LIMIT), (500, "http_error")],
)
async def test_check_classifies_http_failures(service, monkeypatch, status, expected):
    service.set_config(bocha_api_key="bk-test")

    async def fake_http(url, *, headers=None, payload=None, timeout=None):  # noqa: ANN001
        return status, "{}"

    monkeypatch.setattr(search_module, "_http_request", fake_http)
    rows = await service.check_channels(["bocha"])
    bocha = _row(rows, "bocha")
    assert bocha["usable"] is False
    assert bocha["reason_code"] == expected


async def test_check_classifies_network_and_timeout(service, monkeypatch):
    service.set_config(bocha_api_key="bk-test")

    async def timeout_http(url, *, headers=None, payload=None, timeout=None):  # noqa: ANN001
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(search_module, "_http_request", timeout_http)
    rows = await service.check_channels(["bocha"])
    assert _row(rows, "bocha")["reason_code"] == REASON_TIMEOUT

    async def unreachable_http(url, *, headers=None, payload=None, timeout=None):  # noqa: ANN001
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(search_module, "_http_request", unreachable_http)
    rows = await service.check_channels(["bocha"])
    assert _row(rows, "bocha")["reason_code"] == REASON_UNREACHABLE


async def test_searxng_without_json_mode_is_reported(service, monkeypatch):
    service.set_config(searxng_url="http://127.0.0.1:8080")

    async def fake_http(url, *, headers=None, payload=None, timeout=None):  # noqa: ANN001
        return 200, "<html>plain search page</html>"

    monkeypatch.setattr(search_module, "_http_request", fake_http)
    rows = await service.check_channels(["searxng"])
    searxng = _row(rows, "searxng")
    assert searxng["usable"] is False
    assert searxng["reason_code"] == REASON_JSON_DISABLED


async def test_unconfigured_channel_is_not_checked_over_network(service, monkeypatch):
    calls: list[str] = []

    async def fake_http(url, *, headers=None, payload=None, timeout=None):  # noqa: ANN001
        calls.append(url)
        return 200, "{}"

    monkeypatch.setattr(search_module, "_http_request", fake_http)
    rows = await service.check_channels(["bocha"])
    assert calls == []
    bocha = _row(rows, "bocha")
    assert bocha["usable"] is False
    assert bocha["reason_code"] == "not_configured"


async def test_scraper_blocked_page_is_reported_as_blocked(service, monkeypatch):
    from agent.services.search import SearchOutcome

    async def fake_search(self, query, top_k=1):  # noqa: ANN001
        return SearchOutcome(status="skipped", error="必应要求人机验证（反爬），已跳过")

    monkeypatch.setattr(search_module.BingProvider, "search", fake_search)
    rows = await service.check_channels(["bing"])
    bing = _row(rows, "bing")
    assert bing["usable"] is False
    assert bing["reason_code"] == REASON_BLOCKED


async def test_scraper_empty_result_still_counts_as_reachable(service, monkeypatch):
    from agent.services.search import SearchOutcome

    async def fake_search(self, query, top_k=1):  # noqa: ANN001
        return SearchOutcome(status="empty")

    monkeypatch.setattr(search_module.BaiduProvider, "search", fake_search)
    rows = await service.check_channels(["baidu"])
    baidu = _row(rows, "baidu")
    assert baidu["usable"] is True
    assert baidu["reason_code"] == REASON_EMPTY


# -- 免密钥通道不是总开关 ------------------------------------------------------


def test_keyless_toggle_keeps_other_search_config():
    """关掉免密钥通道：不清除博查 Key / SearXNG 地址（设置里那些值原样保留）。"""
    service = SearchService(
        searxng_url="http://127.0.0.1:8080", bocha_api_key="bk-test", keyless_fallback=True
    )
    assert service._resolver.bocha_api_key == "bk-test"
    assert service._resolver.searxng_url == "http://127.0.0.1:8080"

    service.set_config(
        searxng_url=service._resolver.searxng_url,
        bocha_api_key=service._resolver.bocha_api_key,
        keyless_fallback=False,
    )

    assert service._resolver.bocha_api_key == "bk-test"
    assert service._resolver.searxng_url == "http://127.0.0.1:8080"
    # 只有免密钥那三个通道退出回退链，其余顺序不变
    assert service.chain_ids() == ["bocha", "searxng", "bing", "baidu"]


def test_keyless_off_does_not_change_relative_order():
    on = SearchService(searxng_url="http://x", bocha_api_key="k").chain_ids()
    off = SearchService(
        searxng_url="http://x", bocha_api_key="k", keyless_fallback=False
    ).chain_ids()
    assert on == ["bocha", "searxng", "exa", "parallel", "duckduckgo", "bing", "baidu"]
    assert off == [name for name in on if name not in ("exa", "parallel", "duckduckgo")]


def test_toggling_keyless_does_not_invalidate_bocha_check(service, monkeypatch):
    """检查结论按通道配置指纹失效：关免密钥不影响博查「检查通过」的结论。"""
    import asyncio

    service.set_config(bocha_api_key="bk-test", keyless_fallback=True)

    async def fake_http(url, *, headers=None, payload=None, timeout=None):  # noqa: ANN001
        return 200, "{}"

    monkeypatch.setattr(search_module, "_http_request", fake_http)
    asyncio.run(service.check_channels(["bocha"]))
    assert _row(service.channel_status(), "bocha")["usable"] is True

    service.set_config(
        bocha_api_key="bk-test", searxng_url=None, keyless_fallback=False
    )
    bocha = _row(service.channel_status(), "bocha")
    assert bocha["usable"] is True, "换掉免密钥开关不该抹掉博查的检查结论"

    # 但博查 Key 变了，旧结论必须作废（不能再显示成验证过）
    service.set_config(bocha_api_key="bk-other", keyless_fallback=False)
    assert _row(service.channel_status(), "bocha")["usable"] is None


def test_chain_positions_are_exposed(service):
    rows = service.channel_status()
    positions = {row["id"]: row["position"] for row in rows}
    assert positions["exa"] == 1
    assert positions["bing"] == 4
    assert _row(rows, "bocha")["position"] is None  # 没配 Key：不在链里


# -- 参数接线：每次调用都读设置 -------------------------------------------------


class _RecordingSearchService:
    def __init__(self) -> None:
        self.last_top_k: int | None = None

    async def search(self, query: str, top_k: int = 5):  # noqa: ANN001
        from agent.services.search import SearchHit, SearchOutcome

        self.last_top_k = top_k
        return SearchOutcome(hits=[SearchHit("t", "u", "s")], status="found")


async def test_web_search_reads_top_k_setting_at_call_time(settings_store):
    """改设置后工具真的用了新值（不是 import 期固化成默认 5）。"""
    import asyncio

    recorder = _RecordingSearchService()
    tool = WebSearchTool(recorder, settings_store)

    await tool.run(query="x")
    assert recorder.last_top_k == 5  # 缺省仍是基线 5

    settings_store.set("search.top_k_default", "8")
    await tool.run(query="x")
    assert recorder.last_top_k == 8

    settings_store.set("search.top_k_default", "11")
    await tool.run(query="x")
    assert recorder.last_top_k == 11

    # 显式给 top_k 时以调用参数为准，仍然受 1..20 收敛
    await tool.run(query="x", top_k=3)
    assert recorder.last_top_k == 3
    await tool.run(query="x", top_k=999)
    assert recorder.last_top_k == 20


async def test_web_search_clamps_out_of_range_setting(settings_store):
    recorder = _RecordingSearchService()
    tool = WebSearchTool(recorder, settings_store)
    settings_store.set("search.top_k_default", "0")
    await tool.run(query="x")
    assert recorder.last_top_k == 1
    settings_store.set("search.top_k_default", "1000")
    await tool.run(query="x")
    assert recorder.last_top_k == 20


async def test_web_fetch_reads_max_chars_setting_at_call_time(settings_store):
    long_text = "正文" * 5000  # 10000 个字符

    async def fake_fetch(url: str):
        return 200, f"<html><body><p>{long_text}</p></body></html>"

    tool = WebFetchTool(fetch_html=fake_fetch, settings=settings_store)

    settings_store.set("search.max_fetch_chars", "1000")
    smallest = await tool.run(url="https://example.com/a")
    assert smallest.ok
    assert len(smallest.content) <= 1000

    settings_store.set("search.max_fetch_chars", "40000")
    largest = await tool.run(url="https://example.com/a")
    assert largest.ok
    assert len(largest.content) > 2000  # 预算放宽后不再是 1000 那个截断

    # 显式 max_chars 优先，但同样受范围收敛
    explicit = await tool.run(url="https://example.com/a", max_chars=1200)
    assert len(explicit.content) <= 1200
