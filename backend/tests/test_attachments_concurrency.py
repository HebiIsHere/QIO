"""并发不变量：同一个 sqlite 连接**永不被两个线程同时使用**（附件后台复制）。

2026-10-06 CI（backend py3.12 / windows）真事故：后台复制把整个 run_prepare 丢进
asyncio.to_thread，工作线程于是既读又写那个与整个应用共享的 sqlite 连接
（storage/db.py 用 check_same_thread=False，连接对象不是线程安全的），
结果出现 sqlite3.InterfaceError 与「刚 POST 成功、马上 GET 404」的幻影状态。
本机（Windows + py3.11）反复全绿只是时序运气 —— 所以这里的用例必须**确定性**：

* 用闸门（threading.Event）把工作线程的复制进程卡住，等到它真的进了复制再继续；
* 复制进行中，主线程在同一附件上高频 GET（正是 CI 上出 404/InterfaceError 的动作）；
* 最后断言「碰过这个 sqlite 连接的线程只有一个」。

修复前：工作线程在 run_prepare 里读行、复制后又写行 → 记录到两个线程 → 断言必红。
修复后：工作线程只跑 copy_to_disk（纯文件 I/O），数据库动作全在事件循环线程 → 单线程。
"""

from __future__ import annotations

import sqlite3
import threading
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
READS_WHILE_COPYING = 300


class _ThreadRecordingConn:
    """连接代理：记录每次 execute 的线程 id，其余语义原样转发给真连接。"""

    def __init__(self, inner: sqlite3.Connection, seen: list[int]) -> None:
        self._inner = inner
        self._seen = seen

    def execute(self, sql: str, *args, **kwargs):  # noqa: ANN002, ANN003 - 透明转发
        self._seen.append(threading.get_ident())
        return self._inner.execute(sql, *args, **kwargs)

    @property
    def in_transaction(self) -> bool:
        return self._inner.in_transaction


@pytest.fixture()
def client(tmp_path: Path):
    conn = connect(tmp_path / "concurrency.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_background_prepare_never_touches_the_shared_connection_off_the_loop_thread(
    client: TestClient, tmp_path: Path, monkeypatch
):
    ctx = client.app.state.ctx
    service = ctx.attachments

    seen: list[int] = []
    monkeypatch.setattr(service, "conn", _ThreadRecordingConn(ctx.conn, seen))

    started = threading.Event()
    release = threading.Event()
    real_copy_once = service._copy_once

    def gated_copy_once(att, **kwargs):
        started.set()  # 工作线程已经进入复制
        assert release.wait(20), "测试没有放行复制（闸门超时）"
        return real_copy_once(att, **kwargs)

    # 只在**文件 I/O** 这一层设闸门：它是新旧实现都有的缝，
    # 且此时「工作线程是否碰数据库」正好是我们要区分的那件事。
    monkeypatch.setattr(service, "_copy_once", gated_copy_once)

    source = tmp_path / "并发.txt"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    created = client.post("/api/attachments", json={"source_path": str(source)}).json()["attachment"]

    assert started.wait(20), "后台复制没有开始（后台任务没跑起来）"

    # 复制进行中：主线程高频读同一个附件。修好之前，工作线程此刻正持有并继续使用
    # 那个共享连接 —— 这里就会出现 InterfaceError / 幻影 404。
    for _ in range(READS_WHILE_COPYING):
        resp = client.get(f"/api/attachments/{created['id']}")
        assert resp.status_code == 200, f"复制期间 GET 到了 {resp.status_code}：{resp.text[:200]}"
        assert resp.json()["attachment"]["state"] in ("prepared", "ready")

    release.set()
    deadline = time.time() + 20
    state = "prepared"
    while time.time() < deadline:
        state = client.get(f"/api/attachments/{created['id']}").json()["attachment"]["state"]
        if state in TERMINAL:
            break
        time.sleep(0.02)
    assert state == "ready", f"后台复制没有进入终态：{state}"

    victims = sorted(set(seen))
    assert len(victims) == 1, (
        f"同一个 sqlite 连接被 {len(victims)} 个线程碰过：{victims} —— "
        "数据库访问必须全部发生在事件循环线程，工作线程只允许跑 copy_to_disk"
    )
