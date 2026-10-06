"""D 独立验证用的假厂商端点（OpenAI 兼容，**支持真 SSE 流式**）。

**这不是真实厂商验证。** 它是一个本地扮演的厂商端点：用它跑出来的结论只能读成
「QIO 自己的链路对」，不能证明任何真实厂商的可用性/兼容性/网络行为。

与既有 scripts/e2e_fake_provider.py 的关系：
* 那个脚本只返回整段 JSON（没有流式），验收清单第 3 条（provider 未结束前前端
  已有非空回答）用它**无法取证**；
* 本脚本是 D 的验证资产，专门补上流式分片、工具参数碎片、中途断流，
  并把每个分片的发送时刻记进时间线（用于「回答先于 provider 结束」的取证）。

用法：

    python scripts/verify_stream_provider.py --port 8798
    python scripts/verify_stream_provider.py --selftest       # 自检，非零即失败

控制面：

    GET  /__health               -> {"ok": true, "port": N}
    GET  /__log                  -> {"requests": [...]}            请求台账（密钥只留指纹）
    GET  /__timeline             -> {"timeline": [...]}            分片发送时刻
    POST /__script {"steps":[]}  -> 设定后续回复脚本（FIFO）
    POST /__reset                -> 清空台账与脚本
    GET  /v1/models              -> 模型列表
    POST /v1/chat/completions    -> 按脚本流式/整段回复

脚本步骤（FIFO，取完用 default）：

    {"chunks": ["第一段", "第二段"], "chunk_delay_ms": 40}   流式正文分片
    {"tool_chunks": [{"id": "call_1", "name": "echo",
                      "args_fragments": ["{\\"te", "xt\\": \\"hi\\"}"]}]}   工具参数碎片
    {"text": "整段回复"}                                      非流式 JSON
    {"status": 500, "body": "boom"}                           厂商故障
    {"abort_after": 2, "chunks": [...]}                       发 2 个分片后断开连接
    {"repeat": 3}                                             让同一步重复

安全：密钥只落 sha256 指纹 + 长度 + 末四位，原文既不落盘也不回显。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY = 8 * 1024 * 1024


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
        self.default: dict[str, Any] = {"chunks": ["（假模型默认回复）"], "chunk_delay_ms": 0}
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


def content_chunk(model: str, text: str, finish: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-verify",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def tool_chunk(
    model: str,
    *,
    index: int = 0,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
    finish: str | None = None,
    content: str | None = None,
) -> str:
    """OpenAI 形状的工具调用增量：首个分片带 id/name，之后只追加参数碎片。"""
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if call_id is not None or name is not None or arguments is not None:
        fn: dict[str, Any] = {}
        if name is not None:
            fn["name"] = name
        if arguments is not None:
            fn["arguments"] = arguments
        entry: dict[str, Any] = {"index": index, "type": "function", "function": fn}
        if call_id is not None:
            entry["id"] = call_id
        delta["tool_calls"] = [entry]
    payload = {
        "id": "chatcmpl-verify",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def usage_chunk(model: str, usage: dict[str, Any] | None = None) -> str:
    payload = {
        "id": "chatcmpl-verify",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": usage or {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "QIOVerifyStreamProvider/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass  # 静音：stdout 留给调用方

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

    def _mark(self, event: str, **extra: Any) -> None:
        self.server.timeline.append(
            {"event": event, "t": time.time(), "mono": time.perf_counter(), **extra}
        )

    def _begin_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk_out(self, text: str) -> None:
        raw = text.encode("utf-8")
        self.wfile.write(b"%x\r\n" % len(raw))
        self.wfile.write(raw)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_sse(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/__health":
            self._json({"ok": True, "port": self.server.server_port, "served": len(self.server.log)})
        elif path == "/__log":
            self._json({"requests": list(self.server.log)})
        elif path == "/__timeline":
            self._json({"timeline": list(self.server.timeline)})
        elif path == "/v1/models":
            self._json(
                {
                    "object": "list",
                    "data": [{"id": "verify-model", "object": "model", "owned_by": "qio-verify"}],
                }
            )
        elif path == "":
            self._json({"ok": True, "hint": "QIO verify streaming provider"})
        else:
            self._json({"error": {"message": "unknown path %s" % path}}, status=404)

    def do_POST(self) -> None:  # noqa: N802
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
            self.server.timeline.clear()
            self._json({"ok": True})
            return
        if path == "/v1/chat/completions":
            self._completion(body)
            return
        self._json({"error": {"message": "unknown path %s" % path}}, status=404)

    def _completion(self, body: dict[str, Any]) -> None:
        step = self.server.script.next()
        entry: dict[str, Any] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mono": time.perf_counter(),
            "path": self.path,
            "key": key_fingerprint(self.headers.get("Authorization")),
            "model": body.get("model"),
            "stream_requested": bool(body.get("stream")),
            "message_count": len(body.get("messages") or []),
            "step_kind": (
                "tool_chunks" if step.get("tool_chunks") else ("status" if step.get("status") else "text")
            ),
        }
        self.server.log.append(entry)
        if step.get("delay_ms"):
            time.sleep(min(30.0, float(step["delay_ms"]) / 1000.0))
        if step.get("status"):
            self._json(
                {"error": {"message": str(step.get("body") or "fake failure"), "type": "verify"}},
                status=int(step["status"]),
            )
            return

        delay = max(0.0, float(step.get("chunk_delay_ms") or 0) / 1000.0)
        abort_after = step.get("abort_after")
        model = str(body.get("model") or "verify-model")

        if not body.get("stream"):
            text = str(step.get("text") or "".join(step.get("chunks") or []))
            self._json(
                {
                    "id": "chatcmpl-verify",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
                }
            )
            return

        texts = [str(x) for x in (step.get("chunks") or [])]
        tools = list(step.get("tool_chunks") or [])
        self._begin_sse()
        self._mark("stream_start", model=model)
        sent = 0
        try:
            for piece in texts:
                if piece:
                    self._chunk_out(content_chunk(model, piece))
                    sent += 1
                    self._mark("chunk_sent", kind="content", chars=len(piece))
                if delay:
                    time.sleep(delay)
                if abort_after is not None and sent >= int(abort_after):
                    # 干净收束但内容不完整：传输层正常结束，缺 finish_reason。
                    # （hard_abort 才是真正的半截连接。）
                    self._mark("stream_aborted", after=sent)
                    if step.get("hard_abort"):
                        self.close_connection = True
                        return
                    self._end_sse()
                    return
            for call in tools:
                call_id = str(call.get("id") or "call_verify")
                name = str(call.get("name") or "echo")
                fragments = [str(f) for f in (call.get("args_fragments") or [])]
                self._chunk_out(tool_chunk(model, call_id=call_id, name=name, arguments=""))
                self._mark("chunk_sent", kind="tool_header", call_id=call_id)
                if delay:
                    time.sleep(delay)
                for fragment in fragments:
                    self._chunk_out(tool_chunk(model, arguments=fragment))
                    sent += 1
                    self._mark("chunk_sent", kind="tool_args", chars=len(fragment), call_id=call_id)
                    if delay:
                        time.sleep(delay)
                if abort_after is not None and sent >= int(abort_after):
                    self._mark("stream_aborted", after=sent)
                    if step.get("hard_abort"):
                        self.close_connection = True
                        return
                    self._end_sse()
                    return
            finish = "tool_calls" if tools else "stop"
            self._chunk_out(content_chunk(model, "", finish=finish))
            if (body.get("stream_options") or {}).get("include_usage"):
                self._chunk_out(usage_chunk(model))
            self._end_sse()
            self._mark("stream_end", model=model, chunks=sent, finish=finish)
        except (BrokenPipeError, ConnectionResetError):
            self._mark("stream_client_gone", after=sent)
            self.close_connection = True


class StreamingProvider(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], script: Script | None = None) -> None:
        super().__init__(addr, Handler)
        self.script = script or Script()
        self.log: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        self.lock = threading.Lock()


def _selftest() -> int:
    """自检：真起服务，用 httpx 消费 SSE，核对分片顺序/分时/工具碎片/断流/500/脱敏。"""
    import httpx

    server = StreamingProvider(("127.0.0.1", 0))
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    failures: list[str] = []

    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(
                base + "/__script",
                json={
                    "steps": [
                        {"chunks": ["你", "好", "世界"], "chunk_delay_ms": 30},
                        {
                            "tool_chunks": [
                                {
                                    "id": "call_1",
                                    "name": "echo",
                                    "args_fragments": ['{"te', 'xt": "hi"}'],
                                }
                            ],
                            "chunk_delay_ms": 10,
                        },
                        {"abort_after": 1, "chunks": ["半", "句", "永远发不出"]},
                        {"status": 500, "body": "boom"},
                    ]
                },
            )
            pieces: list[str] = []
            marks: list[float] = []
            with client.stream(
                "POST",
                base + "/v1/chat/completions",
                json={"model": "verify-model", "stream": True, "messages": []},
                headers={"Authorization": "Bearer sk-verify-selftest-0001"},
            ) as resp:
                if resp.status_code != 200:
                    failures.append("流式状态码 %d" % resp.status_code)
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = json.loads(line[6:])
                    delta = data["choices"][0]["delta"] if data.get("choices") else {}
                    if delta.get("content"):
                        pieces.append(delta["content"])
                        marks.append(time.perf_counter())
            if pieces != ["你", "好", "世界"]:
                failures.append("分片不齐: %r" % pieces)
            if len(marks) == 3 and (marks[-1] - marks[0]) < 0.05:
                failures.append("分片没有真的分时发送: %r" % marks)

            tool_fragments: list[str] = []
            with client.stream(
                "POST",
                base + "/v1/chat/completions",
                json={"model": "verify-model", "stream": True, "messages": []},
            ) as resp:
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = json.loads(line[6:])
                    if not data.get("choices"):
                        continue
                    for call in data["choices"][0]["delta"].get("tool_calls") or []:
                        tool_fragments.append(call["function"].get("arguments") or "")
            joined = "".join(tool_fragments)
            if json.loads(joined) != {"text": "hi"}:
                failures.append("工具参数碎片组装失败: %r" % joined)

            partial: list[str] = []
            try:
                with client.stream(
                    "POST",
                    base + "/v1/chat/completions",
                    json={"model": "verify-model", "stream": True, "messages": []},
                ) as resp:
                    for line in resp.iter_lines():
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            if data.get("choices"):
                                partial.append(data["choices"][0]["delta"].get("content") or "")
            except httpx.HTTPError as exc:
                failures.append("断流场景把客户端读挂了: %s" % exc)
            if partial != ["半"]:
                failures.append("断流场景分片不符: %r" % partial)

            try:
                client.post(
                    base + "/v1/chat/completions",
                    json={"model": "verify-model", "stream": True, "messages": []},
                ).raise_for_status()
                failures.append("500 场景没有失败")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 500:
                    failures.append("错误码不符: %d" % exc.response.status_code)

            timeline = client.get(base + "/__timeline").json()["timeline"]
            events = [item["event"] for item in timeline]
            for required in ("stream_start", "chunk_sent", "stream_end", "stream_aborted"):
                if required not in events:
                    failures.append("时间线缺 %s: %r" % (required, events))
            log = client.get(base + "/__log").json()["requests"]
            dumped = json.dumps(log, ensure_ascii=False)
            if "sk-verify-selftest-0001" in dumped:
                failures.append("日志里出现了密钥原文")
            if not any(item["key"].get("sha256_16") for item in log):
                failures.append("日志里没有密钥指纹")
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print("[FAIL] verify_stream_provider 自检失败：")
        for item in failures:
            print("  -", item)
        return 1
    print("[PASS] verify_stream_provider 自检通过：流式分片/工具碎片/断流/500/时间线/脱敏")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="QIO 验证用假厂商端点（支持 SSE 流式）")
    parser.add_argument("--port", type=int, default=8798)
    parser.add_argument("--script", help="启动时加载的脚本 JSON 文件")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return _selftest()
    steps = None
    if args.script:
        with open(args.script, "r", encoding="utf-8") as fh:
            steps = json.load(fh)
    server = StreamingProvider(("127.0.0.1", args.port), Script(steps))
    print("verify_stream_provider listening on http://127.0.0.1:%d" % server.server_port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
