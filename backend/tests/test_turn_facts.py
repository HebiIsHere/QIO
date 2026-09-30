"""每轮事实台账：什么时候补注记、什么时候什么都不加。"""

from __future__ import annotations

from agent.core import turn_facts as tf
from agent.core.turn_facts import DevTaskFact, TurnFacts


def _failed(facts: TurnFacts, name: str = "dev_run_tests", error: str | None = "断言不匹配"):
    facts.record_tool(
        call_id="c1", tool_name=name, ok=False, status="failed",
        category="assertion", error=error,
    )


def test_status_constants_match_tool_feedback():
    from agent.core import tool_feedback

    assert tf.STATUS_FAILED == tool_feedback.STATUS_FAILED
    assert tf.STATUS_CANCELLED == tool_feedback.STATUS_CANCELLED


def test_clean_turn_adds_nothing():
    facts = TurnFacts()
    facts.record_tool(call_id="c1", tool_name="dev_run_tests", ok=True, status="success")
    assert facts.annotation() is None


def test_unresolved_tool_failure_is_annotated():
    facts = TurnFacts()
    _failed(facts)
    note = facts.annotation()
    assert note is not None
    assert "dev_run_tests" in note
    assert "断言不匹配" in note
    assert "系统核对" in note


def test_later_success_clears_the_same_tool():
    facts = TurnFacts()
    _failed(facts)
    facts.record_tool(call_id="c2", tool_name="dev_run_tests", ok=True, status="success")
    assert facts.annotation() is None


def test_other_tool_failure_does_not_clear_a_different_tool():
    facts = TurnFacts()
    _failed(facts, name="dev_run_tests")
    facts.record_tool(call_id="c2", tool_name="dev_write_file", ok=True, status="success")
    assert facts.annotation() is not None


def test_cancelled_is_not_a_failure():
    facts = TurnFacts()
    facts.record_tool(call_id="c1", tool_name="run_shell", ok=False, status="cancelled")
    assert facts.annotation() is None


def test_accepted_declaration_suppresses_the_note():
    facts = TurnFacts()
    _failed(facts)
    facts.record_declaration(accepted=True, basis="测试 1/1 通过 · 版本 a1b2")
    assert facts.declaration_accepted is True
    assert facts.annotation() is None


def test_rejected_declaration_does_not_suppress_the_note():
    facts = TurnFacts()
    _failed(facts)
    facts.record_declaration(accepted=False, basis=None)
    assert facts.annotation() is not None


def test_dev_task_without_evidence_is_annotated():
    facts = TurnFacts()
    facts.record_dev_task(DevTaskFact(task_id="ws_ab12cd34ef56", tool_name="create_tool"))
    note = facts.annotation()
    assert note is not None
    assert "ws_ab12cd34ef56" in note
    assert "测试证据" in note


def test_dev_task_with_failed_test_is_annotated():
    facts = TurnFacts()
    facts.record_dev_task(DevTaskFact(
        task_id="ws_ab12cd34ef56",
        tool_name="dev_run_tests",
        test_state="current",
        test_passed=False,
        test_summary="0/1 tests passed",
    ))
    note = facts.annotation() or ""
    assert "0/1 tests passed" in note


def test_stale_evidence_after_a_pass_is_annotated():
    facts = TurnFacts()
    facts.record_dev_task(DevTaskFact(
        task_id="ws_ab12cd34ef56", tool_name="dev_write_file",
        test_state="stale", test_passed=True, test_summary="1/1 tests passed",
    ))
    note = facts.annotation() or ""
    assert "失效" in note and "重跑" in note


def test_subagent_task_does_not_require_tests():
    facts = TurnFacts()
    facts.record_dev_task(DevTaskFact(
        task_id="ws_ab12cd34ef56", tool_name="dev_run_tests", requires_tests=False,
    ))
    assert facts.annotation() is None


def test_dev_task_latest_fact_wins():
    facts = TurnFacts()
    facts.record_dev_task(DevTaskFact(task_id="ws_ab12cd34ef56", tool_name="create_tool"))
    facts.record_dev_task(DevTaskFact(
        task_id="ws_ab12cd34ef56", tool_name="dev_run_tests",
        test_state="current", test_passed=True, test_summary="1/1 tests passed",
    ))
    assert facts.annotation() is None


def test_annotation_is_redacted_and_has_no_braces():
    facts = TurnFacts()
    _failed(facts, name="web_fetch", error="auth failed api_key=sk-abcdef123456 {'a': 1}")
    note = facts.annotation() or ""
    assert "sk-abcdef123456" not in note
    assert "{" not in note and "}" not in note


def test_annotation_is_capped_and_mentions_the_rest():
    facts = TurnFacts()
    for i in range(5):
        _failed(facts, name=f"tool_{i}", error="boom")
    note = facts.annotation(limit=200) or ""
    assert "另有" in note
    assert len(note) <= 200 + len("\n…[核对说明已截断]")


def test_record_facts_reads_dev_task_and_declaration():
    facts = TurnFacts()
    facts.record_facts({
        "dev_task": {
            "id": "ws_ab12cd34ef56",
            "tool_name": "dev_run_tests",
            "phase": "testing_passed",
            "version": "a" * 64,
            "submitted": False,
            "test": {"state": "current", "passed": True, "summary": "1/1 tests passed"},
        },
        "declaration": {"accepted": True, "basis": "测试 1/1 通过"},
    })
    assert facts.declaration_accepted is True
    assert facts.annotation() is None


def test_record_facts_ignores_garbage():
    facts = TurnFacts()
    facts.record_facts(None)
    facts.record_facts({"dev_task": "nope"})
    facts.record_facts({"dev_task": {"id": ""}})
    assert facts.annotation() is None
