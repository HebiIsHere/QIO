"""独立验收：稳定提交身份链路（契约 §1，E 在第二段新增的验收项）。

不看任何人的自述：自己建库、自己起应用、自己注入 fake provider、自己数数据库行与模型调用次数。

覆盖（每一条都是契约 §1.2 / §1.3 的红线）：
  S1 同身份重试：原样返回原事实（replayed），不落新提交、不建新理解请求、不再调用模型；
  S2 同身份但 note 不同：409 request_identity_conflict，且不落新提交、不调用模型；
  S3 同身份但 baseStateVersion 不同：同样 409；
  S4 查证只认精确匹配：命中返回自己的事实；
  S5 「最近一条」诱饵：另一条更新的提交存在时，未用过的身份必须 404；
  S6 「时间接近」诱饵：紧接着提交后查一个从未用过的身份，必须 404；
  S7 数据库事实：client_request_id 唯一、理解请求数与提交数一一对应。

用法（worktree 根目录）：
  backend/.venv/Scripts/python.exe scripts/irm-int-verify/verify_submission_identity.py \
      [--json out/verify-identity-<sha>.json]

退出码 0 = 全部通过。脚本只用自己的临时库与 fake provider：不联网、不读密钥、不产生模型费用。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")
os.environ.pop("QIO_DATA_DIR", None)

ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from fastapi.testclient import TestClient  # noqa: E402

from agent.adapters.fake import FakeStreamAdapter, StreamScript  # noqa: E402
from agent.api.server import create_app  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.credentials.store import MemoryKeyring  # noqa: E402
from agent.interactive import models  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402

import logging  # noqa: E402

# 迁移的半截自愈会在 warning 级打印「语句已应用过」；这里只关心业务断言
logging.getLogger("agent.storage.migrate").setLevel(logging.ERROR)

BOARD = models.DEFAULT_BOARD_ID
RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)})
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ((" :: " + str(detail)) if detail else ""))
    return bool(ok)


class Provider:
    """所有模型调用共用一个脚本化 adapter，并计数。"""

    def __init__(self, *texts: str) -> None:
        self.adapter = FakeStreamAdapter([StreamScript(text=t) for t in texts])
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return self.adapter


def _state(cards: list[dict]) -> dict:
    return {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": [],
        "links": [],
        "selection": [],
    }


def _meta(client: TestClient) -> dict:
    resp = client.get(f"/api/interactive/boards/{BOARD}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def save_state(client: TestClient, cards: list[dict]) -> None:
    state = _state(cards)
    state["seq"] = int(_meta(client)["seq"])
    resp = client.put(
        f"/api/interactive/boards/{BOARD}/state", json={"state": state, "reason": "e-identity"}
    )
    assert resp.status_code == 200, resp.text


def wait_understanding(client: TestClient, uid: str, timeout: float = 20.0) -> dict | None:
    settled = {"understood", "needs_clarification", "no_action", "failed"}
    deadline = time.time() + timeout
    seen = None
    while time.time() < deadline:
        resp = client.get(f"/api/interactive/boards/{BOARD}/understanding")
        if resp.status_code == 200:
            for item in resp.json().get("understandings", []):
                if item.get("id") == uid:
                    seen = item
                    if item.get("status") in settled:
                        return item
        time.sleep(0.05)
    return seen


def submit(client: TestClient, **body) -> tuple[int, dict]:
    resp = client.post(f"/api/interactive/boards/{BOARD}/submissions", json=body)
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = {"_raw": resp.text}
    return resp.status_code, payload


def acceptance(client: TestClient, request_id: str) -> tuple[int, dict]:
    resp = client.get(
        f"/api/interactive/boards/{BOARD}/submissions/acceptance",
        params={"clientRequestId": request_id},
    )
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = {"_raw": resp.text}
    return resp.status_code, payload


DB_ERRORS: list[str] = []


def count(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    """计数；对象不存在（迁移还没到位）时记一条错误并返回 -1，让断言失败信息可读。"""
    try:
        return int(conn.execute(sql, args).fetchone()[0])
    except sqlite3.Error as exc:
        DB_ERRORS.append(f"{exc} :: {sql.strip()[:80]}")
        return -1


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    return any(str(r[1]) == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _index_exists(conn: sqlite3.Connection, index: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)
    ).fetchone()
    return row is not None


def current_seq(client: TestClient) -> int:
    """服务端当前板面版本：新身份的提交必须基于它（重试才用原基准）。"""
    return int(_meta(client)["seq"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="irm-int-e-identity-"))
    conn = connect(tmp / "app.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    # 三次不同脚本都返回 no_action：理解不会建任务，但确实调用了模型
    provider = Provider(json.dumps({"kind": "no_action", "summary": "没有需要开展的任务"}, ensure_ascii=False))
    app.state.interactive_adapter_provider = provider

    try:
        with TestClient(app) as client:
            save_state(
                client,
                [
                    models.new_card("file", "甲材料：单价 100 元", id="m1", meta={"name": "甲.txt"}),
                    models.new_card("code", "乙材料：交付 3 天", id="m2", meta={"language": "text"}),
                    models.new_card("text", "请比较这两份材料的成本与交付", id="req", checked=True),
                ],
            )
            seq = current_seq(client)
            visible = ["m1", "m2", "req"]

            print("\n[S0] 迁移 39 的对象（身份链路的落库前提）")
            check("S0.1 board_submissions.client_request_id 存在",
                  _column_exists(conn, "board_submissions", "client_request_id"))
            check("S0.2 唯一索引 idx_board_submissions_client_request 存在",
                  _index_exists(conn, "idx_board_submissions_client_request"))

            print("\n[S1] 同身份重试：原事实 + 不重复调用模型")
            status, first = submit(
                client,
                requestedVisible=visible,
                note="比较两份材料",
                baseStateVersion=seq,
                clientRequestId="E-1",
            )
            check("S1.1 首次提交 200", status == 200, f"status={status} body={json.dumps(first, ensure_ascii=False)[:200]}")
            sub1 = (first.get("submission") or {}).get("id")
            delivery1 = first.get("delivery") or {}
            uid1 = delivery1.get("understandingId")
            check("S1.2 首次提交有受理回执与理解身份",
                  bool(sub1) and bool(uid1),
                  f"submissionId={sub1} understandingId={uid1} delivery={delivery1}")
            if uid1:
                wait_understanding(client, str(uid1))
            calls_after_first = provider.calls
            rows_after_first = count(
                conn, "SELECT COUNT(*) FROM board_submissions WHERE client_request_id = ?", ("E-1",)
            )
            check("S1.3 库里只有一条该身份的提交", rows_after_first == 1, f"rows={rows_after_first}")
            check("S1.4 受理后确实调用了模型", calls_after_first >= 1, f"calls={calls_after_first}")

            status, replay = submit(
                client,
                requestedVisible=visible,
                note="比较两份材料",
                baseStateVersion=seq,
                clientRequestId="E-1",
            )
            replay_sub = (replay.get("submission") or {}).get("id")
            replay_uid = (replay.get("delivery") or {}).get("understandingId")
            check("S1.5 同身份重试 200", status == 200, f"status={status}")
            check("S1.6 返回原提交身份", replay_sub == sub1, f"{replay_sub} vs {sub1}")
            check("S1.7 返回原理解身份", replay_uid == uid1, f"{replay_uid} vs {uid1}")
            check("S1.8 replayed 标记为真", replay.get("replayed") is True, f"replayed={replay.get('replayed')}")
            check("S1.9 重试不新增提交行",
                  count(conn, "SELECT COUNT(*) FROM board_submissions WHERE client_request_id = ?", ("E-1",)) == 1)
            check("S1.10 重试不新增理解请求",
                  count(conn, "SELECT COUNT(*) FROM board_understanding WHERE submission_id = ?", (sub1,)) == 1,
                  f"rows={count(conn, 'SELECT COUNT(*) FROM board_understanding WHERE submission_id = ?', (sub1,))}")
            check("S1.11 重试不重复调用模型", provider.calls == calls_after_first,
                  f"{calls_after_first} -> {provider.calls}")

            print("\n[S2] 同身份不同内容：必须 409 拒绝")
            status, conflict = submit(
                client,
                requestedVisible=visible,
                note="换一个要求（不同内容）",
                baseStateVersion=seq,
                clientRequestId="E-1",
            )
            check("S2.1 返回 409", status == 409, f"status={status} body={json.dumps(conflict, ensure_ascii=False)[:200]}")
            check("S2.2 错误码是 request_identity_conflict",
                  "request_identity_conflict" in json.dumps(conflict, ensure_ascii=False),
                  json.dumps(conflict, ensure_ascii=False)[:200])
            check("S2.3 不落新提交",
                  count(conn, "SELECT COUNT(*) FROM board_submissions WHERE client_request_id = ?", ("E-1",)) == 1)
            check("S2.4 不重复调用模型", provider.calls == calls_after_first,
                  f"{calls_after_first} -> {provider.calls}")

            print("\n[S3] 同身份不同基准版本：同样拒绝")
            status, conflict2 = submit(
                client,
                requestedVisible=visible,
                note="比较两份材料",
                baseStateVersion=seq + 5,
                clientRequestId="E-1",
            )
            check("S3.1 返回 409", status == 409, f"status={status}")
            check("S3.2 不落新提交",
                  count(conn, "SELECT COUNT(*) FROM board_submissions WHERE client_request_id = ?", ("E-1",)) == 1)

            print("\n[S4] 精确查证：第二个身份与它的查证")
            status, second = submit(
                client,
                requestedVisible=visible,
                note="第二个身份的要求",
                baseStateVersion=current_seq(client),
                clientRequestId="E-2",
            )
            sub2 = (second.get("submission") or {}).get("id")
            check("S4.1 新身份提交成功", status == 200 and bool(sub2), f"status={status} submissionId={sub2}")
            st, acc1 = acceptance(client, "E-1")
            acc1_body = acc1.get("acceptance") or {}
            check("S4.2 E-1 查到自己（不是最近一条）", st == 200 and acc1_body.get("submissionId") == sub1,
                  f"status={st} got={acc1_body.get('submissionId')} want={sub1}")
            st, acc2 = acceptance(client, "E-2")
            acc2_body = acc2.get("acceptance") or {}
            check("S4.3 E-2 查到自己", st == 200 and acc2_body.get("submissionId") == sub2,
                  f"status={st} got={acc2_body.get('submissionId')} want={sub2}")
            check("S4.4 查证不回带别人的理解身份",
                  bool(acc1_body.get("understandingId")) and acc1_body.get("understandingId") != (second.get("delivery") or {}).get("understandingId"),
                  f"E-1 understandingId={acc1_body.get('understandingId')}")

            print("\n[S5] 「最近一条」诱饵：没用过的身份必须查不到")
            st, acc_none = acceptance(client, "E-9-never-used")
            check("S5.1 未用过的身份 404", st == 404, f"status={st}")
            check("S5.2 404 时不返回任何受理事实",
                  acc_none.get("acceptance") is None and bool(acc_none.get("reason")),
                  json.dumps(acc_none, ensure_ascii=False)[:200])

            print("\n[S6] 「时间接近」诱饵：紧接着的提交之后立即查一个假身份")
            status, third = submit(
                client,
                requestedVisible=visible,
                note="第三个身份，紧挨着上一条提交",
                baseStateVersion=current_seq(client),
                clientRequestId="E-3",
            )
            check("S6.1 第三条提交成功", status == 200, f"status={status}")
            st, acc_after = acceptance(client, "E-4-never-used")
            check("S6.2 刚刚有提交，未用身份仍然 404", st == 404, f"status={st}")
            check("S6.3 没有把时间接近当成证据",
                  (acc_after.get("acceptance") is None),
                  json.dumps(acc_after, ensure_ascii=False)[:200])
            st, acc2_again = acceptance(client, "E-2")
            check("S6.4 旧身份仍返回旧事实（未被新提交顶掉）",
                  st == 200 and (acc2_again.get("acceptance") or {}).get("submissionId") == sub2,
                  f"status={st} got={(acc2_again.get('acceptance') or {}).get('submissionId')} want={sub2}")

            print("\n[S7] 数据库事实")
            total_subs = count(conn, "SELECT COUNT(*) FROM board_submissions WHERE board_id = ?", (BOARD,))
            total_und = count(conn, "SELECT COUNT(*) FROM board_understanding")
            distinct_ids = count(
                conn,
                "SELECT COUNT(DISTINCT client_request_id) FROM board_submissions"
                " WHERE client_request_id <> '' AND board_id = ?",
                (BOARD,),
            )
            check("S7.1 三个身份三条提交", total_subs == 3, f"submissions={total_subs}")
            check("S7.2 三个身份互不相同", distinct_ids == 3, f"distinct={distinct_ids}")
            check("S7.3 理解请求数不超过提交数（可复用的不留多余请求）",
                  0 <= total_und <= 3, f"understandings={total_und}")
            dup = count(
                conn,
                "SELECT COUNT(*) FROM (SELECT client_request_id, COUNT(*) c FROM board_submissions"
                " WHERE client_request_id <> '' GROUP BY board_id, client_request_id HAVING c > 1)",
            )
            check("S7.4 不存在同 (board, 身份) 的重复行", dup == 0, f"dup={dup}")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)

    if DB_ERRORS:
        print("\n数据库层错误（对象缺失时会出现）：")
        for item in dict.fromkeys(DB_ERRORS):
            print("  - " + item)
    failed = [r for r in RESULTS if not r["ok"]]
    summary = {
        "script": "scripts/irm-int-verify/verify_submission_identity.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "worktree": str(ROOT),
        "head": _head(),
        "db_errors": sorted(set(DB_ERRORS)),
        "checks_total": len(RESULTS),
        "checks_failed": len(failed),
        "passed": not failed,
        "checks": RESULTS,
    }
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n证据已写入 {out}")
    print(f"\n结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过"
          + ("" if not failed else "；失败：" + ", ".join(r["check"] for r in failed)))
    return 1 if failed else 0


def _head() -> str:
    import subprocess

    out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


if __name__ == "__main__":
    raise SystemExit(main())

