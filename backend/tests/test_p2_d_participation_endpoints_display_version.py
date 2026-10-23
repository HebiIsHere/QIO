# -*- coding: utf-8 -*-
"""Phase 2-D：参与状态 / 组端点 / 图片比例 / 对象版本（后端定向）。

契约：D:/qio-dev/qio-interactive-v2/docs/interactive-page-v2-contract.md §5.1–§5.4。

锁死的性质：

- participation 是参与范围的唯一事实源；旧数据按 hidden / checked / kind 一次性迁移；
- 组端点写法 group:<id>；不可见组不能进载荷，组快照不泄露未参与成员；
- 图片 file 卡的 meta.display 只接受真实像素/比例，非法值丢弃且不猜；w/h 服从比例；
- card/group/link version 由服务端权威推进；纯布局不改语义版本，普通保存不丢版本。
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from agent.interactive import board, board_store, intent_service, models, runtime, text_migration
from agent.interactive import tool_models

from test_p3_b_support import (
    FakeApp,
    add_submission,
    board_tools,
    bound,
    intent_scope,
    intent_spec,
    make_board,
    prepare,
)

BOARD = "board_p2d"


@pytest.fixture(autouse=True)
def _clean_version_memory():
    """每个用例从空的服务端版本基线开始（否则同一 boardId 的用例会互相影响）。"""
    board._BOARD_VERSIONS.clear()
    yield
    board._BOARD_VERSIONS.clear()


# --- 造数据 -----------------------------------------------------------------


def _state(cards=(), groups=(), links=(), selection=(), *, board_id: str = BOARD) -> dict:
    return {
        "boardId": board_id,
        "seq": 1,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": list(groups),
        "links": list(links),
        "selection": list(selection),
    }


def _card(cid: str, kind: str = "text", *, content: str = "", meta=None, **fields) -> dict:
    card = models.new_card(kind, content, **({**fields, "meta": meta} if meta is not None else fields))
    card["id"] = cid
    card["x"] = float(fields.get("x", 10))
    card["y"] = float(fields.get("y", 20))
    return card


def _group(gid: str, members, **fields) -> dict:
    group = models.new_group("默认组名")
    group.update({"id": gid, "members": list(members)})
    group.update(fields)
    return group


def _link(src: str, dst: str, **fields) -> dict:
    link = models.new_link(src, dst)
    link.update(fields)
    return link


def _cards_by_id(state: dict) -> dict[str, dict]:
    return {str(card["id"]): card for card in state["cards"]}


# --- §5.1 参与状态：一次性迁移 / 唯一事实源 ----------------------------------


def test_participation_migration_rules_and_marker():
    unchecked = _card("t_off", "text", content="未勾选")
    checked = _card("t_on", "text", content="已勾选", checked=True)
    hidden = _card("t_hidden", "text", content="隐藏", checked=True, hidden=True)
    file_card = _card("f1", "file", meta={"file": {"name": "报告.pdf"}}, checked=False)
    reply_off = _card("r_off", "reply", content="未许可结果", checked=False)
    reply_on = _card("r_on", "reply", content="已许可结果", checked=True)
    code = _card("legacy_code", "code", content="print(1)", meta={"language": "python"}, checked=True)
    url = _card("legacy_url", "url", content="https://a.example", meta={"url": {"href": "https://a.example"}}, checked=True)

    state = board.normalize_state(_state([unchecked, checked, hidden, file_card, reply_off, reply_on, code, url]))
    by_id = _cards_by_id(state)

    assert state["participationVersion"] == 1
    assert by_id["t_off"]["participation"] == models.PARTICIPATION_EXCLUDED
    assert by_id["t_on"]["participation"] == models.PARTICIPATION_INCLUDED
    assert by_id["t_hidden"]["participation"] == models.PARTICIPATION_EXCLUDED
    assert by_id["f1"]["participation"] == models.PARTICIPATION_INCLUDED
    assert by_id["r_off"]["participation"] == models.PARTICIPATION_EXCLUDED
    assert by_id["r_on"]["participation"] == models.PARTICIPATION_INCLUDED
    assert by_id["legacy_code"]["participation"] == models.PARTICIPATION_EXCLUDED
    assert by_id["legacy_url"]["participation"] == models.PARTICIPATION_EXCLUDED

    visible = models.selectable_ids(state)
    assert "t_on" in visible and "t_off" not in visible and "t_hidden" not in visible
    assert "f1" in visible
    assert "r_on" in visible and "r_off" not in visible
    assert "legacy_code" not in visible and "legacy_url" not in visible


def test_participation_is_idempotent_and_explicit_value_wins():
    card = _card("t1", "text", content="x", checked=True)
    first = board.normalize_state(_state([card]))
    second = board.normalize_state(first)
    assert second["cards"][0]["participation"] == models.PARTICIPATION_INCLUDED
    assert second["participationVersion"] == 1

    explicit = dict(first["cards"][0])
    explicit["participation"] = models.PARTICIPATION_EXCLUDED
    explicit["checked"] = True
    normalized = board.normalize_state(_state([explicit]))
    assert normalized["cards"][0]["participation"] == models.PARTICIPATION_EXCLUDED


def test_migrated_legacy_code_stays_excluded():
    code = models.new_card("code", "print(2)", meta={"language": "python"})
    code.pop("participation", None)  # 真实历史数据没有 Phase 2 字段
    migrated, changed = text_migration.migrate_card(code)
    assert changed is True and migrated["kind"] == "text"
    state = board.normalize_state(_state([migrated]))
    assert state["cards"][0]["participation"] == models.PARTICIPATION_EXCLUDED
    assert models.selectable_ids(state) == set()


def test_set_participation_and_legacy_checkbox_mirror():
    state = board.normalize_state(_state([_card("t1", "text", checked=False)]))
    assert state["cards"][0]["participation"] == models.PARTICIPATION_EXCLUDED
    included = board.set_participation(state, "t1", True)
    assert included["cards"][0]["participation"] == models.PARTICIPATION_INCLUDED
    assert included["cards"][0]["checked"] is True

    excluded = board.update_card(included, "t1", {"participation": "excluded"})
    assert excluded["cards"][0]["participation"] == models.PARTICIPATION_EXCLUDED
    checked_again = board.set_checked(excluded, "t1", True)
    assert checked_again["cards"][0]["participation"] == models.PARTICIPATION_INCLUDED


def test_models_loads_migrates_participation_and_marker():
    raw = models.dumps(_state([_card("t1", "text", checked=True)]))
    loaded = models.loads(raw, None)
    assert loaded["participationVersion"] == 1
    assert loaded["cards"][0]["participation"] == models.PARTICIPATION_INCLUDED


# --- §5.3 组端点 ------------------------------------------------------------


def _endpoint_state() -> dict:
    a = _card("a", "text", content="A", checked=True)
    b = _card("b", "text", content="B", checked=False)
    c = _card("c", "text", content="C", checked=True)
    d = _card("d", "text", content="D", checked=False)
    g_mixed = _group("g_mixed", ["a", "b"])
    g_none = _group("g_none", ["d"])
    links = [
        models.new_link("a", "group:g_mixed", meaning="卡到组"),
        models.new_link("group:g_mixed", "a", meaning="反向重复"),
        models.new_link("group:g_none", "c", meaning="不可见组"),
        models.new_link("c", "a", meaning="卡到卡"),
        models.new_link("group:g_mixed", "group:g_mixed", meaning="自连"),
        models.new_link("a", "group:ghost", meaning="不存在的组"),
    ]
    return _state([a, b, c, d], [g_mixed, g_none], links)


def test_group_endpoints_normalize_validate_and_dedupe():
    state = board.normalize_state(_endpoint_state())
    pairs = {(link["src"], link["dst"]) for link in state["links"]}
    assert ("a", "group:g_mixed") in pairs
    assert ("c", "a") in pairs
    # 不可见组、自连、不存在的组全部丢弃；反向重复只留一条。
    assert len(state["links"]) == 2
    assert all("g_none" not in (link["src"] + link["dst"]) for link in state["links"])
    assert all("ghost" not in (link["src"] + link["dst"]) for link in state["links"])

    # card:<id> 兼容写法归一化成裸 card id
    compat = board.normalize_state(
        _state([_card("a", "text", checked=True), _card("b", "text", checked=True)],
               links=[models.new_link("card:a", "b")])
    )
    assert [(link["src"], link["dst"]) for link in compat["links"]] == [("a", "b")]


def test_group_participation_and_visibility():
    state = board.normalize_state(_endpoint_state())
    by_id = {group["id"]: group for group in state["groups"]}
    assert models.group_participation(state, by_id["g_mixed"]) == "partial"
    assert models.group_participation(state, by_id["g_none"]) == "none"
    assert models.visible_group_ids(state) == {"g_mixed"}
    assert models.link_endpoint_visible(state, "group:g_mixed") is True
    assert models.link_endpoint_visible(state, "group:g_none") is False
    assert models.link_visible(state, {"src": "a", "dst": "group:g_mixed"}) is True
    assert models.link_visible(state, {"src": "c", "dst": "group:g_none"}) is False
    visible_pairs = {(link["src"], link["dst"]) for link in models.visible_links(state)}
    assert visible_pairs == {("a", "group:g_mixed"), ("c", "a")}


def test_add_and_update_link_group_endpoints():
    state = board.normalize_state(_endpoint_state())
    added = board.add_link(state, "c", "group:g_mixed", False, "补充关系")
    pair = [(link["src"], link["dst"]) for link in added["links"]]
    assert ("c", "group:g_mixed") in pair
    assert board.add_link(state, "c", "group:g_none", False, "x")["links"] == state["links"]
    assert board.add_link(state, "group:g_mixed", "group:g_mixed", False, "自连")["links"] == state["links"]


def test_dissolve_group_drops_group_endpoint_links_but_keeps_card_links():
    a = _card("a", "text", checked=True)
    b = _card("b", "text", checked=True)
    group = _group("g1", ["a"])
    state = board.normalize_state(
        _state([a, b], [group], [models.new_link("a", "group:g1"), models.new_link("a", "b")])
    )
    assert len(state["links"]) == 2
    dissolved = board.dissolve_group(state, "g1")
    pairs = [(link["src"], link["dst"]) for link in dissolved["links"]]
    assert pairs == [("a", "b")]


def test_tool_inspect_filters_group_members_and_links(env_p3):
    conn, app = env_p3
    with bound(board_id=BOARD):
        result = asyncio.run(board_tools(app)["board_inspect"].run())
    assert result.ok is True, result.error
    groups = {group["id"]: group for group in result.data["groups"]}
    assert groups["g_mixed"]["members"] == ["a"]
    assert groups["g_mixed"]["participation"] == "partial"
    assert "g_none" not in groups
    pairs = {(link["src"], link["dst"]) for link in result.data["links"]}
    assert ("a", "group:g_mixed") in pairs
    assert all("g_none" not in (src + dst) for src, dst in pairs)
    cards = {card["id"]: card for card in result.data["cards"]}
    assert cards["a"]["participation"] == "included"
    assert "b" not in cards


def test_tool_add_link_group_endpoint_requires_authorization(env_p3):
    conn, app = env_p3
    tools = board_tools(app)
    state = board_store.current_state(conn, BOARD)
    # 额外建一个「可见但未授权」的组，用来证明组端点需要授权。
    state = board.create_group(state, ["c"], "其他组")
    board_store.save_board(conn, BOARD, state, reason="p2d-other-group")
    g_other = next(group for group in state["groups"] if group["name"] == "其他组")
    spec = intent_spec(
        scope=intent_scope(
            read_card_ids=["a", "c"],
            group_ids=["g_mixed"],
            allowed_actions=["add_link"],
            max_new_links=2,
        )
    )
    proposed = intent_service.propose_intents(
        conn, board_id=BOARD, submission_id="sub_p2d", intents=[spec]
    )
    assert proposed["ok"], proposed
    intent_id = proposed["intents"][0]["intent_id"]
    intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=[intent_id])

    with bound(board_id=BOARD, submission_id="sub_p2d"):
        denied = asyncio.run(
            tools["board_add_link"].run(
                intent_id=intent_id,
                operation_key="link-group-denied",
                src="a",
                dst="group:" + str(g_other["id"]),
                meaning="未授权组",
            )
        )
        assert denied.ok is False
        assert denied.code == "scope_violation"
        allowed = asyncio.run(
            tools["board_add_link"].run(
                intent_id=intent_id,
                operation_key="link-group-ok",
                src="a",
                dst="group:g_mixed",
                meaning="卡到组",
            )
        )
        assert allowed.ok is True, allowed.error


def test_tool_order_group_rejects_partially_visible_group(env_p3):
    conn, app = env_p3
    tools = board_tools(app)
    spec = intent_spec(
        scope=intent_scope(group_ids=["g_mixed"], allowed_actions=["order_group"])
    )
    proposed = intent_service.propose_intents(
        conn, board_id=BOARD, submission_id="sub_p2d", intents=[spec]
    )
    assert proposed["ok"], proposed
    intent_id = proposed["intents"][0]["intent_id"]
    intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=[intent_id])
    state = board_store.current_state(conn, BOARD)
    version = int(next(card for card in state["cards"] if card["id"] == "a")["version"])
    with bound(board_id=BOARD, submission_id="sub_p2d"):
        result = asyncio.run(
            tools["board_order_group"].run(
                intent_id=intent_id,
                operation_key="order-partial",
                group_id="g_mixed",
                member_order=["a"],
                expected_versions={"a": version},
            )
        )
    assert result.ok is False
    assert result.code == "group_has_excluded_members"
    assert "members" not in result.data, "不得把未参与成员 id 列给模型"


# --- §5.2 图片比例 ----------------------------------------------------------


def _image_card(cid: str = "img", *, display=None, name: str = "图.png", w: float = 260.0, h: float = 170.0) -> dict:
    meta = {"file": {"name": name, "attachmentId": "att_1"}}
    if display is not None:
        meta["display"] = display
    card = _card(cid, "file", meta=meta)
    card["w"], card["h"] = w, h
    return card


def test_display_valid_pixels_compute_ratio_and_constrain_size():
    state = board.normalize_state(
        _state([_image_card(display={"width": 800, "height": 600, "aspectRatio": 0.1})])
    )
    card = state["cards"][0]
    assert card["meta"]["display"] == {"width": 800, "height": 600, "aspectRatio": round(800 / 600, 6)}
    assert card["w"] == pytest.approx(260.0)
    assert card["h"] == pytest.approx(195.0)


@pytest.mark.parametrize(
    "display",
    [
        {"width": 0, "height": 600},
        {"width": 800},
        {"width": 800, "height": 0},
        {"width": float("nan"), "height": 600},
        {"width": -1, "height": 600},
        {"width": 1_000_000, "height": 600},
        {"aspectRatio": 0},
        {"aspectRatio": 2000},
    ],
)
def test_invalid_display_is_dropped_without_guessing(display):
    card = _image_card(display=display, w=310.0, h=180.0)
    state = board.normalize_state(_state([card]))
    got = state["cards"][0]
    assert "display" not in got["meta"]
    assert (got["w"], got["h"]) == (310.0, 180.0), "未知比例不得改卡片尺寸"


def test_non_image_file_display_is_dropped_and_ratio_only_is_kept():
    pdf = _image_card("pdf", display={"width": 800, "height": 600}, name="报告.pdf")
    state = board.normalize_state(_state([pdf]))
    assert "display" not in state["cards"][0]["meta"]

    ratio_only = _image_card("ratio", display={"aspectRatio": 2.0})
    ratio_state = board.normalize_state(_state([ratio_only]))
    assert ratio_state["cards"][0]["meta"]["display"] == {"aspectRatio": 2.0}
    assert ratio_state["cards"][0]["h"] == pytest.approx(ratio_state["cards"][0]["w"] / 2.0)


# --- §5.4 版本 --------------------------------------------------------------


def test_layout_change_keeps_version_and_semantic_change_bumps():
    card = _card("t1", "text", content="原始正文", checked=True)
    card["version"] = 5
    state = board.normalize_state(_state([card]))
    assert state["cards"][0]["version"] == 5

    moved = board.update_card(state, "t1", {"x": 300, "y": 400})
    assert moved["cards"][0]["version"] == 5, "纯布局不改语义版本"
    folded = board.update_card(moved, "t1", {"folded": True})
    assert folded["cards"][0]["version"] == 5

    edited = board.update_card(folded, "t1", {"content": "改过的正文"})
    assert edited["cards"][0]["version"] == 6, "正文变化推进版本"


def test_group_and_link_versions_follow_semantics():
    a = _card("a", "text", checked=True)
    b = _card("b", "text", checked=True)
    group = _group("g1", ["a"])
    group["version"] = 3
    link = models.new_link("a", "b", meaning="原含义")
    link["version"] = 2
    state = board.normalize_state(_state([a, b], [group], [link]))
    assert state["groups"][0]["version"] == 3
    assert state["links"][0]["version"] == 2

    joined = board.join_group(state, "b", "g1")
    assert joined["groups"][0]["version"] == 4, "组成员变化推进组版本"
    same = board.update_link(joined, joined["links"][0]["id"], {"meaning": "原含义"})
    assert same["links"][0]["version"] == 2, "无变化不推进"
    changed = board.update_link(same, same["links"][0]["id"], {"meaning": "新含义"})
    assert changed["links"][0]["version"] == 3


def test_display_change_does_not_bump_material_version():
    card = _image_card(display={"width": 800, "height": 600})
    state = board.normalize_state(_state([card]))
    version = state["cards"][0]["version"]
    changed = board.update_card(
        state, "img", {"meta": {"file": {"name": "图.png", "attachmentId": "att_1"},
                               "display": {"width": 1024, "height": 768}}}
    )
    assert changed["cards"][0]["version"] == version
    assert changed["cards"][0]["meta"]["display"]["width"] == 1024


def test_save_roundtrip_keeps_and_authoritatively_advances_versions(db_conn):
    board_id = "board_p2d_versions"
    a = _card("a", "text", content="A", checked=True)
    b = _card("b", "text", content="B", checked=True)
    a["version"], b["version"] = 5, 5
    group = _group("g1", ["a", "b"])
    group["version"] = 3
    link = models.new_link("a", "b", meaning="关联")
    link["version"] = 2
    board_store.save_board(db_conn, board_id, _state([a, b], [group], [link], board_id=board_id))

    loaded = board_store.load_board(db_conn, board_id)["state"]
    assert {card["id"]: card["version"] for card in loaded["cards"]} == {"a": 5, "b": 5}
    assert loaded["groups"][0]["version"] == 3
    assert loaded["links"][0]["version"] == 2

    moved = board.update_card(loaded, "a", {"x": 500, "y": 600})
    board_store.save_board(db_conn, board_id, moved)
    reloaded = board_store.load_board(db_conn, board_id)["state"]
    assert next(card for card in reloaded["cards"] if card["id"] == "a")["version"] == 5
    assert reloaded["groups"][0]["version"] == 3

    edited = board.update_card(reloaded, "a", {"content": "A2"})
    board_store.save_board(db_conn, board_id, edited)
    reloaded2 = board_store.load_board(db_conn, board_id)["state"]
    assert next(card for card in reloaded2["cards"] if card["id"] == "a")["version"] == 6
    assert next(card for card in reloaded2["cards"] if card["id"] == "b")["version"] == 5

    # 客户端丢掉 version（普通保存路径）时，服务端仍按上一版语义推进。
    stripped = copy.deepcopy(reloaded2)
    for card in stripped["cards"]:
        card.pop("version", None)
    for item in stripped["groups"] + stripped["links"]:
        item.pop("version", None)
    target = next(card for card in stripped["cards"] if card["id"] == "a")
    target["content"] = "A3"
    board_store.save_board(db_conn, board_id, stripped)
    final = board_store.load_board(db_conn, board_id)["state"]
    assert next(card for card in final["cards"] if card["id"] == "a")["version"] == 7

    # 模拟进程重启：清空进程内版本记忆，普通保存仍然以数据库上一版为基线。
    board._BOARD_VERSIONS.clear()
    stripped_again = copy.deepcopy(final)
    for card in stripped_again["cards"]:
        card.pop("version", None)
    for item in stripped_again["groups"] + stripped_again["links"]:
        item.pop("version", None)
    next(card for card in stripped_again["cards"] if card["id"] == "a")["content"] = "A4"
    board_store.save_board(db_conn, board_id, stripped_again)
    restarted = board_store.load_board(db_conn, board_id)["state"]
    assert next(card for card in restarted["cards"] if card["id"] == "a")["version"] == 8

    # 客户端旧 version（例如缓存里的 1）也不能把服务端版本拉回去。
    old_client = copy.deepcopy(restarted)
    for card in old_client["cards"]:
        card["version"] = 1
    for item in old_client["groups"] + old_client["links"]:
        item["version"] = 1
    next(card for card in old_client["cards"] if card["id"] == "a")["content"] = "A5"
    board_store.save_board(db_conn, board_id, old_client)
    authoritative = board_store.load_board(db_conn, board_id)["state"]
    assert next(card for card in authoritative["cards"] if card["id"] == "a")["version"] == 9
    assert next(card for card in authoritative["cards"] if card["id"] == "b")["version"] == 5



# --- §5.3 提交投影 / 差异：组端点与不可见成员 -------------------------------


def test_submission_projection_keeps_group_endpoint_without_leaking_members():
    from agent.interactive import submission

    state = board.normalize_state(_endpoint_state())
    visible = submission.project_snapshot(state)
    ids = {card["id"] for card in visible["cards"]}
    assert "a" in ids and "c" in ids and "b" not in ids and "d" not in ids
    pairs = {(link["src"], link["dst"]) for link in visible["links"]}
    assert ("a", "group:g_mixed") in pairs
    assert all("g_none" not in (src + dst) for src, dst in pairs)
    group = next(item for item in visible["groups"] if item["id"] == "g_mixed")
    assert group["members"] == ["a"]
    assert "b" not in ids and "d" not in ids, "未参与成员不得出现在投影里"
    assert all("b" not in item["members"] and "d" not in item["members"] for item in visible["groups"])

    range_payload = submission.visible_range(state)
    assert {(link["src"], link["dst"]) for link in range_payload["links"]} == pairs


def test_submission_diff_and_baseline_handle_group_endpoints():
    from agent.interactive import submission

    state = board.normalize_state(_endpoint_state())
    before = {"cards": [], "groups": [], "links": [], "selection": [], "empty": True}
    after = submission.project_snapshot(state)
    expressions = submission.diff_states(before, after)
    added = [item for item in expressions if item["kind"] == "link_added"]
    group_link = next(item for item in added if "组「" in item["summary"])
    assert group_link["cardIds"] == ["a"], "表达式里的 cardIds 只放卡片端点"
    assert all("b" not in item.get("cardIds", []) and "d" not in item.get("cardIds", []) for item in expressions)

    baseline = {
        "snapshot": copy.deepcopy(after),
        "seq": 1,
        "submittedAt": models.now_iso(),
        "contentHash": "",
    }
    projected = submission.project_baseline(baseline, state)
    assert ("a", "group:g_mixed") in {(link["src"], link["dst"]) for link in projected["links"]}

    # 组不可见后，before 不再重现该组端点（不借撤回泄露组名/成员）。
    dissolved = board.dissolve_group(state, "g_mixed")
    projected_after = submission.project_baseline(baseline, dissolved)
    assert all("g_mixed" not in (link["src"] + link["dst"]) for link in projected_after["links"])


def test_display_change_is_not_a_content_expression():
    from agent.interactive import submission

    card = _image_card(display={"width": 800, "height": 600})
    before = board.normalize_state(_state([card]))
    changed = board.update_card(
        before, "img",
        {"meta": {"file": {"name": "图.png", "attachmentId": "att_1"},
                  "display": {"width": 1024, "height": 768}}},
    )
    b = submission.project_snapshot(before)
    a = submission.project_snapshot(changed)
    assert submission.diff_states(b, a) == []
    assert submission.content_fingerprint(b) == submission.content_fingerprint(a)


def test_material_content_version_ignores_display_but_not_content_or_attachment():
    from agent.interactive import materials

    card = _image_card(display={"width": 800, "height": 600})
    changed_display = _image_card(display={"width": 1024, "height": 768})
    changed_content = _image_card(display={"width": 800, "height": 600})
    changed_content["content"] = "新的说明"
    changed_attachment = _image_card(display={"width": 800, "height": 600})
    changed_attachment["meta"]["file"]["attachmentId"] = "att_2"

    base_version = materials._content_version(card)
    assert materials._content_version(changed_display) == base_version
    assert materials._content_version(changed_content) != base_version
    assert materials._content_version(changed_attachment) != base_version



def test_save_old_data_without_versions_uses_saved_baseline(db_conn):
    """旧数据（历史行里没有 version）第一次保存按基线 1，之后仍由数据库上一版推进。"""
    board_id = "board_p2d_old_versions"
    a = _card("a", "text", content="旧正文", checked=True)
    a.pop("version", None)
    board_store.save_board(db_conn, board_id, _state([a], board_id=board_id))
    loaded = board_store.load_board(db_conn, board_id)["state"]
    assert loaded["cards"][0]["version"] == 1

    board._BOARD_VERSIONS.clear()  # 模拟重启
    stripped = copy.deepcopy(loaded)
    stripped["cards"][0].pop("version", None)
    stripped["cards"][0]["content"] = "旧数据上的新正文"
    board_store.save_board(db_conn, board_id, stripped)
    final = board_store.load_board(db_conn, board_id)["state"]
    assert final["cards"][0]["version"] == 2


# --- fixture：DB + 已保存的组端点板面 ---------------------------------------


@pytest.fixture()
def env_p3(db_conn):
    prepare(db_conn)
    make_board(db_conn, board_id=BOARD)
    add_submission(db_conn, board_id=BOARD, submission_id="sub_p2d")
    state = board.normalize_state(_endpoint_state())
    board_store.save_board(db_conn, BOARD, state, reason="p2d-seed")
    return db_conn, FakeApp(db_conn)
