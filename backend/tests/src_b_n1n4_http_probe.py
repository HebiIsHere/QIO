"""【子智能体 B · 第④层真实 HTTP 证据】：uvicorn + 真实临时 QIO_DATA_DIR。

覆盖：
- N1：真实 HTTP 上的乱序写入（第二版先落地、迟到的第一版必须 409 stale_state，不覆盖、不推进快照）、
  缺失版本 409、用最新版本重试 200、带 confirm 的保存同样先过版本门；
- N4：真实 HTTP 上的 revert_rest + decisionIds：等待期间的新正文先保留并重新说明，
  用户按新说明再决定后才真正撤回。

用法（后端必须先起在 --base 指定的地址，且 QIO_DATA_DIR 与 --data-dir 一致）：

    uv run --frozen uvicorn agent.main:create_app --factory --host 127.0.0.1 --port 8877

    backend\.venv\Scripts\python.exe backend\tests\src_b_n1n4_http_probe.py \
        --base http://127.0.0.1:8877 --data-dir <同一个临时数据目录>

退出码 0 = 全部通过；1 = 至少一条不符。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

BOARD = "board_src_b_http_probe"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" | {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def call(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:  # noqa: PERF203 - 错误体也要按真实内容核对
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def detail_of(body: dict) -> dict:
    value = body.get("detail")
    return value if isinstance(value, dict) else {}


def stored_board(db_path: Path) -> tuple[int, dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT seq, state FROM board_states WHERE board_id = ?", (BOARD,)
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) FROM board_state_snapshots WHERE board_id = ?", (BOARD,)
        ).fetchone()[0]
    finally:
        conn.close()
    return (int(row[0]), json.loads(row[1]), int(count)) if row else (0, {}, int(count))


def stored_intent_revert(db_path: Path, intent_id: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT revert, reason FROM board_intents WHERE id = ?", (intent_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    payload = json.loads(row[0] or "{}")
    payload["__reason"] = row[1]
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    data_dir = Path(args.data_dir).resolve()
    db_path = data_dir / "app.db"
    check("0 真实临时数据库存在", db_path.exists(), str(db_path))
    if not db_path.exists():
        return 1

    # ---- N1：乱序写入 -----------------------------------------------------
    status, created = call(
        base, "POST", "/api/interactive/boards", {"boardId": BOARD, "title": "B 证据板"}
    )
    check("N1.1 新建板面", status == 200, f"status={status}")

    seed = {
        "boardId": BOARD,
        "seq": 0,
        "cards": [{"id": "n1", "kind": "text", "content": "初始", "checked": False}],
        "groups": [],
        "links": [],
        "selection": [],
    }
    status_seed, seeded = call(
        base, "PUT", f"/api/interactive/boards/{BOARD}/state", {"state": seed, "reason": "seed"}
    )
    check("N1.2 新板第一次保存（seq=0）成功", status_seed == 200, f"status={status_seed}")

    status_meta, meta = call(base, "GET", f"/api/interactive/boards/{BOARD}/state")
    seq_base = int(meta.get("seq") or 0)
    check("N1.3 读到已保存版本", status_meta == 200 and seq_base == 1, f"seq={seq_base}")

    def candidate(content: str, seq: int) -> dict:
        state = json.loads(json.dumps(meta["state"]))
        state["seq"] = seq
        state["cards"] = [{**c, "content": content} if c["id"] == "n1" else c for c in state["cards"]]
        return state

    first = candidate("第一版（迟到）", seq_base)
    second = candidate("第二版（新正文）", seq_base)

    status_b, saved_b = call(
        base, "PUT", f"/api/interactive/boards/{BOARD}/state", {"state": second, "reason": "r3-second"}
    )
    check("N1.4 第二版先保存成功", status_b == 200 and int(saved_b.get("seq") or 0) == seq_base + 1,
          f"status={status_b} seq={saved_b.get('seq')}")

    snapshots_before = stored_board(db_path)[2]
    status_a, late = call(
        base, "PUT", f"/api/interactive/boards/{BOARD}/state", {"state": first, "reason": "r3-first-late"}
    )
    late_detail = detail_of(late)
    check("N1.5 迟到的第一版被 409 拒绝", status_a == 409, f"status={status_a} body={json.dumps(late, ensure_ascii=False)[:160]}")
    check("N1.6 错误形状 stale_state + currentSeq", late_detail.get("error") == "stale_state"
          and late_detail.get("currentSeq") == seq_base + 1
          and bool(str(late_detail.get("reason") or "").strip()),
          json.dumps(late_detail, ensure_ascii=False)[:200])

    seq_stored, state_stored, snapshots_after = stored_board(db_path)
    contents = [c.get("content") for c in state_stored.get("cards") or [] if c.get("id") == "n1"]
    check("N1.7 库里仍是第二版（未被覆盖）", seq_stored == seq_base + 1 and contents == ["第二版（新正文）"],
          f"seq={seq_stored} contents={contents}")
    check("N1.8 被拒绝的写入没有推进快照", snapshots_after == snapshots_before,
          f"{snapshots_before} -> {snapshots_after}")

    no_seq = candidate("没有版本声明", seq_base)
    del no_seq["seq"]
    status_noseq, body_noseq = call(
        base, "PUT", f"/api/interactive/boards/{BOARD}/state", {"state": no_seq, "reason": "no-seq"}
    )
    check("N1.9 缺失版本被 409 拒绝", status_noseq == 409
          and detail_of(body_noseq).get("error") == "stale_state",
          f"status={status_noseq}")

    retry = candidate("用最新版本重试", seq_stored)
    status_retry, saved_retry = call(
        base, "PUT", f"/api/interactive/boards/{BOARD}/state", {"state": retry, "reason": "retry"}
    )
    check("N1.10 用最新版本重试成功", status_retry == 200 and int(saved_retry.get("seq") or 0) == seq_stored + 1,
          f"status={status_retry} seq={saved_retry.get('seq')}")

    # ---- N1：带 confirm 的保存同样先过版本门（真实 M4 链路） ----------------
    materials = {
        "boardId": BOARD,
        "seq": 0,
        "cards": [
            {"id": "m1", "kind": "file", "content": "材料一", "meta": {"name": "a.pdf"}},
            {"id": "m2", "kind": "file", "content": "材料二", "meta": {"name": "b.pdf"}},
            {"id": "n1", "kind": "text", "content": "整理说明", "checked": True},
        ],
        "groups": [],
        "links": [],
        "selection": [],
    }
    _, meta2 = call(base, "GET", f"/api/interactive/boards/{BOARD}/state")
    materials["seq"] = int(meta2["seq"])
    status_seed2, _ = call(
        base, "PUT", f"/api/interactive/boards/{BOARD}/state", {"state": materials, "reason": "materials"}
    )
    check("N1.11 材料种子保存成功", status_seed2 == 200, f"status={status_seed2}")

    _, created_intents = call(base, "POST", f"/api/interactive/boards/{BOARD}/intents", {"demo": True})
    combine = next(
        (item for item in created_intents.get("created") or [] if "归为一组" in str(item.get("title"))),
        None,
    )
    check("N1.12 演示意图已创建", combine is not None)
    if combine is None:
        return 1
    status_approve, approved = call(
        base, "POST", f"/api/interactive/intents/{combine['id']}/approve", {}
    )
    check("N1.13 任务进入执行中",
          status_approve == 200 and approved.get("intent", {}).get("status") == "running",
          f"status={status_approve}")
    print(f"       combine_intent_id={combine['id']}")

    _, meta3 = call(base, "GET", f"/api/interactive/boards/{BOARD}/state")
    base_seq = int(meta3["seq"])
    material_candidate = json.loads(json.dumps(meta3["state"]))
    material_candidate["cards"] = [
        {**c, "content": "材料一（替换版）"} if c["id"] == "m1" else c
        for c in material_candidate["cards"]
    ]
    _, check_body = call(
        base,
        "POST",
        f"/api/interactive/boards/{BOARD}/impact-check",
        {"stateVersion": base_seq, "changeSet": {"state": material_candidate}},
    )
    check("N1.14 影响预判给出 checkId", bool(check_body.get("checkId")), json.dumps(check_body, ensure_ascii=False)[:160])

    later = json.loads(json.dumps(meta3["state"]))
    later["cards"] = [
        {**c, "x": float(c.get("x") or 0) + 10} if c["id"] == "n1" else c for c in later["cards"]
    ]
    status_later, saved_later = call(
        base, "PUT", f"/api/interactive/boards/{BOARD}/state", {"state": later, "reason": "wait-edit"}
    )
    check("N1.15 等待确认期间板面又保存了一版", status_later == 200, f"status={status_later}")

    snapshots_before2 = stored_board(db_path)[2]
    status_confirm, confirm_body = call(
        base,
        "PUT",
        f"/api/interactive/boards/{BOARD}/state",
        {
            "state": material_candidate,
            "reason": "late-confirm",
            "confirm": {"checkId": check_body.get("checkId"), "stateVersion": base_seq},
        },
    )
    confirm_detail = detail_of(confirm_body)
    check("N1.16 过期版本的确认被拒（stale_state，不是绕过）",
          status_confirm == 409 and confirm_detail.get("error") == "stale_state",
          json.dumps(confirm_detail, ensure_ascii=False)[:200])
    _, state_after_confirm, snapshots_after2 = stored_board(db_path)
    m1_contents = [c.get("content") for c in state_after_confirm.get("cards") or [] if c.get("id") == "m1"]
    check("N1.17 被拒的确认没有落库、没有推进快照",
          m1_contents == ["材料一"] and snapshots_after2 == snapshots_before2,
          f"m1={m1_contents} snapshots={snapshots_before2}->{snapshots_after2}")

    # ---- N4：revert_rest + decisionIds 的真实 HTTP 行为 ---------------------
    _, created2 = call(base, "POST", f"/api/interactive/boards/{BOARD}/intents", {"demo": True})
    failing = next(
        (item for item in created2.get("created") or [] if "可执行清单" in str(item.get("title"))),
        None,
    )
    check("N4.1 找到失败演示意图", failing is not None)
    if failing is None:
        return 1
    status_fa, _ = call(base, "POST", f"/api/interactive/intents/{failing['id']}/approve", {})
    _, done = call(
        base, "POST", f"/api/interactive/intents/{failing['id']}/demo/advance", {"outcome": "done"}
    )
    applied = (done.get("intent") or {}).get("applied") or {}
    card_id = (applied.get("cardIds") or [None])[0]
    check("N4.2 结果卡片已落地", status_fa == 200 and bool(card_id), f"card={card_id}")
    if not card_id:
        return 1

    _, live = call(base, "GET", f"/api/interactive/boards/{BOARD}/state")
    with_link = json.loads(json.dumps(live["state"]))
    with_link["links"] = (with_link.get("links") or []) + [
        {"id": "l_user", "src": card_id, "dst": "n1", "direction": False, "meaning": "我的依据", "deleted": False}
    ]
    status_link, _ = call(
        base, "PUT", f"/api/interactive/boards/{BOARD}/state", {"state": with_link, "reason": "user-link"}
    )
    check("N4.3 用户关系保存成功", status_link == 200, f"status={status_link}")

    _, failed = call(
        base, "POST", f"/api/interactive/intents/{failing['id']}/demo/advance", {"outcome": "failed"}
    )
    pending = ((failed.get("revert") or {}).get("pendingDecision")) or []
    check("N4.4 失败撤回留下待决定项", [p.get("id") for p in pending] == [card_id],
          json.dumps(failed.get("revert"), ensure_ascii=False)[:200])
    check("N4.5 待决定项带复核依据（signature / impacts）",
          all(p.get("signature") and "impacts" in p for p in pending),
          json.dumps(pending, ensure_ascii=False)[:200])

    # 等待期间用户又写了新正文
    _, live2 = call(base, "GET", f"/api/interactive/boards/{BOARD}/state")
    edited = json.loads(json.dumps(live2["state"]))
    edited["cards"] = [
        {**c, "content": "用户在等待期间写的新正文"} if c["id"] == card_id else c
        for c in edited["cards"]
    ]
    status_edit, _ = call(
        base, "PUT", f"/api/interactive/boards/{BOARD}/state", {"state": edited, "reason": "user-edit"}
    )
    check("N4.6 等待期间的新正文保存成功", status_edit == 200, f"status={status_edit}")

    _, decided = call(
        base,
        "POST",
        f"/api/interactive/intents/{failing['id']}/demo/advance",
        {"outcome": "revert_rest", "decisionIds": [card_id]},
    )
    decision = decided.get("decision") or {}
    check("N4.7 旧决定没有删除新正文（保留 + 重新说明）",
          decided.get("ok") is True and decision.get("processed") == []
          and decision.get("reconfirmed") == [card_id],
          json.dumps(decision, ensure_ascii=False)[:200])
    _, state_now, _ = stored_board(db_path)
    kept_card = next((c for c in state_now.get("cards") or [] if c.get("id") == card_id), None)
    check("N4.8 库里仍然保留用户的新正文",
          kept_card is not None and kept_card.get("deleted") is False
          and kept_card.get("content") == "用户在等待期间写的新正文",
          json.dumps(kept_card, ensure_ascii=False)[:200])
    refreshed = ((decided.get("revert") or {}).get("pendingDecision")) or []
    check("N4.9 刷新后的说明放回待决定清单",
          [p.get("id") for p in refreshed] == [card_id] and "改过" in str(refreshed[0].get("reason")),
          json.dumps(refreshed, ensure_ascii=False)[:200])
    stored_revert = stored_intent_revert(db_path, failing["id"])
    check("N4.10 存储的报告与响应一致（真正落库）",
          [p.get("id") for p in stored_revert.get("pendingDecision") or []] == [card_id],
          json.dumps(stored_revert, ensure_ascii=False)[:200])

    _, again = call(
        base,
        "POST",
        f"/api/interactive/intents/{failing['id']}/demo/advance",
        {"outcome": "revert_rest", "decisionIds": [card_id]},
    )
    check("N4.11 用户按新说明再决定后才真正撤回",
          (again.get("decision") or {}).get("processed") == [card_id],
          json.dumps(again.get("decision"), ensure_ascii=False)[:200])
    _, state_after, _ = stored_board(db_path)
    gone_card = next((c for c in state_after.get("cards") or [] if c.get("id") == card_id), None)
    check("N4.12 库里核对：这次真的被撤回",
          gone_card is not None and gone_card.get("deleted") is True,
          json.dumps(gone_card, ensure_ascii=False)[:160])

    print("")
    if FAILURES:
        print(f"结论：失败 {len(FAILURES)} 条 -> " + "；".join(FAILURES))
        return 1
    print("结论：第④层真实 HTTP 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
