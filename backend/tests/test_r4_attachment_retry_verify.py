r"""D 独立验证（R4 问题二）：重试复用附件 —— 新轮必须能**真的读出原附件内容**。

契约来源：docs/plans/2026-10-07-three-remaining-fixes.md §1.2（冻结）。

用户可见规则（plan §3）：
1. 原轮失败 → 点重试 → 新轮读取工具**真实读出原附件内容**（不是只看标签/ID）；
2. 原文件删除后副本重试仍成功（绝不重新依赖用户原文件）；
3. 原轮历史仍可打开副本且归属不变；
4. 无法附加时必须**明确说明**（结构化失败 + 每个附件的人话原因），不允许静默丢弃；
5. 防偷换：跨话题 / 已绑到别的轮次的 id 不得被拿走。

基线（ccb5734）现状：bind_for_turn 只接收 (turn_id, ids, topic_id)、返回 list[Attachment]，
对不满足条件的 id **静默跳过**；/api/turns 没有 retry_of_turn_id 语义，也没有
bound_attachment_ids / rejected 回执 —— 因此「重试拿到新 id 并能读出内容」在修复前应当是**红的**。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r4_attachment_retry_verify.py -q
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

TERMINAL = ("ready", "failed", "changed", "missing", "cancelled")
MARKER = "R4-D 原始附件内容：只有这个副本里才有的标记 7f3a"


@pytest.fixture()
def client(tmp_path: Path):
    conn = connect(tmp_path / "r4_retry.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    # 隔离：Settings(data_dir=...) 会被全局默认覆盖（实测仍是 D:\QIO-data），钉住附件服务的落盘根目录。
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    with TestClient(app) as c:
        yield c


def _wait_state(client: TestClient, attachment_id: str, want: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        last = client.get(f"/api/attachments/{attachment_id}").json()["attachment"]
        if last["state"] == want:
            return last
        time.sleep(0.02)
    raise AssertionError(f"附件没有在 {timeout}s 内变成 {want}：{last}")


def _wait_terminal(client: TestClient, attachment_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        last = client.get(f"/api/attachments/{attachment_id}").json()["attachment"]
        if last["state"] in TERMINAL:
            return last
        time.sleep(0.02)
    raise AssertionError(f"附件没有在 {timeout}s 内进入终态：{last}")


def _register(client: TestClient, path: Path, *, topic_id: str | None = None) -> dict:
    body: dict = {"source_path": str(path)}
    if topic_id:
        body["topic_id"] = topic_id
    resp = client.post("/api/attachments", json=body)
    assert resp.status_code == 200, resp.text
    att = resp.json()["attachment"]
    return _wait_terminal(client, att["id"])


def _submit(
    client: TestClient,
    message: str,
    ids: list[str] | None = None,
    *,
    topic_id: str | None = None,
    retry_of: str | None = None,
):
    body: dict = {"message": message}
    if ids is not None:
        body["attachment_ids"] = ids
    if topic_id:
        body["topic_id"] = topic_id
    if retry_of:
        body["retry_of_turn_id"] = retry_of
    return client.post("/api/turns", json=body)


def _bound_ids(resp) -> list[str]:
    """受理回执里**实际绑定**的附件 id（契约 §1.2：以回执为准）。"""
    payload = resp.json()
    explicit = payload.get("bound_attachment_ids")
    if isinstance(explicit, list):
        return [str(x) for x in explicit]
    return [str(a.get("id")) for a in (payload.get("attachments") or []) if a.get("id")]


def _rejected(resp) -> list:
    payload = resp.json()
    if isinstance(payload.get("rejected"), list):
        return payload["rejected"]
    detail = payload.get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("rejected"), list):
        return detail["rejected"]
    return []


def _read_attachment_text(client: TestClient, attachment_id: str) -> str:
    """用**真实读取工具**读内容（不是只看标签/ID）。"""
    from agent.tools.attachment_tools import ReadAttachmentTool

    ctx = client.app.state.ctx
    tool = ReadAttachmentTool(ctx.attachments, active_turn_id=lambda: None)
    result = asyncio.run(tool.run(attachment_id=attachment_id))
    assert result.ok, ("读取工具失败", result.error)
    return str(result.content)


# ---- 1. 重试必须克隆新 id、复用已保存副本、内容真的读得出来 -------------------------


def test_retry_reuses_saved_copy_and_new_turn_can_read_the_content(client: TestClient, tmp_path: Path):
    src = tmp_path / "原始附件.txt"
    src.write_text(MARKER, encoding="utf-8")
    att = _register(client, src)
    assert att["state"] == "ready", att

    first = _submit(client, "第一轮（会失败）", [att["id"]])
    assert first.status_code == 200 and first.json().get("accepted") is True, first.text
    turn1 = first.json()["turn_id"]
    assert att["id"] in _bound_ids(first), ("第一轮没绑上附件", first.text)

    # 原文件删除：重试只能靠已保存的副本
    src.unlink()

    resp = _submit(client, "重试", [att["id"]], retry_of=turn1)
    assert resp.status_code < 400, (
        "重试被拒绝（契约 §1.2：绑定在原轮上的副本应当被克隆复用）",
        resp.status_code,
        resp.text[:400],
    )
    new_ids = [x for x in _bound_ids(resp) if x != att["id"]]
    assert new_ids, (
        "重试没有为新轮绑定附件（契约 §1.2：必须新建一条记录、复用已保存的副本）",
        {"status": resp.status_code, "body": resp.text[:400], "rejected": _rejected(resp)},
    )
    new_id = new_ids[0]
    row = client.get(f"/api/attachments/{new_id}").json()["attachment"]
    assert row["sha256"] == att["sha256"], ("新副本内容与原件不一致", row)
    assert row["name"] == att["name"], row
    assert row["state"] == "ready", ("新副本应当是 ready（复用已保存副本，不依赖用户原文件）", row)

    # 数据库里要能看出克隆来源（追加迁移，不改历史迁移）
    conn = client.app.state.ctx.conn
    columns = {r[1] for r in conn.execute("PRAGMA table_info(attachments)").fetchall()}
    assert "source_attachment_id" in columns, (
        "attachments 表没有 source_attachment_id 列（契约 §1.2：克隆要指向原行，且只能追加迁移）",
        sorted(columns),
    )
    source = conn.execute(
        "SELECT source_attachment_id FROM attachments WHERE id = ?", (new_id,)
    ).fetchone()
    assert source and source[0] == att["id"], ("克隆来源没有指向原行", source)

    # 新轮真的能读出内容
    text = _read_attachment_text(client, new_id)
    assert MARKER in text, ("新轮读取工具读不到原附件内容", text[:200])

    # 原轮归属不变（行级事实）
    old = client.get(f"/api/attachments/{att['id']}").json()["attachment"]
    assert old["turn_id"] == turn1, ("原行归属被改写", old)
    # 「历史里仍带原附件」需要**真的执行过一轮**（有用户消息行）才谈得上：
    # 本夹具没有 provider，messages 表是空的，所以这里不做历史断言（C 的诊断已核实
    # message rows: []）。历史富集路径由 C 的 tests/test_attachment_history.py 与
    # 阶段二实机（假厂商 + 真跑一轮）覆盖。


# ---- 2. 已绑到别的轮次 / 跨话题：结构化拒绝，绝不静默丢弃 ---------------------------


def test_stealing_attachment_bound_to_another_turn_is_rejected(client: TestClient, tmp_path: Path):
    src = tmp_path / "别的轮的附件.txt"
    src.write_text(MARKER, encoding="utf-8")
    att = _register(client, src)
    first = _submit(client, "第一轮", [att["id"]])
    assert first.status_code == 200, first.text

    resp = _submit(client, "想偷用第一轮的附件（没有 retry_of_turn_id）", [att["id"]])
    assert resp.status_code in (409, 422), (
        "契约 §1.2：请求里的 id 不满足条件时必须结构化失败（409/422），不得静默丢弃后照常受理",
        resp.status_code,
        resp.text[:400],
    )
    body = resp.text
    assert att["id"] in body, ("失败响应必须点出是哪个附件", body[:300])
    assert not resp.json().get("accepted"), ("被拒绝的请求不得返回 accepted", body[:300])
    assert not resp.json().get("turn_id"), ("被拒绝的请求不得入队（不得返回 turn_id）", body[:300])
    assert _rejected(resp), ("失败响应必须带每个附件的人话原因", body[:300])


def test_cross_topic_attachment_is_rejected_with_reason(client: TestClient, tmp_path: Path):
    ctx = client.app.state.ctx
    # 建新话题会**同时把当前话题切过去**（C 的诊断核实过）。要测「跨话题」，
    # 就先把原话题 id 抓在手里，提交时显式用它当 topic_id —— 它与附件所属话题必然不同。
    original_topic = ctx.current_topic()
    other_topic = ctx.topics.nodes.create_topic("别的话题")
    other_id = getattr(other_topic, "id", None) or str(other_topic)
    assert original_topic != other_id, (
        "装置前提不成立：两个话题 id 相同，这条用例测不到跨话题拒绝",
        {"original": original_topic, "other": other_id},
    )
    src = tmp_path / "别话题的附件.txt"
    src.write_text(MARKER, encoding="utf-8")
    att = _register(client, src, topic_id=other_id)

    resp = _submit(client, "跨话题取件", [att["id"]], topic_id=original_topic)
    assert resp.status_code in (409, 422), (
        "契约 §1.2：跨话题的 id 不得被拿来当本轮附件（也不得被静默丢弃）",
        resp.status_code,
        resp.text[:400],
    )
    assert att["id"] in resp.text and _rejected(resp), resp.text[:400]


# ---- 3. 引用型：克隆必须重新检查当前可用性；副本缺失不得静默成功 ---------------------


def test_reference_clone_rechecks_availability_after_source_removed(client: TestClient, tmp_path: Path):
    big = tmp_path / "大引用.bin"
    with big.open("wb") as handle:
        handle.truncate(100_000_001)
    att = _register(client, big)
    assert att["kind"] == "reference", att
    first = _submit(client, "第一轮带引用", [att["id"]])
    assert first.status_code == 200, first.text
    turn1 = first.json()["turn_id"]

    big.rename(tmp_path / "大引用.bin.moved")
    resp = _submit(client, "重试（源文件已移走）", [att["id"]], retry_of=turn1)
    assert resp.status_code < 400, (resp.status_code, resp.text[:300])
    new_ids = [x for x in _bound_ids(resp) if x != att["id"]]
    assert new_ids, ("引用型重试也要有新记录（并如实记 missing/changed）", resp.text[:300])
    row = client.get(f"/api/attachments/{new_ids[0]}").json()["attachment"]
    assert row["state"] in ("missing", "changed"), (
        "引用型克隆沿用了发送时的旧状态（契约 §1.2：必须重新检查当前可用性）",
        row,
    )
    assert row.get("error"), ("缺失/变化必须带原因", row)


def test_missing_stored_copy_is_not_silently_accepted(client: TestClient, tmp_path: Path):
    src = tmp_path / "副本会丢.txt"
    src.write_text(MARKER, encoding="utf-8")
    att = _register(client, src)
    first = _submit(client, "第一轮", [att["id"]])
    assert first.status_code == 200, first.text
    turn1 = first.json()["turn_id"]

    ctx = client.app.state.ctx
    stored = ctx.attachments.get(att["id"], check=False).stored_path
    assert stored, "副本型附件必须有 stored_path"
    Path(stored).unlink()

    resp = _submit(client, "重试（副本已丢）", [att["id"]], retry_of=turn1)
    if resp.status_code >= 400:
        assert att["id"] in resp.text, ("失败必须点出是哪个附件", resp.text[:300])
        return
    new_ids = [x for x in _bound_ids(resp) if x != att["id"]]
    assert new_ids, (
        "副本已经不在磁盘上，重试却既不失败也不绑定（静默丢附件：用户以为带上了）",
        resp.text[:400],
    )
    got = client.get(f"/api/attachments/{new_ids[0]}/content")
    assert got.status_code == 200 and MARKER.encode() in got.content, (
        "绑定了读不出内容的附件（不能把「有 id」当成「带上了」）",
        got.status_code,
        resp.text[:200],
    )

# ---- 4. 阶段一补齐：多附件 / 重复点击 / 排队 / 刷新后重试 / resend -------------------


def _new_ids(resp, requested: list[str]) -> list[str]:
    wanted = set(requested)
    return [x for x in _bound_ids(resp) if x not in wanted]


def test_multi_attachment_retry_clones_every_item(client: TestClient, tmp_path: Path):
    """≥2 个附件、混合 copy/reference：重试要逐个克隆，副本能读出内容，引用如实给状态。"""
    a = tmp_path / "多附件-A.txt"
    a.write_text(MARKER + " A", encoding="utf-8")
    b = tmp_path / "多附件-B.txt"
    b.write_text(MARKER + " B", encoding="utf-8")
    big = tmp_path / "多附件-引用.bin"
    with big.open("wb") as handle:
        handle.truncate(100_000_001)

    atts = [_register(client, a), _register(client, b), _register(client, big)]
    assert [x["kind"] for x in atts] == ["copy", "copy", "reference"], atts
    ids = [x["id"] for x in atts]

    first = _submit(client, "多附件第一轮", ids)
    assert first.status_code == 200, first.text
    turn1 = first.json()["turn_id"]
    assert set(_bound_ids(first)) == set(ids), first.text

    resp = _submit(client, "多附件重试", ids, retry_of=turn1)
    assert resp.status_code < 400, (resp.status_code, resp.text[:300])
    new_ids = _new_ids(resp, ids)
    assert len(new_ids) == len(ids), (
        "重试没有把每个附件都克隆一遍（契约 §1.2：逐个新建记录、复用已保存副本）",
        {"requested": ids, "bound": _bound_ids(resp), "rejected": _rejected(resp)},
    )
    rows = [client.get(f"/api/attachments/{x}").json()["attachment"] for x in new_ids]
    copies = [r for r in rows if r["kind"] == "copy"]
    refs = [r for r in rows if r["kind"] == "reference"]
    assert len(copies) == 2 and len(refs) == 1, rows
    for row, source in zip(copies, [atts[0], atts[1]]):
        assert row["sha256"] == source["sha256"], row
        got = client.get(f"/api/attachments/{row['id']}/content")
        assert got.status_code == 200 and MARKER.encode() in got.content, (
            "克隆出来的副本读不出内容", row["id"], got.status_code, got.text[:200]
        )
    for source in atts:
        old = client.get(f"/api/attachments/{source['id']}").json()["attachment"]
        assert old["turn_id"] == turn1, ("原行归属被改写", old)


def test_double_retry_does_not_error_or_corrupt(client: TestClient, tmp_path: Path):
    """重复点击重试：不得 5xx、不得把原行归属搞乱（可复用同一份副本，也可结构化拒绝）。"""
    src = tmp_path / "连点两次.txt"
    src.write_text(MARKER, encoding="utf-8")
    att = _register(client, src)
    first = _submit(client, "第一轮", [att["id"]])
    turn1 = first.json()["turn_id"]

    r1 = _submit(client, "重试（第一次点击）", [att["id"]], retry_of=turn1)
    r2 = _submit(client, "重试（第二次点击）", [att["id"]], retry_of=turn1)

    for resp in (r1, r2):
        assert resp.status_code < 500, (
            "重复点击重试导致 5xx（用户会看到报错，而不是一个能懂的结果）", resp.status_code, resp.text[:300]
        )
    old = client.get(f"/api/attachments/{att['id']}").json()["attachment"]
    assert old["turn_id"] == turn1, ("重复点击把原行归属改乱了", old)
    cloned = set(_new_ids(r1, [att["id"]])) | set(_new_ids(r2, [att["id"]]))
    assert cloned, ("两次点击都没有克隆出新副本（契约 §1.2：至少要能重试一次）", r1.text[:200], r2.text[:200])
    for cid in cloned:
        row = client.get(f"/api/attachments/{cid}").json()["attachment"]
        assert row["sha256"] == att["sha256"], row


def test_retry_while_source_turn_still_open(client: TestClient, tmp_path: Path):
    """上一轮还在跑（或还在排队）时重试：同样要克隆，不能因为源轮没结束就丢附件。"""
    src = tmp_path / "排队中重试.txt"
    src.write_text(MARKER, encoding="utf-8")
    att = _register(client, src)

    first = _submit(client, "第一轮（可能还在跑）", [att["id"]])
    turn1 = first.json()["turn_id"]
    snapshot = client.get("/api/turns/queue").json()
    still_open = bool(
        (snapshot.get("running") or {}).get("turn_id") == turn1
        or any((q or {}).get("turn_id") == turn1 for q in snapshot.get("queued") or [])
    )

    resp = _submit(client, "还在跑就重试", [att["id"]], retry_of=turn1)
    assert resp.status_code < 400, (resp.status_code, resp.text[:300])
    assert _new_ids(resp, [att["id"]]), (
        "源轮还没结束时重试拿不到附件",
        {"source_turn_still_open_at_submit": still_open, "snapshot": snapshot, "body": resp.text[:300]},
    )


def test_retry_after_history_refresh_uses_message_attachment_ids(client: TestClient, tmp_path: Path):
    """刷新后重试：前端会用历史里那条消息的附件 id 再发一次 —— 后端必须照契约克隆。"""
    src = tmp_path / "刷新后重试.txt"
    src.write_text(MARKER, encoding="utf-8")
    att = _register(client, src)
    first = _submit(client, "第一轮", [att["id"]])
    turn1 = first.json()["turn_id"]

    # 等到那一轮真的写进历史（用户消息落库后历史才带附件）
    deadline = time.time() + 15
    from_history: list[str] = []
    while time.time() < deadline:
        page = client.get("/api/session/context").json()
        from_history = [
            a["id"]
            for m in page.get("messages") or []
            for a in (m.get("attachments") or [])
        ]
        if att["id"] in from_history:
            break
        time.sleep(0.05)
    if att["id"] not in from_history:
        pytest.skip(
            "装置受限：API 级夹具没有能跑通的 provider，turn 不会写用户消息 → 历史里没有附件行。"
            "「刷新后重试」由阶段二实机（假厂商 + 真实重试入口）覆盖；这里不把装置问题算成产品红。"
        )

    resp = _submit(client, "刷新后重试", from_history, retry_of=turn1)
    assert resp.status_code < 400, (resp.status_code, resp.text[:300])
    assert _new_ids(resp, from_history), (
        "按历史里的附件 id 重试没有克隆出新副本", resp.text[:300], _rejected(resp)
    )


def test_resend_interrupted_turn_reuses_attachments(client: TestClient, tmp_path: Path):
    """resend 路径（进程中断恢复）：重发同样不得丢附件。

    装置说明（如实写在这里）：本用例用一条**合成的中断轮**（journal 记 interrupted + 附件绑在它的
    turn_id 上），避免和后台真实轮次的终态写入竞态。它假设 resend 会按「被中断那一轮的 turn_id」
    找回当时绑定的附件 —— 这是契约 §1.2「重放时同样按规则重新归属，不得丢附件」的最小可判定写法；
    若实现改为只认 user_message_id，请告知，我会同步调整装置。
    """
    ctx = client.app.state.ctx
    src = tmp_path / "中断恢复重发.txt"
    src.write_text(MARKER, encoding="utf-8")
    att = _register(client, src)

    turn1 = "turn_r4_interrupted_1"
    ctx.attachments.bind_for_turn(turn1, [att["id"]], topic_id=None)
    journal = ctx.turn_journal
    journal.accepted(turn_id=turn1, message="被进程掐断的那一条（带附件）")
    journal.running(turn1)
    journal.terminal(turn1, "cancelled", reason="shutdown")
    assert journal.recoverable(turn1) is not None, "没有造出可恢复记录（测试装置失效）"

    resp = client.post(f"/api/turns/{turn1}/resend")
    assert resp.status_code == 200, (resp.status_code, resp.text[:300])
    assert _new_ids(resp, [att["id"]]), (
        "resend 丢掉了附件（契约 §1.2：中断恢复的重发同样按规则重新归属）",
        resp.text[:400], _rejected(resp),
    )

