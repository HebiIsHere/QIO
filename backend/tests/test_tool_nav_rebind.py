"""工具导航（create_topic / switch_topic）在同一轮内换话题时的归属。

阶段 1 把「本轮归属」固定成轮前写下的绑定，修好了「回复在跑、用户改导航
会把已提交消息搬走」。但工具自己换话题也被一起排除在外了：建话题的那一轮
（用户提问 + 助手回答）留在**上一个**话题里，新话题的历史从第二轮才开始 ——
追问看不到前提，检索也找不到。

这里固定两条互相制约的行为：

* 本轮的**工具**导航：整轮跟着走（用户消息搬进新话题的开放片段，回答写在
  那里，绑定收尾指向新话题）；
* 本轮的**用户**导航：一轮消息一步不动（阶段 1 的保证，不允许回退）。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class ScriptedAdapter:
    """按脚本先请求工具调用，脚本用完后给最终回答。"""

    mode = "native"
    model = "fake-scripted"

    def __init__(self, script, on_answer=None) -> None:
        self.script = list(script)
        self.on_answer = on_answer
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        if self.script:
            name, args = self.script.pop(0)
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id=f"c{self.calls}", name=name, arguments=args)],
                )
            )
        if self.on_answer is not None:
            self.on_answer()
        return Completion(message=ChatMessage(role="assistant", content="回答完了"))


def _new_topic(ctx: AppContext, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


def _open_fragment(ctx: AppContext, topic_id: str) -> str:
    return ctx.fragments.get_or_create_open(topic_id).id


def _topic_by_name(ctx: AppContext, name: str) -> str | None:
    row = ctx.conn.execute(
        "SELECT id FROM nodes WHERE type = 'topic' AND name = ?", (name,)
    ).fetchone()
    return row["id"] if row is not None else None


def _fragments_of(ctx: AppContext, topic_id: str) -> list:
    return ctx.conn.execute(
        "SELECT * FROM fragments WHERE topic_id = ?", (topic_id,)
    ).fetchall()


def _turn_messages(ctx: AppContext, turn_id: str) -> list:
    return ctx.conn.execute(
        "SELECT id, role, fragment_id FROM messages WHERE turn_id = ? ORDER BY created_at",
        (turn_id,),
    ).fetchall()


async def test_create_topic_mid_turn_keeps_the_whole_turn_in_the_new_topic(ctx: AppContext):
    old_topic = _new_topic(ctx, "旧话题")
    old_frag = _open_fragment(ctx, old_topic)
    ctx.navigation.enter_topic(old_topic)

    adapter = ScriptedAdapter(
        [("create_topic", {"name": "原神爆料检索", "reason": "与本话题无关，属新话题"})]
    )
    ctx.build_adapter = AsyncMock(return_value=adapter)

    turn = ctx.turns.submit("帮我在内鬼网站里搜索下原神薇斯纳相关的爆料", old_topic)
    await ctx.turns.wait(turn.turn_id, timeout=10)

    new_topic = _topic_by_name(ctx, "原神爆料检索")
    assert new_topic is not None, "这一轮应当已经建出新话题"
    fragments = _fragments_of(ctx, new_topic)
    assert len(fragments) == 1, "新话题的第一个片段应当由这一轮创建"
    new_frag = fragments[0]["id"]

    messages = _turn_messages(ctx, turn.turn_id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert {m["fragment_id"] for m in messages} == {new_frag}

    # 旧话题不该被这一轮污染
    assert ctx.conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE fragment_id = ?", (old_frag,)
    ).fetchone()["c"] == 0

    # 绑定收尾跟随这一轮的真实归属
    binding = ctx.bindings.binding_for(turn.turn_id)
    assert (binding.topic_id, binding.fragment_id) == (new_topic, new_frag)

    # 片段边界要覆盖被搬进来的用户消息，而不是只从回答开始
    assert fragments[0]["start_message_id"] == messages[0]["id"]
    assert fragments[0]["end_message_id"] == messages[1]["id"]


async def test_user_navigation_during_the_turn_keeps_the_turn_in_its_bound_fragment(
    ctx: AppContext,
):
    old_topic = _new_topic(ctx, "旧话题")
    other_topic = _new_topic(ctx, "用户点开的另一话题")
    old_frag = _open_fragment(ctx, old_topic)
    ctx.navigation.enter_topic(old_topic)

    def user_navigates_while_running() -> None:
        # 模拟另一个请求里的用户导航（不是工具路径）：它只该影响后续轮次
        ctx.navigation.enter_topic(other_topic)

    adapter = ScriptedAdapter(
        [("create_topic", {"name": "工具中途建的话题", "reason": "看起来是新话题"})],
        on_answer=user_navigates_while_running,
    )
    ctx.build_adapter = AsyncMock(return_value=adapter)

    turn = ctx.turns.submit("这句话先落在旧话题里", old_topic)
    await ctx.turns.wait(turn.turn_id, timeout=10)

    binding = ctx.bindings.binding_for(turn.turn_id)
    assert (binding.topic_id, binding.fragment_id) == (old_topic, old_frag)
    assert {m["fragment_id"] for m in _turn_messages(ctx, turn.turn_id)} == {old_frag}
    # 用户的选择赢：Anchor 停在用户点的话题上
    assert ctx.navigation.anchors.get_active().topic_id == other_topic


async def test_switch_topic_mid_turn_moves_the_turn_to_the_target_topic(ctx: AppContext):
    start_topic = _new_topic(ctx, "起始话题")
    target_topic = _new_topic(ctx, "目标话题")
    start_frag = _open_fragment(ctx, start_topic)
    target_frag = _open_fragment(ctx, target_topic)
    ctx.navigation.enter_topic(start_topic)

    adapter = ScriptedAdapter(
        [("switch_topic", {"topic_id": target_topic, "reason": "用户说到目标话题"})]
    )
    ctx.build_adapter = AsyncMock(return_value=adapter)

    turn = ctx.turns.submit("换成目标话题聊", start_topic)
    await ctx.turns.wait(turn.turn_id, timeout=10)

    messages = _turn_messages(ctx, turn.turn_id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert {m["fragment_id"] for m in messages} == {target_frag}
    assert ctx.conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE fragment_id = ?", (start_frag,)
    ).fetchone()["c"] == 0

    binding = ctx.bindings.binding_for(turn.turn_id)
    assert (binding.topic_id, binding.fragment_id) == (target_topic, target_frag)
