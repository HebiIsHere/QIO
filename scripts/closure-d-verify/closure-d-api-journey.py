"""【独立验收 D · closure-d】第④层证据：真实前后端进程上的 HTTP 旅程（临时数据目录 + 真实 sqlite）。

跑法（服务需已用显式临时 QIO_DATA_DIR 启动；脚本自己核对数据库路径）：
    backend/.venv/Scripts/python.exe scripts/closure-d-verify/closure-d-api-journey.py \
        --base http://127.0.0.1:8734 --data-dir <临时目录>

覆盖：
  R3：第一版保存后 seq 前进；第二版必须以服务器新 seq 继续保存并回读落地（真实 sqlite 里核对）；
  R5：无 checkId 的保存被服务端门 409 拒绝且不落库；过期 checkId 409 stale_check 且不落库；
      重新预判拿新 checkId 后保存 200；
  R6：服务端说明包含被影响的执行中任务；确认落库后直接 SELECT board_intents.status == 'paused'。

输出：每条检查打印 PASS/FAIL + 真实数字；退出码非 0 表示有 FAIL。
所有路径均为仓库相对路径解析，不写死盘符。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []
STAMP = "2026-10-10T00:00:00Z"


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + " :: " + detail, flush=True)


def call(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8") or "{}"
        try:
            return err.code, json.loads(raw)
        except json.JSONDecodeError:
            return err.code, {"raw": raw}


def detail_of(body: dict) -> dict:
    value = body.get("detail")
    return value if isinstance(value, dict) else {}


def card(cid: str, kind: str, content: str, checked: bool, x: float = 0.0) -> dict:
    return {
        "id": cid, "kind": kind, "content": content, "meta": {"name": cid + ".pdf"},
        "x": x, "y": 0.0, "w": 200.0, "h": 120.0,
        "checked": checked, "hidden": False, "folded": False, "bookmarked": False,
        "deleted": False, "createdAt": STAMP, "updatedAt": STAMP,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8734")
    parser.add_argument("--data-dir", required=True, help="服务进程实际使用的 QIO_DATA_DIR（必须与启动时一致）")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    db_path = (Path(args.data_dir).resolve() / "app.db")
    check("0 数据库路径存在（验收在临时数据目录上）", db_path.exists(), str(db_path))
    if not db_path.exists():
        return 1

    boards = call(base, "GET", "/api/interactive/boards")[1]
    board_id = (boards.get("boards") or [{}])[0].get("id")
    check("1 取到板面 id", bool(board_id), json.dumps(boards, ensure_ascii=False)[:120])
    if not board_id:
        return 1
    prefix = "/api/interactive/boards/" + board_id

    # ---- 种子：材料 m1/m2 + 勾选注释 n1（一次保存，之后不再重排板面） ----
    seed = {"boardId": board_id, "seq": 0, "updatedAt": STAMP,
            "cards": [card("m1", "file", "材料一", True), card("m2", "file", "材料二", True, x=220.0),
                      card("n1", "text", "整理说明", True, x=440.0)],
            "groups": [], "links": [], "selection": []}
    status_seed, seeded = call(base, "PUT", prefix + "/state", {"state": seed, "reason": "seed"})
    check("2 材料种子保存成功", status_seed == 200, "status=" + str(status_seed))
    seq_seed = int(seeded.get("seq") or 0)

    # ---- R3：在既有板面上保存两个版本，第二版用服务器返回的新 seq ----
    first_state = json.loads(json.dumps(seeded.get("state") or {}))
    first_state["seq"] = seq_seed
    first_state["cards"] = [c for c in first_state["cards"] if c.get("id") != "n1"] + [
        card("n1", "text", "第一版", True, x=440.0)
    ]
    status_first, first = call(base, "PUT", prefix + "/state", {"state": first_state, "reason": "r3-first"})
    check("R3.1 第一版保存成功", status_first == 200, "status=" + str(status_first))
    seq_after_first = int(first.get("seq") or 0)
    check("R3.2 保存回执给出前进后的 seq", seq_after_first > seq_seed, str(seq_seed) + " -> " + str(seq_after_first))

    second_state = json.loads(json.dumps(first.get("state") or {}))
    second_state["seq"] = seq_after_first
    second_state["cards"] = [{**c, "content": "第二版"} if c.get("id") == "n1" else c for c in second_state["cards"]]
    status_second, second = call(base, "PUT", prefix + "/state", {"state": second_state, "reason": "r3-second"})
    check("R3.3 第二版用新 seq 继续保存成功", status_second == 200, "status=" + str(status_second))
    seq_after_second = int(second.get("seq") or 0)
    check("R3.4 第二版让 seq 继续前进", seq_after_second > seq_after_first, str(seq_after_first) + " -> " + str(seq_after_second))

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT seq, state FROM board_states WHERE board_id = ? ORDER BY seq DESC LIMIT 1", (board_id,)).fetchone()
    finally:
        conn.close()
    landed = json.loads(row[1]) if row else {}
    contents = [c.get("content") for c in (landed.get("cards") or []) if c.get("id") == "n1"]
    check("R3.5 库里落地的是第二版", contents == ["第二版"], "seq=" + str(row[0] if row else None) + " contents=" + repr(contents))

    # ---- 创建并批准演示任务（材料 m1/m2 仍在板面上，预览依据有效） ----
    _, created = call(base, "POST", prefix + "/intents", {"demo": True})
    listed = call(base, "GET", prefix + "/intents")[1].get("intents") or []
    combine = next((item for item in listed if "归为一组" in str(item.get("title"))), None)
    check("R6.1 演示任务已创建", combine is not None, "created=" + str(len(created.get("created") or [])))
    if combine is None:
        return 1
    status_approve, approved = call(base, "POST", "/api/interactive/intents/" + combine["id"] + "/approve", {})
    check("R6.2 任务批准后进入执行中", approved.get("ok") is True and approved.get("intent", {}).get("status") == "running",
          "status=" + str(status_approve) + " body=" + json.dumps(approved, ensure_ascii=False)[:140])

    live = call(base, "GET", prefix + "/state")[1]
    seq_live = int(live.get("seq") or 0)
    candidate = json.loads(json.dumps(live.get("state") or {}))
    candidate["cards"] = [{**c, "content": "材料一（替换版）"} if c.get("id") == "m1" else c for c in candidate["cards"]]

    status_gate, gate = call(base, "PUT", prefix + "/state", {"state": candidate, "reason": "material"})
    gate_detail = detail_of(gate)
    check("R5.1 不带确认改材料被服务端门拒绝",
          status_gate == 409 and gate_detail.get("error") == "impact_confirmation_required",
          "status=" + str(status_gate) + " detail=" + json.dumps(gate_detail, ensure_ascii=False)[:160])
    affected_ids = [item.get("intentId") for item in gate_detail.get("affectedTasks") or []]
    check("R5.2 门里如实列出受影响任务（R6 的完整列表来源）", combine["id"] in affected_ids, repr(affected_ids))
    after_gate = call(base, "GET", prefix + "/state")[1]
    check("R5.3 被拒绝的保存没有落库", int(after_gate.get("seq") or 0) == seq_live,
          str(seq_live) + " -> " + str(after_gate.get("seq")))

    check_resp = call(base, "POST", prefix + "/impact-check", {"stateVersion": seq_live, "changeSet": {"state": candidate}})[1]
    check("R5.4 影响预判给出 checkId 与完整受影响列表",
          check_resp.get("ok") is True and bool(check_resp.get("checkId")) and
          combine["id"] in [item.get("intentId") for item in check_resp.get("affectedTasks") or []],
          json.dumps({k: check_resp.get(k) for k in ("ok", "checkId", "stateVersion", "impactConfirmationRequired")}, ensure_ascii=False))
    if not check_resp.get("checkId"):
        print("---- 预判失败，后续 R5/R6 无法继续 ----", flush=True)
        return 1

    # 让版本前进（只改位置），旧 checkId 随即失效 → 必须 409 stale_check 且不落库
    bumped = json.loads(json.dumps(after_gate.get("state") or {}))
    bumped["cards"] = [{**c, "x": float(c.get("x") or 0) + 10.0} if c.get("id") == "n1" else c for c in bumped["cards"]]
    status_bump, bump_resp = call(base, "PUT", prefix + "/state", {"state": bumped, "reason": "bump"})
    seq_bumped = int(bump_resp.get("seq") or 0)
    check("R5.5 位置微调保存成功（seq 前进）", status_bump == 200 and seq_bumped > seq_live, str(seq_live) + " -> " + str(seq_bumped))

    status_stale, stale = call(base, "PUT", prefix + "/state",
                               {"state": candidate, "reason": "stale",
                                "confirm": {"checkId": check_resp["checkId"], "stateVersion": check_resp.get("stateVersion")}})
    check("R5.6 过期 checkId 确认被拒 stale_check",
          status_stale == 409 and detail_of(stale).get("error") == "stale_check",
          "status=" + str(status_stale) + " detail=" + json.dumps(detail_of(stale), ensure_ascii=False)[:160])
    after_stale = call(base, "GET", prefix + "/state")[1]
    check("R5.7 过期确认没有落库", int(after_stale.get("seq") or 0) == seq_bumped,
          str(seq_bumped) + " -> " + str(after_stale.get("seq")))

    fresh = call(base, "POST", prefix + "/impact-check", {"stateVersion": seq_bumped, "changeSet": {"state": candidate}})[1]
    status_ok, saved = call(base, "PUT", prefix + "/state",
                            {"state": candidate, "reason": "confirmed",
                             "confirm": {"checkId": fresh.get("checkId"), "stateVersion": fresh.get("stateVersion")}})
    check("R5.8 用新 checkId 确认后保存成功", status_ok == 200, "status=" + str(status_ok))
    paused = [item.get("intentId") for item in (saved.get("materialImpact") or {}).get("paused") or []]
    check("R6.3 保存响应如实报告被暂停的任务", combine["id"] in paused, repr(paused))

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT status, progress FROM board_intents WHERE id = ?", (combine["id"],)).fetchone()
    finally:
        conn.close()
    check("R6.4 库里该任务真的是 paused 且保留进度",
          bool(row) and row[0] == "paused" and bool(json.loads(row[1] or "{}")),
          "status=" + str(row[0] if row else None) + " progress=" + str(row[1] if row else None)[:80])

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("---- 汇总：" + str(len(RESULTS) - len(failed)) + "/" + str(len(RESULTS)) + " 通过 ----", flush=True)
    if failed:
        print("FAIL 项：" + "；".join(failed), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
