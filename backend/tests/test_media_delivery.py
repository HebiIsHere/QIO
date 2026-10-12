"""多媒体交付的诚实事实（契约 §4.2 / §4.4 / §4.5）+ 后续步骤仍带着材料。

判据只有两条：

1. **能力协商**：适配器声明是唯一事实来源。没有音频/视频能力就如实降级
   （`audio_unsupported` / `video_unsupported`），原因必须具体到
   「这台机器上没有可用的转换程序，也没有随包提供」，绝不静默换成用户没配置的外部服务，
   也绝不因为「接口能接受某种消息」就当成「所选模型一定理解」；
2. **诚实事实**：每条材料都带 `read` / `reason` / `range` / `frames` / `engine`；
   抽样帧带**真实时间位置**且明确「不是逐帧」；转写文本只代表实际得到的文字，
   不据此声称理解语气、音乐或全部环境声音。

真实用例：本机有 ffmpeg 时用真视频真音频（现场生成到 pytest tmp 目录）；
没有 ffmpeg 时 skip 并说明，不假装通过。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.adapters.base import AudioPart, ImagePart, VideoPart
from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.server import EventBus
from agent.core.loop import AgentLoop
from agent.interactive import materials, models
from agent.services import media
from agent.services.attachments import AttachmentService
from agent.tools.attachment_tools import ReadAttachmentTool
from agent.tools.builtin import EchoTool
from agent.tools.registry import ToolRegistry

from tests.test_media_service import FFMPEG, real_audio, real_video  # noqa: F401 - 复用真实素材夹具

BOARD = "board_media"
needs_ffmpeg = pytest.mark.skipif(
    not FFMPEG,
    reason=(
        "这台机器上没有可用的 ffmpeg（探测顺序：QIO_FFMPEG → 应用资源目录 → PATH → "
        "常见安装位置），无法现场生成真实音视频；本用例跳过，未假装通过"
    ),
)


@pytest.fixture()
def svc(db_conn: sqlite3.Connection, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _ready(svc: AttachmentService, tmp_path: Path, name: str, body: bytes):
    source = tmp_path / f"m-src-{name}"
    source.write_bytes(body)
    att = svc.run_prepare(svc.prepare(str(source), topic_id="t1").id)
    assert att.state == "ready", (att.state, att.error)
    return att


def _from_file(svc: AttachmentService, path: Path, name: str):
    source = path.parent / name
    if source != path:
        source.write_bytes(path.read_bytes())
    att = svc.run_prepare(svc.prepare(str(source), topic_id="t1").id)
    assert att.state == "ready", (att.state, att.error)
    return att


def _state(*cards) -> dict:
    state = models.empty_state(BOARD)
    state["cards"] = list(cards)
    return state


def _file_card(att) -> dict:
    return models.new_card(
        "file", "", meta={"name": att.original_name, "file": {"name": att.original_name, "attachmentId": att.id}}
    )


def _entries(db_conn, cards, svc, **kwargs):
    state = _state(*cards)
    return materials.resolve_media(
        db_conn, state=state, cards=list(cards), attachments=svc, **kwargs
    )


def _by_card(result: dict, card: dict) -> dict:
    return [e for e in result["entries"] if e["cardId"] == card["id"]][0]


# ---------------------------------------------------------------------------
# 1. 音频：没有转写通道就如实说没有
# ---------------------------------------------------------------------------


def test_audio_without_any_channel_is_unsupported_with_a_specific_reason(db_conn, svc, tmp_path):
    att = _ready(svc, tmp_path, "tone.mp3", b"ID3\x03\x00" + b"\x00" * 64)
    card = _file_card(att)

    result = _entries(db_conn, [card], svc)
    entry = _by_card(result, card)

    assert entry["source"] == "audio_unsupported"
    assert entry["read"] is False
    assert entry["reason"]
    # 没有音频能力、也没有转写通道：原因必须说清是哪一条缺了
    assert "音频" in entry["reason"]
    assert result["parts"] == []


def test_audio_unsupported_when_the_machine_has_no_converter(db_conn, svc, tmp_path, monkeypatch):
    """没有 ffmpeg 时，转写通道即使存在也必须说「这台机器上没有转换程序」。"""
    monkeypatch.setattr(media, "ffmpeg_path", lambda: (None, media.NO_FFMPEG_REASON))
    att = _ready(svc, tmp_path, "tone.wav", b"RIFF" + b"\x00" * 64)
    card = _file_card(att)

    entry = _by_card(_entries(db_conn, [card], svc, audio_transcription="openai.audio.transcriptions"), card)

    assert entry["source"] == "audio_unsupported"
    assert media.NO_FFMPEG_REASON in entry["reason"]
    assert "转换程序" in entry["reason"]


def test_audio_delivered_when_the_route_really_has_audio(db_conn, svc, tmp_path):
    """适配器声明了 bytes 能力：材料层如实说「音频字节已交给模型」，并给 range。"""
    att = _ready(svc, tmp_path, "tone.wav", b"RIFF" + b"\x00" * 64)
    card = _file_card(att)

    result = _entries(db_conn, [card], svc, supports_audio=True)
    entry = _by_card(result, card)

    assert entry["source"] == "audio_delivered"
    assert entry["read"] is True
    assert entry["range"] == {"start": None, "end": None}
    assert entry["frames"] == []
    assert entry["engine"] == {}  # 没有派生处理，就没有「用了谁」
    assert len(result["parts"]) == 1 and isinstance(result["parts"][0], AudioPart)
    assert result["parts"][0].data_url.startswith("data:audio/")


# ---------------------------------------------------------------------------
# 2. 视频：抽样帧交付，或如实说不可用
# ---------------------------------------------------------------------------


@needs_ffmpeg
def test_video_is_sampled_with_real_frames_and_positions(db_conn, svc, tmp_path, real_video: Path):
    att = _from_file(svc, real_video, "clip.mp4")
    card = _file_card(att)
    work = tmp_path / "work"

    result = _entries(
        db_conn, [card], svc, supports_images=True, supports_video=False, work_dir=work
    )
    entry = _by_card(result, card)

    assert entry["source"] == "video_sampled"
    assert entry["read"] is True
    assert entry["engine"], "派生处理用了谁必须写清楚（这里是本机 ffmpeg）"
    assert entry["engine"]["provider"] == "ffmpeg"
    assert 1.5 <= entry["range"]["end"] <= 2.6
    frames = entry["frames"]
    assert 1 <= len(frames) <= materials.MAX_MEDIA_SAMPLE_FRAMES
    times = [float(f["at"]) for f in frames]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert "不是逐帧" in entry["notes"][0] or any("不是逐帧" in str(n) for n in entry["notes"])
    # 抽样帧作为图片内容块交付（走图片能力），并且只写在本轮工作目录
    assert result["parts"], "抽样帧要真的能交给模型"
    for part in result["parts"]:
        assert isinstance(part, ImagePart)
        assert part.data_url.startswith("data:image/")
    assert work.is_dir()
    # 交付给模型的是内容块本身；帧文件只写在本轮工作目录（这里逐张核对）
    jpegs = sorted(work.rglob("frame_*.jpg"))
    assert len(jpegs) == len(result["parts"]), "每一帧都必须在工作目录里，且不多不少"
    for frame in frames:
        assert set(frame) == {"at"}, "frames 只有时间位置（契约 §4.4）"


@needs_ffmpeg
def test_video_without_image_capability_is_unsupported(db_conn, svc, tmp_path, real_video: Path):
    """链路连图片都看不了：抽样帧也交不出去，如实说不可用（而不是假装交付）。"""
    att = _from_file(svc, real_video, "clip2.mp4")
    card = _file_card(att)

    result = _entries(db_conn, [card], svc, supports_images=False, work_dir=tmp_path / "w2")
    entry = _by_card(result, card)

    assert entry["source"] == "video_unsupported"
    assert entry["read"] is False
    assert "图片" in entry["reason"] or "视觉" in entry["reason"]
    assert result["parts"] == []


def test_video_without_converter_explains_the_specific_missing_piece(db_conn, svc, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_path", lambda: (None, media.NO_FFMPEG_REASON))
    att = _ready(svc, tmp_path, "clip3.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    card = _file_card(att)

    entry = _by_card(_entries(db_conn, [card], svc, work_dir=tmp_path / "w3"), card)

    assert entry["source"] == "video_unsupported"
    assert media.NO_FFMPEG_REASON in entry["reason"]
    assert "转换程序" in entry["reason"]


def test_sample_frame_cap_is_written_into_the_fact(db_conn, svc, tmp_path, monkeypatch):
    """请求超过上限：事实里如实写下上限，绝不默默截断。"""
    monkeypatch.setattr(media, "ffmpeg_path", lambda: (None, media.NO_FFMPEG_REASON))
    att = _ready(svc, tmp_path, "clip4.mp4", b"\x00" * 32)
    card = _file_card(att)

    entry = _by_card(
        _entries(db_conn, [card], svc, max_frames=99, work_dir=tmp_path / "w4"), card
    )
    assert str(materials.MAX_MEDIA_SAMPLE_FRAMES) in entry["reason"]


# ---------------------------------------------------------------------------
# 3. read_attachment 工具：音视频分支如实返回
# ---------------------------------------------------------------------------


async def test_tool_reports_audio_video_facts_without_ffmpeg(svc, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_path", lambda: (None, media.NO_FFMPEG_REASON))
    audio = _ready(svc, tmp_path, "voice.mp3", b"ID3\x03\x00" + b"\x00" * 64)
    video = _ready(svc, tmp_path, "movie.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    tool = ReadAttachmentTool(svc, active_turn_id=lambda: "turn_1")

    for att, kind in ((audio, "audio"), (video, "video")):
        result = await tool.run(attachment_id=att.id)
        assert result.ok is True
        payload = json.loads(result.content)
        assert payload["readable"] is False
        assert payload["media_kind"] == kind
        assert media.NO_FFMPEG_REASON in payload["reason"]
        assert payload["content"] == ""


@needs_ffmpeg
async def test_tool_reads_video_metadata_counts_and_samples(svc, tmp_path, real_video: Path):
    att = _from_file(svc, real_video, "movie2.mp4")
    tool = ReadAttachmentTool(svc, active_turn_id=lambda: "turn_1")

    result = await tool.run(attachment_id=att.id)
    payload = json.loads(result.content)

    assert payload["readable"] is True
    assert payload["media"]["hasVideo"] is True
    assert payload["media"]["width"] == 320
    assert "不是逐帧" in " ".join(payload["notes"])


@needs_ffmpeg
async def test_tool_audio_reads_a_real_transcript_only_when_it_really_has_one(svc, tmp_path, real_audio: Path):
    """没有转写通道：音频只给元数据与「没有文字」的事实，绝不编造转写文本。"""
    att = _from_file(svc, real_audio, "voice2.wav")
    tool = ReadAttachmentTool(svc, active_turn_id=lambda: "turn_1")

    result = await tool.run(attachment_id=att.id)
    payload = json.loads(result.content)

    assert payload["readable"] is True
    assert payload["media"]["hasAudio"] is True
    assert payload["media"]["duration"] is not None
    assert payload.get("transcript") in (None, "")
    assert "转写" in " ".join(payload["notes"])


# ---------------------------------------------------------------------------
# 4. 后续步骤仍带着本轮材料（契约 §4 要求 5）
# ---------------------------------------------------------------------------


def _media_parts():
    return [
        ImagePart(data_url="data:image/jpeg;base64,AAAA", label="c1", media_type="image/jpeg", bytes=3),
        AudioPart(data_url="data:audio/wav;base64,BBBB", label="c2", media_type="audio/wav", bytes=3),
        VideoPart(data_url="data:video/mp4;base64,CCCC", label="c3", media_type="video/mp4", bytes=3),
    ]


def _loop_with_echo(adapter, **kwargs) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentLoop(adapter, registry, EventBus(), turn_id="turn_media", **kwargs)


async def test_media_parts_reach_every_model_call_in_the_turn():
    """第 1 次之后**需要再次调用模型**的步骤里仍然带着本轮媒体依据（契约要求 5）。

    脚本：第 1 次先调一次工具（必然导致第 2 次模型调用），第 2 次给正文。
    断言两次请求的首条 user 消息里都挂着同一份媒体内容块 —— 不是只在第一次带。
    """
    parts = _media_parts()
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text="先看一眼。",
                tool_calls=[
                    ScriptedToolCall(id="c1", name="echo", arguments={"text": "material"})
                ],
            ),
            StreamScript(text="看完了，这是回答。"),
        ],
    )
    loop = _loop_with_echo(adapter, media_parts=parts)
    result = await loop.run("看看这个")
    assert result is not None

    calls = adapter.requests
    assert len(calls) >= 2, "这个脚本必须真的发生两次模型调用，才能证明「后续步骤」"
    for call in calls:
        first = call["messages"][0]
        assert first.role == "user"
        assert [p.label for p in (first.images or [])] == ["c1"]
        assert [p.label for p in (first.audios or [])] == ["c2"]
        assert [p.label for p in (first.videos or [])] == ["c3"]


async def test_media_parts_survive_a_second_turn_on_a_reused_loop():
    """同一个 loop 跑第二轮：上一轮的材料**不许**泄漏到下一轮（材料属于轮）。"""
    adapter = FakeStreamAdapter([StreamScript(text="一"), StreamScript(text="二")])
    loop = _loop_with_echo(adapter, media_parts=_media_parts())

    await loop.run("第一轮")
    await loop.run("第二轮")

    second_turn_first = adapter.requests[1]["messages"][0]
    assert second_turn_first.content == "第二轮"
    assert not second_turn_first.images and not second_turn_first.audios
    assert not second_turn_first.videos


async def test_loop_without_media_keeps_the_plain_text_message():
    """没有媒体时消息形状逐字不变（既有调用方一字不用改）。"""
    adapter = FakeStreamAdapter([StreamScript(text="回答")])
    loop = _loop_with_echo(adapter)
    await loop.run("普通消息")

    message = adapter.requests[0]["messages"][0]
    assert message.content == "普通消息"
    assert not message.images and not message.audios and not message.videos
