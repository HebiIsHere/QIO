# -*- coding: utf-8 -*-
"""[final-E] 基线 b67d1fe 后端最小探针（API/数据库层，只读产品代码、只写临时库）。

目的：用真实函数（不走 mock）实证验收矩阵里「基线实际结果」一栏。
运行：cd backend && uv run --frozen python ../scripts/final-e-verify/final-e-baseline-backend-probe.py

探针允许断言「异常确实发生」；正确行为期望统一写在
docs/final-closure-acceptance-matrix.md（编号 09 / 16 / 17 / 18）。
数据落在 %TEMP% 下的独立目录，不碰用户真实 app.db；无任何凭据输出。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402
from agent.interactive import board_store, intents, submission  # noqa: E402
from agent.interactive.models import MATERIAL_KINDS, new_card, new_group, new_link  # noqa: E402


def fresh_conn(tmp: Path, name: str):
    conn = connect(tmp / name)
    apply_migrations(conn)
    return conn


def case_09(tmp: Path) -> dict:
    """09 长草稿：>20,000 字是否被悄悄截短并返回成功。"""
    conn = fresh_conn(tmp, "p09.db")
    board_store.ensure_board(conn, board_id="b09")
    out = {}
    for size in (20000, 20001, 20008):
        marker = f"<结尾唯一标识-{size}>"
        text = "字" * size + marker
        result = board_store.save_draft(conn, "b09", {"card:X": text})
        stored = board_store.get_draft(conn, "b09")["drafts"]["card:X"]
        out[size] = {
            "请求长度": len(text),
            "返回带 error 键": "error" in result,
            "落库长度": len(stored),
            "唯一标识还在": marker in stored,
        }
    conn.close()
    return out


def _board_with_two_materials(conn, board_id: str):
    a = new_card(MATERIAL_KINDS[0], "材料甲：唯一内容 X")
    c = new_card(MATERIAL_KINDS[2], "材料丙：代码 Y")
    state = {
        "boardId": board_id,
        "cards": [a, c],
        "groups": [],
        "links": [new_link(a["id"], c["id"], meaning="依据")],
        "selection": [],
    }
    board_store.save_board(conn, board_id, state, reason="seed")
    return a, c


def case_16(tmp: Path) -> dict:
    """16 材料已变：真实保存接口写入材料改动后，旧预览是否仍可批准。"""
    conn = fresh_conn(tmp, "p16.db")
    board_id = "b16"
    _board_with_two_materials(conn, board_id)
    created = intents.create_demo_intents(conn, board_id=board_id)
    combine = next(item for item in created if item.get("title") == intents.DEMO_TITLES["combine"])
    intent_id = combine["id"]
    before = intents.list_intents(conn, board_id)["intents"]
    before_status = next(x["status"] for x in before if x["id"] == intent_id)
    # 真实保存接口（PUT /state 同一路径）改掉材料正文
    state = board_store.load_board(conn, board_id)["state"]
    for card in state["cards"]:
        if card["kind"] in MATERIAL_KINDS:
            card["content"] += "（已被用户改过：这句不在创建预览时的依据里）"
            break
    board_store.save_board(conn, board_id, state, reason="user-edit")
    outcome = intents.approve_intent(conn, intent_id)
    conn.close()
    return {
        "意图创建时状态": before_status,
        "材料已通过保存接口改动": True,
        "批准结果 ok": outcome.get("ok"),
        "批准结果 reason": outcome.get("reason"),
        "批准后状态": outcome.get("intent", {}).get("status"),
    }


def case_17(tmp: Path) -> dict:
    """17 身份不同内容相同：A→C 成功后提交 B→C，是否被误判 duplicate。"""
    conn = fresh_conn(tmp, "p17.db")
    board_id = "b17"
    a, c = _board_with_two_materials(conn, board_id)
    first = asyncio.run(submission.submit_board(conn, board_id))
    # 换成内容完全相同、身份不同的 B，并把关系改为 B→C
    b = new_card(a["kind"], a["content"])
    state = {
        "boardId": board_id,
        "cards": [b, c],
        "groups": [],
        "links": [new_link(b["id"], c["id"], meaning="依据")],
        "selection": [],
    }
    board_store.save_board(conn, board_id, state, reason="swap-a-b")
    second = asyncio.run(submission.submit_board(conn, board_id))
    conn.close()
    return {
        "第一次提交状态": first["status"],
        "第二次提交状态": second["status"],
        "第二次 error": second.get("error"),
        "第二次返回键": sorted(second.keys()),
    }


def case_18(tmp: Path) -> dict:
    """18 演示成组：材料已在旧组，批准放新组后是否与预览不一致。"""
    conn = fresh_conn(tmp, "p18.db")
    board_id = "b18"
    a, c = _board_with_two_materials(conn, board_id)
    old = new_group("旧组", members=[a["id"], c["id"]], default_name=False)
    state = board_store.load_board(conn, board_id)["state"]
    state["groups"].append(old)
    board_store.save_board(conn, board_id, state, reason="seed-group")
    created = intents.create_demo_intents(conn, board_id=board_id)
    combine = next(item for item in created if item.get("title") == intents.DEMO_TITLES["combine"])
    intent_id = combine["id"]
    approved = intents.approve_intent(conn, intent_id)
    advanced = intents.advance_intent(conn, intent_id, outcome="done")
    fresh = board_store.load_board(conn, board_id)["state"]
    material_group = None
    for group in fresh["groups"]:
        if not group.get("deleted") and (a["id"] in group["members"] or c["id"] in group["members"]):
            material_group = group["name"]
    conn.close()
    return {
        "批准 ok": approved.get("ok"),
        "推进结果 ok": advanced.get("ok"),
        "推进后任务状态": advanced.get("intent", {}).get("status"),
        "applied.groupIds": list(advanced.get("applied", {}).get("groupIds", [])),
        "材料最终所在组": material_group,
        "板上各组与成员": [
            {"name": g["name"], "members": g["members"], "defaultName": g.get("defaultName")}
            for g in fresh["groups"]
            if not g.get("deleted")
        ],
    }


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="qio-final-e-probe-"))
    print("临时数据库目录：", tmp)
    print("\n=== 09 长草稿 ===")
    for size, info in case_09(tmp).items():
        print(f"  {size} 字 → {info}")
    print("\n=== 16 材料已变仍可批准 ===")
    for key, value in case_16(tmp).items():
        print(f"  {key}: {value}")
    print("\n=== 17 身份不同被判 duplicate ===")
    for key, value in case_17(tmp).items():
        print(f"  {key}: {value}")
    print("\n=== 18 演示成组落地 ===")
    for key, value in case_18(tmp).items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
