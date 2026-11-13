# -*- coding: utf-8 -*-
"""PM-QIO032 #28 定向验收：正式提交只能读取提交时的版本（A 版）。

覆盖任务书的每一条：

1. 绑定提交的正式轮次统一按该提交读取：board_inspect、默认 / 显式 source=submission 的
   board_read_cards、read_attachment、首轮模型请求的材料入口；绑定该提交的追问轮同样。
2. 提交快照缺失 / 损坏 / 归属不匹配 / 读不到：返回**可识别的失败状态**（不是成功、
   不是零张卡），界面短文案「读不到这次提交的内容，请重新提交。」，诊断细节在详情里，
   失败结果里没有任何当前板面数据。
3. 源文件变化（含相同字节数替换）、原文件不可读、旧记录缺少足够证据核对版本：
   只交付能确认属于提交当时的材料；交给模型的字节就是核验过的字节（不会校验后又读到
   另一版本）；其余材料如实提示重新提交。
4. 普通互动 / 未绑定提交保持原规则；隐藏 / 排除 / 提交后新增的卡片与文件不进入绑定轮次。

实现只走本地 SQLite + 本地 fake provider（agent.adapters.fake）：不联网、不用真实凭据、
不依赖真实模型。断言的是工具真实执行结果与模型请求里**真实出现的内容**。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from agent.adapters.base import (
    STREAM_DONE,
    STREAM_TEXT,
    STREAM_TOOL_CALL,
    ChatMessage,
    Completion,
    StreamDelta,
    ToolCall,
)
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import board, board_store, materials, models, submission
from agent.services.app import AppContext
from agent.services.attachments import AttachmentService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.attachment_tools import ReadAttachmentTool

BOARD = "board_pm032_version_basis"
TEXT_BODY_A = "A 版正文：预算 100 万"
TEXT_BODY_B = "B 版正文：预算 900 万"
# 同为 16 字节：只按大小核对区分不出来（必须靠受理时冻结的内容哈希）。
TXT_BYTES_A = b"AAAA-OLD-CONTENT"
TXT_BYTES_B = b"BBBB-NEW-CONTENT"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
EXCLUDED_BODY = b"EXCLUDED-MATERIAL-BODY"
HIDDEN_BODY = b"HIDDEN-MATERIAL-BODY"
B_ONLY_BODY = b"B-ONLY-LATER-MATERIAL-BODY"
BOARD_MEANING_A = "A 版关系"


# ---------------------------------------------------------------------------
# 本地 fake provider：脚本化的工具调用 + 记录每一次模型请求
# ---------------------------------------------------------------------------


class _ToolThenAnswerAdapter:
    """本地 fake provider（不联网）：第一步发工具调用，第二步给最终回答。

    记录每次模型调用收到的完整 messages（断言「模型到底收到了什么」）。
    """

    mode = "native"
    model = "fake-28-version-basis"
    supports_stream = True
    supports_images = True
    supports_audio = False
    supports_video = False
    audio_transcription = None
    endpoint = None

    def __init__(self, tool_calls: list[ToolCall], *, answer: str = "已按提交时的内容核对") -> None:
        self._tool_calls = list(tool_calls)
        self._answer = answer
        self._sent_tools = False
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):
        self.requests.append(
            {"messages": list(messages), "tools": list(tools), "stream": False}
        )
        return Completion(message=ChatMessage(role="assistant", content=self._answer))

    async def stream(self, messages, tools, **kwargs):
        self.requests.append(
            {"messages": list(messages), "tools": list(tools), "stream": True}
        )
        if not self._sent_tools:
            self._sent_tools = True
            for index, call in enumerate(self._tool_calls):
                yield StreamDelta(
                    kind=STREAM_TOOL_CALL,
                    index=index,
                    call_id=call.id,
                    name=call.name,
                )
            yield StreamDelta(
                kind=STREAM_DONE,
                completion=Completion(
                    message=ChatMessage(
                        role="assistant", tool_calls=list(self._tool_calls)
                    )
                ),
            )
            return
        yield StreamDelta(kind=STREAM_TEXT, text=self._answer)
        yield StreamDelta(
            kind=STREAM_DONE,
            completion=Completion(
                message=ChatMessage(role="assistant", content=self._answer)
            ),
        )


async def _return(value):
    return value


# ---------------------------------------------------------------------------
# 环境 / 便于阅读的包装
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "pm032-version-basis.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    service = AttachmentService(ctx.conn, Path(ctx.settings.data_dir))
    ctx.attachments = service
    ctx.services.register("attachments", service)
    # 生产 create_app 会注册这个工具；测试里按同一构造接线。
    ctx.registry.register(
        ReadAttachmentTool(
            service,
            active_turn_id=lambda: (
                ctx.turns.active.turn_id if ctx.turns.active else None
            ),
        )
    )
    return ctx


def _ready(service: AttachmentService, tmp_path: Path, name: str, body: bytes):
    source = tmp_path / ("src-" + name)
    source.write_bytes(body)
    att = service.run_prepare(service.prepare(str(source), topic_id="t1").id)
    assert att.state == "ready", (att.state, att.error)
    return att


def _add_text_card(state: dict, content: str, *, included: bool = True) -> tuple[dict, str]:
    state = board.add_card(state, "text", content)
    card_id = str(state["cards"][-1]["id"])
    state = board.update_card(
        state,
        card_id,
        {
            "participation": (
                models.PARTICIPATION_INCLUDED
                if included
                else models.PARTICIPATION_EXCLUDED
            )
        },
    )
    return state, card_id


def _add_file_card(
    state: dict, att, name: str, *, included: bool = True
) -> tuple[dict, str]:
    block = {"name": name, "attachmentId": att.id, "size": int(att.size_bytes)}
    state = board.add_card(state, "file", "", meta={"name": name, "file": block})
    card_id = str(state["cards"][-1]["id"])
    state = board.update_card(
        state,
        card_id,
        {
            "participation": (
                models.PARTICIPATION_INCLUDED
                if included
                else models.PARTICIPATION_EXCLUDED
            )
        },
    )
    return state, card_id


def _seed_a(ctx: AppContext, tmp_path: Path) -> dict:
    """提交 A 的板面：参与文字卡 + 可读 TXT + 可读 PNG；另有排除 / 隐藏文件卡。"""
    service = ctx.attachments
    txt = _ready(service, tmp_path, "材料.txt", TXT_BYTES_A)
    png = _ready(service, tmp_path, "图.png", PNG_BYTES)
    excluded = _ready(service, tmp_path, "排除.txt", EXCLUDED_BODY)
    hidden = _ready(service, tmp_path, "隐藏.txt", HIDDEN_BODY)

    board_store.ensure_board(ctx.conn, board_id=BOARD)
    state = board_store.current_state(ctx.conn, BOARD)
    state, text_id = _add_text_card(state, TEXT_BODY_A)
    state, other_id = _add_text_card(state, "A 版第二张卡片")
    state = board.add_link(state, text_id, other_id, False, BOARD_MEANING_A)
    link_id = str(state["links"][-1]["id"])
    state, txt_card = _add_file_card(state, txt, "材料.txt")
    state, png_card = _add_file_card(state, png, "图.png")
    state, excluded_card = _add_file_card(state, excluded, "排除.txt", included=False)
    state, hidden_card = _add_file_card(state, hidden, "隐藏.txt")
    state = board.set_hidden(state, hidden_card, True)
    board_store.save_board(ctx.conn, BOARD, state, reason="seed-a")
    return {
        "txt": txt,
        "png": png,
        "excluded": excluded,
        "hidden": hidden,
        "text_id": text_id,
        "other_id": other_id,
        "link_id": link_id,
        "txt_card": txt_card,
        "png_card": png_card,
        "excluded_card": excluded_card,
        "hidden_card": hidden_card,
    }


async def _submit_a(ctx: AppContext, topic_id: str) -> str:
    """走正式提交入口（真实受理路径：冻结 materialVersions、写提交记录）。

    受理返回的 turnId 这里不需要真的跑一轮：本文件验证的是「绑定该提交的追问轮」。
    """

    def accept(**kwargs) -> dict:
        return {
            "ok": True,
            "turnId": "turn_submit_" + str(kwargs.get("submission_id") or "")[:8],
            "topicId": topic_id,
        }

    result = await submission.submit_board(ctx.conn, BOARD, turn_acceptor=accept)
    assert result["status"] == "succeeded", result
    return str(result["submission"]["id"])


def _submit_a_sync(ctx: AppContext, topic_id: str) -> str:
    """同步用例（材料层单元口径）用的提交入口：同一个真实受理路径。"""
    import asyncio

    return asyncio.run(_submit_a(ctx, topic_id))


def _save_b(ctx: AppContext, seeded: dict, tmp_path: Path) -> dict:
    """提交后保存 B 版：改正文、加新卡/新文件、把 A 的 TXT 同字节数替换。"""
    state = board_store.current_state(ctx.conn, BOARD)
    state = board.update_card(state, seeded["text_id"], {"content": TEXT_BODY_B})
    state, b_text_id = _add_text_card(state, "B 版新增卡片")
    b_att = _ready(ctx.attachments, tmp_path, "B新材料.txt", B_ONLY_BODY)
    state, b_card = _add_file_card(state, b_att, "B新材料.txt")
    board_store.save_board(ctx.conn, BOARD, state, reason="seed-b")
    # 真实改动已保存的 A 副本：字节数相同、内容不同（只按大小看不出来）。
    target = ctx.attachments.content_target(seeded["txt"].id)
    path = Path(target[0]) if isinstance(target, tuple) else Path(target)
    path.write_bytes(TXT_BYTES_B)
    return {"b_att": b_att, "b_card": b_card, "b_text_id": b_text_id}


async def _run_bound_turn(
    ctx: AppContext, topic_id: str, submission_id: str, adapter, question: str
):
    """开一轮**绑定该提交**的追问，等它真正跑完（工具真的执行过）。"""
    ctx.build_adapter = lambda: _return(adapter)
    turn = ctx.turns.submit(question, topic_id)
    turn.mode = "interactive"
    turn.board_id = BOARD
    turn.board_submission_id = submission_id
    turn.board_topic_id = str(topic_id)
    turn.board_resume = False
    turn.board_attempt = 1
    await ctx.turns.wait(turn.turn_id, timeout=60)
    return turn


def _joined(messages) -> str:
    return "\n".join(str(message.content or "") for message in messages)


def _tool_envelopes(request: dict) -> dict:
    """从一次模型请求里取出真实交给模型的工具结果（按 tool_call_id 索引）。"""
    out: dict = {}
    for message in request["messages"]:
        if str(getattr(message, "role", "")) != "tool":
            continue
        raw = str(message.content or "")
        try:
            envelope = json.loads(raw)
        except ValueError:  # pragma: no cover - 结构化结果必须是 JSON
            envelope = {"raw": raw}
        out[str(getattr(message, "tool_call_id", ""))] = envelope
    return out


def _tool_payload(envelope: dict) -> dict:
    """工具结果：结构化 envelope 直接用；旧正文兼容结果（成功正文 JSON）解开看。"""
    if "raw" not in envelope:
        return envelope
    try:
        payload = json.loads(envelope["raw"])
    except ValueError:  # pragma: no cover - 旧正文兼容结果一定是 JSON
        return {"raw": envelope["raw"]}
    return payload if isinstance(payload, dict) else {"raw": envelope["raw"]}


def _compact_result(envelope: dict) -> dict:
    """证据里的一条工具结果：只留可复核的机器事实（状态 / 身份 / 卡片与关系 / 原因）。"""
    payload = _tool_payload(envelope)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    error = payload.get("error")
    out: dict = {"ok": payload.get("ok"), "code": payload.get("code")}
    if isinstance(error, dict):
        out["error"] = error.get("message")
    elif error:
        out["error"] = str(error)
    basis = data.get("basis") if isinstance(data.get("basis"), dict) else None
    if basis:
        out["basis"] = {
            "source": basis.get("source"),
            "available": basis.get("available"),
        }
    if data.get("submission_id"):
        out["submission_id"] = data["submission_id"]
    cards = data.get("cards")
    if isinstance(cards, list):
        out["card_ids"] = [str(card.get("id")) for card in cards if isinstance(card, dict)]
        out["card_contents"] = [
            str(card.get("content") or card.get("content_preview") or "")
            for card in cards
            if isinstance(card, dict)
        ]
    links = data.get("links")
    if isinstance(links, list):
        out["link_meanings"] = [
            str(link.get("meaning") or "") for link in links if isinstance(link, dict)
        ]
    stale = data.get("stale")
    if isinstance(stale, list):
        out["stale"] = [
            {"cardId": item.get("cardId"), "reason": item.get("reason")}
            for item in stale
            if isinstance(item, dict)
        ]
    return out


def _dump_evidence(name: str, adapter, extra: dict) -> None:
    """打印证据：机器可复核的摘要 +（最后一条）完整请求快照。"""
    summary_requests = []
    for index, request in enumerate(adapter.requests):
        joined = _joined(request["messages"])
        summary_requests.append(
            {
                "index": index,
                "stream": request["stream"],
                "images": len(
                    [
                        part
                        for message in request["messages"]
                        for part in (message.images or [])
                    ]
                ),
                "markers": {
                    marker: (text in joined) for marker, text in extra["markers"].items()
                }
                if "markers" in extra
                else {},
            }
        )
    summary = {
        "case": name,
        "submission_id": extra.get("submission_id"),
        "tool_calls": extra.get("tool_calls"),
        "tool_results": {
            call_id: _compact_result(envelope)
            for call_id, envelope in (extra.get("tool_results") or {}).items()
        },
        "model_requests": summary_requests,
        "markers": extra.get("markers"),
    }
    print(
        "PM-QIO032-28-SUMMARY "
        + json.dumps(summary, ensure_ascii=False, sort_keys=True)
    )
    """把这次验收里工具调用 / 返回 / 模型请求写成可复查的证据（pytest -s 直接可见）。"""
    requests = []
    for index, request in enumerate(adapter.requests):
        requests.append(
            {
                "index": index,
                "stream": request["stream"],
                "messages": [
                    {
                        "role": str(getattr(message, "role", "")),
                        "tool_call_id": getattr(message, "tool_call_id", None),
                        "content": str(message.content or ""),
                    }
                    for message in request["messages"]
                ],
            }
        )
    print(
        "PM-QIO032-28-EVIDENCE "
        + json.dumps(
            {"case": name, "requests": requests, **extra},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


# ---------------------------------------------------------------------------
# 1) 提交 A 后编辑为 B：绑定追问轮只读 A（工具真实执行 + 最终模型请求）
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bound_followup_turn_reads_only_submission_version(app_ctx, tmp_path):
    ctx = app_ctx
    seeded = _seed_a(ctx, tmp_path)
    topic = ctx.topics.nodes.create_topic("提交版本追问").id
    submission_id = await _submit_a(ctx, topic)
    before = board_store.current_state(ctx.conn, BOARD)
    assert before["cards"][0]["content"] == TEXT_BODY_A
    b = _save_b(ctx, seeded, tmp_path)

    calls = [
        ToolCall(id="c_inspect", name="board_inspect", arguments={}),
        ToolCall(id="c_read_latest", name="board_read_cards", arguments={}),
        ToolCall(
            id="c_read_submission",
            name="board_read_cards",
            arguments={"source": "submission"},
        ),
        ToolCall(
            id="c_read_txt",
            name="read_attachment",
            arguments={"attachment_id": seeded["txt"].id},
        ),
        ToolCall(
            id="c_read_b_only",
            name="read_attachment",
            arguments={"attachment_id": b["b_att"].id},
        ),
        ToolCall(
            id="c_read_png",
            name="read_attachment",
            arguments={"attachment_id": seeded["png"].id},
        ),
        ToolCall(
            id="c_read_b_card",
            name="board_read_cards",
            arguments={"source": "submission", "card_ids": [b["b_text_id"]]},
        ),
        ToolCall(
            id="c_read_hidden",
            name="read_attachment",
            arguments={"attachment_id": seeded["hidden"].id},
        ),
    ]
    adapter = _ToolThenAnswerAdapter(calls)
    await _run_bound_turn(ctx, topic, submission_id, adapter, "核对这次提交的材料")

    # 工具真的执行了：第一次请求带工具调用，第二次请求带着它们的真实返回。
    assert len(adapter.requests) >= 2, adapter.requests
    first_request = adapter.requests[0]
    result_request = adapter.requests[1]
    envelopes = _tool_envelopes(result_request)
    assert set(envelopes) == {call.id for call in calls}, envelopes.keys()

    inspected = envelopes["c_inspect"]
    assert inspected["ok"] is True, inspected
    assert inspected["data"]["basis"]["source"] == "submission"
    assert inspected["data"]["submission_id"] == submission_id
    previews = [card["content_preview"] for card in inspected["data"]["cards"]]
    assert TEXT_BODY_A in previews, "board_inspect 必须读到 A 版正文"
    assert [link["meaning"] for link in inspected["data"]["links"]] == [BOARD_MEANING_A]
    assert b["b_text_id"] not in {card["id"] for card in inspected["data"]["cards"]}

    for call_id in ("c_read_latest", "c_read_submission"):
        read = envelopes[call_id]
        assert read["ok"] is True, read
        assert read["data"]["basis"]["source"] == "submission"
        assert read["data"]["submission_id"] == submission_id
        by_id = {card["id"]: card for card in read["data"]["cards"]}
        assert by_id[seeded["text_id"]]["content"] == TEXT_BODY_A
        assert b["b_text_id"] not in by_id, "B 版新增卡片不得出现在绑定轮次"
        assert seeded["hidden_card"] not in by_id
        assert seeded["excluded_card"] not in by_id

    # A 的 TXT 已被同字节数替换 → 无法确认是提交当时的内容 → 不交付，提示重新提交。
    txt_result = envelopes["c_read_txt"]
    assert txt_result["ok"] is False, txt_result
    assert txt_result["code"] == "material_source_changed", txt_result
    assert "重新提交" in txt_result["error"]["message"]
    # 提交后新增的附件不在 A 版范围。
    b_only = envelopes["c_read_b_only"]
    assert b_only["ok"] is False, b_only
    assert b_only["code"] == "not_in_submission_scope", b_only
    # B 版新增卡片不能通过显式读提交的路径绕过范围。
    b_card_read = envelopes["c_read_b_card"]
    assert b_card_read["ok"] is False, b_card_read
    assert b_card_read["code"] == "not_readable", b_card_read
    # 隐藏 / 未参与的材料也不在提交允许的附件范围。
    hidden_read = envelopes["c_read_hidden"]
    assert hidden_read["ok"] is False, hidden_read
    assert hidden_read["code"] == "not_in_submission_scope", hidden_read
    # 未变化的 PNG 仍然可读（A 版材料里能确认的继续可读）。
    png_payload = _tool_payload(envelopes["c_read_png"])
    assert png_payload["ok"] is True, png_payload

    # 最终模型请求里只出现 A 的内容，B 的一个字都不能出现。
    joined_result = _joined(result_request["messages"])
    assert TEXT_BODY_A in joined_result
    assert TXT_BYTES_B.decode() not in joined_result
    assert TEXT_BODY_B not in joined_result
    assert "B 版新增卡片" not in joined_result
    assert B_ONLY_BODY.decode() not in joined_result
    assert EXCLUDED_BODY.decode() not in joined_result
    assert HIDDEN_BODY.decode() not in joined_result

    # 首轮（追问轮的第一条模型请求）材料入口同样是 A 版：
    joined_first = _joined(first_request["messages"])
    assert TEXT_BODY_A in joined_first
    assert TEXT_BODY_B not in joined_first
    assert "B 版新增卡片" not in joined_first
    assert B_ONLY_BODY.decode() not in joined_first
    assert "重新提交" in joined_first, "被替换的 A 材料必须在材料层如实提示重新提交"
    assert HIDDEN_BODY.decode() not in joined_first
    assert EXCLUDED_BODY.decode() not in joined_first
    images = [
        part for message in first_request["messages"] for part in (message.images or [])
    ]
    assert images, "A 版 PNG 应该作为图片交付给模型"
    assert any(part.data_url.startswith("data:image/png") for part in images)

    _dump_evidence(
        "bound-followup-a-then-b",
        adapter,
        {
            "submission_id": submission_id,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in calls
            ],
            "tool_results": envelopes,
            # 证据里逐条列出「哪些内容出现过 / 哪些必须不出现」（由断言保证）。
            "markers": {
                "A_TEXT_BODY(应出现)": TEXT_BODY_A,
                "A_TXT_ORIGINAL(已被替换,不应出现)": TXT_BYTES_A.decode(),
                "B_TEXT_BODY(不应出现)": TEXT_BODY_B,
                "B_REPLACED_BODY(不应出现)": TXT_BYTES_B.decode(),
                "B_ONLY_BODY(不应出现)": B_ONLY_BODY.decode(),
                "EXCLUDED_BODY(不应出现)": EXCLUDED_BODY.decode(),
                "HIDDEN_BODY(不应出现)": HIDDEN_BODY.decode(),
                "RESUBMIT_HINT(应出现)": "重新提交",
                "A_LINK_MEANING(应出现)": BOARD_MEANING_A,
            },
        },
    )


# ---------------------------------------------------------------------------
# 2) 旧记录无法核对版本 + 源文件不可读：不交付，提示重新提交（单元口径）
# ---------------------------------------------------------------------------


def _record_legacy_submission(conn, state: dict) -> dict:
    """旧记录形状：提交行 + 可见投影，但**没有** materialVersions（无法核对版本）。"""
    snapshot = submission.project_snapshot(state)
    conn.execute(
        "INSERT OR REPLACE INTO board_submissions (id, board_id, seq, status,"
        " before_state, after_state, visible, expressions, baseline, delivery,"
        " content_hash, error, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "sub_legacy_28",
            BOARD,
            1,
            "succeeded",
            models.dumps(submission._empty_projection(first_submission=True)),
            models.dumps(snapshot),
            models.dumps(snapshot),
            "[]",
            "{}",
            "{}",
            "",
            None,
            models.now_iso(),
        ),
    )
    return snapshot


def _make_txt_card_board(ctx: AppContext, tmp_path: Path) -> tuple[dict, str]:
    txt = _ready(ctx.attachments, tmp_path, "旧记录.txt", TXT_BYTES_A)
    board_store.ensure_board(ctx.conn, board_id=BOARD)
    state = board_store.current_state(ctx.conn, BOARD)
    state, card_id = _add_file_card(state, txt, "旧记录.txt")
    board_store.save_board(ctx.conn, BOARD, state, reason="seed-legacy")
    _record_legacy_submission(ctx.conn, state)
    return {"txt": txt, "card_id": card_id}, "sub_legacy_28"


def test_legacy_snapshot_without_frozen_versions_is_not_delivered(app_ctx, tmp_path):
    """旧记录只登记文件名 / 大小：无法核对内容版本 → 不读取、不交付，提示重新提交。"""
    ctx = app_ctx
    seeded, submission_id = _make_txt_card_board(ctx, tmp_path)
    # 用户后来把文件换成同字节数的另一份内容（同名同大小都不能证明没变）。
    target = ctx.attachments.content_target(seeded["txt"].id)
    path = Path(target[0]) if isinstance(target, tuple) else Path(target)
    path.write_bytes(TXT_BYTES_B)

    resolved = materials.submission_materials(
        ctx.conn,
        board_id=BOARD,
        submission_id=submission_id,
        attachments=ctx.attachments,
    )
    assert resolved["available"] is True
    stale = {item["cardId"]: item for item in resolved["stale"]}
    assert seeded["card_id"] in stale, resolved["stale"]
    assert "重新提交" in stale[seeded["card_id"]]["reason"]
    entry = {item["cardId"]: item for item in resolved["materials"]}[seeded["card_id"]]
    assert entry["read"] is False
    assert TXT_BYTES_B.decode() not in entry["text"]
    assert resolved["images"] == []
    scope = materials.submission_attachment_scope(
        ctx.conn,
        board_id=BOARD,
        submission_id=submission_id,
        attachments=ctx.attachments,
    )
    assert scope["allowed"][seeded["txt"].id]["sha256"] == ""
    assert seeded["txt"].id in scope["stale"]
    assert "重新提交" in scope["stale"][seeded["txt"].id]


def test_unreadable_source_is_not_delivered_and_asks_resubmit(app_ctx, tmp_path):
    """提交后源文件不可读：材料层与 read_attachment 都不交付，提示重新提交。"""
    import asyncio

    ctx = app_ctx
    seeded = _seed_a(ctx, tmp_path)
    topic = ctx.topics.nodes.create_topic("源文件不可读").id
    submission_id = _submit_a_sync(ctx, topic)
    # 删掉 QIO 保存的内容（源文件不可读）。
    target = ctx.attachments.content_target(seeded["txt"].id)
    path = Path(target[0]) if isinstance(target, tuple) else Path(target)
    path.unlink()

    resolved = materials.submission_materials(
        ctx.conn,
        board_id=BOARD,
        submission_id=submission_id,
        attachments=ctx.attachments,
    )
    stale = {item["cardId"]: item for item in resolved["stale"]}
    assert seeded["txt_card"] in stale
    assert "重新提交" in stale[seeded["txt_card"]]["reason"]
    assert "无法核对" in stale[seeded["txt_card"]]["reason"]
    # 没变的 PNG 仍可交付（只交付能确认的部分）。
    assert seeded["png_card"] not in stale
    assert {item["cardId"] for item in resolved["images"]} == {seeded["png_card"]}

    # read_attachment 在同一条授权规则下也必须拒绝，且说清「重新提交」。
    class _App:
        def __init__(self, conn):
            self.conn = conn

    from agent.interactive import runtime as runtime_module
    from agent.tools.attachment_tools import ReadAttachmentTool as _Tool

    tool = _Tool(ctx.attachments)
    token = runtime_module.set_board_turn(
        runtime_module.BoardTurnContext(
            turn_id="turn_read_unreadable",
            mode=runtime_module.MODE_INTERACTIVE,
            board_id=BOARD,
            submission_id=submission_id,
        )
    )
    try:
        result = asyncio.run(tool.run(attachment_id=seeded["txt"].id))
    finally:
        runtime_module.reset_board_turn(token)
    assert result.ok is False, result
    assert result.code == "material_source_changed", result
    assert "重新提交" in str(result.error)
    assert TXT_BYTES_B.decode() not in str(result.data)


# ---------------------------------------------------------------------------
# 3) 快照读不到的绑定首轮：材料入口是可识别失败，不给当前板面
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bound_first_round_with_missing_snapshot_marks_materials_unavailable(
    app_ctx, tmp_path
):
    ctx = app_ctx
    # 当前板面有内容，但提交记录没有当时的快照 → 本轮不许读当前板面。
    board_store.ensure_board(ctx.conn, board_id=BOARD)
    state = board_store.current_state(ctx.conn, BOARD)
    state, _card = _add_text_card(state, "当前板面正文")
    board_store.save_board(ctx.conn, BOARD, state, reason="current-only")
    ctx.conn.execute(
        "INSERT OR IGNORE INTO board_submissions (id, board_id, seq, status, created_at)"
        " VALUES (?, ?, 1, 'accepted', ?)",
        ("sub_no_snapshot_28", BOARD, models.now_iso()),
    )

    calls = [
        ToolCall(id="c_inspect", name="board_inspect", arguments={}),
        ToolCall(id="c_read", name="board_read_cards", arguments={}),
    ]
    adapter = _ToolThenAnswerAdapter(calls)
    topic = ctx.topics.nodes.create_topic("快照缺失首轮").id
    await _run_bound_turn(ctx, topic, "sub_no_snapshot_28", adapter, "看看这次提交")

    first_request = adapter.requests[0]
    joined_first = _joined(first_request["messages"])
    assert '"materials_status": "unavailable"' in joined_first, joined_first[:2000]
    assert "读不到这次提交的内容，请重新提交。" in joined_first
    assert '"materials_available": false' in joined_first
    assert "当前板面正文" not in joined_first

    envelopes = _tool_envelopes(adapter.requests[1])
    inspected = envelopes["c_inspect"]
    assert inspected["ok"] is False, inspected
    assert inspected["code"] == "submission_snapshot_unavailable", inspected
    assert inspected["error"]["message"] == "读不到这次提交的内容，请重新提交。"
    assert "当前板面正文" not in json.dumps(inspected, ensure_ascii=False)
    read = envelopes["c_read"]
    assert read["ok"] is False, read
    assert read["code"] == "submission_snapshot_unavailable", read
    assert "当前板面正文" not in json.dumps(read, ensure_ascii=False)

    _dump_evidence(
        "first-round-missing-snapshot",
        adapter,
        {
            "tool_calls": [{"id": c.id, "name": c.name} for c in calls],
            "tool_results": envelopes,
            "markers": {
                "CURRENT_BOARD_BODY(不应出现)": "当前板面正文",
                "RESUBMIT_NOTICE(应出现)": "读不到这次提交的内容，请重新提交。",
                "MATERIALS_UNAVAILABLE(应出现)": '"materials_status": "unavailable"',
            },
        },
    )


# ---------------------------------------------------------------------------
# 4) 普通互动 / 未绑定提交保持原规则
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unbound_interactive_turn_still_reads_current_board(app_ctx, tmp_path):
    """没有提交绑定的互动轮：仍按既有规则读当前板面（#28 不扩大也不缩小它的范围）。"""
    ctx = app_ctx
    board_store.ensure_board(ctx.conn, board_id=BOARD)
    state = board_store.current_state(ctx.conn, BOARD)
    state, card_id = _add_text_card(state, "当前板面正文")
    board_store.save_board(ctx.conn, BOARD, state, reason="current")

    calls = [
        ToolCall(id="c_inspect", name="board_inspect", arguments={}),
        ToolCall(id="c_read", name="board_read_cards", arguments={}),
    ]
    adapter = _ToolThenAnswerAdapter(calls)
    topic = ctx.topics.nodes.create_topic("未绑定互动").id
    await _run_bound_turn(ctx, topic, "", adapter, "看看板面")

    envelopes = _tool_envelopes(adapter.requests[1])
    inspected = envelopes["c_inspect"]
    assert inspected["ok"] is True, inspected
    assert inspected["data"]["basis"]["source"] == "current"
    assert any(
        card["content_preview"] == "当前板面正文"
        for card in inspected["data"]["cards"]
    )
    read = envelopes["c_read"]
    assert read["ok"] is True, read
    assert read["data"]["basis"]["source"] == "current"
    assert read["data"]["cards"][0]["content"] == "当前板面正文"
