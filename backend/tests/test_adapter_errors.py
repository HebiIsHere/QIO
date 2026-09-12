from __future__ import annotations

import pytest

from agent.adapters import errors as e


class AuthenticationError(Exception):
    """Stands in for openai.AuthenticationError (matched by class name)."""


class RateLimitError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class BadRequestError(Exception):
    pass


def test_normalize_by_class_name():
    assert isinstance(e.normalize_error(AuthenticationError("bad key")), e.AuthenticationError)
    assert isinstance(e.normalize_error(RateLimitError("429")), e.RateLimitError)
    assert isinstance(e.normalize_error(APIConnectionError("boom")), e.NetworkError)
    assert isinstance(e.normalize_error(BadRequestError("bad args")), e.InvalidToolCall)


def test_normalize_unknown_is_internal():
    err = e.normalize_error(ValueError("weird"))
    assert isinstance(err, e.ProviderInternalError)
    assert "ValueError" in str(err)


def test_normalize_passthrough_for_own_types():
    original = e.UnsupportedCapability("no tools")
    assert e.normalize_error(original) is original


def test_error_codes_are_stable():
    assert e.AuthenticationError.code == "authentication"
    assert e.RateLimitError.code == "rate_limit"
    assert e.NetworkError.code == "network"
    assert e.InvalidToolCall.code == "invalid_tool_call"
    assert e.ProviderInternalError.code == "provider_internal"
    assert e.UnsupportedCapability.code == "unsupported_capability"
