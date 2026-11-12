"""E2E 专用假厂商：包一层 scripts/verify_stream_provider.py，额外把**请求正文**记进台账。

为什么需要：共享的 verify_stream_provider.py 只记元数据（model/stream/tool_count），
验收 ①/② 必须断言「参与项正文进了模型请求、排除项/隐藏项/未提交项没进去、图片是否以
image_url 交付」，没有正文就无法取证。这里不复制/不修改共享脚本，只 import 后子类化
Handler，把原始 body 附在同一条台账记录上（密钥仍只留指纹，正文按 2MB 截断）。

用法：python scripts/verify-pm-qio032/fake_provider.py --port 8807
控制面与原脚本完全一致：/__health /__log /__script /__reset /v1/models。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_stream_provider as base  # noqa: E402

MAX_BODY_CHARS = 2_000_000


class RecordingHandler(base.Handler):
    def _completion(self, body):
        start = time.perf_counter()
        super()._completion(body)
        # 并发很少（E2E 顺序提交）：按 mono 找到本次请求在台账里的条目再附正文。
        try:
            raw = json.dumps(body, ensure_ascii=False)
        except Exception:
            raw = str(body)
        truncated = len(raw) > MAX_BODY_CHARS
        for entry in reversed(self.server.log):
            if entry.get("mono", 0) >= start and "_body" not in entry:
                entry["_body"] = raw[:MAX_BODY_CHARS]
                entry["body_truncated"] = truncated
                entry["message_roles"] = [
                    (m or {}).get("role") for m in (body.get("messages") or [])
                ]
                break


class RecordingProvider(base.StreamingProvider):
    def __init__(self, addr, script=None):
        super().__init__(addr, script)
        self.RequestHandlerClass = RecordingHandler


def main() -> int:
    parser = argparse.ArgumentParser(description="QIO E2E 假厂商（记录请求正文）")
    parser.add_argument("--port", type=int, default=8807)
    parser.add_argument("--script", help="启动时加载的脚本 JSON 文件")
    args = parser.parse_args()
    steps = None
    if args.script:
        with open(args.script, "r", encoding="utf-8") as fh:
            steps = json.load(fh)
    server = RecordingProvider(("127.0.0.1", args.port), base.Script(steps))
    print("fake_provider (recording) listening on http://127.0.0.1:%d" % server.server_port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
