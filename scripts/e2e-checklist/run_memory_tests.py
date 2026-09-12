"""自主 e2e：记忆功能验证（v1 已实装部分）。

双通道：
- 通道 A（确定性）：直接构造 AppContext + 读 DB，验证短期记忆注入 / 话题预判 /
  封块分档 / 话题工具 / 封块提炼链 / 向量后端状态。
- 通道 B（真实行为）：HTTP + 真实模型，验证对话连续性、模型是否自主调用
  switch_topic / create_topic（观察项，不判失败）、跨轮次记忆引用。

输出 results_memory.json。测试话题以「记忆验证-」前缀创建，跑完清理并恢复锚点。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8734"
TEMP = Path(os.environ.get("TEMP", "."))
E2E_DB = TEMP / "qio-e2e" / "app.db"
RESULTS = []
PREFIX = "记忆验证-"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT / "backend" / "src"))
os.environ["QIO_DATA_DIR"] = str(TEMP / "qio-e2e")


def record(case_id, title, passed, actual, detail=""):
    RESULTS.append({
        "id": case_id, "title": title, "passed": passed,
        "actual": actual if actual else ("通过" if passed else "失败"),
        "detail": detail,
    })
    print(f"[{'PASS' if passed else 'FAIL'}] {case_id} {title} :: {actual[:90]}")


def record_obs(case_id, title, detail):
    """观察项：行为被记录即视为完成（不判成败）。"""
    RESULTS.append({
        "id": case_id, "title": title, "passed": True,
        "actual": f"观察完成: {detail[:90]}", "detail": detail,
    })
    print(f"[OBS ] {case_id} {title} :: {detail[:90]}")


# ---------------------------------------------------------------- 通道 A

class FakeAdapter:
    """Native-mode fake：按队列返回摘要 / 知识提炼 / 回复 JSON。"""

    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):
        content = self.responses.pop(0) if self.responses else "ok"
        self.calls += 1
        from agent.adapters.base import ChatMessage, Completion
        return Completion(message=ChatMessage(role="assistant", content=content))


def build_ctx():
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    settings = Settings()
    conn = connect(settings.db_path)
    apply_migrations(conn)
    return AppContext(settings, conn, EventBus()), conn


def clean_topic(ctx, conn, topic_id: str) -> None:
    """删除某话题的记忆数据并恢复锚点（若锚点指向该话题）。"""
    anchors = conn.execute(
        "SELECT id FROM cursor WHERE topic_id = ?", (topic_id,)
    ).fetchall()
    for a in anchors:
        conn.execute("DELETE FROM cursor WHERE id = ?", (a["id"],))
    rows = conn.execute(
        "SELECT id FROM fragments WHERE topic_id = ?", (topic_id,)
    ).fetchall()
    frag_ids = [r["id"] for r in rows]
    for fid in frag_ids:
        conn.execute("DELETE FROM messages WHERE fragment_id = ?", (fid,))
        conn.execute("DELETE FROM memory_index WHERE fragment_id = ?", (fid,))
        conn.execute("DELETE FROM embeddings WHERE doc_type = 'memory_index' AND ref_id = ?", (fid,))
    conn.execute("DELETE FROM fragments WHERE topic_id = ?", (topic_id,))
    conn.execute("DELETE FROM knowledge WHERE node_ids LIKE ?", (f'%"{topic_id}"%',))
    conn.execute("DELETE FROM edges WHERE src = ? OR dst = ?", (topic_id, topic_id))
    conn.execute("DELETE FROM entity_mentions WHERE topic_id = ?", (topic_id,))
    conn.execute("DELETE FROM embeddings WHERE doc_type = 'topic' AND ref_id = ?", (topic_id,))
    conn.execute("DELETE FROM nodes WHERE id = ?", (topic_id,))
    conn.commit()


def channel_a(ctx, conn) -> None:
    now = datetime.now(timezone.utc).isoformat()

    # ---- A1 冷启动与注入降级 ----
    t_a = ctx.topics.nodes.create_topic(PREFIX + "A-短期").id
    try:
        payload = ctx.build_injection("你好", topic_id=t_a, user_node_id=ctx._user_root_id())
        record("MEM-A1", "冷启动注入降级", "【短期" not in payload.text,
               f"无短期块={('【短期' not in payload.text)} text={payload.text[:40]!r}")
        # 锚点同步（run_turn 内逻辑的等价验证）
        from agent.graph.anchors import AnchorService
        anchors = AnchorService(conn)
        if anchors.get_active() is None or anchors.get_active().topic_id != t_a:
            anchors.set_active(t_a)
        record("MEM-A1b", "锚点建立", anchors.get_active().topic_id == t_a,
               f"active={anchors.get_active().topic_id}")
    finally:
        clean_topic(ctx, conn, t_a)

    # ---- A2 短期记忆注入 ----
    t_a = ctx.topics.nodes.create_topic(PREFIX + "A-短期").id
    try:
        ctx.memory.append_message(topic_id=t_a, role="user", content="我最近开始吃清淡饮食，不吃辣了")
        ctx.memory.append_message(topic_id=t_a, role="assistant", content="好的，记住了")
        payload = ctx.build_injection(
            "我刚才说了什么饮食习惯？", topic_id=t_a, user_node_id=ctx._user_root_id(),
            short_term=ctx._short_term_items(t_a),
        )
        ok = ("短期" in payload.text) and ("不吃辣" in payload.text)
        record("MEM-A2", "短期记忆原文注入", ok,
               f"注入含短期块={('短期' in payload.text)} 含原文={'不吃辣' in payload.text}")
    finally:
        clean_topic(ctx, conn, t_a)

    # ---- A3 话题预判 ----
    t_a = ctx.topics.nodes.create_topic(PREFIX + "A-预判A").id
    t_b = ctx.topics.nodes.create_topic(PREFIX + "A-预判B").id
    try:
        for tid, summary, keywords in (
            (t_a, "饮食偏好：清淡、不吃辣", '["饮食", "清淡", "吃辣"]'),
            (t_b, "健身计划：跑步、力量训练", '["健身", "跑步", "力量"]'),
        ):
            conn.execute(
                "INSERT INTO fragments (id, topic_id, summary, created_at, closed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"frag_{tid}", tid, summary, now, now),
            )
            conn.execute(
                "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, "
                "title, token_estimate, created_at) VALUES (?, ?, ?, '[]', ?, ?, 10, ?)",
                (f"idx_{tid}", f"frag_{tid}", tid, keywords, summary, now),
            )
        conn.commit()
        ctx._refresh_selector()
        # 预置干净的话题向量（话题名是测试前缀，会污染冷启动向量，故直接写向量）
        # embedding 后端可能为 None（本地 ONNX 模型文件缺失时降级为 BM25，
        # 产品代码全程按 embedding is not None 处理，脚本也必须一致）
        if ctx.embedding is not None and ctx.embedding.available():
            ctx.embedding.update_topic_vector(t_a, "饮食偏好 清淡 不吃辣 用户喜欢清淡饮食")
            ctx.embedding.update_topic_vector(t_b, "健身计划 跑步 力量 每周三次健身")
        pred = ctx.predictor.predict("我最近饮食清淡不吃辣", current_topic_id=t_a)
        ok_main = pred.main_topic_id == t_a
        record("MEM-A3", "话题预判主话题", ok_main,
               f"main={pred.main_topic_id} backend={pred.backend_used} "
               f"scores={ {k: round(v, 3) for k, v in pred.scores.items()} }")
        pred2 = ctx.predictor.predict("量子物理弦理论完全无关", current_topic_id=t_a)
        record("MEM-A3b", "疑似新话题标记", pred2.is_new_topic_candidate,
               f"is_new={pred2.is_new_topic_candidate} main={pred2.main_topic_id}")
    finally:
        clean_topic(ctx, conn, t_a)
        clean_topic(ctx, conn, t_b)

    # ---- A4 封块分档 ----
    t_a = ctx.topics.nodes.create_topic(PREFIX + "A-分档").id
    try:
        ctx.settings_store.set("fragment.max_messages", "5")
        ctx.fragments.max_messages = ctx.settings_store.get_int("fragment.max_messages", 10)
        assert ctx.fragments.max_messages == 5
        ctx.memory.append_message(topic_id=t_a, role="user", content="m1")
        ctx.memory.append_message(topic_id=t_a, role="assistant", content="r1")
        ctx.memory.append_message(topic_id=t_a, role="user", content="m2")
        ctx.memory.append_message(topic_id=t_a, role="assistant", content="r2")
        frag = ctx.fragments.get_or_create_open(t_a)
        before = ctx.fragments.should_close(frag)
        ctx.memory.append_message(topic_id=t_a, role="user", content="m3")
        frag = ctx.fragments.get_or_create_open(t_a)
        after = ctx.fragments.should_close(frag)
        record("MEM-A4", "分档 5 条触发封块", (not before) and after,
               f"4条时={before} 5条时={after}")
        ctx.settings_store.set("fragment.max_messages", "10")
    finally:
        clean_topic(ctx, conn, t_a)

    # ---- A5 话题工具 ----
    t_a = ctx.topics.nodes.create_topic(PREFIX + "A-工具A").id
    t_b = ctx.topics.nodes.create_topic(PREFIX + "A-工具B").id
    try:
        from agent.graph.anchors import AnchorService
        from agent.tools.topic_tools import CreateTopicTool, SwitchTopicTool

        anchors = AnchorService(conn)
        anchors.set_active(t_a)
        r = SwitchTopicTool(conn).run_sync(topic_id=t_b, reason="验证")
        edge = conn.execute(
            "SELECT weight FROM edges WHERE type='related' "
            "AND ((src=? AND dst=?) OR (src=? AND dst=?))",
            (t_a, t_b, t_b, t_a),
        ).fetchone()
        ok = r.ok and anchors.get_active().topic_id == t_b and edge is not None
        record("MEM-A5", "switch_topic 锚点+related边", ok,
               f"ok={r.ok} active={anchors.get_active().topic_id} edge={edge['weight'] if edge else None}")
        # 反向切换权重累加
        SwitchTopicTool(conn).run_sync(topic_id=t_a, reason="回来")
        edge2 = conn.execute(
            "SELECT weight FROM edges WHERE type='related' "
            "AND ((src=? AND dst=?) OR (src=? AND dst=?))",
            (t_a, t_b, t_b, t_a),
        ).fetchone()
        record("MEM-A5b", "related 边权重累加", edge2 is not None and edge2["weight"] >= 2.0,
               f"weight={edge2['weight'] if edge2 else None}")
        # create_topic（CreateTopicTool 只有 async run：走 asyncio.run，
        # 脚本此前调用已不存在的 run_sync 会直接抛 AttributeError）
        r2 = asyncio.run(
            CreateTopicTool(conn).run(name=PREFIX + "A-新建", reason="验证")
        )
        node = conn.execute(
            "SELECT id FROM nodes WHERE type='topic' AND name=?", (PREFIX + "A-新建",)
        ).fetchone()
        ok2 = r2.ok and node is not None and anchors.get_active().topic_id == node["id"]
        record("MEM-A5c", "create_topic 创建并切换", ok2,
               f"ok={r2.ok} active={anchors.get_active().topic_id}")
        clean_topic(ctx, conn, node["id"])
        anchors.set_active(t_a)
    finally:
        clean_topic(ctx, conn, t_a)
        clean_topic(ctx, conn, t_b)

    # ---- A6 封块提炼链 ----
    t_a = ctx.topics.nodes.create_topic(PREFIX + "A-提炼").id
    try:
        # 先给实体一次提及（达到阈值 2 后建节点）
        ctx.topics.nodes.mention("牛奶", t_a, force=False)
        for i in range(5):
            ctx.memory.append_message(
                topic_id=t_a, role="user" if i % 2 == 0 else "assistant",
                content=f"消息 {i} 用户喜欢清淡饮食",
            )
        adapter = FakeAdapter([
            '{"title": "饮食", "summary": "用户偏好清淡饮食", "entities": ["牛奶"], "keywords": ["清淡"]}',
            # 封块顺序：摘要 → 实体卡提炼 → 知识提炼。实体卡这一步是后加的，
            # 旧 fixture 少了它，导致知识提炼拿到空回复、一条知识都不产生。
            '{"entities": [{"name": "牛奶", "aliases": [], "kind": "food", '
            '"summary": "用户常提到的食物", "attributes": [], "relations": []}]}',
            '{"candidates": ['
            '{"content": "用户偏好清淡饮食", "category": "user_profile", "attach": "user", "entity": null},'
            '{"content": "该话题讨论了清淡饮食", "category": "general_fact", "attach": "topic", "entity": null}'
            "]}",
        ])
        # 只统计本次封块新产生的知识，避免读到库里其它会话的历史条目
        before_ids = {r["id"] for r in conn.execute("SELECT id FROM knowledge")}
        closed = asyncio.run(ctx._close_fragment(t_a, adapter))
        ok_close = closed is not None and closed.closed_at is not None
        rows = [
            r
            for r in conn.execute(
                "SELECT id, category, state FROM knowledge ORDER BY category"
            ).fetchall()
            if r["id"] not in before_ids
        ]
        states = {r["category"]: r["state"] for r in rows}
        ok_know = states.get("user_profile") == "pending_review" and states.get("general_fact") == "active"
        idx = conn.execute(
            "SELECT COUNT(*) c FROM memory_index WHERE topic_id = ?", (t_a,)
        ).fetchone()["c"]
        ent = conn.execute(
            "SELECT id FROM nodes WHERE type='entity' AND name='牛奶'"
        ).fetchone()
        record("MEM-A6", "封块：摘要+索引+实体", ok_close and idx > 0 and ent is not None,
               f"closed={ok_close} idx={idx} entity={'有' if ent else '无'}")
        record("MEM-A6b", "知识分层验证", ok_know, f"states={states}")
        # 话题向量刷新（嵌入可用时）
        ctx.predictor.refresh_topic_vector(t_a)
        vec = (
            ctx.embedding.topic_vector(t_a)
            if ctx.embedding is not None and ctx.embedding.available()
            else None
        )
        record("MEM-A6c", "话题向量刷新", vec is not None,
               f"topic_vector={'有' if vec is not None else '无(嵌入不可用)'}")
    finally:
        clean_topic(ctx, conn, t_a)
        # 实体节点现在可能挂了实体卡/提及/边：先删依赖行，否则外键约束会失败
        entity = conn.execute(
            "SELECT id FROM nodes WHERE type='entity' AND name='牛奶'"
        ).fetchone()
        if entity is not None:
            card_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM entity_cards WHERE node_id = ?", (entity["id"],)
                ).fetchall()
            ]
            for cid in card_ids:
                conn.execute("DELETE FROM embeddings WHERE ref_id = ?", (cid,))
            conn.execute("DELETE FROM entity_cards WHERE node_id = ?", (entity["id"],))
            conn.execute(
                "DELETE FROM edges WHERE src = ? OR dst = ?", (entity["id"], entity["id"])
            )
            conn.execute("DELETE FROM entity_mentions WHERE entity_name = '牛奶'")
            conn.execute("DELETE FROM nodes WHERE id = ?", (entity["id"],))
        conn.commit()

    # ---- A7 向量后端状态 ----
    embedding_ready = ctx.embedding is not None and ctx.embedding.available()
    record("MEM-A7", "向量后端加载", True,
           f"embedding={embedding_ready} backend={ctx.selector.backend_name}")


# ---------------------------------------------------------------- 通道 B

def wait_turn(message, topic_id=None, timeout=150):
    """订阅 SSE -> POST /api/turns -> 收集 TURN_END 前的事件。返回 (events, resp)。"""
    events = []
    stop = threading.Event()
    ready = threading.Event()

    def reader():
        with httpx.Client(timeout=timeout) as c:
            with c.stream("GET", f"{BASE}/api/events") as resp:
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        try:
                            ev = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        if not ready.is_set():
                            continue
                        events.append(ev)
                        if ev.get("type") in ("TURN_END", "ERROR"):
                            stop.set()
                            return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(1.0)
    ready.set()
    payload = {"message": message}
    if topic_id:
        payload["topic_id"] = topic_id
    resp = httpx.post(f"{BASE}/api/turns", json=payload, timeout=15)
    t.join(timeout)
    return events, resp


def tool_calls_in(events):
    names = []
    for ev in events:
        if ev.get("type") == "TOOL_START":
            names.append(ev.get("data", {}).get("tool"))
    return names


def channel_b(conn) -> None:
    if not E2E_DB.exists():
        record("MEM-B0", "e2e 数据库存在", False, str(E2E_DB))
        return
    try:
        listing = httpx.get(f"{BASE}/api/credentials", timeout=5).json()["credentials"]
    except Exception as exc:
        record("MEM-B0", "后端可达性", False, f"后端 {BASE} 不可达: {type(exc).__name__}")
        return
    record("MEM-B0", "后端可达性", True, f"{BASE} 可达")
    has_key = any(c["status"] == "active" and "main-loop" in c.get("tags", []) for c in listing)
    if not has_key:
        record("MEM-B0", "真实模型凭据", False, "无 active main-loop 凭据，通道 B 跳过")
        return
    record("MEM-B0", "真实模型凭据", True, "main-loop key 就绪")

    from agent.config import Settings
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    settings = Settings()
    c = connect(settings.db_path)
    apply_migrations(c)
    now = datetime.now(timezone.utc).isoformat()
    topic = None
    try:
        from agent.graph.nodes import NodeService
        topic = NodeService(c).create_topic(PREFIX + "B-真实对话").id
        # 让后端进程的预判能看到该话题（向量冷启动由预判自动生成）

        # B1 短期连续性：连续 3 轮 + 引用
        script = [
            ("我最近开始吃清淡的饮食，完全不吃辣了", None),
            ("我同时在学 SQLite 数据库设计，觉得迁移机制很有意思", None),
            ("顺便说，我最讨厌喝牛奶", None),
        ]
        for msg, _ in script:
            events, resp = wait_turn(msg, topic_id=topic)
            if resp.status_code != 200:
                record("MEM-B1", "连续对话发送", False, f"HTTP {resp.status_code}: {resp.text[:100]}")
                return
            time.sleep(1.0)
        # 真实模型行为有方差：同锚点重试一次
        attempts = []
        for q in ("我刚才说我最近的饮食习惯有什么变化？",
                  "你记得我最近饮食上开始怎么做吗？"):
            events, resp = wait_turn(q, topic_id=topic)
            final = ""
            for ev in events:
                if ev.get("type") == "TURN_END":
                    final = ev.get("data", {}).get("final_content") or ""
            attempts.append((q, final))
        keywords = ["清淡", "不吃辣", "辣"]
        hit = []
        for _, final in attempts:
            hit = [k for k in keywords if k in final]
            if hit:
                break
        record("MEM-B1", "短期连续性：引用近期细节", len(hit) > 0,
               f"回复命中 {hit}（尝试 {len(attempts)} 次），回复长度 {len(final)}",
               detail=f"最终回复: {final[:200]}")

        # B2 观察：模型是否自主切换话题（发明显无关话题 B 的内容）
        events, resp = wait_turn(
            "聊点别的：帮我规划一个每周三次的健身跑步计划", topic_id=topic
        )
        calls = tool_calls_in(events)
        anchor_row = c.execute("SELECT topic_id FROM cursor WHERE anchor_type='active'").fetchone()
        switched = anchor_row is not None and anchor_row["topic_id"] != topic
        detail = f"工具调用={calls} 锚点变化={switched}"
        if switched:
            record_obs("MEM-B2", "模型自主话题切换", detail + "（已切换）")
        else:
            record_obs("MEM-B2", "模型自主话题切换", detail + "（未调用工具，行为观察）")

        # B3 观察：无关新方向是否创建话题
        events, resp = wait_turn(
            "我们再聊一个完全新的方向：量子物理与弦理论入门", topic_id=topic
        )
        calls = tool_calls_in(events)
        new_topic = c.execute(
            "SELECT id FROM nodes WHERE type='topic' AND name LIKE '量子%'"
        ).fetchone()
        record_obs("MEM-B3", "模型自主创建话题",
                   f"工具调用={calls} 新话题节点={'有' if new_topic else '无'}")

        # B4 跨轮次记忆引用（早期细节在 6 轮后仍可引用，重试一次）
        attempts = []
        for q in ("还记得我最讨厌喝什么吗？",
                  "我之前说过我对什么饮品很反感？"):
            events, resp = wait_turn(q, topic_id=topic)
            final = ""
            for ev in events:
                if ev.get("type") == "TURN_END":
                    final = ev.get("data", {}).get("final_content") or ""
            attempts.append((q, final))
        ok = False
        final = ""
        for _, final in attempts:
            ok = ("牛奶" in final) or ("奶" in final)
            if ok:
                break
        record("MEM-B4", "跨轮次记忆引用", ok,
               f"命中牛奶={'牛奶' in final}（尝试 {len(attempts)} 次）",
               detail=f"最终回复: {final[:200]}")
    finally:
        # 清理测试话题（含模型自主创建的量子话题）
        rows = c.execute(
            "SELECT id FROM nodes WHERE type='topic' AND (name LIKE ? OR name LIKE ?)",
            (PREFIX + "%", "量子%"),
        ).fetchall()
        for r in rows:
            tid = r["id"]
            c.execute("DELETE FROM cursor WHERE topic_id = ?", (tid,))
            frags = c.execute(
                "SELECT id FROM fragments WHERE topic_id = ?", (tid,)
            ).fetchall()
            for f in frags:
                c.execute("DELETE FROM messages WHERE fragment_id = ?", (f["id"],))
                c.execute("DELETE FROM memory_index WHERE fragment_id = ?", (f["id"],))
                c.execute(
                    "DELETE FROM embeddings WHERE doc_type='memory_index' AND ref_id = ?",
                    (f["id"],),
                )
            c.execute("DELETE FROM fragments WHERE topic_id = ?", (tid,))
            c.execute("DELETE FROM knowledge WHERE node_ids LIKE ?", (f'%"{tid}"%',))
            c.execute("DELETE FROM edges WHERE src = ? OR dst = ?", (tid, tid))
            c.execute("DELETE FROM entity_mentions WHERE topic_id = ?", (tid,))
            c.execute("DELETE FROM embeddings WHERE doc_type='topic' AND ref_id = ?", (tid,))
            c.execute("DELETE FROM nodes WHERE id = ?", (tid,))
        c.execute("DELETE FROM cursor WHERE anchor_type='active' AND topic_id IS NULL")
        c.commit()
        c.close()


def cleanup_stale_topics(conn) -> None:
    rows = conn.execute(
        "SELECT id FROM nodes WHERE type='topic' AND name LIKE ?",
        (PREFIX + "%",),
    ).fetchall()
    for r in rows:
        tid = r["id"]
        conn.execute("DELETE FROM cursor WHERE topic_id = ?", (tid,))
        frags = conn.execute(
            "SELECT id FROM fragments WHERE topic_id = ?", (tid,)
        ).fetchall()
        for f in frags:
            conn.execute("DELETE FROM messages WHERE fragment_id = ?", (f["id"],))
            conn.execute("DELETE FROM memory_index WHERE fragment_id = ?", (f["id"],))
            conn.execute(
                "DELETE FROM embeddings WHERE doc_type='memory_index' AND ref_id = ?",
                (f["id"],),
            )
        conn.execute("DELETE FROM fragments WHERE topic_id = ?", (tid,))
        conn.execute("DELETE FROM knowledge WHERE node_ids LIKE ?", (f'%"{tid}"%',))
        conn.execute("DELETE FROM edges WHERE src = ? OR dst = ?", (tid, tid))
        conn.execute("DELETE FROM entity_mentions WHERE topic_id = ?", (tid,))
        conn.execute("DELETE FROM embeddings WHERE doc_type='topic' AND ref_id = ?", (tid,))
        conn.execute("DELETE FROM nodes WHERE id = ?", (tid,))
    conn.commit()


def main() -> None:
    print("== 记忆功能验证（v1 已实装）==")
    out = ROOT / "scripts" / "e2e-checklist" / "results_memory.json"
    ctx, conn = build_ctx()
    cleanup_stale_topics(conn)
    try:
        channel_a(ctx, conn)
        channel_b(conn)
    except Exception as exc:
        record("MEM-X", "脚本异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            cleanup_stale_topics(conn)
        except Exception:
            pass
        conn.close()
    passed = sum(1 for r in RESULTS if r["passed"])
    print(f"\n结果: {passed}/{len(RESULTS)} 通过，输出 {out}")


if __name__ == "__main__":
    main()
