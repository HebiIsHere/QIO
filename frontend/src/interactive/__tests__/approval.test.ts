/**
 * 审批纯函数的用例（子智能体 C 原有 + 子智能体 D 追加批次判定）。
 *
 * 重点：预览语义比较（只改位置 vs 改语义）、状态文案、批量选择，
 * 以及契约 §9.2 / §9.3 的批次判定（本地按次记录 / 真实 submissionId / 不成批）、
 * 批量列表资格（该批总量 ≥4，保留到处理完）与部分选择。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { computed, ref } from "vue";
import {
  approveAvailability,
  batchEntryText,
  batchHandledCount,
  batchRemainingCount,
  batchesWithList,
  batchKeyOf,
  BATCH_RECORD_TTL_MS,
  hasRecoverableBatch,
  isBatchItemSelectable,
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
// --- 批次判定（契约 §9.2 / §9.3，覆盖 §8.5） ---------------------------------
//
// 重点覆盖：
// - 只认可证明的来源：① 前端按次记录的创建批次、② 真实 submissionId；
// - **反例**：同一秒内两批各两项，清掉本地记录后不能变成四项一批（旧实现按创建秒归批会误合并）；
// - 本地记录被禁用 / 抛异常 / 损坏 / 写满 / 过期时一律降级为「各自成批」，绝不混批；
// - 批量列表资格按**该批总量** ≥4，保留到处理完；另一批 3 项 + 这批 1 项不会凑成四项。

/** 每个用例都从「没有会话批次记录」开始 */
function clearBatchStorage(): void {
  try {
    localStorage.removeItem(INTENT_BATCH_STORAGE_KEY);
  } catch {
    // 存储不可用时忽略
  }
}

/** 同一批产生的若干项：不写 submissionId，只能靠本地按次记录（①）归批 */
function batchItems(ids: string[], status: IntentStatus = "pending"): Intent[] {
  return ids.map((id) => intent({ id, status, submissionId: null, createdAt: "2026-10-07T04:00:00.500Z" }));
}

describe("批次判定：只认可证明的来源（契约 §9.2）", () => {
  beforeEach(clearBatchStorage);

  it("① 同一次创建动作记下的四项属于同一批", () => {
    recordIntentBatch("session:demo-1", ["b0", "b1", "b2", "b3"]);
    const batches = groupIntentsByBatch(batchItems(["b0", "b1", "b2", "b3"]));
    expect(batches.length).toBe(1);
    expect(batches[0].intentIds).toEqual(["b0", "b1", "b2", "b3"]);
    expect(batches[0].pendingIds.length).toBe(4);
    expect(batchKeyOf(batchItems(["b0"])[0])).toBe("session:demo-1");
  });

  it("① 本地记录压过服务端 submissionId", () => {
    recordIntentBatch("session:demo-x", ["b0"]);
    const item = intent({ id: "b0", status: "pending", submissionId: "sub_9" });
    expect(batchKeyOf(item)).toBe("session:demo-x");
  });

  it("② 没有本地记录时用真实 submissionId（同一次提交产生的多项）", () => {
    const items = [
      intent({ id: "b0", status: "pending", submissionId: "sub_7", createdAt: "2026-10-07T04:00:00.000Z" }),
      intent({ id: "b1", status: "pending", submissionId: "sub_7", createdAt: "2026-10-07T04:00:03.000Z" }),
      intent({ id: "b2", status: "pending", submissionId: "sub_7" }),
      intent({ id: "b3", status: "pending", submissionId: "sub_7" }),
    ];
    const batches = groupIntentsByBatch(items);
    expect(batches.length).toBe(1);
    expect(batches[0].key).toBe("submission:sub_7");
    expect(batchesWithList(items)).toHaveLength(1);
    expect(batchesWithList(items)[0].pendingIds.length).toBe(4);
  });

  it("不同 submissionId 不属于同一批", () => {
    const items = [
      intent({ id: "b0", status: "pending", submissionId: "sub_1" }),
      intent({ id: "b1", status: "pending", submissionId: "sub_2" }),
      intent({ id: "b2", status: "pending", submissionId: "sub_2" }),
      intent({ id: "b3", status: "pending", submissionId: "sub_2" }),
    ];
    expect(groupIntentsByBatch(items).length).toBe(2);
    expect(batchesWithList(items)).toEqual([]);
  });

  it("反例：同一秒内两批各两项，清掉本地记录后不能变成四项一批", () => {
    // 两批本来各自有本地创建记录；记录被清掉（换浏览器 / 清存储 / 记录过期）后，
    // 旧实现只剩「创建秒相同」这一条推断，会把两批并成 4 项并给出批量列表 —— 必须避免。
    const items = [
      intent({ id: "a0", status: "pending", submissionId: null, createdAt: "2026-10-07T04:00:00.100Z" }),
      intent({ id: "a1", status: "pending", submissionId: null, createdAt: "2026-10-07T04:00:00.900Z" }),
      intent({ id: "b0", status: "pending", submissionId: null, createdAt: "2026-10-07T04:00:00.200Z" }),
      intent({ id: "b1", status: "pending", submissionId: null, createdAt: "2026-10-07T04:00:00.800Z" }),
    ];
    const batches = groupIntentsByBatch(items);
    expect(batches).toHaveLength(4);
    expect(new Set(batches.map((batch) => batch.key)).size).toBe(4);
    expect(batches.map((batch) => batch.pendingIds.length)).toEqual([1, 1, 1, 1]);
    expect(batchesWithList(items)).toEqual([]);
  });

  it("创建时间非法或相同都不猜：没有可证明来源就各自成批（intent:<id>）", () => {
    const items = [
      intent({ id: "b0", status: "pending", submissionId: null, createdAt: "" }),
      intent({ id: "b1", status: "pending", submissionId: null, createdAt: "不是时间" }),
      intent({ id: "b2", status: "pending", submissionId: null, createdAt: "2026-10-07T04:00:00.100Z" }),
      intent({ id: "b3", status: "pending", submissionId: null, createdAt: "2026-10-07T04:00:00.100Z" }),
    ];
    const batches = groupIntentsByBatch(items);
    expect(batches).toHaveLength(4);
    expect(batches.every((batch) => batch.key.startsWith("intent:"))).toBe(true);
    expect(batchesWithList(items)).toEqual([]);
  });

  it("写入本地记录后，基于 batchesWithList 的 computed 会重新计算（实机回归）", () => {
    // 实机现象：演示入口一次生成四项时，组件先因 intents 更新重算过一次（那时记录还没写入），
    // 记录随后写入却没有任何响应式变化 → 批量入口要等刷新才出现。
    // 现在读取路径会 touch 模块内的版本号，computed 必须能感知到这次写入。
    const intents = ref(batchItems(["b0", "b1", "b2", "b3"]));
    const list = computed(() => batchesWithList(intents.value));
    expect(list.value).toEqual([]);
    recordIntentBatch("session:reactive", ["b0", "b1", "b2", "b3"]);
    expect(list.value).toHaveLength(1);
    expect(list.value[0].pendingIds).toHaveLength(4);
    // 只碰存储、没有经过本模块写入时，不谎报「已变化」；重新求值才看到真实内容
    localStorage.setItem(INTENT_BATCH_STORAGE_KEY, "{坏数据");
    expect(computed(() => batchesWithList(intents.value)).value).toEqual([]);
  });

  it("hasRecoverableBatch：没有可证明来源时界面要如实说「批次不可恢复」", () => {
    expect(hasRecoverableBatch(intent({ id: "x", status: "pending", submissionId: "sub_1" }))).toBe(true);
    recordIntentBatch("session:demo", ["y"]);
    expect(hasRecoverableBatch(batchItems(["y"])[0])).toBe(true);
    expect(hasRecoverableBatch(batchItems(["z"])[0])).toBe(false);
  });
});

describe("批次判定：本地记录异常一律降级，绝不混批", () => {
  beforeEach(clearBatchStorage);

  it("解析失败：不抛错，退回服务端来源", () => {
    localStorage.setItem(INTENT_BATCH_STORAGE_KEY, "{不是 JSON");
    const item = intent({ id: "b0", status: "pending", submissionId: "sub_3" });
    expect(() => batchKeyOf(item)).not.toThrow();
    expect(batchKeyOf(item)).toBe("submission:sub_3");
    expect(() => groupIntentsByBatch([item])).not.toThrow();
  });

  it("结构损坏（数组里是垃圾值 / 键为空）：不抛错，只忽略坏记录", () => {
    localStorage.setItem(
      INTENT_BATCH_STORAGE_KEY,
      JSON.stringify([1, null, { id: "b0" }, { id: "b1", key: "" }, "x", { key: "session:x" }]),
    );
    expect(() => groupIntentsByBatch(batchItems(["b0", "b1"]))).not.toThrow();
    expect(() => recordIntentBatch("session:demo", ["b2"])).not.toThrow();
    // 坏记录被忽略：b0 / b1 没有可证明来源 → 各自成批（不合并），b2 用新记下的会话批次
    const batches = groupIntentsByBatch(batchItems(["b0", "b1", "b2"]));
    expect(batches.map((batch) => batch.intentIds)).toEqual([["b0"], ["b1"], ["b2"]]);
    expect(batches[2].key).toBe("session:demo");
    expect(batchKeyOf(batchItems(["b2"])[0])).toBe("session:demo");
  });

  it("写入抛异常（写满 / 被禁用）：静默降级，不混批", () => {
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    try {
      expect(() => recordIntentBatch("session:demo", ["b0", "b1", "b2", "b3"])).not.toThrow();
      // 没有记下来 → 这批退回服务端来源，不会把不同批次相加
      expect(batchKeyOf(intent({ id: "b0", status: "pending", submissionId: "sub_5" }))).toBe("submission:sub_5");
      expect(batchKeyOf(intent({ id: "b9", status: "pending", submissionId: null, createdAt: "" }))).toBe("intent:b9");
      expect(batchesWithList(batchItems(["b0", "b1", "b2", "b3"]))).toEqual([]);
    } finally {
      spy.mockRestore();
    }
  });

  it("读取抛异常（隐私模式 / 被策略禁用）：静默降级，不混批", () => {
    const getSpy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    const setSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    try {
      expect(() => recordIntentBatch("session:demo", ["b0", "b1", "b2", "b3"])).not.toThrow();
      expect(() => batchKeyOf(batchItems(["b0"])[0])).not.toThrow();
      const batches = groupIntentsByBatch(batchItems(["b0", "b1", "b2", "b3"]));
      expect(batches).toHaveLength(4);
      expect(new Set(batches.map((batch) => batch.key)).size).toBe(4);
    } finally {
      getSpy.mockRestore();
      setSpy.mockRestore();
    }
  });

  it("没有可用批次键时不记录（宁可不成批，也不错误合并）", () => {
    expect(() => recordIntentBatch("", ["b0"])).not.toThrow();
    expect(() => recordIntentBatch("  ", ["b0"])).not.toThrow();
    expect(() => recordIntentBatch("session:demo", [])).not.toThrow();
    expect(localStorage.getItem(INTENT_BATCH_STORAGE_KEY)).toBeNull();
  });

  it("过期记录不再参与归批，并在读写时被清掉（有界）", () => {
    const expiredAt = Date.now() - BATCH_RECORD_TTL_MS - 1000;
    localStorage.setItem(
      INTENT_BATCH_STORAGE_KEY,
      JSON.stringify({ version: 2, records: [{ id: "b0", key: "session:old", at: expiredAt }] }),
    );
    // 过期 → 没有可证明来源 → 自成一批
    expect(batchKeyOf(batchItems(["b0"])[0])).toBe("intent:b0");
    // 新记录写入时把过期记录一起清掉，存储不会无限增长
    recordIntentBatch("session:new", ["b1"]);
    const raw = localStorage.getItem(INTENT_BATCH_STORAGE_KEY) ?? "";
    expect(raw).not.toContain("session:old");
    expect(raw).toContain("session:new");
  });

  it("旧格式（没有时间戳）仍可用：下一次写入补上时间戳，之后按 TTL 过期", () => {
    localStorage.setItem(INTENT_BATCH_STORAGE_KEY, JSON.stringify([{ id: "b0", key: "session:legacy" }]));
    // 旧版本写下的是有效记录，不因为缺时间戳就被丢掉（否则升级一次就丢一批）
    expect(batchKeyOf(batchItems(["b0"])[0])).toBe("session:legacy");
    // 写一次之后补上时间戳，从此按 TTL 过期；条数上限始终有效
    recordIntentBatch("session:new", ["b1"]);
    const parsed = JSON.parse(localStorage.getItem(INTENT_BATCH_STORAGE_KEY) ?? "{}") as {
      records: { id: string; key: string; at?: number }[];
    };
    expect(typeof parsed.records.find((record) => record.id === "b0")?.at).toBe("number");
  });

  it("读侧也有条数上限：存储被塞进超大内容时不会无限读取", () => {
    const records = Array.from({ length: 500 }, (_, index) => ({
      id: "h" + index,
      key: "session:bulk",
      at: Date.now(),
    }));
    localStorage.setItem(INTENT_BATCH_STORAGE_KEY, JSON.stringify({ version: 2, records }));
    // 前 400 条仍然可用；超出上限的部分退回「各自成批」，不会把不同批次混在一起
    expect(batchKeyOf(batchItems(["h0"])[0])).toBe("session:bulk");
    expect(batchKeyOf(batchItems(["h499"])[0])).toBe("intent:h499");
  });

  it("条数上限：写满后丢最旧的批，剩下的批次键仍然互不混淆", () => {
    for (let index = 0; index < 61; index += 1) {
      recordIntentBatch("session:g" + index, ["id" + index]);
    }
    // 最旧的一批被丢掉 → 退回「各自成批」；最近的一批仍然有效
    expect(batchKeyOf(batchItems(["id0"])[0])).toBe("intent:id0");
    expect(batchKeyOf(batchItems(["id60"])[0])).toBe("session:g60");
    const raw = localStorage.getItem(INTENT_BATCH_STORAGE_KEY) ?? "";
    const parsed = JSON.parse(raw) as { records: { id: string; key: string; at: number }[] };
    expect(parsed.records.length).toBeLessThanOrEqual(400);
    expect(new Set(parsed.records.map((record) => record.key)).size).toBeLessThanOrEqual(60);
  });
});

describe("批量列表资格：该批总量 ≥4，保留到处理完（契约 §9.3）", () => {
  beforeEach(clearBatchStorage);

  it("同批 4 项 → 处理 1 项仍在（3 项）→ 2 项 → 1 项仍在 → 0 项消失", () => {
    recordIntentBatch("session:demo-4", ["b0", "b1", "b2", "b3"]);
    const withStatuses = (statuses: IntentStatus[]) =>
      statuses.map((status, index) =>
        intent({ id: "b" + index, status, submissionId: null, createdAt: "2026-10-07T04:00:00.500Z" }),
      );

    let items = withStatuses(["pending", "pending", "pending", "pending"]);
    let batches = batchesWithList(items);
    expect(batches).toHaveLength(1);
    expect(batchRemainingCount(batches[0])).toBe(4);
    expect(batchHandledCount(batches[0])).toBe(0);

    items = withStatuses(["done", "pending", "pending", "pending"]);
    batches = batchesWithList(items);
    expect(batches).toHaveLength(1);
    expect(batchRemainingCount(batches[0])).toBe(3);
    expect(batchHandledCount(batches[0])).toBe(1);

    items = withStatuses(["done", "rejected", "pending", "pending"]);
    expect(batchRemainingCount(batchesWithList(items)[0])).toBe(2);

    items = withStatuses(["done", "rejected", "running", "pending"]);
    batches = batchesWithList(items);
    expect(batches).toHaveLength(1);
    expect(batchRemainingCount(batches[0])).toBe(1);

    items = withStatuses(["done", "rejected", "running", "failed"]);
    expect(batchesWithList(items)).toEqual([]);
  });

  it("反例：处理掉 1 项后入口与列表仍在（旧实现按剩余待审批 ≥4 会提前消失）", () => {
    recordIntentBatch("session:demo-4", ["b0", "b1", "b2", "b3"]);
    const items = [
      intent({ id: "b0", status: "done", submissionId: null }),
      intent({ id: "b1", status: "pending", submissionId: null }),
      intent({ id: "b2", status: "pending", submissionId: null }),
      intent({ id: "b3", status: "pending", submissionId: null }),
    ];
    const batches = batchesWithList(items);
    expect(batches).toHaveLength(1);
    expect(batches[0].intentIds).toEqual(["b0", "b1", "b2", "b3"]);
    expect(batches[0].pendingIds).toEqual(["b1", "b2", "b3"]);
  });

  it("反例：另一批 3 项 + 这批 1 项不会凑成 4 项列表", () => {
    recordIntentBatch("session:three", ["t0", "t1", "t2"]);
    recordIntentBatch("session:one", ["o0"]);
    const items = batchItems(["t0", "t1", "t2", "o0"]);
    expect(groupIntentsByBatch(items).map((batch) => batch.intentIds.length)).toEqual([3, 1]);
    expect(batchesWithList(items)).toEqual([]);
  });

  it("刷新（重新分组）后资格与处理状态保持：资格只看总量，不靠组件内临时标记", () => {
    recordIntentBatch("session:demo-4", ["b0", "b1", "b2", "b3"]);
    const items = [
      intent({ id: "b0", status: "done", submissionId: null }),
      intent({ id: "b1", status: "pending", submissionId: null }),
      intent({ id: "b2", status: "pending", submissionId: null }),
      intent({ id: "b3", status: "pending", submissionId: null }),
    ];
    const first = batchesWithList(items);
    const afterReload = batchesWithList([...items]);
    expect(afterReload).toHaveLength(1);
    expect(afterReload[0].key).toBe(first[0].key);
    expect(afterReload[0].intentIds).toEqual(first[0].intentIds);
    expect(batchRemainingCount(afterReload[0])).toBe(3);
  });

  it("已处理项不能再次被选中 / 提交", () => {
    recordIntentBatch("session:demo-4", ["b0", "b1", "b2", "b3"]);
    const items = [
      intent({ id: "b0", status: "done", submissionId: null }),
      intent({ id: "b1", status: "pending", submissionId: null }),
      intent({ id: "b2", status: "pending", submissionId: null }),
      intent({ id: "b3", status: "pending", submissionId: null }),
    ];
    const batch = batchesWithList(items)[0];
    expect(isBatchItemSelectable(batch, "b0")).toBe(false);
    expect(isBatchItemSelectable(batch, "b1")).toBe(true);
    expect(isBatchItemSelectable(batch, "不属于这批")).toBe(false);
    // 选中已处理项不生效；提交时被挡下并给出真实原因
    expect(toggleBatchSelectionIn(batch, [], "b0")).toEqual([]);
    const split = splitBatchIn(items, batch, ["b0", "b1"], "approve");
    expect(split.ids).toEqual(["b1"]);
    expect(split.blocked.map((item) => item.id)).toEqual(["b0"]);
  });

  it("入口文案显示总量、剩余待处理数与已处理数", () => {
    const batch = {
      key: "session:x",
      intentIds: ["b0", "b1", "b2", "b3"],
      pendingIds: ["b1", "b2", "b3"],
    };
    // 剩余待处理数写清楚，同时给出总量与已处理数（契约 §9.3）
    expect(batchEntryText(batch)).toContain("这一批待审批 3 项");
    expect(batchEntryText(batch)).toContain("共 4 项");
    expect(batchEntryText(batch)).toContain("已处理 1 项");
    expect(batchEntryText(batch, 1)).toContain("另有 1 项在其他批次等待");
  });
});

describe("批量列表：部分选择只影响选中项", () => {
  beforeEach(() => {
    clearBatchStorage();
    recordIntentBatch("session:partial", ["b0", "b1", "b2", "b3"]);
  });

  const items = () => [
    intent({ id: "b0", status: "pending", title: "合并材料", submissionId: null }),
    intent({ id: "b1", status: "pending", title: "分开整理", submissionId: null }),
    intent({ id: "b2", status: "needs_update", title: "需要更新", submissionId: null }),
    intent({ id: "b3", status: "pending", title: "后续步骤", submissionId: null }),
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
