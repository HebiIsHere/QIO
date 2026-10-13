"""会话模式的图片交付（2026-10-13 线上缺口的回归测试）。

真实故障：桌面端 0.2.2 里，对话模式附上的 .jpeg **从来没有**交给模型 ——
媒体候选清单用 `classify_media_kind` 归类，而它按设计只认音频 / 视频，
图片归类为空 → 候选为空 → 没有内容块 → 模型只看到「附加了文件」，
于是如实回答「我看不到画面」（用户侧观感就是「无法识图」）。

这一组用例锁住四件事：

1. 候选清单必须认图片（不然下面所有断言都无从谈起）；
2. 有视觉能力位时**真的交付字节**：内容块里的 base64 与文件逐字节一致；
3. 没有能力位 / 格式不支持 / 超限时如实说不交付，且**不编造画面**；
4. **绝不把图片当视频抽帧**（历史反例：`_media_candidate_entry` 曾无条件落到 `_video_entry`，
   回执写成 `video_unsupported`「没有抽到任何一帧」）。

图片夹具是 ffmpeg 现场生成后再嵌进来的**真实字节**（4x4 JPEG / 4x4 PNG），
所以本文件**不需要 ffmpeg** 也能在 CI 上跑。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agent.adapters.base import ChatMessage, Completion, ImagePart
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import materials
from agent.services.app import AppContext
from agent.services.attachments import AttachmentService
from agent.services.turn_orchestrator import media_facts_note
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

#: 真 JPEG（ffmpeg: color=red:size=4x4 -> mjpeg，225 字节）
JPEG_B64 = (
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMgD/2wBDAAgEBAQEBAUFBQUFBQYFBgYGBgYG"
    "BgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABMAAEBAAAAAAAAAAAAAAAA"
    "AAAABgEBAQAAAAAAAAAAAAAAAAAABgcQAQAAAAAAAAAAAAAAAAAAAAARAQAAAAAAAAAAAAAAAAAAAAD/wAARCAAE"
    "AAQDASIAAhEAAxEA/9oADAMBAAIRAxEAPwCLAE1/f//Z"
)
#: 真 PNG（ffmpeg: color=blue:size=4x4，97 字节）
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAACXBIWXMAAAABAAAAAQBPJcTWAAAAE0lEQVR4"
    "nGNkYPjHAAMscBZeDgAyPgEMeXcFHgAAAABJRU5ErkJggg=="
)


def _write(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _jpeg(tmp_path: Path, name: str = "shot.jpeg") -> Path:
    return _write(tmp_path, name, base64.b64decode(JPEG_B64))


def _png(tmp_path: Path, name: str = "shot.png") -> Path:
    return _write(tmp_path, name, base64.b64decode(PNG_B64))


def _candidate(path: Path, *, kind: str = "image", attachment_id: str = "att_probe") -> dict:
    return {
        "attachmentId": attachment_id,
        "card_id": attachment_id,
        "name": path.name,
        "kind": kind,
        "path": path,
        "bytes": path.stat().st_size,
        "version": "v1",
    }


def _resolve(candidates: list[dict], *, images: bool):
    return materials.resolve_media(
        candidates=candidates,
        supports_images=images,
        supports_audio=False,
        supports_video=False,
        audio_transcription=None,
        transcriber=None,
        work_dir=None,
    )


def _ctx(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "chat-image.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


class _CapturingAdapter:
    """记录每一次模型调用收到的消息（与既有 e2e 同一形状）。"""

    mode = "native"
    model = "fake-capture"
    supports_stream = False
    supports_images = True
    supports_audio = False
    supports_video = False
    audio_transcription = None

    def __init__(self, *, images: bool = True) -> None:
        self.supports_images = bool(images)
        self.calls: list[list[ChatMessage]] = []

    async def complete(self, messages, tools, **kwargs):
        self.calls.append(list(messages))
        return Completion(message=ChatMessage(role="assistant", content="看到了"))


async def _return(value):
    return value


async def _run_turn_with(ctx: AppContext, source: Path, *, images: bool, message: str = "这是什么"):
    service = AttachmentService(ctx.conn, Path(ctx.settings.data_dir))
    ctx.services.register("attachments", service)
    topic = ctx.topics.nodes.create_topic("图片").id
    tctx = ctx.turns.submit(message, topic)
    att = service.prepare(str(source), topic_id=topic, turn_id=tctx.turn_id)
    service.run_prepare(att.id)
    adapter = _CapturingAdapter(images=images)
    ctx.build_adapter = lambda: _return(adapter)  # type: ignore[assignment]
    await ctx.turns.wait(tctx.turn_id, timeout=60)
    return adapter


# --- 1. 候选清单必须认图片（根因） ----------------------------------------


async def test_collected_candidates_include_images(tmp_path: Path):
    """附件清单里的一张 .jpeg 必须成为 `image` 候选（修复前是空清单）。"""
    ctx = _ctx(tmp_path)
    service = AttachmentService(ctx.conn, Path(ctx.settings.data_dir))
    topic = ctx.topics.nodes.create_topic("图片").id
    tctx = ctx.turns.submit("这是什么", topic)
    att = service.prepare(str(_jpeg(tmp_path)), topic_id=topic, turn_id=tctx.turn_id)
    service.run_prepare(att.id)

    candidates = materials.collected_media_candidates(service, [tctx.turn_id])

    assert [c["kind"] for c in candidates] == ["image"], candidates
    assert candidates[0]["attachmentId"] == att.id


def test_classify_keeps_audio_video_semantics_by_default():
    """默认语义不变：`classify_media_kind` 不认图片，只有显式要求时才认。"""
    from agent.services.attachments import classify_media_kind

    assert classify_media_kind("a.jpeg") == ""
    assert classify_media_kind("a.jpeg", include_images=True) == "image"
    assert classify_media_kind("a.png", include_images=True) == "image"
    assert classify_media_kind("a.mp4") == "video"
    assert classify_media_kind("a.wav") == "audio"
    assert classify_media_kind("a.txt", include_images=True) == ""


# --- 2. 有能力位就真的交付字节 --------------------------------------------


@pytest.mark.parametrize("maker,media_type", [(_jpeg, "image/jpeg"), (_png, "image/png")])
def test_image_is_delivered_with_byte_identical_payload(tmp_path: Path, maker, media_type: str):
    source = maker(tmp_path)
    out = _resolve([_candidate(source)], images=True)

    entry = out["entries"][0]
    assert entry["source"] == "image_delivered", entry
    assert entry["read"] is True
    assert entry["bytes"] == source.stat().st_size

    parts = out["parts"]
    assert len(parts) == 1, parts
    part = parts[0]
    assert isinstance(part, ImagePart)
    head, _, payload = part.data_url.partition(",")
    assert head == "data:" + media_type + ";base64", head
    assert base64.b64decode(payload) == source.read_bytes(), "交付的字节必须与文件逐字节一致"


# --- 3. 不能交付时如实说，且不编造 ----------------------------------------


def test_image_without_vision_is_unsupported_with_readable_reason(tmp_path: Path):
    out = _resolve([_candidate(_jpeg(tmp_path))], images=False)

    entry = out["entries"][0]
    assert entry["source"] == "image_unsupported", entry
    assert entry["read"] is False
    assert "视觉能力" in entry["reason"], entry["reason"]
    assert out["parts"] == [], "没有视觉能力就不许把图片当内容块交付"


def test_oversize_image_is_refused_instead_of_truncated(tmp_path: Path):
    source = _jpeg(tmp_path)
    candidate = _candidate(source)
    candidate["bytes"] = materials.MAX_IMAGE_BYTES + 1

    out = _resolve([candidate], images=True)

    entry = out["entries"][0]
    assert entry["source"] == "image_unsupported"
    assert "超过交付上限" in entry["reason"], entry["reason"]
    assert out["parts"] == [], "不截断成半张图"


def test_unsupported_image_type_is_named_in_the_reason(tmp_path: Path):
    source = _write(tmp_path, "scan.tiff", base64.b64decode(PNG_B64))

    out = _resolve([_candidate(source)], images=True)

    entry = out["entries"][0]
    assert entry["source"] == "image_unsupported"
    assert ".tiff" in entry["reason"], entry["reason"]
    assert out["parts"] == []


def test_non_media_candidate_is_never_sampled_as_video(tmp_path: Path):
    """把非媒体当视频抽帧是历史反例：这里必须如实说「不是媒体」。"""
    source = _write(tmp_path, "note.txt", b"hello")

    out = _resolve([_candidate(source, kind="")], images=True)

    entry = out["entries"][0]
    assert entry["source"] == "unreadable", entry
    assert "不是音频 / 视频 / 图片" in entry["reason"], entry["reason"]
    assert out["parts"] == []


def test_media_facts_note_reports_the_image_as_delivered():
    note = media_facts_note([{"source": "image_delivered", "label": "截图"}])
    assert "图片「截图」" in note
    assert "已交给本次链路" in note


# --- 4. 端到端：会话轮把图片挂在首条 user 消息上 --------------------------


async def test_conversation_turn_delivers_the_image_to_every_model_call(tmp_path: Path):
    """真实流水线：附件 → 候选 → 内容块 → 每一次模型调用都带着这张图。"""
    source = _png(tmp_path)
    ctx = _ctx(tmp_path)

    adapter = await _run_turn_with(ctx, source, images=True)

    assert adapter.calls, "模型没有被调用"
    for messages in adapter.calls:
        first = messages[0]
        assert first.role == "user"
        images = list(first.images or [])
        assert images, "每一次模型调用都必须带着本轮的图片字节"
        head, _, payload = images[0].data_url.partition(",")
        assert head == "data:image/png;base64", head
        assert base64.b64decode(payload) == source.read_bytes()


async def test_conversation_turn_without_vision_says_so_in_the_prompt(tmp_path: Path):
    """没有视觉能力：不交付内容块，并在提示词里**如实**说明原因。"""
    source = _png(tmp_path)
    ctx = _ctx(tmp_path)

    adapter = await _run_turn_with(ctx, source, images=False, message="看看这张图")

    assert adapter.calls, "模型没有被调用"
    first = adapter.calls[0][0]
    assert not first.images, "没有视觉能力就不许把图片当内容块交付"
    assert "没有视觉能力" in (first.content or ""), first.content


# --- 5. 最后一跳：内容块真的变成请求里的 image_url --------------------------


def test_delivered_image_becomes_an_image_url_block_in_the_request(tmp_path: Path):
    """交付的内容块必须真的变成 OpenAI 请求里的 image_url 块（不是只挂在 message 上）。"""
    from agent.adapters import native
    from agent.adapters.base import ChatMessage as Msg

    source = _png(tmp_path)
    out = _resolve([_candidate(source)], images=True)
    message = Msg(role="user", content="这是什么", images=list(out["parts"]))

    payload = native._openai_content(message)

    assert isinstance(payload, list), payload
    assert payload[0]["type"] == "text", payload
    image_blocks = [b for b in payload if b["type"] == "image_url"]
    assert len(image_blocks) == 1, payload
    data_url = image_blocks[0]["image_url"]["url"]
    head, _, encoded = data_url.partition(",")
    assert head == "data:image/png;base64"
    assert base64.b64decode(encoded) == source.read_bytes(), "请求里的字节必须与文件一致"


def test_message_without_images_keeps_the_plain_string_shape():
    """没有图片的消息形状逐字不变（只认字符串的端点不受影响）。"""
    from agent.adapters import native
    from agent.adapters.base import ChatMessage as Msg

    assert native._openai_content(Msg(role="user", content="只有文字")) == "只有文字"


# --- 7. 读不到字节时也要给一句事实（不许静默丢弃） ------------------------


async def test_reference_attachment_yields_an_honest_fact_not_silence(tmp_path: Path):
    """大文件按「引用」登记（不复制副本）：媒体通道读不到字节，但**不许静默丢弃** ——
    修复前这种附件连一条说明都没有（复审用 100MB+ 的 .jpeg 实测 candidates=[]），
    模型因此完全不知道有这张图。现在必须给出一条带可读原因的 `image_unsupported`。"""
    ctx = _ctx(tmp_path)
    service = AttachmentService(ctx.conn, Path(ctx.settings.data_dir))
    topic = ctx.topics.nodes.create_topic("大图").id
    tctx = ctx.turns.submit("这是什么", topic)

    big = tmp_path / "huge.jpeg"
    with big.open("wb") as handle:  # 稀疏文件：不真的占 100MB
        handle.seek(100_000_001)
        handle.write(b"\0")
    att = service.prepare(str(big), topic_id=topic, turn_id=tctx.turn_id)
    service.run_prepare(att.id)
    assert att.kind == "reference", att.kind

    candidates = materials.collected_media_candidates(service, [tctx.turn_id])

    assert len(candidates) == 1, candidates
    assert candidates[0]["kind"] == "image"
    assert candidates[0]["path"] is None
    assert candidates[0]["unreadable_reason"], "必须带上可读原因"

    out = materials.resolve_media(
        candidates=candidates,
        supports_images=True,
        supports_audio=False,
        supports_video=False,
        audio_transcription=None,
        transcriber=None,
        work_dir=None,
    )
    entry = out["entries"][0]
    assert entry["source"] == "image_unsupported", entry
    assert entry["read"] is False
    assert ("引用" in entry["reason"]) or ("副本" in entry["reason"]), entry["reason"]
    assert out["parts"] == []


# --- 6. 真实附件名的形状：长名字被展示层截断，不许影响媒体类型 ------------


def test_long_hash_style_name_still_resolves_the_media_type(tmp_path: Path):
    """真实副本名很长（哈希 + 原名，这里 52 字符）：展示标签只留 24 字符 + 「…」，
    媒体类型必须按**真实文件名**判断 —— 否则图片会被判成「无法确认媒体类型」而拒绝交付。
    （2026-10-13 用线上那张 65KB .jpeg 实测到的第二个坑。）"""
    long_name = "f66a6ab18a08b3a87ae0e7689601319b_7305592917545852020.jpeg"
    source = _jpeg(tmp_path, long_name)

    out = _resolve([_candidate(source)], images=True)

    entry = out["entries"][0]
    assert entry["source"] == "image_delivered", entry
    assert len(out["parts"]) == 1, out["parts"]
    head, _, payload = out["parts"][0].data_url.partition(",")
    assert head == "data:image/jpeg;base64", head
    assert base64.b64decode(payload) == source.read_bytes()


def test_long_name_audio_gets_a_real_media_type_not_a_wildcard(tmp_path: Path):
    """同一条规则也管音频：长名字不再依赖通配类型兜底。"""
    source = _write(tmp_path, ("9f1c" * 12) + "_recording.wav", b"RIFF" + bytes(64))
    candidate = _candidate(source, kind="audio")

    out = materials.resolve_media(
        candidates=[candidate],
        supports_images=False,
        supports_audio=True,
        supports_video=False,
        audio_transcription=None,
        transcriber=None,
        work_dir=None,
    )

    entry = out["entries"][0]
    assert entry["source"] == "audio_delivered", entry
    head, _, payload = out["parts"][0].data_url.partition(",")
    assert head == "data:audio/wav;base64", head
    assert base64.b64decode(payload) == source.read_bytes()
