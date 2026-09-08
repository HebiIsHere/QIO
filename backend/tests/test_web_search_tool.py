from __future__ import annotations

from agent.services.search import SearchHit, SearchOutcome
from agent.tools.web_search import WebSearchTool


class _FakeSearchService:
    def __init__(self, outcome: SearchOutcome) -> None:
        self._outcome = outcome
        self.last_top_k: int | None = None

    async def search(self, query: str, top_k: int = 5) -> SearchOutcome:
        self.last_top_k = top_k
        return self._outcome


async def test_found_lists_results():
    ss = _FakeSearchService(
        SearchOutcome(
            hits=[SearchHit("T", "http://a", "snip")],
            status="found",
        )
    )
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
    ss = _FakeSearchService(
        SearchOutcome(status="found", hits=[SearchHit("t", "u", "s")])
    )
    tool = WebSearchTool(ss)
    await tool.run(query="x", top_k=999)
    assert ss.last_top_k == 20


async def test_missing_query_is_rejected():
    tool = WebSearchTool(_FakeSearchService(SearchOutcome(status="empty")))
    res = await tool.run(query="")
    assert not res.ok


async def test_no_service_reports_unconfigured():
    tool = WebSearchTool(None)
    res = await tool.run(query="x")
    assert not res.ok
    assert "未配置" in res.error
