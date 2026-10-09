# -*- coding: utf-8 -*-
"""独立验收（E）反例 16：材料变了，旧待审批预览是否仍可批准（自己写）。

跑法（backend venv）：
  backend\.venv\Scripts\python.exe scripts\final-verify-subagent\probe16_approval.py

覆盖：
  A 走真实保存接口改材料（服务端保存时标记）→ 不提交 → approve 必须 ok=False / needs_update；
  B 绕过保存时标记（直接写库）只靠 approve 时复核 → 必须仍然拒绝（证明审批时确实重算）；
  C 只改位置（真实接口）→ 不得失效，approve 必须 ok=True；
  D 只改位置（直接写库）→ 不得失效；
  E 材料被标记删除 → 必须失效。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="qio-e16-")
os.environ["QIO_DATA_DIR"] = _TMP
os.environ["QIO_DEV_INSECURE"] = "1"
os.environ["QIO_DISABLE_DB_CHECK"] = "1"

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "backend" / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from agent.api.server import create_app  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.credentials.store import MemoryKeyring  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402
from agent.interactive import board_store, models  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SEPARATE = "［演示］保持材料分开，分别给出说明"
COMBINE = "［演示］把材料归为一组并给出对比摘要"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def _card(cid: str, kind: str, content: str, **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _seed(client: TestClient, board: str) -> None:
    cards = [
        _card("m1", "file", "材料一", meta={"name": "a.pdf"}),
        _card("m2", "file", "材料二", meta={"name": "b.pdf"}),
        _card("n1", "text", "说明", checked=True),
    ]
    state = {
        "boardId": board, "seq": 0, "updatedAt": models.now_iso(),
        "cards": cards, "groups": [], "links": [], "selection": [],
    }
    resp = client.put(f"/api/interactive/boards/{board}/state", json={"state": state, "reason": "seed"})
    assert resp.status_code == 200, resp.text
    created = client.post(f"/api/interactive/boards/{board}/intents", json={"demo": True})
    assert created.status_code == 200, created.text


def _board(client: TestClient, board: str) -> dict:
    resp = client.get(f"/api/interactive/boards/{board}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()["state"]


def _intents(client: TestClient, board: str) -> dict[str, dict]:
    resp = client.get(f"/api/interactive/boards/{board}/intents")
    assert resp.status_code == 200, resp.text
    return {item["title"]: item for item in resp.json()["intents"]}


def _approve(client: TestClient, intent_id: str, **body) -> dict:
    resp = client.post(f"/api/interactive/intents/{intent_id}/approve", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _edit(state: dict, card_id: str, **fields) -> dict:
    for card in state["cards"]:
        if card["id"] == card_id:
            card.update(fields)
    return state


def main() -> int:
    conn = connect(pathlib.Path(_TMP) / "probe16.db")
    apply_migrations(conn)
    settings = Settings(data_dir=pathlib.Path(_TMP) / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()

    with TestClient(app) as client:
        # ---------- A：真实保存接口改材料正文（不提交）→ 保存即失效 + approve 拒绝 ----------
        board = "board_e16_a"
        _seed(client, board)
        listed = _intents(client, board)
        check("16-A0 前置：separate 处于 pending", listed[SEPARATE]["status"] == "pending", listed[SEPARATE]["status"])
        state = _board(client, board)
        state = _edit(state, "m1", content="材料一（材料正文已替换）")
        saved = client.put(f"/api/interactive/boards/{board}/state", json={"state": state, "reason": "probe"})
        check("16-A1 改材料后保存成功", saved.status_code == 200, str(saved.status_code))
        after_save = _intents(client, board)[SEPARATE]
        check("16-A2 保存那一刻旧预览即失效（needs_update）", after_save["status"] == "needs_update", after_save["status"])
        decided = _approve(client, after_save["id"])
        check("16-A3 approve 必须 ok=False", decided.get("ok") is False, json.dumps(decided, ensure_ascii=False)[:200])
        check("16-A4 reason=needs_update", decided.get("reason") == "needs_update", str(decided.get("reason")))
        check("16-A5 拒绝原因里说明材料变化", "材料" in str(decided.get("detail", "")), str(decided.get("detail"))[:120])
        check("16-A6 状态保持 needs_update（未被批准）", _intents(client, board)[SEPARATE]["status"] == "needs_update")

        # ---------- B：绕过保存时标记（直接写库），只靠 approve 时复核 ----------
        board = "board_e16_b"
        _seed(client, board)
        listed = _intents(client, board)
        state = _board(client, board)
        state = _edit(state, "m1", content="材料一（绕过保存钩子直接写库）")
        board_store.save_board(conn, board, state, reason="probe-direct")
        still = _intents(client, board)[SEPARATE]
        check("16-B1 前置：直接写库确实绕过了保存时标记（仍是 pending）", still["status"] == "pending", still["status"])
        decided = _approve(client, still["id"])
        check("16-B2 approve 时服务端重新核算 → 仍必须拒绝", decided.get("ok") is False, json.dumps(decided, ensure_ascii=False)[:200])
        check("16-B3 reason=needs_update（复核路径）", decided.get("reason") == "needs_update", str(decided.get("reason")))

        # ---------- C：只改位置（真实接口）→ 不得失效 ----------
        board = "board_e16_c"
        _seed(client, board)
        listed = _intents(client, board)
        state = _board(client, board)
        state = _edit(state, "m1", x=333.0, y=999.0, w=222.0, h=111.0)
        saved = client.put(f"/api/interactive/boards/{board}/state", json={"state": state, "reason": "probe-pos"})
        check("16-C1 只改位置保存成功", saved.status_code == 200, str(saved.status_code))
        after = _intents(client, board)[COMBINE]
        check("16-C2 只改位置不判失效（仍 pending）", after["status"] == "pending", after["status"])
        decided = _approve(client, after["id"])
        check("16-C3 只改位置 approve 仍 ok=True", decided.get("ok") is True, json.dumps(decided, ensure_ascii=False)[:200])

        # ---------- D：只改位置（直接写库，只靠 approve 时复核）→ 不得失效 ----------
        board = "board_e16_d"
        _seed(client, board)
        listed = _intents(client, board)
        state = _board(client, board)
        state = _edit(state, "m1", x=12.5, y=7.5)
        board_store.save_board(conn, board, state, reason="probe-direct-pos")
        decided = _approve(client, listed[COMBINE]["id"])
        check("16-D1 只改位置（复核路径）approve 仍 ok=True", decided.get("ok") is True, json.dumps(decided, ensure_ascii=False)[:200])

        # ---------- E：材料被标记删除 → 必须失效 ----------
        board = "board_e16_e"
        _seed(client, board)
        listed = _intents(client, board)
        state = _board(client, board)
        state = _edit(state, "m1", deleted=True)
        saved = client.put(f"/api/interactive/boards/{board}/state", json={"state": state, "reason": "probe-delete"})
        check("16-E1 删除材料保存成功", saved.status_code == 200, str(saved.status_code))
        after = _intents(client, board)[SEPARATE]
        decided = _approve(client, after["id"])
        check("16-E2 材料被删 → approve 必须拒绝", decided.get("ok") is False and decided.get("reason") == "needs_update",
              f"status={after['status']} reason={decided.get('reason')}")

    conn.close()
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(("PASS  " if ok else "FAIL  ") + name + ("   | " + detail if detail else ""))
    print(json.dumps({"tmp": _TMP, "total": len(RESULTS), "failed": len(failed)}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
