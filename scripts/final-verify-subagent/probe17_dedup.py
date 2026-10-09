# -*- coding: utf-8 -*-
"""独立验收（E）反例 17：提交去重是否按「身份结构」（自己写，不照抄 C 的用例）。

跑法（backend venv）：
  $env:QIO_DATA_DIR = <临时目录>   # 本脚本自己也会覆盖，避免碰用户真实库
  backend\.venv\Scripts\python.exe scripts\final-verify-subagent\probe17_dedup.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="qio-e17-")
os.environ["QIO_DATA_DIR"] = _TMP
os.environ["QIO_DEV_INSECURE"] = "1"
os.environ["QIO_DISABLE_DB_CHECK"] = "1"

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "backend" / "src"))

from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402
from agent.interactive import board_store, models, submission  # noqa: E402

import asyncio  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def _card(cid: str, kind: str, content: str, **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _link(lid: str, src: str, dst: str, *, meaning: str = "依据") -> dict:
    link = models.new_link(src, dst, direction=False, meaning=meaning)
    link["id"] = lid
    return link


def _save(conn, board_id, cards, groups=None, links=None, selection=None) -> dict:
    state = {
        "boardId": board_id, "seq": 0, "updatedAt": models.now_iso(),
        "cards": cards, "groups": list(groups or []), "links": list(links or []),
        "selection": list(selection or []),
    }
    return board_store.save_board(conn, board_id, state, reason="probe17")


def _submit(conn, board_id) -> dict:
    return asyncio.run(submission.submit_board(conn, board_id))


def _succeeded_count(conn, board_id) -> int:
    info = board_store.ensure_board(conn, board_id=board_id)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM board_submissions WHERE board_id = ? AND status = 'succeeded'",
        (info["id"],),
    ).fetchone()
    return int(row["n"])


def main() -> int:
    conn = connect(pathlib.Path(_TMP) / "probe17.db")
    apply_migrations(conn)

    # ---------- 17-1 指纹层：A、B 同内容不同身份，A→C 与 B→C 的指纹必须不同 ----------
    bid = "board_e17_fp"
    cards = [_card("A", "file", "同一段材料"), _card("B", "file", "同一段材料"), _card("C", "file", "另一份材料")]
    _save(conn, bid, cards, links=[_link("l1", "A", "C")])
    state_a = board_store.load_board(conn, bid)["state"]
    _save(conn, bid, cards, links=[_link("l2", "B", "C")])
    state_b = board_store.load_board(conn, bid)["state"]
    fp_a = submission.content_fingerprint(submission.project_snapshot(state_a))
    fp_b = submission.content_fingerprint(submission.project_snapshot(state_b))
    check("17-1 指纹：A→C 与 B→C（A/B 同内容）指纹不同", fp_a != fp_b, f"A={fp_a[:12]} B={fp_b[:12]}")

    # ---------- 17-2 真提交：同内容不同身份的关系改变必须成功且含 link_added + link_removed ----------
    bid = "board_e17_swap"
    cards = [_card("A", "file", "同一段材料"), _card("B", "file", "同一段材料"), _card("C", "file", "另一份材料")]
    _save(conn, bid, cards, links=[_link("l1", "A", "C")])
    first = _submit(conn, bid)
    check("17-2a A→C 首次提交成功", first["status"] == "succeeded", first["status"])
    _save(conn, bid, cards, links=[_link("l2", "B", "C")])
    second = _submit(conn, bid)
    kinds = [e.get("kind") for e in second.get("expressions", [])]
    check("17-2b A→C 换成 B→C 不是 duplicate", second["status"] == "succeeded", f"status={second['status']} kinds={kinds}")
    check("17-2c 改动表达含 link_added 与 link_removed", "link_added" in kinds and "link_removed" in kinds, str(kinds))

    # ---------- 17-3 真正撤回再加回（同 id 结构同正文，无中间提交）不得构成新提交 ----------
    bid = "board_e17_readd"
    _save(conn, bid, cards, links=[_link("l1", "A", "C")])
    ok1 = _submit(conn, bid)
    before_n = _succeeded_count(conn, bid)
    before_rows = conn.execute(
        "SELECT id FROM board_submissions WHERE board_id = ? AND status = 'succeeded' ORDER BY seq DESC LIMIT 1",
        (board_store.ensure_board(conn, board_id=bid)["id"],),
    ).fetchone()["id"]
    _save(conn, bid, cards, links=[])           # 撤回（未提交中间态）
    _save(conn, bid, cards, links=[_link("l1", "A", "C")])  # 加回同样关系
    again = _submit(conn, bid)
    after_n = _succeeded_count(conn, bid)
    after_rows = conn.execute(
        "SELECT id FROM board_submissions WHERE board_id = ? AND status = 'succeeded' ORDER BY seq DESC LIMIT 1",
        (board_store.ensure_board(conn, board_id=bid)["id"],),
    ).fetchone()["id"]
    check("17-3a 撤回再加回不构成新提交", again["status"] in ("duplicate", "empty"), f"status={again['status']}")
    check("17-3b 撤回再加回不投递 QIO", again["delivery"]["delivered"] is False, str(again["delivery"]))
    check("17-3c 基准未被更新（成功提交数与此前一致）", after_n == before_n == 1 and after_rows == before_rows,
          f"count {before_n}->{after_n} last {before_rows}->{after_rows}")
    check("17-3d 首次提交确实成功（对照）", ok1["status"] == "succeeded", ok1["status"])

    # ---------- 17-3e 撤回再加回但换了 link id（同端点同正文）也必须仍判重复 ----------
    bid = "board_e17_readd_newid"
    _save(conn, bid, cards, links=[_link("l1", "A", "C")])
    _submit(conn, bid)
    _save(conn, bid, cards, links=[])
    _save(conn, bid, cards, links=[_link("l9", "A", "C")])
    re2 = _submit(conn, bid)
    check("17-3e 同端点同正文、换了 link id 仍判重复", re2["status"] in ("duplicate", "empty"), str(re2["status"]))

    # ---------- 17-4 有序组成员顺序变化是真实改动；来回调整不提交则仍判重复 ----------
    bid = "board_e17_order"
    group = models.new_group("一组", ordered=True, default_name=False, members=["A", "B"])
    group["id"] = "g1"
    _save(conn, bid, cards, groups=[group])
    o1 = _submit(conn, bid)
    check("17-4a 有序组首次提交成功", o1["status"] == "succeeded", o1["status"])
    # 未提交的来回调整 [A,B] → [B,A] → [A,B]
    _save(conn, bid, cards, groups=[{**group, "members": ["B", "A"]}])
    _save(conn, bid, cards, groups=[{**group, "members": ["A", "B"]}])
    osc = _submit(conn, bid)
    check("17-4b 来回调整后回到基准顺序不构成新提交", osc["status"] in ("duplicate", "empty"), str(osc["status"]))
    # 真正提交一次顺序变化
    _save(conn, bid, cards, groups=[{**group, "members": ["B", "A"]}])
    reorder = _submit(conn, bid)
    kinds2 = [e.get("kind") for e in reorder.get("expressions", [])]
    check("17-4c 有序组成员顺序变化判为真实改动", reorder["status"] == "succeeded" and "order_changed" in kinds2,
          f"status={reorder['status']} kinds={kinds2}")
    # 基准已更新：相对新基准再改回 [A,B] 也是真实改动
    _save(conn, bid, cards, groups=[{**group, "members": ["A", "B"]}])
    back = _submit(conn, bid)
    check("17-4d 相对新基准的顺序变化同样是真实改动", back["status"] == "succeeded", back["status"])

    # ---------- 17-5 对抗性：同内容不同身份但端点集合不变（A→C 与 B→C 同时存在时移除 A→C） ----------
    bid = "board_e17_mixed"
    _save(conn, bid, cards, links=[_link("l1", "A", "C"), _link("l2", "B", "C")])
    _submit(conn, bid)
    _save(conn, bid, cards, links=[_link("l2", "B", "C")])
    mixed = _submit(conn, bid)
    kinds3 = [e.get("kind") for e in mixed.get("expressions", [])]
    check("17-5 移除 A→C（B→C 保留）是真实改动", mixed["status"] == "succeeded" and "link_removed" in kinds3,
          f"status={mixed['status']} kinds={kinds3}")

    conn.close()

    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(("PASS  " if ok else "FAIL  ") + name + ("   | " + detail if detail else ""))
    print(json.dumps({"tmp": _TMP, "total": len(RESULTS), "failed": len(failed)}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
