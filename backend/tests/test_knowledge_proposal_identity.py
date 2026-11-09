# -*- coding: utf-8 -*-
"""问题 3：同一轮多次知识提交分别保留结果；只有同一身份的重试才更新该项。

被钉住的事实：
1. 不同提交（无身份关联）各自保留 —— A 失败、B 等待审批时两者都可见，
   不能因为最后一次是成功/等待审批就把前一次失败抹掉；
2. 同一身份（fingerprint / knowledge_id）的后续成功**更新**该项，不永久保留旧失败；
3. 身份只来自程序字段（工具给的内容/类别/范围指纹），不做文案关键词匹配。
"""

from __future__ import annotations

from agent.core import tool_feedback
from agent.core.turn_facts import TurnFacts
from agent.tools import knowledge_propose
from agent.tools.base import ToolResult


def _fact(state: str, code: str, *, fingerprint: str | None = None, knowledge_id: str | None = None) -> dict:
    payload: dict = {"state": state, "code": code}
    if fingerprint:
        payload["fingerprint"] = fingerprint
    if knowledge_id:
        payload["knowledge_id"] = knowledge_id
    return {"knowledge_proposal": payload}


def test_fingerprint_is_stable_for_the_same_knowledge():
    a = knowledge_propose._knowledge_fingerprint("user_profile", "  我喜欢简洁  ", ["user_1"])
    b = knowledge_propose._knowledge_fingerprint("user_profile", "我喜欢简洁", ["user_1"])
    assert a == b


def test_fingerprint_differs_for_different_content_category_or_scope():
    base = knowledge_propose._knowledge_fingerprint("user_profile", "我喜欢简洁", ["user_1"])
    assert base != knowledge_propose._knowledge_fingerprint("user_profile", "我喜欢详细", ["user_1"])
    assert base != knowledge_propose._knowledge_fingerprint("goal", "我喜欢简洁", ["user_1"])
    assert base != knowledge_propose._knowledge_fingerprint("user_profile", "我喜欢简洁", ["user_2"])


def test_identityless_failure_and_awaiting_are_both_kept():
    facts = TurnFacts()
    facts.record_facts(_fact("failed", "evidence_not_found"))
    facts.record_facts(_fact("awaiting_approval", "awaiting_user_approval"))
    assert [record["state"] for record in facts.knowledge_proposals] == [
        "failed",
        "awaiting_approval",
    ]
    note = facts.annotation() or ""
    assert "知识提交未成功" in note
    assert "引用依据未通过核对" in note


def test_same_identity_retry_updates_the_submission():
    facts = TurnFacts()
    facts.record_facts(_fact("failed", "evidence_not_found", fingerprint="fp_same"))
    facts.record_facts(_fact("awaiting_approval", "awaiting_user_approval", fingerprint="fp_same"))
    assert facts.knowledge_proposals == [
        {"state": "awaiting_approval", "code": "awaiting_user_approval", "fingerprint": "fp_same"}
    ]
    assert facts.annotation() is None


def test_same_knowledge_id_retry_updates_the_submission():
    """旧数据没有 fingerprint 时，真实 knowledge_id 也是可追踪身份。"""
    facts = TurnFacts()
    facts.record_facts(_fact("failed", "create_failed", knowledge_id="kn_1"))
    facts.record_facts(_fact("active", "already_active", knowledge_id="kn_1"))
    assert facts.knowledge_proposals == [
        {"state": "active", "code": "already_active", "knowledge_id": "kn_1"}
    ]
    assert facts.annotation() is None


def test_two_distinct_failures_are_each_annotated():
    facts = TurnFacts()
    facts.record_facts(_fact("failed", "evidence_not_found", fingerprint="fp_a"))
    facts.record_facts(_fact("failed", "invalid_scope", fingerprint="fp_b"))
    note = facts.annotation() or ""
    assert note.count("知识提交未成功") == 2
    assert "引用依据未通过核对" in note
    assert "归属范围无效" in note


def test_tool_facts_expose_the_proposal_identity_as_knowledge_key():
    result = ToolResult(
        ok=False,
        content="x",
        error="x",
        facts={
            "knowledge_proposal": {
                "state": "failed",
                "code": "evidence_not_found",
                "fingerprint": "fp_9",
            }
        },
    )
    assert tool_feedback.facts(result)["knowledge_key"] == "fp_9"


def test_tool_facts_fall_back_to_knowledge_id_for_old_producers():
    result = ToolResult(
        ok=True,
        content="ok",
        status="awaiting_approval",
        facts={
            "knowledge_proposal": {
                "state": "awaiting_approval",
                "code": "awaiting_user_approval",
                "knowledge_id": "kn_7",
            }
        },
    )
    assert tool_feedback.facts(result)["knowledge_key"] == "kn_7"


def test_unrelated_tools_have_no_knowledge_key():
    result = ToolResult(ok=False, content="x", error="x", facts=None)
    assert tool_feedback.facts(result)["knowledge_key"] is None
