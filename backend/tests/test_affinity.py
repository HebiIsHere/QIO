# -*- coding: utf-8 -*-
"""话题归属分类：延续 / 切换 / 新建 + 实体软信号。"""
from types import SimpleNamespace

from agent.services.affinity import (
    NEW_TOPIC_STRICT,
    SWITCH_THRESHOLD,
    TopicMode,
    classify,
)


def _pred(main=None, scores=None, new=False, backend="rules"):
    # backend_used 决定用哪套门槛（onnx 余弦 vs 关键词重合比例，量纲不同）
    return SimpleNamespace(
        main_topic_id=main, scores=scores or {}, is_new_topic_candidate=new, backend_used=backend
    )


def test_in_topic_when_main_is_current():
    d = classify("继续聊 sqlite", _pred(main="t_sql", scores={"t_sql": 0.8}), "t_sql")
    assert d.mode == TopicMode.IN_TOPIC


def test_switch_when_other_topic_high_score():
    d = classify("聊一下健身", _pred(main="t_fit", scores={"t_fit": 0.6, "t_sql": 0.4}), "t_sql")
    assert d.mode == TopicMode.SWITCH
    assert d.switch_to == "t_fit"


def test_new_when_main_score_below_switch_threshold():
    # 消息长度必须超过短输入护栏（min_new_topic_chars），否则它会被判成「延续」——
    # 这里要测的是 switch 门槛那一支，所以用一句内容完整的消息。
    # 0.01 是**兜底层量纲**下的弱归属：兜底分数是关键词重合比例（实测 0~0.22），
    # 门槛随之标定为 rules_switch_threshold=0.02（见 params.TopicPolicy 与
    # backend/evals/EXPERIMENTS-P3-E.md）；0.5 在这个量纲里是绝对强信号，会走 SWITCH。
    d = classify("那只鹅的伤口是不是还得去医院看看", _pred(main="t_fit", scores={"t_fit": 0.01}), "t_sql")
    assert d.mode == TopicMode.NEW_TOPIC
    assert d.closest_topic == "t_fit"
    assert d.closest_score == 0.01


def test_new_when_no_main():
    d = classify("完全不相关的新话题", _pred(scores={}), "t_sql")
    assert d.mode == TopicMode.NEW_TOPIC
    assert d.closest_topic is None


def test_entity_hints_are_soft_only():
    # 实体关联话题只是提示，不改变硬判定（仍是延续）
    d = classify("sqlite 继续", _pred(main="t_sql", scores={"t_sql": 0.8}), "t_sql",
                 entity_topics=["t_goose"])
    assert d.mode == TopicMode.IN_TOPIC
    assert d.entity_hints == ["t_goose"]

def test_related_topics_orders_by_weight(db_conn):
    now = "2026-08-14T00:00:00+00:00"
    for tid, name in [("t_a", "A"), ("t_b", "B"), ("t_c", "C")]:
        db_conn.execute("INSERT INTO nodes VALUES (?, 'topic', ?, '{}', ?, ?)", (tid, name, now, now))
    # A-B 权重 3（跳转多），A-C 权重 1
    db_conn.execute("INSERT INTO edges (id,src,dst,type,weight,created_at,updated_at) VALUES ('e1','t_a','t_b','related',3,?,?)", (now, now))
    db_conn.execute("INSERT INTO edges (id,src,dst,type,weight,created_at,updated_at) VALUES ('e2','t_c','t_a','related',1,?,?)", (now, now))
    from agent.services.affinity import related_topics
    assert related_topics(db_conn, "t_a", top_n=2) == ["t_b", "t_c"]
    assert related_topics(db_conn, "t_a", top_n=1) == ["t_b"]

def test_relate_shared_entities_builds_topic_edges(db_conn):
    now = "2026-08-14T00:00:00+00:00"
    for nid, typ, name in [("t_a", "topic", "A"), ("t_b", "topic", "B"), ("e1", "entity", "鹅")]:
        db_conn.execute("INSERT INTO nodes VALUES (?, ?, ?, '{}', ?, ?)", (nid, typ, name, now, now))
    db_conn.execute("INSERT INTO edges (id,src,dst,type,weight,created_at,updated_at) VALUES ('m1','t_a','e1','mention',1,?,?)", (now, now))
    db_conn.execute("INSERT INTO edges (id,src,dst,type,weight,created_at,updated_at) VALUES ('m2','t_b','e1','mention',1,?,?)", (now, now))
    from agent.services.affinity import relate_shared_entities
    relate_shared_entities(db_conn, "t_a", ["e1"])
    row = db_conn.execute("SELECT type FROM edges WHERE type='related' AND src='t_a' AND dst='t_b'").fetchone()
    assert row is not None and row["type"] == "related"


# -- owner-first 三门槛（2026-10-02；数据见 backend/evals/topic_threshold/） ---------


def test_short_confirmation_never_starts_a_new_topic():
    """1~7 个字的确认/继续句不得被判成新话题（旧实现正是这么错的）。"""
    for text in ("好", "嗯", "继续", "ok", "继续刚才那个"):
        d = classify(text, _pred(scores={"t_sql": 0.0}), "t_sql")
        assert d.mode == TopicMode.IN_TOPIC, text


def test_incumbent_keeps_the_current_topic_when_no_other_topic_is_strong():
    """另一个话题只是略强（达不到 margin）时，不能把用户搬走。"""
    d = classify(
        "数据库迁移这块再确认一下",
        _pred(main="t_fit", scores={"t_fit": 0.44, "t_sql": 0.43}),
        "t_sql",
    )
    assert d.mode == TopicMode.IN_TOPIC


def test_new_topic_when_nothing_owns_it_and_current_has_no_signal():
    """无归属 + 现任无信号 + 长度足够 → 新话题；两个后端量纲各自成立。"""
    # 兜底层量纲：0.10 在关键词重合比例里已是明确信号（阈值 rules_incumbent_threshold=0.02），
    # 「现任无信号」要用真正的 0 来构造
    rules = classify(
        "推荐几本推理小说", _pred(main=None, scores={"t_sql": 0.0, "t_fit": 0.0}, backend="rules"), "t_sql"
    )
    assert rules.mode == TopicMode.NEW_TOPIC
    onnx = classify(
        "推荐几本推理小说", _pred(main=None, scores={"t_sql": 0.20, "t_fit": 0.18}, backend="onnx"), "t_sql"
    )
    assert onnx.mode == TopicMode.NEW_TOPIC


def test_rules_backend_uses_the_rules_scale_thresholds():
    """兜底路径的分数是关键词重合比例（量纲不同），必须走 rules_* 门槛。

    owner 由预测器按 rules_new_topic_threshold 判定后放在 main_topic_id 里；
    0.25 在 onnx 余弦下「没有归属」，在关键词重合下已经是明确命中。
    """
    from agent.services.params import TOPIC

    d = classify(
        "数据库 迁移 索引",
        SimpleNamespace(
            main_topic_id="t_sql",
            scores={"t_sql": 0.25},
            is_new_topic_candidate=False,
            backend_used="rules",
        ),
        "t_sql",
    )
    assert d.mode == TopicMode.IN_TOPIC
    assert TOPIC.rules_incumbent_threshold <= 0.25


def test_switch_still_only_suggests_and_never_executes():
    """推测切换的语义不变：这里只产出 SWITCH 决定，移动 Anchor 由上层确认。"""
    d = classify(
        "对了，健身那边教练说什么了",
        _pred(main="t_fit", scores={"t_fit": 0.72, "t_sql": 0.30}),
        "t_sql",
    )
    assert d.mode == TopicMode.SWITCH
    assert d.switch_to == "t_fit"