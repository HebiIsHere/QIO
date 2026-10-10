"""A 组反例：F02 大文本按行迭代仍一次分配整条超长行。

契约来源：docs/plans/2026-10-09-process-attachment-audit-consolidation.md C7；
AGENTS.md「安全表述必须诚实」。基线现状（attachment_tools._iter_slice / _decode）：
* 小文件走 text = _decode(...) + splitlines()，整份进内存；
* 大文件走 for line in handle，一行 100 MB 会被整条读进内存后再裁到 2000 字符。
因此内存随单行长度线性增长，输出截断并不能限制解析前的分配。

反例（基线应当红）：
* 48 MB 单行 → 只返回约 2 KB，但 Python 分配峰值与文件同量级；
* 第一行 24 MB、第二行很短，读取 offset=1 仍整条读入第一行；
* 峰值不随单行长度线性增长（8 MB vs 40 MB）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.services.attachments import AttachmentService
from agent.tools import attachment_tools as tool_mod
from agent.tools.attachment_tools import ReadAttachmentTool

from _acc_a_probe import run_probe

MB = 1_000_000


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _tool(svc: AttachmentService) -> ReadAttachmentTool:
    return ReadAttachmentTool(svc, active_turn_id=lambda: "t")


def _write_stream(path: Path, chunk: bytes, count: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        for _ in range(count):
            handle.write(chunk)
    return path


def _write_lines(path: Path, lines: list[str], *, newline: str = "\n", trailing: bool = True) -> Path:
    body = newline.join(lines)
    if trailing:
        body += newline
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="")
    return path


# ---- 1. 48 MB 单行：输出很小，内存峰值必须与单行长度脱钩 --------------------------


def test_single_long_line_memory_is_bounded(tmp_path: Path):
    source = _write_stream(tmp_path / "oneline.log", b"x" * MB, 48)
    result = run_probe(tmp_path, source, "oneline.log", offset=0, limit=1)
    assert result["ok"] is True, result["error"]
    assert result["peak"] < 16 * MB, ("单行 48 MB 的分配峰值不应随行长度线性增长", result["peak"])
    payload = json.loads(result["content"])
    assert len(payload["content"]) <= tool_mod.MAX_CHARS
    # 只交付了片段：给出可继续读取的片段游标，而不是静默丢内容
    assert payload["read"]["next_fragment_offset"] > 0 or payload["read"]["next_offset"] is not None


def test_memory_does_not_grow_with_line_length(tmp_path: Path):
    small = _write_stream(tmp_path / "line8.log", b"y" * MB, 8)
    large = _write_stream(tmp_path / "line40.log", b"y" * MB, 40)
    peak_small = run_probe(tmp_path, small, "line8.log", offset=0, limit=1)["peak"]
    peak_large = run_probe(tmp_path, large, "line40.log", offset=0, limit=1)["peak"]
    assert peak_small < 16 * MB, peak_small
    assert peak_large < 16 * MB, ("40 MB 单行的峰值不应随行长度线性增长", peak_large)
    assert peak_large < 3 * peak_small + 4 * MB, (peak_small, peak_large)


# ---- 2. 跳过超长行不能把整条行读进内存 --------------------------------------------


def test_skipping_a_huge_line_does_not_allocate_it(tmp_path: Path):
    source = tmp_path / "skip.log"
    with open(source, "wb") as handle:
        handle.write(b"z" * (24 * MB))
        handle.write(b"\nsecond line\n")
    result = run_probe(tmp_path, source, "skip.log", offset=1, limit=1)
    assert result["ok"] is True, result["error"]
    assert "second line" in result["content"]
    assert result["peak"] < 16 * MB, ("跳过 24 MB 行不应把它整条读进内存", result["peak"])


# ---- 3. 跨块多字节、CRLF、末尾无换行、较大 offset（行为正确） --------------------


async def test_multibyte_across_chunk_boundary(svc: AttachmentService, tmp_path: Path, monkeypatch):
    # 人为把块缩小，逼出「多字节字符跨块」的情形
    monkeypatch.setattr(tool_mod, "CHUNK_BYTES", 16)
    body = ("中文内容" * 100) + "\n第二行\n"
    source = tmp_path / "跨块.txt"
    source.write_text(body, encoding="utf-8", newline="")
    att = svc.prepare(str(source))
    att = svc.run_prepare(att.id)
    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=1)
    assert result.ok is True, result.error
    payload = json.loads(result.content)
    assert "\ufffd" not in payload["content"], payload["content"]
    assert payload["content"].startswith("中文内容中文内容")


async def test_crlf_and_no_trailing_newline(svc: AttachmentService, tmp_path: Path):
    source = _write_lines(tmp_path / "crlf.txt", ["第一行", "第二行", "第三行"], newline="\r\n", trailing=False)
    att = svc.prepare(str(source))
    att = svc.run_prepare(att.id)
    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=10)
    assert result.ok is True, result.error
    payload = json.loads(result.content)
    assert payload["read"]["total_units"] == 3
    assert payload["content"].split("\n") == ["第一行", "第二行", "第三行"]
    assert "\r" not in payload["content"]


async def test_large_offset_reads_the_right_lines(svc: AttachmentService, tmp_path: Path):
    lines = [("第%05d行" % i) for i in range(1000)]
    source = _write_lines(tmp_path / "many.txt", lines, trailing=True)
    att = svc.prepare(str(source))
    att = svc.run_prepare(att.id)
    result = await _tool(svc).run(attachment_id=att.id, offset=995, limit=5)
    assert result.ok is True, result.error
    payload = json.loads(result.content)
    assert payload["content"].split("\n") == lines[995:1000]
    assert payload["read"]["next_offset"] is None  # 到末尾
