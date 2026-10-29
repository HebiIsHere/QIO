# -*- coding: utf-8 -*-
"""F 真实界面证据的数据准备（隔离数据目录）：写入可回看的审批历史（刷新/重连后仍在）。

只用于验收脚本；不参与产品逻辑。history 行是后端 GET /api/approvals/history 的真实来源。
"""
import json
import sqlite3
import time
import urllib.request

API = "http://127.0.0.1:8842"
DB = r"D:\qio-dev\.tmp-fu2-f-ui-data\app.db"


def call(path, method="GET", body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + path, data=data, method=method, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


for _ in range(120):
    try:
        call("/api/health")
        break
    except Exception:
        time.sleep(0.5)
try:
    call("/api/onboarding/complete", "POST", {})
except Exception as exc:
    print("onboarding:", exc)

now = "2026-10-26T01:40:00+00:00"
rows = [
    (
        "appr_f_hist_a",
        "tool_execution",
        "已执行：写入 2 个文件",
        "executed",
        json.dumps({"ok": True, "kind": "tool_execution", "label": "写入发布材料"}, ensure_ascii=False),
        now,
    ),
    (
        "appr_f_hist_b",
        "tool_execution",
        "执行失败：目标文件被占用",
        "failed",
        json.dumps({"ok": False, "kind": "tool_execution", "label": "修改发布材料", "error": "目标文件被占用"}, ensure_ascii=False),
        now,
    ),
]
conn = sqlite3.connect(DB)
for row in rows:
    conn.execute(
        "INSERT OR REPLACE INTO approval_history (approval_id, kind, summary, outcome, detail, at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        row,
    )
conn.commit()
print("seeded history:", [r[0] for r in rows])
conn.close()
