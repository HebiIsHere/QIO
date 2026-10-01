"""离线 E2E 用的假模型服务（OpenAI 兼容），**不需要真实 API Key、不联网**。

**它不是真实服务验证。** 这个服务是本地扮演的厂商端点：用它跑出来的结论只能读成
「QIO 自己的链路对」，**不能**证明任何真实厂商的可用性、兼容性或网络行为；
验收记录里凡是用到它的用例，都必须标明是假厂商。

为什么需要它：真实应用端到端验收（审批出队、工具卡推进、已核对标记、无进展暂停、
凭据改地址）都要求「真的有一轮模型对话」，而项目铁律禁止用真实 Key。
这里起一个本地 HTTP 服务扮演厂商端点，按脚本返回文本或工具调用，
并把**收到的每个请求**记录成可断言的证据。

安全：日志里**不出现密钥原文**。密钥只落 sha256 指纹 + 长度 + 末四位，
足以断言「这一枪是旧 Key 还是新 Key」，但无法还原。

用法：

    python scripts/e2e_fake_provider.py --port 8799

控制面（都在同一个端口上，方便测试直接问）：

    GET  /__health                  -> {"ok": true, "port": N, "served": n}
    GET  /__log                     -> {"requests": [...]}  收到的请求（脱敏）
    POST /__script  {"steps":[...]} -> 设定后续回复脚本（FIFO）
    POST /__reset                   -> 清空日志与脚本
    GET  /v1/models                 -> 模型列表
    POST /v1/chat/completions       -> 按脚本回复

脚本步骤（FIFO，取完用 default）：

    {"text": "回复文本"}
    {"tool": "tool_name", "args": {...}}
    {"tool_calls": [{"name": "...", "args": {...}}, ...]}
    {"status": 500, "body": "..."}      # 模拟厂商故障
    {"delay_ms": 3000, "text": "..."}   # 模拟慢响应
    {"repeat": 3}                       # 让同一步重复 3 次

自检：

    python scripts/e2e_fake_provider.py --selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY = 4 * 1024 * 1024


def key_fingerprint(value: str | None) -> dict[str, Any]:
    """把密钥压成可断言的指纹；原文既不落盘也不回显。"""
    raw = (value or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        return {"present": False}
    return {
        "present": True,
        "sha256_16": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        "length": len(raw),
        "tail": raw[-4:],
    }


class Script:
    """FIFO 回复脚本；repeat 让同一步可以重复若干次。"""

    def __init__(self, steps: list[dict[str, Any]] | None = None) -> None:
        self._steps: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.default: dict[str, Any] = {"text": "（假模型默认回复）"}
        self.set(steps or [])

    def set(self, steps: list[dict[str, Any]]) -> None:
        expanded: list[dict[str, Any]] = []
        for step in steps:
            repeat = int(step.get("repeat") or 1)
            for _ in range(max(1, repeat)):
                expanded.append({k: v for k, v in step.items() if k != "repeat"})
        with self._lock:
            self._steps = expanded

    def next(self) -> dict[str, Any]:
        with self._lock:
            if self._steps:
                return dict(self._steps.pop(0))
        return dict(self.default)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._steps)


def completion_body(step: dict[str, Any], model: str) -> dict[str, Any]:
    """把一步脚本渲染成 OpenAI ChatCompletion JSON。"""
    calls = step.get("tool_calls")
    if calls is None and step.get("tool"):
        calls = [{"name": step["tool"], "args": step.get("args") or {}}]
    message: dict[str, Any] = {"role": "assistant"}
    if calls:
        message["content"] = step.get("text") or None
        message["tool_calls"] = [
            {
                "id": "call_%d_%d" % (i, int(time.time() * 1000)),
                "type": "function",
                "function": {
                    "name": c.get("name") or c.get("tool") or "",
                    "arguments": json.dumps(c.get("args") or {}, ensure_ascii=False),
                },
            }
            for i, c in enumerate(calls)
        ]
        finish = "tool_calls"
    else:
        message["content"] = step.get("text") or ""
        finish = "stop"
    return {
        "id": "chatcmpl-fake-%d" % int(time.time() * 1000),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "QIOFakeProvider/1.0"
    protocol_version = "HTTP/1.1"

    def _json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            return {}

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # 静音：stdout 留给控制台证据

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/__health":
            self._json({"ok": True, "port": self.server.server_port, "served": len(self.server.log)})
        elif path == "/__log":
            self._json({"requests": list(self.server.log)})
        elif path == "/v1/models":
            self._record("GET", None)
            self._json(
                {
                    "object": "list",
                    "data": [
                        {"id": "fake-model", "object": "model", "owned_by": "qio-e2e"},
                        {"id": "fake-model-pro", "object": "model", "owned_by": "qio-e2e"},
                    ],
                }
            )
        elif path == "":
            self._json({"ok": True, "hint": "QIO fake provider"})
        else:
            self._record("GET", None)
            self._json({"error": {"message": "unknown path %s" % path}}, status=404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        body = self._read_body()
        if path == "/__script":
            self.server.script.set(body.get("steps") or [])
            if "default" in body:
                self.server.script.default = body["default"]
            self._json({"ok": True, "remaining": len(self.server.script.snapshot())})
            return
        if path == "/__reset":
            self.server.script.set([])
            self.server.log.clear()
            self._json({"ok": True})
            return
        if path == "/v1/chat/completions":
            entry = self._record("POST", body)
            step = self.server.script.next()
            entry["step"] = {k: v for k, v in step.items() if k != "text"}
            if step.get("delay_ms"):
                time.sleep(min(30.0, float(step["delay_ms"]) / 1000.0))
            if step.get("status"):
                self._json(
                    {"error": {"message": str(step.get("body") or "fake failure"), "type": "fake_error"}},
                    status=int(step["status"]),
                )
                return
            model = str(body.get("model") or "fake-model")
            self._json(completion_body(step, model))
            return
        self._record("POST", body)
        self._json({"error": {"message": "unknown path %s" % path}}, status=404)

    def _record(self, method: str, body: dict[str, Any] | None) -> dict[str, Any]:
        messages = list((body or {}).get("messages") or [])
        tools = [t.get("function", {}).get("name") for t in ((body or {}).get("tools") or [])]
        entry: dict[str, Any] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "method": method,
            "path": self.path,
            "key": key_fingerprint(self.headers.get("Authorization")),
            "model": (body or {}).get("model"),
            "message_count": len(messages),
            "tool_names_offered": tools,
            "last_user": next(
                (str(m.get("content"))[:200] for m in reversed(messages) if m.get("role") == "user"),
                None,
            ),
            "last_tool_result": next(
                (str(m.get("content"))[:200] for m in reversed(messages) if m.get("role") == "tool"),
                None,
            ),
        }
        self.server.log.append(entry)
        return entry


class FakeProvider(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], script: Script | None = None) -> None:
        super().__init__(addr, Handler)
        self.script = script or Script()
        self.log: list[dict[str, Any]] = []


def _selftest() -> int:
    """自检：起服务、走一遍文本/工具调用/错误/控制面，返回非零即失败。"""
    server = FakeProvider(("127.0.0.1", 0))
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    failures: list[str] = []

    def post(path: str, payload: dict[str, Any], key: str | None = "sk-selftest-0001") -> dict[str, Any]:
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if key:
            request.add_header("Authorization", "Bearer " + key)
        with urllib.request.urlopen(request, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get(path: str) -> dict[str, Any]:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        post("/__script", {"steps": [{"text": "你好"}, {"tool": "list_dir", "args": {"path": "."}}]})
        first = post("/v1/chat/completions", {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]})
        if first["choices"][0]["message"]["content"] != "你好":
            failures.append("文本回复不符: %s" % first)
        second = post("/v1/chat/completions", {"model": "fake-model", "messages": [{"role": "user", "content": "跑"}]})
        calls = second["choices"][0]["message"].get("tool_calls") or []
        if not calls or calls[0]["function"]["name"] != "list_dir":
            failures.append("工具调用不符: %s" % second)
        if second["choices"][0]["finish_reason"] != "tool_calls":
            failures.append("finish_reason 不是 tool_calls")
        third = post("/v1/chat/completions", {"model": "fake-model", "messages": []})
        if third["choices"][0]["message"]["content"] != "（假模型默认回复）":
            failures.append("默认回复不符: %s" % third)
        log = get("/__log")["requests"]
        if len(log) != 3:
            failures.append("日志条数不符: %d" % len(log))
        key = log[0]["key"]
        if key.get("sha256_16") != hashlib.sha256(b"sk-selftest-0001").hexdigest()[:16]:
            failures.append("密钥指纹不符: %s" % key)
        if "sk-selftest-0001" in json.dumps(log, ensure_ascii=False):
            failures.append("日志里出现了密钥原文")
        post("/__script", {"steps": [{"status": 500, "body": "boom"}]})
        try:
            post("/v1/chat/completions", {"model": "fake-model", "messages": []})
            failures.append("500 场景没有抛错")
        except urllib.error.HTTPError as exc:
            if exc.code != 500:
                failures.append("错误码不符: %d" % exc.code)
        if get("/__health")["ok"] is not True:
            failures.append("健康检查失败")
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print("[FAIL] 自检失败：")
        for item in failures:
            print("  -", item)
        return 1
    print("[PASS] 自检通过：文本/工具调用/默认回复/日志脱敏/500/健康检查")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="QIO 离线 E2E 假模型服务")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--script", help="启动时加载的脚本 JSON 文件")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return _selftest()

    steps: list[dict[str, Any]] = []
    if args.script:
        with open(args.script, encoding="utf-8") as fh:
            steps = json.load(fh)
    server = FakeProvider(("127.0.0.1", args.port), Script(steps))
    print("fake provider listening on http://127.0.0.1:%d/v1" % server.server_port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
