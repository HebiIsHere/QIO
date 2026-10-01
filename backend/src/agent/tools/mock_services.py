"""显式的模拟服务夹具：让「写死 HTTP 的工具」也能在**不联网、不用真实凭据**的前提下被测。

为什么需要它：工具测试不注入真实凭据（这是正确的安全边界），代价是写死 HTTP 的工具
在测试里要么去连真实服务（不行）、要么根本测不了。这里给一条显式的第三条路：

* 工具项目里声明一个 `qio-mocks.json`（只有测试会用它，运行时是惰性的）；
* 测试前用 `bootstrap()` 把夹具写进一个目录（`sitecustomize.py` + 运行时 + 声明），
  再把 `QIO_MOCK_PLAN` / `QIO_MOCK_REPORT` / `PYTHONPATH` 交给沙箱子进程；
* 子进程里**默认关闭网络**：只有声明过的 host 会被接到本机桩服务器，其它连接一律
  拒绝（连 DNS 都不做）；凭据只注入明显的假值 `qio-mock-credential`；
* 测试结束时夹具把「调用了哪些模拟接口、拦掉了哪些连接」写成报告，调用方据此如实
  告诉模型与用户：**这次测试是模拟的，不等于真实服务验证过**。

边界（说清楚，不夸大）：

* 只对「显式声明了 mocks 的测试」生效：没有声明的工具测试行为不变。要不要把「所有
  工具测试一律断网」升级成平台级策略，涉及 policy.network 与审批语义，不在这个夹具
  的范围里；
* 桩服务器是本机 HTTP（明文）环回：客户端如果非 TLS 不可，那一层不做中间人 ——
  连不上就如实失败，不假装通过；
* 只按声明返回固定响应，不模拟真实服务的语义（限流、分页、鉴权规则、真实数据形状）。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 工具项目里的声明文件名（跟着项目文件一起进沙箱，但运行时不会有人读它）。
MOCK_FILE_NAME = "qio-mocks.json"
PLAN_ENV = "QIO_MOCK_PLAN"
REPORT_ENV = "QIO_MOCK_REPORT"
# 夹具给测试注入的凭据值：一眼能看出是假的，绝不可能等于真实 Key。
FIXTURE_CREDENTIAL = "qio-mock-credential"
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_CREDENTIAL_NAME_RE = re.compile(r"^QIO_KEY_[A-Z0-9_]+$")
_SCHEMA = 1


class MockServiceError(ValueError):
    """模拟服务声明不合法（调用方要把它当成测试失败，而不是忽略）。"""


@dataclass(frozen=True)
class MockRoute:
    method: str
    path: str
    status: int = 200
    json_body: Any = None
    text_body: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    delay_ms: int = 0

    def to_json(self) -> dict:
        payload: dict[str, Any] = {"method": self.method, "path": self.path, "status": self.status}
        if self.json_body is not None:
            payload["json"] = self.json_body
        else:
            payload["text"] = self.text_body or ""
        if self.headers:
            payload["headers"] = dict(self.headers)
        if self.delay_ms:
            payload["delay_ms"] = self.delay_ms
        return payload


@dataclass(frozen=True)
class MockService:
    host: str
    routes: tuple[MockRoute, ...]

    def to_json(self) -> dict:
        return {"host": self.host, "routes": [route.to_json() for route in self.routes]}


@dataclass(frozen=True)
class MockServicePlan:
    """工具测试要用到的模拟服务清单（不可变；哈希就是它内容的指纹）。"""

    services: tuple[MockService, ...]
    credentials: tuple[str, ...] = ()
    note: str = ""

    # -- 解析与校验 -------------------------------------------------------

    @classmethod
    def from_json(cls, data: Any) -> "MockServicePlan":
        if not isinstance(data, dict):
            raise MockServiceError("模拟服务声明必须是一个 JSON 对象")
        unknown = set(data) - {"schema", "services", "credentials", "note"}
        if unknown:
            raise MockServiceError("模拟服务声明里有不认识的字段：" + "、".join(sorted(unknown)))
        if int(data.get("schema") or _SCHEMA) != _SCHEMA:
            raise MockServiceError(f"不支持的模拟服务声明版本：{data.get('schema')!r}")
        raw_services = data.get("services")
        if not isinstance(raw_services, list) or not raw_services:
            raise MockServiceError("模拟服务声明至少要有一个 services（host + routes）")
        services: list[MockService] = []
        seen_hosts: set[str] = set()
        for raw_service in raw_services:
            if not isinstance(raw_service, dict):
                raise MockServiceError("services 里的每一项都必须是对象")
            unknown_service = set(raw_service) - {"host", "routes"}
            if unknown_service:
                raise MockServiceError(
                    "模拟服务里有不认识的字段：" + "、".join(sorted(unknown_service))
                )
            host = str(raw_service.get("host") or "").strip().lower()
            if not host or any(char in host for char in "/: "):
                raise MockServiceError(
                    f"host 必须是纯主机名（不带协议/端口/路径）：{raw_service.get('host')!r}"
                )
            if host in seen_hosts:
                raise MockServiceError(f"host 重复声明了：{host}")
            seen_hosts.add(host)
            raw_routes = raw_service.get("routes")
            if not isinstance(raw_routes, list) or not raw_routes:
                raise MockServiceError(f"{host} 至少要声明一条 routes")
            routes: list[MockRoute] = []
            for raw_route in raw_routes:
                routes.append(_parse_route(raw_route, host))
            services.append(MockService(host=host, routes=tuple(routes)))
        credentials = _parse_credentials(data.get("credentials"))
        return cls(services=tuple(services), credentials=credentials, note=str(data.get("note") or ""))

    @classmethod
    def load(cls, path: str | Path) -> "MockServicePlan":
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise MockServiceError(f"读不到模拟服务声明：{exc}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise MockServiceError(f"模拟服务声明不是合法 JSON：{exc}") from exc
        return cls.from_json(data)

    @classmethod
    def from_definition(cls, definition: Any) -> "MockServicePlan | None":
        """从工具定义的项目文件里找 `qio-mocks.json`；没有声明就返回 None。"""
        raw = (getattr(definition, "files", None) or {}).get(MOCK_FILE_NAME)
        if raw is None:
            return None
        try:
            data = json.loads(str(raw))
        except ValueError as exc:
            raise MockServiceError(f"{MOCK_FILE_NAME} 不是合法 JSON：{exc}") from exc
        return cls.from_json(data)

    # -- 输出 -------------------------------------------------------------

    def to_json(self) -> dict:
        payload: dict[str, Any] = {
            "schema": _SCHEMA,
            "services": [service.to_json() for service in self.services],
        }
        if self.credentials:
            payload["credentials"] = list(self.credentials)
        if self.note:
            payload["note"] = self.note
        return payload

    def hosts(self) -> list[str]:
        return [service.host for service in self.services]

    def route_count(self) -> int:
        return sum(len(service.routes) for service in self.services)

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> str:
        """给模型/用户看的一句话：这次测试会用到哪些模拟接口。"""
        parts = [
            f"{service.host}（{len(service.routes)} 个接口）" for service in self.services
        ]
        text = "模拟服务：" + "、".join(parts)
        if self.credentials:
            text += "；凭据用明显的假值（" + "、".join(self.credentials) + "）"
        return text

    def bootstrap(self, *, workdir: str | Path | None = None) -> "MockFixture":
        """把夹具落到磁盘：`workdir/qio-mock-fixture/`，没给就在系统临时目录里新建一个。

        夹具与报告在同一个目录里，`cleanup()` 一次删干净（一次测试运行一份夹具）。
        """
        if workdir is None:
            directory = Path(tempfile.mkdtemp(prefix="qio-mock-fixture-"))
        else:
            directory = Path(workdir) / "qio-mock-fixture"
            directory.mkdir(parents=True, exist_ok=True)
        (directory / "qio_mock_service.py").write_text(_RUNTIME_SOURCE, encoding="utf-8")
        (directory / "sitecustomize.py").write_text(_SITECUSTOMIZE_SOURCE, encoding="utf-8")
        (directory / "plan.json").write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return MockFixture(plan=self, directory=directory, report_path=directory / "report.json")


def _parse_route(raw_route: Any, host: str) -> MockRoute:
    if not isinstance(raw_route, dict):
        raise MockServiceError(f"{host} 的 routes 里每一项都必须是对象")
    unknown = set(raw_route) - {"method", "path", "status", "json", "text", "headers", "delay_ms"}
    if unknown:
        raise MockServiceError("路由里有不认识的字段：" + "、".join(sorted(unknown)))
    path = str(raw_route.get("path") or "").strip()
    if not path.startswith("/"):
        raise MockServiceError(f"路由 path 必须以 / 开头：{raw_route.get('path')!r}")
    method = str(raw_route.get("method") or "GET").strip().upper()
    if method not in _METHODS:
        raise MockServiceError(f"不支持的 HTTP 方法：{raw_route.get('method')!r}")
    try:
        status = int(raw_route.get("status") or 200)
    except (TypeError, ValueError) as exc:
        raise MockServiceError(f"status 必须是整数：{raw_route.get('status')!r}") from exc
    if not 100 <= status <= 599:
        raise MockServiceError(f"status 超出范围：{status}")
    has_json = "json" in raw_route
    has_text = "text" in raw_route
    if has_json and has_text:
        raise MockServiceError("一条路由只能有 json 或 text 其中之一")
    if not has_json and not has_text:
        raise MockServiceError(f"{host}{path} 缺少响应内容（json 或 text）")
    headers_raw = raw_route.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise MockServiceError("headers 必须是对象")
    try:
        delay_ms = int(raw_route.get("delay_ms") or 0)
    except (TypeError, ValueError) as exc:
        raise MockServiceError(f"delay_ms 必须是整数：{raw_route.get('delay_ms')!r}") from exc
    if not 0 <= delay_ms <= 5000:
        raise MockServiceError(f"delay_ms 超出范围（0-5000）：{delay_ms}")
    return MockRoute(
        method=method,
        path=path,
        status=status,
        json_body=raw_route.get("json") if has_json else None,
        text_body=str(raw_route.get("text")) if has_text else None,
        headers={str(key): str(value) for key, value in headers_raw.items()},
        delay_ms=delay_ms,
    )


def _parse_credentials(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise MockServiceError(
            "credentials 只能是环境变量名列表（例如 [\"QIO_KEY_WEATHER\"]）："
            "夹具不接受真实凭据值，值会被替换成明显的假值"
        )
    names: list[str] = []
    for item in raw:
        name = str(item).strip()
        if not _CREDENTIAL_NAME_RE.match(name):
            raise MockServiceError(
                f"凭据只能声明成环境变量名（QIO_KEY_ 开头的大写名）：{item!r}"
            )
        if name not in names:
            names.append(name)
    return tuple(names)


@dataclass(frozen=True)
class MockServiceReport:
    """夹具在沙箱里跑完留下的报告：模拟了哪些接口、拦了哪些连接。"""

    simulated: bool
    verified_real_service: bool
    hosts: list[str]
    calls: list[dict]
    blocked: list[dict]
    credentials: list[str]
    note: str
    plan_error: str = ""

    def summary(self) -> str:
        matched = [call for call in self.calls if call.get("matched")]
        text = (
            "本次测试使用模拟服务："
            + ("、".join(self.hosts) if self.hosts else "（没有声明任何 host）")
            + f"，命中 {len(matched)} 次模拟响应，拦下 {len(self.blocked)} 条未声明的连接"
        )
        if self.credentials:
            text += "；凭据为明显的假值（" + "、".join(self.credentials) + "）"
        else:
            text += "；没有注入任何凭据"
        if self.plan_error:
            text += f"；注意：{self.plan_error}"
        text += "。没有访问真实服务，也没有使用真实凭据：测试通过不等于真实服务已验证。"
        return text


def read_report(path: str | Path) -> MockServiceReport | None:
    """读沙箱留下的报告；没有/读不动就返回 None（调用方要如实说「没拿到报告」）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return MockServiceReport(
        simulated=bool(data.get("simulated")),
        verified_real_service=bool(data.get("verified_real_service")),
        hosts=[str(item) for item in data.get("hosts") or []],
        calls=[item for item in data.get("calls") or [] if isinstance(item, dict)],
        blocked=[item for item in data.get("blocked") or [] if isinstance(item, dict)],
        credentials=[str(item) for item in data.get("credentials") or []],
        note=str(data.get("note") or ""),
        plan_error=str(data.get("plan_error") or ""),
    )


@dataclass(frozen=True)
class MockFixture:
    """一次测试要用的夹具：目录（sitecustomize + 运行时）、报告路径、环境变量。"""

    plan: MockServicePlan
    directory: Path
    report_path: Path

    def extra_env(self) -> dict[str, str]:
        """交给沙箱子进程的环境变量（沙箱只允许白名单 + 这些显式项）。"""
        return {
            "PYTHONPATH": str(self.directory),
            PLAN_ENV: str(self.directory / "plan.json"),
            REPORT_ENV: str(self.report_path),
        }

    def read_report(self) -> MockServiceReport | None:
        return read_report(self.report_path)

    def note_for_result(self) -> str:
        """给测试结论用的一句话：**必须**让模型看到这是模拟，不是真实服务验证。"""
        report = self.read_report()
        if report is None:
            hosts = "、".join(self.plan.hosts()) or "（没有声明 host）"
            return (
                f"（这次测试声明了模拟服务（{hosts}），但没有拿到模拟服务的运行报告："
                "不能据此说测试覆盖了真实服务。）"
            )
        return "（" + report.summary() + "）"

    def cleanup(self) -> None:
        """删掉这份夹具（含报告）；不删也不影响测试，只是临时目录里会多一份。"""
        shutil.rmtree(self.directory, ignore_errors=True)


def fixture_for_definition(
    definition: Any, *, workdir: str | Path | None = None
) -> MockFixture | None:
    """按工具定义里的 `qio-mocks.json` 建夹具；没有声明就返回 None。"""
    plan = MockServicePlan.from_definition(definition)
    if plan is None:
        return None
    return plan.bootstrap(workdir=workdir)


# 下面是真正跑在沙箱子进程里的夹具源码（标准库；由 sitecustomize 自动加载）。
# 它必须自包含：沙箱里没有 QIO 的包，也不能联网去装任何东西。

_RUNTIME_SOURCE = '''"""QIO 工具测试的模拟服务夹具（由 sitecustomize 自动加载，只在声明了 mocks 时生效）。

装上之后：

* **默认关闭网络**：任何没有声明的 host:port 都直接抛 MockServiceBlocked，连 DNS 都不做；
* 声明过的 host 被接到本机 127.0.0.1 的桩服务器：请求的 Host/路径/方法不变，响应来自
  声明里的固定内容（所以写死 URL 的工具也能被测）；
* 凭据只注入明显的假值（qio-mock-credential），真实凭据不会进这个进程；
* 进程退出时把「命中哪些模拟接口、拦掉哪些连接」写到 QIO_MOCK_REPORT，供上层如实说明
  「这次测试是模拟的，不等于真实服务验证过」。
"""

import atexit
import http.server
import json
import os
import socket
import threading
import time

PLAN_ENV = "QIO_MOCK_PLAN"
REPORT_ENV = "QIO_MOCK_REPORT"
FAKE_CREDENTIAL = "qio-mock-credential"


class MockServiceBlocked(OSError):
    """测试里默认没有网络：这条连接没有被声明为模拟服务。"""


def _load_plan():
    path = os.environ.get(PLAN_ENV) or ""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        # 声明读不出来：保持「没有声明任何服务」（等于默认拒绝一切连接），
        # 并把原因写进报告，让上层看得见，而不是悄悄放行网络。
        return {"services": [], "credentials": [], "error": "无法读取模拟服务声明：%s" % (exc,)}


class _State:
    def __init__(self):
        self.plan = _load_plan()
        self.services = {}
        for service in (self.plan or {}).get("services", []):
            host = str(service.get("host") or "").lower()
            if host:
                self.services[host] = service
        self.calls = []
        self.blocked = []
        self.credentials = []
        self.port = 0


STATE = _State()


def _find_route(host, method, path):
    service = STATE.services.get(str(host).lower())
    if service is None:
        return None
    wanted = str(path).split("?", 1)[0]
    for route in service.get("routes", []):
        candidate = str(route.get("path") or "/").split("?", 1)[0]
        if candidate == wanted and str(route.get("method") or "GET").upper() == str(method).upper():
            return route
    return None


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        # 不打访问日志：工具的结果通道是 stdout 上的一行 JSON，stderr 也不能被噪音塞满。
        pass

    def _serve(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        path = self.path
        route = _find_route(host, self.command, path)
        if route is None:
            STATE.calls.append({"host": host, "method": self.command, "path": path, "matched": False})
            payload = json.dumps(
                {"error": "没有声明这条模拟路由", "host": host, "path": path}, ensure_ascii=False
            ).encode("utf-8")
            self._respond(501, "application/json; charset=utf-8", payload)
            return
        STATE.calls.append(
            {
                "host": host,
                "method": self.command,
                "path": path,
                "matched": True,
                "status": int(route.get("status") or 200),
            }
        )
        delay = float(route.get("delay_ms") or 0) / 1000.0
        if delay > 0:
            time.sleep(delay)
        if "json" in route:
            payload = json.dumps(route["json"], ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            payload = str(route.get("text") or "").encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        self._respond(int(route.get("status") or 200), content_type, payload, route.get("headers"))

    def _respond(self, status, content_type, payload, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in (extra_headers or {}).items():
            self.send_header(str(key), str(value))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _serve


def _start_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    STATE.port = int(server.server_address[1])
    threading.Thread(
        target=server.serve_forever, name="qio-mock-service", daemon=True
    ).start()
    return server


def _host_of(address):
    if isinstance(address, tuple) and address:
        return str(address[0])
    return ""


def _port_of(address):
    if isinstance(address, tuple) and len(address) > 1:
        return address[1]
    return None


_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _is_loopback(host):
    # 本机环回放行：桩服务器就在 127.0.0.1 上，夹具自己也要用；
    # 「默认不联网」拦的是外网/真实服务，不是本机回环（受限子进程本来也不是安全边界）。
    return str(host).lower() in _LOOPBACK


def _deny(host, port):
    STATE.blocked.append({"host": host, "port": port, "reason": "默认关闭网络"})
    raise MockServiceBlocked(
        "工具测试默认不联网：%s:%s 没有被声明为模拟服务（在项目的 qio-mocks.json 里声明）"
        % (host, port)
    )


_real_create_connection = socket.create_connection
_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guard_create_connection(address, *args, **kwargs):
    host, port = _host_of(address), _port_of(address)
    if _is_loopback(host):
        return _real_create_connection(address, *args, **kwargs)
    if host.lower() not in STATE.services:
        _deny(host, port)
    return _real_create_connection(("127.0.0.1", STATE.port), *args, **kwargs)


def _guard_getaddrinfo(host, port, *args, **kwargs):
    if _is_loopback(host):
        return _real_getaddrinfo(host, port, *args, **kwargs)
    if str(host).lower() not in STATE.services:
        _deny(host, port)
    return _real_getaddrinfo("127.0.0.1", STATE.port, *args, **kwargs)


def _guard_connect(self, address):
    host = _host_of(address)
    if not host or _is_loopback(host):
        return _real_connect(self, address)
    if host.lower() not in STATE.services:
        _deny(host, _port_of(address))
    return _real_connect(self, ("127.0.0.1", STATE.port))


def _guard_connect_ex(self, address):
    host = _host_of(address)
    if not host or _is_loopback(host):
        return _real_connect_ex(self, address)
    if host.lower() not in STATE.services:
        _deny(host, _port_of(address))
    return _real_connect_ex(self, ("127.0.0.1", STATE.port))


def _neutralize_proxies():
    """模拟服务期间关掉系统代理自动发现（Windows 上 urllib 会读注册表代理）。

    不关掉的话，「模拟」的请求可能被本机代理（Clash 之类）转发到真实外网：
    既不是模拟，也把测试变成了真实网络调用（实测本机会因此拿到代理的 502）。
    """
    try:
        import urllib.request
    except ImportError:
        return
    urllib.request.getproxies = lambda: {}
    urllib.request.getproxies_environment = lambda: {}
    urllib.request.proxy_bypass = lambda host: True
    urllib.request.proxy_bypass_environment = lambda host, proxies=None: True
    urllib.request.proxy_bypass_registry = lambda host: True


def _write_report():
    path = os.environ.get(REPORT_ENV) or ""
    if not path:
        return
    payload = {
        "simulated": True,
        "verified_real_service": False,
        "hosts": sorted(STATE.services),
        "calls": STATE.calls,
        "blocked": STATE.blocked,
        "credentials": sorted(STATE.credentials),
        "plan_error": (STATE.plan or {}).get("error", ""),
        "note": (
            "本次测试使用模拟服务：没有访问真实服务，也没有使用真实凭据；"
            "测试通过只说明逻辑在模拟响应下成立，不等于真实服务已验证。"
        ),
    }
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def activate():
    if STATE.plan is None:
        return False
    for name in (STATE.plan or {}).get("credentials", []):
        os.environ[str(name)] = FAKE_CREDENTIAL
        STATE.credentials.append(str(name))
    _neutralize_proxies()
    _start_server()
    socket.create_connection = _guard_create_connection
    socket.getaddrinfo = _guard_getaddrinfo
    socket.socket.connect = _guard_connect
    socket.socket.connect_ex = _guard_connect_ex
    atexit.register(_write_report)
    return True


ACTIVE = activate()
'''

_SITECUSTOMIZE_SOURCE = '''"""sitecustomize：接上 QIO 的模拟服务夹具（只在设置了 QIO_MOCK_PLAN 时生效）。

sitecustomize 是解释器启动时由 site.py 自动导入的钩子，所以工具代码什么都不用改：
它照样发它写死的 HTTP 请求，夹具在本机把它接住（没有声明就一律拒绝）。
"""

import os

if os.environ.get("QIO_MOCK_PLAN"):
    # 导入即生效：安装拦截 + 起桩服务器。装不上就让测试失败，绝不悄悄放行网络。
    import qio_mock_service  # noqa: F401
'''
