# -*- coding: utf-8 -*-
"""D 独立契约测试：记忆契约（docs/qio-refactor-contract.md §5）。

覆盖：封块摘要完整覆盖（不得只取尾部）、长输入分段可追溯、滚动摘要只处理
未覆盖消息、覆盖位置跨重启持久化、摘要失败不阻塞、固定提示词来自 prompts.py。

口径：契约措辞 → 可验证断言；全部 fake provider，不联网、不读密钥。
阶段 1（B 的实现未合并）允许失败，失败消息写明契约条款。

运行（只跑本文件）：
    cd backend
    & 'D:\\qio-dev\\qio\\backend\\.venv\\Scripts\\python.exe' -m pytest tests/test_p1_d_memory_contract.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import prompts
from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.memory.summary import summarize_fragment_outcome
from agent.memory.contract import FragmentSummary
from agent.memory.rolling import summarize_rolling_outcome
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _summary(title: str = "标题", summary: str = "摘要", *, entities=None, keywords=None) -> str:
    return json.dumps(
        {
            "title": title,
            "summary": summary,
            "entities": list(entities or []),
            "keywords": list(keywords or []),
        },
        ensure_ascii=False,
    )


def _scripted_summaries(count: int, prefix: str = "分段") -> list[StreamScript]:
    """足够多的合法摘要响应：分段摘要 + 逐层合并都会各消耗一次模型调用。"""
    return [StreamScript(text=_summary(title=f"{prefix}{index}")) for index in range(count)]


def _prompt_of(adapter: FakeStreamAdapter, index: int = 0) -> str:
    request = adapter.requests[index]
    messages = request["messages"]
    return "\n".join(str(m.content or "") for m in messages)


# ---------------------------------------------------------------------------
# 封块摘要：完整覆盖（§5 第 1、2 条）
# ---------------------------------------------------------------------------


async def test_closed_summary_prompt_uses_frozen_text():
    """§2.1：封块摘要的固定规则必须来自 prompts.PROMPTS['CLOSED_SUMMARY_SYSTEM']。"""
    adapter = FakeStreamAdapter([StreamScript(text=_summary())])
    outcome = await summarize_fragment_outcome(
        adapter, [{"role": "user", "content": "一段对话原文"}]
    )
    assert outcome.value is not None, f"摘要校验失败：{outcome.error}"
    prompt = _prompt_of(adapter)
    assert prompts.PROMPTS["CLOSED_SUMMARY_SYSTEM"] in prompt, (
        "契约 §2.1：封块摘要必须使用 PROMPTS['CLOSED_SUMMARY_SYSTEM']（固定文案唯一来源）；"
        f"实际提示词开头={prompt[:200]!r}"
    )


async def test_closed_summary_covers_head_and_tail_of_long_transcript():
    """§5：封块摘要覆盖完整片段，不得只取尾部（删除 [-12_000:] 一类截断）。"""
    head = "HEAD_SENTINEL_9d2 开头必须出现：" + "甲" * 6000
    middle = "MIDDLE_SENTINEL_9d2 中间必须出现：" + "乙" * 6000
    tail = "TAIL_SENTINEL_9d2 结尾必须出现：" + "丙" * 6000
    messages = [
        {"id": "m_0001", "role": "user", "content": head},
        {"id": "m_0002", "role": "assistant", "content": middle},
        {"id": "m_0003", "role": "user", "content": tail},
    ]
    # 契约 §5 要求分段摘要 + 合并；每段/每次合并都是一次模型调用，脚本必须够用。
    adapter = FakeStreamAdapter(_scripted_summaries(120))
    outcome = await summarize_fragment_outcome(adapter, messages)
    assert outcome.value is not None, f"摘要校验失败：{outcome.error}"
    prompt = "\n".join(
        _prompt_of(adapter, index) for index in range(len(adapter.requests))
    )
    for sentinel in ("HEAD_SENTINEL_9d2", "MIDDLE_SENTINEL_9d2", "TAIL_SENTINEL_9d2"):
        assert sentinel in prompt, (
            "契约 §5：封块摘要只取了部分片段（开头/中间/结尾之一丢失），"
            f"缺少 {sentinel}；实际提示词长度={len(prompt)}"
        )


async def test_long_transcript_is_segmented_and_mergeable():
    """§5：长输入按完整消息分段；分段后必须保留消息身份，并用 MERGE_SUMMARY_SYSTEM 合成。"""
    messages = []
    sentinels = []
    for index in range(6):
        sentinel = f"SEG_SENTINEL_{index}_f19"
        sentinels.append(sentinel)
        messages.append(
            {
                "id": f"m_{index:04d}",
                "role": "user" if index % 2 == 0 else "assistant",
                "content": sentinel + "：" + "内容" * 3000,
            }
        )
    adapter = FakeStreamAdapter(_scripted_summaries(400))
    outcome = await summarize_fragment_outcome(adapter, messages)
    assert outcome.value is not None, f"摘要校验失败：{outcome.error}"
    prompts_text = "\n".join(_prompt_of(adapter, i) for i in range(len(adapter.requests)))
    missing = [s for s in sentinels if s not in prompts_text]
    assert not missing, (
        "契约 §5：分段摘要没有覆盖完整片段（这些段落从未出现在任何模型调用里）："
        f"{missing}"
    )
    if len(adapter.requests) > 1:
        assert prompts.PROMPTS["MERGE_SUMMARY_SYSTEM"] in prompts_text, (
            "契约 §5：多段摘要必须用 PROMPTS['MERGE_SUMMARY_SYSTEM'] 合成，"
            "且不得只取尾部"
        )
        for mid in (f"m_{i:04d}" for i in range(len(messages))):
            assert mid in prompts_text, (
                f"契约 §5：长输入分段必须保留消息身份（消息 id/序号），缺少 {mid}"
            )


# ---------------------------------------------------------------------------
# 滚动摘要：只处理未覆盖消息 + 覆盖位置持久化（§5 第 3 条）
# ---------------------------------------------------------------------------


def _make_ctx(tmp_path: Path, name: str = "roll.db") -> tuple[AppContext, Path]:
    db_path = tmp_path / name
    conn = connect(db_path)
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path / ("data_" + name)), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx, db_path


def _clear_consolidation_cooldown(ctx: AppContext, topic_id: str) -> None:
    """把 cooldown 拨到很久以前（保留 B 可能新增的其它 meta 键）。"""
    fragment = ctx.fragments.get_or_create_open(topic_id)
    meta = dict(fragment.meta or {})
    meta["consolidated_at"] = "2000-01-01T00:00:00+00:00"
    ctx.conn.execute(
        "UPDATE fragments SET meta = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), fragment.id),
    )
    ctx.conn.commit()


async def test_rolling_summary_prompt_uses_frozen_text_and_old_summary():
    """§2.1：滚动摘要使用 PROMPTS['ROLLING_SUMMARY_SYSTEM']，且带旧摘要 + 新消息。"""
    adapter = FakeStreamAdapter([StreamScript(text=_summary(title="滚动"))])
    outcome = await summarize_rolling_outcome(
        adapter,
        "旧摘要-OLD-SENTINEL",
        [{"role": "user", "content": "新消息-NEW-SENTINEL"}],
    )
    assert outcome.value is not None, f"滚动摘要校验失败：{outcome.error}"
    prompt = _prompt_of(adapter)
    assert prompts.PROMPTS["ROLLING_SUMMARY_SYSTEM"] in prompt, (
        "契约 §2.1：滚动摘要必须使用 PROMPTS['ROLLING_SUMMARY_SYSTEM']"
    )
    assert "旧摘要-OLD-SENTINEL" in prompt, "契约 §5：滚动摘要必须带上已有摘要"
    assert "新消息-NEW-SENTINEL" in prompt, "契约 §5：滚动摘要必须带新增消息"


async def test_rolling_summary_only_covers_uncovered_messages_after_restart(tmp_path: Path):
    """§5：滚动摘要只处理未覆盖的新消息；覆盖位置与输入版本持久化，可重启恢复。"""
    ctx, db_path = _make_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("滚动摘要契约测试").id
    ctx.memory.append_message(topic_id=topic, role="user", content="COVERED_A_7c1：旧消息一")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="COVERED_B_7c1：旧消息二")

    first = FakeStreamAdapter([StreamScript(text=_summary(title="第一次滚动", summary="第一次滚动摘要正文"))])
    assert await ctx.memory_lifecycle.consolidate(topic, first) is True, "装置失效：第一次滚动摘要未完成"
    assert len(first.requests) == 1
    assert "COVERED_A_7c1" in _prompt_of(first, 0), "装置失效：第一次滚动摘要没有看到已覆盖消息"

    ctx.memory.append_message(topic_id=topic, role="user", content="UNCOVERED_C_7c1：真正新增的消息")
    _clear_consolidation_cooldown(ctx, topic)
    second = FakeStreamAdapter([StreamScript(text=_summary(title="第二次滚动", summary="第二次滚动摘要正文"))])
    assert await ctx.memory_lifecycle.consolidate(topic, second) is True
    second_prompt = _prompt_of(second, 0)
    assert "UNCOVERED_C_7c1" in second_prompt, "契约 §5：滚动摘要跳过了未覆盖的新消息"
    assert "COVERED_A_7c1" not in second_prompt, (
        "契约 §5：滚动摘要重复处理已覆盖原文（应只处理未覆盖的新消息）"
    )
    assert "COVERED_B_7c1" not in second_prompt, (
        "契约 §5：滚动摘要重复处理已覆盖原文（应只处理未覆盖的新消息）"
    )

    # 模拟重启：关掉连接，用同一个库文件新建 AppContext，再追加一条新消息。
    ctx.conn.close()
    conn = connect(db_path)
    apply_migrations(conn)
    restarted = AppContext(Settings(data_dir=tmp_path / "data_rolled_restart"), conn, EventBus())
    restarted.credentials._kr = MemoryKeyring()
    restarted.memory.append_message(
        topic_id=topic, role="user", content="AFTER_RESTART_D_7c1：重启后的新消息"
    )
    _clear_consolidation_cooldown(restarted, topic)
    third = FakeStreamAdapter([StreamScript(text=_summary(title="重启后滚动", summary="重启后滚动摘要正文"))])
    assert await restarted.memory_lifecycle.consolidate(topic, third) is True
    third_prompt = _prompt_of(third, 0)
    assert "AFTER_RESTART_D_7c1" in third_prompt, (
        "契约 §5：重启后滚动摘要没有处理未覆盖的新消息"
    )
    for covered in ("COVERED_A_7c1", "COVERED_B_7c1", "UNCOVERED_C_7c1"):
        assert covered not in third_prompt, (
            f"契约 §5：覆盖位置没有持久化/恢复（重启后再次把 {covered} 当新消息处理）"
        )
    restarted.conn.close()


async def test_rolling_summary_failure_does_not_raise_or_block():
    """§5：摘要失败不得卡住对话、不得回退封块（这里只验证异常路径不抛出）。"""
    adapter = FakeStreamAdapter([StreamScript(error=RuntimeError("provider down"))])
    outcome = await summarize_rolling_outcome(adapter, "旧摘要", [{"role": "user", "content": "新消息"}])
    assert outcome.value is None
    assert outcome.error, "契约 §5：摘要失败必须给出可读失败原因（不能静默）"


async def test_closed_summary_failure_returns_readable_reason():
    """§5：原始对话保留、摘要失败可回查；失败必须以可读原因返回而不是抛异常。"""
    adapter = FakeStreamAdapter([StreamScript(text="不是 JSON 的模型输出")])
    outcome = await summarize_fragment_outcome(adapter, [{"role": "user", "content": "原文"}])
    assert outcome.value is None
    assert outcome.error, "契约 §5：摘要校验失败必须给出可读原因"
