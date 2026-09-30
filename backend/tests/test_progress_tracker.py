"""进展判断：按**证据**认定「这一轮没有往前走」。

判定不看标签、也不看失败次数：指纹 =（工具 + 规范化参数 + 结果）。只要连续几次
拿到完全一样的东西，就说明这一轮没有新的信息 —— 无论那次调用报的是成功还是失败。
"""

from __future__ import annotations

from agent.core.progress import ProgressTracker


def _observe(tracker: ProgressTracker, *, tool="echo", arguments=None, ok=True, result_text="x"):
    return tracker.observe(
        tool=tool,
        arguments=arguments if arguments is not None else {"text": "x"},
        ok=ok,
        result_text=result_text,
    )


def test_same_call_same_result_three_times_is_no_progress():
    tracker = ProgressTracker()

    assert _observe(tracker) is None
    assert _observe(tracker) is None
    reason = _observe(tracker)

    assert reason is not None
    assert "echo" in reason
    assert "3" in reason


def test_a_different_result_counts_as_progress():
    """同样的调用给出新内容 = 确实往前走了（例如文件被改过、任务状态变了）。"""
    tracker = ProgressTracker()

    for i in range(5):
        assert (
            _observe(tracker, tool="dev_list_files", arguments={"workspace": "ws_1"}, result_text=f"文件 {i}")
            is None
        )


def test_different_arguments_are_not_repetition():
    tracker = ProgressTracker()

    for i in range(5):
        assert _observe(tracker, arguments={"text": str(i)}) is None


def test_repeated_failures_with_the_same_error_also_count():
    """成功没信息增量要停，失败反复踩同一个坑同样要停。"""
    tracker = ProgressTracker()

    for _ in range(2):
        assert (
            _observe(tracker, tool="dev_run_tests", arguments={"workspace": "ws_1"}, ok=False, result_text="同样的错") is None
        )
    reason = _observe(
        tracker, tool="dev_run_tests", arguments={"workspace": "ws_1"}, ok=False, result_text="同样的错"
    )

    assert reason is not None
    assert "dev_run_tests" in reason


def test_failure_after_success_is_not_treated_as_repetition():
    """同一次调用的成败变了就是新信息，不能算重复。"""
    tracker = ProgressTracker()
    _observe(tracker, result_text="same")
    _observe(tracker, result_text="same")

    assert _observe(tracker, ok=False, result_text="same") is None


def test_interleaved_calls_reset_the_count():
    tracker = ProgressTracker()

    _observe(tracker, tool="a")
    _observe(tracker, tool="b")
    _observe(tracker, tool="a")
    assert _observe(tracker, tool="b") is None


def test_limit_is_configurable():
    tracker = ProgressTracker(repeat_limit=2)

    assert _observe(tracker) is None
    assert _observe(tracker) is not None
