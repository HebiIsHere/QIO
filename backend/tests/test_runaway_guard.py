from __future__ import annotations

from agent.core.guard import GuardVerdict, RunawayGuard


def test_same_call_thresholds():
    g = RunawayGuard()
    args = {"q": "x"}
    assert g.observe("web_search", args, False) == GuardVerdict.OK          # 1
    assert g.observe("web_search", args, False) == GuardVerdict.WARN        # 2
    assert g.observe("web_search", args, False) == GuardVerdict.WARN        # 3
    assert g.observe("web_search", args, False) == GuardVerdict.WARN        # 4
    assert g.observe("web_search", args, False) == GuardVerdict.BLOCK       # 5


def test_same_tool_halt_at_eight():
    g = RunawayGuard()
    verdicts = [g.observe("run_cmd", {"c": i}, False) for i in range(8)]
    assert verdicts[-1] == GuardVerdict.HALT
    assert GuardVerdict.HALT not in verdicts[:-1]


def test_success_never_escalates():
    g = RunawayGuard()
    for _ in range(10):
        assert g.observe("web_search", {"q": "x"}, True) == GuardVerdict.OK


def test_halt_dominates_block_on_same_call():
    g = RunawayGuard()
    last = None
    for _ in range(8):
        last = g.observe("web_search", {"q": "same"}, False)
    assert last == GuardVerdict.HALT


def test_distinct_calls_do_not_share_call_counter():
    g = RunawayGuard()
    # 不同参数：同一工具累计，但调用级计数各自独立，前几次不应 BLOCK
    assert g.observe("web_search", {"q": "a"}, False) == GuardVerdict.OK
    assert g.observe("web_search", {"q": "b"}, False) == GuardVerdict.OK
