"""模拟服务夹具：写死 HTTP 的工具也能在不联网、不用真实凭据的前提下被测。

这些用例跑的是**真实的沙箱执行路径**（SandboxExecutor → tool_worker 子进程 →
sitecustomize 夹具 → 本机桩服务器）：写死 URL 的工具照样发请求，夹具在本机接住，
并留下「这次是模拟」的报告。
"""

from __future__ import annotations

import json
import socket

import pytest

from agent.tools.mock_services import (
    FIXTURE_CREDENTIAL,
    MOCK_FILE_NAME,
    MockServiceError,
    MockServicePlan,
    fixture_for_definition,
)
from agent.tools.sandbox import SandboxExecutor

PLAN_JSON = {
    "services": [
        {
            "host": "api.weather.example",
            "routes": [
                {"method": "GET", "path": "/v1/now", "json": {"temp_c": 21, "city": "上海"}},
                {"method": "GET", "path": "/v1/broken", "status": 503, "text": "上游不可用"},
            ],
        }
    ],
    "credentials": ["QIO_KEY_WEATHER"],
    "note": "天气接口是假的",
}

HTTP_TOOL = '''
import json
import os
import urllib.request


def run(**kwargs):
    request = urllib.request.Request("http://api.weather.example/v1/now")
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return {
        "temp_c": payload["temp_c"],
        "status": response.status,
        "credential": os.environ.get("QIO_KEY_WEATHER"),
    }
'''

BROKEN_PATH_TOOL = '''
import json
import urllib.error
import urllib.request


def run(**kwargs):
    try:
        urllib.request.urlopen("http://api.weather.example/v1/broken", timeout=5)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return {"status": exc.code, "body": body}
    return {"status": 200}
'''

UNDECLARED_TOOL = '''
import socket


def run(**kwargs):
    try:
        socket.create_connection(("198.51.100.9", 80), timeout=2)
    except OSError as exc:
        return {"blocked": True, "error": str(exc)}
    return {"blocked": False}
'''

LOOPBACK_TOOL = '''
import socket


def run(**kwargs):
    try:
        socket.create_connection(("127.0.0.1", int(kwargs["port"])), timeout=2)
    except OSError as exc:
        return {"blocked": True, "error": str(exc)}
    return {"blocked": False}
'''


def _sandbox() -> SandboxExecutor:
    return SandboxExecutor(executor="subprocess")


def _plan(**overrides) -> MockServicePlan:
    payload = json.loads(json.dumps(PLAN_JSON))
    payload.update(overrides)
    return MockServicePlan.from_json(payload)


async def test_a_tool_that_hard_codes_http_is_served_by_the_declared_mock(tmp_path, monkeypatch):
    # 就算父进程环境里有真实 Key，也不该进到工具里（沙箱白名单 + 夹具都会拦住）
    monkeypatch.setenv("QIO_KEY_WEATHER", "sk-real-looking-credential")
    fixture = _plan().bootstrap(workdir=tmp_path / "fixture")

    result = await _sandbox().execute(HTTP_TOOL, {}, extra_env=fixture.extra_env())

    assert result.ok, result.diagnostic()
    assert result.value["temp_c"] == 21
    assert result.value["status"] == 200
    assert result.value["credential"] == FIXTURE_CREDENTIAL  # 只有明显的假值
    report = fixture.read_report()
    assert report is not None
    assert report.simulated is True
    assert report.verified_real_service is False
    assert report.hosts == ["api.weather.example"]
    assert report.credentials == ["QIO_KEY_WEATHER"]
    assert [call["path"] for call in report.calls] == ["/v1/now"]
    assert report.calls[0]["matched"] is True
    # 结论里必须说清「是模拟、不等于真实服务验证」
    note = fixture.note_for_result()
    assert "模拟服务" in note
    assert "没有访问真实服务" in note
    assert "不等于真实服务已验证" in note
    fixture.cleanup()


async def test_a_declared_route_can_return_an_error_status(tmp_path):
    fixture = _plan().bootstrap(workdir=tmp_path / "fixture")

    result = await _sandbox().execute(BROKEN_PATH_TOOL, {}, extra_env=fixture.extra_env())

    assert result.ok, result.diagnostic()
    assert result.value == {"status": 503, "body": "上游不可用"}
    fixture.cleanup()


async def test_an_undeclared_path_is_not_silently_mocked(tmp_path):
    fixture = _plan().bootstrap(workdir=tmp_path / "fixture")
    code = HTTP_TOOL.replace("/v1/now", "/v1/unknown")

    result = await _sandbox().execute(code, {}, extra_env=fixture.extra_env())

    assert result.ok is False
    assert "501" in (result.error or "") or "501" in result.diagnostic()
    report = fixture.read_report()
    assert report is not None
    assert [call["matched"] for call in report.calls] == [False]
    fixture.cleanup()


async def test_an_undeclared_host_is_blocked_before_any_traffic(tmp_path):
    fixture = _plan().bootstrap(workdir=tmp_path / "fixture")

    result = await _sandbox().execute(UNDECLARED_TOOL, {}, extra_env=fixture.extra_env())

    assert result.ok, result.diagnostic()
    assert result.value["blocked"] is True
    assert "默认不联网" in result.value["error"]
    report = fixture.read_report()
    assert report is not None
    assert [item["host"] for item in report.blocked] == ["198.51.100.9"]
    assert report.calls == []
    fixture.cleanup()


async def test_without_a_declaration_nothing_is_intercepted(tmp_path):
    """夹具是显式生效的：没声明 mocks 的测试行为不变（不是偷偷全局断网）。"""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    result = await _sandbox().execute(LOOPBACK_TOOL, {"port": port})

    assert result.ok, result.diagnostic()
    assert result.value["blocked"] is True
    assert "默认不联网" not in result.value["error"]  # 是真实的连接失败，不是夹具拦的


def test_the_fixture_never_accepts_real_credential_values():
    with pytest.raises(MockServiceError) as excinfo:
        _plan(credentials={"QIO_KEY_WEATHER": "sk-real"})
    assert "不接受真实凭据值" in str(excinfo.value)

    with pytest.raises(MockServiceError):
        _plan(credentials=["WEATHER_KEY"])  # 必须是 QIO_KEY_ 开头的大写名


def test_the_result_note_is_honest_even_without_a_report(tmp_path):
    fixture = _plan().bootstrap(workdir=tmp_path / "fixture")

    note = fixture.note_for_result()

    assert "没有拿到模拟服务的运行报告" in note
    assert "不能据此说测试覆盖了真实服务" in note
    fixture.cleanup()


@pytest.mark.parametrize(
    "payload",
    [
        {"services": [{"host": "https://api.example.com", "routes": [{"path": "/x", "text": "a"}]}]},
        {"services": [{"host": "api.example.com", "routes": []}]},
        {"services": []},
        {"services": [{"host": "api.example.com", "routes": [{"path": "x", "text": "a"}]}]},
        {"services": [{"host": "api.example.com", "routes": [{"path": "/x"}]}]},
        {
            "services": [
                {"host": "api.example.com", "routes": [{"path": "/x", "json": {}, "text": "a"}]}
            ]
        },
        {"services": [{"host": "api.example.com", "routes": [{"path": "/x", "text": "a"}], "x": 1}]},
        {"services": [{"host": "api.example.com", "routes": [{"path": "/x", "text": "a"}]}], "extra": 1},
        {"services": [{"host": "api.example.com", "routes": [{"path": "/x", "text": "a", "status": 999}]}]},
    ],
)
def test_invalid_declarations_are_rejected(payload):
    with pytest.raises(MockServiceError):
        MockServicePlan.from_json(payload)


def test_the_declaration_is_read_from_the_tool_project_file(tmp_path):
    class _Definition:
        def __init__(self, files):
            self.files = files

    assert fixture_for_definition(_Definition({})) is None

    fixture = fixture_for_definition(
        _Definition({MOCK_FILE_NAME: json.dumps(PLAN_JSON, ensure_ascii=False)})
    )

    assert fixture is not None
    assert (fixture.directory / "sitecustomize.py").is_file()
    assert (fixture.directory / "qio_mock_service.py").is_file()
    plan_path = fixture.directory / "plan.json"
    assert json.loads(plan_path.read_text(encoding="utf-8"))["services"][0]["host"] == "api.weather.example"
    environment = fixture.extra_env()
    assert environment["PYTHONPATH"] == str(fixture.directory)
    assert environment["QIO_MOCK_PLAN"] == str(plan_path)
    assert environment["QIO_MOCK_REPORT"] == str(fixture.report_path)
    fixture.cleanup()

    def _broken():
        return _Definition({MOCK_FILE_NAME: "{not json"})

    with pytest.raises(MockServiceError):
        fixture_for_definition(_broken())
