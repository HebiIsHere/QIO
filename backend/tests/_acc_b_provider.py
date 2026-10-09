"""B 组验证用的最小真实 HTTP/SSE 假厂商（不联网、不读任何密钥）。

只服务 tests/test_acc_b_*.py：同一台本地 HTTP 服务同时扮演

* OpenAI 兼容端点 POST /v1/chat/completions（真 SSE，chat.completion.chunk）；
* Anthropic 端点 POST /v1/messages（真 SSE，message_start / content_block_delta /
  message_delta / message_stop）。

脚本按 FIFO 消费，每个步骤决定这一条流怎么发：

    {"chunks": ["a", "b"], "finish": "stop"}      正常结束（带结束标记）
    {"chunks": ["a"], "abort": true}              发完已给分块后干净收束，**没有**结束标记
    {"chunks": ["a"], "hard_abort": true}         发完已给分块后直接掐断连接（传输错误）
    {"usage_only": true}                          只发一个 usage 分块，没有结束标记
    {"tool_chunks": [{"id": ..., "name": ..., "args_fragments": [...]}], "abort": true}
                                                  工具参数碎片发完就断（调用未结束）
    {"chunks": ["a"], "finish": "length"}         合法长度截断（OpenAI）
    {"chunks": ["a"], "finish": "max_tokens"}     合法长度截断（Anthropic）

Anthropic 步骤默认带 message_stop；设 {"abort": true} 时只发到分块结束（缺 message_stop）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class Script:
    """FIFO 脚本；耗尽后用 default（默认 = 空流且没有结束标记，绝不伪造完成）。"""

    def __init__(self, steps: list[dict[str, Any]] | None = None) -> None:
        self._steps = list(steps or [])
        self._lock = threading.Lock()
        self.default: dict[str, Any] = {"chunks": [], "abort": True}
        self.requests: list[dict[str, Any]] = []

    def set(self, steps: list[dict[str, Any]]) -> None:
        with self._lock:
            self._steps = list(steps)

    def next(self) -> dict[str, Any]:
        with self._lock:
            if self._steps:
                return dict(self._steps.pop(0))
        return dict(self.default)

    def remaining(self) -> int:
        with self._lock:
            return len(self._steps)


def _content_chunk(model: str, text: str, finish: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-accb",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _tool_chunk(
    model: str,
    *,
    index: int = 0,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> str:
    function: dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    entry: dict[str, Any] = {"index": index, "type": "function", "function": function}
    if call_id is not None:
        entry["id"] = call_id
    payload = {
        "id": "chatcmpl-accb",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": {"tool_calls": [entry]}, "finish_reason": None}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _usage_chunk(model: str) -> str:
    payload = {
        "id": "chatcmpl-accb",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _anthropic_line(event: str, payload: dict[str, Any]) -> str:
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(payload, ensure_ascii=False))


class _Handler(BaseHTTPRequestHandler):
    server_version = "QIOAccBProvider/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass

    def _begin_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _out(self, text: str) -> None:
        raw = text.encode("utf-8")
        self.wfile.write(b"%x\r\n" % len(raw))
        self.wfile.write(raw)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_sse(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            return {}

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        step = self.server.script.next()  # type: ignore[attr-defined]
        self.server.script.requests.append(  # type: ignore[attr-defined]
            {"path": self.path, "stream": bool(body.get("stream"))}
        )
        if step.get("status"):
            raw = json.dumps({"error": {"message": "fake failure"}}).encode()
            self.send_response(int(step["status"]))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.rstrip("/").endswith("/messages"):
            self._anthropic(step)
        else:
            self._openai(step)

    # -- OpenAI 兼容 -------------------------------------------------------

    def _openai(self, step: dict[str, Any]) -> None:
        model = "acc-b-native"
        self._begin_sse()
        for piece in step.get("chunks") or []:
            if piece:
                self._out(_content_chunk(model, str(piece)))
        for fragment in step.get("tool_chunks") or []:
            call_id = str(fragment.get("id") or "call_accb")
            name = str(fragment.get("name") or "echo")
            self._out(_tool_chunk(model, call_id=call_id, name=name, arguments=""))
            for piece in fragment.get("args_fragments") or []:
                self._out(_tool_chunk(model, arguments=str(piece)))
        if step.get("usage_only"):
            self._out(_usage_chunk(model))
            self._end_sse()
            return
        if step.get("abort"):
            self._end_sse()  # 干净收束但没有 finish_reason
            return
        if step.get("hard_abort"):
            self.close_connection = True  # 半截连接：客户端读到传输错误
            return
        finish = step.get("finish")
        if finish is None:
            finish = "tool_calls" if step.get("tool_chunks") else "stop"
        self._out(_content_chunk(model, "", finish=str(finish)))
        if step.get("usage"):
            self._out(_usage_chunk(model))
        self._end_sse()

    # -- Anthropic ---------------------------------------------------------

    def _anthropic(self, step: dict[str, Any]) -> None:
        self._begin_sse()
        self._out(
            _anthropic_line(
                "message_start",
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 5}, "stop_reason": None},
                },
            )
        )
        self._out(
            _anthropic_line(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            )
        )
        for piece in step.get("chunks") or []:
            if piece:
                self._out(
                    _anthropic_line(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": str(piece)},
                        },
                    )
                )
        if step.get("abort"):
            self._end_sse()  # 缺 message_stop
            return
        finish = str(step.get("finish") or "end_turn")
        self._out(
            _anthropic_line(
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": finish}, "usage": {"output_tokens": 3}},
            )
        )
        if step.get("message_stop", True):
            self._out(_anthropic_line("message_stop", {"type": "message_stop"}))
        self._end_sse()


class AccSseProvider(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], steps: list[dict[str, Any]] | None = None) -> None:
        super().__init__(addr, _Handler)
        self.script = Script(steps)

    def set(self, steps: list[dict[str, Any]]) -> None:
        self.script.set(steps)
