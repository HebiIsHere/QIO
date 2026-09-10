from __future__ import annotations

from agent.trace.redact import REDACTED, assert_clean, preview, redact_any, redact_text


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
