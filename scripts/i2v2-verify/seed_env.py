#!/usr/bin/env python
"""I2V2F · D 组浏览器验收的受控种子数据（只通过 HTTP API 写，不直接改 SQLite）。

用法（stage 2，Lead 已给隔离后端）：
  $env:QIO_ACCEPT_API = "http://127.0.0.1:8744"
  python scripts/i2v2-verify/seed_env.py --board board_d_accept

它只准备「D 的验收板面」，不碰 Lead 的 board_default：
- 8 张卡片（2 份文件材料 + 2 条注释 + 4 张填充卡）、2 个组、2 条关系（含含义/方向/曲线）；
- 4 个演示意图，其中 1 个批准后处于 running，并依赖材料卡 c_demand / c_constraint
  （用于流程 1「修改运行任务材料 → 影响确认」与流程 4「任务暂停/继续/取消」）；
- 一份服务器草稿（流程 5 的草稿冲突基线）。
幂等：同一个 board 反复运行会覆盖成同一份受控状态。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("QIO_ACCEPT_API", "http://127.0.0.1:8744").rstrip("/")
BOARD = os.environ.get("QIO_ACCEPT_BOARD", "board_d_accept")

STAMP = "2026-10-25T00:00:00.000Z"


def call(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(API + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {error.code} {method} {path}: {detail[:500]}") from error


def card(card_id: str, kind: str, content: str, x: int, y: int, **extra) -> dict:
    base = {
        "id": card_id,
        "kind": kind,
        "content": content,
        "meta": {},
        "x": x,
        "y": y,
        "w": 240,
        "h": 150,
        "checked": False,
        "hidden": False,
        "folded": False,
        "bookmarked": False,
        "deleted": False,
        "createdAt": STAMP,
        "updatedAt": STAMP,
    }
    base.update(extra)
    return base


def build_state() -> dict:
    cards = [
        card("c_demand", "file", "需求材料", 120, 140, meta={"name": "需求材料.pdf"}),
        card("c_constraint", "file", "约束说明", 420, 140, meta={"name": "约束说明.pdf"}),
        card("c_note", "text", "整理发布材料，先列出两份材料的关键约束。", 120, 380, checked=True),
        card("c_note2", "text", "这份注释是草稿冲突流程的载体。", 420, 380),
        card("c_f1", "text", "填充卡 1", 760, 140),
        card("c_f2", "text", "填充卡 2", 760, 340),
        card("c_f3", "text", "填充卡 3", 760, 540),
        card("c_f4", "text", "填充卡 4", 120, 620),
    ]
    groups = [
        {
            "id": "g_plan",
            "name": "发布计划",
            "defaultName": False,
            "ordered": True,
            "x": 80,
            "y": 330,
            "w": 640,
            "h": 260,
            "members": ["c_note", "c_note2"],
            "deleted": False,
            "createdAt": STAMP,
            "updatedAt": STAMP,
        },
        {
            "id": "g_fill",
            "name": "背景",
            "defaultName": False,
            "ordered": False,
            "x": 720,
            "y": 90,
            "w": 340,
            "h": 660,
            "members": ["c_f1", "c_f2", "c_f3"],
            "deleted": False,
            "createdAt": STAMP,
            "updatedAt": STAMP,
        },
    ]
    links = [
        {
            "id": "l_meaning",
            "src": "c_demand",
            "dst": "c_constraint",
            "direction": True,
            "meaning": "属于",
            "curve": {"x": 40, "y": 10},
            "deleted": False,
            "createdAt": STAMP,
            "updatedAt": STAMP,
        },
        {
            "id": "l_group",
            "src": "group:g_plan",
            "dst": "c_demand",
            "direction": False,
            "meaning": "依据",
            "curve": {"x": -20, "y": 0},
            "deleted": False,
            "createdAt": STAMP,
            "updatedAt": STAMP,
        },
    ]
    return {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": STAMP,
        "cards": cards,
        "groups": groups,
        "links": links,
        "selection": [],
    }


def main() -> int:
    global BOARD
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", default=BOARD)
    args = parser.parse_args()
    BOARD = args.board

    saved = call(f"/api/interactive/boards/{BOARD}/state", "PUT", {"state": build_state(), "reason": "D 验收种子"})
    seq = saved.get("seq")
    print(f"[seed] board={BOARD} seq={seq}")

    created = call(f"/api/interactive/boards/{BOARD}/intents", "POST", {"demo": True})
    print(f"[seed] demo intents created={len(created.get('created', []))}")

    listed = call(f"/api/interactive/boards/{BOARD}/intents")
    intents = listed.get("intents", [])
    combine = next((item for item in intents if "把材料归为一组" in str(item.get("title", ""))), None)
    if combine is None:
        raise SystemExit("[seed] 没有找到演示意图「把材料归为一组并给出对比摘要」：" + json.dumps([i.get("title") for i in intents], ensure_ascii=False))
    approved = call(f"/api/interactive/intents/{combine['id']}/approve", "POST", {})
    print(f"[seed] approve running intent={combine['id']} ok={approved.get('ok')} status={(approved.get('intent') or {}).get('status')}")

    # 流程 5 的服务器草稿基线：同一张卡在服务器上有一份「别人的版本」。
    current = call(f"/api/interactive/drafts/{BOARD}")
    rev = current.get("rev")
    call(
        f"/api/interactive/drafts/{BOARD}",
        "PUT",
        {"mode": "patch", "drafts": {"card:c_note2": "服务器上的草稿版本"}, **({"baseRev": rev} if rev is not None else {})},
    )
    print("[seed] server draft card:c_note2 = 服务器上的草稿版本")

    # 给浏览器脚本一份稳定的 id / 期望值清单。
    views = call(f"/api/interactive/boards/{BOARD}/intent-views")
    running = [item for item in views.get("intents", []) if item.get("status") == "running"]
    payload = {
        "api": API,
        "board": BOARD,
        "seq": seq,
        "runningIntentIds": [item.get("intent_id") for item in running],
        "relation": {"id": "l_meaning", "meaning": "属于", "direction": True, "curve": {"x": 40, "y": 10}},
        "group": {"id": "g_plan", "members": ["c_note", "c_note2"]},
        "draft": {"key": "card:c_note2", "serverText": "服务器上的草稿版本"},
    }
    print("[seed] manifest=" + json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
