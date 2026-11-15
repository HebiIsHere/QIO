"""QIO #46 第八轮 harness 种子：在隔离数据目录给板面卡插一条「执行中」任务。

只写 r8/e2e-data（QIO_DATA_DIR），不碰产品代码与用户真实数据。
用法（cwd=r8/verify，env QIO_DATA_DIR=隔离目录，env PYTHONPATH=后台 backend/src）：
  uv run --frozen --project D:\qio-dev\q46-r8\backend python seed-r8.py --api http://127.0.0.1:8735 --board b --card c --title t
输出：JSON 一行（ok / intentId / boardId）。
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

from agent.interactive import board_store, intents, models
from agent.storage import db as db_mod


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", required=True)
    parser.add_argument("--card", required=True)
    parser.add_argument("--title", default="执行中任务（harness）")
    parser.add_argument("--status", default="running")
    parser.add_argument("--api", default="http://127.0.0.1:8735")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("QIO_DATA_DIR") or "")
    if not data_dir or not (data_dir / "app.db").exists():
        print(json.dumps({"ok": False, "reason": "QIO_DATA_DIR/app.db 不存在", "dataDir": str(data_dir)}))
        return 2
    conn = db_mod.connect(data_dir / "app.db")
    # 让 running 不被「重启恢复」降级：progress.__ownerInstance 必须等于当前后端实例 id
    instance_id = ""
    try:
        with urllib.request.urlopen(args.api.rstrip("/") + "/api/instance", timeout=5) as resp:
            instance_id = str(json.loads(resp.read().decode("utf-8")).get("instance_id") or "")
    except Exception:
        instance_id = ""
    board_store.ensure_board(conn, board_id=args.board)
    state = board_store.load_board(conn, board_id=args.board)["state"]
    card_ids = [str(c.get("id")) for c in state.get("cards") or []]
    if args.card not in card_ids:
        print(json.dumps({"ok": False, "reason": "card 不在板面上: " + args.card, "cards": card_ids}))
        return 3

    payload = intents._insert_intent(
        conn,
        board_id=args.board,
        title=args.title,
        status=args.status,
        material_refs=[args.card],
        progress={"done": 35, "text": "执行中"},
        operations=[],
        basis=[],
        intent_key="k_harness_" + args.card,
    )
    row = conn.execute("SELECT * FROM board_intents WHERE id = ?", (payload["id"],)).fetchone()
    watch = intents._watch_for(conn, row, state)
    progress = models.loads(row["progress"], {})
    progress["__materialWatch"] = watch
    if instance_id:
        progress["__ownerInstance"] = instance_id
    intents._update(conn, payload["id"], progress=progress)
    conn.commit()
    print(json.dumps({"ok": True, "intentId": payload["id"], "boardId": args.board, "card": args.card}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
