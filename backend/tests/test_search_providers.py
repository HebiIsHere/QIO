"""联网搜索通道：Bing 入口 / Bing 兜底 / 百度反爬冷却（不联网，全部打桩 _fetch）。"""

from __future__ import annotations

from agent.services.search import (
    BaiduProvider,
    BingProvider,
    SearchProviderResolver,
    SearchService,
)

BING_HTML_WITH_RESULTS = """
<html><body><ol id="b_results">
  <li class="b_algo">
    <a class="tilk" href="https://example.com/cite">example.com https://example.com › 引用</a>
    <h2><a href="https://example.com/a">QIO 记忆系统设计（甲）</a></h2>
    <p>QIO 记忆系统的分块与检索说明</p>
  </li>
  <li class="b_algo">
    <a class="tilk" href="https://example.com/cite2">example.com https://example.com › 引用</a>
    <h2><a href="https://example.com/b">QIO 记忆系统实现（乙）</a></h2>
    <p>QIO 记忆系统实现细节</p>
  </li>
</ol></body></html>
"""

# 风控诱饵页：有 li.b_algo 容器，但内容与 query 完全无关
BING_UNRELATED_RESULTS = """
<html><body><ol id="b_results">
  <li class="b_algo"><h2><a href="https://example.com/x">Netflix - 知乎</a></h2><p>热门话题</p></li>
  <li class="b_algo"><h2><a href="https://example.com/y">DonanımHaber Forum</a></h2><p>forum</p></li>
</ol></body></html>
"""

BING_HOMEPAGE = (
    "<html><head><title>搜索 - Microsoft 必应</title></head>"
    "<body><form id='sb_form'></form></body></html>"
)

BING_NO_RESULTS = (
    "<html><head><title>QIO - 搜索</title></head><body>"
    "<div class='b_no'>没有找到与 QIO 相关的结果</div></body></html>"
)

BING_RSS = """<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0"><channel>
  <item><title>QIO 记忆系统 RSS 甲</title><link>https://example.com/rss-a</link><description>QIO 记忆系统摘要甲</description></item>
  <item><title>QIO 记忆系统 RSS 乙</title><link>https://example.com/rss-b</link><description>QIO 记忆系统摘要乙</description></item>
</channel></rss>
"""

BAIDU_ANTIBOT = (
    "<html><head><title>百度安全验证</title></head>"
    "<body>请输入验证码完成安全验证</body></html>"
)


def _bing(fetch):
    provider = BingProvider()
    provider._fetch = fetch
    return provider


async def test_bing_uses_reachable_search_endpoint():
    """cn.bing.com/search 会被 302 到首页（实测 0 结果），必须用 www.bing.com/search。"""
    seen: list[str] = []

    async def fetch(url: str):
        seen.append(url)
        return 200, BING_HTML_WITH_RESULTS

    outcome = await _bing(fetch).search("QIO 记忆", top_k=5)

    assert outcome.status == "found"
    assert [h.title for h in outcome.hits] == ["QIO 记忆系统设计（甲）", "QIO 记忆系统实现（乙）"]
    # 标题/链接必须来自 h2 里的结果锚点，而不是站点引用锚点（a.tilk）
    assert outcome.hits[0].url == "https://example.com/a"
    assert len(seen) == 1
    assert "www.bing.com/search" in seen[0]
    assert "cn.bing.com/search" not in seen[0]
    assert "mkt=zh-CN" in seen[0]


async def test_bing_falls_back_to_rss_when_html_has_no_results():
    seen: list[str] = []

    async def fetch(url: str):
        seen.append(url)
        if "format=rss" in url:
            return 200, BING_RSS
        return 200, BING_HOMEPAGE

    outcome = await _bing(fetch).search("QIO 记忆", top_k=5)

    assert outcome.status == "found"
    assert [h.title for h in outcome.hits] == ["QIO 记忆系统 RSS 甲", "QIO 记忆系统 RSS 乙"]
    assert outcome.hits[0].url == "https://example.com/rss-a"
    assert len(seen) == 2 and "format=rss" in seen[1]


async def test_bing_genuine_empty_stays_empty():
    async def fetch(url: str):
        return 200, BING_NO_RESULTS

    outcome = await _bing(fetch).search("找不到的东西", top_k=5)
    assert outcome.status == "empty"
    assert outcome.error is None


async def test_bing_unrelated_results_are_treated_as_soft_block():
    """实测：必应风控时会返回「有容器但内容与 query 无关」的诱饵页。

    这种结果绝不能当成搜索结果交给模型；要按通道异常跳过（并冷却）。
    """

    async def fetch(url: str):
        return 200, BING_UNRELATED_RESULTS

    outcome = await _bing(fetch).search("QIO 记忆系统设计", top_k=5)
    assert outcome.status == "skipped"
    assert outcome.hits == []
    assert outcome.error and "无关" in outcome.error


async def test_bing_unexpected_page_is_error_not_empty():
    """被跳转到首页 / 改版页面时不能谎报「没有结果」，要让调用方知道通道坏了。"""

    async def fetch(url: str):
        return 200, BING_HOMEPAGE

    outcome = await _bing(fetch).search("QIO", top_k=5)
    assert outcome.status == "error"
    assert outcome.error and "结果页" in outcome.error


async def test_bing_anti_bot_page_is_skipped_in_chinese():
    async def fetch(url: str):
        return 200, "<html><body>百度安全验证</body></html>"

    outcome = await _bing(fetch).search("QIO", top_k=5)
    assert outcome.status == "skipped"
    assert outcome.error and "必应" in outcome.error
    assert "anti-bot" not in outcome.error


def test_provider_order_self_hosted_before_keyless_before_scrapers():
    names = [
        p.name
        for p in SearchProviderResolver(
            searxng_url="https://searx.example", bocha_api_key=None
        ).providers()
    ]
    # 顺序：自建实例（用户显式配置）→ 免密钥 MCP/DDG → 抓取类（必应/百度）兜底
    assert names == ["searxng", "exa", "parallel", "duckduckgo", "bing", "baidu"]


async def test_search_service_success_never_touches_baidu():
    calls: list[str] = []

    async def bing_fetch(url: str):
        calls.append("bing")
        return 200, BING_HTML_WITH_RESULTS

    async def baidu_fetch(url: str):
        calls.append("baidu")
        return 200, BAIDU_ANTIBOT

    # 关掉免密钥通道，专门验证「抓取链里必应成功就不碰百度」
    svc = SearchService()
    svc.set_config(searxng_url=None, bocha_api_key=None, keyless_fallback=False)
    providers = svc._resolver.providers()
    providers[0]._fetch = bing_fetch
    providers[-1]._fetch = baidu_fetch
    svc._resolver.providers = lambda: providers  # type: ignore[assignment]

    outcome = await svc.search("QIO", top_k=3)
    assert outcome.status == "found"
    assert calls == ["bing"]


async def test_anti_bot_provider_is_cooled_down_and_error_is_chinese():
    """百度反爬后不应被反复重试；错误信息要是中文、可读。"""
    calls: list[str] = []

    async def bing_fetch(url: str):
        calls.append("bing")
        return 200, BING_HOMEPAGE  # 通道坏了（被跳转）

    async def baidu_fetch(url: str):
        calls.append("baidu")
        return 200, BAIDU_ANTIBOT

    svc = SearchService()
    svc.set_config(searxng_url=None, bocha_api_key=None, keyless_fallback=False)
    providers = svc._resolver.providers()
    providers[0]._fetch = bing_fetch
    providers[-1]._fetch = baidu_fetch
    svc._resolver.providers = lambda: providers  # type: ignore[assignment]

    first = await svc.search("QIO", top_k=3)
    second = await svc.search("QIO 再次", top_k=3)

    assert first.status == "error" and second.status == "error"
    assert calls.count("baidu") == 1, "反爬后必须冷却，不能每次搜索都再撞一次百度"
    for outcome in (first, second):
        assert outcome.error
        assert "百度" in outcome.error
        assert "all providers failed" not in outcome.error
        assert "anti-bot" not in outcome.error


async def test_baidu_provider_message_is_chinese():
    async def fetch(url: str):
        return 200, BAIDU_ANTIBOT

    provider = BaiduProvider()
    provider._fetch = fetch
    outcome = await provider.search("QIO", top_k=3)
    assert outcome.status == "skipped"
    assert outcome.error and "百度" in outcome.error and "反爬" in outcome.error
