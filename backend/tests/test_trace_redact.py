from __future__ import annotations

from pathlib import Path

import pytest

from agent.trace import redact as redact_module
from agent.trace.redact import (
    REDACTED,
    assert_clean,
    clear_registered_secrets,
    preview,
    redact_any,
    redact_text,
    register_secret,
    register_secrets,
    registered_secret_count,
)


@pytest.fixture(autouse=True)
def _clean_known_secrets():
    """登记表是进程级的：每条用例自己清干净，避免互相影响。"""
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def test_redact_api_key_in_text():
    out = redact_text("key is sk-abcDEF123456xyz")
    assert "sk-abcDEF123456xyz" not in out
    assert REDACTED in out


def test_redact_bearer_and_authorization():
    out = redact_text("Authorization: Bearer eyJhbGciOi.foo.bar")
    assert "eyJhbGciOi.foo.bar" not in out


def test_redact_kv_forms():
    for text in [
        "api_key=SUPERSECRET",
        "token: abc123",
        "password=hunter2",
        "cookie=session=deadbeef",
    ]:
        out = redact_text(text)
        assert "SUPERSECRET" not in out
        assert "abc123" not in out
        assert "hunter2" not in out
        assert "deadbeef" not in out


def test_redact_mapping_secret_fields():
    data = {
        "api_key": "sk-live-1234567890",
        "authorization": "Bearer zzz",
        "password": "p@ss",
        "note": "plain text ok",
        "nested": {"token": "t0k3n", "value": 3},
    }
    out = redact_any(data)
    assert out["api_key"] == REDACTED
    assert out["authorization"] == REDACTED
    assert out["password"] == REDACTED
    assert out["note"] == "plain text ok"
    assert out["nested"]["token"] == REDACTED
    assert out["nested"]["value"] == 3


def test_redact_tool_schema_declared_secret():
    out = redact_any({"weather_key": "abc123", "city": "成都"}, secret_fields={"weather_key"})
    assert out["weather_key"] == REDACTED
    assert out["city"] == "成都"


def test_qio_key_env_redacted():
    out = redact_text("env QIO_KEY_WEATHER_KEY=abc123 set")
    assert "abc123" not in out


def test_preview_truncates_and_redacts():
    out = preview({"secret": "sk-aaaaaaaabbbbbbbb", "text": "x" * 500}, limit=50)
    assert "sk-aaaaaaaabbbbbbbb" not in out
    assert len(out) <= 51  # 50 + ellipsis


def test_assert_clean_detects_leak():
    assert_clean("nothing sensitive", ["topsecret"])
    try:
        assert_clean("here topsecret is", ["topsecret"])
        raise AssertionError("should have raised")
    except AssertionError as exc:
        assert "leaked" in str(exc)


def test_token_counts_are_not_redacted():
    data = {"total_tokens": 120, "output_tokens": 10, "input_tokens": 5, "tokens": 3}
    out = redact_any(data)
    assert out == data  # 计数字段不受影响


def test_key_suffix_field_redacted():
    out = redact_any({"weather_key": "abc123", "keywords": ["a"]})
    assert out["weather_key"] == REDACTED
    assert out["keywords"] == ["a"]

# ---------- D4：已知密钥登记表（exact match，不靠正则猜形状） ----------


def test_a_secret_with_no_key_shape_is_still_redacted():
    """形状不像密钥的密钥（随机串 / 密码）只有 exact match 挡得住。"""
    assert register_secret("Zx9Qm2Wv7Lp4") is True
    out = redact_text("db password is Zx9Qm2Wv7Lp4, keep the rest")
    assert "Zx9Qm2Wv7Lp4" not in out
    assert REDACTED in out
    assert "keep the rest" in out


def test_a_registered_secret_is_redacted_inside_structures():
    register_secret("Zx9Qm2Wv7Lp4")
    out = redact_any({"note": "value Zx9Qm2Wv7Lp4 here", "items": ["Zx9Qm2Wv7Lp4"]})
    assert "Zx9Qm2Wv7Lp4" not in str(out)


def test_registered_secret_is_redacted_in_preview():
    register_secret("Zx9Qm2Wv7Lp4")
    out = preview({"text": "token Zx9Qm2Wv7Lp4"})
    assert "Zx9Qm2Wv7Lp4" not in out


def test_short_values_are_not_registered():
    """太短的值会把正常文本打烂，而且它本来也不像密钥。"""
    for value in (None, "", "abc", "12345"):
        assert register_secret(value) is False
    assert registered_secret_count() == 0


def test_registration_is_idempotent_and_reports_counts():
    assert register_secret("Zx9Qm2Wv7Lp4") is True
    assert register_secret("Zx9Qm2Wv7Lp4") is False
    assert registered_secret_count() == 1
    assert register_secrets(["Qq11Ww22Ee33", "Zx9Qm2Wv7Lp4", ""]) == 1
    assert registered_secret_count() == 2


def test_longer_registered_secret_is_replaced_first():
    """短的前缀先替换会把长的打碎（剩下半截仍然泄露）。"""
    register_secrets(["prefix-secret-123456", "prefix-secret-12"])
    out = redact_text("value=prefix-secret-123456")
    assert "prefix-secret" not in out
    assert out.count(REDACTED) == 1


def test_a_registered_secret_can_be_forgotten():
    register_secret("Zx9Qm2Wv7Lp4")
    assert registered_secret_count() == 1
    clear_registered_secrets()
    assert registered_secret_count() == 0
    # 凭据被删除后主动失效：值不再被替换（因为它已经不在库里了）
    assert "Zx9Qm2Wv7Lp4" in redact_text("Zx9Qm2Wv7Lp4")


def test_the_registry_never_hands_the_value_back():
    """登记表只用于匹配：对外只能问到条数，拿不回值。"""
    register_secret("Zx9Qm2Wv7Lp4")
    assert registered_secret_count() == 1
    assert not hasattr(redact_module, "known_secrets")
    assert not hasattr(redact_module, "registered_secrets")
    assert not hasattr(redact_module, "registry")


def test_redaction_is_local_and_never_asks_a_model():
    """打码不调用模型：模块里不许出现模型调用（值也不送给任何外部组件）。"""
    source = Path(redact_module.__file__).read_text(encoding="utf-8").lower()
    assert "adapter" not in source
    assert "complete(" not in source
    assert "requests" not in source
    assert "httpx" not in source


# ---------- D4：结构化打码（字符串里嵌的 JSON 也按字段名打） ----------


def test_embedded_json_is_redacted_by_field_name():
    out = redact_text('{"password": "hunter2", "note": "ok", "n": 1}')
    import json

    parsed = json.loads(out)
    assert parsed["password"] == REDACTED
    assert parsed["note"] == "ok"
    assert parsed["n"] == 1


def test_embedded_json_uses_the_tool_declared_secret_fields():
    out = redact_text('{"weather_key": "abc123", "city": "成都"}', secret_fields={"weather_key"})
    import json

    parsed = json.loads(out)
    assert parsed["weather_key"] == REDACTED
    assert parsed["city"] == "成都"


def test_embedded_json_also_matches_registered_secrets():
    register_secret("Zx9Qm2Wv7Lp4")
    out = redact_text('{"note": "Zx9Qm2Wv7Lp4"}')
    assert "Zx9Qm2Wv7Lp4" not in out


def test_prose_that_merely_mentions_a_brace_is_not_reformatted():
    text = "结果是 {不是 JSON}，原样保留"
    assert redact_text(text) == text


# ---------- D4：形状正则的补充（JWT / URL 里的凭据 / PEM 私钥块） ----------


def test_standalone_jwt_is_redacted():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    out = redact_text(f"token is {token}")
    assert token not in out
    assert REDACTED in out


def test_url_embedded_credentials_are_redacted():
    out = redact_text("fetch https://alice:s3cr3tP4ss@example.com/data")
    assert "s3cr3tP4ss" not in out
    assert "example.com" in out


def test_pem_private_key_block_is_redacted():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact_text(f"key material:\n{pem}\nend")
    assert "MIIEowIBAAKCAQEA" not in out
    assert "end" in out

# ---------- 裸 qio_key_ 令牌（正则兜底，不是主防线） ----------


def test_a_bare_qio_key_token_is_redacted():
    """没有 `=` / `:` 分隔符的裸令牌，以前整条漏过去（Lead 独立复现过）。"""
    for text in (
        "and qio_key_ABCDEF0123456789",
        "QIO_KEY_DEADBEEF0123456789 leaked",
        "token 是 qio_key_abcdef-123456 结束",
    ):
        out = redact_text(text)
        assert "ABCDEF0123456789" not in out
        assert "DEADBEEF0123456789" not in out
        assert "abcdef-123456" not in out
        assert REDACTED in out


def test_the_env_var_form_still_redacts_its_value():
    """顺序回归：先让 kv 规则吃掉「名字=值」的值，再由裸令牌规则吃掉名字 ——
    反过来（先吃名字）kv 规则就看不到 `名字=值`，值会漏出去。"""
    out = redact_text("env QIO_KEY_WEATHER_KEY=abc123 set")
    assert "abc123" not in out
    assert "QIO_KEY_WEATHER_KEY" not in out
    assert out.count(REDACTED) >= 1


def test_words_that_merely_look_like_the_prefix_are_not_touched():
    """反例：qio_key 后面必须跟下划线，普通词与短标识符不许被吞。"""
    for text in (
        "qio_keyword_test 是普通词",
        "qio_keys 表示复数",
        "qio_key_abc 太短，不当令牌",
        "QIO_KEY 只是一个前缀",
    ):
        assert redact_text(text) == text
