"""第二批独立对抗性探针 · 08：服务端影响门与提交版本（真实 sqlite 临时库）。

跑法（必须在临时数据目录里跑；脚本内部也会再设一次环境变量）：
    $env:QIO_DATA_DIR = <临时目录>
    backend/.venv/Scripts/python.exe scripts/final-verify-subagent/batch2/probe08_impact_gate.py

覆盖（逐条对照收尾项 08 的 M4 契约）：
  1) 改动执行中任务依赖的材料、**不带 confirm** → 409 impact_confirmation_required，
     且 seq 不变、材料正文在库里没有被改动；
  2) impact-check 拿到 checkId 后，先用一次**无关保存**把板面版本推进，
     再用**过期版本**的 checkId 保存同一份材料改动 → 409 stale_check，且不落库；
  3) 提交带**过期 baseStateVersion** → 409 stale_state，且不产生成功提交记录、基准不变。

说明：脚本不使用开发者写好的 pytest 夹具；自己在临时目录建真实 sqlite 库、
跑真实迁移、用 Starlette/FastAPI 的完整 ASGI 路由（TestClient）发起请求，
并**直接读 sqlite 文件**核对落库事实。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3] / "backend"
sys.path.insert(0, str(BACKEND / "src"))

# 必须在 import agent.config 之前设置：QIO_DATA_DIR 会被 Settings 无条件采用。
_TMP = Path(tempfile.mkdtemp(prefix="qio-b2-probe08-"))
os.environ["QIO_DATA_DIR"] = str(_TMP)
os.environ["QIO_DEV_INSECURE"] = "1"
os.environ["QIO_DISABLE_DB_CHECK"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from agent.api.server import create_app  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.credentials.store import MemoryKeyring  # noqa: E402
from agent.interactive import models  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402

BOARD = "board_b2_probe08"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))


def _card(cid: str, kind: str, content: str = "", **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _state(client: TestClient) -> dict:
    resp = client.get(f"/api/interactive/boards/{BOARD}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _board(client: TestClient) -> dict:
    return _state(client)["state"]


def _save(client: TestClient, state: dict, **extra):
    return client.put(f"/api/interactive/boards/{BOARD}/state", json={"state": state, "reason": "probe08", **extra})


def _intents(client: TestClient) -> dict[str, dict]:
    listed = client.get(f"/api/interactive/boards/{BOARD}/intents")
    assert listed.status_code == 200, listed.text
    return {item["title"]: item for item in listed.json()["intents"]}


def _db_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT seq FROM board_states WHERE board_id = ?", (BOARD,)).fetchone()
    return int(row["seq"])


def _db_state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT state FROM board_states WHERE board_id = ?", (BOARD,)).fetchone()
    return json.loads(row["state"])


def _db_card_content(conn: sqlite3.Connection, card_id: str) -> str:
    for card in _db_state(conn).get("cards") or []:
        if str(card.get("id")) == card_id:
            return str(card.get("content") or "")
    return "<缺失>"


def _db_snapshot_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) c FROM board_state_snapshots WHERE board_id = ?", (BOARD,)).fetchone()["c"])


def _db_succeeded_submissions(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) c FROM board_submissions WHERE board_id = ? AND status = 'succeeded'", (BOARD,)
        ).fetchone()["c"]
    )


def _db_submission_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) c FROM board_submissions WHERE board_id = ?", (BOARD,)).fetchone()["c"])


def main() -> int:
    db_path = _TMP / "probe08.db"
    conn = connect(db_path)
    apply_migrations(conn)
    settings = Settings(data_dir=_TMP)
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()

    check("0 真实 sqlite 临时库已建立", db_path.exists(), "db=" + str(db_path))
    print("[INFO] 临时数据目录 = " + str(_TMP))

    with TestClient(app) as client:
        # --- 播种：m1/m2 两份材料 + 一条注释；演示 combine 依赖材料并进入 running ---
        stamp = models.now_iso()
        state = {
            "boardId": BOARD, "seq": 0, "updatedAt": stamp,
            "cards": [
                _card("m1", "file", "材料一", meta={"name": "a.pdf"}),
                _card("m2", "file", "材料二", meta={"name": "b.pdf"}),
                _card("n1", "text", "整理说明", checked=True),
            ],
            "groups": [], "links": [], "selection": [],
        }
        seeded = _save(client, state)
        check("1 播种保存 200", seeded.status_code == 200, "status=" + str(seeded.status_code))
        client.post(f"/api/interactive/boards/{BOARD}/intents", json={"demo": True})
        combine = _intents(client)["［演示］把材料归为一组并给出对比摘要"]
        approved = client.post(f"/api/interactive/intents/{combine['id']}/approve", json={}).json()
        check("2 演示任务批准并进入 running", approved.get("ok") is True and _intents(client)["［演示］把材料归为一组并给出对比摘要"]["status"] == "running",
              json.dumps(approved, ensure_ascii=False)[:160])

        seq_before = _state(client)["seq"]
        snap_before = _db_snapshot_count(conn)
        cand = _board(client)
        for card in cand["cards"]:
            if card["id"] == "m1":
                card["content"] = "材料一（替换）"

        # --- 检查一：不带 confirm 改材料 → 409 impact_confirmation_required 且不落库 ---
        r1 = _save(client, cand)
        d1 = r1.json().get("detail") if isinstance(r1.json(), dict) else None
        check("3 不带确认改材料被服务端门拒绝（409 impact_confirmation_required）",
              r1.status_code == 409 and isinstance(d1, dict) and d1.get("error") == "impact_confirmation_required",
              "status=" + str(r1.status_code) + " body=" + json.dumps(r1.json(), ensure_ascii=False)[:200])
        check("4 被拒绝的保存没有推进 seq（内存视图）", _state(client)["seq"] == seq_before,
              "seq " + str(seq_before) + " -> " + str(_state(client)["seq"]))
        check("5 被拒绝的保存没有推进 seq（直接读 sqlite）", _db_seq(conn) == seq_before,
              "db seq=" + str(_db_seq(conn)))
        check("6 被拒绝的保存没有改动库里的材料正文", _db_card_content(conn, "m1") == "材料一",
              "db m1=" + _db_card_content(conn, "m1"))
        check("7 被拒绝的保存没有新增状态快照", _db_snapshot_count(conn) == snap_before,
              "snapshots " + str(snap_before) + " -> " + str(_db_snapshot_count(conn)))

        # --- 检查二：impact-check 的 checkId 过期后不许放行 ---
        chk = client.post(
            f"/api/interactive/boards/{BOARD}/impact-check",
            json={"stateVersion": seq_before, "changeSet": {"state": cand}},
        )
        chk_body = chk.json()
        check("8 影响预判给出 checkId 且要求确认",
              chk.status_code == 200 and bool(chk_body.get("checkId")) and chk_body.get("impactConfirmationRequired") is True,
              "status=" + str(chk.status_code) + " body=" + json.dumps(chk_body, ensure_ascii=False)[:200])
        check_id = chk_body.get("checkId")

        # 先做一次**无关保存**（只改注释 n1），把板面版本推进，让 checkId 过期
        unrelated = _board(client)
        for card in unrelated["cards"]:
            if card["id"] == "n1":
                card["content"] = "整理说明（无关改动）"
        r_unrelated = _save(client, unrelated)
        seq_after_unrelated = _state(client)["seq"]
        check("9 无关保存（不动材料）正常落库并推进版本",
              r_unrelated.status_code == 200 and seq_after_unrelated != seq_before,
              "status=" + str(r_unrelated.status_code) + " seq " + str(seq_before) + " -> " + str(seq_after_unrelated))

        sub_before = _db_snapshot_count(conn)
        r2 = _save(client, cand, confirm={"checkId": check_id, "stateVersion": seq_before})
        d2 = r2.json().get("detail") if isinstance(r2.json(), dict) else None
        check("10 过期 checkId 的确认保存被拒（409 stale_check）",
              r2.status_code == 409 and isinstance(d2, dict) and d2.get("error") == "stale_check",
              "status=" + str(r2.status_code) + " body=" + json.dumps(r2.json(), ensure_ascii=False)[:200])
        check("11 stale_check 没有落库（seq 不变，直接读 sqlite）",
              _db_seq(conn) == seq_after_unrelated and _state(client)["seq"] == seq_after_unrelated,
              "db seq=" + str(_db_seq(conn)) + " expect=" + str(seq_after_unrelated))
        check("12 stale_check 没有改动材料正文（直接读 sqlite）", _db_card_content(conn, "m1") == "材料一",
              "db m1=" + _db_card_content(conn, "m1"))
        check("13 stale_check 没有新增状态快照", _db_snapshot_count(conn) == sub_before,
              "snapshots " + str(sub_before) + " -> " + str(_db_snapshot_count(conn)))

        # --- 检查三：提交带过期 baseStateVersion → 409 stale_state，且不产生成功提交记录 ---
        current_seq = _state(client)["seq"]
        succeeded_before = _db_succeeded_submissions(conn)
        subs_before = _db_submission_count(conn)
        stale_submit = client.post(
            f"/api/interactive/boards/{BOARD}/submissions", json={"baseStateVersion": 1}
        )
        sd = stale_submit.json().get("detail") if isinstance(stale_submit.json(), dict) else None
        check("14 过期 baseStateVersion 的提交被拒（409 stale_state）",
              stale_submit.status_code == 409 and isinstance(sd, dict) and sd.get("error") == "stale_state",
              "status=" + str(stale_submit.status_code) + " body=" + json.dumps(stale_submit.json(), ensure_ascii=False)[:200])
        check("15 被拒提交不产生**成功**提交记录",
              _db_succeeded_submissions(conn) == succeeded_before,
              "succeeded " + str(succeeded_before) + " -> " + str(_db_succeeded_submissions(conn)))
        check("16 被拒提交不新增任何提交记录",
              _db_submission_count(conn) == subs_before,
              "submissions " + str(subs_before) + " -> " + str(_db_submission_count(conn)))
        listed = client.get(f"/api/interactive/boards/{BOARD}/submissions").json()["submissions"]
        check("17 接口列出的提交里没有 succeeded",
              all(item.get("status") != "succeeded" for item in listed),
              "listed=" + json.dumps(listed, ensure_ascii=False)[:160])
        check("18 被拒提交没有改变已保存板面版本", _db_seq(conn) == current_seq,
              "db seq=" + str(_db_seq(conn)) + " expect=" + str(current_seq))

        # 对照组：当前版本的提交能被正常处理（证明第 14 条的 409 不是「提交接口整体坏了」）
        ok_submit = client.post(
            f"/api/interactive/boards/{BOARD}/submissions", json={"baseStateVersion": current_seq}
        )
        ok_body = ok_submit.json()
        check("19 对照组：当前版本提交可正常处理",
              ok_submit.status_code == 200 and ok_body.get("status") in ("succeeded", "empty", "duplicate"),
              "status=" + str(ok_submit.status_code) + " body=" + json.dumps(ok_body, ensure_ascii=False)[:200])
        check("20 对照组不伪造 delivery.delivered",
              ok_body.get("delivery", {}).get("delivered") is not True,
              json.dumps(ok_body.get("delivery"), ensure_ascii=False)[:160])

    conn.close()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        print(("PASS " if ok else "FAIL ") + name + (("  " + detail) if not ok else ""))
    print("")
    print("结果：" + str(passed) + "/" + str(len(RESULTS)) + " 通过")
    print("临时库：" + str(db_path))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
