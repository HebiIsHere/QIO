"""D 端到端取证：真实 SSE 链路里的「回答是否先于 provider 结束到达」。

这不是单元测试，是对着**真实进程**取证：
* 假厂商端点（scripts/verify_stream_provider.py）真分片发送；
* 后端真跑一轮（POST /api/turns），真走 SSE（GET /api/events）；
* 脚本记录每个事件的墙钟时刻，再与 provider 的分片时间线对比。

结论只能读成「QIO 自己的链路对」：假厂商是本机扮演的，不证明任何真实厂商行为。

用法（阶段二，在集成分支上；后端与 provider 先起来）：

    python scripts/verify_sse_capture.py \
        --base http://127.0.0.1:8734 \
        --provider http://127.0.0.1:8798 \
        --message "请分多次回答" \
        --chunks 20 --chunk-delay-ms 45 \
        --out docs/verification-e2e-stream.json

退出码：0 全部通过；1 有失败项（失败项会打印在同一条 JSON 里）。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

DEFAULT_CHUNKS = ["第%02d段。" % i for i in range(1, 21)]


def _request(url: str, *, payload: dict | None = None, timeout: float = 30.0) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


class SseCapture:
    """后台线程读 SSE；每条事件记墙钟时刻。进程退出时线程随之结束。"""

    def __init__(self, base: str) -> None:
        self.base = base
        self.events: list[dict] = []
        self.error: str | None = None
        self.ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        self.ready.wait(timeout=10)

    def _run(self) -> None:
        try:
            request = urllib.request.Request(self.base.rstrip("/") + "/api/events")
            with urllib.request.urlopen(request, timeout=600) as resp:
                self.ready.set()
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    self.events.append(
                        {"t": time.time(), "mono": time.perf_counter(), "event": event}
                    )
        except Exception as exc:  # noqa: BLE001 - 取证脚本：失败要如实写下
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self.ready.set()

    def wait_for(self, predicate, timeout: float) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for item in list(self.events):
                if predicate(item["event"]):
                    return item
            time.sleep(0.02)
        return None


def _assistant_text(event: dict) -> str:
    return str((event.get("data") or {}).get("content") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="QIO 真实 SSE 链路取证（假厂商）")
    parser.add_argument("--base", default="http://127.0.0.1:8734")
    parser.add_argument("--provider", default="http://127.0.0.1:8798")
    parser.add_argument("--message", default="请分多次回答，讲清楚每一步。")
    parser.add_argument("--chunks", type=int, default=len(DEFAULT_CHUNKS))
    parser.add_argument("--chunk-delay-ms", type=int, default=45)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    chunks = (DEFAULT_CHUNKS * 10)[: max(2, args.chunks)]
    provider = args.provider.rstrip("/")

    _request(provider + "/__reset", payload={})
    _request(
        provider + "/__script",
        payload={"steps": [{"chunks": chunks, "chunk_delay_ms": args.chunk_delay_ms}]},
    )

    capture = SseCapture(args.base)
    capture.start()

    started = time.time()
    turn = _request(args.base.rstrip("/") + "/api/turns", payload={"message": args.message})
    turn_id = str(turn.get("turn_id") or "")

    ended = capture.wait_for(
        lambda event: event.get("type") == "TURN_END"
        and (not turn_id or str((event.get("data") or {}).get("turn_id")) == turn_id),
        args.timeout,
    )

    timeline = _request(provider + "/__timeline").get("timeline") or []
    provider_end = next((item for item in timeline if item["event"] == "stream_end"), None)
    provider_chunks = [item for item in timeline if item["event"] == "chunk_sent"]

    assistant = [
        item
        for item in capture.events
        if item["event"].get("type") == "ASSISTANT" and _assistant_text(item["event"]).strip()
    ]
    answers = [item for item in assistant if not (item["event"].get("data") or {}).get("interim")]
    first_answer = answers[0] if answers else None
    final_content = str(((ended or {}).get("event", {}).get("data") or {}).get("final_content") or "")
    full_text = "".join(chunks)

    checks: list[dict] = []

    def check(name: str, ok: bool, detail) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check(
        "provider 真的分片发送",
        len(provider_chunks) >= 2 and provider_end is not None,
        {"chunks": len(provider_chunks), "stream_end": bool(provider_end)},
    )
    check(
        "SSE 收到非空正式回答增量",
        bool(answers),
        {"assistant_events": len(assistant), "answer_events": len(answers)},
    )
    if first_answer is not None and provider_end is not None:
        delta_ms = round((provider_end["t"] - first_answer["t"]) * 1000)
        check(
            "回答在 provider 结束之前就到了",
            first_answer["t"] < provider_end["t"],
            {
                "first_answer_lead_ms": delta_ms,
                "first_answer_latency_ms": round((first_answer["t"] - started) * 1000),
                "provider_stream_end_lag_ms": round((provider_end["t"] - started) * 1000),
            },
        )
        check(
            "增量是累计快照（单调不回退）",
            all(
                b["event"]["data"]["content"].startswith(a["event"]["data"]["content"])
                for a, b in zip(answers, answers[1:])
            ),
            [a["event"]["data"]["content"] for a in answers],
        )
    check(
        "TURN_END 的最终全文与 provider 发出的完全一致",
        final_content == full_text,
        {"final_len": len(final_content), "provider_len": len(full_text)},
    )
    check(
        "SSE 全流里没有出现重复的完整回答",
        sum(1 for item in answers if _assistant_text(item["event"]) == full_text) <= 1,
        None,
    )

    report = {
        "base": args.base,
        "provider": provider,
        "turn_id": turn_id,
        "message": args.message,
        "provider_chunks": len(provider_chunks),
        "sse_events": len(capture.events),
        "sse_error": capture.error,
        "turn_end_status": (((ended or {}).get("event", {}).get("data") or {}).get("status")),
        "checks": checks,
        "failed": [item["name"] for item in checks if not item["ok"]],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("evidence written: %s" % path)
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
