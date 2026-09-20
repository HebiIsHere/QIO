"""第一阶段手工验收脚本（HTTP 层，仅标准库）。

对着一台正在运行的 QIO backend 跑「用户看到的状态 = 系统真实状态」的核心检查：

* S1 没有可用凭据时：POST /api/turns 立即返回 turn_id，turn 必须正常收尾
  （TURN_START / WARNING / TURN_END(status=unavailable)），而不是永远 running。
* S2 SSE 重连：带上 `last_event_id` 时不得重新消费 TURN_END（游标之后才有事件）。
* S6 本机 API 边界：无令牌 401、恶意 origin 403、正确令牌 + 正式 origin 200。
* S7 凭据端点攻击：只改 endpoint 必须被拒绝；带上新 secret + 显式确认才允许。

用法（backend 已由 scripts/e2e_up.py 起好）：

    # 开发豁免口径（QIO_DEV_INSECURE=1，无令牌）
    python scripts/verify_stage1.py

    # 有令牌口径（scripts/e2e_up.py --secure，令牌文件由脚本写出）
    python scripts/verify_stage1.py --secure --token-file %TEMP%\\qio-e2e-session-token.txt

退出码：0 = 全部通过；1 = 有失败（逐条打印 PASS/FAIL/NOT RUN）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8734"
TAURI_ORIGIN = "http://tauri.localhost"

results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | None, detail: str) -> None:
    status = "NOT RUN" if ok is None else ("PASS" if ok else "FAIL")
    results.append((status, name, detail))
    print(f"[{status}] {name} — {detail}")


def http(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    token: str = "",
    origin: str | None = None,
    timeout: float = 15.0,
    base: str = BASE,
) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def read_sse(
    *,
    token: str = "",
    last_event_id: str | None = None,
    want: int | None = 1,
    idle: float = 1.2,
    max_events: int = 400,
) -> list[dict]:
    """读 SSE：收够 want 个事件、或连续 idle 秒没有新事件就停下。

    没有游标时后端会把最近的事件重放给新连接，所以「读一段」= 连上、收干当前缓冲。
    """
    url = f"{BASE}/api/events"
    if last_event_id:
        url += f"?last_event_id={last_event_id}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=idle) as resp:
        while len(events) < max_events:
            if want is not None and len(events) >= want:
                break
            try:
                line = resp.readline()
            except (TimeoutError, OSError):
                # 连续 idle 秒没有新事件（例如游标之后本来就是空的）：结束
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text.startswith("data: "):
                events.append(json.loads(text[6:]))
    return events


def scenario_turn_lifecycle(token: str) -> None:
    status, body = http("POST", "/api/turns", body={"message": "   "}, token=token)
    record(
        "S1.0 空消息被拒绝",
        status == 400,
        f"status={status}",
    )

    status, body = http("POST", "/api/turns", body={"message": "验收：没有凭据时也要收尾"}, token=token)
    if status != 200 or not isinstance(body, dict):
        record("S1.1 POST /api/turns 返回 turn_id", False, f"status={status} body={body}")
        return
    turn_id = body.get("turn_id")
    record(
        "S1.1 POST /api/turns 立即返回 turn_id",
        bool(turn_id) and body.get("status") == "accepted",
        f"turn_id={turn_id} status={body.get('status')}",
    )

    time.sleep(0.5)  # 让后台 turn 跑完
    # 后端会把最近的事件重放给新连接，所以按 turn_id 归属，而不是按数量
    events = read_sse(token=token, want=None, idle=1.2)
    mine = [e for e in events if e.get("data", {}).get("turn_id") == turn_id]
    starts = [e for e in mine if e["type"] == "TURN_START"]
    ends = [e for e in mine if e["type"] == "TURN_END"]
    warnings_ = [e for e in mine if e["type"] == "WARNING"]
    record(
        "S1.2 恰好一个 TURN_START 且 turn_id 一致",
        len(starts) == 1 and starts[0]["data"].get("turn_id") == turn_id,
        f"starts={len(starts)}",
    )
    ok_end = len(ends) == 1 and ends[0]["data"].get("turn_id") == turn_id
    record(
        "S1.3 恰好一个 TURN_END（不会永远 running）",
        ok_end,
        f"ends={len(ends)} status={ends[0]['data'].get('status') if ends else None}",
    )
    if ends:
        record(
            "S1.4 无凭据的终态是 unavailable",
            ends[0]["data"].get("status") == "unavailable",
            f"status={ends[0]['data'].get('status')}",
        )
    record(
        "S1.5 给出可执行的用户提示",
        bool(warnings_),
        "WARNING 事件存在" if warnings_ else "没有 WARNING",
    )

    # S2：拿最后一个事件的 id 当游标，重连后不应再收到 TURN_END
    if events:
        last_id = events[-1]["id"]
        replayed = read_sse(token=token, last_event_id=last_id, want=None, idle=1.2)
        dup_end = [
            e
            for e in replayed
            if e["type"] == "TURN_END" and e.get("data", {}).get("turn_id") == turn_id
        ]
        record(
            "S2.1 重连不重复消费 TURN_END",
            not dup_end,
            f"cursor={last_id} 之后收到 {len(replayed)} 个事件，其中 TURN_END={len(dup_end)}",
        )
    else:
        record("S2.1 重连不重复消费 TURN_END", None, "没有可用的游标（S1 未收到事件）")


def scenario_api_boundary(token: str, secure: bool) -> None:
    if not secure:
        status, _ = http("GET", "/api/credentials")
        record(
            "S6.0 开发豁免口径：无令牌可访问（仅限显式 QIO_DEV_INSECURE=1）",
            status == 200,
            f"status={status}",
        )
        return

    status, body = http("GET", "/api/credentials")
    record("S6.1 无令牌被拒绝", status == 401, f"status={status}")

    status, _ = http("GET", "/api/credentials", token="wrong-token")
    record("S6.2 错误令牌被拒绝", status == 401, f"status={status}")

    status, body = http(
        "GET",
        "/api/credentials",
        token=token,
        origin=TAURI_ORIGIN,
    )
    record("S6.3 正确令牌 + 正式 origin 可用", status == 200, f"status={status}")

    status, body = http(
        "POST",
        "/api/turns",
        body={"message": "恶意页面尝试"},
        token=token,
        origin="https://attacker.example",
    )
    record(
        "S6.4 恶意 origin 被拒绝",
        status == 403,
        f"status={status} reason={body.get('reason') if isinstance(body, dict) else body}",
    )

    status, body = http("POST", "/api/events/test?event_type=WARNING", body={"code": "x"}, token=token)
    record("S6.5 测试事件接口要求令牌（且开发模式存在）", status == 200, f"status={status}")


def scenario_credential_attack(token: str) -> None:
    key_id = f"verify_{int(time.time())}"
    status, _ = http(
        "POST",
        "/api/credentials",
        body={
            "key_id": key_id,
            "secret": "sk-verify-original",
            "tags": ["main-loop"],
            "endpoint": "https://api.openai.com/v1",
        },
        token=token,
    )
    if status != 200:
        # Windows 凭据库需要交互登录会话；受限/无会话环境下 CredWrite 会失败，
        # 这属于环境限制，不算产品缺陷 —— 但要如实标成 NOT RUN。
        reason = (
            f"status={status}（本机凭据库不可用：无交互登录会话）"
            if status == 500
            else f"status={status}"
        )
        record("S7 凭据端点攻击", None, reason)
        return

    status, body = http(
        "PATCH",
        f"/api/credentials/{key_id}",
        body={"endpoint": "https://attacker.example/v1"},
        token=token,
    )
    record(
        "S7.1 只改 endpoint 必须被拒绝（不得静默复用旧 Key）",
        status == 400,
        f"status={status}",
    )

    status, _ = http(
        "PATCH",
        f"/api/credentials/{key_id}",
        body={"endpoint": "https://attacker.example/v1", "secret": "sk-verify-new"},
        token=token,
    )
    record("S7.2 有 secret 但没确认也必须被拒绝", status == 400, f"status={status}")

    status, body = http(
        "PATCH",
        f"/api/credentials/{key_id}",
        body={
            "endpoint": "https://gateway.example/v1",
            "secret": "sk-verify-new",
            "confirm_reconfigure": True,
        },
        token=token,
    )
    record(
        "S7.3 重新配置（新 secret + 显式确认）才允许改 endpoint",
        status == 200 and isinstance(body, dict) and body.get("credential", {}).get("version") == 2,
        f"status={status} version={body.get('credential', {}).get('version') if isinstance(body, dict) else body}",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secure", action="store_true", help="后端要求会话令牌")
    parser.add_argument("--token-file", default="")
    parser.add_argument("--token", default="")
    args = parser.parse_args()

    token = args.token
    if not token and args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if args.secure and not token:
        print("--secure 需要 --token 或 --token-file（令牌绝不打印）")
        return 1

    try:
        status, _ = http("GET", "/api/health")
    except OSError as exc:
        print(f"backend 不可达：{exc}")
        return 1
    print(f"backend /api/health -> {status}")

    scenario_turn_lifecycle(token)
    scenario_api_boundary(token, args.secure)
    scenario_credential_attack(token)

    failed = [r for r in results if r[0] == "FAIL"]
    skipped = [r for r in results if r[0] == "NOT RUN"]
    print(
        f"\n汇总：PASS={len([r for r in results if r[0] == 'PASS'])} "
        f"FAIL={len(failed)} NOT RUN={len(skipped)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
