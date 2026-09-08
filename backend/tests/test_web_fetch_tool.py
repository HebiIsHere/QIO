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
