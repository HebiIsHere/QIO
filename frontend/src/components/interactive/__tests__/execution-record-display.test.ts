/**
 * 执行记录可读展示映射（#69）的定向用例。
 *
 * 要求：用户可见处只出现可读名称；未知枚举给可读回退并保留原文；
 * 原始 ID / 内部枚举 / 结构数据只进诊断；任何对象值都不能变成 [object Object]。
 * 纯函数、不联网。
 */
import { describe, expect, it } from "vitest";
import type { IntentBasisRefView, IntentScopeView } from "../../../interactive/intentWorkbench";
import {
  actionLabel,
  authorizationLines,
  basisRoleLabel,
  basisSummaryLine,
  diagnosticJson,
  operationStatusLabel,
  readableValue,
  resultContentDisplay,
  resultKindLabel,
  retryStatusLabel,
  scopeLines,
  scopeSummary,
} from "../../../interactive/executionRecordDisplay";

describe("操作与状态的可读名称", () => {
  it("板面工具与账本动作枚举都映射成中文，不把内部枚举直接给用户", () => {
    expect(actionLabel("board_update_card")).toBe("修改卡片");
    expect(actionLabel("board_create_card")).toBe("创建成果卡片");
    expect(actionLabel("create_card")).toBe("创建卡片");
    expect(actionLabel("update_card")).toBe("修改卡片");
    expect(actionLabel("add_text_card")).toBe("生成文字卡片");
    expect(actionLabel("add_reply_card")).toBe("生成结果卡片");
    expect(actionLabel("group_cards")).toBe("卡片分组");
    expect(actionLabel("move_card")).toBe("移动卡片");
  });

  it("未知 / 缺失 / 非字符串的操作值只给纯可读回退，原始枚举/ID 只进诊断", () => {
    const unknown = actionLabel("legacy_weird");
    expect(unknown).toBe("其他操作");
    expect(unknown).not.toContain("legacy_weird");
    expect(actionLabel("")).toBe("操作未记录");
    expect(actionLabel(undefined)).toBe("操作未记录");
    expect(actionLabel(null)).toBe("操作未记录");
    expect(actionLabel({ name: "未知动作" })).toBe("其他操作");
    // 原始内部枚举 / ID 绝不内嵌在主文本里，只能在诊断 JSON 里查到。
    expect(diagnosticJson({ action: "legacy_weird" })).toContain("legacy_weird");
  });

  it("账本操作状态映射成中文；未知回退原文，缺失不冒充状态", () => {
    expect(operationStatusLabel("applied")).toBe("已生效");
    expect(operationStatusLabel("running")).toBe("执行中");
    expect(operationStatusLabel("pending")).toBe("未开始");
    expect(operationStatusLabel("failed")).toBe("失败");
    expect(operationStatusLabel("conflict")).toBe("冲突");
    expect(operationStatusLabel("weird_status")).toBe("状态未记录");
    expect(operationStatusLabel("weird_status")).not.toContain("weird_status");
    expect(operationStatusLabel("")).toBe("状态未记录");
  });

  it("重试状态同样映射成中文；未知回退原文", () => {
    expect(retryStatusLabel("pending")).toBe("等待自动重试");
    expect(retryStatusLabel("running")).toBe("重试进行中");
    expect(retryStatusLabel("stopped")).toBe("重试已停止");
    expect(retryStatusLabel("exhausted")).toBe("重试次数已用尽");
    expect(retryStatusLabel("completed")).toBe("重试已完成");
    expect(retryStatusLabel("odd")).toBe("重试状态未记录");
    expect(retryStatusLabel("odd")).not.toContain("odd");
    expect(retryStatusLabel("")).toBe("重试状态未记录");
  });

  it("结果类型用可读名称，未知回退原文", () => {
    expect(resultKindLabel("reply")).toBe("回复卡片");
    expect(resultKindLabel("text")).toBe("文字卡片");
    expect(resultKindLabel("file")).toBe("文件卡片");
    expect(resultKindLabel("mystery")).toBe("其他类型");
    expect(resultKindLabel("mystery")).not.toContain("mystery");
    expect(resultKindLabel("")).toBe("结果");
  });
});

describe("确认材料的可读摘要", () => {
  const basis: IntentBasisRefView[] = [
    { card_id: "card_a", version: 3, role: "source" },
    { card_id: "card_b", version: 1, role: "target" },
    { card_id: "card_c", version: 2, role: "context" },
  ];

  it("材料数量与用途用可读名称汇总，原始 card_id 只留在诊断", () => {
    const summary = basisSummaryLine(basis);
    expect(summary).toContain("共 3 份材料");
    expect(summary).toContain("来源 1");
    expect(summary).toContain("目标 1");
    expect(summary).toContain("上下文 1");
    expect(summary).not.toContain("card_a");
    expect(basisRoleLabel("source")).toBe("来源");
    expect(basisRoleLabel("target")).toBe("目标");
    expect(basisRoleLabel("context")).toBe("上下文");
    expect(basisRoleLabel("other")).toBe("其他用途");
    expect(basisRoleLabel("other")).not.toContain("other");
    expect(basisSummaryLine([])).toBe("没有记录确认材料。");
    expect(basisSummaryLine(undefined)).toBe("没有记录确认材料。");
  });

  it("诊断保留原始依据结构", () => {
    const json = diagnosticJson(basis);
    expect(json).toContain("card_a");
    expect(json).toContain("\"version\": 3");
    expect(json).toContain("source");
  });
});

describe("授权范围的可读摘要", () => {
  const scope: IntentScopeView = {
    read_card_ids: ["card_a", "card_b"],
    write_card_ids: ["card_a"],
    group_ids: ["group_1", "group_2"],
    allowed_actions: ["update_card", "create_card"],
    max_new_cards: 2,
    max_new_groups: 1,
    max_new_links: 0,
  };

  function valueOf(lines: Array<{ label: string; value: string }>, label: string): string {
    return lines.find((line) => line.label === label)?.value ?? "";
  }

  it("读/写卡片、组、允许操作都给数量 + 可读名称", () => {
    const lines = scopeLines(scope);
    expect(valueOf(lines, "可读卡片")).toBe("2 张");
    expect(valueOf(lines, "可写卡片")).toBe("1 张");
    expect(valueOf(lines, "分组")).toBe("2 个");
    expect(valueOf(lines, "允许操作")).toBe("2 类：修改卡片、创建卡片");
    expect(valueOf(lines, "新建上限")).toBe("卡片 2 张 · 分组 1 个");
    // 主区域不出现原始 card_id / group_id / 内部动作枚举，原始值只进诊断。
    const text = lines.map((line) => line.value).join(" ");
    expect(text).not.toContain("card_a");
    expect(text).not.toContain("group_1");
    expect(text).not.toContain("update_card");
    expect(diagnosticJson(scope)).toContain("update_card");
    expect(diagnosticJson(scope)).toContain("group_1");
  });

  it("缺失范围不编造；空数组是真实的「0」而不是「没有记录」", () => {
    expect(scopeLines(undefined)).toEqual([{ label: "授权范围", value: "未记录" }]);
    const empty = scopeLines({
      read_card_ids: [],
      write_card_ids: [],
      group_ids: [],
      allowed_actions: [],
    });
    expect(valueOf(empty, "可读卡片")).toBe("0 张");
    expect(valueOf(empty, "允许操作")).toBe("0 类");
    expect(valueOf(empty, "新建上限")).toBe("未记录");
    expect(scopeSummary(undefined)).toBe("范围未记录");
    expect(scopeSummary(scope)).toContain("读取 2 张卡");
    expect(scopeSummary(scope)).toContain("修改 1 张卡");
    expect(scopeSummary(scope)).toContain("组 2 个");
  });

  it("授权快照用可读字段，未知结构值也不出现 [object Object]", () => {
    const lines = authorizationLines({
      version: 3,
      confirmed_at: "2026-10-24T00:00:00.000Z",
      confirmed_by: "user",
      source: "explicit_user_confirmation",
      depends_on: ["intent_1"],
      conflicts_with: [],
      scope,
    });
    expect(valueOf(lines, "授权版本")).toBe("3");
    expect(valueOf(lines, "确认方式")).toBe("用户");
    expect(valueOf(lines, "确认时间")).toBe("2026-10-24T00:00:00.000Z");
    expect(valueOf(lines, "授权来源")).toBe("用户显式确认");
    expect(valueOf(lines, "依赖")).toBe("1 项");
    expect(valueOf(lines, "互斥")).toBe("0 项");
    const text = lines.map((line) => line.value).join(" ");
    expect(text).not.toContain("[object Object]");
    const unknownAuth = authorizationLines({ confirmed_by: { robot: true }, source: "mystery_source" });
    const unknownText = unknownAuth.map((line) => line.value).join(" ");
    expect(unknownText).not.toContain("[object Object]");
    expect(unknownText).not.toContain("robot");
    expect(unknownText).not.toContain("mystery_source");
    expect(unknownText).toContain("其他确认方式");
    expect(unknownText).toContain("其他来源");
    expect(diagnosticJson({ confirmed_by: "机器人", source: "mystery_source" })).toContain("mystery_source");
    expect(authorizationLines(undefined)).toEqual([{ label: "授权快照", value: "未记录" }]);
    // 原始授权结构（含 scope.allowed_actions）只在诊断 JSON 里。
    expect(diagnosticJson({ version: 3, scope })).toContain("allowed_actions");
  });
});

describe("结果正文的对象值", () => {
  it("字符串正文原样显示", () => {
    expect(resultContentDisplay("正文内容")).toEqual({ text: "正文内容", json: "\"正文内容\"" });
  });

  it("对象正文给可读标题 / 摘要，不直接插值", () => {
    const withTitle = resultContentDisplay({ title: "摘要标题", text: "正文内容" });
    expect(withTitle.text).toBe("摘要标题：正文内容");
    expect(withTitle.text).not.toContain("[object Object]");
    expect(withTitle.json).toContain("摘要标题");

    const unknownShape = resultContentDisplay({ a: 1, b: 2 });
    expect(unknownShape.text).toBe("结构化内容（2 个字段）");
    expect(unknownShape.text).not.toContain("[object Object]");
    expect(unknownShape.json).toContain("\"a\": 1");

    const array = resultContentDisplay([{ title: "甲" }, { title: "乙" }]);
    expect(array.text).toBe("共 2 项结构化内容");
    expect(array.text).not.toContain("[object Object]");

    expect(resultContentDisplay(null)).toEqual({ text: "", json: "（未记录）" });
    expect(resultContentDisplay(42).text).toBe("42");
  });

  it("任意对象值都有可读文本，绝不出现 [object Object]", () => {
    const values: unknown[] = [
      { nested: { deep: [1, 2] } },
      [1, 2, 3],
      { title: { weird: true } },
      { toString: null },
      Object.create(null),
    ];
    for (const value of values) {
      expect(readableValue(value)).not.toContain("[object Object]");
    }
    // 结构数据只给可读摘要；原始 JSON / ID 不内嵌进主文本。
    expect(readableValue({ nested: { deep: [1, 2] } })).toBe("结构化数据（1 个字段）");
    expect(readableValue([1, 2, 3])).toBe("共 3 项");
    expect(readableValue({ title: { weird: true } })).toBe("结构化数据（1 个字段）");
    expect(readableValue(Object.create(null))).toBe("结构化数据");
    expect(readableValue(undefined)).toBe("");
    expect(readableValue("文字")).toBe("文字");
  });
});
