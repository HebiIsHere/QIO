/**
 * 审批纯函数的用例（子智能体 C 原有 + 子智能体 D 追加批次判定）。
 *
 * 重点：预览语义比较（只改位置 vs 改语义）、状态文案、批量选择，
 * 以及契约 §8.5 的批次判定（会话记录 / submissionId / 创建秒 / 不成批）与部分选择。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  approveAvailability,
  batchEntryText,
  batchesWithList,
  batchKeyOf,
  clearBatchSelectionIn,
  groupIntentsByBatch,
  INTENT_BATCH_STORAGE_KEY,
  locatePreview,
  pruneBatchSelection,
  recordIntentBatch,
  selectAllInBatch,
  splitBatchIn,
  batchSummaryIn,
  toggleBatchSelectionIn,
  batchCandidates,
  batchSummary,
  clearBatchSelection,
  comparePreview,
  defaultBatchSelection,
  describePreviewChanges,
  impactSections,
  isDecidable,
  previewBounds,
  previewSemantics,
  rejectAvailability,
  revertSections,
  selectAllBatch,
  splitBatchDecision,
  statusText,
  toggleBatchSelection,
  waitingForLabels,
} from "../approval";
import type { BoardCard, BoardGroup, BoardLink, Intent, IntentStatus } from "../types";

function card(partial: Partial<BoardCard> & { id: string }): BoardCard {
  return {
    kind: "reply",
    content: "预览内容",
    meta: {},
    x: 40,
    y: 40,
    w: 240,
    h: 96,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
    ...partial,
  };
}

function group(partial: Partial<BoardGroup> & { id: string }): BoardGroup {
  return {
    name: "组 1",
    defaultName: true,
    ordered: false,
    x: 20,
    y: 20,
    w: 320,
    h: 240,
    members: [],
    deleted: false,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
    ...partial,
  };
}

function link(partial: Partial<BoardLink> & { id: string }): BoardLink {
  return {
    src: "c1",
    dst: "c2",
    direction: false,
    meaning: "对比依据",
    deleted: false,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
    ...partial,
  };
}

function intent(partial: Partial<Intent> & { id: string; status: IntentStatus }): Intent {
  return {
    boardId: "board_default",
    submissionId: null,
    title: "任务 " + partial.id,
    summary: "",
    preview: { cards: [], groups: [], links: [] },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: true,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
    ...partial,
  };
}

describe("预览语义比较", () => {
  const base = {
    cards: [card({ id: "c1", x: 40, y: 40 }), card({ id: "c2", x: 40, y: 200, kind: "reply" })],
    groups: [group({ id: "g1", members: ["c1", "c2"] })],
    links: [link({ id: "l1", src: "c1", dst: "c2" })],
    note: "虚线预览（演示）",
  };

  it("只改位置：仍可直接批准", () => {
    const moved = {
      ...base,
      cards: [card({ id: "c1", x: 300, y: 120 }), card({ id: "c2", x: 40, y: 200 })],
    };
    const result = comparePreview(base, moved);
    expect(result.level).toBe("layout_only");
    expect(result.canApprove).toBe(true);
    expect(result.text).toContain("位置");
    expect(result.changes).toEqual([]);
  });

  it("没有变化：same", () => {
    expect(comparePreview(base, JSON.parse(JSON.stringify(base))).level).toBe("same");
    // 卡片数组顺序变化不算变化
    const reordered = { ...base, cards: [...base.cards].reverse() };
    expect(comparePreview(base, reordered).level).toBe("same");
  });

  it("改工作内容：需要更新，不能批准", () => {
    const changed = {
      ...base,
      cards: [card({ id: "c1", content: "改成只输出结论", x: 40, y: 40 }), base.cards[1]],
    };
    const result = comparePreview(base, changed);
    expect(result.level).toBe("semantic");
    expect(result.canApprove).toBe(false);
    expect(result.changes.join("")).toContain("修改结果卡片");
    expect(result.text).toContain("需要提交");
  });

  it("改材料范围（勾选 / 隐藏）与改关系含义都算语义变化", () => {
    const checked = { ...base, cards: [card({ id: "c1", checked: true }), base.cards[1]] };
    expect(comparePreview(base, checked).level).toBe("semantic");

    const hidden = { ...base, cards: [card({ id: "c1", hidden: true }), base.cards[1]] };
    expect(comparePreview(base, hidden).level).toBe("semantic");

    const meaning = { ...base, links: [link({ id: "l1", src: "c1", dst: "c2", meaning: "因果" })] };
    const result = comparePreview(base, meaning);
    expect(result.level).toBe("semantic");
    expect(result.changes.join("")).toContain("修改关系");
  });

  it("新增 / 移除结果、组与关系都被描述出来", () => {
    const added = {
      cards: [...base.cards, card({ id: "c3", content: "新增一张" })],
      groups: base.groups,
      links: base.links,
      note: base.note,
    };
    const changes = describePreviewChanges(base, added);
    expect(changes.join("")).toContain("新增结果卡片");

    const removed = { cards: [base.cards[0]], groups: [], links: [], note: base.note };
    const more = describePreviewChanges(base, removed);
    expect(more.join("")).toContain("移除结果卡片");
    expect(more.join("")).toContain("移除组");
    expect(more.join("")).toContain("移除关系");
  });

  it("预览范围用于定位板面，空预览返回 null", () => {
    expect(previewBounds({ cards: [], groups: [], links: [] })).toBeNull();
    const bounds = previewBounds(base);
    expect(bounds).toEqual({ x: 20, y: 20, w: 320, h: 276 });
    expect(previewBounds({ cards: [card({ id: "c9", x: 100, y: 50, w: 10, h: 20 })], groups: [], links: [] })).toEqual(
      { x: 100, y: 50, w: 10, h: 20 },
    );
  });

  it("预览语义键忽略位置但保留结构与关系", () => {
    const moved = { ...base, cards: [card({ id: "c1", x: 900, y: 900 }), base.cards[1]] };
    expect(previewSemantics(moved)).toBe(previewSemantics(base));

    const regrouped = { ...base, groups: [group({ id: "g1", members: ["c2", "c1"] })] };
    expect(previewSemantics(regrouped)).not.toBe(previewSemantics(base));
  });
});

describe("状态文案", () => {
  const statuses: IntentStatus[] = [
    "pending",
    "needs_update",
    "rejected",
    "waiting_dependency",
    "waiting_confirm",
    "running",
    "paused",
    "done",
    "failed",
    "cancelled",
  ];

  it("每个状态都有文字说明，不是只靠颜色", () => {
    for (const status of statuses) {
      const text = statusText(intent({ id: "i_" + status, status }));
      expect(text.label.length).toBeGreaterThan(0);
      expect(text.detail.length).toBeGreaterThan(0);
    }
  });

  it("需要更新时优先显示服务端给出的原因", () => {
    const text = statusText(
      intent({ id: "i1", status: "needs_update", reason: "相关材料在提交 sub_1 后发生了变化" }),
    );
    expect(text.detail).toContain("sub_1");
    expect(text.tone).toBe("problem");
  });

  it("等待前项与等待再次确认都说明「不会自动开始」", () => {
    expect(statusText(intent({ id: "i2", status: "waiting_dependency" })).detail).toContain(
      "不会自动开始",
    );
    expect(statusText(intent({ id: "i3", status: "waiting_confirm" })).detail).toContain(
      "不会自动开始",
    );
  });

  it("暂停状态说明「确认后按当前材料继续」", () => {
    const text = statusText(intent({ id: "i_paused", status: "paused" }));
    expect(text.detail).toContain("按当前材料继续");
    expect(text.detail).toContain("不会自动继续");
  });

  it("失败状态说明撤回与不自动重试", () => {
    const text = statusText(intent({ id: "i4", status: "failed" }));
    expect(text.detail).toContain("撤回");
    expect(text.detail).toContain("不会自动重试");
  });
});

describe("审批可用性（只显示服务端结果）", () => {
  it("pending 可以批准；waiting_confirm 需要再次确认", () => {
    expect(approveAvailability(intent({ id: "i1", status: "pending" })).allowed).toBe(true);
    const confirm = approveAvailability(intent({ id: "i2", status: "waiting_confirm" }));
    expect(confirm.allowed).toBe(true);
    expect(confirm.needsConfirm).toBe(true);
  });

  it("needs_update / waiting_dependency 不能批准", () => {
    const needsUpdate = approveAvailability(
      intent({ id: "i3", status: "needs_update", reason: "材料已变化" }),
    );
    expect(needsUpdate.allowed).toBe(false);
    expect(needsUpdate.text).toContain("材料已变化");

    const waiting = approveAvailability(intent({ id: "i4", status: "waiting_dependency" }));
    expect(waiting.allowed).toBe(false);
    expect(waiting.text).toContain("不会自动开始");
  });

  it("暂停的任务可以「按当前材料继续」，但必须先确认", () => {
    const paused = approveAvailability(intent({ id: "i8", status: "paused" }));
    expect(paused.allowed).toBe(true);
    expect(paused.needsConfirm).toBe(true);
    expect(paused.text).toContain("按当前材料继续");
    expect(paused.text).toContain("不会自动重试");

    // 暂停时不能「拒绝」（要取消得先继续或走演示推进），继续之后是 running
    expect(rejectAvailability(intent({ id: "i9", status: "paused" })).allowed).toBe(false);
    expect(approveAvailability(intent({ id: "i10", status: "running" })).allowed).toBe(false);
  });

  it("已经结束的状态不能批准也不能拒绝；执行中不能拒绝", () => {
    for (const status of ["rejected", "done", "failed", "cancelled"] as IntentStatus[]) {
      expect(approveAvailability(intent({ id: "i_" + status, status })).allowed).toBe(false);
      expect(rejectAvailability(intent({ id: "r_" + status, status })).allowed).toBe(false);
    }
    expect(rejectAvailability(intent({ id: "i5", status: "running" })).allowed).toBe(false);
    expect(rejectAvailability(intent({ id: "i6", status: "paused" })).allowed).toBe(false);
    expect(rejectAvailability(intent({ id: "i7", status: "pending" })).allowed).toBe(true);
    expect(isDecidable("needs_update")).toBe(true);
    expect(isDecidable("running")).toBe(false);
  });
});

describe("批量选择", () => {
  const items = [
    intent({ id: "a", status: "pending" }),
    intent({ id: "b", status: "needs_update" }),
    intent({ id: "c", status: "waiting_dependency", dependsOn: ["a"] }),
    intent({ id: "d", status: "waiting_confirm" }),
    intent({ id: "e", status: "running" }),
    intent({ id: "f", status: "done" }),
  ];

  it("候选只包含等待审批的状态（4 项时提供批量列表）", () => {
    const candidates = batchCandidates(items);
    expect(candidates.map((item) => item.id)).toEqual(["a", "b", "c", "d"]);
    expect(candidates.length).toBeGreaterThanOrEqual(4);
  });

  it("默认选中全部可批准项，needs_update 不能被批准", () => {
    expect(defaultBatchSelection(items)).toEqual(["a", "c", "d"]);
  });

  it("选择 / 全选 / 清空", () => {
    expect(toggleBatchSelection(["a"], "b")).toEqual(["a", "b"]);
    expect(toggleBatchSelection(["a", "b"], "a")).toEqual(["b"]);
    expect(selectAllBatch(items)).toEqual(["a", "b", "c", "d"]);
    expect(clearBatchSelection()).toEqual([]);
  });

  it("批量批准时把不能批准的项列出来（未选中的继续等待）", () => {
    const approve = splitBatchDecision(items, ["a", "b", "c", "d"], "approve");
    // c 已经批准、正在等前项，不需要再批准一次
    expect(approve.ids).toEqual(["a", "d"]);
    expect(approve.blocked.map((item) => item.id)).toEqual(["b", "c"]);
    expect(approve.blocked[0].reason).toContain("不能批准");
    expect(approve.blocked[1].reason).toContain("不会自动开始");

    const rejected = splitBatchDecision(items, ["a", "b", "c", "d"], "reject");
    expect(rejected.ids).toEqual(["a", "b", "c", "d"]);
    expect(rejected.blocked).toEqual([]);
  });

  it("已经不在列表里的 id 会被挡下", () => {
    const result = splitBatchDecision(items, ["a", "e"], "approve");
    expect(result.ids).toEqual(["a"]);
    expect(result.blocked.map((item) => item.id)).toEqual(["e"]);
  });

  it("摘要说明选中多少、未选中的继续等待", () => {
    const summary = batchSummary(items, ["a", "b"]);
    expect(summary).toContain("已选 2 项");
    expect(summary).toContain("未选中的 2 项继续等待");

    const all = batchSummary(items, ["a", "b", "c", "d"]);
    expect(all).toContain("全部已选中");
  });
});

describe("影响说明与依赖", () => {
  it("对象 / 任务 / 后果三段都在", () => {
    const sections = impactSections({
      objects: ["文件「a.pdf」"],
      tasks: ["新增一张对比摘要"],
      consequences: ["正式板面会新增 1 张结果卡片"],
    });
    expect(sections.map((section) => section.label)).toEqual(["对象", "任务", "后果"]);
    expect(sections[0].items).toEqual(["文件「a.pdf」"]);
    expect(sections[2].items[0]).toContain("结果卡片");
  });

  it("撤回报告分成已撤回 / 已保留 / 等待决定", () => {
    const sections = revertSections({
      reverted: ["卡片 c1：已撤回"],
      kept: ["卡片 c2：你修改过，已保留"],
      pendingDecision: [{ id: "c3", reason: "撤回会影响别的工作", impact: "它是某条关系的一端" }],
      reasonText: "任务失败，已撤回 1 项",
    });
    expect(sections.map((section) => section.label)).toEqual([
      "已撤回",
      "已保留（你后来的修改）",
      "等待你决定",
    ]);
    expect(sections[2].items[0]).toContain("影响：");
  });

  it("等待前项时把 id 换成标题", () => {
    const first = intent({ id: "a", status: "pending", title: "把材料归为一组" });
    const dependent = intent({
      id: "b",
      status: "waiting_dependency",
      dependsOn: ["a"],
      waitingFor: ["a"],
    });
    expect(waitingForLabels(dependent, [first, dependent])).toEqual(["把材料归为一组"]);
    expect(waitingForLabels(dependent, [])).toEqual(["a"]);
  });
});
// --- 批次判定（D 实现；契约 §8.5） ------------------------------------------
//
// 重点覆盖验收要求的六条：同批 3 项不出列表、同批 4 项出列表、不同批 3+1 不误触发、
// 三种来源的优先级、localStorage 异常时降级、部分选择只影响选中项。

/** 每个用例都从「没有会话批次记录」开始 */
function clearBatchStorage(): void {
  try {
    localStorage.removeItem(INTENT_BATCH_STORAGE_KEY);
  } catch {
    // 存储不可用时忽略
  }
}

/** 一次创建动作产生的 4 项：同一秒、同一 submissionId */
function sameBatch(count: number, overrides: Partial<Intent> = {}): Intent[] {
  return Array.from({ length: count }, (_, index) =>
    intent({
      id: "b" + index,
      status: "pending",
      submissionId: "sub_1",
      createdAt: "2026-10-07T04:00:00.500Z",
      ...overrides,
    }),
  );
}

describe("批次判定：会话记录（①）", () => {
  beforeEach(clearBatchStorage);

  it("同一次创建动作记下的四项属于同一批", () => {
    recordIntentBatch("session:demo-1", ["b0", "b1", "b2", "b3"]);
    const batches = groupIntentsByBatch(sameBatch(4, { submissionId: null }));
    expect(batches.length).toBe(1);
    expect(batches[0].intentIds).toEqual(["b0", "b1", "b2", "b3"]);
    expect(batches[0].pendingIds.length).toBe(4);
    expect(batchKeyOf(sameBatch(1)[0])).toContain("demo-1");
  });

  it("同批 3 项：不出批量列表", () => {
    recordIntentBatch("session:demo-3", ["b0", "b1", "b2"]);
    const items = sameBatch(3, { submissionId: null });
    expect(groupIntentsByBatch(items).length).toBe(1);
    expect(batchesWithList(items)).toEqual([]);
  });

  it("同批 4 项：出批量列表", () => {
    recordIntentBatch("session:demo-4", ["b0", "b1", "b2", "b3"]);
    const batches = batchesWithList(sameBatch(4, { submissionId: null }));
    expect(batches.length).toBe(1);
    expect(batches[0].pendingIds.length).toBe(4);
  });

  it("不同批次 3+1 不误触发（不同批次绝不累加）", () => {
    recordIntentBatch("session:demo-a", ["b0", "b1", "b2"]);
    recordIntentBatch("session:demo-b", ["b3"]);
    const items = sameBatch(4, { submissionId: null });
    const batches = groupIntentsByBatch(items);
    expect(batches.length).toBe(2);
    expect(batches.map((batch) => batch.pendingIds.length)).toEqual([3, 1]);
    expect(batchesWithList(items)).toEqual([]);
  });

  it("已经结束的项不计入这一批的等待数量", () => {
    recordIntentBatch("session:demo-mix", ["b0", "b1", "b2", "b3"]);
    const items = [
      ...sameBatch(3, { submissionId: null }),
      intent({ id: "b3", status: "done", submissionId: null, createdAt: "2026-10-07T04:00:00.500Z" }),
    ];
    const batches = groupIntentsByBatch(items);
    expect(batches.length).toBe(1);
    expect(batches[0].intentIds.length).toBe(4);
    expect(batches[0].pendingIds.length).toBe(3);
    expect(batchesWithList(items)).toEqual([]);
  });
});

describe("批次判定：来源优先级（① > ② > ③）", () => {
  beforeEach(clearBatchStorage);

  it("① 会话记录压过服务端 submissionId", () => {
    recordIntentBatch("session:demo-x", ["b0"]);
    const item = intent({ id: "b0", status: "pending", submissionId: "sub_9" });
    expect(batchKeyOf(item)).toBe("session:demo-x");
  });

  it("② 没有会话记录时用 submissionId", () => {
    const items = [
      intent({ id: "b0", status: "pending", submissionId: "sub_7", createdAt: "2026-10-07T04:00:00.000Z" }),
      intent({ id: "b1", status: "pending", submissionId: "sub_7", createdAt: "2026-10-07T04:00:03.000Z" }),
    ];
    const batches = groupIntentsByBatch(items);
    expect(batches.length).toBe(1);
    expect(batches[0].key).toBe(batchKeyOf(items[0]));
    expect(batchesWithList([...items, intent({ id: "b2", status: "pending", submissionId: "sub_7" }), intent({ id: "b3", status: "pending", submissionId: "sub_7" })])[0].pendingIds.length).toBe(4);
  });

  it("不同 submissionId 不属于同一批", () => {
    const items = [
      intent({ id: "b0", status: "pending", submissionId: "sub_1" }),
      intent({ id: "b1", status: "pending", submissionId: "sub_2" }),
      intent({ id: "b2", status: "pending", submissionId: "sub_2" }),
      intent({ id: "b3", status: "pending", submissionId: "sub_2" }),
    ];
    const batches = groupIntentsByBatch(items);
    expect(batches.length).toBe(2);
    expect(batchesWithList(items)).toEqual([]);
  });

  it("③ 没有会话记录与 submissionId 时，按创建秒相同分组", () => {
    const items = [
      intent({ id: "b0", status: "pending", submissionId: null, createdAt: "2026-10-07T04:00:00.100Z" }),
      intent({ id: "b1", status: "pending", submissionId: null, createdAt: "2026-10-07T04:00:00.900Z" }),
      intent({ id: "b2", status: "pending", submissionId: null, createdAt: "2026-10-07T04:00:01.000Z" }),
      intent({ id: "b3", status: "pending", submissionId: null, createdAt: "2026-10-07T04:00:01.500Z" }),
    ];
    const batches = groupIntentsByBatch(items);
    expect(batches.length).toBe(2);
    expect(batches[0].intentIds).toEqual(["b0", "b1"]);
    expect(batches[1].intentIds).toEqual(["b2", "b3"]);
    expect(batchesWithList(items)).toEqual([]);
  });

  it("三条都拿不到（含 createdAt 非法）：每个意图自成一批，永不合并", () => {
    const items = [
      intent({ id: "b0", status: "pending", submissionId: null, createdAt: "" }),
      intent({ id: "b1", status: "pending", submissionId: null, createdAt: "不是时间" }),
      intent({ id: "b2", status: "pending", submissionId: null, createdAt: "" }),
      intent({ id: "b3", status: "pending", submissionId: null, createdAt: "" }),
    ];
    const batches = groupIntentsByBatch(items);
    expect(batches.length).toBe(4);
    expect(new Set(batches.map((batch) => batch.key)).size).toBe(4);
    expect(batchesWithList(items)).toEqual([]);
  });
});

describe("批次判定：localStorage 异常时降级", () => {
  beforeEach(clearBatchStorage);

  it("解析失败：不抛错，退回服务端来源", () => {
    localStorage.setItem(INTENT_BATCH_STORAGE_KEY, "{不是 JSON");
    const item = intent({ id: "b0", status: "pending", submissionId: "sub_3" });
    expect(() => batchKeyOf(item)).not.toThrow();
    expect(groupIntentsByBatch([item]).length).toBe(1);
    expect(batchKeyOf(item)).not.toContain("session:");
  });

  it("结构损坏（数组里是垃圾值）：不抛错，只忽略坏记录", () => {
    localStorage.setItem(INTENT_BATCH_STORAGE_KEY, JSON.stringify([1, null, { id: "b0" }, "x"]));
    expect(() => groupIntentsByBatch(sameBatch(2, { submissionId: null }))).not.toThrow();
    expect(() => recordIntentBatch("session:demo", ["b1"])).not.toThrow();
    // 坏记录被忽略：b1 用新记下的会话批次，b0 三条来源都拿不到 → 自成一批（不合并）
    const batches = groupIntentsByBatch(sameBatch(2, { submissionId: null }));
    expect(batches.map((batch) => batch.intentIds)).toEqual([["b0"], ["b1"]]);
    expect(batches[0].key.startsWith("session:")).toBe(false);
    expect(batches[0].key).not.toBe(batches[1].key);
    expect(batchKeyOf(intent({ id: "b1", status: "pending", submissionId: null, createdAt: "" }))).toBe(
      "session:demo",
    );
  });

  it("写满 / 被禁用：静默降级，不抛错也不影响后续读取", () => {
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    try {
      expect(() => recordIntentBatch("session:demo", ["b0", "b1", "b2", "b3"])).not.toThrow();
      // 没有记下来 → 这批退回服务端来源，不会把不同批次相加
      expect(batchKeyOf(intent({ id: "b0", status: "pending", submissionId: "sub_5" }))).not.toContain(
        "session:",
      );
      expect(batchKeyOf(intent({ id: "b9", status: "pending", submissionId: null, createdAt: "" }))).toBe(
        "solo:b9",
      );
    } finally {
      spy.mockRestore();
    }
  });

  it("没有可用批次键时不记录（宁可不成批，也不错误合并）", () => {
    expect(() => recordIntentBatch("", ["b0"])).not.toThrow();
    expect(() => recordIntentBatch("  ", ["b0"])).not.toThrow();
    expect(() => recordIntentBatch("session:demo", [])).not.toThrow();
    expect(localStorage.getItem(INTENT_BATCH_STORAGE_KEY)).toBeNull();
  });
});

describe("批量列表：部分选择只影响选中项", () => {
  beforeEach(clearBatchStorage);

  const items = () => [
    intent({ id: "b0", status: "pending", title: "合并材料" }),
    intent({ id: "b1", status: "pending", title: "分开整理" }),
    intent({ id: "b2", status: "needs_update", title: "需要更新" }),
    intent({ id: "b3", status: "pending", title: "后续步骤" }),
  ];

  it("批次内勾选 / 取消只动这一项，其它项保持原选择", () => {
    const batch = batchesWithList(items())[0];
    let selected = toggleBatchSelectionIn(batch, [], "b0");
    expect(selected).toEqual(["b0"]);
    selected = toggleBatchSelectionIn(batch, selected, "b3");
    expect(selected).toEqual(["b0", "b3"]);
    selected = toggleBatchSelectionIn(batch, selected, "b0");
    expect(selected).toEqual(["b3"]);
    expect(selectAllInBatch(batch)).toEqual(["b0", "b1", "b2", "b3"]);
    expect(clearBatchSelectionIn()).toEqual([]);
  });

  it("已经不在这一批里的 id 不会被选中", () => {
    const batch = batchesWithList(items())[0];
    expect(toggleBatchSelectionIn(batch, ["b0"], "不属于这批")).toEqual(["b0"]);
  });

  it("选一部分批准：只把选中的交给服务端，未选中的继续等待", () => {
    const list = items();
    const batch = batchesWithList(list)[0];
    const selected = ["b0", "b3"];
    const approve = splitBatchIn(list, batch, selected, "approve");
    expect(approve.ids).toEqual(["b0", "b3"]);
    expect(approve.blocked).toEqual([]);
    // 未选中的 b1 / b2 不在这份决定里
    expect(approve.ids).not.toContain("b1");
    expect(approve.ids).not.toContain("b2");
    expect(batchSummaryIn(list, batch, selected)).toContain("已选 2 项");
    expect(batchSummaryIn(list, batch, selected)).toContain("未选中的 2 项继续等待");
  });

  it("选中不能批准的项时按实际原因挡下（服务端仍会再判一次）", () => {
    const list = items();
    const batch = batchesWithList(list)[0];
    const approve = splitBatchIn(list, batch, ["b0", "b2"], "approve");
    expect(approve.ids).toEqual(["b0"]);
    expect(approve.blocked.map((item) => item.id)).toEqual(["b2"]);
    const reject = splitBatchIn(list, batch, ["b0", "b2"], "reject");
    expect(reject.ids).toEqual(["b0", "b2"]);
    expect(reject.blocked).toEqual([]);
  });

  it("数量变化后清掉不在列表里的选择，不改变其他选择", () => {
    const list = items();
    const batch = batchesWithList(list)[0];
    const afterOneApproved = { ...batch, pendingIds: ["b0", "b2", "b3"] };
    expect(pruneBatchSelection(afterOneApproved, ["b0", "b1", "b3"])).toEqual(["b0", "b3"]);
  });

  it("入口文案分开说明「这一批」与「其他批次」，不累加", () => {
    const batch = { key: "session:x", intentIds: ["b0", "b1", "b2", "b3"], pendingIds: ["b0", "b1", "b2", "b3"] };
    expect(batchEntryText(batch)).toContain("这一批待审批 4 项");
    expect(batchEntryText(batch, 1)).toContain("另有 1 项在其他批次等待");
  });
});

describe("定位事件", () => {
  it("派发 qio:interactive:locate-preview 事件带意图 id 与范围", () => {
    const received: unknown[] = [];
    const listener = (event: Event) => received.push((event as CustomEvent).detail);
    window.addEventListener("qio:interactive:locate-preview", listener);
    try {
      locatePreview("i1", { x: 10, y: 20, w: 30, h: 40 });
    } finally {
      window.removeEventListener("qio:interactive:locate-preview", listener);
    }
    expect(received).toEqual([{ intentId: "i1", bounds: { x: 10, y: 20, w: 30, h: 40 } }]);
  });
});
