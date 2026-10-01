# -*- coding: utf-8 -*-
"""分段边界评测语料：确定性生成（覆盖任务书要求的七类边界 + 三类误判陷阱）。

类别：
  same_stage_continue 同一工作阶段继续        → 不该切
  stage_change        阶段改变（进入新交付）  → 应切
  new_goal            新目标                  → 应切
  short_ack           短确认                  → 不该切
  correction          对前一步的修正          → 不该切
  side_branch         插入旁支                → 不该切
  return_to_task      回到原任务              → 不该切
  negated / quoted / hypothetical             → 不该切（规则层已有的陷阱）

用法（backend 目录下）：uv run --frozen python evals/boundary_semantic/build_cases.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "cases.jsonl"

# (类别, 当前输入, 上一轮上下文, 期望, 说明)
CASES = [
    # -- 同一阶段继续（不该切） ------------------------------------------
    ("same_stage_continue", "这段逻辑再帮我看看有没有边界问题", "我们正在写记忆封存的实现", "continue", "同阶段追问"),
    ("same_stage_continue", "把这个函数拆小一点吧", "刚才在重构摘要生成器", "continue", "同阶段调整"),
    ("same_stage_continue", "测试也一起补上", "正在实现分段策略", "continue", "同阶段补工作"),
    ("same_stage_continue", "这里的错误处理要加上重试", "在写派生任务的重试逻辑", "continue", "同阶段细化"),
    ("same_stage_continue", "再跑一遍刚才那个用例", "在调试排序层的用例", "continue", "同阶段复现"),
    ("same_stage_continue", "日志级别调成 debug 看看", "正在排查注入为空的问题", "continue", "同阶段排查"),
    # -- 阶段改变（该切） -------------------------------------------------
    ("stage_change", "方案就这样，开始写代码吧", "我们刚把设计方案讨论完", "split", "明确开工指令"),
    ("stage_change", "好，进入开发阶段", "需求已经确认", "split", "明确进入开发"),
    ("stage_change", "按这个方案实现吧", "评审通过了", "split", "按方案实现"),
    ("stage_change", "需求没问题了，动手实现", "需求文档刚定稿", "split", "明确开工"),
    ("stage_change", "设计定稿，开始落地", "设计评审结束", "split", "明确落地"),
    ("stage_change", "方案没问题，我这就去把接口写出来", "刚讨论完接口设计", "split", "无固定词组的开工表达（语义信号应能识别）"),
    ("stage_change", "讨论够了，我们直接把它做出来", "方案已经聊透", "split", "无固定词组的开工表达"),
    ("stage_change", "那就开工", "评审结论已出", "split", "口语开工"),
    # -- 新目标（该切） ---------------------------------------------------
    ("new_goal", "另外我想做个导出功能", "刚才在聊检索排序", "split", "新目标"),
    ("new_goal", "还有个事，客户端也要接这个接口", "在讨论服务端实现", "split", "新增目标"),
    ("new_goal", "顺便把埋点也加上", "在写导出功能", "split", "追加目标"),
    ("new_goal", "下一步做多语言支持", "当前功能已经完成", "split", "下一个目标"),
    # -- 短确认（不该切） -------------------------------------------------
    ("short_ack", "好", "刚给出三条建议", "continue", "短确认"),
    ("short_ack", "嗯嗯", "刚解释完原因", "continue", "短确认"),
    ("short_ack", "可以", "刚提了两个方案", "continue", "短确认"),
    ("short_ack", "就这样", "刚确认了细节", "continue", "短确认"),
    ("short_ack", "ok", "刚给出草稿", "continue", "英文短确认"),
    ("short_ack", "开始吧", "刚说可以动手了", "continue", "短确认跟随下一步"),
    # -- 对前一步的修正（不该切） ------------------------------------------
    ("correction", "不对，我说的不是这个意思", "刚解释了一段判断逻辑", "continue", "否定前一步"),
    ("correction", "其实应该反过来", "刚给出排序顺序", "continue", "修正"),
    ("correction", "换成先判断再取值", "刚讨论取值顺序", "continue", "修正"),
    ("correction", "再补充一下，还有第三种情况", "刚列了两种场景", "continue", "补充"),
    ("correction", "为什么不能直接用现成的库", "刚给出自研方案", "continue", "追问"),
    # -- 插入旁支（不该切） ------------------------------------------------
    ("side_branch", "对了，那个日志文件在哪来着", "正在写实现", "continue", "旁支插问"),
    ("side_branch", "等我接个电话", "正在讨论方案", "continue", "中断插话"),
    ("side_branch", "顺带问一句，服务器密码是多少", "正在部署", "continue", "旁支插问"),
    ("side_branch", "先看下现在几点", "正在排期", "continue", "旁支插问"),
    # -- 回到原任务（不该切） ----------------------------------------------
    ("return_to_task", "回到刚才那个问题", "中间插了一段别的", "continue", "回到原任务"),
    ("return_to_task", "继续说实现的事", "刚聊了几句别的", "continue", "回到原任务"),
    ("return_to_task", "还是刚才那个方案，接着往下", "中间确认了别的事", "continue", "回到原任务"),
    # -- 规则陷阱：否定 / 引用 / 假设（不该切） ----------------------------
    ("negated", "先不要开始写代码", "方案还没定", "continue", "否定句"),
    ("negated", "别急着动手实现", "还在讨论", "continue", "否定句"),
    ("negated", "暂时不进入开发", "文档还没评审", "continue", "否定句"),
    ("quoted", "他当时说「开始写代码」我就笑了", "在回忆一次评审", "continue", "引号里的是引用"),
    ("quoted", "文档里写着“按照方案实现”，你确认下", "在核对需求文档", "continue", "引用文档措辞"),
    ("hypothetical", "如果明天开始写代码，需要几天", "在估算排期", "continue", "假设句"),
    ("hypothetical", "等需求定了就动手实现", "需求还没定", "continue", "条件句"),
    ("hypothetical", "假设我们现在进入开发阶段，会缺什么", "在评估风险", "continue", "假设句"),
]


def main() -> int:
    lines = []
    for i, (cat, text, recent, expect, note) in enumerate(CASES):
        lines.append(
            json.dumps(
                {
                    "id": f"b{i:02d}",
                    "category": cat,
                    "text": text,
                    "recent": recent,
                    "expect": expect,
                    "note": note,
                },
                ensure_ascii=False,
            )
        )
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cats: dict = {}
    for c in CASES:
        cats[c[0]] = cats.get(c[0], 0) + 1
    print(f"cases={len(CASES)} -> {OUT}")
    print("categories:", json.dumps(cats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
