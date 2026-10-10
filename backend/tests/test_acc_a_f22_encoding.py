"""A 组反例：F22 编码嗅探截在 UTF-8 字符中间，被误判为其他编码。

契约来源：docs/plans/2026-10-09-process-attachment-audit-consolidation.md C7。
基线现状（_sniff_encoding）：把前 64 KB 直接 head.decode(encoding)（final 语义），
多字节 UTF-8 字符跨嗅探尾部边界时整段严格 decode 失败，于是错误回退到 GBK/UTF-16/cp1252。

反例（基线应当红）：
* 3 字节 UTF-8 字符跨 64 KB 边界（首 1 字节 / 首 2 字节落在头部）→ 嗅探必须是 utf-8；
* 头部以不完整前缀结尾 ≠ 无效编码；
* 真 GBK / UTF-16 / BOM 仍要判对，并且完整文件真的能读回正确文本。
"""
from __future__ import annotations

import codecs
import json
from pathlib import Path

import pytest

from agent.services.attachments import AttachmentService
from agent.tools import attachment_tools as tool_mod
from agent.tools.attachment_tools import ReadAttachmentTool

SNIFF = tool_mod.SNIFF_BYTES


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _tool(svc: AttachmentService) -> ReadAttachmentTool:
    return ReadAttachmentTool(svc, active_turn_id=lambda: "t")


def _prefix_with_short_lines(target: int) -> bytes:
    full, rem = divmod(target, 65)  # 每行 64 个 ascii + 换行
    return (b"a" * 64 + b"\n") * full + b"a" * rem


def _prepare(svc: AttachmentService, path: Path):
    att = svc.prepare(str(path))
    return svc.run_prepare(att.id)


@pytest.mark.parametrize("lead_len", [1, 2])
async def test_utf8_char_straddling_sniff_boundary(tmp_path: Path, svc: AttachmentService, lead_len: int):
    raw = "中".encode("utf-8")
    prefix = _prefix_with_short_lines(SNIFF - lead_len)
    path = tmp_path / "boundary.txt"
    path.write_bytes(prefix + raw + b"\ntail line\n")
    assert (SNIFF - lead_len) + len(raw) > SNIFF  # 字符确实跨过了嗅探尾部

    assert tool_mod._sniff_encoding(path) == "utf-8", (
        "跨嗅探边界的合法 UTF-8 不能被误判成其它编码",
        tool_mod._sniff_encoding(path),
    )

    full, _rem = divmod(SNIFF - lead_len, 65)
    att = _prepare(svc, path)
    result = await _tool(svc).run(attachment_id=att.id, offset=full, limit=3)
    assert result.ok is True, result.error
    payload = json.loads(result.content)
    assert "中" in payload["content"], payload["content"]
    assert "tail line" in payload["content"]


def test_incomplete_prefix_is_not_invalid_encoding(tmp_path: Path):
    path = tmp_path / "prefix.txt"
    path.write_bytes(b"a" * 100 + b"\xe4")  # 合法的 UTF-8 不完整前缀
    assert tool_mod._sniff_encoding(path) == "utf-8"


@pytest.mark.parametrize(
    "payload_bytes,expected",
    [
        (codecs.BOM_UTF8 + "中文内容\n".encode("utf-8"), "utf-8-sig"),
        ("中文内容\n".encode("utf-16"), "utf-16"),
        ("姓名,数量\n张三,3\n".encode("gbk"), "gbk"),
    ],
)
async def test_real_encodings_still_detected_and_read(tmp_path: Path, svc: AttachmentService, payload_bytes, expected):
    path = tmp_path / "encoded.txt"
    path.write_bytes(payload_bytes)
    assert tool_mod._sniff_encoding(path) == expected
    att = _prepare(svc, path)
    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=5)
    assert result.ok is True, result.error
    payload = json.loads(result.content)
    if expected == "gbk":
        assert "张三" in payload["content"]
    else:
        assert "中文内容" in payload["content"]


async def test_binary_is_reported_not_faked(svc: AttachmentService, tmp_path: Path):
    path = tmp_path / "mystery.bin"
    path.write_bytes(bytes(range(256)) * 8)
    att = _prepare(svc, path)
    result = await _tool(svc).run(attachment_id=att.id)
    assert result.ok is True, result.error
    payload = json.loads(result.content)
    assert payload["readable"] is False
    assert "不可读取" in payload["reason"]
