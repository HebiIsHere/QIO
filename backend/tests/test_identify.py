from __future__ import annotations

from agent.services import identify
from agent.services.identify import PROVIDERS, identify_key


def _find(name: str):
    for p in PROVIDERS:
        if p.name == name:
            return p
    raise AssertionError(f"provider not found: {name}")


def test_mimo_in_providers() -> None:
    mimo = _find("MiMo（小米）")
    assert mimo.base_url == "https://api.xiaomimimo.com/v1"
    assert mimo.kind == "openai"
    assert mimo.default_model == "mimo-v2.5"


async def test_prefix_fallback_probes_remaining_providers(monkeypatch) -> None:
    # `sk-` prefix hits DeepSeek first; when it fails we must fall through to
    # the full table so shared-prefix providers like MiMo still get probed.
    async def fake_probe(base_url: str, key: str) -> bool:
        return base_url == "https://api.xiaomimimo.com/v1"

    async def fake_list(provider, key: str) -> list[str]:
        return ["mimo-v2.5", "mimo-v2.5-pro"]

    monkeypatch.setattr(identify, "_probe_openai", fake_probe)
    monkeypatch.setattr(identify, "_probe_anthropic", fake_probe)
    monkeypatch.setattr(identify, "_list_models", fake_list)

    result = await identify_key("sk-c8vty-test-mimo-key")
    assert result is not None
    assert result["provider"] == "MiMo（小米）"
    assert result["base_url"] == "https://api.xiaomimimo.com/v1"
    assert result["kind"] == "openai"
    assert result["default_model"] == "mimo-v2.5"
    assert result["models"] == ["mimo-v2.5", "mimo-v2.5-pro"]


async def test_no_match_returns_none(monkeypatch) -> None:
    async def fake_probe(base_url: str, key: str) -> bool:
        return False

    monkeypatch.setattr(identify, "_probe_openai", fake_probe)
    monkeypatch.setattr(identify, "_probe_anthropic", fake_probe)

    assert await identify_key("sk-unknown") is None
