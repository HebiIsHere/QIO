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
import { INTENT_BATCH_STORAGE_KEY } from "../approval";
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
