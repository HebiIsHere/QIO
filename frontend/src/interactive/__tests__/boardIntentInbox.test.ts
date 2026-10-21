/**
 * 对话内「待批准板面意图」的纯逻辑测试。
 *
 * 契约（Lead 冻结）：普通对话（mode=board）里 QIO 产生的 proposed / awaiting_confirmation
 * 意图要在对话视图里可批准；确认走 store.confirmIntent；confirm 响应可能带
 * continuation { enqueued, turn_id }（无操作计划意图确认后自动续作）。
 * 这里只测展示映射与结果文字，不挂组件、不发请求。
 */
import { describe, expect, it } from "vitest";

import {
  BOARD_PENDING_STATUSES,
  CONVERSATION_BOARD_ID,
  boardPendingIntents,
  confirmDetailText,
  continuationText,
  conversationIntentSummary,
  isBoardPendingIntent,
} from "../boardIntentInbox";
import type { Intent, IntentPreview } from "../types";

function preview(cards: { id: string; content: string }[] = [{ id: "a", content: "结果 A" }]): IntentPreview {
  return {
    cards: cards.map((card, index) => ({
      id: card.id,
      kind: "reply",
      content: card.content,
      meta: {},
      x: 100 + index * 10,
      y: 100,
      w: 320,
      h: 240,
      checked: false,
      hidden: false,
      folded: false,
      bookmarked: false,
      deleted: false,
      createdAt: "2026-10-21T00:00:00+00:00",
      updatedAt: "2026-10-21T00:00:00+00:00",
    })),
    groups: [],
    links: [],
    note: "",
  };
}

function intent(status: Intent["status"], overrides: Partial<Intent> = {}): Intent {
  return {
    id: "in_1",
    boardId: CONVERSATION_BOARD_ID,
    submissionId: null,
    title: "整理材料",
    summary: "",
    status,
    preview: preview(),
    operations: [{ op: "add_reply_card", ref: "a", content: "结果 A" }],
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: false,
    createdAt: "2026-10-21T00:00:00+00:00",
    updatedAt: "2026-10-21T00:00:00+00:00",
    ...overrides,
  } as Intent;
}

describe("待批准状态与列表", () => {
  it("只有 proposed / awaiting_confirmation 进对话内批准列表", () => {
    expect(BOARD_PENDING_STATUSES).toEqual(["proposed", "awaiting_confirmation"]);
    expect(isBoardPendingIntent(intent("proposed"))).toBe(true);
    expect(isBoardPendingIntent(intent("awaiting_confirmation"))).toBe(true);
    for (const status of ["pending", "approved", "running", "completed", "rejected", "failed", "cancelled"] as const) {
      expect(isBoardPendingIntent(intent(status)), status).toBe(false);
    }
  });

  it("列表保持服务端顺序，且不只看状态：所属板面是 board_default", () => {
    const list = boardPendingIntents([
      intent("awaiting_confirmation", { id: "in_1" }),
      intent("completed", { id: "in_2", status: "completed" }),
      intent("proposed", { id: "in_3", boardId: "other_board" }),
      intent("proposed", { id: "in_4" }),
    ]);
    expect(list.map((item) => item.id)).toEqual(["in_1", "in_4"]);
  });

  it("对话内读取的板面就是 board_default（后端默认板面）", () => {
    expect(CONVERSATION_BOARD_ID).toBe("board_default");
  });
});

describe("意图摘要（标题 / 目标 / 简短预演）", () => {
  it("给出状态文案、卡片/组/关系数量与卡片正文片段", () => {
    const summary = conversationIntentSummary(intent("proposed"));
    expect(summary.title).toBe("整理材料");
    expect(summary.statusLabel).toBe("待确认");
    expect(summary.previewText).toContain("1 张卡片");
    expect(summary.previewText).toContain("结果 A");
  });

  it("没有可展示预演内容时如实说没有，不编造", () => {
    const summary = conversationIntentSummary(
      intent("proposed", { preview: { cards: [], groups: [], links: [], note: "" } }),
    );
    expect(summary.previewText).toContain("没有可展示的预演");
  });

  it("长正文只取片段（不在对话里堆整段原文）", () => {
    const long = "很长的正文".repeat(20);
    const summary = conversationIntentSummary(intent("proposed", { preview: preview([{ id: "a", content: long }]) }));
    expect(summary.previewText.length).toBeLessThan(120);
  });
});

describe("续作（continuation）文字", () => {
  it("有 turn_id 时说「正在继续执行」并带上轮次", () => {
    const text = continuationText({ enqueued: true, turn_id: "turn_9" });
    expect(text).toContain("正在继续执行");
    expect(text).toContain("turn_9");
  });

  it("没有 turn_id 时仍然如实说正在继续执行（不编造 id）", () => {
    const text = continuationText({ enqueued: true });
    expect(text).toContain("正在继续执行");
    expect(text).not.toContain("turn_");
  });

  it("没有续作时为空串", () => {
    expect(continuationText(null)).toBe("");
    expect(continuationText({ enqueued: false })).toBe("");
  });
});

describe("确认结果文字（确认 ≠ 已完成）", () => {
  it("已落地 + 已续作：两件事都如实说", () => {
    const text = confirmDetailText({
      ok: true,
      landedText: "2 张卡片",
      continuation: { enqueued: true, turn_id: "turn_9" },
    });
    expect(text).toContain("已确认并落地");
    expect(text).toContain("2 张卡片");
    expect(text).toContain("正在继续执行");
    expect(text).not.toContain("已完成");
  });

  it("无操作计划的意图：确认后自动续作也要说出来", () => {
    const text = confirmDetailText({
      ok: true,
      continuation: { enqueued: true, turn_id: "turn_9" },
    });
    expect(text).toContain("已确认");
    expect(text).toContain("正在继续执行");
    expect(text).toContain("账本");
  });

  it("既没有落地也没有续作：不假报已落地/已完成", () => {
    const text = confirmDetailText({ ok: true });
    expect(text).toContain("已确认");
    expect(text).toContain("没有返回落地结果");
    expect(text).not.toContain("已完成");
  });

  it("落地失败优先如实报告（board_changed 等），不当成已落地", () => {
    const text = confirmDetailText({
      ok: false,
      applyError: "板面在确认之后发生了变化：本次没有写入。",
      landedText: "1 张卡片",
      continuation: { enqueued: true },
    });
    expect(text).toContain("落地没有完成");
    expect(text).toContain("板面在确认之后发生了变化");
    expect(text).not.toContain("已落地：板面新增");
  });

  it("确认被拒：给出真实原因", () => {
    const text = confirmDetailText({ ok: false, reason: "与已确认的意图冲突" });
    expect(text).toContain("确认没有完成");
    expect(text).toContain("与已确认的意图冲突");
  });

  it("刷新失败不改变确认事实，但要如实附上", () => {
    const text = confirmDetailText({
      ok: true,
      landedText: "1 张卡片",
      refreshError: "网络中断",
    });
    expect(text).toContain("已确认并落地");
    expect(text).toContain("板面刷新失败");
    expect(text).toContain("网络中断");
  });
});
