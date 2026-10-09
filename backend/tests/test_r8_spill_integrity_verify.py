r"""D 独立验证（R8 问题四）：暂存完整性必须按**字节事实**核对，不得误报「完整」。

契约：docs/plans/2026-10-09-send-cancel-integrity.md §1.4（冻结）。
基线 9f5bc07 缺陷（core/answer_buffer.py::collect）：只把**读取 OSError** 当故障，
**不核对读回字节数与成功写入的事实** → 暂存被截短/清空/异常增长时仍报 complete=true，
用户看不到任何不完整说明（审查实测：暂存 37,874 字节、截到 100 字节 → 只交付 262,244 却报完整）。

判定规则（用户可见结果）：
* 交付长度必须与**实际读回**一致；故障事实必须有**可见警告**（且不得说成「正文超过上限」）；
* 三个数字分开：原生成 / 成功保存 / 实际交付（警告里的数字必须真实）；
* **不得**用 replacement 字符掩盖损坏（多字节边界被截断时）；
* 模型只调用 1 次；正常超阈值仍原样完整、0 警告。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r8_spill_integrity_verify.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from agent.adapters.base import (
    STREAM_DONE,
    STREAM_TEXT,
    AdapterMode,
    BaseAdapter,
    ChatMessage,
    Completion,
    StreamDelta,
)
from agent.api.bus import EventBus
from agent.core import answer_buffer as buffer_module
from agent.core.answer_buffer import UNDECLARED_MEMORY_LIMIT
from agent.core.loop import AgentLoop
from agent.tools.registry import ToolRegistry

MEMORY_LIMIT = int(UNDECLARED_MEMORY_LIMIT)   # 262,144 字节
TEXT_BYTES = 300_018
ASCII_TEXT = "A" * TEXT_BYTES
CHINESE_TEXT = "甲" * (TEXT_BYTES // 3)
LIMIT_WORDS = ("超过上限", "上限", "limit")


class _ChunkAdapter(BaseAdapter):
    mode = AdapterMode.NATIVE
    supports_stream = True

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001
        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content=self.text))

    async def stream(self, messages, tools, **kwargs):  # noqa: ANN001
        self.calls += 1
        yield StreamDelta(kind=STREAM_TEXT, text=self.text)
        yield StreamDelta(
            kind=STREAM_DONE,
            completion=Completion(message=ChatMessage(role="assistant", content=self.text)),
        )


@contextlib.contextmanager
def _corrupt_spill(mode: str, *, size: int = 0):
    """在 collect() 读取**之前**破坏暂存文件（真实文件操作，不是 mock 返回值）。"""
    real_close = buffer_module.AnswerBuffer._close_spill
    skipped: list[str] = []
    state: dict = {"fired": False, "saved_bytes": None, "readable_bytes": None}

    async def _patched(self):  # noqa: ANN001
        await real_close(self)
        path = getattr(self, "_spill_path", None)
        if path is None:
            skipped.append("no-spill")
            return
        file = Path(path)
        if not file.exists():
            skipped.append("missing")
            return
        state["fired"] = True
        state["saved_bytes"] = file.stat().st_size
        if mode == "truncate":
            with open(file, "r+b") as handle:
                handle.truncate(size)
        elif mode == "empty":
            with open(file, "r+b") as handle:
                handle.truncate(0)
        elif mode == "grow":
            with open(file, "ab") as handle:
                handle.write(b"Z" * size)
        state["readable_bytes"] = file.stat().st_size

    buffer_module.AnswerBuffer._close_spill = _patched
    try:
        yield state
    finally:
        buffer_module.AnswerBuffer._close_spill = real_close


async def _run(text: str, *, spill_dir: Path, turn_id: str):
    adapter = _ChunkAdapter(text)
    loop = AgentLoop(adapter, ToolRegistry(), EventBus(), turn_id=turn_id, spill_dir=str(spill_dir))
    result = await asyncio.wait_for(loop.run("请回答我"), timeout=120)
    warnings = [e.data for e in loop.bus._history if e.type.value == "WARNING"]
    return loop, adapter, result, warnings


def _warning_text(warnings: list[dict]) -> str:
    return " | ".join(str(w.get("message") or w.get("detail") or w) for w in warnings)


def _assert_incomplete_reported(label: str, warnings: list[dict], *, delivered: int, generated: int) -> str:
    text = _warning_text(warnings)
    assert warnings, (
        "%s：暂存已被破坏，却**没有任何用户可见的不完整说明**（基线只核对 OSError）" % label,
        {"delivered": delivered, "generated": generated},
    )
    assert any(word in text for word in ("读取", "读回", "暂存", "存储", "损坏", "不完整", "未完整", "截")), (
        "%s：警告里没有可理解的存储/不完整原因" % label, text[:240]
    )
    assert not any(word in text for word in LIMIT_WORDS), (
        "%s：存储损坏被说成「正文超过上限」（契约禁止混淆）" % label, text[:240]
    )
    assert str(delivered) in text or str(generated) in text, (
        "%s：警告里没有与实际交付/生成量一致的字节数（三个数字必须真实）" % label,
        {"warning": text[:240], "delivered": delivered, "generated": generated},
    )
    return text


# ---- 1. 核心：截短 / 清空 / 异常增长 → 不得报完整 -------------------------------------


@pytest.mark.parametrize(
    "mode,text,size,expected_readable",
    [
        ("truncate", ASCII_TEXT, 100, 100),
        ("empty", ASCII_TEXT, 0, 0),
        # 异常增长：文件被写大，但**已确认写入**的字节数不变 → 交付必须是「已确认」的那部分，
        # 且必须给出异常警告（交付长度按 saved_bytes 算，见下）
        ("grow", ASCII_TEXT, 4096, "saved"),
    ],
    ids=["truncate-100", "empty", "grow"],
)
async def test_corrupted_spill_is_not_reported_complete(tmp_path: Path, mode, text, size, expected_readable):
    with _corrupt_spill(mode, size=size) as state:
        loop, adapter, result, warnings = await _run(text, spill_dir=tmp_path, turn_id="r8_spill_" + mode)
    assert state["fired"], ("暂存破坏注入没有触发（装置失效）", state["skipped"] if "skipped" in state else None)
    delivered = len((result.final_content or "").encode("utf-8"))
    want_readable = (state["saved_bytes"] or 0) if expected_readable == "saved" else expected_readable
    if expected_readable == "saved":
        assert state["readable_bytes"] == (state["saved_bytes"] or 0) + size, (
            "装置：异常增长注入没有生效", state
        )
    else:
        assert state["readable_bytes"] == want_readable, ("装置：暂存实际长度与预期不符", state, want_readable)
    assert delivered == MEMORY_LIMIT + want_readable, (
        "交付长度必须等于「内存部分 + 实际读回的暂存部分」",
        {"delivered": delivered, "expected": MEMORY_LIMIT + want_readable},
    )
    _assert_incomplete_reported(
        mode, warnings, delivered=delivered, generated=TEXT_BYTES
    )
    assert adapter.calls == 1, ("不得为找回原文再调一次模型", adapter.calls)
    print("[诊断] %s：交付=%d（内存 %d + 读回 %d）；警告=%s"
          % (mode, delivered, MEMORY_LIMIT, want_readable, _warning_text(warnings)[:150]))


# ---- 2. 中文末字被截断（多字节边界）→ 不得用 replacement 掩盖 -------------------------


async def test_truncated_multibyte_tail_is_not_masked(tmp_path: Path):
    with _corrupt_spill("truncate", size=1) as state:
        loop, adapter, result, warnings = await _run(
            CHINESE_TEXT, spill_dir=tmp_path, turn_id="r8_spill_midchar"
        )
    assert state["fired"] and state["readable_bytes"] == 1, state
    delivered = result.final_content or ""
    assert "\ufffd" not in delivered, (
        "暂存在多字节边界被截断，交付里出现了 replacement 字符 —— 契约禁止用它掩盖损坏",
        {"replacement_count": delivered.count("\ufffd"), "tail": delivered[-6:]},
    )
    _assert_incomplete_reported(
        "中文末字截断", warnings, delivered=len(delivered.encode("utf-8")), generated=TEXT_BYTES
    )
    assert adapter.calls == 1, adapter.calls
    print("[诊断] 中文末字截断：交付=%d 字节；replacement=0；警告=%s"
          % (len(delivered.encode("utf-8")), _warning_text(warnings)[:150]))


# ---- 3. 绿守卫：读取异常（r7 已修）与正常路径 ----------------------------------------


async def test_read_error_is_reported(tmp_path: Path):
    real_read_bytes = Path.read_bytes

    def _boom(self):  # noqa: ANN001
        if str(self).endswith(buffer_module.SPILL_SUFFIX):
            raise OSError(5, "受控错误：读取暂存文件失败（输入输出错误）")
        return real_read_bytes(self)

    Path.read_bytes = _boom
    try:
        loop, adapter, result, warnings = await _run(ASCII_TEXT, spill_dir=tmp_path, turn_id="r8_spill_ioerr")
    finally:
        Path.read_bytes = real_read_bytes
    delivered = len((result.final_content or "").encode("utf-8"))
    assert delivered == MEMORY_LIMIT, delivered
    _assert_incomplete_reported("读取异常", warnings, delivered=delivered, generated=TEXT_BYTES)
    assert adapter.calls == 1, adapter.calls


@pytest.mark.parametrize("label,text", [("ASCII", ASCII_TEXT), ("中文", CHINESE_TEXT)], ids=["ascii", "cjk"])
async def test_normal_over_threshold_still_complete(tmp_path: Path, label, text):
    loop, adapter, result, warnings = await _run(text, spill_dir=tmp_path, turn_id="r8_spill_ok")
    assert result.final_content == text, (label, len(result.final_content or ""), len(text))
    assert not warnings, ("正常路径不得产生任何警告", _warning_text(warnings)[:200])
    assert adapter.calls == 1, adapter.calls
    assert not list(Path(tmp_path).glob("*" + buffer_module.SPILL_SUFFIX)), "暂存必须收敛"
    print("[诊断] %s 正常超阈值：完整交付 %d 字节、0 警告" % (label, len(text.encode("utf-8"))))
