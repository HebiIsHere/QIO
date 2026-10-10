"""契约 6 打码组反例（test_sr_verify_redaction）。

对应冻结契约 2026-10-09 的第 3 组：register 假密钥 → 假 provider 异常 →
直接经 bus + sse_format 抓字节断言。全部断言用**布尔取证**（原始值绝不
出现在断言消息、pytest 报告或证据文件里）。

反例（未修复基线）：bus.publish 不打码 event.data —— ERROR 事件原文
（异常文本里的注册密钥、结构化 api_key 字段）原样出现在：
* 实时 SSE 字节；
* 重连重放（Last-Event-ID 之后的补发）。
日志路径（LogRecord 工厂）基线已覆盖，所以这里同时钉住它不被回退。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parents[2] / "scripts" / "sr-verify"
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import probes  # noqa: E402  (scripts/sr-verify/probes.py)


async def test_sr_verify_registered_secret_not_in_sse_or_replay_bytes():
    ev = await probes.probe_redaction_bundle(label="redaction_bus_sse")
    assert ev["registry_works"], "密钥登记表未生效（sanity 前提失败）"
    assert not ev["sse_raw_leaked"], (
        "反例：实时 SSE 字节含未打码的注册密钥原文（布尔取证，不展示原文）"
    )
    assert ev["sse_has_marker"], "SSE 字节未含打码标记"
    assert ev["event_ids_present"], "打码业务语义被破坏：事件 id / turn_id / 业务字段应保留"


async def test_sr_verify_registered_secret_not_in_replay_bytes():
    ev = await probes.probe_redaction_bundle(label="redaction_bus_sse")
    assert not ev["replay_raw_leaked"], "反例：重连重放的 SSE 字节含未打码密钥原文"
    assert ev["replay_has_marker"], "重放字节未含打码标记"


async def test_sr_verify_structured_api_key_field_redacted():
    ev = await probes.probe_redaction_bundle(label="redaction_bus_sse")
    assert not ev["api_key_raw_leaked"], "反例：结构化 api_key 字段值未打码（先打码再序列化缺失）"


async def test_sr_verify_log_text_clean():
    ev = await probes.probe_redaction_bundle(label="redaction_bus_sse")
    assert not ev["caplog_raw_leaked"], "日志文本含未打码的注册密钥原文（LogRecord 工厂被回退）"
