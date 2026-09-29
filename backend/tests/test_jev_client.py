# -*- coding: utf-8 -*-
"""Jev 客户端：请求形状、解析、错误处理。

全部用假的 httpx transport —— 仓库约定是测试不得以真实 API Key 或联网为前提。
密钥泄露那条断言尤其重要：错误信息里出现 key 原文就等于把它写进了日志。
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent.eval import jev_client
from agent.eval.jev_client import JevClient, JevUnavailable


def _client(handler, **kwargs) -> JevClient:
    return JevClient(
        api_key=kwargs.pop("api_key", "sk-or-v1-TESTKEY"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def test_ask_posts_state_and_questions_then_parses_answers():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "answers": {"x": {"type": "noul", "noul": 0.9}},
                "usage": {"input_tokens": 12, "output_tokens": 3, "cost": 0.0000005},
            },
        )

    answer = _client(handler).ask({"message": "hi"}, {"x": {"type": "noul", "instructions": "?"}})
    assert captured["url"].endswith("/api/alpha/decisions")
    assert captured["body"]["model"] == "typesafe/jev-1.13"
    assert captured["body"]["state"] == {"message": "hi"}
    assert captured["auth"] == "Bearer sk-or-v1-TESTKEY"
    assert answer.answers["x"]["noul"] == 0.9
    assert answer.input_tokens == 12
    assert answer.cost_usd == pytest.approx(0.0000005)


def test_totals_accumulate_across_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"answers": {}, "usage": {"input_tokens": 10, "cost": 0.000001}}
        )

    client = _client(handler)
    client.ask({"m": 1}, {})
    client.ask({"m": 2}, {})
    assert client.total_input_tokens == 20
    assert client.total_cost_usd == pytest.approx(0.000002)
    assert client.requests == 2


def test_missing_key_raises_jev_unavailable(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(jev_client, "_registry_key", lambda: "")
    with pytest.raises(JevUnavailable):
        JevClient().ask({"m": 1}, {})


def test_http_error_is_raised_without_leaking_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key sk-or-v1-SECRET"}})

    client = _client(handler, api_key="sk-or-v1-SECRET")
    with pytest.raises(JevUnavailable) as exc:
        client.ask({"m": 1}, {})
    assert "SECRET" not in str(exc.value)
    assert "sk-or" not in str(exc.value)
