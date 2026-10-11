"""收尾轮 Lead 分层验收 · 第③层（API/数据库）：真实 HTTP + 真实临时库。

跑法（服务需已用显式临时数据目录启动）：
    .venv/Scripts/python.exe scripts/final-lead-verify/final-lead-api-journey.py http://127.0.0.1:8734

验证 M4 完整链路的真实 HTTP 行为（不使用测试客户端）：
1) 保存（无影响）→ 200；
2) 改动执行中任务依赖的材料、不带确认 → 409 impact_confirmation_required，且不落库；
3) impact-check → 200 + checkId；带 confirm 保存 → 200，任务暂停并保留进度；
4) 提交带过期 baseStateVersion → 409 stale_state，不产生成功的提交记录；
5) 提交带当前 baseStateVersion → 正常处理；delivery.delivered 未被伪造。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8734").rstrip("/")
RESULTS: list[tuple[str, bool, str]] = []


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8") or "{}"
        try:
            return err.code, json.loads(raw)
        except json.JSONDecodeError:
            return err.code, {"raw": raw}


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))


def main() -> int:
    boards = call("GET", "/api/interactive/boards")[1]
    board_id = (boards.get("boards") or [{}])[0].get("id")
    assert board_id, "no board: " + json.dumps(boards, ensure_ascii=False)

    stamp = "2026-10-09T00:00:00Z"
    material_a = {"id": "m_a", "kind": "file", "content": "材料 A", "meta": {"name": "a.txt"},
                  "x": 0, "y": 0, "w": 200, "h": 120, "checked": False, "hidden": False,
                  "folded": False, "bookmarked": False, "deleted": False,
                  "createdAt": stamp, "updatedAt": stamp}
    material_b = dict(material_a, id="m_b", content="材料 B", meta={"name": "b.txt"})
    state = {"boardId": board_id, "seq": 0, "updatedAt": stamp,
             "cards": [material_a, material_b], "groups": [], "links": [], "selection": []}
    status, _ = call("PUT", "/api/interactive/boards/" + board_id + "/state", {"state": state, "reason": "seed"})
    check("1 普通保存返回 200", status == 200, "status=" + str(status))

    call("POST", "/api/interactive/boards/" + board_id + "/intents", {"demo": True})
    intents = call("GET", "/api/interactive/boards/" + board_id + "/intents")[1]["intents"]
    combine = next(i for i in intents if not i["dependsOn"])
    approved = call("POST", "/api/interactive/intents/" + combine["id"] + "/approve", {})[1]
    check("2 演示意图批准成功", approved.get("ok") is True, json.dumps(approved, ensure_ascii=False)[:120])
    first_status = call("GET", "/api/interactive/boards/" + board_id + "/intents")[1]["intents"][0]["status"]
    check("3 批准后进入执行中", first_status in ("running", "paused"), "status=" + str(first_status))

    live = call("GET", "/api/interactive/boards/" + board_id + "/state")[1]
    seq_before = live["seq"]
    candidate = dict(live["state"])
    candidate["cards"] = [dict(c, content="材料 A（替换）") if c["id"] == "m_a" else c for c in candidate["cards"]]
    status409, body409 = call("PUT", "/api/interactive/boards/" + board_id + "/state", {"state": candidate, "reason": "material"})
    detail = body409.get("detail") if isinstance(body409, dict) else None
    check("4 不带确认改材料被服务端门拒绝",
          status409 == 409 and isinstance(detail, dict) and detail.get("error") == "impact_confirmation_required",
          "status=" + str(status409) + " body=" + json.dumps(body409, ensure_ascii=False)[:160])
    after409 = call("GET", "/api/interactive/boards/" + board_id + "/state")[1]
    check("5 被拒绝的保存没有落库",
          after409["seq"] == seq_before and after409["state"]["cards"][0]["content"] == "材料 A",
          "seq " + str(seq_before) + " -> " + str(after409["seq"]))

    checkresp = call("POST", "/api/interactive/boards/" + board_id + "/impact-check",
                     {"stateVersion": seq_before, "changeSet": {"state": candidate}})[1]
    check("6 影响预判返回可确认句柄", bool(checkresp.get("checkId")) and checkresp.get("ok") is True,
          json.dumps(checkresp, ensure_ascii=False)[:160])
    confirmed = call("PUT", "/api/interactive/boards/" + board_id + "/state",
                     {"state": candidate, "reason": "material",
                      "confirm": {"checkId": checkresp.get("checkId"), "stateVersion": checkresp.get("stateVersion")}})
    check("7 带确认的保存落库成功", confirmed[0] == 200,
          "status=" + str(confirmed[0]) + " body=" + json.dumps(confirmed[1], ensure_ascii=False)[:160])
    statuses = {i["id"]: i["status"] for i in call("GET", "/api/interactive/boards/" + board_id + "/intents")[1]["intents"]}
    check("8 执行中任务被暂停", statuses.get(combine["id"]) == "paused", "status=" + str(statuses.get(combine["id"])))

    fresh = call("GET", "/api/interactive/boards/" + board_id + "/state")[1]
    stale = call("POST", "/api/interactive/boards/" + board_id + "/submissions", {"baseStateVersion": 1})[1]
    stale_detail = stale.get("detail") if isinstance(stale, dict) else None
    check("9 过期版本提交被拒绝",
          isinstance(stale_detail, dict) and stale_detail.get("error") == "stale_state",
          json.dumps(stale, ensure_ascii=False)[:160])
    submissions = call("GET", "/api/interactive/boards/" + board_id + "/submissions")[1]["submissions"]
    check("10 被拒绝的提交没有产生成功记录",
          all(s.get("status") != "succeeded" for s in submissions), "submissions=" + str(len(submissions)))
    ok_submit = call("POST", "/api/interactive/boards/" + board_id + "/submissions", {"baseStateVersion": fresh["seq"]})
    check("11 当前版本提交被正常处理",
          ok_submit[0] == 200 and ok_submit[1].get("status") in ("succeeded", "empty", "duplicate"),
          json.dumps(ok_submit[1], ensure_ascii=False)[:160])
    check("12 delivery.delivered 没有被伪造为 true",
          ok_submit[1].get("delivery", {}).get("delivered") is not True,
          json.dumps(ok_submit[1].get("delivery"), ensure_ascii=False)[:160])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        print(("PASS " if ok else "FAIL ") + name + (("  " + detail) if not ok else ""))
    print("")
    print("结果：" + str(passed) + "/" + str(len(RESULTS)) + " 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
