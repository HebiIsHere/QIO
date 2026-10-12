"""会话模式真实链路的端到端媒体交付（契约 §4 要求 4 / 5）。

这一条用例走的是**真实流水线**：AttachmentService 绑定到 turn → TurnOrchestrator
的 build_context 解析本轮媒体 → AgentLoop 把内容块挂在首条 user 消息上 →
每一次模型调用都能看到它。断言的是「模型到底收到了什么」，
而不是「某个函数被调用过」。

本机没有 ffmpeg 时 skip 并说明（不假装通过）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import materials
from agent.services import media
from agent.services.app import AppContext
from agent.services.attachments import AttachmentService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

from tests.test_media_service import FFMPEG, real_video  # noqa: F401 - 复用真实素材夹具

needs_ffmpeg = pytest.mark.skipif(
    not FFMPEG,
    reason=(
        "这台机器上没有可用的 ffmpeg（探测顺序：QIO_FFMPEG → 应用资源目录 → PATH → "
        "常见安装位置），无法现场生成真实视频；本用例跳过，未假装通过"
    ),
)


class _CapturingAdapter:
    """记录每一次模型调用收到的消息（形状与既有假适配器一致）。"""

    mode = "native"
    model = "fake-capture"
    supports_stream = False
    supports_images = True
    supports_audio = False
    supports_video = False
    audio_transcription = None

    def __init__(self) -> None:
        self.calls: list[list[ChatMessage]] = []

    async def complete(self, messages, tools, **kwargs):
        self.calls.append(list(messages))
        return Completion(message=ChatMessage(role="assistant", content="看到了"))


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "media-e2e.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


@needs_ffmpeg
async def test_conversation_turn_delivers_sampled_frames_to_every_model_call(
    ctx: AppContext, tmp_path: Path, real_video: Path
):
    service = AttachmentService(ctx.conn, Path(ctx.settings.data_dir))
    ctx.services.register("attachments", service)

    topic = ctx.topics.nodes.create_topic("媒体").id
    tctx = ctx.turns.submit("看看这段视频", topic)
    att = service.prepare(str(real_video), topic_id=topic, turn_id=tctx.turn_id)
    service.run_prepare(att.id)

    adapter = _CapturingAdapter()
    ctx.build_adapter = lambda: _return(adapter)  # type: ignore[assignment]
    await ctx.turns.wait(tctx.turn_id, timeout=60)

    assert adapter.calls, "模型没有被调用"
    delivered = None
    for messages in adapter.calls:
        first = messages[0]
        assert first.role == "user"
        images = list(first.images or [])
        assert images, "每一次模型调用都必须带着本轮媒体依据"
        assert all(part.data_url.startswith("data:image/") for part in images)
        delivered = images
    assert delivered, "抽样帧没有交付"
    # 「不是逐帧」这句事实必须留在这一轮（材料事实台账）
    assert materials.MAX_MEDIA_SAMPLE_FRAMES >= len(delivered)

    # 本轮工作目录在收尾时被清干净（样本帧不留在数据目录里）
    work_root = Path(ctx.settings.data_dir) / "tmp" / f"media-{tctx.turn_id}"
    assert not work_root.exists()


@needs_ffmpeg
async def test_conversation_turn_with_audio_attaches_no_audio_block_without_capability(
    ctx: AppContext, tmp_path: Path, real_video: Path
):
    """链路没有音频能力：音频不当成内容块交付，也不编造转写文本。"""
    service = AttachmentService(ctx.conn, Path(ctx.settings.data_dir))
    ctx.services.register("attachments", service)

    topic = ctx.topics.nodes.create_topic("媒体2").id
    tctx = ctx.turns.submit("听听这段", topic)
    att = service.prepare(str(real_video), topic_id=topic, turn_id=tctx.turn_id)
    service.run_prepare(att.id)

    adapter = _CapturingAdapter()
    ctx.build_adapter = lambda: _return(adapter)  # type: ignore[assignment]
    await ctx.turns.wait(tctx.turn_id, timeout=60)

    for messages in adapter.calls:
        assert not messages[0].audios, "没有音频能力就不许把音频当内容块交付"


async def _return(value):
    return value


def test_resolve_media_never_claims_transcription_without_a_real_transcript(db_conn, tmp_path):
    """没有真实转写文本时，绝不产出 audio_transcribed（引擎与时段都不许凭空写）。"""
    service = AttachmentService(db_conn, tmp_path / "data")
    source = tmp_path / "voice.mp3"
    source.write_bytes(b"ID3\x03\x00" + b"\x00" * 64)
    att = service.run_prepare(
        service.prepare(str(source), topic_id="t1", turn_id="turn_audio").id
    )
    assert att.state == "ready"

    candidates = materials.collected_media_candidates(service, ["turn_audio"])
    assert len(candidates) == 1, candidates

    result = materials.resolve_media(
        candidates=candidates,
        audio_transcription="openai.audio.transcriptions",
    )
    entry = result["entries"][0]

    assert entry["source"] != "audio_transcribed"
    assert entry["read"] is False or entry["source"] == "audio_unsupported"
    assert entry.get("transcript") in (None, "")
    assert entry["engine"].get("provider") == "openai.audio.transcriptions"
