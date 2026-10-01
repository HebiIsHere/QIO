"""TurnOrchestrator: the readable single-turn pipeline.

    begin          → adapter + anchor + trace begin
    build_context  → topic prediction/classify + memory append + injection
    execute_loop   → AgentLoop (planning/tool/observing)
    persist        → move message on topic switch + assistant message
    post_turn      → fragment close / consolidation
    finish         → trace finish + result

It composes the process-level services held by AppContext (facade); it owns
only per-turn flow, so AppContext stops being a pile of domain details.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from agent.api.events import EventType, make_event
from agent.services.navigation import (
    clear_tool_navigation,
    set_tool_nav_turn,
    take_tool_navigation,
)

# 边界策略的运行模式（阶段 4）：
# off     不评估；
# shadow  只记录建议（默认先观察，不实际切分）；
# enabled 确定性规则实际生效。
BOUNDARY_MODE_KEY = "fragment.boundary_mode"
DEFAULT_BOUNDARY_MODE = "shadow"


def verification_raw(result) -> dict | None:
    """assistant 消息的 `raw`：把后端核对通过的结论一起存下来。

    前端在回答下方渲染「后端已核对」那一行就靠它（见 core/turn_facts.py）；
    这一轮没有核对结论时返回 None，落库行为与以前完全一样（raw = 空对象）。
    """
    verification = getattr(result, "verification", None)
    return {"verified": verification} if verification else None


@dataclass
class _Plan:
    topic: str
    prediction: Any = None
    decision: Any = None
    aux_topic_ids: list[str] = field(default_factory=list)
    new_topic_candidate: bool = False
    reason: str = ""
    extra_note: str = ""
    msg_id: str | None = None
    payload: Any = None
    prompt: str = ""


class TurnOrchestrator:
    def __init__(self, app) -> None:
        self.app = app

    async def execute(self, ctx) -> None:
        """一轮的完整流水线。

        `TurnManager` 负责 turn 的终态与唯一的 TURN_END；本方法只负责推进阶段，
        并在每个阶段之间检查取消（用户点「停止」= 停止这个 turn 的后续一切）。

        Trace 的时间窗从这里打开（而不是等到 begin() 里解析完话题之后）：
        凭据/能力探测也是这一轮的时间，「turn 多久」必须与「时间去哪了」对得上。
        """
        app = self.app
        self._begin_trace(ctx)
        if ctx.notify:
            await app._execute_notify_turn(ctx)
            return
        # 本轮的工具导航（create_topic / switch_topic）要能被认出来：工具在
        # turn 派生的 task 里执行，读得到这个标记；别的请求（用户导航）读不到。
        set_tool_nav_turn(ctx.turn_id)
        # 本轮产生的审批绑定到本 turn（工具不需要各自传参）
        app.approvals.set_context(turn_id=ctx.turn_id)
        try:
            await self._execute_turn(ctx)
        finally:
            app.approvals.set_context(turn_id=None)
            set_tool_nav_turn(None)
            clear_tool_navigation(ctx.turn_id)

    async def _execute_turn(self, ctx) -> None:
        """阶段必须铺满时间轴（见 trace/phases.py）。

        每个阶段是一个顶层区段：上下文装配 / 循环（模型 + 工具 + 审批）/ 落库 /
        收尾记忆处理 / 终态。真实事故里「51.5 秒的 turn 只有 1.7 秒模型耗时」，
        差的那 50 秒必须落进这些具名阶段之一，而不是 unknown。
        """
        app = self.app
        tracer = ctx.trace
        if ctx.cancelled:
            return
        adapter = await self.begin(ctx)
        if adapter is None:
            return
        if ctx.cancelled:
            return
        with tracer.phase("context_assembly"):
            plan = await self.build_context(ctx, adapter)
        if ctx.cancelled:
            return
        with tracer.phase("agent_loop"):
            result = await self.execute_loop(ctx, adapter, plan)
        if result is None:
            app.bindings.mark_status(ctx.turn_id, ctx.status or "failed")
            return
        # 迟到的系统通知（例如子任务恰好在最后一次 planning 之后完成）：
        # 塞进本轮已经读不到了，别丢掉 —— 让它自己成为一轮。
        for notice in result.unread_notices:
            app.turns.submit(notice, None, notify=True)
        # 取消检查点：不得把取消后产生的内容保存成正常最终回答
        if ctx.cancelled or result.cancelled:
            ctx.cancelled = True
            app.bindings.mark_status(ctx.turn_id, "cancelled")
            app.trace_store.finish(ctx.turn_id, "cancelled")
            return
        with tracer.phase("persistence"):
            final_topic = await self.persist(ctx, adapter, plan, result)
        ctx.final_content = result.final_content
        ctx.usage = {
            "iterations": result.iterations_used,
            "tokens": result.tokens_used,
            "tool_calls": result.tool_calls_made,
        }
        if ctx.cancelled:
            # 答案已经落库（用户能看见），但不再做收尾记忆处理
            app.bindings.mark_status(ctx.turn_id, "cancelled")
            app.trace_store.finish(
                ctx.turn_id, "cancelled", final_topic=final_topic, final_preview=ctx.final_content or ""
            )
            return
        with tracer.phase("memory_post"):
            await self.post_turn(ctx, adapter, plan, final_topic)
        if ctx.cancelled:
            app.bindings.mark_status(ctx.turn_id, "cancelled")
            app.trace_store.finish(
                ctx.turn_id, "cancelled", final_topic=final_topic, final_preview=ctx.final_content or ""
            )
            return
        with tracer.phase("finalize"):
            # 高影响知识候选：只有在回答真的完成之后才进协议（前端也只在 TURN_END
            # 之后才显示），绝不打断正在进行的回答（spec 第 15~16 条）。
            await app.emit_knowledge_candidates(ctx)
            await self.advance_anchor(ctx, final_topic)
            self.finish(ctx, plan, result, final_topic)

    # -- stages -----------------------------------------------------------

    def _begin_trace(self, ctx):
        """打开这一轮的 Trace 时间窗，并登记排队等待时长。

        排队等待不属于「执行」（duration_ms 只覆盖执行期），但它同样是用户等的时间，
        所以单独记一个显式数字（phases.notes.queue_wait_ms），而不是丢掉。
        """
        from agent.trace.recorder import TurnTracer

        app = self.app
        tracer = getattr(ctx, "trace", None)
        if tracer is None:
            tracer = TurnTracer(app.trace_store, ctx.turn_id)
            ctx.trace = tracer
        app.trace_store.ensure_started(ctx.turn_id, initial_topic=ctx.initial_topic)
        accepted = getattr(ctx, "accepted_perf", None)
        started = getattr(ctx, "started_perf", None)
        if accepted is not None and started is not None:
            tracer.note("queue_wait_ms", max(0, int((started - accepted) * 1000)))
        return tracer

    async def begin(self, ctx):
        from agent.memory.fragment import resolve_max_tokens, resolve_max_turns
        from agent.services.app import make_warning
        from agent.trace.recorder import TurnTracer

        app = self.app
        tracer = getattr(ctx, "trace", None)
        if tracer is None:
            tracer = TurnTracer(app.trace_store, ctx.turn_id)
            ctx.trace = tracer
        # 凭据/能力探测（首次可能要真实打一次 provider）：以前这段时间在 Trace
        # 开场之前，既不算时长也没有分区。
        with tracer.phase("adapter_setup"):
            adapter = await app.build_adapter()
        if adapter is None:
            # 凭据不可用：状态进协议（前端据此给人话提示），警告负责显示，
            # 二者职责不同（spec 第 43~46 条）。
            await app.announce_credential_unavailable(ctx.turn_id)
            await app.bus.publish(
                make_warning(
                    "还没有配置可用的模型凭据：请在「设置 → 凭据」里添加一个 API Key 后再对话",
                    turn_id=ctx.turn_id,
                )
            )
            ctx.result = {"ok": False, "reason": "no_credential"}
            # 终态由 TurnManager 收口：没有凭据也必须产生 TURN_END(unavailable)，
            # 否则前端会永远停在 running。
            ctx.status = "unavailable"
            ctx.error = "no_credential"
            app.bindings.mark_status(ctx.turn_id, "unavailable")
            # 没有凭据也是这一轮的终态：Trace 不能永远停在 running（阶段时间由
            # TurnManager 的兜底收口写入）。
            app.trace_store.finish(ctx.turn_id, "unavailable", error="no_credential")
            return None
        with tracer.phase("turn_setup"):
            # 能力模式与降级：正常时不显示，降级时用户得到一次性低干扰提示
            await app.announce_capability(adapter, ctx.turn_id)
            # 封块阈值是「轮」不是「消息条数」（第三阶段 spec 第 57~60 条）
            app.fragments.max_turns = resolve_max_turns(app.settings_store)
            # 阶段 4：内容长度也参与兜底（长度到点分块，但不代表任务完成）
            app.fragments.max_tokens = resolve_max_tokens(app.settings_store)
            topic = ctx.initial_topic or app.current_topic()

            # ── 轮前：固定本轮归属（Topic / Fragment）──────────────────────
            # 顺序是有意的：先解析「用户明确要求切换」，再落实「已授权的历史接续」，
            # 最后才写绑定。绑定一旦写下，本轮的消息归属就不再受后续导航影响。
            from agent.services.navigation import detect_explicit_navigation

            explicit_target = detect_explicit_navigation(
                ctx.message, [(n.id, n.name) for n in app.topics.nodes.list_topics()]
            )
            if explicit_target and explicit_target != topic:
                app.navigation.enter_topic(explicit_target, relate=True)
                topic = explicit_target
            ctx.explicit_target = explicit_target
            ctx.current_topic = topic

            fragment_id: str | None = None
            intent_version: int | None = None
            if ctx.intent_id:
                intent = app.bindings.intent_by_id(ctx.intent_id)
                if intent is not None and intent.state in ("registered", "resolved"):
                    applied = app.navigation.apply_continuation(intent.topic_id, ctx.intent_id)
                    topic = applied.topic_id
                    fragment_id = applied.fragment_id
                    intent_version = intent.version
                    ctx.current_topic = topic
                    await app._publish_anchor_event()
            if fragment_id is None:
                open_fragment = app.fragments.open_fragment(topic)
                fragment_id = open_fragment.id if open_fragment is not None else None

        # Trace 的 initial_topic 以这里解析出的归属为准（开场时还只知道提交时的话题）。
        # trace 在边界判断之前就已经就位：shadow 模式的「建议」就是写进 trace 的。
        app.trace_store.set_initial_topic(ctx.turn_id, topic)

        with tracer.phase("turn_binding"):
            # ── 阶段 4：轮前边界判断（只使用当前输入与此前已完成的上下文）──
            # 确定性规则（明确进入新的交付工作）可以在 enabled 模式下实际生效；
            # shadow 只记录建议；off 不评估。容量仍由 post_turn 在完整轮次边界处理。
            self._apply_boundary_policy(ctx, topic, fragment_id)
            fragment_id = ctx.bound_fragment_id

            ctx.bound_topic = topic
            ctx.bound_fragment_id = fragment_id
            ctx.bound_intent_version = intent_version
            app.bindings.record_binding(
                ctx.turn_id,
                topic,
                fragment_id=fragment_id,
                intent_id=ctx.intent_id,
                intent_version=intent_version,
            )
            # speaking in a topic anchors it (if the anchor is absent or stale)。
            # 写入只走 Navigator：会话起点变化也是「进入话题」这一种导航。
            active_anchor = app.navigation.anchors.get_active()
            if active_anchor is None or active_anchor.topic_id != topic:
                app.navigation.enter_topic(topic)
                await app._publish_anchor_event()
        return adapter

    async def build_context(self, ctx, adapter) -> _Plan:
        from agent.entities.cards import EntityCardService
        from agent.graph.anchors import AnchorService
        from agent.services.affinity import (
            NEW_TOPIC_STRICT,
            TopicMode,
            classify,
            related_topics,
        )
        from agent.trace.redact import preview as _preview

        app = self.app
        topic = ctx.current_topic
        message = ctx.message
        tracer = ctx.trace

        # 明确导航已经在 begin() 里解析并写进本轮绑定（阶段 1）：
        # 这里只读取结果，用于「不再叠加推测切换」与 trace 记录。
        explicit_target = getattr(ctx, "explicit_target", None)

        # 话题预判会用嵌入模型（缺本地模型时降级到规则层）——单独成段，
        # 不要让「第一次加载嵌入模型」的几秒钟变成上下文装配里的黑盒。
        with tracer.phase("topic_prediction"):
            prediction = app.predictor.predict(message, current_topic_id=topic)
            decision = classify(message, prediction, topic, app._entity_card_topics(message))
            if decision.mode == TopicMode.IN_TOPIC:
                aux_topic_ids = related_topics(app.conn, topic, top_n=2)
            elif decision.mode == TopicMode.SWITCH and decision.switch_to:
                aux_topic_ids = [decision.switch_to]
            else:
                aux_topic_ids = []
        new_topic_candidate = False
        reason = ""
        if decision.mode == TopicMode.NEW_TOPIC:
            if decision.closest_score < NEW_TOPIC_STRICT or prediction.is_new_topic_candidate:
                new_topic_candidate = True
                reason = f"最高话题相似度 {decision.closest_score:.2f} 低于阈值，无匹配话题"
        extra_note = ""
        if (
            decision.mode == TopicMode.NEW_TOPIC
            and decision.closest_topic
            and decision.closest_score >= NEW_TOPIC_STRICT
        ):
            n = app.topics.nodes.get_topic(decision.closest_topic)
            extra_note += (
                f"；最相似话题「{n.name if n else decision.closest_topic}」"
                f"（相似度 {decision.closest_score:.2f}），如确属新话题需人工确认"
            )
        if decision.entity_hints:
            names = []
            for tid in decision.entity_hints:
                n = app.topics.nodes.get_topic(tid)
                names.append(n.name if n else tid)
            extra_note += "；提及实体关联话题：" + "、".join(names)

        # 推测切换（spec 第 29~30 条）：内容明显属于另一个话题，但用户没有说要切。
        # 只登记「待确认」，Anchor 留在原地 —— 由用户点「转到这里」才真的切。
        if not explicit_target:
            suggested = (
                decision.switch_to
                if decision.mode == TopicMode.SWITCH and decision.switch_to
                else (prediction.main_topic_id if prediction.suggested_switch else None)
            )
            if suggested and suggested != topic:
                suggestion = app.navigation.request_switch(
                    suggested, reason="这段内容看起来属于另一个话题"
                )
                await app.bus.publish(
                    make_event(
                        EventType.TOPIC_SWITCH_SUGGESTED,
                        {
                            "from_topic_id": topic,
                            "topic_id": suggestion["topic_id"],
                            "topic_name": suggestion["topic_name"],
                            "reason": suggestion["reason"],
                            "turn_id": ctx.turn_id,
                        },
                    )
                )

        # write user message into memory domain first
        with tracer.phase("persistence", "user_message"):
            msg_id, _ = app.memory.append_message(
                topic_id=topic,
                role="user",
                content=message,
                content_type="text",
                model=adapter.model,
                turn_id=ctx.turn_id,
            )
        ctx.user_message_id = msg_id
        # 用户消息已经进历史：台账记下来，重启后能如实区分「连消息都没进」
        # 与「消息已保存、只是没生成回答」。
        app.turns.note_user_message(ctx.turn_id, msg_id)
        tracer.write("messages", msg_id)
        # 话题的第一段是懒创建的：这里把真正落库的片段补进本轮绑定（细化，不是改归属）。
        # 不补的话，「本轮正在写入哪个片段」在绑定里是空的 —— 封存时的写入占用检查会漏掉它。
        if not ctx.bound_fragment_id:
            row = app.conn.execute(
                "SELECT fragment_id FROM messages WHERE id = ?", (msg_id,)
            ).fetchone()
            actual_fragment = row["fragment_id"] if row is not None else None
            if actual_fragment:
                ctx.bound_fragment_id = actual_fragment
                app.bindings.record_binding(
                    ctx.turn_id,
                    topic,
                    fragment_id=actual_fragment,
                    intent_id=ctx.intent_id,
                    intent_version=ctx.bound_intent_version,
                )
        # Focus 只服务「用户选中的历史位置」：一旦本轮消息写进当前开放片段，
        # 位置推进后就不再重复强调同一个历史片段（见 advance_anchor）。
        focus_fragment = AnchorService(app.conn).focus_fragment(topic)
        focus_block = app._focus_block(topic, focus_fragment) if focus_fragment else ""
        # 本轮绑定的片段决定「路径前提」：当前片段 → 直接来源 → 祖先，
        # 同话题但不在路径上的片段只能作为「仅参考」出现（阶段 3）。
        short_term = app._short_term_items(
            topic,
            exclude_message_id=ctx.user_message_id,
            fragment_id=ctx.bound_fragment_id,
        )
        topic_note = app._topic_note(topic, prediction)
        if extra_note:
            topic_note = topic_note + extra_note
        card_svc = EntityCardService(app.conn)
        # 实体卡也要标来源与适用范围（阶段 3）：挂在别的话题上的卡、以及没有归属记录的卡，
        # 只能作为「参考」，不能被当成本轮已经接受的前提。
        entity_cards = []
        for card in card_svc.match_cards(message):
            topics = card_svc.topics_of(card)
            if not topics:
                note = "（来源未知：这张卡没有关联话题记录｜只作参考）"
            elif topic not in topics:
                names = []
                for other in topics:
                    node = app.topics.nodes.get_topic(other)
                    names.append(node.name if node is not None else other)
                note = (
                    f"（来自其他话题：{'、'.join(names)}｜只作参考，"
                    "不代表本轮已接受的结论）"
                )
            else:
                note = None
            entity_cards.append(card_svc.format_card(card, note=note))
        # 检索（记忆 + 知识 + 实体卡）单独成段：嵌入与向量检索的耗时以前完全不可见。
        with tracer.phase("retrieval"):
            payload = app.build_injection(
                message,
                topic_id=topic,
                aux_topic_ids=aux_topic_ids,
                entity_ids=app._topic_entity_ids(topic),
                user_node_id=app._user_root_id(),
                model=adapter.model,
                short_term=short_term,
                new_topic_candidate=new_topic_candidate,
                new_topic_reason=reason,
                topic_note=topic_note,
                focus_block=focus_block,
                focus_item_id=focus_fragment,
                entity_cards=entity_cards,
                # 预算必须贴近 Adapter 真正发出去的内容：system prompt（text 档含
                # 全部工具说明）与协议开销都要如实扣除，而不是恒为 0。
                system_prompt_tokens=_system_prompt_tokens(adapter, app.registry),
                adapter_overhead_tokens=_adapter_overhead_tokens(adapter, app.registry),
                tool_definitions_tokens=_tool_definitions_tokens(adapter, app.registry),
            )
        ctx.knowledge_snapshot = [
            {
                "item_id": item.item_id,
                "content": (item.text.split("] ", 1)[-1] if "] " in item.text else item.text),
            }
            for item in payload.plan.knowledge
        ]
        tracer.injection(
            items=[
                {
                    "surface": it.surface,
                    "item_id": it.item_id,
                    "tokens": it.tokens,
                    "score": round(it.score, 4),
                    "preview": _preview(it.text, 160),
                }
                for it in payload.plan.all_items
            ],
            total_tokens=payload.plan.total_tokens,
            budget={
                "hard_cap": payload.plan.hard_cap,
                "truncated": payload.plan.truncated,
                "needs_consolidation": payload.plan.needs_consolidation,
            },
            dropped=[],
        )
        prompt = message
        if payload.text:
            prompt = f"{payload.text}\n\n【用户消息】\n{message}"
        return _Plan(
            topic=topic,
            prediction=prediction,
            decision=decision,
            aux_topic_ids=aux_topic_ids,
            new_topic_candidate=new_topic_candidate,
            reason=reason,
            extra_note=extra_note,
            msg_id=msg_id,
            payload=payload,
            prompt=prompt,
        )

    async def execute_loop(self, ctx, adapter, plan: _Plan):
        import logging

        from agent.core.guard import RunawayGuard
        from agent.core.loop import AgentLoop
        from agent.credentials.usage import credential_usage_sink
        from agent.services.app import make_error

        app = self.app
        loop = AgentLoop(
            adapter, app.registry, app.bus,
            # 用量归因：这一轮的 token 记到本轮实际用的那把凭据上（上限才有可能真的生效）
            usage_sink=credential_usage_sink(app.credentials, adapter),
            tool_trace=app._record_tool_call,
            tool_selector=app._route_tools,
            max_iterations=app._loop_max_iterations() or None,
            token_budget=app._loop_token_budget() or None,
            approvals=app.approvals,
            guard=RunawayGuard(),
            turn_id=ctx.turn_id,
            trace=ctx.trace,
            # 进程级权威状态：TOOL_END 丢了，这一轮的最终结果仍然查得到
            tool_state=app.tool_state,
            # 取消检查点：本 turn 被取消后循环不再发起新的模型/工具调用
            is_cancelled=lambda: ctx.cancelled,
            # 执行叙事：模型决定说不说，AppContext 负责落库 + 广播 + 批次结束补写系统摘要
            narrative_sink=app._on_narrative,
            narrative_settler=app._settle_narrative,
        )
        ctx.loop = loop
        try:
            return await loop.run(plan.prompt)
        except Exception as exc:
            logging.getLogger(__name__).exception("turn failed")
            await app.bus.publish(
                make_error(
                    "turn_failed",
                    f"本轮执行失败：{str(exc)[:180]}",
                    recoverable=True,
                    turn_id=ctx.turn_id,
                )
            )
            app.trace_store.finish(ctx.turn_id, "failed", error=str(exc)[:200])
            ctx.result = {"ok": False, "reason": "turn_failed"}
            # 终态 + ERROR 事件：ERROR 只说明「出错了」，结束 turn 的只有 TURN_END
            ctx.status = "failed"
            ctx.error = f"{type(exc).__name__}: {str(exc)[:180]}"
            return None
        finally:
            ctx.loop = None

    async def persist(self, ctx, adapter, plan: _Plan, result) -> str:
        app = self.app
        # 阶段 1：回答写进**本轮绑定的**话题 / 片段，不再看「此刻的 Anchor」。
        # 旧实现会在这里把已经提交的用户消息搬到当前 Anchor 所在的话题，
        # 于是「回复在跑、用户改了导航」会把这一轮拆家。
        #
        # 唯一的例外是本轮**工具**自己换的话题（阶段 1 的缺口）：那种情况下
        # 整轮跟着走，判定与落实见 _apply_tool_nav_rebind。用户导航不会登记，
        # 所以「回复在跑、用户改导航」的保证一点没动。
        tool_nav_topic = take_tool_navigation(ctx.turn_id)
        if tool_nav_topic:
            self._apply_tool_nav_rebind(ctx, tool_nav_topic)
        final_topic = getattr(ctx, "bound_topic", None) or plan.topic
        if final_topic != plan.topic:
            # 只记录，不搬动：绑定的归属优先
            ctx.trace.write("bound_topic", f"{final_topic} (context={plan.topic})")
        assistant_msg_id, _ = app.memory.append_message(
            topic_id=final_topic,
            role="assistant",
            content=result.final_content or "",
            content_type="text",
            model=adapter.model,
            raw=verification_raw(result),
            turn_id=ctx.turn_id,
        )
        ctx.final_verification = getattr(result, "verification", None)
        ctx.trace.write("messages", assistant_msg_id)
        app.bindings.mark_write_closed(ctx.turn_id)
        # 本轮消息真正写入的片段 = 成功之后的「当前位置」
        row = app.conn.execute(
            "SELECT fragment_id FROM messages WHERE id = ?", (assistant_msg_id,)
        ).fetchone()
        ctx.position_fragment_id = row["fragment_id"] if row is not None else None
        return final_topic

    def _apply_tool_nav_rebind(self, ctx, topic_id: str) -> bool:
        """本轮模型自己换了话题：把这一轮整体搬到那个话题。

        两个前提缺一不可，任何一条不成立都退回阶段 1 的默认行为（留在原绑定）：

        * Anchor 仍停在 `topic_id` —— 用户在本轮之后又导航过，用户优先；
        * 本轮绑定还没收尾（`write_state='open'`）；

        目标片段由 `get_or_create_open` 懒创建（按定义是开放片段）；搬不动
        （片段已封存 / 消息不存在）就抛 `BindingMismatch` 让本轮显式失败 ——
        与「归属对不上时绝不就近写」的既有约定一致。
        """
        app = self.app
        binding = app.bindings.binding_for(ctx.turn_id)
        active = app.navigation.anchors.get_active()
        if (
            binding is None
            or binding.write_state != "open"
            or active is None
            or active.topic_id != topic_id
        ):
            ctx.trace.write("tool_nav_rebind_skipped", topic_id)
            return False
        previous_topic = binding.topic_id
        if previous_topic == topic_id:
            return False
        # 新话题的片段是懒创建的：建话题那一刻还没有片段，这里才落下第一段。
        target = app.fragments.get_or_create_open(topic_id)
        app.bindings.rebind_topic_from_tool_nav(ctx.turn_id, topic_id)
        if ctx.user_message_id:
            app.memory.move_message(ctx.user_message_id, to_fragment_id=target.id)
        # 片段靠已有的「懒创建补全」分支补进绑定，不另开一条改片段的路径。
        app.bindings.record_binding(
            ctx.turn_id,
            topic_id,
            fragment_id=target.id,
            intent_id=binding.intent_id,
            intent_version=getattr(ctx, "bound_intent_version", None),
        )
        ctx.bound_topic = topic_id
        ctx.bound_fragment_id = target.id
        ctx.trace.write("tool_nav_rebind", f"{previous_topic} -> {topic_id}/{target.id}")
        return True

    async def post_turn(self, ctx, adapter, plan: _Plan, final_topic: str) -> None:
        app = self.app
        tracer = getattr(ctx, "trace", None)
        fragment = app.fragments.get_or_create_open(final_topic)
        if app.fragments.should_close(fragment):
            # 阶段 2：这里**只封存**（事务内、不等模型）。
            # 摘要与索引是派生数据，失败可以重试，不能拖住这一轮的终态 ——
            # 也不能让「模型调用失败」把已经封存的片段回退成未封存。
            sealed = app.memory_lifecycle.seal_fragment(
                final_topic,
                reason="capacity",
                tracer=ctx.trace,
                # 容量分段是「同一阶段接着往下」：记住来源与同阶段，
                # 路径不会在容量边界断掉（阶段 4）
                continue_same_stage=True,
            )
            if sealed is not None:
                app.predictor.refresh_topic_vector(final_topic)
                # 派生工作（摘要 / 知识抽取）是后台任务：把 tracer 交下去，
                # 失败与本地修正才会落进这一轮的 trace，而不是只留在日志里。
                self._schedule_derived_work(adapter, getattr(ctx, "trace", None))
        if plan.payload.plan.needs_consolidation:
            # 收尾的压缩整理会发起**额外**的模型调用（不进 model_calls）：
            # 它同样要能在阶段账本里被看见，否则又是一段无法解释的等待。
            with tracer.phase("model_wait", "consolidate"):
                await app.consolidate(final_topic, adapter)

    def _schedule_derived_work(self, adapter, tracer=None) -> None:
        """后台把派生任务做掉。失败只记录：派生数据不影响对话与导航。

        但「只记录在日志里」是不够的（可观测性缺口）：派生任务的结构性失败与
        本地修正必须落进这一轮的 trace（tracer 走 drain_derived_tasks 既有的
        tracer 通道），耗时则作为 **turn 结束之后** 的工作单独记账 ——
        它不阻塞这一轮的终态，因此不能算进 duration_ms（见 phases.after_turn）。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _run() -> None:
            import time as _time

            _t0 = _time.perf_counter()
            try:
                await self.app.memory_lifecycle.drain_derived_tasks(
                    adapter, limit=3, tracer=tracer
                )
            except Exception as exc:  # noqa: BLE001 - 派生失败不得影响会话
                logging.getLogger(__name__).warning("derived work failed", exc_info=True)
                writer = getattr(tracer, "warning", None)
                if writer is not None:
                    try:
                        writer("derived_work_failed", f"{type(exc).__name__}: {exc}"[:200])
                    except Exception:  # noqa: BLE001 - trace 写入不得影响后台任务
                        pass
            finally:
                record = getattr(tracer, "record_after_turn", None)
                if record is not None:
                    try:
                        record(
                            "derived_work",
                            int((_time.perf_counter() - _t0) * 1000),
                            detail="summary/knowledge derivation",
                        )
                    except Exception:  # noqa: BLE001 - 记账不得影响后台任务
                        pass

        loop.create_task(_run())

    def _apply_boundary_policy(self, ctx, topic: str, fragment_id: str | None) -> None:
        """轮前边界判断（阶段 4）：只使用当前输入与此前已完成的上下文。

        * `off`：不评估；
        * `shadow`：只把建议写进 trace，不实际切分；
        * `enabled`：**确定性规则**（明确进入新的交付工作）实际生效 ——
          在当前片段上做一次原子交接（封存 → 新建同话题的下一段），
          本轮绑定到新片段。容量边界不在这里处理（它由 post_turn 在完整轮次边界执行）。

        绝不在有未结束写入的片段上交接（安全边界）。
        """
        from agent.memory.boundary import REASON_PHASE_CHANGE, FragmentBoundaryPolicy

        app = self.app
        mode = str(app.settings_store.get(BOUNDARY_MODE_KEY, DEFAULT_BOUNDARY_MODE))
        if mode == "off" or not fragment_id:
            ctx.bound_fragment_id = fragment_id
            return

        current = app.fragments.get(fragment_id)
        if current is None or current.closed_at is not None:
            ctx.bound_fragment_id = fragment_id
            return
        if app.bindings.fragment_write_busy(fragment_id):
            ctx.bound_fragment_id = fragment_id
            return

        policy = FragmentBoundaryPolicy(
            max_turns=app.fragments.max_turns, max_tokens=app.fragments.max_tokens
        )
        decision = policy.decide(
            user_input=ctx.message,
            fragment_turns=app.fragments.turn_count(fragment_id),
            fragment_tokens=app.fragments.content_tokens(fragment_id),
        )
        ctx.bound_fragment_id = fragment_id
        if ctx.trace is not None:
            ctx.trace.write(
                "boundary", f"{mode}:{decision.action}:{decision.reason}:{decision.confidence}"
            )

        if mode != "enabled" or not decision.is_split or decision.reason != REASON_PHASE_CHANGE:
            return
        sealed = app.memory_lifecycle.seal_fragment(
            topic, reason="stage_change", tracer=ctx.trace
        )
        if sealed is None:
            return
        child = app.fragments.create_child(
            topic,
            source_fragment_id=sealed.id,
            relation_type="normal",
            boundary_reason="stage_change",
            same_stage=False,
        )
        ctx.bound_fragment_id = child
        if ctx.trace is not None:
            ctx.trace.write("boundary_split", f"{sealed.id} -> {child}")

    async def advance_anchor(self, ctx, final_topic: str) -> None:
        """成功一轮：把当前位置推进到本轮真实片段（用户的历史选择就此消费）。

        失败 / 取消 / 没写出消息的轮次不推进 —— 用户「从这里开始」的选择必须
        留给下一次重试，而不是因为一次失败就被静默丢弃。

        版本比较：如果用户在本轮绑定之后又做了更新的接续选择（version 更大），
        本轮不得推进 Anchor —— 否则旧轮次收尾会覆盖用户刚做的导航。
        """
        if getattr(ctx, "cancelled", False):
            return
        fragment_id = getattr(ctx, "position_fragment_id", None)
        if not fragment_id:
            return
        app = self.app
        pending = app.bindings.peek_intent()
        bound_version = getattr(ctx, "bound_intent_version", None) or 0
        if pending is not None and pending.version > bound_version:
            ctx.trace.write("anchor_skipped", fragment_id, pending_intent=pending.intent_id)
            return
        if ctx.intent_id:
            # 首轮成功推进：接续提示可以撤下（来源关系永久保留）
            app.bindings.consume_intent(ctx.intent_id)
        anchors = app.navigation.anchors
        active = anchors.get_active()
        # 本轮执行期间发生了更新的导航（工具切/建话题）：位置归新的导航，
        # 本轮只把自己的片段推进限制在**仍然停在同一个话题**时。
        if active is not None and active.topic_id and active.topic_id != final_topic:
            ctx.trace.write("anchor_kept", f"{active.topic_id} (turn_fragment={fragment_id})")
            return
        if (
            active is not None
            and active.topic_id == final_topic
            and active.fragment_id == fragment_id
        ):
            return
        app.navigation.enter_topic(
            final_topic, fragment_id=fragment_id, relate=False
        )
        await app._publish_anchor_event()

    def finish(self, ctx, plan: _Plan, result, final_topic: str) -> None:
        app = self.app
        app.bindings.mark_status(ctx.turn_id, "completed")
        decision = plan.decision
        prediction = plan.prediction
        ctx.trace.topic(
            initial=plan.topic,
            predictor_backend=getattr(prediction, "backend_used", None),
            scores={k: round(v, 4) for k, v in dict(prediction.scores or {}).items()},
            suspected_new=plan.new_topic_candidate,
            operation=(
                "switch" if final_topic != plan.topic else getattr(decision.mode, "value", "none")
            ),
            final=final_topic,
        )
        app.trace_store.finish(
            ctx.turn_id,
            "done",
            final_topic=final_topic,
            final_preview=result.final_content or "",
        )
        ctx.final_content = result.final_content
        ctx.result = {"ok": True, "turn": result.__dict__}


def _tool_spec_tokens(registry) -> int:
    """估算 tool definitions 占用的 token（供预算扣除）。"""
    import json

    from agent.memory.index import estimate_tokens

    try:
        specs = registry.specs()
        blob = json.dumps(
            [
                {"name": s.name, "description": s.description, "parameters": s.parameters}
                for s in specs
            ],
            ensure_ascii=False,
        )
        return estimate_tokens(blob)
    except Exception:  # noqa: BLE001 - budget estimate must never break a turn
        return 0


def adapter_prompt_costs(adapter, registry) -> tuple[int, int, int]:
    """(system_prompt_tokens, adapter_overhead_tokens, tool_definitions_tokens)。

    供应商差异交给 Adapter：由它回答「我会额外拼多少 prompt、协议本身有多少固定
    开销、工具定义是走 API 字段还是已经拼进 prompt」，业务层只负责扣减。
    """
    return (
        _system_prompt_tokens(adapter, registry),
        _adapter_overhead_tokens(adapter, registry),
        _tool_definitions_tokens(adapter, registry),
    )


def _specs_or_empty(registry) -> list:
    try:
        return list(registry.specs())
    except Exception:  # noqa: BLE001 - 预算估算不得影响本轮
        return []


def _system_prompt_tokens(adapter, registry) -> int:
    from agent.memory.index import estimate_tokens

    specs = _specs_or_empty(registry)
    if not specs:
        return 0
    try:
        text = adapter.system_prompt_text(specs)
    except Exception:  # noqa: BLE001
        return 0
    return estimate_tokens(text) if text else 0


def _adapter_overhead_tokens(adapter, registry) -> int:
    specs = _specs_or_empty(registry)
    try:
        return max(0, int(adapter.protocol_overhead_tokens(specs)))
    except Exception:  # noqa: BLE001
        return 0


def _tool_definitions_tokens(adapter, registry) -> int:
    specs = _specs_or_empty(registry)
    if not specs:
        return 0
    # 工具说明已经拼进 system prompt 的档位不再按 API tools 计一遍
    if getattr(adapter, "tools_in_prompt", False):
        return 0
    return _tool_spec_tokens(registry)
