r"""E 核验 · 确认「顺序依赖」的来源：哪个测试模块把派生任务的实例身份绑定了。

用法（在 <wt>\backend 下）：
  $env:PYTHONPATH='<wt>\backend\src'
  python -m pytest tests/test_derivation_recovery.py <本文件> -q -s -p no:cacheprovider
"""

from __future__ import annotations

from agent.services import derived_tasks as dt


def test_report_who_bound_the_instance() -> None:
    print("\n[E-ord] 此刻 bound_instance_id =", dt.bound_instance_id())
    print("[E-ord] _REGISTRY =", type(dt._REGISTRY).__name__ if dt._REGISTRY is not None else None)
