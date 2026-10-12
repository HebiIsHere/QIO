"""历史逐卡迁移（text_migration.py）的契约测试。

契约：docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md
§1.1（逐卡迁移，绝不合并）、§1.4（冗余字段）、§2.2（board_store 加载后迁移一次）。

锁死的性质：

- code 卡 → 文字卡，一个 code 块，块 text **逐字**等于原 content，语言取自 meta.language，checked=True；
- url 卡 → 文字卡，一个 link 块，href 取自 meta.url.href（缺失回退 content），标题取自 meta.url.title；
- 已有 meta.doc 的 text 卡原样不动；缺 meta.doc 的 text 卡补齐 doc / docText，**checked 不变**；
- 其他 kind 原样不动；
- id / x / y / w / h / createdAt / 分组 / 关系 / 书签 / 折叠 / 隐藏 / 删除全部原样保留；
- 幂等：第二次迁移不再改动，board_store 也不再写板面（seq 不变）。
"""

from __future__ import annotations

import sqlite3

from agent.interactive import board_store, models, text_card, text_migration

BOARD = "board_migration"
FENCE = text_card.FENCE

CODE_BODY = "  def f():\n      return 1\n"


def _state(cards=(), groups=(), links=(), selection=()) -> dict:
    return {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": list(groups),
        "links": list(links),
        "selection": list(selection),
    }


def _code_card(**fields) -> dict:
    return models.new_card("code", CODE_BODY, meta={"language": "python"}, **fields)


def _url_card(**fields) -> dict:
    card = models.new_card("url", "https://fallback.example/x", **fields)
    return card


def _block(doc: dict, kind: str) -> dict:
    found = [block for block in doc["blocks"] if block["kind"] == kind]
    assert found, [block["kind"] for block in doc["blocks"]]
    return found[0]


# -- code 卡 ---------------------------------------------------------------


def test_code_card_becomes_text_with_verbatim_code_block():
    card = _code_card(checked=False)
    migrated, changed = text_migration.migrate_card(card)

    assert changed is True
    assert migrated["kind"] == "text"
    assert migrated["checked"] is True, "迁移后的旧代码卡视为已许可"
    block = _block(migrated["meta"]["doc"], "code")
    assert block["text"] == CODE_BODY, "代码必须逐字保留（缩进 / 换行 / 行尾空白）"
    assert block["attrs"]["language"] == "python"
    assert migrated["meta"]["doc"]["schema"] == text_card.TEXT_DOC_SCHEMA
    assert migrated["meta"]["docText"].startswith(FENCE + "python")
    assert migrated["content"].strip() == CODE_BODY.strip()


def test_code_card_without_language_keeps_empty_language():
    card = models.new_card("code", "x = 1")
    migrated, changed = text_migration.migrate_card(card)
    assert changed is True
    assert _block(migrated["meta"]["doc"], "code")["attrs"]["language"] == ""
    assert migrated["content"] == "x = 1"


# -- url 卡 ----------------------------------------------------------------


def test_url_card_becomes_text_with_link_block():
    card = models.new_card(
        "url",
        "https://fallback.example/x",
        meta={"url": {"href": "https://github.com/HebiIsHere/QIO", "title": "QIO 仓库"}},
    )
    migrated, changed = text_migration.migrate_card(card)

    assert changed is True
    assert migrated["kind"] == "text"
    assert migrated["checked"] is True
    block = _block(migrated["meta"]["doc"], "link")
    assert block["attrs"]["href"] == "https://github.com/HebiIsHere/QIO"
    assert block["text"] == "QIO 仓库"
    assert migrated["content"] == "QIO 仓库 <https://github.com/HebiIsHere/QIO>"
    assert migrated["meta"]["url"]["title"] == "QIO 仓库", "旧 meta 原样保留"


def test_url_card_without_href_falls_back_to_content():
    card = _url_card(meta={"url": {"title": "只有标题"}})
    migrated, changed = text_migration.migrate_card(card)
    assert changed is True
    block = _block(migrated["meta"]["doc"], "link")
    assert block["attrs"]["href"] == "https://fallback.example/x"
    assert block["text"] == "只有标题"


def test_url_card_without_meta_url_at_all():
    card = models.new_card("url", "https://only.example")
    migrated, changed = text_migration.migrate_card(card)
    assert changed is True
    block = _block(migrated["meta"]["doc"], "link")
    assert block["attrs"]["href"] == "https://only.example"
    assert block["text"] == ""


# -- text 卡 ---------------------------------------------------------------


def test_text_card_with_doc_is_untouched():
    card = text_card.new_text_card("已经迁移过的文字", checked=True)
    same, changed = text_migration.migrate_card(card)
    assert changed is False
    assert same is card or same == card
    assert same["meta"]["doc"] == card["meta"]["doc"]
    assert same["checked"] is True


def test_text_card_without_doc_gets_doc_and_keeps_unchecked():
    card = models.new_card("text", "普通注释", checked=False)
    migrated, changed = text_migration.migrate_card(card)
    assert changed is True
    assert migrated["meta"]["doc"]["schema"] == text_card.TEXT_DOC_SCHEMA
    assert migrated["meta"]["docText"] == "普通注释\n"
    assert migrated["checked"] is False, "缺 meta.doc 的旧注释迁移后仍不可见"
    assert models.visible_except_deleted(migrated) is False


def test_text_card_without_doc_keeps_checked_true():
    card = models.new_card("text", "已勾选的注释", checked=True)
    migrated, changed = text_migration.migrate_card(card)
    assert changed is True
    assert migrated["checked"] is True
    assert models.visible_except_deleted(migrated) is True


def test_empty_text_card_gets_empty_doc():
    card = models.new_card("text", "")
    migrated, changed = text_migration.migrate_card(card)
    assert changed is True
    assert migrated["meta"]["doc"] == text_card.empty_doc()
    assert migrated["meta"]["docText"] == ""


def test_ready_text_card_is_not_migrated_again():
    card = models.new_card("text", "第一遍", checked=False)
    first, changed = text_migration.migrate_card(card)
    assert changed is True
    second, again = text_migration.migrate_card(first)
    assert again is False
    assert second["meta"]["doc"] == first["meta"]["doc"]
    assert second["meta"]["docText"] == first["meta"]["docText"]
    assert second["checked"] is False


# -- 其他 kind --------------------------------------------------------------


def test_other_kinds_are_untouched():
    for kind in ("file", "image", "reply"):
        card = models.new_card(kind, "内容")
        same, changed = text_migration.migrate_card(card)
        assert changed is False, kind
        assert same["kind"] == kind
        assert same["meta"] == {}


def test_non_dict_is_returned_untouched():
    same, changed = text_migration.migrate_card(None)  # type: ignore[arg-type]
    assert changed is False
    assert same is None


# -- 身份 / 板面字段保留 ----------------------------------------------------


def test_identity_and_board_fields_are_preserved():
    card = _code_card(
        checked=False,
        x=11.5,
        y=22.25,
        w=333.0,
        h=199.0,
        bookmarked=True,
        folded=True,
        hidden=False,
        deleted=False,
        createdAt="2026-01-02T03:04:05+00:00",
    )
    card["id"] = "c_fixed_legacy_code"

    migrated, changed = text_migration.migrate_card(card)
    assert changed is True
    for key, value in (
        ("id", "c_fixed_legacy_code"),
        ("x", 11.5),
        ("y", 22.25),
        ("w", 333.0),
        ("h", 199.0),
        ("createdAt", "2026-01-02T03:04:05+00:00"),
        ("bookmarked", True),
        ("folded", True),
        ("hidden", False),
        ("deleted", False),
    ):
        assert migrated[key] == value, key
    assert migrated["meta"]["language"] == "python"


def test_deleted_and_hidden_legacy_cards_keep_their_flags():
    deleted = _code_card(checked=True, deleted=True)
    migrated, changed = text_migration.migrate_card(deleted)
    assert changed is True and migrated["deleted"] is True

    hidden = models.new_card("url", "https://a.example", hidden=True)
    migrated_hidden, changed_hidden = text_migration.migrate_card(hidden)
    assert changed_hidden is True
    assert migrated_hidden["hidden"] is True
    assert migrated_hidden["checked"] is True, "迁移不改变 hidden（仍是明确隐藏）"
    assert models.visible_except_deleted(migrated_hidden) is False


# -- migrate_state ----------------------------------------------------------


def _board_with_legacy() -> tuple[dict, dict, dict, dict]:
    code = _code_card(bookmarked=True)
    url = models.new_card("url", "https://a.example", meta={"url": {"href": "https://a.example", "title": "A"}})
    note = models.new_card("text", "旧注释", checked=False)
    file_card = models.new_card("file", "报告.pdf", meta={"name": "报告.pdf"})
    group = models.new_group("默认组名", members=[code["id"], url["id"], note["id"]])
    link = models.new_link(code["id"], url["id"], meaning="同一条线")
    state = _state([code, url, note, file_card], groups=[group], links=[link])
    return state, code, url, note


def test_migrate_state_migrates_every_card_in_place_and_keeps_relations():
    state, code, url, note = _board_with_legacy()
    migrated_state, changed = text_migration.migrate_state(state)

    assert changed is True
    kinds = [card["kind"] for card in migrated_state["cards"]]
    assert kinds == ["text", "text", "text", "file"]
    by_id = {card["id"]: card for card in migrated_state["cards"]}
    assert by_id[code["id"]]["meta"]["doc"]["blocks"][0]["kind"] == "code"
    assert by_id[url["id"]]["meta"]["doc"]["blocks"][0]["kind"] == "link"
    assert by_id[note["id"]]["checked"] is False
    assert by_id[note["id"]]["meta"]["doc"]["blocks"][0]["text"] == "旧注释"
    assert by_id[file_card_id(migrated_state)]["meta"] == {"name": "报告.pdf"}

    # 分组与关系原样
    assert migrated_state["groups"][0]["members"] == [code["id"], url["id"], note["id"]]
    assert migrated_state["links"][0]["src"] == code["id"]
    assert migrated_state["links"][0]["dst"] == url["id"]
    assert migrated_state["links"][0]["meaning"] == "同一条线"
    assert by_id[code["id"]]["bookmarked"] is True
    assert [card["id"] for card in migrated_state["cards"]] == [
        card["id"] for card in state["cards"]
    ], "绝不合并或重排卡片"


def file_card_id(state: dict) -> str:
    for card in state["cards"]:
        if card["kind"] == "file":
            return card["id"]
    raise AssertionError("no file card")


def test_migrate_state_is_idempotent():
    state, *_ = _board_with_legacy()
    once, changed = text_migration.migrate_state(state)
    assert changed is True
    snapshot = models.dumps(once)
    twice, changed_again = text_migration.migrate_state(once)
    assert changed_again is False
    assert models.dumps(twice) == snapshot


def test_migrate_state_without_legacy_cards_reports_no_change():
    state = _state([models.new_card("file", "a.txt")])
    same, changed = text_migration.migrate_state(state)
    assert changed is False
    assert same["cards"][0]["kind"] == "file"


# -- board_store：加载后迁移一次 --------------------------------------------


def _put_legacy_state(conn, state: dict, *, seq: int = 1) -> None:
    """把「升级前就存在」的板面直接写进库（模拟历史数据，不走 save_board 的规范化）。"""
    board_store.ensure_board(conn, board_id=BOARD)
    stamp = models.now_iso()
    dumped = models.dumps({**state, "boardId": BOARD, "seq": seq})
    conn.execute(
        "UPDATE board_states SET seq = ?, state = ?, reason = 'op', updated_at = ?"
        " WHERE board_id = ?",
        (seq, dumped, stamp, BOARD),
    )
    conn.execute(
        "INSERT INTO board_state_snapshots (id, board_id, seq, state, reason, created_at)"
        " VALUES (?, ?, ?, ?, 'op', ?)",
        (models.new_id("snap"), BOARD, seq, dumped, stamp),
    )
    conn.commit()


def test_board_store_migrates_on_load_and_writes_once(db_conn):
    state, code, url, note = _board_with_legacy()
    _put_legacy_state(db_conn, state, seq=1)

    loaded = board_store.load_board(db_conn, BOARD)
    assert loaded["seq"] == 2, "迁移要保存一次（seq+1）"
    assert loaded["state"]["cards"][0]["kind"] == "text"

    row = db_conn.execute(
        "SELECT seq, reason FROM board_states WHERE board_id = ?", (BOARD,)
    ).fetchone()
    assert row["seq"] == 2
    assert row["reason"] == "text_card_migration"

    # 第二次加载：已经迁移过，不再写
    again = board_store.load_board(db_conn, BOARD)
    assert again["seq"] == 2
    assert again["state"]["cards"][0]["kind"] == "text"

    # 历史快照不被改写：seq=1 的快照里仍是旧的 code 卡
    history = board_store.list_board_states(db_conn, BOARD)
    first_snapshot = [item for item in history if item["seq"] == 1][0]
    assert first_snapshot["state"]["cards"][0]["kind"] == "code"
    assert first_snapshot["reason"] == "op"


def test_board_store_migration_keeps_drafts(db_conn):
    board_store.save_draft(db_conn, BOARD, {"c_legacy": "还没提交的输入"})
    _put_legacy_state(db_conn, _state([_code_card()]), seq=1)
    loaded = board_store.load_board(db_conn, BOARD)
    assert loaded["state"]["cards"][0]["kind"] == "text"
    assert board_store.get_draft(db_conn, BOARD)["drafts"] == {"c_legacy": "还没提交的输入"}


def test_board_store_does_not_write_when_nothing_to_migrate(db_conn):
    board_store.save_board(db_conn, BOARD, _state([models.new_card("file", "a.txt")]))
    loaded = board_store.load_board(db_conn, BOARD)
    assert loaded["seq"] == 1
    assert loaded["migratedFromSeq"] is None


def test_board_store_save_normalizes_legacy_cards(db_conn):
    """保存即规范化：保存返回的 seq 就是库里的 seq，随后加载不会再偷偷 +1。"""
    saved = board_store.save_board(db_conn, BOARD, _state([_code_card()]))
    assert saved["seq"] == 1
    assert saved["state"]["cards"][0]["kind"] == "text"
    loaded = board_store.load_board(db_conn, BOARD)
    assert loaded["seq"] == 1
    assert loaded["migratedFromSeq"] is None
    assert loaded["state"]["cards"][0]["kind"] == "text"


def test_text_card_visible_range_after_migration():
    unchecked = models.new_card("text", "未许可", checked=False)
    checked = models.new_card("text", "已许可", checked=True)
    state = _state([unchecked, checked])
    migrated, changed = text_migration.migrate_state(state)
    assert changed is True
    visible = models.selectable_ids(migrated)
    assert unchecked["id"] not in visible
    assert checked["id"] in visible
