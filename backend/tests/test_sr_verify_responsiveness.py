"""契约 6 响应性组反例（test_sr_verify_responsiveness）。

对应冻结契约 2026-10-09 的第 5 组：把 embed / 文件扫描替换成「闸门方式
暂停」（threading.Event 控制），用健康检查模拟（心跳协程）观察事件循环
是否被推迟。证据含线程 id 与相对时间戳（probes 的 records 字段）。

反例（未修复基线）：
* ToolRouter 没有 route_async —— 路由耗时计算只能在事件循环上同步跑；
* route() 期间心跳（同期挂起的事件循环工作）零推进；
* fs_find 的目录扫描也冻结整个循环。

修复后期望：route_async 存在且不阻塞循环；embed 与 _find_files 都离开
主线程（线程 id 取证）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parents[2] / "scripts" / "sr-verify"
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import probes  # noqa: E402  (scripts/sr-verify/probes.py)


async def test_sr_verify_async_route_exists():
    ev = await probes._probe_responsiveness_gate(label="responsiveness_event_loop_gate")
    assert ev["route_async_exists"], (
        "反例：ToolRouter.route_async 不存在（契约 4 要求的异步路由入口缺失，"
        "耗时计算只能阻塞事件循环）"
    )


async def test_sr_verify_route_async_does_not_starve_event_loop():
    ev = await probes._probe_responsiveness_gate(label="responsiveness_event_loop_gate")
    blocked = ev.get("route_async_blocks_loop")
    assert blocked is False, (
        "反例：异步路由期间心跳推进数=%s —— 事件循环仍被推迟（闸门期间全部冻结）"
        % ev.get("route_async_beats_in_window")
    )
    assert ev["embed_off_main_thread"], (
        "embed 线程 id=%s，主线程=%s —— 耗时 embedding 仍在主线程执行"
        % (ev.get("embed_thread_id"), ev["main_thread_id"])
    )


async def test_sr_verify_fs_find_does_not_starve_event_loop():
    ev = await probes._probe_responsiveness_gate(label="responsiveness_event_loop_gate")
    blocked = ev.get("fs_find_blocks_loop")
    assert blocked is False, (
        "反例：fs_find 目录扫描期间心跳推进数=%s —— 事件循环被文件 I/O 冻结"
        % ev.get("fs_find_beats_in_window")
    )
    assert ev["fs_find_off_main_thread"], (
        "扫描线程 id=%s，主线程=%s —— 文件扫描仍在主线程执行"
        % (ev.get("fs_find_thread_id"), ev["main_thread_id"])
    )


async def test_sr_verify_baseline_route_blocks_loop_documented():
    """基线取证（信息项）：同步 route() 在事件循环上确实冻结心跳。

    这条不设「修复后」期望（契约要求保留同步 route()，接线由 Lead 换成
    route_async）；它钉住的是反例证据本身，防止证据机制失效。
    """
    ev = await probes._probe_responsiveness_gate(label="responsiveness_event_loop_gate")
    assert ev["sync_route_blocks_loop"], (
        "证据机制失效：同步 route() 竟未冻结心跳（闸门装置未生效）"
    )