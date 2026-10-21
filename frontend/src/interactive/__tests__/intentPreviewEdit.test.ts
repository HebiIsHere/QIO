/**
 * 预演编辑（Stage 2）的纯逻辑测试：成组 / 连线 / 正文编辑的 operations 变换。
 *
 * 只测可离线验证的规则，不挂组件、不发请求：
 * - 多选切换；幽灵卡 id ↔ add 操作引用（续用 Stage 1 的映射口径）；
 * - 成组：>=2 张，追加 group_cards（ref 唯一、成员用 ref/id）；
 * - 连线：恰好 2 张，追加 add_link（src/dst、meaning 空、direction=false）；
 * - 正文编辑：改对应 add 操作的 content；空白正文本地拦截（服务端会拒绝）；
 * - 重复成组 / 重复连线 / 条数上限 / 找不到操作都必须如实失败，且绝不修改入参。
 */
import { describe, expect, it } from "vitest";

import {
  editPreviewCardContent,
  groupPreviewCards,
  linkPreviewCards,
  previewCardRef,
  readPreviewCardContent,
  togglePreviewSelection,
  uniquePlanRef,
} from "../intentPreviewEdit";
import type { IntentOperation } from "../types";

const PLAN: IntentOperation[] = [
  { op: "add_reply_card", ref: "a", content: "结果 A", x: 100, y: 100 },
  { op: "add_text_card", ref: "b", content: "文字 B" },
  { op: "add_text_card", cardId: "c", content: "文字 C" },
  { op: "add_text_card", content: "没有符号" },
];

describe("多选与幽灵卡引用", () => {
  it("切换选中：加入 / 移除，顺序按点击顺序", () => {
    let selected = togglePreviewSelection([], "a");
    selected = togglePreviewSelection(selected, "b");
    expect(selected).toEqual(["a", "b"]);
    selected = togglePreviewSelection(selected, "a");
    expect(selected).toEqual(["b"]);
  });

  it("幽灵卡在 operations 里的稳定引用：ref → cardId；没有稳定符号返回 null", () => {
    expect(previewCardRef(PLAN, "a")).toBe("a");
    expect(previewCardRef(PLAN, "c")).toBe("c");
    // p_3 是没有 ref / cardId 的新增卡：服务端落地时解析不出成员 → 不能成组 / 连线
    expect(previewCardRef(PLAN, "p_3")).toBeNull();
    expect(previewCardRef(PLAN, "missing")).toBeNull();
  });

  it("新符号避开 operations 里已有的 ref / groupId / cardId", () => {
    const ops: IntentOperation[] = [
      { op: "group_cards", ref: "group_1", cardIds: ["a"] },
      { op: "add_text_card", cardId: "group_2", content: "x" },
    ];
    expect(uniquePlanRef(ops, "group_")).toBe("group_3");
    expect(uniquePlanRef([], "group_")).toBe("group_1");
  });
});

describe("成组", () => {
  it("选中 2 张以上：追加 group_cards，ref 唯一、成员用 ref/id", () => {
    const result = groupPreviewCards(PLAN, ["a", "c"]);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const added = result.operations[result.operations.length - 1];
    expect(added).toEqual({ op: "group_cards", ref: "group_1", cardIds: ["a", "c"] });
    // 原有操作原样保留
    expect(result.operations.slice(0, PLAN.length)).toEqual(PLAN);
    // 入参没有被改
    expect(PLAN).toHaveLength(4);
  });

  it("不足 2 张 / 找不到对应 add 操作时如实失败（不追加任何操作）", () => {
    expect(groupPreviewCards(PLAN, ["a"]).ok).toBe(false);
    expect(groupPreviewCards(PLAN, ["a", "missing"]).ok).toBe(false);
  });

  it("没有稳定符号（p_<下标>）的幽灵卡不能成组：如实说明原因", () => {
    const result = groupPreviewCards(PLAN, ["a", "p_3"]);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.reason).toContain("稳定符号");
  });

  it("同一组成员的组已经存在时不重复添加", () => {
    const grouped = groupPreviewCards(PLAN, ["a", "b"]);
    expect(grouped.ok).toBe(true);
    if (!grouped.ok) return;
    const again = groupPreviewCards(grouped.operations, ["b", "a"]);
    expect(again.ok).toBe(false);
    if (again.ok) return;
    expect(again.reason).toContain("已经是");
  });

  it("操作条数达到上限 20 条时如实失败（服务端硬上限）", () => {
    const full: IntentOperation[] = Array.from({ length: 20 }, (_, index) => ({
      op: "add_text_card" as const,
      ref: "r" + index,
      content: "c" + index,
    }));
    const result = groupPreviewCards(full, ["r0", "r1"]);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.reason).toContain("20");
  });
});

describe("连线", () => {
  it("恰好 2 张：追加 add_link（含义空、无方向）", () => {
    const result = linkPreviewCards(PLAN, ["a", "b"]);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const added = result.operations[result.operations.length - 1];
    expect(added).toEqual({
      op: "add_link",
      src: "a",
      dst: "b",
      meaning: "",
      direction: false,
    });
    expect(result.operations.slice(0, PLAN.length)).toEqual(PLAN);
  });

  it("不是恰好 2 张时如实失败", () => {
    expect(linkPreviewCards(PLAN, ["a"]).ok).toBe(false);
    expect(linkPreviewCards(PLAN, ["a", "b", "c"]).ok).toBe(false);
  });

  it("没有稳定符号的幽灵卡不能连线：如实说明原因", () => {
    const result = linkPreviewCards(PLAN, ["p_3", "a"]);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.reason).toContain("稳定符号");
  });

  it("同一对卡片之间已有关系时不重复添加（无序比较）", () => {
    const linked = linkPreviewCards(PLAN, ["a", "b"]);
    expect(linked.ok).toBe(true);
    if (!linked.ok) return;
    const again = linkPreviewCards(linked.operations, ["b", "a"]);
    expect(again.ok).toBe(false);
    if (again.ok) return;
    expect(again.reason).toContain("关系");
  });
});

describe("正文编辑", () => {
  it("只改对应 add 操作的 content，保留 Markdown 原文", () => {
    const markdown = "## 小标题\n\n- 一条\n- 两条";
    const result = editPreviewCardContent(PLAN, "b", markdown);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.operations[1].content).toBe(markdown);
    // 其他操作原样
    expect(result.operations[0]).toEqual(PLAN[0]);
    expect(result.operations[2]).toEqual(PLAN[2]);
    expect(PLAN[1].content).toBe("文字 B");
  });

  it("空白正文本地拦截并提示（服务端会拒绝）", () => {
    for (const blank of ["", "   ", "\n\t"]) {
      const result = editPreviewCardContent(PLAN, "a", blank);
      expect(result.ok, JSON.stringify(blank)).toBe(false);
      if (result.ok) return;
      expect(result.reason).toContain("正文");
    }
  });

  it("找不到对应 add 操作时如实失败", () => {
    expect(editPreviewCardContent(PLAN, "missing", "新正文").ok).toBe(false);
  });

  it("读取当前正文：有 add 操作就给原始 content（没有就给 null）", () => {
    expect(readPreviewCardContent(PLAN, "a")).toBe("结果 A");
    expect(readPreviewCardContent(PLAN, "missing")).toBeNull();
  });
});
