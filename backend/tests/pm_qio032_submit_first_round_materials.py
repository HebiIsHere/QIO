# -*- coding: utf-8 -*-
"""PM-QIO032 #01：互动提交轮的首轮材料交付（真实编排链路，fake adapter 捕获请求）。

BRIEF #01：点提交时，本轮立即获得允许参与材料的可读正文及图片；TXT 与 PNG 用同一个
正式提交入口验证，不能只验证卡面。

本文件走**真实流水线**：真实 AppContext / TurnOrchestrator / AgentLoop，只有模型是
fake adapter（记录每次模型调用收到的消息）。断言的是「模型到底收到了什么」：
- 提交当时快照里的 TXT 正文出现在交互上下文块里；
- PNG 真实字节作为图片内容块进入首轮模型调用（adapter.supports_images=True）；
- 没有视觉能力时如实标 image_unsupported，不声称看懂（不编造）。

另含一条读授权接线用例：提交快照允许的附件 id 会并入本轮 read grants（走既有
attachment_access.authorize_read 网关），否则板面材料的 read_attachment 会被前置
read_allowed 拦掉。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import board, board_store, models, submission
from agent.services.app import AppContext
from agent.services.attachments import AttachmentService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

BOARD = "board_pm032_first_round"
TXT_BODY = "第一行正文：预算 100 万\n第二行正文：排期 6 周"
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _CapturingAdapter:
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
        return Completion(message=ChatMessage(role="assistant", content="收到材料"))


async def _return(value):
    return value


@pytest.fixture()
def app_ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "first-round.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    service = AttachmentService(ctx.conn, Path(ctx.settings.data_dir))
    ctx.services.register("attachments", service)
    ctx._test_attachment_service = service  # type: ignore[attr-defined]
    return ctx


def _ready(service: AttachmentService, tmp_path: Path, name: str, body: bytes):
    source = tmp_path / ("src-" + name)
    source.write_bytes(body)
    att = service.run_prepare(service.prepare(str(source), topic_id="t1").id)
    assert att.state == "ready", (att.state, att.error)
    return att


def _file_card(state: dict, *, att, name: str) -> dict:
    block = {"name": name, "attachmentId": att.id, "size": int(att.size_bytes)}
    state = board.add_card(state, "file", "", meta={"name": name, "file": block})
    card_id = str(state["cards"][-1]["id"])
    return board.update_card(
        state, card_id, {"participation": models.PARTICIPATION_INCLUDED}
    )


def _seed_board(ctx: AppContext, tmp_path: Path) -> dict:
    service = ctx._test_attachment_service  # type: ignore[attr-defined]
    txt = _ready(service, tmp_path, "材料.txt", TXT_BODY.encode("utf-8"))
    png = _ready(service, tmp_path, "图.png", _PNG_BYTES)
    board_store.ensure_board(ctx.conn, board_id=BOARD)
    state = board_store.current_state(ctx.conn, BOARD)
    state = board.add_card(state, "text", "请整理这份材料")
    text_id = str(state["cards"][-1]["id"])
    state = board.update_card(
        state, text_id, {"participation": models.PARTICIPATION_INCLUDED}
    )
    state = _file_card(state, att=txt, name="材料.txt")
    state = _file_card(state, att=png, name="图.png")
    board_store.save_board(ctx.conn, BOARD, state, reason="seed")
    return {"txt": txt, "png": png}


async def _submit_board_turn(ctx: AppContext, topic_id: str) -> dict:
    """走正式提交入口，并把受理结果接到真实主循环上的一个 Turn（与生产接线一致）。"""

    def accept(**kwargs) -> dict:
        turn = ctx.turns.submit(str(kwargs.get("message") or ""), topic_id)
        turn.mode = "interactive"
        turn.board_id = str(kwargs.get("board_id") or "")
        turn.board_submission_id = str(kwargs.get("submission_id") or "")
        turn.board_topic_id = str(topic_id)
        turn.board_attempt = 1
        return {"ok": True, "turnId": turn.turn_id, "topicId": topic_id}

    result = await submission.submit_board(ctx.conn, BOARD, turn_acceptor=accept)
    assert result["status"] == "succeeded", result
    await ctx.turns.wait(result["turnId"], timeout=60)
    return result


def _joined(messages: list[ChatMessage]) -> str:
    return "\n".join(str(message.content or "") for message in messages)


@pytest.mark.anyio
async def test_first_round_delivers_txt_text_and_png_bytes_to_model(app_ctx):
    ctx = app_ctx
    _seed_board(ctx, Path(ctx.settings.data_dir).parent)
    adapter = _CapturingAdapter()
    ctx.build_adapter = lambda: _return(adapter)  # type: ignore[assignment]
    topic = ctx.topics.nodes.create_topic("互动首轮材料").id

    await _submit_board_turn(ctx, topic)

    assert adapter.calls, "模型没有被调用：首轮材料无从验证"
    for messages in adapter.calls:
        joined = _joined(messages)
        # ① TXT 正文真的进了首轮提示词（交互上下文块里的 materials）。
        assert "第一行正文" in joined, "TXT 正文没有交付给模型"
        assert '"interactive"' in joined, "互动上下文块缺失"
        assert '"materials"' in joined, "材料条目没有进入交互上下文块"
        # ② PNG 字节真的作为图片内容块交付（fake adapter 声明 supports_images=True）。
        images = [part for message in messages for part in (message.images or [])]
        assert images, "PNG 图片字节没有交付给模型"
        assert any(part.data_url.startswith("data:image/png") for part in images)


@pytest.mark.anyio
async def test_first_round_without_vision_marks_image_unsupported(app_ctx):
    ctx = app_ctx
    _seed_board(ctx, Path(ctx.settings.data_dir).parent)

    class _NoVisionAdapter(_CapturingAdapter):
        supports_images = False

    adapter = _NoVisionAdapter()
    ctx.build_adapter = lambda: _return(adapter)  # type: ignore[assignment]
    topic = ctx.topics.nodes.create_topic("互动首轮无视觉").id

    await _submit_board_turn(ctx, topic)

    assert adapter.calls, "模型没有被调用"
    joined = _joined(adapter.calls[0])
    assert "image_unsupported" in joined, "没有视觉能力时必须如实标记，不能假装看懂"
    images = [part for message in adapter.calls[0] for part in (message.images or [])]
    assert not images, "没有视觉能力时不应交付图片字节"


@pytest.mark.anyio
async def test_submission_attachments_join_turn_read_grants(app_ctx, monkeypatch):
    """提交快照允许的附件并入本轮 read grants（否则 read_attachment 被前置门拦）。"""
    from agent.services import attachment_access

    ctx = app_ctx
    seeded = _seed_board(ctx, Path(ctx.settings.data_dir).parent)
    topic = ctx.topics.nodes.create_topic("互动读授权").id
    adapter = _CapturingAdapter()
    ctx.build_adapter = lambda: _return(adapter)  # type: ignore[assignment]

    recorded: list[set[str]] = []
    original = attachment_access.set_read_grants

    def spy(attachment_ids):
        recorded.append({str(item) for item in attachment_ids or ()})
        return original(attachment_ids)

    monkeypatch.setattr(attachment_access, "set_read_grants", spy)

    from agent.interactive import materials as im

    scope = im.submission_attachment_scope(
        ctx.conn,
        board_id=BOARD,
        submission_id="missing-submission",
        attachments=ctx._test_attachment_service,  # type: ignore[attr-defined]
    )
    # 没有提交快照时如实不可用（不编造 allowed）。
    assert scope["available"] is False
    assert not scope["allowed"]

    result = await _submit_board_turn(ctx, topic)
    scope = im.submission_attachment_scope(
        ctx.conn,
        board_id=BOARD,
        submission_id=str(result["submission"]["id"]),
        attachments=ctx._test_attachment_service,  # type: ignore[attr-defined]
    )
    allowed = set(scope["allowed"].keys())
    assert seeded["txt"].id in allowed and seeded["png"].id in allowed
    # 本轮 read grants 里确实包含这些附件（生产路径由 authorize_read 网关放行）。
    wanted = {seeded["txt"].id, seeded["png"].id}
    assert any(wanted <= granted for granted in recorded), (
        f"提交材料附件没有进入本轮 read grants：{recorded}"
    )
