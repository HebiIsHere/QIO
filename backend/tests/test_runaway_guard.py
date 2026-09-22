from __future__ import annotations

from agent.core.guard import GuardVerdict, RunawayGuard


def test_tool_warns_at_fifteen_and_asks_at_thirty():
    """2026-09-22 起：同一工具累计失败 15 次警告、30 次 HALT（由 loop 转成"询问用户"）。"""
    g = RunawayGuard()
    verdicts = [g.observe("web_search", {"q": f"{i}"}, False) for i in range(30)]
    assert verdicts[13] == GuardVerdict.OK       # 第 14 次还不吵
    assert verdicts[14] == GuardVerdict.WARN     # 第 15 次开始警告
    assert all(v == GuardVerdict.WARN for v in verdicts[14:29])
    assert verdicts[29] == GuardVerdict.HALT     # 第 30 次交给用户决定


def test_reset_tool_clears_both_counters():
    """用户点"继续"之后清零：再攒 30 次才重新问，不是下一次失败立刻又问。"""
    g = RunawayGuard()
    for _ in range(30):
        g.observe("run_cmd", {"c": 1}, False)
    assert g.failures_for("run_cmd") == 30
    g.reset_tool("run_cmd")
    assert g.failures_for("run_cmd") == 0
    assert g.observe("run_cmd", {"c": 1}, False) == GuardVerdict.OK


def test_failures_are_counted_per_tool():
    """口径是"同一工具累计"：换个工具不会继承别人的失败计数。"""
    g = RunawayGuard()
    for _ in range(20):
        g.observe("web_search", {"q": "x"}, False)
    assert g.failures_for("web_search") == 20
    assert g.failures_for("fs_read") == 0
    assert g.observe("fs_read", {"path": "a"}, False) == GuardVerdict.OK


def test_success_never_escalates():
    g = RunawayGuard()
    for _ in range(10):
        assert g.observe("web_search", {"q": "x"}, True) == GuardVerdict.OK


def test_distinct_calls_do_not_share_call_counter():
    g = RunawayGuard()
    # 不同参数：同一工具累计，但前几次不应升级
    assert g.observe("web_search", {"q": "a"}, False) == GuardVerdict.OK
    assert g.observe("web_search", {"q": "b"}, False) == GuardVerdict.OK
