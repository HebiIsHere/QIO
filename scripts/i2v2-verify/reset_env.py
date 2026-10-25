#!/usr/bin/env python
"""把 D 组受控板面复位到「干净种子 + 一个执行中意图」，供真实页面流程之间复用。

只用于隔离数据域（QIO_ACCEPT_DB 指向的测试库），不碰产品数据。
用法（跑在独立后端 8745 / 独立数据域时）：
  $env:QIO_ACCEPT_API="http://127.0.0.1:8745"
  $env:QIO_ACCEPT_DB="D:\qio-dev\.i2v2-data-d\app.db"
  python scripts/i2v2-verify/reset_env.py --board board_default
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("QIO_ACCEPT_API", "http://127.0.0.1:8745").rstrip("/")
DB = os.environ.get("QIO_ACCEPT_DB", r"D:\qio-dev\.i2v2-data-d\app.db")
BOARD = os.environ.get("QIO_ACCEPT_BOARD", "board_default")
STAMP = "2026-10-25T00:00:00.000Z"


def call(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {error.code} {method} {path}: {detail[:400]}") from error


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def card(cid: str, kind: str, content: str, x: int, y: int, **extra) -> dict:
    item = {
        "id": cid, "kind": kind, "content": content, "meta": {}, "x": x, "y": y,
        "w": 240, "h": 150, "checked": False, "hidden": False, "folded": False,
        "bookmarked": False, "deleted": False, "createdAt": STAMP, "updatedAt": STAMP,
    }
    item.update(extra)
    return item


def build_state(seq: int) -> dict:
    cards = [
        card("c_demand", "file", "需求材料", 120, 520, meta={"name": "需求材料.pdf"}),
        card("c_constraint", "file", "约束说明", 420, 520, meta={"name": "约束说明.pdf"}),
        card("c_note", "text", "整理发布材料，先列出两份材料的关键约束。", 120, 740, checked=True),
        card("c_note2", "text", "这份注释是草稿冲突流程的载体。", 420, 740),
        card("c_f1", "text", "填充卡 1", 760, 520),
        card("c_f2", "text", "填充卡 2", 760, 740),
        card("c_f3", "text", "填充卡 3", 1020, 520),
        card("c_f4", "text", "填充卡 4", 1020, 740),
    ]
    groups = [
        {"id": "g_plan", "name": "发布计划", "defaultName": False, "ordered": True,
         "x": 80, "y": 690, "w": 640, "h": 260, "members": ["c_note", "c_note2"],
         "deleted": False, "createdAt": STAMP, "updatedAt": STAMP},
        {"id": "g_fill", "name": "背景", "defaultName": False, "ordered": False,
         "x": 720, "y": 470, "w": 340, "h": 660, "members": ["c_f1", "c_f2", "c_f3"],
         "deleted": False, "createdAt": STAMP, "updatedAt": STAMP},
    ]
    links = [
        {"id": "l_meaning", "src": "c_demand", "dst": "c_constraint", "direction": True,
         "meaning": "属于", "curve": {"x": 40, "y": 10}, "deleted": False,
         "createdAt": STAMP, "updatedAt": STAMP},
        {"id": "l_group", "src": "group:g_plan", "dst": "c_demand", "direction": False,
         "meaning": "依据", "curve": {"x": -20, "y": 0}, "deleted": False,
         "createdAt": STAMP, "updatedAt": STAMP},
    ]
    return {"boardId": BOARD, "seq": seq, "updatedAt": now(), "cards": cards,
            "groups": groups, "links": links, "selection": []}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", default=BOARD)
    args = parser.parse_args()
    board = args.board

    # 1) 意图全部回到 pending（保留记录与材料依赖），再挑第一条置 running。
    rows = []
    try:
        conn = sqlite3.connect(DB, timeout=20)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, progress FROM board_intents WHERE board_id=? ORDER BY rowid", (board,)).fetchall()
        instance = call("/api/runtime/state").get("instance_id", "")
        for row in rows:
            progress = json.loads(row["progress"] or "{}")
            progress.pop("__ownerInstance", None)
            conn.execute(
                "UPDATE board_intents SET status='pending', progress=? WHERE id=?",
                (json.dumps(progress, ensure_ascii=False), row["id"]),
            )
        if rows:
            first = rows[0]
            progress = json.loads(first["progress"] or "{}")
            progress["__ownerInstance"] = instance
            conn.execute(
                "UPDATE board_intents SET status='running', demo=0, title=?, material_refs=?, progress=? WHERE id=?",
                ("整理需求与约束并给出对比摘要", json.dumps(["c_demand", "c_constraint"]),
                 json.dumps(progress, ensure_ascii=False), first["id"]),
            )
        conn.commit()
        conn.close()
    except sqlite3.Error as error:
        raise SystemExit(f"重置意图失败（DB 不可写？）：{error}") from error

    # 2) 板面回到受控种子（用服务器当前 seq 覆盖，避免 stale_state）。
    current = call(f"/api/interactive/boards/{board}/state")
    state = build_state(int(current.get("seq") or 0))
    code = call(f"/api/interactive/boards/{board}/state", "PUT", {"state": state, "reason": "D reset"})
    print(f"[reset] board={board} seq={code.get('seq')} running={rows[0]['id'] if rows else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
