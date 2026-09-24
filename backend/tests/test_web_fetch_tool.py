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

    async def fake(url: str) -> tuple[int, str]:
        return (500, "<html></html>")

    tool.fetch_html = fake
    res = await tool.run(url="http://example.invalid")
    assert not res.ok
    assert "无法读取" in res.error


async def test_success_extracts_main_text_and_strips_noise():
    tool = WebFetchTool()
    html = (
        "<html><head><style>.x{}</style></head><body>"
        "<nav>NAVBAR</nav><article><p>Hello world</p></article>"
        "<footer>FOOTER</footer><script>var x=1</script></body></html>"
    )

    async def fake(url: str) -> tuple[int, str]:
        return (200, html)

    tool.fetch_html = fake
    res = await tool.run(url="http://a.com")
    assert res.ok
    assert "Hello world" in res.content
    assert "NAVBAR" not in res.content
    assert "FOOTER" not in res.content


async def test_verify_page_rejected():
    tool = WebFetchTool()

    async def fake(url: str) -> tuple[int, str]:
        return (200, "<html>captcha</html>")

    tool.fetch_html = fake
    res = await tool.run(url="http://a.com")
    assert not res.ok
    assert "登录或验证" in res.error


async def test_non_http_url_rejected():
    tool = WebFetchTool()
    res = await tool.run(url="file:///etc/passwd")
    assert not res.ok
    assert "http" in res.error


async def test_empty_page_rejected():
    tool = WebFetchTool()

    async def fake(url: str) -> tuple[int, str]:
        return (200, "<html><body><script>x</script></body></html>")

    tool.fetch_html = fake
    res = await tool.run(url="http://a.com")
    assert not res.ok


async def test_max_chars_capped_budget():
    tool = WebFetchTool()
    body = "<article><p>" + "B" * 60000 + "</p></article>"

    async def fake(url: str) -> tuple[int, str]:
        return (200, f"<html><body>{body}</body></html>")

    tool.fetch_html = fake
    res = await tool.run(url="http://a.com", max_chars=999999)
    assert res.ok
    assert len(res.content) <= 40000


async def test_javascript_only_page_reported_as_such():
    """真实事故：Project Amber 的页面自己写着「没有 JavaScript 就不能正常显示」，
    但抓取器把它报成「需要登录或验证」，模型因此以为这些站点读不到。"""
    tool = WebFetchTool()
    html = (
        '<html><head><title>Project Amber</title></head><body><div id="app"></div>'
        "<noscript>We're sorry but Project Amber doesn't work properly without "
        "JavaScript enabled. Please enable it to continue.</noscript></body></html>"
    )

    async def fake(url: str) -> tuple[int, str]:
        return (200, html)

    tool.fetch_html = fake
    res = await tool.run(url="http://a.com")
    assert not res.ok
    assert "JavaScript" in res.error
    # 必须说清「不是登录墙」，不能沿用「需要登录或验证」那句误判
    assert "不是登录或验证问题" in res.error


async def test_agent_hint_containing_verify_word_is_not_a_login_wall():
    """真实事故：页面里写着邀请 AI agent 去读 /api/AGENTS 的说明（含 verify 这个词），
    以前命中裸词 verify 被判成「该页面需要登录或验证」。"""
    tool = WebFetchTool()
    html = (
        '<html><head><meta name="description" content="For code assistants, agents or '
        'chat models: to verify agentic access, please fetch the page at /api/AGENTS">'
        "</head><body><article><p>正文在这里</p></article></body></html>"
    )

    async def fake(url: str) -> tuple[int, str]:
        return (200, html)

    tool.fetch_html = fake
    res = await tool.run(url="http://a.com")
    assert res.ok
    assert "正文在这里" in res.content


async def test_connection_failure_names_the_url_and_cause():
    """真实事故：错误信息只有「ConnectError: 」，既没有地址也没有原因（22 次）。"""
    tool = WebFetchTool()

    async def boom(url: str) -> tuple[int, str]:
        raise RuntimeError("")

    tool.fetch_html = boom
    res = await tool.run(url="http://a.com/x")
    assert not res.ok
    assert "http://a.com/x" in res.error
    assert "RuntimeError" in res.error
