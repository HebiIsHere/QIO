"""文件身份与移动修复（契约 §7）：同一文件编辑 / 同名不同文件 / 同内容另一份。

覆盖：
* identity_of / same_file / content_sha256 的真实文件系统语义（Windows FileIdInfo、POSIX stat）；
* 同一文件编辑后身份不变、内容哈希变化；
* 同内容的另一份文件**不是**原文件（绝不被当成同一文件）；
* 同卷移动/改名后身份不变，resolve_source 在有限范围按身份自动修复；
* 仅同名/同大小/同内容只能作为候选，必须用户确认；
* 多份同身份文件（硬链接）→ ambiguous，不擅自选一份。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent.services.attachments import (
    AttachmentConfirmationRequired,
    AttachmentService,
    REFERENCE_AMBIGUOUS,
    REFERENCE_AVAILABLE,
)
from agent.services.file_identity import (
    content_sha256,
    find_by_identity,
    identity_of,
    same_file,
)


@pytest.fixture()
def service(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _register(service: AttachmentService, path: Path, **kwargs):
    return service.register_source(
        path=str(path), source_kind="desktop_path", **kwargs
    )


def test_identity_survives_edit_but_hash_changes(tmp_path: Path):
    path = tmp_path / "文稿.txt"
    path.write_text("第一版", encoding="utf-8")
    before = identity_of(path)
    assert before is not None
    first_hash = content_sha256(path)

    path.write_text("第二版（被用户编辑）", encoding="utf-8")
    after = identity_of(path)
    assert same_file(before, after), "同一文件被编辑后身份必须不变"
    assert content_sha256(path) != first_hash, "内容哈希必须如实变化"


def test_identical_copy_is_not_the_same_file(tmp_path: Path):
    original = tmp_path / "原件.bin"
    original.write_bytes(b"same-bytes")
    copied = tmp_path / "复制件.bin"
    copied.write_bytes(b"same-bytes")

    a, b = identity_of(original), identity_of(copied)
    assert a is not None and b is not None
    assert a.key != b.key
    assert not same_file(a, b), "同内容的另一份文件不是原文件"
    assert content_sha256(original) == content_sha256(copied)


def test_move_and_rename_keep_identity_and_findable(tmp_path: Path):
    source = tmp_path / "会被移动.txt"
    source.write_text("移动前", encoding="utf-8")
    before = identity_of(source)
    assert before is not None

    target_dir = tmp_path / "父目录" / "新位置"
    target_dir.mkdir(parents=True)
    moved = target_dir / "改名后.txt"
    source.rename(moved)

    after = identity_of(moved)
    assert same_file(before, after), "同卷移动/改名不得改变文件身份"
    found = find_by_identity([tmp_path], before)
    assert found == [moved]


def test_resolve_auto_repairs_only_by_file_identity(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "项目材料.txt"
    source.write_text("原始内容", encoding="utf-8")
    att = _register(service, source)
    assert att.file_id

    moved = tmp_path / "改名后材料.txt"
    source.rename(moved)

    resolved = service.resolve_source(att.id)
    assert resolved.state == REFERENCE_AVAILABLE
    assert resolved.path == moved
    refreshed = service.get(att.id)
    assert refreshed.source_path == str(moved)
    assert refreshed.reference_state == "relocated"
    facts = json.loads(refreshed.legacy_paths)
    assert facts and facts[-1]["event"] == "auto_relocate"
    assert facts[-1]["mechanism"] == "file_identity_match"


def test_same_name_other_file_needs_user_confirmation(service: AttachmentService, tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    source = root / "报告.docx"
    source.write_bytes("真正的内容".encode("utf-8"))
    att = _register(service, source)

    # 原件被移走；原目录放一份"同名、同大小、不同内容"的文件
    real_moved = tmp_path / "归档" / "报告.docx"
    real_moved.parent.mkdir()
    source.rename(real_moved)
    source.write_bytes("伪造的内容".encode("utf-8"))

    resolved = service.resolve_source(att.id)
    assert resolved.state == REFERENCE_AMBIGUOUS, resolved
    assert resolved.candidates, "同名不同文件必须给出候选让用户确认"
    assert any(item["same_name"] for item in resolved.candidates)

    # 用户如果把"同名但不同身份"的文件当成原件：必须显式确认，默认拒绝
    with pytest.raises(AttachmentConfirmationRequired) as excinfo:
        service.relocate_reference(att.id, str(source))
    assert excinfo.value.candidates

    # 真正的原件（身份相同）不需要确认，自动修复
    fixed = service.relocate_reference(att.id, str(real_moved))
    assert fixed.source_path == str(real_moved)
    assert fixed.reference_state == "relocated"
    assert json.loads(fixed.legacy_paths)[-1]["event"] == "relocate"
    assert json.loads(fixed.legacy_paths)[-1]["mechanism"] == "file_identity_match"


def test_same_content_other_file_is_not_auto_fixed(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "数据.csv"
    source.write_bytes(b"a,b\n1,2\n")
    att = _register(service, source)

    copy = tmp_path / "另一份同名内容.csv"
    copy.write_bytes(b"a,b\n1,2\n")
    source.unlink()  # 原件不在了，只剩另一份同内容文件

    resolved = service.resolve_source(att.id)
    # 不同文件名不会成为同名候选；同内容不构成"同一文件"依据 → 如实 missing
    assert resolved.state != REFERENCE_AVAILABLE or resolved.path is None
    if resolved.path is not None:
        pytest.fail("不得把另一份同内容文件当成原文件")
    assert resolved.state in ("missing", "ambiguous")
    if resolved.state == REFERENCE_AMBIGUOUS:
        assert all(item["same_file"] is False for item in resolved.candidates)


def test_unknown_identity_requires_confirmation(service: AttachmentService, tmp_path: Path, db_conn):
    source = tmp_path / "无身份.txt"
    source.write_text("内容", encoding="utf-8")
    att = _register(service, source)
    # 模拟文件系统不支持 file id（或历史记录没有身份）：不能凭同名/同大小自动修复
    with db_conn:
        db_conn.execute("UPDATE attachments SET file_id = NULL WHERE id = ?", (att.id,))
    other = tmp_path / "另一个.txt"
    other.write_text("另一份", encoding="utf-8")
    with pytest.raises(AttachmentConfirmationRequired):
        service.relocate_reference(att.id, str(other))
    fixed = service.relocate_reference(att.id, str(other), confirmed_identity=True)
    assert fixed.source_path == str(other)


def test_multiple_same_identity_paths_are_ambiguous(service: AttachmentService, tmp_path: Path):
    root = tmp_path / "hardlinks"
    root.mkdir()
    source = root / "原件.bin"
    source.write_bytes(b"hardlink-content")
    try:
        link_a = root / "副本A.bin"
        os.link(source, link_a)
        link_b = root / "副本B.bin"
        os.link(source, link_b)
    except OSError as exc:  # 文件系统不支持硬链接：这是环境能力事实，不是被跳过的业务用例
        pytest.skip(f"本机文件系统不支持硬链接：{exc}")

    att = _register(service, source)
    source.unlink()  # 只删这一个目录项，另两个硬链接仍指向同一份文件

    resolved = service.resolve_source(att.id)
    assert resolved.state == REFERENCE_AMBIGUOUS, resolved
    assert len(resolved.candidates) >= 2
    assert all(item["same_file"] for item in resolved.candidates)
