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
        """
        app = self.app
        if ctx.notify:
            await app._execute_notify_turn(ctx)
            return
        # 本轮产生的审批绑定到本 turn（工具不需要各自传参）
        app.approvals.set_context(turn_id=ctx.turn_id)
        try:
            await self._execute_turn(ctx)
        finally:
            app.approvals.set_context(turn_id=None)

    async def _execute_turn(self, ctx) -> None:
        app = self.app
        if ctx.cancelled:
            return
        adapter = await self.begin(ctx)
        if adapter is None:
            return
        if ctx.cancelled:
            return
        plan = await self.build_context(ctx, adapter)
        if ctx.cancelled:
            return
        result = await self.execute_loop(ctx, adapter, plan)
        if result is None:
            app.bindings.mark_status(ctx.turn_id, ctx.status or "failed")
            return
        # 取消检查点：不得把取消后产生的内容保存成正常最终回答
        if ctx.cancelled or result.cancelled:
            ctx.cancelled = True
            app.bindings.mark_status(ctx.turn_id, "cancelled")
            app.trace_store.finish(ctx.turn_id, "cancelled")
            return
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
        await self.post_turn(ctx, adapter, plan, final_topic)
        if ctx.cancelled:
            app.bindings.mark_status(ctx.turn_id, "cancelled")
            app.trace_store.finish(
                ctx.turn_id, "cancelled", final_topic=final_topic, final_preview=ctx.final_content or ""
            )
            return
        # 高影响知识候选：只有在回答真的完成之后才进协议（前端也只在 TURN_END
        # 之后才显示），绝不打断正在进行的回答（spec 第 15~16 条）。
        await app.emit_knowledge_candidates(ctx)
        await self.advance_anchor(ctx, final_topic)
        self.finish(ctx, plan, result, final_topic)

    # -- stages -----------------------------------------------------------

    async def begin(self, ctx):
        from agent.memory.fragment import resolve_max_turns
        from agent.services.app import make_warning
        from agent.trace.recorder import TurnTracer

        app = self.app
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
            return None
        # 能力模式与降级：正常时不显示，降级时用户得到一次性低干扰提示
        await app.announce_capability(adapter, ctx.turn_id)
        # 封块阈值是「轮」不是「消息条数」（第三阶段 spec 第 57~60 条）
        app.fragments.max_turns = resolve_max_turns(app.settings_store)
        topic = ctx.initial_topic or app.current_topic()

        # ── 轮前：固定本轮归属（Topic / Fragment）──────────────────────────
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

        tracer = TurnTracer(app.trace_store, ctx.turn_id)
        ctx.trace = tracer
        app.trace_store.begin(ctx.turn_id, initial_topic=topic)
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
        msg_id, _ = app.memory.append_message(
            topic_id=topic,
            role="user",
            content=message,
            content_type="text",
            model=adapter.model,
            turn_id=ctx.turn_id,
        )
        ctx.user_message_id = msg_id
        tracer.write("messages", msg_id)
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
        entity_cards = [card_svc.format_card(c) for c in card_svc.match_cards(message)]
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
        from agent.services.app import make_error

        app = self.app
        loop = AgentLoop(
            adapter, app.registry, app.bus,
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
            turn_id=ctx.turn_id,
        )
        ctx.trace.write("messages", assistant_msg_id)
        app.bindings.mark_write_closed(ctx.turn_id)
        # 本轮消息真正写入的片段 = 成功之后的「当前位置」
        row = app.conn.execute(
            "SELECT fragment_id FROM messages WHERE id = ?", (assistant_msg_id,)
        ).fetchone()
        ctx.position_fragment_id = row["fragment_id"] if row is not None else None
        return final_topic

    async def post_turn(self, ctx, adapter, plan: _Plan, final_topic: str) -> None:
        app = self.app
        fragment = app.fragments.get_or_create_open(final_topic)
        if app.fragments.should_close(fragment):
            # 阶段 2：这里**只封存**（事务内、不等模型）。
            # 摘要与索引是派生数据，失败可以重试，不能拖住这一轮的终态 ——
            # 也不能让「模型调用失败」把已经封存的片段回退成未封存。
            sealed = app.memory_lifecycle.seal_fragment(
                final_topic, reason="capacity", tracer=ctx.trace
            )
            if sealed is not None:
                app.predictor.refresh_topic_vector(final_topic)
                self._schedule_derived_work(adapter)
        if plan.payload.plan.needs_consolidation:
            await app.consolidate(final_topic, adapter)

    def _schedule_derived_work(self, adapter) -> None:
        """后台把派生任务做掉。失败只记录：派生数据不影响对话与导航。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _run() -> None:
            try:
                await self.app.memory_lifecycle.drain_derived_tasks(adapter, limit=3)
            except Exception:  # noqa: BLE001 - 派生失败不得影响会话
                logging.getLogger(__name__).warning("derived work failed", exc_info=True)

        loop.create_task(_run())

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
