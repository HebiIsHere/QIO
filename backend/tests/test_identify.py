"""厂商预设 + 本地前缀提示 + 「只打用户选定的地址」的验证。

这个文件以前守护的是「把同一把 Key 依次探测十几个候选厂商，谁先返回 200 就是谁」。
那正是现在要停掉的行为：同一个 Key 会被发给用户没有选过的服务。现在这里守护相反
的事实：

- 预设表是唯一来源，每条都带协议（kind）与类别（官方/三方转发/自定义）；
- Key 前缀只做**本地**提示，不产生任何网络请求；
- 验证只请求调用方给的那个 base_url，失败也不会去试探别家。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.credentials import providers
from agent.services import verify as verify_service

pytestmark = pytest.mark.real_verify


class _RecordingOpenAI:
    """Fake openai.AsyncOpenAI：记录构造参数与是否被关闭。"""

    instances: list["_RecordingOpenAI"] = []
    error: BaseException | None = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.base_url = kwargs.get("base_url")
        self.closed = False
        _RecordingOpenAI.instances.append(self)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):  # noqa: ANN003
        if type(self).error is not None:
            raise type(self).error
        return SimpleNamespace(choices=[])

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_openai():
    _RecordingOpenAI.instances = []
    _RecordingOpenAI.error = None
    yield
    _RecordingOpenAI.instances = []
    _RecordingOpenAI.error = None


def _install_fake_openai(monkeypatch, error: BaseException | None = None) -> None:
    import openai

    _RecordingOpenAI.error = error
    monkeypatch.setattr(openai, "AsyncOpenAI", _RecordingOpenAI)


def _api_error(cls, status: int, message: str) -> BaseException:
    """构造一个真实的 openai SDK 错误（带 response），保证分类路径与线上一致。"""
    import httpx

    request = httpx.Request("POST", "https://selected.example/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": message}})
    return cls(message, response=response, body=None)


def test_presets_declare_protocol_and_destination_kind() -> None:
    presets = providers.list_presets()
    assert presets, "预设表不能为空"
    for preset in presets:
        assert preset.kind in providers.KINDS, preset.id
        assert preset.category in (
            providers.CATEGORY_OFFICIAL,
            providers.CATEGORY_AGGREGATOR,
        ), preset.id
        assert preset.base_url.startswith("https://"), preset.id
    # 自定义服务是显式的一条，不属于任何厂商预设
    assert providers.CUSTOM_PRESET.id == "custom"
    assert providers.CUSTOM_PRESET.category == providers.CATEGORY_CUSTOM
    # 三方转发与官方服务必须能分开：OpenRouter 是转发，DeepSeek 是官方
    assert providers.find_preset("openrouter").category == providers.CATEGORY_AGGREGATOR
    assert providers.find_preset("deepseek").category == providers.CATEGORY_OFFICIAL


def test_presets_without_reliable_default_model_are_marked_empty() -> None:
    """没有可靠默认模型的服务必须留空：界面据此要求用户自己选。"""
    empty = [p.id for p in providers.list_presets() if not p.suggested_model]
    assert "aihubmix" in empty
    assert "deepbricks" in empty


def test_key_hint_is_local_only(monkeypatch) -> None:
    """前缀提示是纯本地的：连 httpx 都不该被碰到。"""

    def _explode(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("本地提示不应该发起任何网络请求")

    monkeypatch.setattr(verify_service.httpx, "AsyncClient", _explode)

    assert providers.key_hint("sk-ant-api03-xxxx").id == "anthropic"
    assert providers.key_hint("gsk_abcdefghij").id == "groq"
    assert providers.key_hint("sk-or-v1-abcdef").id == "openrouter"
    # 共享前缀 `sk-`：只做提示，不声明归属
    assert providers.key_hint("sk-1234567890").id in {"deepseek", "openai"}
    assert providers.key_hint("") is None


def test_preset_lookup_by_endpoint_is_exact() -> None:
    assert providers.preset_for_endpoint("https://api.deepseek.com/v1").id == "deepseek"
    assert providers.preset_for_endpoint("https://api.deepseek.com/v1/").id == "deepseek"
    # 域名相近但不是同一家：不能猜
    assert providers.preset_for_endpoint("https://api.deepseek.com.evil.test/v1") is None
    assert providers.preset_for_endpoint("") is None


async def test_verify_requests_only_the_selected_endpoint(monkeypatch) -> None:
    """验证只打用户选定的地址：失败也不会把 Key 发给别家。"""
    import openai

    _install_fake_openai(monkeypatch, _api_error(openai.AuthenticationError, 401, "bad key"))
    result = await verify_service.verify_model(
        secret="sk-user-selected",
        endpoint="https://api.moonshot.cn/v1",
        model="kimi-k2",
        kind="openai",
    )
    assert result.ok is False
    assert result.reason_code == verify_service.REASON_INVALID_KEY
    assert len(_RecordingOpenAI.instances) == 1, "只允许构造一个客户端"
    assert _RecordingOpenAI.instances[0].base_url == "https://api.moonshot.cn/v1"
    # 错误文本里不能出现密钥
    assert "sk-user-selected" not in result.detail
    assert _RecordingOpenAI.instances[0].closed is True


async def test_verify_reports_model_permission_problem(monkeypatch) -> None:
    import openai

    _install_fake_openai(
        monkeypatch,
        _api_error(openai.PermissionDeniedError, 403, "model gpt-x: does not have access"),
    )
    result = await verify_service.verify_model(
        secret="sk-x",
        endpoint="https://api.openai.com/v1",
        model="gpt-x",
    )
    assert result.ok is False
    assert result.reason_code == verify_service.REASON_MODEL_NOT_ALLOWED
    assert "权限" in result.message


async def test_verify_needs_endpoint_and_model(monkeypatch) -> None:
    _install_fake_openai(monkeypatch)
    missing_endpoint = await verify_service.verify_model(
        secret="sk-x", endpoint="", model="m"
    )
    assert missing_endpoint.reason_code == verify_service.REASON_ENDPOINT_MISSING
    missing_model = await verify_service.verify_model(
        secret="sk-x", endpoint="https://api.deepseek.com/v1", model=""
    )
    assert missing_model.reason_code == verify_service.REASON_MODEL_MISSING
    assert _RecordingOpenAI.instances == []


async def test_verify_anthropic_uses_anthropic_protocol(monkeypatch) -> None:
    from agent.adapters import anthropic as anthropic_module

    seen: dict[str, str] = {}

    async def fake_probe(api_key: str, model: str, endpoint: str) -> str:
        seen["endpoint"] = endpoint
        seen["model"] = model
        return "native"

    monkeypatch.setattr(anthropic_module, "probe_anthropic", fake_probe)
    _install_fake_openai(monkeypatch)

    result = await verify_service.verify_model(
        secret="sk-ant-x",
        endpoint="https://api.anthropic.com/v1",
        model="claude-sonnet-4",
        kind="anthropic",
    )
    assert result.ok is True
    assert seen["endpoint"] == "https://api.anthropic.com/v1"
    # Anthropic 走 Anthropic 协议，不能顺手构造 OpenAI 客户端
    assert _RecordingOpenAI.instances == []


async def test_list_models_never_raises(monkeypatch) -> None:
    import httpx

    class _BoomClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise httpx.ConnectError("down")

    monkeypatch.setattr(verify_service.httpx, "AsyncClient", _BoomClient)
    assert (
        await verify_service.list_models(
            secret="sk-x", endpoint="https://api.deepseek.com/v1", kind="openai"
        )
        == []
    )
