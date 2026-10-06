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

import os
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


def _bind(svc: AttachmentService, turn_id: str, ids, *, topic_id: str = "t1", **kw):
    """按冻结接口调用（全关键字）。"""
    return svc.bind_for_turn(
        turn_id=turn_id, attachment_ids=ids, topic_id=topic_id, **kw
    )


def _tool(svc: AttachmentService, turn_id: str) -> ReadAttachmentTool:
    return ReadAttachmentTool(svc, active_turn_id=lambda: turn_id)


# -- 1. 静默丢弃 → 结构化拒绝 ------------------------------------------------


def test_bind_reports_rejection_instead_of_silently_dropping(svc: AttachmentService, tmp_path: Path):
    """重试没带上 retry_of_turn_id：必须进 rejected，而不是「返回空列表、请求照旧 accepted」。"""
    source = _write(tmp_path / "原轮附件.txt", b"\xe5\x86\x85\xe5\xae\xb9")
    att = _ready_copy(svc, source)
    _bind(svc, "turn_old", [att.id])

    outcome = _bind(svc, "turn_new", [att.id], message_id="msg_new")

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
    _bind(svc, "turn_old", [att.id])

    outcome = _bind(
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


def test_retry_clone_does_not_re_read_user_source_file(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """克隆必须复用 QIO 的副本：即使用户原文件被换掉，新轮拿到的仍是当初那份。"""
    source = _write(tmp_path / "原始.txt", "原始内容".encode("utf-8"))
    att = _ready_copy(svc, source)
    _bind(svc, "turn_old", [att.id])
    # 用户把原文件换成别的内容（副本已经与它无关）
    source.write_bytes("换掉的内容".encode("utf-8"))

    outcome = _bind(
        svc, "turn_new", [att.id], message_id="msg_new", retry_of_turn_id="turn_old"
    )
    clone = svc.get(outcome.bound[0])
    assert Path(clone.stored_path).read_bytes() == "原始内容".encode("utf-8")


# -- 3. 防偷换：只接受「未绑定」或「绑定在 retry_of_turn_id 那一轮」 ----------


def test_retry_never_steals_attachment_bound_to_another_turn(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "别人的.txt", b"x")
    att = _ready_copy(svc, source)
    _bind(svc, "turn_a", [att.id])

    outcome = _bind(svc, "turn_new", [att.id], retry_of_turn_id="turn_b")

    assert outcome.bound == []
    assert [item[0] for item in outcome.rejected] == [att.id]
    assert svc.get(att.id).turn_id == "turn_a"
    # 没有产生任何克隆行
    assert [a.id for a in svc.list(turn_id="turn_new")] == []


def test_retry_rejects_unknown_cross_topic_and_not_bindable_ids(svc: AttachmentService, tmp_path: Path):
    ready = _ready_copy(svc, _write(tmp_path / "ready.txt", b"r"), topic_id="t1")
    other_topic = _ready_copy(svc, _write(tmp_path / "other.txt", b"o"), topic_id="t2")
    broken_source = _write(tmp_path / "gone.txt", b"g")
    broken = _ready_copy(svc, broken_source)
    Path(broken.stored_path).unlink()  # 副本丢了 → missing，不允许绑定

    outcome = _bind(
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


def test_retry_bound_ids_are_deduplicated(svc: AttachmentService, tmp_path: Path):
    att = _ready_copy(svc, _write(tmp_path / "dup.txt", b"d"))
    _bind(svc, "turn_old", [att.id])

    outcome = _bind(
        svc,
        "turn_new",
        [att.id, att.id],
        message_id="msg_new",
        retry_of_turn_id="turn_old",
    )

    assert len(outcome.bound) == 1
    assert outcome.rejected == []


# -- 4. reference 克隆：重新检查当前可用性与变化，不沿用旧状态 ----------------


def test_reference_clone_rechecks_state_instead_of_inheriting(
    svc: AttachmentService, tmp_path: Path
):
    # (a) 位置没变 → 克隆 ready
    unchanged = _sparse(tmp_path / "steady.bin", COPY_MAX_BYTES + 1)
    att_a = svc.prepare(str(unchanged), topic_id="t1")
    att_a = svc.run_prepare(att_a.id)
    assert att_a.kind == "reference"
    _bind(svc, "turn_a", [att_a.id])
    cloned_a = svc.get(_bind(svc, "turn_new_a", [att_a.id], retry_of_turn_id="turn_a").bound[0])
    assert cloned_a.kind == "reference"
    assert cloned_a.state == "ready"
    assert cloned_a.source_attachment_id == att_a.id
    assert cloned_a.stored_path is None

    # (b) 登记之后被改动 → 克隆如实记 changed（带原因），不是沿用 ready
    changing = _sparse(tmp_path / "moving.bin", COPY_MAX_BYTES + 1)
    att_b = svc.prepare(str(changing), topic_id="t1")
    att_b = svc.run_prepare(att_b.id)
    _bind(svc, "turn_b", [att_b.id])
    later = os.stat(changing).st_mtime + 60
    os.utime(changing, (later, later))
    outcome_b = _bind(svc, "turn_new_b", [att_b.id], retry_of_turn_id="turn_b")
    cloned_b = svc.get(outcome_b.bound[0])
    assert cloned_b.state == "changed"
    assert cloned_b.error and "变了" in cloned_b.error

    # (c) 文件被删掉 → 克隆如实记 missing（带原因），仍然归属新轮
    vanished = _sparse(tmp_path / "vanish.bin", COPY_MAX_BYTES + 1)
    att_c = svc.prepare(str(vanished), topic_id="t1")
    att_c = svc.run_prepare(att_c.id)
    _bind(svc, "turn_c", [att_c.id])
    vanished.unlink()
    outcome_c = _bind(svc, "turn_new_c", [att_c.id], retry_of_turn_id="turn_c")
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


def test_empty_list_and_missing_field_semantics_are_preserved(svc: AttachmentService, tmp_path: Path):
    unbound = _ready_copy(svc, _write(tmp_path / "pending.txt", b"p"))

    empty = _bind(svc, "turn_x", [])
    assert empty.bound == [] and empty.rejected == []
    assert svc.get(unbound.id).turn_id is None, "显式空列表不得落进兜底分支"

    fallback = _bind(svc, "turn_y", None)
    assert fallback.bound == [unbound.id]
    assert fallback.rejected == []
    assert svc.get(unbound.id).turn_id == "turn_y"


def test_retry_attachment_ids_lists_what_the_source_turn_bound(svc: AttachmentService, tmp_path: Path):
    first = _ready_copy(svc, _write(tmp_path / "one.txt", b"1"))
    second = _ready_copy(svc, _write(tmp_path / "two.txt", b"2"))
    _bind(svc, "turn_old", [first.id, second.id])

    assert svc.retry_attachment_ids("turn_old") == [first.id, second.id]
    assert svc.retry_attachment_ids("turn_absent") == []


# -- 6. 受理前预检（route 在 submit 之前调用：rejected 非空就不入队） ------------------


def test_precheck_reports_the_same_rejections_without_touching_anything(
    svc: AttachmentService, tmp_path: Path
):
    """预检只读：判据与 bind_for_turn 同一份，且不落库、不克隆。"""
    ready = _ready_copy(svc, _write(tmp_path / "ok.txt", b"ok"), topic_id="t1")
    other_topic = _ready_copy(svc, _write(tmp_path / "other.txt", b"o"), topic_id="t2")
    bound = _ready_copy(svc, _write(tmp_path / "bound.txt", b"b"), topic_id="t1")
    _bind(svc, "turn_old", [bound.id])

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


def test_precheck_accepts_retry_of_turn_and_stays_read_only(svc: AttachmentService, tmp_path: Path):
    att = _ready_copy(svc, _write(tmp_path / "retry.txt", b"r"), topic_id="t1")
    _bind(svc, "turn_old", [att.id])

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


def test_receipt_is_always_accurate(svc: AttachmentService, tmp_path: Path):
    """Lead 复核项：回执必须永远准确 —— bound 的每一行确实绑到了本轮，
    rejected 的每一行都没有被绑上（不允许「回执说绑上了但行里没有」）。"""
    fresh = _ready_copy(svc, _write(tmp_path / "fresh.txt", b"f"), topic_id="t1")
    old = _ready_copy(svc, _write(tmp_path / "old.txt", b"o"), topic_id="t1")
    _bind(svc, "turn_old", [old.id])
    stranger = _ready_copy(svc, _write(tmp_path / "stranger.txt", b"s"), topic_id="t1")
    _bind(svc, "turn_other", [stranger.id])

    outcome = _bind(
        svc,
        "turn_new",
        [fresh.id, old.id, stranger.id, "att_nope_0002"],
        message_id="msg_new",
        retry_of_turn_id="turn_old",
    )

    receipt = outcome.as_receipt()
    assert receipt["bound_attachment_ids"] == outcome.bound
    assert [row["id"] for row in receipt["rejected"]] == [i for i, _ in outcome.rejected]
    assert len(outcome.bound) == 2, "直接绑的 fresh + 克隆的 old 副本"
    assert {i for i, _ in outcome.rejected} == {stranger.id, "att_nope_0002"}
    # bound：每一个 id 在库里确实绑到了这一轮
    for attachment_id in outcome.bound:
        row = svc.get(attachment_id, check=False)
        assert row is not None and row.turn_id == "turn_new"
    # rejected：每一个 id 都没有被绑到这一轮（归属保持原样）
    assert svc.get(stranger.id, check=False).turn_id == "turn_other"
    assert svc.get(fresh.id, check=False).turn_id == "turn_new"
    assert svc.get("att_nope_0002") is None


def test_bind_outcome_is_still_list_shaped_for_existing_callers(svc: AttachmentService, tmp_path: Path):
    """旧调用点（server.py / 既有测试）按 list[Attachment] 消费：迁移期不能突然 500。"""
    att = _ready_copy(svc, _write(tmp_path / "compat.txt", b"c"))

    outcome = _bind(svc, "turn_compat", [att.id])

    assert [item.id for item in outcome] == [att.id]
    assert outcome.bound == [att.id]
    assert outcome.rejected == []
