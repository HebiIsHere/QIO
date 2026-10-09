"""收尾轮反例测试（C）：17 真实关系改变被判为重复提交。

正确行为期望（去重按**身份结构**）：

- 卡片 id、链接端点 (src, dst) 的对象身份、组成员 id 集合与顺序构成去重键；
  正文不替换端点身份。同正文不同身份 ≠ duplicate；同身份同结构同正文 = duplicate。
- 覆盖：同内容多卡片（A、B 内容一样）、不同连接（A→C 换成 B→C）、组成员 / 顺序变化，
  以及真正撤回再加回（同 id 结构同正文）必须**仍然**被判 duplicate（不废除去重）。
- 旧基准（content_hash 是旧内容指纹方案）也兼容：以基准快照重新计算身份指纹判定。
"""

from __future__ import annotations

import asyncio
import sqlite3

from agent.interactive import board_store, models, submission

BOARD = "board_final_c_dedup"


def _card(cid: str, kind: str, content: str = "", **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _link(lid: str, src: str, dst: str, *, meaning: str = "") -> dict:
    link = models.new_link(src, dst, direction=False, meaning=meaning)
    link["id"] = lid
    return link


def _save(conn: sqlite3.Connection, cards: list[dict], groups=None, links=None, selection=None) -> dict:
    state = {
        "boardId": BOARD, "seq": 0, "updatedAt": models.now_iso(),
        "cards": cards, "groups": list(groups or []), "links": list(links or []),
        "selection": list(selection or []),
    }
    return board_store.save_board(conn, BOARD, state, reason="test")


def _submit(conn: sqlite3.Connection, **kwargs) -> dict:
    return asyncio.run(submission.submit_board(conn, BOARD, **kwargs))


def _seed(conn: sqlite3.Connection):
    """A、B 内容相同但身份不同；C 是另一份材料。"""
    _save(conn, [_card("A", "file", "同一段材料"), _card("B", "file", "同一段材料"), _card("C", "file", "另一份材料")])


def test_same_text_but_different_identity_is_not_duplicate(db_conn):
    _seed(db_conn)
    first = _submit(db_conn)
    assert first["status"] == "succeeded"

    # 成功提交 A→C 后，把关系换成 B→C（表达已有新增 / 移除关系）
    state = board_store.load_board(db_conn, BOARD)["state"]
    state["links"] = [_link("l2", "B", "C", meaning="依据")]
    _save(db_conn, state["cards"], [], state["links"])

    second = _submit(db_conn)
    assert second["status"] == "succeeded", "身份不同 + 关系结构变化 → 不是重复提交"
    kinds = [expr["kind"] for expr in second["expressions"]]
    assert "link_added" in kinds and "link_removed" in kinds


def test_same_content_multiple_cards_with_new_link_not_duplicate(db_conn):
    """A、B 内容相同：第一次提交了 A→C；之后新增 B→C（A→C 保留）。旧实现按内容指纹会误判 duplicate。"""
    _seed(db_conn)
    state = board_store.load_board(db_conn, BOARD)["state"]
    state["links"] = [_link("l1", "A", "C", meaning="依据")]
    _save(db_conn, state["cards"], [], state["links"])
    first = _submit(db_conn)
    assert first["status"] == "succeeded"

    state["links"] = [_link("l1", "A", "C", meaning="依据"), _link("l2", "B", "C", meaning="依据")]
    _save(db_conn, state["cards"], [], state["links"])
    second = _submit(db_conn)
    assert second["status"] == "succeeded", "同内容不同卡的新关系是真实的新表达"


def test_true_withdraw_and_readd_is_still_duplicate(db_conn):
    """撤回 A→C 再加回完全一样的 A→C（同 id 结构同正文）→ 仍然 duplicate。"""
    _seed(db_conn)
    state = board_store.load_board(db_conn, BOARD)["state"]
    state["links"] = [_link("l1", "A", "C", meaning="依据")]
    _save(db_conn, state["cards"], [], state["links"])
    assert _submit(db_conn)["status"] == "succeeded"

    state["links"] = []
    _save(db_conn, state["cards"], [], state["links"])
    withdrawn = _submit(db_conn)
    assert withdrawn["status"] == "succeeded"

    state["links"] = [_link("l1", "A", "C", meaning="依据")]
    _save(db_conn, state["cards"], [], state["links"])
    again = _submit(db_conn)
    assert again["status"] == "duplicate", "同身份同结构同正文确实是重复提交"
    assert again["delivery"]["delivered"] is False


def test_group_membership_and_order_changes_are_real(db_conn):
    _seed(db_conn)
    state = board_store.load_board(db_conn, BOARD)["state"]
    group = models.new_group("一组", ordered=True, default_name=False, members=["A", "B"])
    group["id"] = "g1"
    _save(db_conn, state["cards"], [group], [])
    assert _submit(db_conn)["status"] == "succeeded"

    # 顺序变化：[A, B] → [B, A]（有序组的顺序是表达）
    state["groups"] = [{**group, "members": ["B", "A"]}]
    _save(db_conn, state["cards"], state["groups"], [])
    reordered = _submit(db_conn)
    assert reordered["status"] == "succeeded", "有序组成员顺序变化是真实改动"

    # 撤回顺序改动（回到 [A, B]）→ 与基准身份结构一致 → duplicate
    state["groups"] = [{**group, "members": ["A", "B"]}]
    _save(db_conn, state["cards"], state["groups"], [])
    restored = _submit(db_conn)
    assert restored["status"] == "duplicate"

    # 组成员变化（移除 B）→ 真实改动
    state["groups"] = [{**group, "members": ["A"]}]
    _save(db_conn, state["cards"], state["groups"], [])
    member_changed = _submit(db_conn)
    assert member_changed["status"] == "succeeded"


def test_legacy_baseline_hash_still_deduplicates(db_conn):
    """旧成功提交里存的 content_hash 是旧方案：按基准快照重新计算身份指纹来判定。"""
    _seed(db_conn)
    first = _submit(db_conn)
    assert first["status"] == "succeeded"
    db_conn.execute(
        "UPDATE board_submissions SET content_hash = 'legacy-content-hash' WHERE id = ?",
        (first["submission"]["id"],),
    )
    db_conn.commit()

    again = _submit(db_conn)
    assert again["status"] == "duplicate", "旧基准快照重算后仍应识别重复"
