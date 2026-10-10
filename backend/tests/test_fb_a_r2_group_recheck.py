"""最终边界 R2（P1）：前项在后项克隆等待期间被删，集合仍返回成功（缺整组最终复核）。

用户可见规则（Lead 冻结）：
* 所有等待结束后、**放行前**按当下事实做**整组复核**：存在、身份/版本、用户与
  topic/turn 归属、该类型既有契约要求的文件可用性；
* 放行集合 = **实际绑定集合**：任一项失效 → 按既有集合契约整轮拒绝并完整补偿，
  新克隆/绑定不得半成功；
* 不复活用户删除的附件、不覆盖其他 turn 的绑定、不删旧 turn 的历史副本；
* 复核 + 放行在同一个**无 await 的提交段**里；
* 纯文本 [] 可发送、历史 missing 引用按既有降级重试不变。

反例形状（Lead/fb-e 实测）：先绑定 a（这一步没有 await）→ b 的重试克隆被闸门卡住 →
等待期间删除 a → 放行后回执 bound=[a, b 的克隆]、rejected=[]。

覆盖顺序：direct→clone（本文件）与 clone→direct（交换顺序）；前项移动、权限变化、
改归属、操作版本变化、克隆失败。

运行：cd backend; uv run --frozen pytest -q -p no:warnings tests/test_fb_a_r2_group_recheck.py
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent.services import attachments as attachments_mod
from agent.services.attachments import STATE_READY, AttachmentService


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _ready(svc: AttachmentService, path: Path, topic: str = "t1"):
    return svc.run_prepare(svc.prepare(str(path), topic_id=topic).id)


def _decode_output(raw: bytes) -> str:
    """解码 icacls 的字节输出：解码本身绝不能再抛异常（否则掩盖真正的装置故障）。"""
    for encoding in ("utf-8", "mbcs", "latin-1"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _whoami() -> str:
    """当下解析执行身份；解析不出来就没有可用的主体，必须大声失败。"""
    done = subprocess.run(["whoami"], capture_output=True)
    if done.returncode != 0:
        raise AssertionError(
            "whoami 解析失败（拒读装置无法确定主体）："
            f"退出码 {done.returncode}\nSTDOUT:\n{_decode_output(done.stdout)}\nSTDERR:\n{_decode_output(done.stderr)}"
        )
    user = _decode_output(done.stdout).strip()
    if not user:
        raise AssertionError("whoami 没有输出主体标识，拒读装置不可用")
    return user


def _icacls(*args: str):
    """执行 icacls 并强制检查返回码；失败必须带退出码与 stdout/stderr。"""
    cmd = ["icacls", *[str(part) for part in args]]
    done = subprocess.run(cmd, capture_output=True)
    if done.returncode != 0:
        raise AssertionError(
            "icacls 调用失败：\n"
            f"命令：{' '.join(cmd)}\n退出码：{done.returncode}\n"
            f"STDOUT:\n{_decode_output(done.stdout)}\nSTDERR:\n{_decode_output(done.stderr)}"
        )
    return done


def _readable(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _deny_read(path: Path) -> None:
    """造真实拒读并**自证生效**；不生效就跳过（绝不把无效装置当成权限反例）。

    Windows 上**不动继承**：直接 icacls <path> /deny <user>:(RD)。

    为什么是 (RD) 而不是 (R)：icacls 的 (R) 连 READ_CONTROL 一起拒；pytest 的 tmp_path
    在 Windows 上是「只有 OWNER RIGHTS」的 ACL，而普通用户 token 里 Administrators 是
    仅用于拒绝（UAC 过滤），此时 (R) 会让 icacls 自己都读不到安全描述符、/remove:d 返回
    rc=5 Access denied —— 装置再也恢复不了（本机实测）。只拒 FILE_READ_DATA(RD) 精确地
    让 open/read 抛 PermissionError，而恢复动作始终可行。
    上一版先 /inheritance:r + /grant:r <user>:(F) 再 /deny：icacls 会把那条 grant
    改写成去掉 R 的 (W,D,WDAC,WO,X,DC)，于是只 /remove:d 仍读不了，而"再 grant 回来"
    在部分 runner 上没有生效（check=False 吞掉失败）→ run 38033354514 的 F17/F18 两条红。
    """
    if sys.platform == "win32":
        user = _whoami()  # 当下解析；诊断信息里带上这个主体
        _icacls(str(path), "/deny", f"{user}:(RD)")  # 只拒 FILE_READ_DATA，恢复始终可行
    else:
        if os.geteuid() == 0:
            pytest.skip("root 下 chmod 不构成权限反例（chmod 无效）")
        path.chmod(0o000)
    if _readable(path):
        _allow_read(path)
        pytest.skip("本机权限装置无法真正拒读（可能以特权/备份权限运行）；不作为反例")


def _allow_read(path: Path) -> None:
    """Windows 恢复只做 icacls <path> /remove:d <user>（继承从未被破坏，读权限自动回来）。

    恢复必须**复核可读**：恢复不了要大声失败（带 icacls 输出），绝不静默放过，
    也绝不把装置故障当成产品缺陷或"测试通过"；/reset 只作兜底。
    """
    if sys.platform == "win32":
        user = _whoami()
        _icacls(str(path), "/remove:d", user)
        if not _readable(path):
            _icacls(str(path), "/reset")  # 兜底：重建继承
            if not _readable(path):
                raise AssertionError(
                    f"拒读装置无法恢复可读：{path}（主体 {user}）；"
                    "icacls /remove:d 与 /reset 之后仍然读不到。这是装置故障，不是产品缺陷。"
                )
        return
    path.chmod(0o600)
    if not _readable(path):
        raise AssertionError(f"拒读装置无法恢复可读：{path}（chmod 600 之后仍然读不到）")


def _force_copy_fallback(monkeypatch) -> None:
    """强制走 shutil.copyfile 退路：那才是 f15/f16 已经用过的、可下闸门的缝。"""

    def refuse_link(*args, **kwargs):
        raise OSError(errno.EPERM, "hardlink not permitted")

    monkeypatch.setattr(attachments_mod.os, "link", refuse_link)


def _gated_copyfile(monkeypatch):
    """把克隆的文件 I/O 卡在闸门上：返回 (entered, release)。"""
    real = shutil.copyfile
    entered = threading.Event()
    release = threading.Event()

    def gated(src_path, dst_path, *args, **kwargs):
        entered.set()
        if not release.wait(15):
            raise AssertionError("测试闸门没有被放开")
        return real(src_path, dst_path, *args, **kwargs)

    monkeypatch.setattr(attachments_mod.shutil, "copyfile", gated)
    return entered, release


async def _wait_entered(entered) -> None:
    assert await asyncio.to_thread(entered.wait, 15), "克隆没有进入文件 I/O 闸门"


def _orphans(data_dir: Path, keep: list[str | None]) -> list[Path]:
    keep_set = {str(Path(p)) for p in keep if p}
    return [p for p in data_dir.rglob("*") if p.is_file() and str(p) not in keep_set]


def _receipt(outcome) -> dict:
    return outcome.as_receipt()


def _assert_all_rejected(outcome, *, expect_ids: list[str], why: str) -> None:
    receipt = _receipt(outcome)
    assert receipt["bound_attachment_ids"] == [], (
        why + "：放行集合必须 == 实际绑定集合（回执里不得留 bound）",
        receipt,
    )
    assert list(outcome) == [], (why, [getattr(a, "id", a) for a in outcome])
    assert outcome.bound == [], receipt
    rejected_ids = [item for item, _reason in outcome.rejected]
    for expected in expect_ids:
        assert expected in rejected_ids, (why, "被拒的 id 必须在 rejected 里", receipt)
    assert receipt["rejected"], receipt


# -- 1. 红：direct→clone 顺序，前项（已绑定）在克隆等待期间被删除 --------------------


async def test_first_bound_item_deleted_during_second_clone_rejects_all(
    svc, tmp_path, monkeypatch
):
    """Lead/fb-e 的形状：第 1 条没有 await 就绑定成功，第 2 条克隆期间删掉第 1 条。"""
    a = _ready(svc, _write(tmp_path / "一.bin", b"a" * 2048), topic="t1")
    b = _ready(svc, _write(tmp_path / "二.bin", b"b" * 2048), topic="t1")
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[b.id], topic_id="t1")

    # 第 1 条：**本轮的直绑**（另一话题，先绑到本轮）
    await svc.bind_for_turn(turn_id="turn_new", attachment_ids=[a.id], topic_id="t1")

    _force_copy_fallback(monkeypatch)
    entered, release = _gated_copyfile(monkeypatch)
    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[a.id, b.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    await _wait_entered(entered)  # b 的克隆已经过了「提交前复核」，正卡在文件 I/O

    # 等待期间用户删掉第 1 条（它已经是本轮的绑定）
    removed = svc.delete(a.id, purge_copy=True)
    assert removed["removed"] is True

    release.set()
    outcome = await asyncio.wait_for(task, timeout=20)

    _assert_all_rejected(outcome, expect_ids=[a.id], why="等待期间第 1 条被删除，整轮必须拒绝")
    # 不得复活被删的记录
    assert svc.get(a.id, check=False) is None, "被删的附件不得被复活"
    # 本轮不得留下任何绑定/克隆
    assert svc.list(turn_id="turn_new", check=False) == [], "被拒的轮次不得留下任何行"
    # 原历史副本与源行归属不变
    assert svc.get(b.id).turn_id == "turn_old"
    assert Path(b.stored_path).read_bytes() == b"b" * 2048
    # 不得留下无人认领的克隆副本
    leftovers = _orphans(tmp_path / "data", [b.stored_path])
    assert leftovers == [], ("整轮被拒后不得留下克隆资产", [str(p) for p in leftovers])


# -- 2. 交换顺序：clone→direct（先克隆第 1 条，直绑第 2 条） -----------------------


async def test_clone_then_direct_order_rejects_all_when_first_deleted(
    svc, tmp_path, monkeypatch
):
    """交换 direct/clone 顺序：克隆在前、直绑在后；克隆等待期间删掉**已经直绑的那条**。"""
    a = _ready(svc, _write(tmp_path / "甲.bin", b"a" * 2048), topic="t1")
    b = _ready(svc, _write(tmp_path / "乙.bin", b"b" * 2048), topic="t1")
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[a.id], topic_id="t1")
    await svc.bind_for_turn(turn_id="turn_new", attachment_ids=[b.id], topic_id="t1")

    _force_copy_fallback(monkeypatch)
    entered, release = _gated_copyfile(monkeypatch)
    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[a.id, b.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    await _wait_entered(entered)  # a 的克隆在跑；b 还没轮到（复核在克隆之后）

    removed = svc.delete(b.id, purge_copy=True)
    assert removed["removed"] is True

    release.set()
    outcome = await asyncio.wait_for(task, timeout=20)

    _assert_all_rejected(outcome, expect_ids=[b.id], why="克隆等待期间第 2 条被删除，整轮必须拒绝")
    assert svc.get(b.id, check=False) is None
    assert svc.list(turn_id="turn_new", check=False) == []
    assert svc.get(a.id).turn_id == "turn_old", "源行归属不得被本轮改掉"
    assert Path(a.stored_path).read_bytes() == b"a" * 2048
    leftovers = _orphans(tmp_path / "data", [a.stored_path])
    assert leftovers == [], ("克隆必须完整补偿", [str(p) for p in leftovers])


# -- 3. 前项副本在等待期间变得不可读（权限变化） ---------------------------------


async def test_first_item_unreadable_during_second_clone_rejects_all(
    svc, tmp_path, monkeypatch
):
    import subprocess
    import sys

    a = _ready(svc, _write(tmp_path / "前置.bin", b"a" * 1024), topic="t1")
    b = _ready(svc, _write(tmp_path / "后置.bin", b"b" * 1024), topic="t1")
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[b.id], topic_id="t1")
    await svc.bind_for_turn(turn_id="turn_new", attachment_ids=[a.id], topic_id="t1")
    stored = Path(a.stored_path)

    _force_copy_fallback(monkeypatch)
    entered, release = _gated_copyfile(monkeypatch)
    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[a.id, b.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    await _wait_entered(entered)

    _deny_read(stored)  # 真拒读并自证：Windows 上只 /deny，不动继承

    try:
        release.set()
        outcome = await asyncio.wait_for(task, timeout=20)
    finally:
        _allow_read(stored)  # 恢复只做 /remove:d，并复核可读；恢复不了大声失败

    _assert_all_rejected(outcome, expect_ids=[a.id], why="等待期间前置附件变得不可读，整轮必须拒绝")
    # 整轮回滚：本轮写下的行全部补偿回上一个归属（a 之前就已经绑在本轮 → 恢复后仍留在本轮）
    assert [row.id for row in svc.list(turn_id="turn_new", check=False)] == [a.id], (
        "被拒的轮次不得留下新克隆/新绑定（a 是上一轮就已经绑上的行，恢复后仍在本轮）"
    )
    assert svc.get(a.id).turn_id == "turn_new", "拒绝不得把已经绑上的行改回旧归属（它是同一轮）"


# -- 4. 前项被移动到别的轮（归属变化） --------------------------------------------


async def test_first_item_taken_by_another_turn_during_clone_rejects_all(
    svc, tmp_path, monkeypatch
):
    a = _ready(svc, _write(tmp_path / "移动甲.bin", b"a" * 1024), topic="t1")
    b = _ready(svc, _write(tmp_path / "移动乙.bin", b"b" * 1024), topic="t1")
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[b.id], topic_id="t1")
    await svc.bind_for_turn(turn_id="turn_new", attachment_ids=[a.id], topic_id="t1")

    _force_copy_fallback(monkeypatch)
    entered, release = _gated_copyfile(monkeypatch)
    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[a.id, b.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    await _wait_entered(entered)

    # 等待期间 a 的归属被改到别的话题/轮次（直接写库模拟并发的另一条路径）
    svc._update(a.id, turn_id="turn_moved", topic_id="t9")

    release.set()
    outcome = await asyncio.wait_for(task, timeout=20)

    _assert_all_rejected(outcome, expect_ids=[a.id], why="等待期间归属被改走，整轮必须拒绝")
    assert svc.get(a.id).turn_id == "turn_moved", "拒绝不得把别人的归属抢回来"
    assert svc.list(turn_id="turn_new", check=False) == []
    leftovers = _orphans(tmp_path / "data", [a.stored_path, b.stored_path])
    assert leftovers == [], ("归属变化也必须完整补偿", [str(p) for p in leftovers])


# -- 5. 克隆本身失败（既有集合契约不得退化） ---------------------------------------


async def test_clone_failure_still_rejects_whole_group(svc, tmp_path, monkeypatch):
    a = _ready(svc, _write(tmp_path / "失败甲.bin", b"a" * 1024), topic="t1")
    b = _ready(svc, _write(tmp_path / "失败乙.bin", b"b" * 1024), topic="t1")
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[a.id], topic_id="t1")
    await svc.bind_for_turn(turn_id="turn_new", attachment_ids=[b.id], topic_id="t1")

    _force_copy_fallback(monkeypatch)

    def broken_copyfile(src_path, dst_path, *args, **kwargs):
        raise OSError(errno.ENOSPC, "no space left on device（受控）")

    monkeypatch.setattr(attachments_mod.shutil, "copyfile", broken_copyfile)
    outcome = await svc.bind_for_turn(
        turn_id="turn_new",
        attachment_ids=[a.id, b.id],
        topic_id="t1",
        retry_of_turn_id="turn_old",
    )
    _assert_all_rejected(outcome, expect_ids=[a.id], why="克隆失败必须整轮拒绝")
    # b 早先就已经绑在本轮：它被恢复（不是新写入），a 的克隆失败被完整补偿
    assert [row.id for row in svc.list(turn_id="turn_new", check=False)] == [b.id], (
        "克隆失败必须整轮拒绝且不留下新行",
        [row.id for row in svc.list(turn_id="turn_new", check=False)],
    )
    assert svc.get(a.id).turn_id == "turn_old", "源行归属不得被改掉"
    leftovers = _orphans(tmp_path / "data", [a.stored_path, b.stored_path])
    assert leftovers == [], [str(p) for p in leftovers]


# -- 6. 绿守卫：正常多附件 / 重试克隆仍然有效 --------------------------------------


async def test_normal_multi_attachment_and_retry_clone_still_work(svc, tmp_path):
    a = _ready(svc, _write(tmp_path / "正常甲.bin", b"a" * 1024), topic="t1")
    b = _ready(svc, _write(tmp_path / "正常乙.bin", b"b" * 1024), topic="t1")

    first = await svc.bind_for_turn(turn_id="turn_direct", attachment_ids=[a.id, b.id], topic_id="t1")
    assert first.rejected == []
    assert set(first.bound) == {a.id, b.id}
    for att in first:
        assert att.state == STATE_READY

    # 重试复用：旧轮的附件克隆到新轮（含引用型不走文件 I/O 的分支）
    retry = await svc.bind_for_turn(
        turn_id="turn_retry",
        attachment_ids=[a.id, b.id],
        topic_id="t1",
        retry_of_turn_id="turn_direct",
    )
    assert retry.rejected == []
    assert len(retry.bound) == 2 and set(retry.bound) != {a.id, b.id}
    clones = [svc.get(item) for item in retry.bound]
    assert {c.source_attachment_id for c in clones} == {a.id, b.id}
    assert {c.turn_id for c in clones} == {"turn_retry"}
    # 旧轮的历史副本与源行归属不变
    assert svc.get(a.id).turn_id == "turn_direct" and svc.get(b.id).turn_id == "turn_direct"
    assert Path(a.stored_path).read_bytes() == b"a" * 1024


async def test_plain_text_empty_list_and_missing_history_unchanged(svc, tmp_path):
    a = _ready(svc, _write(tmp_path / "纯文本.bin", b"a" * 1024), topic="t1")

    # 显式空列表：这一轮不带附件（纯文字发送）
    empty = await svc.bind_for_turn(turn_id="turn_plain", attachment_ids=[], topic_id="t1")
    assert empty.bound == [] and empty.rejected == []
    assert svc.get(a.id).turn_id is None, "纯文字发送不得把遗留附件绑进来"

    # 历史 missing 引用：既有降级行为不变（重试时如实登记新行）
    source = _write(tmp_path / "会消失.bin", b"m" * 4096)
    ref = svc.run_prepare(svc.prepare(str(source), topic_id="t1").id)
    await svc.bind_for_turn(turn_id="turn_hist", attachment_ids=[ref.id], topic_id="t1")
    Path(ref.stored_path).unlink()
    gone = svc.get(ref.id)
    assert gone.state == "missing", gone.state

    # 既有降级行为不变：副本真的丢了就不许复用（结构化拒绝 + 准确的 id），
    # 且失败必须完整补偿（不留克隆行、不留无人认领的文件），源行归属不动。
    retry = await svc.bind_for_turn(
        turn_id="turn_hist2",
        attachment_ids=[ref.id],
        topic_id="t1",
        retry_of_turn_id="turn_hist",
    )
    assert retry.bound == [] and retry.rejected, (
        "历史 missing 的复用必须按既有契约结构化拒绝",
        retry.as_receipt(),
    )
    assert [item for item, _reason in retry.rejected] == [ref.id], retry.rejected
    assert svc.list(turn_id="turn_hist2", check=False) == [], "被拒的重试不得留下克隆行"
    assert svc.get(ref.id).turn_id == "turn_hist", "源行归属不得被改掉"
    assert _orphans(tmp_path / "data", [a.stored_path]) == [], (
        "被拒的重试不得留下无人认领的文件",
        [str(p) for p in _orphans(tmp_path / "data", [a.stored_path])],
    )

# -- 7. 真实 /api/turns 入口：整轮拒绝且模型调用数 0 -------------------------------


def _load_provider_module():
    import importlib.util

    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "scripts" / "verify_stream_provider.py"
    assert path.exists(), f"验证资产缺失：{path}"
    spec = importlib.util.spec_from_file_location("verify_stream_provider_r2", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def provider():
    module = _load_provider_module()
    server = module.StreamingProvider(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture()
def client_app(tmp_path: Path, provider):
    """真实应用（含 /api/turns 与 DELETE /api/attachments）：模型一律走本地 fake provider。"""
    from agent.api.server import create_app
    from agent.config import Settings
    from agent.credentials.store import MemoryKeyring
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "r2_api.db")
    apply_migrations(conn)
    application = create_app(Settings(data_dir=tmp_path / "data"), conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    return application


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=180.0
    )


async def _prepare_credential(client, provider) -> None:
    resp = await client.post(
        "/api/credentials",
        json={
            "provider": "custom",
            "endpoint": "http://127.0.0.1:%d/v1" % provider.server_port,
            "secret": "sk-r2-fake-0001",
            "default_model": "verify-model",
            "tags": ["main-loop"],
        },
    )
    assert resp.status_code == 200 and (resp.json().get("verify") or {}).get("ok"), resp.text[:200]


async def _provider_calls(provider) -> int:
    import httpx

    base = "http://127.0.0.1:%d" % provider.server_port
    body = await asyncio.to_thread(lambda: httpx.get(base + "/__log", timeout=10).json())
    return len(body.get("requests") or [])


async def _await_ready(attachments, attachment_id: str, timeout: float = 60.0) -> str:
    """等首次准备收敛（**事件循环线程**轮询，不把数据库访问挪到第二个线程）。"""
    import time

    deadline = time.monotonic() + timeout
    state = "prepared"
    while time.monotonic() < deadline:
        att = attachments.get(attachment_id, check=False)
        if att is None:
            return "missing"
        state = att.state
        if state in ("ready", "failed", "changed", "missing", "cancelled"):
            return state
        await asyncio.sleep(0.02)
    return state


async def test_api_turns_rejects_whole_group_when_bound_item_deleted_mid_clone(
    client_app, provider, tmp_path: Path
):
    """真实 /api/turns 入口：预检通过后第 1 条被删、第 2 条克隆放行
    → 409 + bound 为空 + 模型调用数 0 + 无半绑定/无半成功克隆。

    前置状态（两条附件、turn_old/turn_new）直接用服务 API 摆好，只让**被测路径**
    （POST /api/turns 的 bind 段、DELETE 附件）走真实 HTTP。轮询也留在事件循环线程，
    不制造「第二个线程碰数据库」的假警报。
    """
    attachments = client_app.state.ctx.attachments
    a_id = attachments.prepare(
        str(_write(tmp_path / "api-一.txt", b"a" * 1024)), topic_id="t1"
    ).id
    b_id = attachments.prepare(
        str(_write(tmp_path / "api-二.txt", b"b" * 1024)), topic_id="t1"
    ).id
    a = attachments.run_prepare(a_id)
    b = attachments.run_prepare(b_id)
    assert a.state == STATE_READY and b.state == STATE_READY

    await attachments.bind_for_turn(turn_id="turn_old", attachment_ids=[b.id], topic_id="t1")
    await attachments.bind_for_turn(turn_id="turn_new", attachment_ids=[a.id], topic_id="t1")
    assert attachments.get(a.id).turn_id == "turn_new"

    entered = threading.Event()
    release = threading.Event()
    real_finish = attachments_mod.AttachmentService._finish_copy_clone

    async def gated_finish(self, plan):
        entered.set()
        if not await asyncio.to_thread(release.wait, 30):
            raise AssertionError("测试闸门没有被放开")
        return await real_finish(self, plan)

    async with _client(client_app) as client:
        await _prepare_credential(client, provider)
        calls_before = await _provider_calls(provider)

        attachments_mod.AttachmentService._finish_copy_clone = gated_finish
        try:
            send = asyncio.create_task(
                client.post(
                    "/api/turns",
                    json={
                        "message": "重试复用二；期间第 1 条被用户删除",
                        "attachment_ids": [a.id, b.id],
                        "topic_id": "t1",
                        "retry_of_turn_id": "turn_old",
                    },
                )
            )
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 30), timeout=45)
            # 嘎门打开的这一刻：第 1 条（无 await 的直绑）已经写进 turn_new，第 2 条正在克隆
            row = attachments.get(a.id, check=False)
            assert row is not None and row.turn_id == "turn_new", row

            # 等待期间用户删掉第 1 条 —— 走真实 HTTP（DELETE 只清 QIO 自己的副本）
            deleted = await client.delete("/api/attachments/%s" % a.id)
            assert deleted.status_code in (200, 204), deleted.text[:200]
            assert attachments.get(a.id, check=False) is None

            release.set()
            response = await asyncio.wait_for(send, timeout=60)
        finally:
            attachments_mod.AttachmentService._finish_copy_clone = real_finish

        calls_during = (await _provider_calls(provider)) - calls_before

    assert response.status_code == 409, (
        "等待期间第 1 条被删除，却仍然受理（放行集合 != 实际绑定集合）",
        {"status": response.status_code, "body": response.text[:300]},
    )
    detail = response.json().get("detail") or {}
    assert detail.get("bound_attachment_ids") == [], detail
    rejected_ids = [item.get("id") for item in detail.get("rejected") or []]
    assert a.id in rejected_ids, detail
    assert calls_during == 0, (
        "被拒的轮次不得调用模型（模型调用数必须为 0）",
        {"calls_during": calls_during, "detail": detail},
    )
    assert attachments.get(a.id, check=False) is None, "被删的记录不得复活"
    assert attachments.get(b.id).turn_id == "turn_old", "源行归属不得被改掉"
    assert attachments.list(turn_id="turn_new", limit=50, check=False) == [], (
        "被拒的轮次不得留下任何绑定/克隆"
    )
