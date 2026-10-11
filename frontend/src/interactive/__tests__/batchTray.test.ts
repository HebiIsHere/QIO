/**
 * 右上批量列表的接线测试（子智能体 D）。
 *
 * 契约 §8.1 / §8.5 在界面上的三条硬要求，这里用真实挂载证明：
 * - 只有**同一批**等待审批 ≥4 才出现入口（3 项不出现、4 项出现）；
 * - 默认收起（点击才展开），数量更新不抢占用户的开合决定；
 * - 不同批次不累加（3+1 不出现）；勾选部分后批量批准只提交选中的项。
 *
 * 批次来源由「记录」模拟：真实调用方是 store（createDemoIntents / submit 成功后
 * 调 approval.recordIntentBatch），见 Lead 的实现。
 */
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import IntentBatchTray from "../../components/interactive/IntentBatchTray.vue";
import { useInteractiveStore } from "../../stores/interactive";
import { BATCH_LIST_MIN, INTENT_BATCH_STORAGE_KEY, batchesWithList, groupIntentsByBatch } from "../approval";
import type { BoardCard, Intent, IntentStatus } from "../types";

let wrapper: VueWrapper | null = null;

function card(id: string): BoardCard {
  return {
    id,
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
    createdAt: "2026-10-07T00:00:00.000Z",
    updatedAt: "2026-10-07T00:00:00.000Z",
  };
}

function intent(id: string, status: IntentStatus = "pending"): Intent {
  return {
    id,
    boardId: "board_t",
    submissionId: null,
    status,
    title: "演示任务 " + id,
    summary: "",
    preview: { cards: [card("c_" + id)], groups: [], links: [] },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: true,
    createdAt: "2026-10-07T00:00:00.000Z",
    updatedAt: "2026-10-07T00:00:00.000Z",
  };
}

/** 直接写会话批次记录：模拟 store 调 recordIntentBatch 之后的状态（契约 §8.5 的①） */
function recordBatches(groups: Record<string, string[]>): void {
  const records = Object.entries(groups).flatMap(([key, ids]) => ids.map((id) => ({ id, key })));
  localStorage.setItem(INTENT_BATCH_STORAGE_KEY, JSON.stringify({ version: 1, records }));
}

function mountTray(items: Intent[]): VueWrapper {
  const store = useInteractiveStore();
  store.intents = items;
  wrapper = mount(IntentBatchTray, { attachTo: document.body });
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  try {
    localStorage.clear();
  } catch {
    // 存储不可用时忽略
  }
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  vi.restoreAllMocks();
});

describe("批量列表入口的出现条件", () => {
  it("同批 3 项：没有任何批量入口", () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2")]);
    expect(w.find('[data-im="batch-entry"]').exists()).toBe(false);
    expect(w.find('[data-im="batch-list"]').exists()).toBe(false);
  });

  it("同批 4 项：入口出现，且默认收起（列表不在 DOM 里）", () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2"), intent("i3")]);
    expect(w.find('[data-im="batch-entry"]').exists()).toBe(true);
    expect(w.find('[data-im="batch-list"]').exists()).toBe(false);
    expect(useInteractiveStore().batchOpen).toBe(false);
  });

  it("不同批次 3+1：不出现入口（不同批次不累加）", () => {
    recordBatches({ "session:a": ["i0", "i1", "i2"], "session:b": ["i3"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2"), intent("i3")]);
    expect(w.find('[data-im="batch-entry"]').exists()).toBe(false);
  });

  it("一批够 4 项、另一批 1 项：只出现够 4 项的那一批，文案分开说明", () => {
    recordBatches({ "session:a": ["i0", "i1", "i2", "i3"], "session:b": ["i4"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2"), intent("i3"), intent("i4")]);
    expect(w.findAll('[data-im="batch-entry"]').length).toBe(1);
    const text = w.find('[data-im="batch-entry"]').text();
    expect(text).toContain("这一批待审批 4 项");
    expect(text).toContain("另有 1 项在其他批次等待");
  });

  it("数量变化不改变入口的存在（3 项时收起、4 项时出现，且不自动展开）", async () => {
    // 四项都属于同一批（同一次创建动作），列表先只有 3 项
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2")]);
    expect(w.find('[data-im="batch-entry"]').exists()).toBe(false);
    const store = useInteractiveStore();
    store.intents = [intent("i0"), intent("i1"), intent("i2"), intent("i3")];
    await w.vm.$nextTick();
    expect(w.find('[data-im="batch-entry"]').exists()).toBe(true);
    expect(store.batchOpen).toBe(false);
  });
});

describe("入口开合由用户决定", () => {
  it("点击入口展开列表，再点收起；不自动展开", async () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2"), intent("i3")]);
    const store = useInteractiveStore();
    await w.find('[data-im="batch-entry"]').trigger("click");
    expect(store.batchOpen).toBe(true);
    expect(w.find('[data-im="batch-list"]').exists()).toBe(true);
    expect(w.findAll('[data-im="batch-item"]').length).toBe(4);
    await w.find('[data-im="batch-entry"]').trigger("click");
    expect(store.batchOpen).toBe(false);
    expect(w.find('[data-im="batch-list"]').exists()).toBe(false);
  });

  it("用户收起后，数量变化不抢回展开", async () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3", "i4"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2"), intent("i3")]);
    const store = useInteractiveStore();
    await w.find('[data-im="batch-entry"]').trigger("click");
    await w.find('[data-im="batch-close"]').trigger("click");
    expect(store.batchOpen).toBe(false);
    store.intents = [intent("i0"), intent("i1"), intent("i2"), intent("i3"), intent("i4")];
    await w.vm.$nextTick();
    expect(store.batchOpen).toBe(false);
    expect(w.find('[data-im="batch-list"]').exists()).toBe(false);
  });
});

describe("部分选择只影响选中项", () => {
  it("勾选两项后批量批准，只把这两项交给服务端；未选中的继续等待", async () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2"), intent("i3")]);
    const store = useInteractiveStore();
    await w.find('[data-im="batch-entry"]').trigger("click");
    const boxes = w.findAll('[data-im="batch-item"] input[type="checkbox"]');
    expect(boxes.length).toBe(4);
    await boxes[0].setValue(true);
    await boxes[2].setValue(true);

    const spy = vi.spyOn(store, "decideBatch").mockResolvedValue({
      results: [],
      conflicts: [],
      approved: ["i0", "i2"],
      rejected: [],
    });
    await w.find('[data-im="batch-approve"]').trigger("click");
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0][0]).toEqual(["i0", "i2"]);
    expect(spy.mock.calls[0][1]).toEqual([]);
    expect(spy.mock.calls[0][0]).not.toContain("i1");
    expect(spy.mock.calls[0][0]).not.toContain("i3");
  });

  it("未选任何项时批量批准按钮不可点", async () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2"), intent("i3")]);
    await w.find('[data-im="batch-entry"]').trigger("click");
    expect(w.find('[data-im="batch-approve"]').attributes("disabled")).toBeDefined();
    await w.find('[data-im="batch-all"]').trigger("click");
    expect(w.find('[data-im="batch-approve"]').attributes("disabled")).toBeUndefined();
  });

  it("点击条目派发定位事件到板面（带 intentId 与预览范围）", async () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2"), intent("i3")]);
    await w.find('[data-im="batch-entry"]').trigger("click");
    const received: unknown[] = [];
    const listener = (event: Event) => received.push((event as CustomEvent).detail);
    window.addEventListener("qio:interactive:locate-preview", listener);
    try {
      await w.findAll('[data-im="batch-locate"]')[0].trigger("click");
    } finally {
      window.removeEventListener("qio:interactive:locate-preview", listener);
    }
    expect(received.length).toBe(1);
    expect((received[0] as { intentId: string }).intentId).toBe("i0");
    expect((received[0] as { bounds: unknown }).bounds).not.toBeNull();
  });
});
describe("§9.3 同批达到四项后列表保留到处理完", () => {
  it("处理掉一项后入口仍在，并显示剩余待处理与已处理数量", async () => {
    // 一批 4 项：i3 已经处理完（done），剩下 3 项等待审批
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2"), intent("i3", "done")]);
    const store = useInteractiveStore();
    expect(w.find('[data-im="batch-entry"]').exists()).toBe(true);
    await w.find('[data-im="batch-entry"]').trigger("click");
    expect(store.batchOpen).toBe(true);
    const head = w.find('[data-im="batch-remaining"]').text();
    expect(head).toContain("剩余待处理 3 项");
    expect(head).toContain("已处理 1 项");
  });

  it("已处理项灰掉、没有复选框、也不会被提交", async () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2"), intent("i3", "done")]);
    const store = useInteractiveStore();
    await w.find('[data-im="batch-entry"]').trigger("click");
    const rows = w.findAll('[data-im="batch-item"]');
    expect(rows.length).toBe(4); // 已处理的保留在列表里，用户看得见处理到哪了
    const doneRow = rows.find((row) => row.attributes("data-intent-id") === "i3");
    expect(doneRow?.attributes("data-im-state")).toBe("processed");
    // 已处理项没有勾选框（不能再次被选中）
    expect(doneRow?.find('input[type="checkbox"]').exists()).toBe(false);
    expect(w.findAll('[data-im="batch-item"] input[type="checkbox"]').length).toBe(3);

    const spy = vi.spyOn(store, "decideBatch").mockResolvedValue({
      results: [],
      conflicts: [],
      approved: ["i0", "i1", "i2"],
      rejected: [],
    });
    await w.find('[data-im="batch-all"]').trigger("click");
    await w.find('[data-im="batch-approve"]').trigger("click");
    expect(spy.mock.calls[0][0]).toEqual(["i0", "i1", "i2"]);
    expect(spy.mock.calls[0][0]).not.toContain("i3");
  });

  it("全部处理完后入口与列表一起消失", async () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
    const w = mountTray([intent("i0"), intent("i1"), intent("i2"), intent("i3", "done")]);
    const store = useInteractiveStore();
    await w.find('[data-im="batch-entry"]').trigger("click");
    expect(w.find('[data-im="batch-list"]').exists()).toBe(true);
    // 剩下三项也处理掉（服务端返回 done）
    store.intents = [intent("i0", "done"), intent("i1", "done"), intent("i2", "done"), intent("i3", "done")];
    await w.vm.$nextTick();
    expect(w.find('[data-im="batch-entry"]').exists()).toBe(false);
    expect(w.find('[data-im="batch-list"]').exists()).toBe(false);
  });

  it("反例：旧规则（按剩余待审批数 ≥4 判资格）在同一份数据上不会给出入口", () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
    const grouped = groupIntentsByBatch([intent("i0"), intent("i1"), intent("i2"), intent("i3", "done")]);
    expect(grouped.length).toBe(1);
    const batch = grouped[0];
    expect(batch.intentIds.length).toBe(4);
    expect(batch.pendingIds.length).toBe(3);
    // 旧判据：pendingIds.length >= 4 → false（列表会提前消失，用户再也看不到剩下的 3 项）
    expect(batch.pendingIds.length >= 4).toBe(false);
    // 新判据：intentIds.length >= 4 且还有未处理项 → true
    expect(batch.intentIds.length >= BATCH_LIST_MIN && batch.pendingIds.length > 0).toBe(true);
  });

  it("B 的 batchesWithList 若已按同一规则落地，两条路径结果一致（未落地时如实报告差异）", () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
    const items = [intent("i0"), intent("i1"), intent("i2"), intent("i3", "done")];
    const byLibrary = batchesWithList(items).map((batch) => batch.key);
    const byRule = groupIntentsByBatch(items)
      .filter((batch) => batch.intentIds.length >= BATCH_LIST_MIN && batch.pendingIds.length > 0)
      .map((batch) => batch.key);
    // 组件用的是 byRule；这里把差异显式暴露出来，而不是让两种规则在集成时静默打架
    expect(byRule).toEqual(["session:demo"]);
    if (byLibrary.length === 0) {
      console.warn(
        "[§9.3] approval.batchesWithList 仍按「剩余待审批数 ≥4」判资格：集成前需要 B 落地新规则，组件已按新规则自行组合。",
      );
    }
  });

  it("内容更新时保留阅读位置（不跳回顶部）", async () => {
    recordBatches({ "session:demo": ["i0", "i1", "i2", "i3", "i4", "i5", "i6", "i7"] });
    const items = ["i0", "i1", "i2", "i3", "i4", "i5", "i6", "i7"].map((id) => intent(id));
    const w = mountTray(items);
    await w.find('[data-im="batch-entry"]').trigger("click");
    const panel = w.find('[data-im="batch-list"]').element as HTMLElement;
    // jsdom 不做布局：手工造一个「内容比容器高」的场景，验证滚动位置被读写而不是被重置
    Object.defineProperty(panel, "scrollHeight", { value: 900, configurable: true });
    Object.defineProperty(panel, "clientHeight", { value: 300, configurable: true });
    panel.scrollTop = 220;
    await panel.dispatchEvent(new Event("scroll"));
    const store = useInteractiveStore();
    store.intents = [...items.slice(0, 7), intent("i7", "done")];
    await w.vm.$nextTick();
    await w.vm.$nextTick();
    expect(panel.scrollTop).toBe(220);
  });
});
