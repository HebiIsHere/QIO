"""真实后端 × 真实前端字段名接缝：抓取**服务端原样 JSON**。

起真后端（E 自己起，端口 8848，临时数据目录），走真 HTTP：
  建板面 → 保存状态 → 带 clientRequestId 提交 → GET .../submissions/acceptance
把每一步的**原始响应体**写进 JSON 证据，供前端 vitest 直接使用（活体或夹具两种模式）。

用法：
  backend/.venv/Scripts/python.exe scripts/irm-int-verify/probe_wire.py \
      --base http://127.0.0.1:8848 [--json out/wire-<sha>.json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOARD = "board_e_wire"
API = "/api/interactive"


def http(method: str, url: str, body: dict | None = None, timeout: float = 30.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return exc.code, parsed, raw


def wait_up(base: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _payload, _raw = http("GET", f"{base}{API}/boards", timeout=3)
            if status < 500:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def card(cid: str, kind: str, content: str, **extra) -> dict:
    return {
        "id": cid,
        "kind": kind,
        "content": content,
        "meta": extra.pop("meta", {}),
        "x": 0.0,
        "y": 0.0,
        "w": 240.0,
        "h": 120.0,
        "checked": extra.pop("checked", False),
        "hidden": False,
        "folded": False,
        "bookmarked": False,
        "deleted": False,
        "createdAt": "2026-10-11T00:00:00+00:00",
        "updatedAt": "2026-10-11T00:00:00+00:00",
        **extra,
    }


def head_sha() -> str:
    out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8848")
    ap.add_argument("--json", default="")
    ap.add_argument("--request-id", default="creq_e_wire_0001")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    evidence: dict = {
        "script": "scripts/irm-int-verify/probe_wire.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base": base,
        "head": head_sha(),
        "board_id": BOARD,
        "steps": [],
    }
    if not wait_up(base):
        print(f"后端没起来：{base}")
        return 2

    # 1) 建板面
    status, body, raw = http("POST", f"{base}{API}/boards", {"boardId": BOARD, "title": "E 字段名接缝"})
    evidence["steps"].append({"name": "create-board", "status": status, "body": body, "raw": raw})
    print(f"[1] POST /boards -> {status}")

    # 2) 保存状态（两条材料 + 一条勾选注释）
    state = {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": "2026-10-11T00:00:00+00:00",
        "cards": [
            card("m1", "file", "甲材料：单价 100 元", meta={"name": "甲.txt"}),
            card("m2", "code", "乙材料：交付 3 天", meta={"language": "text"}),
            card("req", "text", "请比较这两份材料的成本与交付", checked=True),
        ],
        "groups": [],
        "links": [],
        "selection": [],
    }
    status, body, raw = http("PUT", f"{base}{API}/boards/{BOARD}/state", {"state": state, "reason": "e-wire"})
    evidence["steps"].append({"name": "put-state", "status": status, "body": body, "raw": raw})
    print(f"[2] PUT /state -> {status}")
    if status == 409:
        detail = (body or {}).get("detail") or {}
        meta = http("GET", f"{base}{API}/boards/{BOARD}/state")[1]
        seq = meta["seq"]
        state["seq"] = seq
        check = http(
            "POST",
            f"{base}{API}/boards/{BOARD}/impact-check",
            {"stateVersion": seq, "changeSet": {"state": state}},
        )[1]
        status, body, raw = http(
            "PUT",
            f"{base}{API}/boards/{BOARD}/state",
            {"state": state, "reason": "e-wire", "confirm": {"checkId": check["checkId"], "stateVersion": check.get("stateVersion")}},
        )
        evidence["steps"].append({"name": "put-state-confirmed", "status": status, "body": body, "raw": raw})
        print(f"[2b] PUT /state（确认影响后） -> {status}")

    seq = int((http("GET", f"{base}{API}/boards/{BOARD}/state")[1] or {}).get("seq", 0))
    evidence["base_state_version"] = seq

    # 3) 带 clientRequestId 提交
    submit_body = {
        "requestedVisible": ["m1", "m2", "req"],
        "note": "E 字段名接缝",
        "baseStateVersion": seq,
        "clientRequestId": args.request_id,
    }
    status, body, raw = http("POST", f"{base}{API}/boards/{BOARD}/submissions", submit_body)
    evidence["steps"].append({"name": "submit", "status": status, "request": submit_body, "body": body, "raw": raw})
    print(f"[3] POST /submissions(clientRequestId={args.request_id}) -> {status}")

    # 4) 同身份重试
    status_replay, body_replay, raw_replay = http("POST", f"{base}{API}/boards/{BOARD}/submissions", submit_body)
    evidence["steps"].append({"name": "submit-replay", "status": status_replay, "request": submit_body,
                              "body": body_replay, "raw": raw_replay})
    print(f"[4] 同身份重试 -> {status_replay} replayed={((body_replay or {}).get('replayed'))}")

    # 5) 精确查证
    status, body, raw = http("GET", f"{base}{API}/boards/{BOARD}/submissions/acceptance?clientRequestId={args.request_id}")
    evidence["steps"].append({"name": "acceptance-hit", "status": status, "body": body, "raw": raw})
    print(f"[5] GET /acceptance（命中） -> {status}")

    # 6) 未用身份查证（诱饵：刚刚有提交）
    status, body, raw = http("GET", f"{base}{API}/boards/{BOARD}/submissions/acceptance?clientRequestId=creq_e_never_used_9999")
    evidence["steps"].append({"name": "acceptance-miss", "status": status, "body": body, "raw": raw})
    print(f"[6] GET /acceptance（没这个身份） -> {status}")

    # 7) 同身份不同内容（409）
    conflict_body = dict(submit_body)
    conflict_body["note"] = "换一个要求"
    status, body, raw = http("POST", f"{base}{API}/boards/{BOARD}/submissions", conflict_body)
    evidence["steps"].append({"name": "submit-conflict", "status": status, "request": conflict_body, "body": body, "raw": raw})
    print(f"[7] 同身份不同内容 -> {status}")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"原样 JSON 证据写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
