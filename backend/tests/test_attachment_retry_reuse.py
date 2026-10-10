"""R4 问题二：重试复用原轮附件（契约 §1.2）——**先写红**。

复现的缺陷（修复前）：
1. 原轮绑定了 1 个附件 → 重试时前端把**原 attachment_id** 传给新一轮，
   服务端 `_bindable` 看到 `att.turn_id == 原轮` → **静默跳过**；
2. 调用方拿不到任何结构化原因（只有一句日志），请求照旧 accepted；
3. 于是「界面有附件、模型实际没有」，重试轮的附件数为 0。

本文件按**冻结接口**写：
`bind_for_turn(*, turn_id, message_id, attachment_ids, topic_id, retry_of_turn_id=None)
-> BindOutcome(bound: list[str], rejected: list[tuple[str, str]])`

覆盖：静默丢弃 → 结构化拒绝、copy 克隆复用副本（硬链接优先 / 不重读用户原文件）、
原行归属与历史不变、reference 克隆重新检查可用性、防偷换（别轮 / 跨话题 / 不存在 /
状态不允许）、空列表与缺字段语义保留、重试轮**真实读出**附件内容。
"""

from __future__ import annotations

import asyncio
import errno
import inspect
import os
import shutil
import threading
from pathlib import Path

import pytest

from agent.services import attachments as attachments_mod
from agent.services.attachments import COPY_MAX_BYTES, AttachmentService
from agent.tools.attachment_tools import ReadAttachmentTool


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sparse(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)
    return path


def _ready_copy(svc: AttachmentService, path: Path, *, topic_id: str = "t1"):
    att = svc.prepare(str(path), topic_id=topic_id)
    return svc.run_prepare(att.id)


async def _bind(svc: AttachmentService, turn_id: str, ids, *, topic_id: str = "t1", **kw):
    """按冻结接口调用（全关键字）。R5 §1.3 起 bind_for_turn 是 async。"""
    return await svc.bind_for_turn(
        turn_id=turn_id, attachment_ids=ids, topic_id=topic_id, **kw
    )


def _tool(svc: AttachmentService, turn_id: str) -> ReadAttachmentTool:
    return ReadAttachmentTool(svc, active_turn_id=lambda: turn_id)


# -- 1. 静默丢弃 → 结构化拒绝 ------------------------------------------------


async def test_bind_reports_rejection_instead_of_silently_dropping(svc: AttachmentService, tmp_path: Path):
    """重试没带上 retry_of_turn_id：必须进 rejected，而不是「返回空列表、请求照旧 accepted」。"""
    source = _write(tmp_path / "原轮附件.txt", b"\xe5\x86\x85\xe5\xae\xb9")
    att = _ready_copy(svc, source)
    await _bind(svc, "turn_old", [att.id])

    outcome = await _bind(svc, "turn_new", [att.id], message_id="msg_new")

    assert outcome.bound == [], "没有 retry_of_turn_id 时不得把别轮的附件绑到新轮"
    assert [item[0] for item in outcome.rejected] == [att.id]
    assert "别的一轮" in outcome.rejected[0][1]
    # 原行归属不变（没有被抢走、也没有被改写）
    assert svc.get(att.id).turn_id == "turn_old"


# -- 2. copy 克隆：复用副本、原行不变、新轮真实可读 ---------------------------


async def test_retry_clones_copy_attachment_and_new_turn_reads_real_content(
    svc: AttachmentService, tmp_path: Path
):
    body = "第一行\n第二行\n".encode("utf-8")
    source = _write(tmp_path / "报告.txt", body)
    att = _ready_copy(svc, source)
    await _bind(svc, "turn_old", [att.id])

    outcome = await _bind(
        svc, "turn_new", [att.id], message_id="msg_new", retry_of_turn_id="turn_old"
    )

    assert outcome.rejected == []
    assert len(outcome.bound) == 1
    clone = svc.get(outcome.bound[0])
    assert clone is not None and clone.id != att.id
    # 新记录：归属新一轮，继承名称/大小/摘要/保存方式，并指向原行
    assert clone.turn_id == "turn_new"
    assert clone.topic_id == "t1"
    assert clone.kind == "copy"
    assert clone.original_name == att.original_name
    assert clone.size_bytes == att.size_bytes
    assert clone.sha256 == att.sha256
    assert clone.state == "ready"
    assert clone.message_id == "msg_new"
    assert clone.source_attachment_id == att.id
    # 原行归属与历史不变（副本路径也还在）
    original = svc.get(att.id)
    assert original.turn_id == "turn_old"
    assert original.stored_path == att.stored_path
    assert Path(att.stored_path).read_bytes() == body

    # 复用已保存的副本：优先硬链接（同 inode），否则至少内容一致
    new_stat = os.stat(clone.stored_path)
    old_stat = os.stat(att.stored_path)
    hardlinked = (new_stat.st_ino, new_stat.st_dev) == (old_stat.st_ino, old_stat.st_dev)
    assert hardlinked or Path(clone.stored_path).read_bytes() == body

    # 用户原文件删掉之后，重试轮仍能**真实读出**内容（不是只看标签/ID）
    source.unlink()
    refreshed = svc.get(clone.id)
    assert refreshed.state == "ready"
    result = await _tool(svc, "turn_new").run(attachment_id=clone.id, offset=0, limit=10)
    assert result.ok is True
    assert "第一行" in result.content and "第二行" in result.content
    # 归属不变：新一轮的上下文列出克隆行，原轮的上下文仍只列原行
    new_ctx = svc.turn_note("turn_new") or ""
    assert clone.id in new_ctx
    old_ctx = svc.turn_note("turn_old") or ""
    assert att.id in old_ctx
    assert clone.id not in old_ctx


async def test_retry_clone_does_not_re_read_user_source_file(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """克隆必须复用 QIO 的副本：即使用户原文件被换掉，新轮拿到的仍是当初那份。"""
    source = _write(tmp_path / "原始.txt", "原始内容".encode("utf-8"))
    att = _ready_copy(svc, source)
    await _bind(svc, "turn_old", [att.id])
    # 用户把原文件换成别的内容（副本已经与它无关）
    source.write_bytes("换掉的内容".encode("utf-8"))

    outcome = await _bind(
        svc, "turn_new", [att.id], message_id="msg_new", retry_of_turn_id="turn_old"
    )
    clone = svc.get(outcome.bound[0])
    assert Path(clone.stored_path).read_bytes() == "原始内容".encode("utf-8")


# -- 3. 防偷换：只接受「未绑定」或「绑定在 retry_of_turn_id 那一轮」 ----------


async def test_retry_never_steals_attachment_bound_to_another_turn(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "别人的.txt", b"x")
    att = _ready_copy(svc, source)
    await _bind(svc, "turn_a", [att.id])

    outcome = await _bind(svc, "turn_new", [att.id], retry_of_turn_id="turn_b")

    assert outcome.bound == []
    assert [item[0] for item in outcome.rejected] == [att.id]
    assert svc.get(att.id).turn_id == "turn_a"
    # 没有产生任何克隆行
    assert [a.id for a in svc.list(turn_id="turn_new")] == []


async def test_retry_rejects_unknown_cross_topic_and_not_bindable_ids(svc: AttachmentService, tmp_path: Path):
    ready = _ready_copy(svc, _write(tmp_path / "ready.txt", b"r"), topic_id="t1")
    other_topic = _ready_copy(svc, _write(tmp_path / "other.txt", b"o"), topic_id="t2")
    broken_source = _write(tmp_path / "gone.txt", b"g")
    broken = _ready_copy(svc, broken_source)
    Path(broken.stored_path).unlink()  # 副本丢了 → missing，不允许绑定

    outcome = await _bind(
        svc,
        "turn_new",
        ["att_missing_0001", other_topic.id, broken.id],
        message_id="msg_new",
    )

    reasons = dict(outcome.rejected)
    assert set(reasons) == {"att_missing_0001", other_topic.id, broken.id}
    assert "没有这个附件" in reasons["att_missing_0001"]
    assert "话题" in reasons[other_topic.id]
    assert "副本" in reasons[broken.id] or "不在" in reasons[broken.id]
    assert outcome.bound == []
    # 跨话题 / 副本丢失都没有被改写
    assert svc.get(other_topic.id).turn_id is None


async def test_retry_bound_ids_are_deduplicated(svc: AttachmentService, tmp_path: Path):
    att = _ready_copy(svc, _write(tmp_path / "dup.txt", b"d"))
    await _bind(svc, "turn_old", [att.id])

    outcome = await _bind(
        svc,
        "turn_new",
        [att.id, att.id],
        message_id="msg_new",
        retry_of_turn_id="turn_old",
    )

    assert len(outcome.bound) == 1
    assert outcome.rejected == []


# -- 4. reference 克隆：重新检查当前可用性与变化，不沿用旧状态 ----------------


async def test_reference_clone_rechecks_state_instead_of_inheriting(
    svc: AttachmentService, tmp_path: Path
):
    # (a) 位置没变 → 克隆 ready
    unchanged = _sparse(tmp_path / "steady.bin", COPY_MAX_BYTES + 1)
    att_a = svc.prepare(str(unchanged), topic_id="t1")
    att_a = svc.run_prepare(att_a.id)
    assert att_a.kind == "reference"
    await _bind(svc, "turn_a", [att_a.id])
    cloned_a = svc.get((await _bind(svc, "turn_new_a", [att_a.id], retry_of_turn_id="turn_a")).bound[0])
    assert cloned_a.kind == "reference"
    assert cloned_a.state == "ready"
    assert cloned_a.source_attachment_id == att_a.id
    assert cloned_a.stored_path is None

    # (b) 登记之后被改动 → 克隆如实记 changed（带原因），不是沿用 ready
    changing = _sparse(tmp_path / "moving.bin", COPY_MAX_BYTES + 1)
    att_b = svc.prepare(str(changing), topic_id="t1")
    att_b = svc.run_prepare(att_b.id)
    await _bind(svc, "turn_b", [att_b.id])
    later = os.stat(changing).st_mtime + 60
    os.utime(changing, (later, later))
    outcome_b = await _bind(svc, "turn_new_b", [att_b.id], retry_of_turn_id="turn_b")
    cloned_b = svc.get(outcome_b.bound[0])
    assert cloned_b.state == "changed"
    assert cloned_b.error and "变了" in cloned_b.error

    # (c) 文件被删掉 → 克隆如实记 missing（带原因），仍然归属新轮
    vanished = _sparse(tmp_path / "vanish.bin", COPY_MAX_BYTES + 1)
    att_c = svc.prepare(str(vanished), topic_id="t1")
    att_c = svc.run_prepare(att_c.id)
    await _bind(svc, "turn_c", [att_c.id])
    vanished.unlink()
    outcome_c = await _bind(svc, "turn_new_c", [att_c.id], retry_of_turn_id="turn_c")
    assert outcome_c.rejected == [], "源文件不可用也要建记录并如实报状态，不能既不建又不报错"
    assert len(outcome_c.bound) == 1
    cloned_c = svc.get(outcome_c.bound[0])
    assert cloned_c.turn_id == "turn_new_c"
    assert cloned_c.state == "missing"
    assert cloned_c.error and "不在原位" in cloned_c.error
    # 新轮的上下文如实说「当前不可访问」，模型不会凭文件名猜内容
    ctx_c = svc.turn_note("turn_new_c") or ""
    assert cloned_c.id in ctx_c
    assert "当前不可访问" in ctx_c
    # 原行状态由文件世界决定（check=True），归属不变
    assert svc.get(att_c.id).turn_id == "turn_c"


# -- 5. 语义保留：空列表 / 缺字段 / 旧调用形状 -------------------------------


async def test_empty_list_and_missing_field_semantics_are_preserved(svc: AttachmentService, tmp_path: Path):
    unbound = _ready_copy(svc, _write(tmp_path / "pending.txt", b"p"))

    empty = await _bind(svc, "turn_x", [])
    assert empty.bound == [] and empty.rejected == []
    assert svc.get(unbound.id).turn_id is None, "显式空列表不得落进兜底分支"

    fallback = await _bind(svc, "turn_y", None)
    assert fallback.bound == [unbound.id]
    assert fallback.rejected == []
    assert svc.get(unbound.id).turn_id == "turn_y"


async def test_retry_attachment_ids_lists_what_the_source_turn_bound(svc: AttachmentService, tmp_path: Path):
    first = _ready_copy(svc, _write(tmp_path / "one.txt", b"1"))
    second = _ready_copy(svc, _write(tmp_path / "two.txt", b"2"))
    await _bind(svc, "turn_old", [first.id, second.id])

    assert svc.retry_attachment_ids("turn_old") == [first.id, second.id]
    assert svc.retry_attachment_ids("turn_absent") == []


# -- 6. 受理前预检（route 在 submit 之前调用：rejected 非空就不入队） ------------------


async def test_precheck_reports_the_same_rejections_without_touching_anything(
    svc: AttachmentService, tmp_path: Path
):
    """预检只读：判据与 bind_for_turn 同一份，且不落库、不克隆。"""
    ready = _ready_copy(svc, _write(tmp_path / "ok.txt", b"ok"), topic_id="t1")
    other_topic = _ready_copy(svc, _write(tmp_path / "other.txt", b"o"), topic_id="t2")
    bound = _ready_copy(svc, _write(tmp_path / "bound.txt", b"b"), topic_id="t1")
    await _bind(svc, "turn_old", [bound.id])

    rejected = dict(
        svc.precheck_for_turn(
            attachment_ids=["att_missing_0001", other_topic.id, bound.id, ready.id],
            topic_id="t1",
        )
    )
    assert set(rejected) == {"att_missing_0001", other_topic.id, bound.id}
    assert "没有这个附件" in rejected["att_missing_0001"]
    assert "话题" in rejected[other_topic.id]
    assert "别的一轮" in rejected[bound.id]
    # 结构化失败的一句话（route 直接用它当 detail.message）
    message = attachments_mod.rejected_failure_message(list(rejected.items()))
    assert message.startswith("有 3 个附件没有附上：")
    assert "话题" in message
    # 只读：没有任何行被改写，也没有产生克隆
    assert svc.get(ready.id).turn_id is None
    assert svc.get(other_topic.id).turn_id is None
    assert svc.get(bound.id).turn_id == "turn_old"


async def test_precheck_accepts_retry_of_turn_and_stays_read_only(svc: AttachmentService, tmp_path: Path):
    att = _ready_copy(svc, _write(tmp_path / "retry.txt", b"r"), topic_id="t1")
    await _bind(svc, "turn_old", [att.id])

    assert svc.precheck_for_turn(
        attachment_ids=[att.id], topic_id="t1", retry_of_turn_id="turn_old"
    ) == []
    # 预检本身不克隆（克隆只在 bind_for_turn 里发生）
    assert [a.id for a in svc.list(turn_id="turn_new")] == []
    assert svc.get(att.id).turn_id == "turn_old"


def test_precheck_ignores_missing_field_fallback(svc: AttachmentService, tmp_path: Path):
    """缺字段 = 旧客户端兜底：没有显式清单可预检（兜底永远能绑或跳过）。"""
    _ready_copy(svc, _write(tmp_path / "fallback.txt", b"f"), topic_id="t1")
    assert svc.precheck_for_turn(attachment_ids=None, topic_id="t1") == []


async def test_receipt_is_always_accurate(svc: AttachmentService, tmp_path: Path):
    """Lead 复核项：回执必须永远准确 —— bound 的每一行确实绑到了本轮，
    rejected 的每一行都没有被绑上（不允许「回执说绑上了但行里没有」）。

    R7 §1.3 之后多了一条：**整轮拒绝时一个字节都不写**（不允许半绑状态），
    所以「一部分 bound、一部分 rejected」这种混合结果不再可能出现 ——
    下面的用例把这两件事都钉住。
    """
    fresh = _ready_copy(svc, _write(tmp_path / "fresh.txt", b"f"), topic_id="t1")
    old = _ready_copy(svc, _write(tmp_path / "old.txt", b"o"), topic_id="t1")
    await _bind(svc, "turn_old", [old.id])
    stranger = _ready_copy(svc, _write(tmp_path / "stranger.txt", b"s"), topic_id="t1")
    await _bind(svc, "turn_other", [stranger.id])

    # (a) 有一条不合格 → 整轮拒绝：rejected 逐条准确，且**一条都不绑**
    refused = await _bind(
        svc,
        "turn_new",
        [fresh.id, old.id, stranger.id, "att_nope_0002"],
        message_id="msg_new",
        retry_of_turn_id="turn_old",
    )
    assert refused.bound == [], "整轮拒绝时不得半绑（哪怕别的附件真的可用）"
    assert {i for i, _ in refused.rejected} == {stranger.id, "att_nope_0002"}
    assert svc.get(fresh.id, check=False).turn_id is None
    assert svc.get(old.id, check=False).turn_id == "turn_old"
    assert svc.get(stranger.id, check=False).turn_id == "turn_other"
    assert svc.get("att_nope_0002") is None

    # (b) 全部合格 → bound 逐条准确，回执与实际一致
    outcome = await _bind(
        svc,
        "turn_new",
        [fresh.id, old.id],
        message_id="msg_new",
        retry_of_turn_id="turn_old",
    )
    receipt = outcome.as_receipt()
    assert receipt["bound_attachment_ids"] == outcome.bound
    assert [row["id"] for row in receipt["rejected"]] == [i for i, _ in outcome.rejected]
    assert len(outcome.bound) == 2, "直接绑的 fresh + 克隆的 old 副本"
    for attachment_id in outcome.bound:
        row = svc.get(attachment_id, check=False)
        assert row is not None and row.turn_id == "turn_new"
    assert svc.get(fresh.id, check=False).turn_id == "turn_new"
    assert svc.get(old.id, check=False).turn_id == "turn_old", "原行归属不变"


async def test_bind_outcome_is_still_list_shaped_for_existing_callers(svc: AttachmentService, tmp_path: Path):
    """旧调用点（server.py / 既有测试）按 list[Attachment] 消费：迁移期不能突然 500。"""
    att = _ready_copy(svc, _write(tmp_path / "compat.txt", b"c"))

    outcome = await _bind(svc, "turn_compat", [att.id])

    assert [item.id for item in outcome] == [att.id]
    assert outcome.bound == [att.id]
    assert outcome.rejected == []

# -- 7. 复制退路必须离开事件循环线程（R5 §1.3）--------------------------------
#
# 缺陷：`os.link` 失败后退化为同步 `shutil.copyfile`，而 `bind_for_turn` 被 async 路由
# 在**主事件循环线程**同步调用 —— 接近 100MB 的副本复制会卡住其它 API 与 SSE。
# 判据用**线程身份 + 事件循环是否仍在推进**，不用毫秒阈值。


async def _maybe_await(result):
    """兼容旧同步实现：今天的实现返回 BindOutcome（不是协程），
    这样反例断言的是**真实缺陷（跑在事件循环线程上）**，而不是「忘了 await」的 API 形状。"""
    if inspect.isawaitable(result):
        return await result
    return result


def _gated_copyfile(monkeypatch, *, fail_with: OSError | None = None):
    """把复制退路换成「进门先关门、记录线程、等闸门」的桩。返回 (entered, release, seen)。"""
    real = shutil.copyfile
    entered = threading.Event()
    release = threading.Event()
    seen: dict[str, object] = {}

    def gated(src_path, dst_path, *args, **kwargs):
        seen["thread"] = threading.current_thread()
        seen["ident"] = threading.get_ident()
        entered.set()
        if not release.wait(10):  # 测试自己的闸门，不会永远挂着
            raise AssertionError("闸门没有被放开（测试装置问题）")
        if fail_with is not None:
            raise fail_with
        return real(src_path, dst_path, *args, **kwargs)

    monkeypatch.setattr(attachments_mod.shutil, "copyfile", gated)
    return entered, release, seen


def _force_copy_fallback(monkeypatch):
    """强制 os.link 抛 OSError：确保真的走复制退路。"""

    def refuse_link(*args, **kwargs):
        raise OSError(errno.EPERM, "hardlink not permitted")

    monkeypatch.setattr(attachments_mod.os, "link", refuse_link)


def test_bind_for_turn_is_async():
    """冻结接口：路由必须能 `await`（三段式的第一步与第三步都在事件循环线程上）。"""
    assert inspect.iscoroutinefunction(AttachmentService.bind_for_turn), (
        "R5 §1.3 冻结接口：bind_for_turn 必须是 async（文件 I/O 交给 asyncio.to_thread）"
    )


async def test_copy_fallback_runs_off_the_event_loop_thread_and_loop_keeps_running(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """复制退路必须在工作线程里跑；闸门关着时事件循环仍在推进、行停在 prepared（不是 ready）。"""
    att = _ready_copy(svc, _write(tmp_path / "重试.bin", b"x" * 8192))
    await _bind(svc, "turn_old", [att.id])
    _force_copy_fallback(monkeypatch)
    entered, release, seen = _gated_copyfile(monkeypatch)

    task = asyncio.create_task(
        _maybe_await(
            svc.bind_for_turn(
                turn_id="turn_new",
                attachment_ids=[att.id],
                topic_id="t1",
                retry_of_turn_id="turn_old",
            )
        )
    )
    assert await asyncio.to_thread(entered.wait, 10), "复制退路没有被走到（装置问题）"

    # 1) 线程身份：复制必须不在事件循环线程上
    loop_ident = threading.get_ident()
    assert seen["ident"] != loop_ident, (
        "复制退路跑在事件循环线程上 —— 大副本会卡住其它 API 与 SSE（R5 §1.3 缺陷）"
    )
    assert seen["thread"] is not threading.current_thread()

    # 2) 闸门仍关着：事件循环必须能推进（今天的实现会在这里卡死）
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(20):
            ticks += 1
            await asyncio.sleep(0.01)

    await asyncio.wait_for(ticker(), timeout=5)
    assert ticks == 20, "复制期间事件循环必须继续推进"

    # 3) 复制没完成之前，这一行只能停在 prepared（不得提前 ready）
    rows = svc.list(turn_id="turn_new", check=False)
    assert len(rows) == 1 and rows[0].state == "prepared", rows

    release.set()
    outcome = await asyncio.wait_for(task, timeout=10)

    assert outcome.rejected == [], outcome.rejected
    assert len(outcome.bound) == 1
    cloned = svc.get(outcome.bound[0], check=False)
    assert cloned.state == "ready"
    assert Path(cloned.stored_path).read_bytes() == b"x" * 8192
    # 原行归属不变
    assert svc.get(att.id).turn_id == "turn_old"


async def test_clone_row_deleted_during_copy_is_not_committed_ready(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """复制途中这一行被删（用户删附件/取消）：迟到结果不得提交 ready，也不留无人认领副本。"""
    att = _ready_copy(svc, _write(tmp_path / "半路被删.bin", b"y" * 4096))
    await _bind(svc, "turn_old", [att.id])
    _force_copy_fallback(monkeypatch)
    entered, release, seen = _gated_copyfile(monkeypatch)

    task = asyncio.create_task(
        _maybe_await(
            svc.bind_for_turn(
                turn_id="turn_new",
                attachment_ids=[att.id],
                topic_id="t1",
                retry_of_turn_id="turn_old",
            )
        )
    )
    assert await asyncio.to_thread(entered.wait, 10)
    rows = svc.list(turn_id="turn_new", check=False)
    assert len(rows) == 1
    target = svc.copy_path(rows[0])
    svc.delete(rows[0].id, purge_copy=True)  # 复制还在跑的时候行被删掉

    release.set()
    outcome = await asyncio.wait_for(task, timeout=10)

    assert outcome.bound == [], "行已经不在了，绝不能把迟到结果写成 bound"
    assert [item[0] for item in outcome.rejected] == [att.id]
    assert svc.get(rows[0].id) is None
    assert not target.exists(), f"无人认领的副本必须清掉：{target}"
    assert svc.list(turn_id="turn_new", check=False) == []


async def test_copy_failure_leaves_no_prepared_row_and_no_file(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """复制失败：不留 prepared 行、不留半截文件，原因结构化返回。"""
    att = _ready_copy(svc, _write(tmp_path / "复制失败.bin", b"z" * 4096))
    await _bind(svc, "turn_old", [att.id])
    _force_copy_fallback(monkeypatch)
    _gated_copyfile(monkeypatch, fail_with=OSError(errno.ENOSPC, "no space left on device"))
    # 闸门立即放开：这里不测暂停，只测失败收尾
    entered, release, _ = _gated_copyfile(monkeypatch, fail_with=OSError(errno.ENOSPC, "no space"))
    release.set()

    outcome = await svc.bind_for_turn(
        turn_id="turn_new", attachment_ids=[att.id], topic_id="t1", retry_of_turn_id="turn_old"
    )

    assert outcome.bound == []
    assert [item[0] for item in outcome.rejected] == [att.id]
    assert "副本" in outcome.rejected[0][1] or "失败" in outcome.rejected[0][1]
    assert svc.list(turn_id="turn_new", check=False) == [], "不得留下 prepared 行"
    data_dir = tmp_path / "data"
    source_copy = Path(att.stored_path)
    leftovers = [p for p in data_dir.rglob("*") if p.is_file() and p != source_copy]
    assert leftovers == [], f"不得留下无人认领的副本：{leftovers}"


async def test_concurrent_retries_of_same_source_both_succeed(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """同一源副本并发重试：各自新 id、内容正确、都 ready（互不抢）。"""
    att = _ready_copy(svc, _write(tmp_path / "并发.bin", b"c" * 2048))
    await _bind(svc, "turn_old", [att.id])
    _force_copy_fallback(monkeypatch)

    first, second = await asyncio.gather(
        svc.bind_for_turn(
            turn_id="turn_new_1", attachment_ids=[att.id], topic_id="t1", retry_of_turn_id="turn_old"
        ),
        svc.bind_for_turn(
            turn_id="turn_new_2", attachment_ids=[att.id], topic_id="t1", retry_of_turn_id="turn_old"
        ),
    )

    assert len(first.bound) == 1 and len(second.bound) == 1
    assert first.bound[0] != second.bound[0]
    for outcome in (first, second):
        cloned = svc.get(outcome.bound[0], check=False)
        assert cloned.state == "ready"
        assert Path(cloned.stored_path).read_bytes() == b"c" * 2048
    assert svc.get(att.id).turn_id == "turn_old"


async def test_cancelled_bind_leaves_no_prepared_row_and_no_orphan_copy(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """复制途中取消（客户端断开）：不得留下 prepared 行，也不得留下无人认领的副本。"""
    att = _ready_copy(svc, _write(tmp_path / "取消.bin", b"q" * 4096))
    await _bind(svc, "turn_old", [att.id])
    _force_copy_fallback(monkeypatch)
    entered, release, _ = _gated_copyfile(monkeypatch)

    task = asyncio.create_task(
        _maybe_await(
            svc.bind_for_turn(
                turn_id="turn_new",
                attachment_ids=[att.id],
                topic_id="t1",
                retry_of_turn_id="turn_old",
            )
        )
    )
    assert await asyncio.to_thread(entered.wait, 10)
    rows = svc.list(turn_id="turn_new", check=False)
    assert len(rows) == 1
    target = svc.copy_path(rows[0])

    task.cancel()
    release.set()  # 工作线程会照常写完，但迟到结果不得提交 ready
    with pytest.raises(asyncio.CancelledError):
        await task
    # 给「取消后的收尾」一点时间（它等的是工作线程真正结束）
    for _ in range(100):
        if svc.list(turn_id="turn_new", check=False) == [] and not target.exists():
            break
        await asyncio.sleep(0.05)

    assert svc.list(turn_id="turn_new", check=False) == [], "取消后不得留下 prepared 行"
    assert not target.exists(), f"取消后不得留下无人认领的副本：{target}"


