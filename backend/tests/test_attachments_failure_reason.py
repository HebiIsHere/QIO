"""R6 问题四：失败原因保留与可用操作（契约 §1.4）—— **先写红**。

复现的缺陷：真实上传路由里「建目录失败」→ 记录 `failed`（原因「目标位置不可用：…」），
但 `GET /api/attachments/{id}` 走 `_check`（services/attachments.py:1682-1685）时，
`kind == copy` 且没有可读 `stored_path` → **一律改判 `missing`**，原始原因被
「QIO 保存的副本文件已经不在了」覆盖 —— 用户重新打开界面只看到一句通用文案，
而这条其实「从来没有保存成功过」，重试才是正确的下一步（不是「重新定位」）。

冻结规则（§1.4）：
1. `failed` 在 GET / list / history / payload 可用性检查下**粘性**：不得自动改成
   missing/ready，原始 error 不得被通用文案覆盖；
2. 只有**显式重试成功**或**可验证的恢复**（stored_path 存在且 sha256 与登记值一致）才转 ready；
3. `ready → 文件消失` 继续如实 missing；`prepared` / `cancelled` 行为不变；
4. 可用操作按 kind 区分：copy 从未保存成功 → 只有重试；reference failed/missing/changed →
   重新定位；浏览器字节上传（无 source_path）→ 不能自行从原地址恢复。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_attachments_failure_reason.py -q
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.attachments import DiskOutcome
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

TERMINAL = ("ready", "failed", "changed", "missing", "cancelled")


@pytest.fixture()
def client(tmp_path: Path):
    conn = connect(tmp_path / "failure_reason.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _ctx(client: TestClient):
    return client.app.state.ctx


def _wait_terminal(client: TestClient, attachment_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        last = client.get(f"/api/attachments/{attachment_id}").json()["attachment"]
        if last["state"] in TERMINAL:
            return last
        time.sleep(0.02)
    raise AssertionError(f"附件没有在 {timeout}s 内进入终态：{last}")


def _wait_state(client: TestClient, attachment_id: str, want: str, timeout: float = 20.0) -> dict:
    """等一个**指定**状态：重试场景里 failed 本身就是终态，不能拿终态当完成信号。"""
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        last = client.get(f"/api/attachments/{attachment_id}").json()["attachment"]
        if last["state"] == want:
            return last
        time.sleep(0.02)
    raise AssertionError(f"附件没有在 {timeout}s 内变成 {want}：{last}")


def _block_target_directory(ctx) -> Path:
    """让「目标目录」建不出来：把 attachments 根下本年的那层占成一个**文件**。

    这是真实世界的条件（目标位置被同名文件占住），不用打桩：
    `target.parent.mkdir(parents=True, exist_ok=True)` 会抛 ENOTDIR/EACCES 一类 OSError。
    """
    year = datetime.now(timezone.utc).astimezone().strftime("%Y")
    blocker = ctx.attachments.root / year
    blocker.parent.mkdir(parents=True, exist_ok=True)
    if blocker.exists():
        blocker.unlink()
    blocker.write_bytes("占位".encode("utf-8"))
    return blocker


def _upload(client: TestClient, ctx, *, name: str = "报告.txt", body: bytes = b"hello") -> str:
    """走**真实上传路由**；用测试专用的探针拿到刚登记的 id（不改实现行为）。"""
    created: list[str] = []
    real_begin = ctx.attachments.begin_upload

    def spy(**kwargs):
        att = real_begin(**kwargs)
        created.append(att.id)
        return att

    ctx.attachments.begin_upload = spy  # type: ignore[method-assign]
    try:
        resp = client.post(
            "/api/attachments/upload",
            content=body,
            # 头只能是 ASCII：文件名按前端同样的方式 URL 编码（路由里 unquote）
            headers={"X-QIO-Name": quote(name), "X-QIO-Topic-Id": "t1"},
        )
    finally:
        ctx.attachments.begin_upload = real_begin  # type: ignore[method-assign]
    assert created, f"上传路由没有登记附件：{resp.status_code} {resp.text[:200]}"
    return created[0], resp


# -- 1. 建目录失败：failed 粘性、原始原因不被覆盖 -----------------------------


def test_directory_failure_keeps_failed_and_original_reason(client: TestClient, tmp_path: Path):
    ctx = _ctx(client)
    _block_target_directory(ctx)

    att_id, resp = _upload(client, ctx)
    assert resp.status_code >= 400, f"建目录失败必须让这次上传明确失败：{resp.status_code} {resp.text}"

    # 原始数据库状态（**任何 GET 之前**）：上传路由如实记了 failed + 真实原因
    raw = ctx.attachments.get(att_id, check=False)
    assert raw.state == "failed", f"上传失败必须落 failed：{raw}"
    assert raw.error and "目标位置不可用" in raw.error, f"原始原因必须落库：{raw.error}"
    original_reason = raw.error

    row = _wait_terminal(client, att_id)
    assert row["state"] == "failed", row
    assert original_reason, "失败原因必须落库（用户可见）"
    assert row["stored_path"] is None, "从来没有产生有效副本"

    # 首次 GET / 重复 GET / 列表：都必须是同一条 failed 与同一个原因
    for _ in range(3):
        again = client.get(f"/api/attachments/{att_id}").json()["attachment"]
        assert again["state"] == "failed", f"failed 必须是粘性的：{again}"
        assert again["error"] == original_reason, "原始失败原因不得被通用文案覆盖"
    listed = client.get("/api/attachments").json()["attachments"]
    mine = [item for item in listed if item["id"] == att_id]
    assert mine and mine[0]["state"] == "failed" and mine[0]["error"] == original_reason

    # 服务层 payload / availability（历史页与工具读的是同一条判据）
    att = ctx.attachments.get(att_id)
    payload = ctx.attachments.payload(att, check=True)
    assert payload["state"] == "failed" and payload["error"] == original_reason
    assert ctx.attachments.get(att_id).state == "failed"


def test_local_copy_retry_after_failure_reaches_ready(client: TestClient, tmp_path: Path):
    """有真实原路径的本地文件：显式重试成功 → ready，且不再保留旧失败原因。"""
    ctx = _ctx(client)
    source = tmp_path / "本地重试.bin"
    source.write_bytes(b"retry me")
    att = ctx.attachments.prepare(str(source), topic_id="t1")
    att = ctx.attachments.run_prepare(att.id)
    assert att.state == "ready"

    # 先让它真的失败（副本被清掉 + 记录 failed，原始原因保留）
    Path(att.stored_path).unlink()
    ctx.attachments._update(att.id, state="failed", error="保存失败：磁盘配额用完了")
    failed = client.get(f"/api/attachments/{att.id}").json()["attachment"]
    assert failed["state"] == "failed" and failed["error"] == "保存失败：磁盘配额用完了"

    retry = client.post(f"/api/attachments/{att.id}/retry")
    assert retry.status_code == 200, retry.text
    row = _wait_state(client, att.id, "ready")
    assert row["error"] is None, "显式重试成功后不再保留旧失败原因"
    assert Path(row["stored_path"]).read_bytes() == b"retry me"


def test_browser_upload_failure_offers_reupload_not_a_fake_retry(client: TestClient):
    """浏览器字节上传：没有原路径 → 服务端重试**拿不到内容**，界面不能假装能重试。"""
    ctx = _ctx(client)
    _block_target_directory(ctx)
    att_id, _resp = _upload(client, ctx)
    assert _wait_terminal(client, att_id)["state"] == "failed"

    payload = client.get(f"/api/attachments/{att_id}").json()["attachment"]
    assert payload["kind"] == "copy" and payload["source_path"] is None
    assert payload["actions"] == ["reupload"], payload["actions"]
    assert payload["recoverable_from_source"] is False

    # 证据：服务端 POST /retry 对它**不可能**成功（没有字节可重放）——
    # 所以可用操作里不能出现 retry（否则界面会给出一个必然失败的动作）。
    assert client.post(f"/api/attachments/{att_id}/retry").status_code == 200
    time.sleep(0.5)  # 让它真的跑一次（预期它不可能成功）
    again = client.get(f"/api/attachments/{att_id}").json()["attachment"]
    assert again["state"] == "failed", "浏览器上传没有原路径：服务端重试不可能成功"


def test_second_failure_keeps_the_new_reason(client: TestClient):
    """再次失败保留**新的**原因（不是第一次的，也不是通用文案）。"""
    ctx = _ctx(client)
    _block_target_directory(ctx)
    att_id, _resp = _upload(client, ctx)
    first = _wait_terminal(client, att_id)["error"]

    ctx.attachments.apply_outcome(
        att_id, DiskOutcome(state="failed", error="第二次失败：磁盘配额用完了")
    )

    row = client.get(f"/api/attachments/{att_id}").json()["attachment"]
    assert row["state"] == "failed"
    assert row["error"] == "第二次失败：磁盘配额用完了"
    assert row["error"] != first


# -- 2. 既有行为不变 ---------------------------------------------------------


def test_ready_copy_deleted_later_becomes_missing(client: TestClient):
    ctx = _ctx(client)
    att_id, resp = _upload(client, ctx)
    assert resp.status_code == 200, resp.text
    ready = _wait_terminal(client, att_id)
    assert ready["state"] == "ready"

    Path(ready["stored_path"]).unlink()  # 曾成功保存，后来副本丢了

    row = client.get(f"/api/attachments/{att_id}").json()["attachment"]
    assert row["state"] == "missing", "ready → 文件消失 继续如实 missing（行为不变）"


def test_prepared_and_cancelled_are_untouched(client: TestClient):
    ctx = _ctx(client)
    att = ctx.attachments.begin_upload(name="准备中.txt", topic_id="t1")
    assert ctx.attachments.get(att.id).state == "prepared"
    assert ctx.attachments.get(att.id).state == "prepared", "prepared 不越权改状态"

    ctx.attachments.apply_outcome(
        att.id, DiskOutcome(state="cancelled", error="已取消（可以重试）")
    )
    again = ctx.attachments.get(att.id)
    assert again.state == "cancelled" and again.error == "已取消（可以重试）"


# -- 3. 可验证的恢复 ---------------------------------------------------------


def test_failed_copy_recovers_only_with_matching_sha256(client: TestClient, tmp_path: Path):
    ctx = _ctx(client)
    att_id, _resp = _upload(client, ctx)
    ready = _wait_terminal(client, att_id)
    assert ready["state"] == "ready"
    stored = Path(ready["stored_path"])
    body = stored.read_bytes()
    digest = ready["sha256"]
    assert digest

    # 人为造出「记录是 failed、但磁盘上副本仍在且内容一致」：可验证的恢复 → ready
    ctx.attachments._update(att_id, state="failed", error="上传期间落库失败")
    assert ctx.attachments.get(att_id).state == "ready", "sha256 一致 → 允许恢复为 ready"

    # 内容不一致：不得声称恢复
    ctx.attachments._update(att_id, state="failed", error="上传期间落库失败")
    stored.write_bytes(body + b"tampered")
    again = ctx.attachments.get(att_id)
    assert again.state == "failed", "sha256 不一致 → 保持 failed（不得假装 ready）"
    assert again.error == "上传期间落库失败"


# -- 4. 可用操作按 kind 区分 -------------------------------------------------


def test_available_actions_distinguish_kinds(client: TestClient, tmp_path: Path):
    ctx = _ctx(client)
    # (a) 浏览器字节上传失败：没有 source_path → 只能重试，不能自行从原地址恢复
    _block_target_directory(ctx)
    att_id, _resp = _upload(client, ctx)
    _wait_terminal(client, att_id)
    payload = client.get(f"/api/attachments/{att_id}").json()["attachment"]
    assert payload["kind"] == "copy"
    # 浏览器上传没有源路径：服务端重试不可能成功（见下面那条用例的证据），
    # 唯一真能成功的动作是「重新上传」；也不得声称 QIO 能从原地址恢复。
    assert payload["actions"] == ["reupload"], payload["actions"]
    assert payload["recoverable_from_source"] is False

    # (b) 引用型附件丢失：位置是唯一依据 → 重新定位（大文件走引用档）
    source = tmp_path / "引用.bin"
    with open(source, "wb") as handle:
        handle.truncate(ctx.attachments.max_upload_bytes + 1)
    ref = ctx.attachments.prepare(str(source), topic_id="t1")
    ref = ctx.attachments.run_prepare(ref.id)
    source.unlink()
    ref_payload = client.get(f"/api/attachments/{ref.id}").json()["attachment"]
    assert ref_payload["kind"] == "reference"
    assert ref_payload["state"] == "missing"
    assert ref_payload["actions"] == ["relocate"], ref_payload["actions"]
    assert ref_payload["recoverable_from_source"] is True


def test_local_file_copy_failure_offers_relocate_too(client: TestClient, tmp_path: Path):
    """有真实原路径的 copy（老式本地文件复制）：失败后既可以重试，也可以重新定位。"""
    ctx = _ctx(client)
    source = tmp_path / "本地.bin"
    source.write_bytes(b"local")
    att = ctx.attachments.prepare(str(source), topic_id="t1")
    att = ctx.attachments.run_prepare(att.id)
    assert att.kind == "copy" and att.state == "ready"

    # 记录失败 + 副本不在（否则「sha256 一致的副本还在」会按可验证恢复转 ready）
    ctx.attachments._update(
        att.id,
        state="failed",
        error="保存失败：权限不足",
        stored_path=str(tmp_path / "已经不在了.bin"),
    )
    payload = client.get(f"/api/attachments/{att.id}").json()["attachment"]
    assert payload["state"] == "failed"
    assert "retry" in payload["actions"], payload["actions"]
    assert "relocate" in payload["actions"], payload["actions"]
    assert payload["recoverable_from_source"] is True
