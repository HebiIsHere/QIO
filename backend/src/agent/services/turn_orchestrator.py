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

from dataclasses import dataclass, field
from typing import Any


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
        app = self.app
        if ctx.notify:
            await app._execute_notify_turn(ctx)
            return
        adapter = await self.begin(ctx)
        if adapter is None:
            return
        plan = await self.build_context(ctx, adapter)
        result = await self.execute_loop(ctx, adapter, plan)
        if result is None:
            return
        final_topic = await self.persist(ctx, adapter, plan, result)
        await self.post_turn(ctx, adapter, plan, final_topic)
        self.finish(ctx, plan, result, final_topic)

    # -- stages -----------------------------------------------------------

    async def begin(self, ctx):
        from agent.graph.anchors import AnchorService
        from agent.services.app import DEFAULT_FRAGMENT_MAX_MESSAGES, make_warning
        from agent.trace.recorder import TurnTracer

        app = self.app
        adapter = await app.build_adapter()
        if adapter is None:
            await app.bus.publish(
                make_warning("no main-loop credential configured; add one in Settings")
            )
            ctx.result = {"ok": False, "reason": "no_credential"}
            ctx.status = "done"
            return None
        app.fragments.max_messages = app.settings_store.get_int(
            "fragment.max_messages", DEFAULT_FRAGMENT_MAX_MESSAGES
        )
        topic = ctx.initial_topic or app.current_topic()
        ctx.current_topic = topic
        tracer = TurnTracer(app.trace_store, ctx.turn_id)
        ctx.trace = tracer
        app.trace_store.begin(ctx.turn_id, initial_topic=topic)
        # speaking in a topic anchors it (if the anchor is absent or stale)
        active_anchor = AnchorService(app.conn).get_active()
        if active_anchor is None or active_anchor.topic_id != topic:
            AnchorService(app.conn).set_active(topic)
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

        # write user message into memory domain first
        msg_id, _ = app.memory.append_message(
            topic_id=topic,
            role="user",
            content=message,
            content_type="text",
            model=adapter.model,
        )
        ctx.user_message_id = msg_id
        tracer.write("messages", msg_id)
        active_anchor = AnchorService(app.conn).get_active()
        focus_block = ""
        if active_anchor is not None and active_anchor.fragment_id:
            focus_block = app._focus_block(topic, active_anchor.fragment_id)
        short_term = app._short_term_items(topic, exclude_message_id=ctx.user_message_id)
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
            entity_cards=entity_cards,
            tool_definitions_tokens=_tool_spec_tokens(app.registry),
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
        )
        ctx.loop = loop
        try:
            return await loop.run(plan.prompt)
        except Exception as exc:
            logging.getLogger(__name__).exception("turn failed")
            await app.bus.publish(
                make_error("turn_failed", str(exc)[:200], recoverable=True)
            )
            app.trace_store.finish(ctx.turn_id, "failed", error=str(exc)[:200])
            ctx.result = {"ok": False, "reason": "turn_failed"}
            ctx.status = "done"
            return None
        finally:
            ctx.loop = None

    async def persist(self, ctx, adapter, plan: _Plan, result) -> str:
        from agent.graph.anchors import AnchorService

        app = self.app
        final_topic = plan.topic
        active = AnchorService(app.conn).get_active()
        if active is not None and active.topic_id and active.topic_id != plan.topic:
            final_topic = active.topic_id
            app._move_message(plan.msg_id, plan.topic, final_topic)
        assistant_msg_id, _ = app.memory.append_message(
            topic_id=final_topic,
            role="assistant",
            content=result.final_content or "",
            content_type="text",
            model=adapter.model,
        )
        ctx.trace.write("messages", assistant_msg_id)
        return final_topic

    async def post_turn(self, ctx, adapter, plan: _Plan, final_topic: str) -> None:
        app = self.app
        fragment = app.fragments.get_or_create_open(final_topic)
        if app.fragments.should_close(fragment):
            closed = await app._close_fragment(final_topic, adapter, tracer=ctx.trace)
            if closed is not None:
                app._refresh_selector()
                app.predictor.refresh_topic_vector(final_topic)
        if plan.payload.plan.needs_consolidation:
            await app.consolidate(final_topic, adapter)

    def finish(self, ctx, plan: _Plan, result, final_topic: str) -> None:
        app = self.app
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
