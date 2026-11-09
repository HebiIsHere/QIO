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


def test_declaration_is_exposed_for_persistence():
    """被接受的声明要能被落库/事件取走（前端那条「已核对」标记靠它）。"""
    facts = TurnFacts()
    facts.record_declaration(accepted=True, basis="版本 a1b2；测试 1/1 通过")
    assert facts.declaration == {
        "accepted": True,
        "basis": "版本 a1b2；测试 1/1 通过",
        "claims": [],
    }


def test_declaration_is_none_when_not_accepted():
    facts = TurnFacts()
    assert facts.declaration is None
    facts.record_declaration(accepted=False, basis=None)
    assert facts.declaration is None


def test_record_facts_keeps_the_claims():
    facts = TurnFacts()
    facts.record_facts({
        "declaration": {
            "accepted": True,
            "basis": "版本 a1b2",
            "claims": ["test_passed", "registered"],
        }
    })
    assert facts.declaration["claims"] == ["test_passed", "registered"]

# ---------------------------------------------------------------------------
# 知识提交（propose_knowledge）：结构化事实 → 可读的核对说明
#
# 截图缺陷：知识提交在「依据校验」这一步失败后，默认可见区只有
# 「N 项失败：evidence_ids 必须来自…」这种技术原文，模型随后仍可能说
# 「已让它等你审批」。这里钉住：失败用什么程序字段表达、注记怎么说人话、
# 后续成功如何覆盖失败（有证据按证据呈现）。
# ---------------------------------------------------------------------------


def _knowledge(state: str, code: str) -> dict:
    return {"knowledge_proposal": {"state": state, "code": code}}


def test_knowledge_failure_gets_a_readable_annotation():
    """证据校验失败：默认注记说人话，不把技术原文当成结论。"""
    facts = TurnFacts()
    facts.record_facts(_knowledge("failed", "evidence_not_found"))
    note = facts.annotation() or ""
    assert "知识提交未成功" in note
    assert "引用依据未通过核对" in note
    assert "evidence_ids" not in note


def test_knowledge_failure_is_not_repeated_as_raw_tool_error():
    """同一次失败既记了工具终态又记了结构化事实：默认注记只说一遍。"""
    facts = TurnFacts()
    facts.record_tool(
        call_id="c1",
        tool_name="propose_knowledge",
        ok=False,
        status="failed",
        error="evidence_ids 必须来自本轮实际证据目录，未找到：ghost",
    )
    facts.record_facts(_knowledge("failed", "evidence_not_found"))
    note = facts.annotation() or ""
    assert note.count("知识提交") == 1
    assert "propose_knowledge 执行失败" not in note
    assert "未找到：ghost" not in note


def test_knowledge_failure_with_unknown_code_does_not_invent_a_reason():
    facts = TurnFacts()
    facts.record_facts(_knowledge("failed", "some_future_code"))
    note = facts.annotation() or ""
    assert "知识提交未成功" in note
    assert "引用依据" not in note


def test_awaiting_approval_is_not_a_failure():
    facts = TurnFacts()
    facts.record_facts(_knowledge("awaiting_approval", "awaiting_user_approval"))
    assert facts.annotation() is None
    assert facts.knowledge_proposal == {
        "state": "awaiting_approval",
        "code": "awaiting_user_approval",
    }


def test_later_proposal_without_identity_does_not_clear_an_earlier_failure():
    """没有身份关联时不得默认是同一条重试：A 失败 + B 等待审批仍要保留 A 的失败。"""
    facts = TurnFacts()
    facts.record_tool(call_id="c1", tool_name="propose_knowledge", ok=False, status="failed")
    facts.record_facts(_knowledge("failed", "evidence_not_found"))
    facts.record_tool(call_id="c2", tool_name="propose_knowledge", ok=True, status="success")
    facts.record_facts(_knowledge("awaiting_approval", "awaiting_user_approval"))
    assert [record["state"] for record in facts.knowledge_proposals] == [
        "failed",
        "awaiting_approval",
    ]
    note = facts.annotation() or ""
    assert "知识提交未成功" in note


def test_rejected_knowledge_content_is_annotated_readably():
    facts = TurnFacts()
    facts.record_facts(_knowledge("rejected", "rejected_content"))
    note = facts.annotation() or ""
    assert "知识提交未成功" in note
    assert "拒绝" in note


def test_knowledge_facts_ignore_garbage():
    facts = TurnFacts()
    facts.record_facts({"knowledge_proposal": "nope"})
    facts.record_facts({"knowledge_proposal": {"state": "weird", "code": "x"}})
    facts.record_facts({"knowledge_proposal": {"state": "failed"}})
    assert facts.knowledge_proposal is None
