"""板面操作语义的纯函数用例（子智能体 A 负责实现）。

契约：docs/interactive-mode-contract.md §1.2（G1..G8）/ §1.3（分组与顺序）/ §3。

这里不碰数据库、不碰网络、不碰文件：全部是对 board.py 纯函数的断言。
行文按契约条文分组，便于逐条对照验收。
"""

from __future__ import annotations

import copy

import pytest

from agent.interactive import board, models

# --- 造数据的小工具 --------------------------------------------------------


def mk_card(
    cid: str,
    x: float = 0,
    y: float = 0,
    *,
    w: float = 100.0,
    h: float = 60.0,
    kind: str = "text",
    content: str = "",
    meta: dict | None = None,
    **extra: object,
) -> dict:
    card = models.new_card(kind, content)
    card.update({"id": cid, "x": float(x), "y": float(y), "w": float(w), "h": float(h)})
    if meta is not None:
        card["meta"] = dict(meta)
    card.update(extra)
    return card


def mk_group(
    gid: str,
    members: list[str],
    *,
    name: str = "组 1",
    ordered: bool = False,
    default_name: bool = True,
    **extra: object,
) -> dict:
    group = models.new_group(name, ordered=ordered, default_name=default_name)
    group.update({"id": gid, "members": list(members)})
    group.update(extra)
    return group


def mk_state(
    cards: list[dict] | None = None,
    groups: list[dict] | None = None,
    links: list[dict] | None = None,
    selection: list[str] | None = None,
    *,
    board_id: str = "board_t",
) -> dict:
    return {
        "boardId": board_id,
        "seq": 0,
        "updatedAt": "2026-10-06T00:00:00+00:00",
        "cards": list(cards or []),
        "groups": list(groups or []),
        "links": list(links or []),
        "selection": list(selection or []),
    }


def group_ids(state: dict) -> list[str]:
    return [group["id"] for group in state["groups"]]


def members_of(state: dict, gid: str) -> list[str]:
    for group in state["groups"]:
        if group["id"] == gid:
            return list(group["members"])
    return []


def card_of(state: dict, cid: str) -> dict:
    card = models.card_by_id(state, cid)
    assert card is not None, f"card not found: {cid}"
    return card


def group_of(state: dict, gid: str) -> dict:
    for group in state["groups"]:
        if group["id"] == gid:
            return group
    raise AssertionError(f"group not found: {gid}")


def link_pairs(state: dict) -> list[tuple[str, str]]:
    return [(link["src"], link["dst"]) for link in state["links"]]


# --- §1.2 状态不变式 G1..G8 ------------------------------------------------


def test_g1_card_in_two_groups_keeps_first_membership_and_dedupes_members():
    """G1：一张卡最多属于一个组；同一组里成员不重复。"""
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 200, 0)],
        groups=[
            mk_group("g1", ["a", "a", "b"]),
            mk_group("g2", ["b", "a"]),
        ],
    )
    result = board.normalize_state(state)
    assert members_of(result, "g1") == ["a", "b"]
    # b 已经在 g1 里，g2 只剩 a，而 a 也已经在 g1 → g2 空了，按 G3 消失
    assert group_ids(result) == ["g1"]


def test_g1_second_group_keeps_members_not_claimed_by_first():
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 200, 0)],
        groups=[mk_group("g1", ["a"]), mk_group("g2", ["a", "b"])],
    )
    result = board.normalize_state(state)
    assert members_of(result, "g1") == ["a"]
    assert members_of(result, "g2") == ["b"]


def test_g2_ordered_group_members_are_the_sequence_and_stay_contiguous():
    """G2：有序组 members 顺序即序号，永远连续（1..n 由列表顺序表达）。"""
    state = mk_state(
        cards=[mk_card("a"), mk_card("b"), mk_card("c")],
        groups=[mk_group("g1", ["c", "a", "b"], ordered=True)],
    )
    result = board.normalize_state(state)
    assert members_of(result, "g1") == ["c", "a", "b"]
    assert group_of(result, "g1")["ordered"] is True


def test_g3_group_without_live_members_disappears():
    """G3：成员全被移除或删除的组自动消失（不存在空组）。"""
    state = mk_state(
        cards=[mk_card("a", deleted=True), mk_card("b")],
        groups=[mk_group("g1", ["a"]), mk_group("g2", ["b"])],
    )
    result = board.normalize_state(state)
    assert group_ids(result) == ["g2"]


def test_g4_link_endpoints_must_exist_and_only_one_per_pair():
    """G4：端点必须存在且未删除；同一对 (src,dst) 只保留一条。"""
    state = mk_state(
        cards=[mk_card("a"), mk_card("b"), mk_card("c", deleted=True)],
        links=[
            models.new_link("a", "b", meaning="第一条"),
            models.new_link("a", "b", meaning="重复"),
            models.new_link("b", "a", meaning="反向重复"),
            models.new_link("a", "c", meaning="端点是已删除卡片"),
            models.new_link("a", "ghost", meaning="端点不存在"),
            {**models.new_link("a", "b", meaning="已删除的链接"), "deleted": True},
        ],
    )
    result = board.normalize_state(state)
    assert len(result["links"]) == 1
    assert result["links"][0]["meaning"] == "第一条"


def test_g5_deleted_card_leaves_groups_links_and_selection():
    """G5：被删除的卡片不进组、不进链接、不参与选择。"""
    state = mk_state(
        cards=[mk_card("a"), mk_card("b", deleted=True)],
        groups=[mk_group("g1", ["a", "b"])],
        links=[models.new_link("a", "b")],
        selection=["a", "b"],
    )
    result = board.normalize_state(state)
    assert members_of(result, "g1") == ["a"]
    assert result["links"] == []
    assert result["selection"] == ["a"]


def test_g6_hidden_forces_checked_false():
    """G6：hidden=true 时 checked 强制为 false（两者互斥）。"""
    state = mk_state(cards=[mk_card("a", checked=True, hidden=True), mk_card("b", checked=True)])
    result = board.normalize_state(state)
    assert card_of(result, "a")["checked"] is False
    assert card_of(result, "a")["hidden"] is True
    assert card_of(result, "b")["checked"] is True


def test_g7_reply_card_never_checked():
    """G7：reply 卡片不参与注释勾选语义。"""
    state = mk_state(cards=[mk_card("r", kind="reply", checked=True, content="结果")])
    result = board.normalize_state(state)
    assert card_of(result, "r")["checked"] is False


def test_g8_selection_only_live_existing_cards_and_deduped():
    """G8：selection 只含存在且未删除的卡片。"""
    state = mk_state(
        cards=[mk_card("a"), mk_card("b", deleted=True)],
        selection=["a", "a", "b", "ghost"],
    )
    result = board.normalize_state(state)
    assert result["selection"] == ["a"]


def test_normalize_returns_new_state_and_does_not_mutate_input():
    state = mk_state(
        cards=[mk_card("a", checked=True, hidden=True)],
        groups=[mk_group("g1", ["a", "ghost"])],
        selection=["ghost"],
    )
    snapshot = copy.deepcopy(state)
    result = board.normalize_state(state)
    assert state == snapshot, "normalize_state 不得原地修改输入"
    assert result is not state
    assert result["cards"][0] is not state["cards"][0]


def test_normalize_keeps_ordered_group_numbering_continuous_after_deletion():
    """有序组成员被删除后序号仍然连续（列表本身没有空洞）。"""
    state = mk_state(
        cards=[mk_card("a"), mk_card("b", deleted=True), mk_card("c")],
        groups=[mk_group("g1", ["a", "b", "c"], ordered=True)],
    )
    result = board.normalize_state(state)
    members = members_of(result, "g1")
    assert members == ["a", "c"]
    assert list(range(1, len(members) + 1)) == [1, 2]


def test_normalize_reflows_group_frame_around_members():
    """组框跟着成员走（位置只影响显示，不是意图依据）。"""
    state = mk_state(
        cards=[mk_card("a", 100, 50), mk_card("b", 300, 200)],
        groups=[mk_group("g1", ["a", "b"], x=0, y=0, w=1, h=1)],
    )
    result = board.normalize_state(state)
    frame = group_of(result, "g1")
    assert frame["x"] <= 100 and frame["y"] <= 50
    assert frame["x"] + frame["w"] >= 400
    assert frame["y"] + frame["h"] >= 260


# --- §1.3 拖动预演与放下 ----------------------------------------------------


def two_free_cards():
    return mk_state(cards=[mk_card("a", 0, 0), mk_card("b", 240, 0)])


def test_preview_drop_over_free_card_reports_the_card_to_join():
    state = two_free_cards()
    preview = board.preview_drop(state, "a", 250, 10)
    assert preview["groupId"] is None, "组还不存在，放下后才建组"
    assert preview["mergesWith"] == "b"
    assert preview["index"] is None


def test_preview_drop_into_existing_group_reports_group_and_index():
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 200, 0), mk_card("c", 600, 0)],
        groups=[mk_group("g1", ["a", "b"], name="材料整理", ordered=True, default_name=False)],
    )
    preview = board.preview_drop(state, "c", 300, 30)
    assert preview["groupId"] == "g1"
    assert preview["index"] == 2, "落在第 2 张之后 → 插入序号 2"
    assert preview["mergesWith"] is None


def test_preview_drop_of_grouped_card_onto_other_group_reports_merge():
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 200, 0), mk_card("c", 600, 0)],
        groups=[
            mk_group("g1", ["a", "b"], name="第一组", default_name=False),
            mk_group("g2", ["c"], name="第二组", default_name=False),
        ],
    )
    preview = board.preview_drop(state, "c", 100, 30)
    assert preview["groupId"] == "g1"
    assert preview["mergesWith"] == "g2", "将被并入的是拖过来的那一组"
    assert preview["index"] == 1


def test_preview_drop_on_empty_board_area_reports_nothing_and_does_not_mutate():
    state = two_free_cards()
    snapshot = copy.deepcopy(state)
    preview = board.preview_drop(state, "a", 2000, 2000)
    assert preview == {"groupId": None, "index": None, "mergesWith": None}
    assert state == snapshot, "拖动期间只预演，不改状态"


def test_preview_drop_of_deleted_or_unknown_card_is_empty():
    state = mk_state(cards=[mk_card("a", deleted=True)])
    assert board.preview_drop(state, "a", 0, 0) == {"groupId": None, "index": None, "mergesWith": None}
    assert board.preview_drop(state, "ghost", 0, 0) == {"groupId": None, "index": None, "mergesWith": None}


def test_drop_two_free_cards_overlapping_forms_group_with_default_name():
    """两张未分组卡片重叠 → 自动成组，默认组名「组 1」。"""
    state = two_free_cards()
    result = board.drop_card(state, "a", 250, 10)
    next_state = result["state"]
    assert result["merged"] is False
    assert result["groupId"] is not None
    group = group_of(next_state, result["groupId"])
    assert group["name"] == models.default_group_name(1)
    assert group["defaultName"] is True
    assert group["ordered"] is False
    assert sorted(group["members"]) == ["a", "b"]
    assert card_of(next_state, "a")["x"] == 250


def test_default_group_name_skips_existing_numbers():
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 240, 0), mk_card("c", 2000, 0), mk_card("d", 2040, 0)],
        groups=[mk_group("g7", ["c", "d"], name="组 7", default_name=True)],
    )
    result = board.drop_card(state, "a", 250, 10)
    group = group_of(result["state"], result["groupId"])
    assert group["name"] == models.default_group_name(8), "默认名不与现有「组 N」重名"


def test_drop_free_card_into_existing_group_keeps_group_name():
    """未分组卡片拖入已有组 → 直接加入并保留原组名。"""
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 200, 0), mk_card("c", 600, 0)],
        groups=[mk_group("g1", ["a", "b"], name="发布计划", default_name=False, ordered=True)],
    )
    result = board.drop_card(state, "c", 300, 30)
    next_state = result["state"]
    assert result["groupId"] == "g1"
    assert result["merged"] is False
    assert result["index"] == 2
    group = group_of(next_state, "g1")
    assert group["name"] == "发布计划", "加入已有组不改组名"
    assert group["members"] == ["a", "b", "c"], "按落点插入有序组，后续序号更新"
    assert card_of(next_state, "c")["x"] == 300


def test_drop_new_card_before_first_member_inserts_at_front():
    state = mk_state(
        cards=[mk_card("a", 100, 0), mk_card("b", 300, 0), mk_card("c", 900, 0)],
        groups=[mk_group("g1", ["a", "b"], ordered=True)],
    )
    result = board.drop_card(state, "c", 100, 30)
    assert members_of(result["state"], "g1") == ["c", "a", "b"]
    assert result["index"] == 0


def test_drop_grouped_card_onto_other_group_merges_with_default_name():
    """两个已有组重叠 → 合并：原组不再独立保留，新组用默认名。"""
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 200, 0), mk_card("c", 600, 0), mk_card("d", 800, 0)],
        groups=[
            mk_group("g1", ["a", "b"], name="目标组", default_name=False),
            mk_group("g2", ["c", "d"], name="被拖动组", default_name=False),
        ],
    )
    result = board.drop_card(state, "c", 100, 30)
    next_state = result["state"]
    assert result["merged"] is True
    assert group_ids(next_state) == ["g1"], "原组不再独立保留"
    group = group_of(next_state, "g1")
    assert group["name"] == models.default_group_name(1)
    assert group["defaultName"] is True
    assert group["members"] == ["a", "c", "d", "b"], "被拖入组的成员连续插入目标位置"


def test_drop_ordered_card_onto_ordered_group_preserves_internal_orders_and_renumbers():
    """两个有序组合并：保留各自内部顺序，连续插入、统一编号。"""
    state = mk_state(
        cards=[
            mk_card("a", 0, 0),
            mk_card("b", 200, 0),
            mk_card("c", 600, 0),
            mk_card("d", 800, 0),
            mk_card("e", 1000, 0),
        ],
        groups=[
            mk_group("g1", ["a", "b"], ordered=True, name="第一批", default_name=False),
            mk_group("g2", ["c", "d", "e"], ordered=True, name="第二批", default_name=False),
        ],
    )
    # 落在 a 与 b 之间（a 中心 x=50，b 中心 x=250）→ 整组连续插到第 1 位
    result = board.drop_card(state, "d", 100, 30)
    group = group_of(result["state"], "g1")
    assert group["ordered"] is True
    assert group["members"] == ["a", "c", "d", "e", "b"]
    assert group["name"] == models.default_group_name(1), "合并后的新组用默认名（来源组不再占号）"
    assert group["defaultName"] is True


def test_merge_ordered_with_plain_group_becomes_plain():
    """有序组与普通组合并 → 普通组（取消序号），用户可重新设为有序。"""
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 200, 0), mk_card("c", 600, 0), mk_card("d", 800, 0)],
        groups=[
            mk_group("g1", ["a", "b"], ordered=True),
            mk_group("g2", ["c", "d"], ordered=False, name="普通组", default_name=False),
        ],
    )
    result = board.drop_card(state, "c", 100, 30)
    group = group_of(result["state"], "g1")
    assert group["ordered"] is False
    assert group["members"] == ["a", "c", "d", "b"]
    # 用户可以重新设为有序
    again = board.set_group_ordered(result["state"], "g1", True)
    assert group_of(again, "g1")["ordered"] is True


def test_drop_card_out_of_group_removes_membership_and_drops_empty_group():
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 200, 0)],
        groups=[mk_group("g1", ["a", "b"])],
    )
    result = board.drop_card(state, "a", 2000, 2000)
    next_state = result["state"]
    assert result["groupId"] is None
    assert group_ids(next_state) == ["g1"], "组里还有 b，组还在"
    assert members_of(next_state, "g1") == ["b"]

    single = mk_state(cards=[mk_card("a", 0, 0)], groups=[mk_group("g1", ["a"])])
    moved = board.drop_card(single, "a", 2000, 2000)
    assert group_ids(moved["state"]) == [], "最后一名成员被拖走 → 组消失"
    assert moved["groupId"] is None


def test_drop_within_own_group_reorders_members():
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 200, 0), mk_card("c", 400, 0)],
        groups=[mk_group("g1", ["a", "b", "c"], ordered=True)],
    )
    result = board.drop_card(state, "c", 150, 30)
    assert result["groupId"] == "g1"
    assert result["index"] == 1
    assert members_of(result["state"], "g1") == ["a", "c", "b"]


def test_drop_onto_grouped_card_joins_its_group_not_a_new_one():
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 200, 0), mk_card("z", 2000, 0)],
        groups=[mk_group("g1", ["a", "b"], name="已有组", default_name=False)],
    )
    result = board.drop_card(state, "z", 100, 30)
    assert result["groupId"] == "g1"
    assert group_of(result["state"], "g1")["name"] == "已有组"
    assert sorted(members_of(result["state"], "g1")) == ["a", "b", "z"]


def test_drop_free_card_alone_only_moves_it():
    state = two_free_cards()
    result = board.drop_card(state, "a", 2000, 2000)
    next_state = result["state"]
    assert result == {"state": next_state, "groupId": None, "merged": False, "index": None}
    assert card_of(next_state, "a")["x"] == 2000
    assert group_ids(next_state) == []


def test_drop_overlapping_several_free_cards_forms_one_group_with_all():
    state = mk_state(cards=[mk_card("a", 0, 0), mk_card("b", 120, 0), mk_card("c", 240, 0)])
    result = board.drop_card(state, "c", 60, 10)
    group = group_of(result["state"], result["groupId"])
    assert sorted(group["members"]) == ["a", "b", "c"]


def test_drop_deleted_or_unknown_card_changes_nothing():
    state = mk_state(cards=[mk_card("a", deleted=True), mk_card("b", 2000, 0)])
    before = board.normalize_state(state)
    assert board.drop_card(state, "a", 0, 0)["state"] == before
    assert board.drop_card(state, "ghost", 0, 0)["state"] == before


def test_drop_does_not_explain_link_direction():
    """关系链接只表达关联/方向/用户写明的含义，不自行解释成因果或顺序。"""
    state = mk_state(cards=[mk_card("a", 0, 0), mk_card("b", 400, 0)])
    linked = board.add_link(state, "a", "b", True, "先讨论 b 再看 a")
    assert linked["links"][0]["direction"] is True
    assert linked["links"][0]["meaning"] == "先讨论 b 再看 a"
    # 系统不补写任何解释性文字
    assert linked["links"][0]["meaning"] == "先讨论 b 再看 a"
    assert "因果" not in models.dumps(linked["links"][0])


# --- 分组操作（§4.2 面向前端的同名纯函数） ---------------------------------


def test_join_group_inserts_at_index_and_removes_from_previous_group():
    state = mk_state(
        cards=[mk_card("a"), mk_card("b"), mk_card("c")],
        groups=[mk_group("g1", ["a", "b"]), mk_group("g2", ["c"], name="第二组", default_name=False)],
    )
    joined = board.join_group(state, "c", "g1", 1)
    assert members_of(joined, "g1") == ["a", "c", "b"]
    assert group_ids(joined) == ["g1"], "离开 g2 后 g2 空了 → 消失"
    appended = board.join_group(state, "c", "g1")
    assert members_of(appended, "g1") == ["a", "b", "c"]


def test_join_group_index_is_clamped():
    state = mk_state(cards=[mk_card("a"), mk_card("b"), mk_card("c")], groups=[mk_group("g1", ["a", "b"])])
    assert members_of(board.join_group(state, "c", "g1", 99), "g1") == ["a", "b", "c"]
    assert members_of(board.join_group(state, "c", "g1", -5), "g1") == ["c", "a", "b"]


def test_remove_from_group_and_dissolve_group():
    state = mk_state(
        cards=[mk_card("a"), mk_card("b"), mk_card("c")],
        groups=[mk_group("g1", ["a", "b"]), mk_group("g2", ["c"], name="第二组", default_name=False)],
    )
    removed = board.remove_from_group(state, "a")
    assert members_of(removed, "g1") == ["b"]
    dissolved = board.dissolve_group(state, "g1")
    assert group_ids(dissolved) == ["g2"]
    assert board.dissolve_group(state, "g1")["cards"][0]["id"] == "a", "解除组不动卡片"


def test_rename_group_marks_default_name_only_for_default_pattern():
    state = mk_state(cards=[mk_card("a")], groups=[mk_group("g1", ["a"])])
    renamed = board.rename_group(state, "g1", "  发布计划  ")
    group = group_of(renamed, "g1")
    assert group["name"] == "发布计划"
    assert group["defaultName"] is False
    back = board.rename_group(renamed, "g1", "组 3")
    assert group_of(back, "g1")["defaultName"] is True
    unchanged = board.rename_group(state, "g1", "   ")
    assert group_of(unchanged, "g1")["name"] == "组 1", "空名字不生效"


def test_move_within_group_clamps_and_ignores_outsiders():
    state = mk_state(
        cards=[mk_card("a"), mk_card("b"), mk_card("c")],
        groups=[mk_group("g1", ["a", "b", "c"])],
    )
    moved = board.move_within_group(state, "g1", "c", 0)
    assert members_of(moved, "g1") == ["c", "a", "b"]
    clamped = board.move_within_group(state, "g1", "a", 99)
    assert members_of(clamped, "g1") == ["b", "c", "a"]
    outsider = board.move_within_group(state, "g1", "zzz", 0)
    assert members_of(outsider, "g1") == ["a", "b", "c"]


def test_merge_groups_keeps_target_id_and_uses_default_name():
    state = mk_state(
        cards=[mk_card("a"), mk_card("b"), mk_card("c"), mk_card("d")],
        groups=[
            mk_group("g1", ["a", "b"], name="目标", default_name=False),
            mk_group("g2", ["c", "d"], name="来源", default_name=False),
        ],
    )
    merged = board.merge_groups(state, "g2", "g1", 1)
    assert group_ids(merged) == ["g1"]
    group = group_of(merged, "g1")
    assert group["members"] == ["a", "c", "d", "b"]
    assert group["name"] == models.default_group_name(1)
    assert group["defaultName"] is True


def test_merge_groups_appends_when_no_index():
    state = mk_state(
        cards=[mk_card("a"), mk_card("b"), mk_card("c")],
        groups=[mk_group("g1", ["a"]), mk_group("g2", ["b", "c"])],
    )
    merged = board.merge_groups(state, "g2", "g1")
    assert members_of(merged, "g1") == ["a", "b", "c"]


# --- 关系链接 --------------------------------------------------------------


def test_add_link_stores_user_written_meaning_and_direction():
    state = mk_state(cards=[mk_card("a"), mk_card("b")])
    linked = board.add_link(state, "a", "b", False, "同一批材料")
    assert len(linked["links"]) == 1
    link = linked["links"][0]
    assert (link["src"], link["dst"]) == ("a", "b")
    assert link["direction"] is False
    assert link["meaning"] == "同一批材料"


def test_add_link_rejects_bad_endpoints_and_self_link():
    state = mk_state(cards=[mk_card("a"), mk_card("b", deleted=True)])
    assert board.add_link(state, "a", "b", False, "x")["links"] == []
    assert board.add_link(state, "a", "ghost", False, "x")["links"] == []
    assert board.add_link(state, "a", "a", False, "自环")["links"] == []


def test_add_link_twice_between_same_pair_updates_instead_of_duplicating():
    state = mk_state(cards=[mk_card("a"), mk_card("b")])
    once = board.add_link(state, "a", "b", False, "同一批材料")
    twice = board.add_link(once, "b", "a", True, "先看 b 再看 a")
    assert len(twice["links"]) == 1, "同一对端点只保留一条"
    link = twice["links"][0]
    assert link["direction"] is True
    assert link["meaning"] == "先看 b 再看 a"


def test_update_and_remove_link():
    state = mk_state(cards=[mk_card("a"), mk_card("b")])
    linked = board.add_link(state, "a", "b", False, "关联")
    link_id = linked["links"][0]["id"]
    updated = board.update_link(linked, link_id, {"meaning": "用户改写的含义", "direction": True})
    assert updated["links"][0]["meaning"] == "用户改写的含义"
    assert updated["links"][0]["direction"] is True
    assert board.remove_link(updated, link_id)["links"] == []


def test_links_survive_group_merge_because_endpoints_are_cards():
    state = mk_state(
        cards=[mk_card("a", 0, 0), mk_card("b", 200, 0), mk_card("c", 600, 0)],
        groups=[mk_group("g1", ["a", "b"]), mk_group("g2", ["c"])],
    )
    linked = board.add_link(state, "a", "c", False, "关联")
    merged = board.merge_groups(linked, "g2", "g1", 0)
    assert len(merged["links"]) == 1
    assert members_of(merged, "g1") == ["c", "a", "b"]


# --- 卡片：添加 / 编辑 / 删除 / 复制 ---------------------------------------


def test_add_card_defaults():
    state = board.empty_state("board_t")
    added = board.add_card(state, "text", "一条注释")
    assert len(added["cards"]) == 1
    card = added["cards"][0]
    assert card["kind"] == "text"
    assert card["content"] == "一条注释"
    assert card["checked"] is False
    assert card["deleted"] is False
    assert card["id"].startswith("c_")
    state = added
    for kind in ("file", "image", "code", "url", "reply"):
        state = board.add_card(state, kind, "x")
    assert {card["kind"] for card in state["cards"]} == {"text", "file", "image", "code", "url", "reply"}


def test_add_card_unknown_kind_is_rejected():
    state = board.empty_state("board_t")
    assert board.add_card(state, "video")["cards"] == []


def test_update_card_patches_content_and_meta():
    state = mk_state(cards=[mk_card("a", content="旧")])
    updated = board.update_card(state, "a", {"content": "新", "meta": {"language": "python"}})
    assert card_of(updated, "a")["content"] == "新"
    assert card_of(updated, "a")["meta"] == {"language": "python"}
    assert card_of(state, "a")["content"] == "旧", "纯函数不改原状态"


def test_remove_card_keeps_it_flagged_and_cleans_relations():
    state = mk_state(
        cards=[mk_card("a"), mk_card("b")],
        groups=[mk_group("g1", ["a", "b"])],
        links=[models.new_link("a", "b")],
        selection=["a", "b"],
    )
    removed = board.remove_card(state, "a")
    assert card_of(removed, "a")["deleted"] is True
    assert members_of(removed, "g1") == ["b"]
    assert removed["links"] == []
    assert removed["selection"] == ["b"]


def test_duplicate_card_offsets_copy_and_resets_confirmation():
    state = mk_state(
        cards=[mk_card("a", 10, 20, checked=True, hidden=False, bookmarked=True)],
        groups=[mk_group("g1", ["a"])],
    )
    copied = board.duplicate_card(state, "a")
    assert len(copied["cards"]) == 2
    new_card = [card for card in copied["cards"] if card["id"] != "a"][0]
    assert new_card["content"] == card_of(state, "a")["content"]
    assert (new_card["x"], new_card["y"]) == (34.0, 44.0)
    assert new_card["checked"] is False, "副本是新材料，需要重新勾选"
    assert new_card["deleted"] is False
    assert members_of(copied, "g1") == ["a", new_card["id"]], "副本留在原来的组里"


# --- 选择 / 勾选 / 隐藏 / 折叠 / 书签 / 搜索 --------------------------------


def test_set_selection_dedupes_and_filters_deleted():
    state = mk_state(cards=[mk_card("a"), mk_card("b", deleted=True)])
    assert board.set_selection(state, ["a", "a", "b", "ghost"])["selection"] == ["a"]


def test_select_in_rect_replaces_or_adds():
    state = mk_state(cards=[mk_card("a", 0, 0), mk_card("b", 400, 0), mk_card("c", 800, 0)])
    rect = {"x": -10, "y": -10, "w": 300, "h": 200}
    replaced = board.select_in_rect(state, rect, False)
    assert replaced["selection"] == ["a"]
    additive = board.select_in_rect(mk_state(cards=state["cards"], selection=["c"]), rect, True)
    assert additive["selection"] == ["c", "a"]


def test_set_checked_only_applies_to_text_annotations():
    state = mk_state(
        cards=[
            mk_card("t", kind="text", hidden=True),
            mk_card("f", kind="file", meta={"name": "a.pdf"}),
            mk_card("r", kind="reply"),
        ]
    )
    checked = board.set_checked(state, "t", True)
    assert card_of(checked, "t")["checked"] is True
    assert card_of(checked, "t")["hidden"] is False, "勾选与明确隐藏互斥：勾选即回到讨论范围"
    assert card_of(board.set_checked(state, "f", True), "f")["checked"] is False, "材料没有勾选框"
    assert card_of(board.set_checked(state, "r", True), "r")["checked"] is False, "reply 不参与勾选"
    unchecked = board.set_checked(checked, "t", False)
    assert card_of(unchecked, "t")["checked"] is False


def test_set_hidden_clears_checked_and_unhide_does_not_recheck():
    state = mk_state(cards=[mk_card("t", checked=True)])
    hidden = board.set_hidden(state, "t", True)
    assert card_of(hidden, "t")["hidden"] is True
    assert card_of(hidden, "t")["checked"] is False
    shown = board.set_hidden(hidden, "t", False)
    assert card_of(shown, "t")["hidden"] is False
    assert card_of(shown, "t")["checked"] is False, "取消隐藏不等于重新勾选"


def test_set_folded_and_bookmark_are_display_only():
    state = mk_state(cards=[mk_card("t", checked=True)])
    folded = board.set_folded(state, "t", True)
    assert card_of(folded, "t")["folded"] is True
    assert card_of(folded, "t")["checked"] is True, "折叠不改变任何含义"
    bookmarked = board.set_bookmark(folded, "t", True)
    assert card_of(bookmarked, "t")["bookmarked"] is True


def test_search_cards_finds_unchecked_notes_and_meta_and_prioritizes_bookmarks():
    state = mk_state(
        cards=[
            mk_card("n1", content="发布节奏需要确认", checked=False),
            mk_card("n2", content="发布前检查清单", bookmarked=True),
            mk_card("u1", kind="url", content="", meta={"href": "https://example.com/roadmap", "title": "路线图"}),
            mk_card("d1", content="发布节奏已删除", deleted=True),
        ]
    )
    found = board.search_cards(state, "发布")
    assert found == ["n2", "n1"], "书签优先；未勾选注释也能搜到（板内搜索不是交给 QIO）"
    assert board.search_cards(state, "ROADMAP") == ["u1"], "大小写不敏感，且能搜 meta"
    assert board.search_cards(state, "   ") == []
    assert board.search_cards(state, "不存在的词") == []
