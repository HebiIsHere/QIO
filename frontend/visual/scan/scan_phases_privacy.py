"""C2.2：phases 账本里会不会带路径 / 用户原文 / 密钥形态（敌意内容实测）。

用法（必须在 backend 目录下跑，它要 import agent.*）：
    $env:QIO_DATA_DIR=''; uv run --frozen python ../frontend/visual/scan/scan_phases_privacy.py

构造一轮「用户消息 + 工具参数 + 工具错误」里都带 Windows 路径与假密钥的 turn，
然后扫描 phases 子文档；同时打印同一行 final_preview 的对照结果。
不联网（假 adapter），也不读真实库。
"""
import asyncio
import json
import pathlib
import re
import sys
import tempfile

sys.path.insert(0, "src")

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import Tool, ToolResult

SECRET = "sk-LIVECANARY0123456789abcdef"
WINPATH = "C:\\Users\\someone\\Documents\\private\\plan.md"
PATH_RE = re.compile(r"[A-Za-z]:\\|[A-Za-z]:/")
SECRET_RE = re.compile(r"(?i)(sk-[A-Za-z0-9_\-]{6,}|bearer\s+[A-Za-z0-9._\-]+|api[_-]?key\s*[:=]|qio_key_)")


class Leaky(Tool):
    name = "probe_leak"
    description = "把路径与密钥写进结果与错误"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(
            ok=False,
            error="cannot read " + WINPATH + " with " + SECRET,
            content="path " + WINPATH,
        )


class Adapter:
    mode = "native"
    model = "fake"

    def __init__(self):
        self.n = 0

    async def complete(self, messages, tools, **kwargs):
        self.n += 1
        if self.n == 1:
            return Completion(message=ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="c1", name="probe_leak", arguments={"path": WINPATH, "api_key": SECRET})],
            ))
        return Completion(message=ChatMessage(role="assistant", content="完成 " + WINPATH))


async def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    conn = connect(tmp / "app.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    ctx.registry.register(Leaky())
    adapter = Adapter()

    async def build():
        return adapter

    ctx.build_adapter = build
    topic = ctx.topics.nodes.create_topic("敌意样本").id
    await ctx.run_turn("请读 " + WINPATH + "（key " + SECRET + "）", topic_id=topic)

    row = conn.execute(
        "SELECT phases, final_preview FROM turn_traces ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    phases_raw = row["phases"]
    print("phases 原始长度:", len(phases_raw))
    print("phases 含 WINPATH:", bool(PATH_RE.search(phases_raw)))
    print("phases 含 SECRET 形态:", bool(SECRET_RE.search(phases_raw)))
    print("phases 含用户原文片段:", ("请读 " in phases_raw))
    parsed = json.loads(phases_raw)
    print("阶段名:", sorted({s["name"] for s in parsed.get("spans", [])}))
    print("detail 取值:", sorted({str(s.get("detail")) for s in parsed.get("spans", [])}))
    print("notes:", parsed.get("notes"))
    print("对照 final_preview 含 WINPATH:", WINPATH in (row["final_preview"] or ""))


if __name__ == "__main__":
    asyncio.run(main())
