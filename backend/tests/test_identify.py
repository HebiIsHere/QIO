from __future__ import annotations

from agent.services import identify
from agent.services.identify import PROVIDERS, identify_key


class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status_code = status

    async def json(self) -> dict:
        return {"data": []}


class _FakeClient:
    """Fake httpx client: /models returns `valid_status` with a real bearer and
    `invalid_status` with a deliberately invalid bearer."""

    def __init__(self, valid_status: int, invalid_status: int) -> None:
        self.valid_status = valid_status
        self.invalid_status = invalid_status
        self.calls: list[str] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args) -> bool:  # noqa: ANN002
        return False

    async def get(self, url: str, headers: dict | None = None, timeout=None):
        auth = (headers or {}).get("Authorization", "")
        self.calls.append(auth)
        is_invalid = "invalid-probe" in auth
        return _FakeResp(self.invalid_status if is_invalid else self.valid_status)


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
    async def fake_probe(base_url: str, key: str, auth_path: str = "/models") -> bool:
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
    async def fake_probe(base_url: str, key: str, auth_path: str = "/models") -> bool:
        return False

    monkeypatch.setattr(identify, "_probe_openai", fake_probe)
    monkeypatch.setattr(identify, "_probe_anthropic", fake_probe)

    assert await identify_key("sk-unknown") is None


async def test_probe_rejects_public_models_endpoint(monkeypatch) -> None:
    """GET /models returning 200 for BOTH the real key and an invalid key is a
    public catalog (OpenRouter) and must NOT be treated as a valid key hit."""

    monkeypatch.setattr(
        identify.httpx, "AsyncClient", lambda timeout=None: _FakeClient(200, 200)
    )
    assert await identify._probe_openai("https://openrouter.ai/api/v1", "sk-x") is False


async def test_probe_accepts_auth_sensitive_models_endpoint(monkeypatch) -> None:
    """GET /models returning 200 only for the real key (invalid -> 401) proves
    the key is accepted, so it is a valid hit."""

    monkeypatch.setattr(
        identify.httpx, "AsyncClient", lambda timeout=None: _FakeClient(200, 401)
    )
    assert await identify._probe_openai("https://api.deepseek.com/v1", "sk-x") is True


async def test_openrouter_probes_auth_key_endpoint(monkeypatch) -> None:
    """OpenRouter /models is public, so a valid OpenRouter key must be verified
    via the auth-sensitive /key endpoint and still be detected."""

    seen: dict[str, str] = {}

    async def fake_probe(base_url: str, key: str, auth_path: str = "/models") -> bool:
        if base_url == "https://openrouter.ai/api/v1":
            seen["base_url"] = base_url
            seen["auth_path"] = auth_path
            return True
        return False  # e.g. DeepSeek rejects the OpenRouter key

    async def fake_list(provider, key: str) -> list[str]:
        return ["deepseek/deepseek-v4-flash", "openai/gpt-5.6-luna"]

    monkeypatch.setattr(identify, "_probe_openai", fake_probe)
    monkeypatch.setattr(identify, "_list_models", fake_list)

    result = await identify_key("sk-or-abc123")
    assert result is not None
    assert result["provider"] == "OpenRouter"
    assert result["base_url"] == "https://openrouter.ai/api/v1"
    assert seen["auth_path"] == "/key"
