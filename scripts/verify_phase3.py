"""第三阶段手工验收脚本（HTTP + SSE 层，仅标准库）。

对着一台正在运行的 QIO backend 跑「第三阶段真正要做到的事」：

* P1 记忆封块设置的语义是「轮」（`fragment_max_turns`），旧键 `fragment.max_messages`
  只作为一次性迁移回退；
* P2 Topic Detail 有摘要 / 关键词 / 最近活动 / 真实消息数，且**不内联原文**；
* P3 Fragment 原文按需分页（`offset` / `limit` / `total`）；
* P4 高影响知识可以「忽略」（`revoked` + `provenance.ignored_at`，幂等）；
* P5 真实一轮的事件序列（需要 `--live-turn`，会真的调用你配置的模型）：
  恰好一次 TURN_START / TURN_END、CAPABILITY、TOOL_START 与 TOOL_END 共享 `call_id`、
  TOOL_END 带 `duration_ms`、USAGE 存在；
* P6 审批内容可理解：如果这一轮真的触发了审批，检查 payload 里有
  `description` / `access` / `scope`（拿不到就如实报 NOT RUN）；
* P7 线上事件格式与前端消费的字段一致（用开发模式的测试事件注入口）。

用法（backend 已由 scripts/e2e_up.py 起好）：

    python scripts/verify_phase3.py                 # 只跑不花钱的检查
    python scripts/verify_phase3.py --live-turn     # 额外跑一轮真实对话

退出码：0 = 全部通过；1 = 有失败（逐条打印 PASS/FAIL/NOT RUN）。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8734"

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
    timeout: float = 30.0,
) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


class EventCollector:
    """后台读 SSE，把事件按类型收好（真实线上格式，不是进程内调用）。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        time.sleep(0.5)  # 等连接建立

    def stop(self) -> None:
        self._stop = True

    def _run(self) -> None:
        req = urllib.request.Request(f"{BASE}/api/events")
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                current: dict = {}
                for raw in resp:
                    if self._stop:
                        return
                    line = raw.decode("utf-8", errors="replace").rstrip("\n")
                    if line.startswith("event: "):
                        current["type"] = line[len("event: "):]
                    elif line.startswith("data: "):
                        try:
                            payload = json.loads(line[len("data: "):])
                        except ValueError:
                            continue
                        if current.get("type"):
                            payload.setdefault("type", current["type"])
                        self.events.append(payload)
        except Exception:  # noqa: BLE001 - 采集失败由断言体现
            return

    def of(self, event_type: str) -> list[dict]:
        out = []
        for e in self.events:
            if e.get("type") == event_type:
                out.append(e)
        return out

    def wait_for(self, event_type: str, timeout: float = 180.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            hits = self.of(event_type)
            if hits:
                return hits[-1]
            time.sleep(0.3)
        return None

    def wait_for_turn_end(self, turn_id: str, timeout: float = 300.0) -> dict | None:
        """只等**本轮**的 TURN_END。

        SSE 连接会重放缓冲区里最近的事件（这是重连语义），所以不能拿
        「任意一条 TURN_END」当作本轮收尾 —— 那会立刻返回上一轮的事件。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for e in self.of("TURN_END"):
                if e.get("data", {}).get("turn_id") == turn_id:
                    return e
            time.sleep(0.3)
        return None


# -- P1 记忆封块设置：单位是「轮」 ------------------------------------------


def check_memory_settings() -> None:
    status, data = http("GET", "/api/settings/memory")
    if status != 200 or not isinstance(data, dict):
        record("P1 记忆封块设置可读", False, f"GET /api/settings/memory -> {status} {data}")
        return
    if "fragment_max_turns" not in data:
        record(
            "P1 记忆封块设置可读",
            False,
            f"响应里没有 fragment_max_turns（仍是旧字段？）：{data}",
        )
        return
    original = int(data["fragment_max_turns"])
    record("P1 记忆封块设置可读", 1 <= original <= 30, f"fragment_max_turns={original}")

    probe = 8 if original != 8 else 9
    status, saved = http("PUT", "/api/settings/memory", body={"fragment_max_turns": probe})
    ok = status == 200 and isinstance(saved, dict) and saved.get("fragment_max_turns") == probe
    record("P1 记忆封块可写（单位：轮）", ok, f"PUT -> {status} {saved}")

    status, again = http("GET", "/api/settings/memory")
    ok = (
        status == 200
        and isinstance(again, dict)
        and again.get("fragment_max_turns") == probe
    )
    record("P1 保存后读回一致", ok, f"GET -> {again}")

    status, bad = http("PUT", "/api/settings/memory", body={"fragment_max_turns": 999})
    record("P1 越界值被拒绝", status == 400, f"PUT 999 -> {status} {bad}")

    http("PUT", "/api/settings/memory", body={"fragment_max_turns": original})


# -- P2 Topic Detail 的信息层 ------------------------------------------------


def check_topic_detail() -> None:
    status, listing = http("GET", "/api/graph/topics")
    if status != 200 or not isinstance(listing, dict) or not listing.get("topics"):
        record("P2 Topic Detail 可读", None, f"没有话题可查（status={status}）")
        return
    topic = listing["topics"][0]
    topic_id = topic["topic_id"]
    status, detail = http("GET", f"/api/graph/topics/{topic_id}")
    if status != 200 or not isinstance(detail, dict):
        record("P2 Topic Detail 可读", False, f"status={status} {detail}")
        return
    fields = ["summary", "keywords", "last_activity", "message_count"]
    missing = [f for f in fields if f not in detail]
    record("P2 详情层字段齐全", not missing, f"缺：{missing}" if missing else f"{fields} 都在")
    record(
        "P2 详情层使用真实数据",
        isinstance(detail.get("keywords"), list) and isinstance(detail.get("message_count"), int),
        f"keywords={len(detail.get('keywords') or [])} 个，message_count={detail.get('message_count')}",
    )
    inlined = [f for f in detail.get("fragments", []) if f.get("messages")]
    record("P2 详情不内联原文", not inlined, f"{len(inlined)} 个片段内联了 messages")


# -- P3 Fragment 原文按需分页 ------------------------------------------------


def check_fragment_raw() -> None:
    # 用 planet overview（数据层无上限）找一段「超过一页」的原文，
    # 因为话题列表接口只给有限条、且很多话题的片段都很短。
    status, listing = http("GET", "/api/planet/overview")
    if status != 200 or not isinstance(listing, dict):
        status, listing = http("GET", "/api/graph/topics")
    if status != 200 or not isinstance(listing, dict):
        record("P3 原文分页", None, "拿不到话题列表")
        return
    fragment_id = None
    total = 0
    for topic in listing.get("topics", [])[:80]:
        topic_key = topic.get("topic_id")
        if not topic_key:
            continue
        status, detail = http("GET", f"/api/graph/topics/{topic_key}")
        if status != 200 or not isinstance(detail, dict):
            continue
        for frag in detail.get("fragments", []):
            if int(frag.get("message_count") or 0) > 2:
                fragment_id, total = frag["fragment_id"], int(frag["message_count"])
                break
        if fragment_id:
            break
    if not fragment_id:
        record("P3 原文分页", None, "没有超过一页的片段可验证")
        return
    status, page1 = http("GET", f"/api/fragments/{fragment_id}/messages?offset=0&limit=1")
    ok = (
        status == 200
        and isinstance(page1, dict)
        and len(page1.get("messages", [])) == 1
        and page1.get("total") == total
    )
    record(
        "P3 第一页按需返回",
        ok,
        f"total={page1.get('total') if isinstance(page1, dict) else '?'} 返回 1 条（limit=1）",
    )
    status, page2 = http("GET", f"/api/fragments/{fragment_id}/messages?offset=1&limit=1")
    ids1 = {m["id"] for m in page1.get("messages", [])} if isinstance(page1, dict) else set()
    ids2 = {m["id"] for m in page2.get("messages", [])} if isinstance(page2, dict) else set()
    record(
        "P3 继续读取拿到下一段",
        bool(ids2) and not (ids1 & ids2),
        f"第一页 {len(ids1)} 条、第二页 {len(ids2)} 条，无重复={not (ids1 & ids2)}",
    )


# -- P4 高影响知识的「忽略」 -------------------------------------------------


def check_knowledge_ignore() -> None:
    status, created = http(
        "POST",
        "/api/knowledge",
        body={"category": "general_fact", "content": "第三阶段验收：这条用于验证忽略链路"},
    )
    if status != 200 or not isinstance(created, dict):
        record("P4 忽略链路", False, f"创建知识失败：{status} {created}")
        return
    knowledge_id = created["knowledge"]["id"]
    status, ignored = http("POST", f"/api/knowledge/{knowledge_id}/ignore")
    record("P4 忽略接口可用", status == 200, f"POST ignore -> {status} {ignored}")

    status, listing = http("GET", "/api/knowledge")
    item = None
    if isinstance(listing, dict):
        item = next((k for k in listing.get("knowledge", []) if k["id"] == knowledge_id), None)
    record(
        "P4 忽略后状态为已撤销",
        bool(item) and item.get("state") == "revoked",
        f"state={item.get('state') if item else '找不到条目'}",
    )
    status, again = http("POST", f"/api/knowledge/{knowledge_id}/ignore")
    record("P4 重复忽略是幂等的", status == 200, f"第二次 -> {status}")


# -- P5 / P6 真实一轮 --------------------------------------------------------


def record_p6(approval: dict) -> None:
    """P6：审批界面必须能回答「想做什么 / 会访问什么 / 一次性还是长期」。"""
    payload = approval.get("payload", {}) or {}
    kind = approval.get("kind")
    if kind in ("tool_execution", "computer"):
        ok = bool(payload.get("description")) and "access" in payload and "scope" in payload
        detail = (
            f"kind={kind} description={payload.get('description')!r} "
            f"access={payload.get('access')} scope={payload.get('scope')}"
        )
    elif kind in ("tool_create", "credential_grant"):
        # 工具注册 / 凭据授权：界面按 kind 判定为长期生效，这里至少有中文说明与能力清单
        ok = bool(payload.get("description") or payload.get("name")) and "capabilities" in payload
        detail = (
            f"kind={kind} name={payload.get('name')!r} "
            f"capabilities={len(payload.get('capabilities') or [])} 条"
        )
    else:
        # 「继续 / 停止」这类预算确认不是操作审批，五问不适用
        ok = None
        detail = f"kind={kind}（不是操作审批，五问不适用）"
    record("P6 审批内容可理解", ok, detail)


def check_live_turn(message: str) -> None:
    collector = EventCollector()
    collector.start()
    status, accepted = http(
        "POST",
        "/api/turns",
        body={"message": message, "topic_id": None},
    )
    if status != 200 or not isinstance(accepted, dict) or not accepted.get("turn_id"):
        record("P5 真实一轮被受理", False, f"POST /api/turns -> {status} {accepted}")
        collector.stop()
        return
    turn_id = accepted["turn_id"]
    record("P5 真实一轮被受理", True, f"turn_id={turn_id} status={accepted.get('status')}")

    # 边等边处理：审批到了就先验收并应答（不然工具只能等到自己的 45s 超时）
    deadline = time.time() + 300
    end = None
    handled_approval = False
    while time.time() < deadline:
        end = collector.wait_for_turn_end(turn_id, timeout=1)
        if end is not None:
            break
        if not handled_approval:
            pending = [
                e
                for e in collector.of("APPROVAL_REQUIRED")
                if (e.get("data", {}).get("approval", {}) or {}).get("turn_id") == turn_id
                or e.get("data", {}).get("turn_id") == turn_id
            ]
            if pending:
                approve = pending[-1]["data"].get("approval", pending[-1]["data"])
                record_p6(approve)
                approval_id = approve.get("approval_id")
                if approval_id:
                    http(
                        "POST",
                        f"/api/approvals/{approval_id}/respond",
                        body={"decision": "rejected"},
                    )
                handled_approval = True
        time.sleep(0.3)
    collector.stop()
    if end is None:
        record("P5 真实一轮收尾", False, "5 分钟内没有等到 TURN_END")
        return
    # 只统计本轮的事件：SSE 连接会重放缓冲区里最近的事件（这是重连语义，不是重复）
    def in_turn(event_type: str) -> list[dict]:
        return [
            e for e in collector.of(event_type) if e.get("data", {}).get("turn_id") == turn_id
        ]

    starts = in_turn("TURN_START")
    ends = in_turn("TURN_END")
    record(
        "P5 恰好一次 TURN_START / TURN_END",
        len(starts) == 1 and len(ends) == 1,
        f"start={len(starts)} end={len(ends)} status={end.get('data', {}).get('status')}",
    )
    caps = collector.of("CAPABILITY")
    record(
        "P5 能力模式有事件（正常状态不打扰用户）",
        bool(caps),
        f"CAPABILITY adapter={caps[-1]['data'].get('adapter') if caps else '无'}",
    )
    tool_starts = in_turn("TOOL_START")
    tool_ends = in_turn("TOOL_END")
    if tool_starts:
        start_ids = [e["data"].get("call_id") for e in tool_starts]
        end_ids = [e["data"].get("call_id") for e in tool_ends]
        paired = all(cid in end_ids for cid in start_ids)
        record(
            "P5 工具事件按 call_id 配对",
            paired and all(cid for cid in start_ids),
            f"TOOL_START {len(tool_starts)} 个 / TOOL_END {len(tool_ends)} 个，call_id 配齐={paired}",
        )
        durations = [e["data"].get("duration_ms") for e in tool_ends]
        record(
            "P5 TOOL_END 带耗时",
            all(isinstance(d, int | float) for d in durations) if durations else False,
            f"duration_ms={durations}",
        )
    else:
        record("P5 工具事件按 call_id 配对", None, "这一轮模型没有调用工具")

    usage = in_turn("USAGE")
    record("P5 用量事件存在", bool(usage), f"USAGE {len(usage)} 条")

    # APPROVAL_REQUIRED 的 turn_id 在嵌套的 approval 对象里（与其它事件不同）
    if not handled_approval:
        record("P6 审批内容可理解", None, "这一轮没有触发审批（无法强制）")


# -- P7 事件线上格式 ---------------------------------------------------------


def check_live_knowledge_candidate(message: str) -> None:
    """P8：高影响知识候选必须**在回答完成之后**进入对话，并且可以忽略。

    知识提炼发生在**片段封块**时，所以先把封块阈值临时降到 1 轮，让这一轮结束
    就触发封块与提炼，跑完再恢复原值。真实链路：回答 → 封块 → 提炼 → 候选事件。
    """
    status, current = http("GET", "/api/settings/memory")
    original = int(current["fragment_max_turns"]) if isinstance(current, dict) else 10
    http("PUT", "/api/settings/memory", body={"fragment_max_turns": 1})
    collector = EventCollector()
    collector.start()
    try:
        status, accepted = http("POST", "/api/turns", body={"message": message, "topic_id": None})
        if status != 200 or not isinstance(accepted, dict) or not accepted.get("turn_id"):
            record("P8 高影响知识候选进对话", False, f"POST /api/turns -> {status} {accepted}")
            return
        turn_id = accepted["turn_id"]
        end = collector.wait_for_turn_end(turn_id, timeout=420)
        if end is None:
            record("P8 高影响知识候选进对话", False, "没有等到本轮 TURN_END")
            return
        candidates = [
            e
            for e in collector.of("KNOWLEDGE_CANDIDATE")
            if e.get("data", {}).get("turn_id") == turn_id
        ]
        if not candidates:
            record(
                "P8 高影响知识候选进对话",
                None,
                "这一轮模型没有产出高影响类别候选（候选由模型判断，无法强制）",
            )
            return
        data = candidates[0]["data"]
        record(
            "P8 高影响知识候选进对话",
            bool(data.get("knowledge_id") and data.get("content")),
            f"category={data.get('category')} content={str(data.get('content'))[:40]!r}",
        )
        # 忽略 → 状态为已撤销；同一内容不应再作为候选出现
        knowledge_id = data["knowledge_id"]
        status, ignored = http("POST", f"/api/knowledge/{knowledge_id}/ignore")
        record("P8 候选可以忽略", status == 200, f"POST ignore -> {status} {ignored}")

        collector2 = EventCollector()
        collector2.start()
        status, second = http("POST", "/api/turns", body={"message": message, "topic_id": None})
        second_id = second.get("turn_id") if isinstance(second, dict) else None
        if second_id:
            collector2.wait_for_turn_end(second_id, timeout=420)
            repeats = [
                e
                for e in collector2.of("KNOWLEDGE_CANDIDATE")
                if e.get("data", {}).get("turn_id") == second_id
            ]
            record(
                "P8 忽略后不再重复提示",
                not repeats,
                f"第二轮候选事件 {len(repeats)} 条",
            )
        collector2.stop()
        for e in collector2.events:
            if (e.get("data", {}).get("approval", {}) or {}).get("approval_id"):
                http(
                    "POST",
                    f"/api/approvals/{e['data']['approval']['approval_id']}/respond",
                    body={"decision": "rejected"},
                )
    finally:
        collector.stop()
        http("PUT", "/api/settings/memory", body={"fragment_max_turns": original})


def check_event_wire_format() -> None:
    collector = EventCollector()
    collector.start()
    probes = {
        "TOOL_CREATE_STATUS": {
            "group_id": "dev_probe",
            "phase": "building",
            "label": "正在构建",
            "detail": "验收探针",
        },
        "KNOWLEDGE_CANDIDATE": {
            "knowledge_id": "kn_probe",
            "category": "user_profile",
            "content": "验收探针",
            "impact": "high",
        },
        "CREDENTIAL_STATUS": {"status": "unavailable", "scope": "main_loop", "message": "验收探针"},
    }
    for event_type, payload in probes.items():
        http("POST", f"/api/events/test?event_type={event_type}", body=payload)
    time.sleep(1.5)
    collector.stop()
    for event_type, _ in probes.items():
        hits = collector.of(event_type)
        record(
            f"P7 {event_type} 线上格式",
            bool(hits) and "data" in hits[-1] and hits[-1]["data"],
            f"收到 {len(hits)} 条",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-turn", action="store_true", help="额外跑一轮真实对话（会调用你配置的模型）")
    parser.add_argument(
        "--live-turn-message",
        default="请用 echo 工具回显 ping，然后一句话回答，不要调用其它工具。",
        help="真实一轮发给模型的用户消息（默认只要求一次 echo 调用）",
    )
    parser.add_argument(
        "--live-knowledge",
        action="store_true",
        help="额外跑 P8：真实触发高影响知识候选（会临时把封块阈值降到 1 轮，跑完恢复）",
    )
    parser.add_argument(
        "--live-knowledge-message",
        default="请记住：我更喜欢简洁、不啰嗦的解释风格，不要长篇大论。",
        help="P8 用来触发长期偏好候选的用户消息",
    )
    args = parser.parse_args()

    try:
        status, health = http("GET", "/api/health")
    except urllib.error.URLError as exc:
        print(f"backend 未运行（{exc}）：先执行 python scripts/e2e_up.py")
        return 1
    if status != 200:
        print(f"backend 健康检查失败：{status} {health}")
        return 1

    check_memory_settings()
    check_topic_detail()
    check_fragment_raw()
    check_knowledge_ignore()
    check_event_wire_format()
    if args.live_turn:
        check_live_turn(args.live_turn_message)
    else:
        record("P5 真实一轮", None, "未启用（加 --live-turn）")
        record("P6 审批内容可理解", None, "未启用（加 --live-turn）")
    if args.live_knowledge:
        check_live_knowledge_candidate(args.live_knowledge_message)
    else:
        record("P8 高影响知识候选进对话", None, "未启用（加 --live-knowledge）")

    failed = [r for r in results if r[0] == "FAIL"]
    not_run = [r for r in results if r[0] == "NOT RUN"]
    print("\n==== 汇总 ====")
    print(f"PASS {len(results) - len(failed) - len(not_run)} / FAIL {len(failed)} / NOT RUN {len(not_run)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
