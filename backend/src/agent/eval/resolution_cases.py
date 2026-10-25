# -*- coding: utf-8 -*-
"""用户要求解析的固定标注案例集（R9-G W1）。

每个案例给出一条**新的用户消息**与若干已有 active 要求（label + 原话 + 可选修正链），
人工标注期望关系：
    new      —— 新任务/新目标；
    amend    —— 对某条已有要求的补充/修正（给出 label）；
    withdraw —— 撤回某条已有要求（给出 label）。
真实模型评估时由后端 seed 这些已有要求，再把 ledger.candidates() 交给模型解析；
预期不是模型自评。

字段：
    id / split / message / existing / expected_relation / expected_target_label
    note / risk / allowed_objects（后端可解析对象，用于校验结构化 limits）
"""

from __future__ import annotations

RESOLUTION_CASES: tuple[dict, ...] = (
    # ---- tune ---------------------------------------------------------
    {
        "id": "new_task_simple",
        "split": "tune",
        "message": "帮我把 D:/work/季度报告.md 整理成一份纪要。",
        "existing": (),
        "expected_relation": "new",
        "expected_target_label": None,
        "note": "没有任何已有要求：明确是新任务。",
    },
    {
        "id": "amend_same_goal",
        "split": "tune",
        "message": "补充一下：纪要只保留三段结论，别加新内容。",
        "existing": (
            {
                "label": "report",
                "text": "把 D:/work/季度报告.md 整理成一份纪要。",
            },
        ),
        "expected_relation": "amend",
        "expected_target_label": "report",
        "note": "同一目标的补充/范围调整。",
    },
    {
        "id": "withdraw_same_goal",
        "split": "tune",
        "message": "算了，刚才整理报告的任务取消吧。",
        "existing": (
            {
                "label": "report",
                "text": "把 D:/work/季度报告.md 整理成一份纪要。",
            },
        ),
        "expected_relation": "withdraw",
        "expected_target_label": "report",
        "note": "明确撤回刚才的任务。",
    },
    {
        "id": "new_different_topic",
        "split": "tune",
        "message": "接下来做另一件事：整理一下我的下载目录。",
        "existing": (
            {
                "label": "report",
                "text": "把 D:/work/季度报告.md 整理成一份纪要。",
            },
        ),
        "expected_relation": "new",
        "expected_target_label": None,
        "note": "换了目标/对象，是新任务而不是补充。",
    },
    {
        "id": "amend_after_correction_chain",
        "split": "tune",
        "message": "标题就改成「本周进展」吧，其他保持刚才说的。",
        "existing": (
            {
                "label": "report",
                "text": "把 D:/work/周报.md 整理一下。",
                "corrections": ("只改标题，正文不要动。",),
            },
        ),
        "expected_relation": "amend",
        "expected_target_label": "report",
        "note": "已有修正链（version=2）：这次是继续修正同一目标。",
    },
    {
        "id": "new_capability_question",
        "split": "tune",
        "message": "QIO 能整理 PDF 文件吗？",
        "existing": (
            {
                "label": "report",
                "text": "把 D:/work/季度报告.md 整理成一份纪要。",
            },
        ),
        "expected_relation": "new",
        "expected_target_label": None,
        "risk": "能力询问只是在问能做什么，不应被视为对已有任务的 amend/withdraw。",
        "note": "能力询问不是对已有要求的补充。",
    },
    # ---- holdout ------------------------------------------------------
    {
        "id": "amend_restrict_scope",
        "split": "holdout",
        "message": "只跑项目里的单元测试，别改代码。",
        "existing": (
            {
                "label": "project",
                "text": "修复 D:/proj 项目里的问题。",
            },
        ),
        "expected_relation": "amend",
        "expected_target_label": "project",
        "allowed_objects": ({"kind": "directory", "value": "D:/proj"},),
        "note": "同一修复任务的范围收紧（只测试、不改代码）。",
    },
    {
        "id": "amend_add_second_object",
        "split": "holdout",
        "message": "把 D:/work/报告B.md 也一起改一下。",
        "existing": (
            {
                "label": "report_a",
                "text": "把 D:/work/报告A.md 改一下。",
            },
        ),
        "expected_relation": "amend",
        "expected_target_label": "report_a",
        "note": "同一要求的对象扩展（also/一起），不是全新任务。",
    },
    {
        "id": "withdraw_one_of_two",
        "split": "holdout",
        "message": "整理下载目录那个任务不用做了，取消掉。",
        "existing": (
            {
                "label": "report",
                "text": "把 D:/work/季度报告.md 整理成一份纪要。",
            },
            {
                "label": "downloads",
                "text": "整理一下 D:/Downloads 目录。",
            },
        ),
        "expected_relation": "withdraw",
        "expected_target_label": "downloads",
        "risk": "候选有多条时不能撤错目标；后端校验 target id + version。",
        "note": "按任务内容撤回（评估输入不含完整对话历史，位置指代无法可靠解析，故用内容指代）。",
    },
    {
        "id": "withdraw_without_target",
        "split": "holdout",
        "message": "刚才那个先取消吧。",
        "existing": (),
        "expected_relation": "new",
        "expected_target_label": None,
        "risk": "没有候选时必须按新要求记录，不能凭空编造 target。",
        "note": "无候选可撤回 → 预期 new（后端不会关键词兜底猜测）。",
    },
    {
        "id": "new_after_unrelated_chat",
        "split": "holdout",
        "message": "顺便问下，明天的天气怎么样？",
        "existing": (
            {
                "label": "report",
                "text": "把 D:/work/季度报告.md 整理成一份纪要。",
            },
        ),
        "expected_relation": "new",
        "expected_target_label": None,
        "note": "无关新话题，不是对已有任务的 amend。",
    },
    {
        "id": "amend_explicit_same_goal_paraphrase",
        "split": "holdout",
        "message": "还是那份报告：把结论放到最前面，其余照旧。",
        "existing": (
            {
                "label": "report",
                "text": "把 D:/work/季度报告.md 整理成一份纪要。",
                "corrections": ("只改标题，正文不要动。",),
            },
        ),
        "expected_relation": "amend",
        "expected_target_label": "report",
        "note": "换了措辞但语义指向同一要求（明确延续）。",
    },
)


def resolution_case_by_id(case_id: str) -> dict | None:
    for case in RESOLUTION_CASES:
        if case["id"] == case_id:
            return case
    return None


def resolution_cases(split: str | None = None) -> list[dict]:
    if split:
        return [dict(case) for case in RESOLUTION_CASES if case["split"] == split]
    return [dict(case) for case in RESOLUTION_CASES]


__all__ = [
    "RESOLUTION_CASES",
    "resolution_case_by_id",
    "resolution_cases",
];
