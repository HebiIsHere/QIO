r"""D 独立验证（R7 问题三）：暂存故障必须**如实交付**（不能只写日志）。

契约：docs/plans/2026-10-08-cancel-readiness-spill.md §1.4 / §3（冻结）。
基线 966e2fc 缺陷（core/answer_buffer.py:210）：\`collect()\` 捕获暂存读取 OSError 后**只记日志**，
继续返回内存前缀；\`_take_buffered()\` 在 collect 之前取 truncation_reason、之后立刻 discard
→ 故障事实既没进事件也没进轮次状态：用户看到的是**被静默截断**的回答（0 个 WARNING）。

判定规则（用户可见结果）：必须同时有 ①用户可见的不完整说明（事件/轮次警告）②原因（且**不得**
与「超过上限」混淆）③已确认可交付的正式回答内容 ④最终结果与之一致。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r7_spill_delivery_verify.py -q
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
from contextlib import contextmanager
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
from agent.prompts import ANSWER_MARKER
from agent.tools.registry import ToolRegistry

MEMORY_LIMIT = int(UNDECLARED_MEMORY_LIMIT)          # 256 KiB = 262,144 字节
SPILL_TEXT_BYTES = 300_018                            # 契约指定的复现长度（UTF-8 字节）
ASCII_TEXT = "A" * SPILL_TEXT_BYTES                   # ASCII：字节数 == 字符数
CHINESE_TEXT = "甲" * (SPILL_TEXT_BYTES // 3)          # 中文：每字 3 字节 → 同一字节数
LIMIT_WORDS = ("超过上限", "上限", "limit", "truncat")


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


async def _run(text: str, *, spill_dir: Path, turn_id: str):
    adapter = _ChunkAdapter(text)
    loop = AgentLoop(
        adapter, ToolRegistry(), EventBus(), turn_id=turn_id, spill_dir=str(spill_dir)
    )
    result = await asyncio.wait_for(loop.run("请回答我"), timeout=120)
    warnings = [e.data for e in loop.bus._history if e.type.value == "WARNING"]
    return loop, adapter, result, warnings


def _warning_text(warnings: list[dict]) -> str:
    return " | ".join(str(w.get("message") or w.get("detail") or w) for w in warnings)


# ---- 注入：暂存创建 / 写入 / 读取 ----------------------------------------------------


@contextmanager
def _spill_read_failure():
    """读取暂存时抛 OSError（真实调用点：collect() 里的 path.read_bytes）。"""
    real_read_bytes = Path.read_bytes

    def _boom(self):  # noqa: ANN001
        if str(self).endswith(buffer_module.SPILL_SUFFIX):
            raise OSError(5, "受控错误：读取暂存文件失败（输入输出错误）")
        return real_read_bytes(self)

    Path.read_bytes = _boom
    try:
        yield
    finally:
        Path.read_bytes = real_read_bytes


@contextmanager
def _spill_open_failure(*, fail_write: bool):
    """暂存文件创建失败 / 写入失败（真实调用点：asyncio.to_thread(open, path, 'ab') 与 handle.write）。"""
    real_open, real_io_open = builtins.open, io.open
    state = {"fired": False}

    class _BoomHandle:
        def __init__(self, handle) -> None:
            self._handle = handle

        def write(self, data):  # noqa: ANN001
            raise OSError(28, "受控错误：写入暂存文件失败（磁盘空间不足）")

        def __getattr__(self, name):  # noqa: ANN001
            return getattr(self._handle, name)

    def _wrap(real):  # noqa: ANN001, ANN202
        def _patched(file, mode="r", *args, **kwargs):  # noqa: ANN001
            path = str(file)
            if path.endswith(buffer_module.SPILL_SUFFIX) and "a" in str(mode):
                state["fired"] = True
                if not fail_write:
                    raise OSError(13, "受控错误：创建暂存文件失败（权限不足）")
                return _BoomHandle(real(file, mode, *args, **kwargs))
            return real(file, mode, *args, **kwargs)

        return _patched

    builtins.open, io.open = _wrap(real_open), _wrap(real_io_open)
    if hasattr(buffer_module, "open"):
        buffer_module.open = _wrap(real_open)
    try:
        yield state
    finally:
        builtins.open, io.open = real_open, real_io_open
        with contextlib.suppress(AttributeError):
            del buffer_module.open


# ---- 1. 核心：暂存**读取**失败 → 必须给用户可见的不完整说明 + 原因 -------------------


async def test_spill_read_failure_is_reported_to_the_user(tmp_path: Path):
    with _spill_read_failure():
        loop, adapter, result, warnings = await _run(ASCII_TEXT, spill_dir=tmp_path, turn_id="r7_spill_read")

    delivered = result.final_content or ""
    assert len(delivered.encode("utf-8")) == MEMORY_LIMIT, (
        "读取失败时应当交付已确认可交付的内存部分（256 KiB）",
        {"delivered_bytes": len(delivered.encode("utf-8")), "limit": MEMORY_LIMIT},
    )
    assert warnings, (
        "暂存读取失败**只写了日志**：流上没有任何用户可见的不完整说明（基线缺陷）",
        {"delivered_bytes": len(delivered.encode("utf-8")), "warnings": len(warnings)},
    )
    text = _warning_text(warnings)
    assert any(word in text for word in ("读取", "读回", "暂存", "未完整", "不完整")), (
        "不完整说明里没有可理解的原因（读取/暂存）", text[:240]
    )
    assert not any(word in text for word in LIMIT_WORDS), (
        "原因被描述成「超过上限」—— 与硬上限截断混淆了（契约 §1.4 明确禁止）", text[:240]
    )
    assert adapter.calls == 1, ("不得为找回原文再调一次模型", adapter.calls)
    print("[诊断] 读取失败：交付=%d 字节；警告=%d 条：%s" % (len(delivered.encode("utf-8")), len(warnings), text[:160]))


# ---- 2. 创建 / 写入暂存失败 → 各自的原因必须可区分 -----------------------------------


async def test_spill_create_failure_is_reported_and_distinct(tmp_path: Path):
    with _spill_open_failure(fail_write=False) as injected:
        loop, adapter, result, warnings = await _run(ASCII_TEXT, spill_dir=tmp_path, turn_id="r7_spill_create")
    assert injected["fired"], "暂存创建失败注入没有触发（装置失效）"
    text = _warning_text(warnings)
    assert warnings, ("创建暂存文件失败必须给用户可见说明（不能只写日志）", text[:200])
    assert any(word in text for word in ("创建", "打开", "暂存", "未完整", "不完整")), text[:240]
    assert not any(word in text for word in LIMIT_WORDS), ("创建失败被说成「超过上限」", text[:240])
    assert adapter.calls == 1, adapter.calls
    print("[诊断] 创建失败：交付=%d 字节；警告=%s" % (len((result.final_content or "").encode("utf-8")), text[:140]))


async def test_spill_write_failure_is_reported_and_distinct(tmp_path: Path):
    with _spill_open_failure(fail_write=True) as injected:
        loop, adapter, result, warnings = await _run(ASCII_TEXT, spill_dir=tmp_path, turn_id="r7_spill_write")
    assert injected["fired"], "暂存写入失败注入没有触发（装置失效）"
    text = _warning_text(warnings)
    assert warnings, ("写入暂存失败必须给用户可见说明（不能只写日志）", text[:200])
    assert any(word in text for word in ("写入", "暂存", "未完整", "不完整", "空间")), text[:240]
    assert not any(word in text for word in LIMIT_WORDS), ("写入失败被说成「超过上限」", text[:240])
    assert adapter.calls == 1, adapter.calls
    print("[诊断] 写入失败：交付=%d 字节；警告=%s" % (len((result.final_content or "").encode("utf-8")), text[:140]))


# ---- 3. 正常路径（超过内存上限但暂存正常）：完整交付、0 警告、只 1 次调用 -------------


@pytest.mark.parametrize("label,text", [("ASCII", ASCII_TEXT), ("中文", CHINESE_TEXT)])
async def test_normal_over_threshold_delivers_complete_without_warnings(tmp_path: Path, label, text):
    loop, adapter, result, warnings = await _run(text, spill_dir=tmp_path, turn_id="r7_spill_ok")
    delivered = result.final_content or ""
    assert delivered == text, (
        "%s：暂存正常的超阈值正文必须**完整**交付" % label,
        {"delivered": len(delivered.encode("utf-8")), "want": len(text.encode("utf-8"))},
    )
    assert not warnings, ("%s：正常路径不得产生任何警告" % label, _warning_text(warnings)[:200])
    assert adapter.calls == 1, ("%s：只允许 1 次调用" % label, adapter.calls)
    leftovers = sorted(p.name for p in Path(tmp_path).glob("*" + buffer_module.SPILL_SUFFIX))
    assert not leftovers, ("%s：暂存文件必须收敛" % label, leftovers)
    print("[诊断] %s 正常超阈值：交付=%d 字节；警告=0；暂存残留=%d" % (label, len(delivered.encode("utf-8")), len(leftovers)))


# ---- 4. 合法声明长正文仍真流式（不受暂存改动影响） -----------------------------------


async def test_declared_long_text_is_still_streaming(tmp_path: Path):
    body = "乙" * (SPILL_TEXT_BYTES // 3)
    text = ANSWER_MARKER + "\n" + body
    loop, adapter, result, warnings = await _run(text, spill_dir=tmp_path, turn_id="r7_declared_long")
    assert result.final_content == body, (len(result.final_content or ""), len(body))
    events = [e.data for e in loop.bus._history if e.type.value == "ASSISTANT"]
    assert any(e.get("streaming") is True and e.get("interim") is False for e in events), (
        "合法声明的长正文必须仍然真流式", [(e.get("interim"), e.get("streaming")) for e in events][:3]
    )
    assert not warnings, _warning_text(warnings)[:200]
