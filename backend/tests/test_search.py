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


async def test_baidu_provider_sets_status_skipped_on_verify_page():
    # Baidu 返回安全验证页时应标记为该源失败（status='skipped'），不当作真实结果
    provider = BaiduProvider()

    async def fake(url: str) -> tuple[int, str]:
        return (200, "<html>百度安全验证</html>")

    provider._fetch = fake
    outcome = await provider.search("openai", top_k=5)
    assert outcome.status in ("skipped", "error")
    assert not outcome.hits


def test_resolver_prefers_bocha_when_key_present():
    resolver = SearchProviderResolver(bocha_api_key="bk-1")
    providers = resolver.providers()
    assert any(isinstance(p, BochaProvider) for p in providers)
    assert isinstance(providers[0], BochaProvider)


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


def test_search_hit_and_outcome_defaults():
    hit = SearchHit(title="t", url="u", snippet="s")
    assert hit.title == "t" and hit.url == "u" and hit.snippet == "s"
    outcome = SearchOutcome()
    assert outcome.status == "empty" and outcome.hits == [] and outcome.error is None
